"""集中输出 SQL 闭环的最终失败，避免产生伪成功结果。"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState


async def sql_failed(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    kind = state.get("error_kind", "execution")
    if kind == "policy":
        message = f"查询未执行：{state['error']}"
    elif kind == "repairable":
        message = "SQL 在有限次修正后仍未通过，请补充问题条件后重试。"
    else:
        message = "数据服务暂时不可用或查询超时，请稍后重试。"
    runtime.stream_writer(
        {
            "type": "error",
            "message": message,
            "code": kind,
            "sql_repair_count": state.get("sql_repair_count", 0),
        }
    )
    return {"query_status": "failed"}
