"""模型 SQL 的应用层检查；数据库只读账号仍是部署时的必要隔离层。"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from sqlglot import ErrorLevel, exp, parse
from sqlglot.errors import ParseError, UnsupportedError
from sqlglot.optimizer.scope import traverse_scope


class SQLPolicyError(ValueError):
    """超出允许的数据访问范围，直接停止，不交给模型反复尝试。"""


class SQLValidationError(ValueError):
    """SQL 语法或字段使用有误，可以在次数预算内修正。"""


@dataclass(frozen=True)
class SQLPolicy:
    allowed_tables: frozenset[str]
    database: str = "dw"
    max_rows: int = 200
    timeout_seconds: float = 10


@lru_cache(maxsize=1)
def default_sql_policy() -> SQLPolicy:
    path = Path(__file__).resolve().parents[2] / "conf" / "meta_config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SQLPolicy(frozenset(table["name"] for table in config["tables"]))


# 使用允许列表，避免 SELECT 中的存储函数、文件读取、休眠与锁操作。
# 添加新函数前应补测试并确认 MySQL 中没有写入或外部访问副作用。
# SQLGlot 也将只读谓词 EXISTS 归为 Func；子查询仍经过全树和作用域检查。
ALLOWED_FUNCTIONS = frozenset(
    "ABS AVG SUM COUNT MIN MAX ROUND CEIL FLOOR CAST COALESCE NULLIF IF CASE "
    "YEAR MONTH DAY QUARTER DATE DATE_ADD DATE_SUB DATE_DIFF CURRENT_DATE "
    "CURRENT_TIMESTAMP TIME_TO_STR STR_TO_DATE CONCAT CONCAT_WS LOWER UPPER "
    "TRIM SUBSTRING LENGTH CHAR_LENGTH EXTRACT ROW_NUMBER RANK DENSE_RANK "
    "LAG LEAD FIRST_VALUE LAST_VALUE STDDEV STDDEV_POP STDDEV_SAMP "
    "VARIANCE VARIANCE_POP MOD POW SQRT AND OR NOT EXISTS".split()
)


def prepare_read_query(sql: str, policy: SQLPolicy) -> str:
    """解析后检查，再重新生成将要执行的 SQL，绝不执行原始未检查文本。"""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 20_000:
        raise SQLPolicyError("SQL 为空或超过长度上限")
    if policy.max_rows < 1 or policy.timeout_seconds <= 0:
        raise ValueError("SQL 资源限制必须为正数")
    try:
        statements = [statement for statement in parse(sql, read="mysql") if statement]
    except ParseError as error:
        raise SQLValidationError("SQL 解析失败，请检查语法并只输出 SQL") from error
    if len(statements) != 1:
        raise SQLPolicyError("只允许执行一条查询语句")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SQLPolicyError("只允许 SELECT 或 UNION 查询")
    for node in tree.walk():
        if isinstance(
            node,
            (
                exp.DML,
                exp.DDL,
                exp.Command,
                exp.Into,
                exp.Lock,
                exp.Parameter,
                exp.SessionParameter,
                exp.Hint,
            ),
        ):
            raise SQLPolicyError("查询包含写入、锁、变量或自定义执行提示")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLPolicyError("当前不支持递归查询")
        if isinstance(node, exp.Func) and node.sql_name() not in ALLOWED_FUNCTIONS:
            raise SQLPolicyError(f"当前未开放函数：{node.sql_name()}")
    # 按作用域解析 CTE，不能简单把所有 CTE 名称加进表允许列表。
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                if (
                    not isinstance(source.this, exp.Identifier)
                    or source.catalog
                    or source.db not in ("", policy.database)
                    or source.name not in policy.allowed_tables
                ):
                    raise SQLPolicyError("查询引用了未开放的数据表或数据库")
    for query in tree.find_all(exp.Select, exp.Union):
        for clause in ("limit", "offset"):
            value = query.args.get(clause)
            if value is not None:
                value = value.expression
                if (
                    not isinstance(value, exp.Literal)
                    or not value.is_int
                    or int(value.this) < 0
                ):
                    raise SQLPolicyError("LIMIT 和 OFFSET 必须为非负整数")
    limit = tree.args.get("limit")
    requested_rows = (
        int(limit.expression.this) if limit is not None else policy.max_rows
    )
    tree = tree.limit(min(requested_rows, policy.max_rows))
    try:
        # 删除注释，防止 MySQL 可执行注释原样到达数据库。
        return tree.sql(
            dialect="mysql", comments=False, unsupported_level=ErrorLevel.RAISE
        )
    except UnsupportedError as error:
        raise SQLPolicyError("当前不支持该 SQL 结构") from error
