"""运行真实 LangGraph 拓扑、校验/执行/修正节点；替换外部模型与数据库。"""

import importlib
import unittest
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda

from app.agent.graph import build_graph
from app.agent.nodes.run_sql import run_sql
from app.agent.sql_policy import SQLPolicy, SQLPolicyError, SQLValidationError


class FakeDW:
    policy = SQLPolicy(frozenset({"fact_order"}))

    def __init__(self, validation_errors=(), execution_errors=()):
        self.validation_errors = iter(validation_errors)
        self.execution_errors = iter(execution_errors)
        self.calls = []

    async def validate(self, sql):
        self.calls.append("validate")
        error = next(self.validation_errors, None)
        if error:
            raise error
        return sql

    async def run(self, sql):
        self.calls.append("run")
        error = next(self.execution_errors, None)
        if error:
            raise error
        return [{"amount": 12}]


class SQLLoopTests(unittest.IsolatedAsyncioTestCase):
    async def execute_graph(self, repo):
        async def noop(state):
            return {}

        async def generate(state):
            return {
                "sql": "SELECT SUM(order_amount) FROM fact_order",
                "sql_repair_count": 0,
                "table_infos": [],
                "metric_infos": [],
                "date_info": {},
                "db_info": {},
            }

        overrides = {
            name: noop
            for name in [
                "extract_keywords",
                "recall_column",
                "recall_value",
                "recall_metric",
                "merge_retrieved_info",
                "filter_metric",
                "filter_table",
                "add_extra_context",
            ]
        }
        overrides["generate_sql"] = generate
        # 使用真实 correct_sql 的计数更新，避免测试替身替实现证明自身。
        module = importlib.import_module("app.agent.nodes.correct_sql")
        fake_llm = RunnableLambda(
            lambda prompt: "SELECT SUM(order_amount) FROM fact_order"
        )
        with patch.object(module, "llm", fake_llm):
            result = await build_graph(overrides).ainvoke(
                {"query": "销售额"}, context={"dw_mysql_repository": repo}
            )
        return result

    async def test_success_without_repair(self):
        repo = FakeDW()
        result = await self.execute_graph(repo)
        self.assertEqual(repo.calls, ["validate", "run"])
        self.assertEqual(result["query_status"], "success")
        self.assertEqual(result["result"], [{"amount": 12}])

    async def test_repair_must_pass_validation_again(self):
        repo = FakeDW([SQLValidationError("unknown column")])
        result = await self.execute_graph(repo)
        self.assertEqual(repo.calls, ["validate", "validate", "run"])
        self.assertEqual(result["sql_repair_count"], 1)

    async def test_budget_exhaustion_never_executes(self):
        repo = FakeDW([SQLValidationError("bad sql")] * 5)
        result = await self.execute_graph(repo)
        self.assertEqual(repo.calls, ["validate"] * 3)
        self.assertEqual(result["query_status"], "failed")
        self.assertEqual(result["sql_repair_count"], 2)

    async def test_execution_error_also_goes_through_validation(self):
        repo = FakeDW(execution_errors=[SQLValidationError("unknown column")])
        result = await self.execute_graph(repo)
        self.assertEqual(repo.calls, ["validate", "run", "validate", "run"])
        self.assertEqual(result["query_status"], "success")

    async def test_policy_and_service_errors_stop_without_repair(self):
        for error in [
            SQLPolicyError("not allowed"),
            TimeoutError("slow"),
            ConnectionError("offline"),
        ]:
            with self.subTest(error=type(error).__name__):
                repo = FakeDW([error])
                result = await self.execute_graph(repo)
                self.assertEqual(repo.calls, ["validate"])
                self.assertEqual(result["query_status"], "failed")
                self.assertEqual(result["sql_repair_count"], 0)

    async def test_changed_sql_cannot_execute(self):
        from types import SimpleNamespace

        repo = FakeDW()
        runtime = SimpleNamespace(
            context={"dw_mysql_repository": repo}, stream_writer=lambda event: None
        )
        result = await run_sql(
            {"sql": "SELECT 2", "validated_sql": "SELECT 1"}, runtime
        )
        self.assertEqual(result["error_kind"], "policy")
        self.assertEqual(repo.calls, [])


if __name__ == "__main__":
    unittest.main()
