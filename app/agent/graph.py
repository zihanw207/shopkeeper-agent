"""
电商问数 Agent 图编排

使用 LangGraph 把问数智能体的各个节点串成一条可观测的执行链路
当前链路已经落地关键词抽取和多路召回，字段和指标走 Qdrant 向量检索，字段取值走 ES 全文检索
整体流程先抽取用户问题关键词，再并行召回字段 字段取值和指标信息，
随后合并召回结果 过滤候选表和指标 补充额外上下文，最后生成 校验 修正并执行 SQL
"""

import asyncio

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.filter_metric import filter_metric
from app.agent.nodes.filter_table import filter_table
from app.agent.nodes.generate_sql import generate_sql
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.nodes.run_sql import run_sql
from app.agent.nodes.sql_failed import sql_failed
from app.agent.nodes.validate_sql import validate_sql
from app.agent.sql_routing import route_after_execution, route_after_validation
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository


def build_graph(node_overrides: dict | None = None):
    """构建同一张生产图；测试可替换模型节点，不复制路由逻辑。"""
    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    nodes = {
        "extract_keywords": extract_keywords,
        "recall_column": recall_column,
        "recall_value": recall_value,
        "recall_metric": recall_metric,
        "merge_retrieved_info": merge_retrieved_info,
        "filter_metric": filter_metric,
        "filter_table": filter_table,
        "add_extra_context": add_extra_context,
        "generate_sql": generate_sql,
        "validate_sql": validate_sql,
        "correct_sql": correct_sql,
        "run_sql": run_sql,
        "sql_failed": sql_failed,
    }
    nodes.update(node_overrides or {})
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "extract_keywords")
    recall_nodes = ["recall_column", "recall_value", "recall_metric"]
    for name in recall_nodes:
        builder.add_edge("extract_keywords", name)
    builder.add_edge(recall_nodes, "merge_retrieved_info")
    for name in ["filter_table", "filter_metric"]:
        builder.add_edge("merge_retrieved_info", name)
    builder.add_edge(["filter_table", "filter_metric"], "add_extra_context")
    builder.add_edge("add_extra_context", "generate_sql")
    builder.add_edge("generate_sql", "validate_sql")
    builder.add_conditional_edges(
        "validate_sql",
        route_after_validation,
        {name: name for name in ["run_sql", "correct_sql", "sql_failed"]},
    )
    # 每次修正都必须重新检查，且校验与执行共用同一修正预算。
    builder.add_edge("correct_sql", "validate_sql")
    builder.add_conditional_edges(
        "run_sql",
        route_after_execution,
        {
            "correct_sql": "correct_sql",
            "sql_failed": "sql_failed",
            END: END,
        },
    )
    builder.add_edge("sql_failed", END)
    return builder.compile()


graph = build_graph()

# print(graph.get_graph().draw_mermaid())

if __name__ == "__main__":

    async def test():
        """本地调试关键词抽取和字段 指标 取值三路召回链路"""

        # 多路召回和上下文补全会访问 Qdrant、Embedding、ES、Meta MySQL 和 DW MySQL
        qdrant_client_manager.init()
        embedding_client_manager.init()
        es_client_manager.init()
        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()

        # Meta MySQL 用来补齐元数据，DW MySQL 用来读取数据库方言和版本
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            meta_mysql_repository = MetaMySQLRepository(meta_session)
            dw_mysql_repository = DWMySQLRepository(dw_session)

            # 字段和指标分别使用不同 Qdrant collection，取值检索使用 ES index
            column_qdrant_repository = ColumnQdrantRepository(
                qdrant_client_manager.client
            )
            metric_qdrant_repository = MetricQdrantRepository(
                qdrant_client_manager.client
            )
            value_es_repository = ValueESRepository(es_client_manager.client)

            # 当前只需要传入原始问题，后续节点会逐步写回召回、过滤和额外上下文结果
            state = DataAgentState(query="统计华北地区的销售总额")
            context = DataAgentContext(
                column_qdrant_repository=column_qdrant_repository,
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=metric_qdrant_repository,
                value_es_repository=value_es_repository,
                meta_mysql_repository=meta_mysql_repository,
                dw_mysql_repository=dw_mysql_repository,
            )

            # stream_mode="custom" 会接收各节点通过 runtime.stream_writer 写出的进度信息
            async for chunk in graph.astream(
                input=state, context=context, stream_mode="custom"
            ):
                print(chunk)

        # 关闭显式创建的异步客户端，避免本地调试时连接资源悬挂
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()

    asyncio.run(test())
