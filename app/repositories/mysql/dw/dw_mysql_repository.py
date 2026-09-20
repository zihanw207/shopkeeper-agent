"""
数仓 MySQL 仓储

这一层对应文档里的 DW Repository，职责是到真实数仓中补齐配置文件里
没有显式维护的信息，例如字段类型和字段示例值。Service 层只关心
“需要哪些信息”，具体怎样查数仓由仓储层统一封装
SQL 生成闭环中的数据库环境读取 SQL 校验和最终查询执行也集中放在这里
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sql_policy import (
    SQLPolicy,
    SQLValidationError,
    default_sql_policy,
    prepare_read_query,
)


class DWMySQLRepository:
    """负责查询数仓真实表结构和字段样例值"""

    def __init__(self, session: AsyncSession, policy: SQLPolicy | None = None):
        self.session = session
        self.policy = policy or default_sql_policy()

    async def _execute_query(self, sql: str):
        """同时设置服务端 SELECT 时限与客户端等待时限。"""
        try:
            async with asyncio.timeout(self.policy.timeout_seconds + 1):
                milliseconds = int(self.policy.timeout_seconds * 1000)
                await self.session.execute(
                    text(f"SET SESSION MAX_EXECUTION_TIME = {milliseconds}")
                )
                return await self.session.execute(text(sql))
        except DBAPIError as error:
            code = error.orig.args[0] if getattr(error.orig, "args", ()) else None
            if code in {1052, 1054, 1055, 1064, 1111, 1140, 1146}:
                # 不把连接故障、鉴权失败或超时当成 SQL 错误交给模型重复修正。
                detail = (
                    str(error.orig.args[1])[:1000] if len(error.orig.args) > 1 else ""
                )
                raise SQLValidationError(f"MySQL {code}: {detail}") from error
            raise

    async def get_column_types(self, table_name: str) -> dict[str, str]:
        """查询整张表的字段类型，作为 ColumnInfo.type 的真实来源"""
        sql = f"show columns from {table_name}"
        result = await self.session.execute(text(sql))
        result_dict = result.mappings().fetchall()
        return {row["Field"]: row["Type"] for row in result_dict}

    async def get_column_values(
        self, table_name: str, column_name: str, limit: int = 10
    ) -> list:
        """抽样查询字段示例值，供元数据入库和后续检索链路复用"""
        sql = f"select distinct {column_name} from {table_name} limit {limit}"
        result = await self.session.execute(text(sql))
        return [row[0] for row in result.fetchall()]

    async def get_db_info(self):
        """读取当前数仓数据库的方言和版本，供 SQL 生成提示词使用"""

        sql = "select version()"
        result = await self.session.execute(text(sql))
        version = result.scalar()

        # dialect 来自 SQLAlchemy 当前绑定的数据库方言，例如 mysql
        dialect = self.session.bind.dialect.name
        return {"dialect": dialect, "version": version}

    async def validate(self, sql: str) -> str:
        """用 EXPLAIN 让数据库提前解析 SQL，发现语法 表名 字段名等错误"""
        checked_sql = prepare_read_query(sql, self.policy)
        await self._execute_query(f"EXPLAIN {checked_sql}")
        return checked_sql

    async def run(self, sql: str) -> list[dict]:
        """执行最终 SQL，并把 SQLAlchemy 行对象转换成前端更易消费的字典列表"""
        checked_sql = prepare_read_query(sql, self.policy)
        result = await self._execute_query(checked_sql)
        return [dict(row) for row in result.mappings().fetchall()]
