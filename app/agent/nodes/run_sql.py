"""
SQL 执行节点

负责执行最终 SQL，并记录查询结果。
它是当前 SQL 闭环的结束节点，执行完成后流程进入 END。
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.sql_policy import SQLPolicyError, SQLValidationError
from app.agent.state import DataAgentState
from app.core.log import logger


async def run_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """执行 SQL 并产出最终问数结果"""

    writer = runtime.stream_writer
    step = "执行SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 这里拿到的可能是 generate_sql 直接通过校验的 SQL，也可能是 correct_sql 覆盖后的 SQL
        sql = state["sql"]
        if state.get("validated_sql") != sql:
            raise SQLPolicyError("SQL 尚未通过校验，或在校验后被修改")
        dw_mysql_repository = runtime.context["dw_mysql_repository"]

        # 真实数据库访问统一封装在仓储层，节点只负责从状态取 SQL 并触发执行
        result = await dw_mysql_repository.run(sql)
        logger.info(f"SQL执行结果：{result}")
        writer({"type": "progress", "step": step, "status": "success"})
        writer(
            {
                "type": "result",
                "data": result,
                "sql": sql,
                "max_rows": dw_mysql_repository.policy.max_rows,
                "sql_repair_count": state.get("sql_repair_count", 0),
            }
        )
        return {
            "result": result,
            "error": None,
            "error_kind": None,
            "query_status": "success",
        }

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        kind = (
            "repairable"
            if isinstance(e, SQLValidationError)
            else "policy"
            if isinstance(e, SQLPolicyError)
            else "execution"
        )
        return {"error": str(e), "error_kind": kind, "validated_sql": None}
