"""SQL 修复预算与路由。与模型、数据库无关，便于独立验证。"""

MAX_SQL_REPAIRS = 2


def route_after_validation(state):
    if state.get("error") is None:
        return "run_sql"
    return route_after_execution(state)


def route_after_execution(state):
    if state.get("error") is None:
        return "__end__"
    if (
        state.get("error_kind") == "repairable"
        and state.get("sql_repair_count", 0) < MAX_SQL_REPAIRS
    ):
        return "correct_sql"
    return "sql_failed"
