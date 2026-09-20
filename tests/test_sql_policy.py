import sqlite3
import unittest
from unittest.mock import AsyncMock

from sqlalchemy.exc import OperationalError, ProgrammingError

from app.agent.sql_policy import (
    SQLPolicy,
    SQLPolicyError,
    SQLValidationError,
    prepare_read_query,
)
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

# 来自实际失败场景的查询结构；测试使用独立的小数据，不修改冻结保留集答案。
NOT_EXISTS_QUERY = """
SELECT DISTINCT o.customer_id AS 客户编号
FROM fact_order o
JOIN dim_product p ON o.product_id = p.product_id
JOIN dim_region r ON o.region_id = r.region_id
WHERE o.date_id BETWEEN 20250601 AND 20250630
  AND r.region_name = '华北'
  AND p.category = '手机数码'
  AND NOT EXISTS (
      SELECT 1
      FROM fact_order o2
      JOIN dim_product p2 ON o2.product_id = p2.product_id
      JOIN dim_region r2 ON o2.region_id = r2.region_id
      WHERE o2.customer_id = o.customer_id
        AND o2.date_id BETWEEN 20250701 AND 20250731
        AND r2.region_name = '华北'
        AND p2.category = '手机数码'
  )
"""


class SQLPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SQLPolicy(frozenset({"fact_order", "dim_region"}))

    def test_aggregate_and_cap(self):
        sql = prepare_read_query(
            "SELECT SUM(order_amount) AS amount FROM fact_order", self.policy
        )
        self.assertIn("SUM(order_amount)", sql)
        self.assertTrue(sql.endswith("LIMIT 200"))

    def test_preserves_smaller_limit_and_offset(self):
        self.assertTrue(
            prepare_read_query(
                "SELECT * FROM fact_order LIMIT 5 OFFSET 10", self.policy
            ).endswith("LIMIT 5 OFFSET 10")
        )

    def test_caps_large_limit(self):
        self.assertTrue(
            prepare_read_query(
                "SELECT * FROM fact_order LIMIT 10000", self.policy
            ).endswith("LIMIT 200")
        )

    def test_cte_and_union(self):
        for sql in [
            "WITH orders AS (SELECT * FROM fact_order) SELECT * FROM orders",
            "SELECT region_id FROM fact_order UNION SELECT region_id FROM dim_region",
            "SELECT * FROM dw.fact_order",
        ]:
            with self.subTest(sql=sql):
                self.assertIn("LIMIT 200", prepare_read_query(sql, self.policy))

    def test_rejects_unsafe_statements(self):
        for sql in [
            "DELETE FROM fact_order",
            "DROP TABLE fact_order",
            "UPDATE fact_order SET order_amount=0",
            "SELECT 1; DELETE FROM fact_order",
            "SELECT * FROM mysql.user",
            "SELECT * FROM meta.fact_order",
            "SELECT * FROM secrets",
            "SELECT SLEEP(1)",
            "SELECT GET_LOCK('x',1)",
            "SELECT LOAD_FILE('/tmp/x')",
            "SELECT * FROM fact_order FOR UPDATE",
            "SELECT @x:=1",
            "SELECT @@version",
            "SELECT /*+ MAX_EXECUTION_TIME(0) */ * FROM fact_order",
            "WITH RECURSIVE q AS (SELECT 1) SELECT * FROM q",
            "WITH secrets AS (SELECT * FROM secrets) SELECT * FROM secrets",
            "SELECT * FROM fact_order LIMIT -1",
        ]:
            with self.subTest(sql=sql), self.assertRaises(SQLPolicyError):
                prepare_read_query(sql, self.policy)

    def test_strips_executable_comments(self):
        checked = prepare_read_query(
            "SELECT 1 /*!50000 INTO OUTFILE '/tmp/x' */", self.policy
        )
        self.assertNotIn("OUTFILE", checked)
        self.assertNotIn("/*", checked)

    def test_strings_containing_sql_keywords_are_data(self):
        checked = prepare_read_query(
            "SELECT 'DELETE; DROP' AS note FROM fact_order", self.policy
        )
        self.assertIn("'DELETE; DROP'", checked)

    def test_parse_failure_is_repairable(self):
        with self.assertRaises(SQLValidationError):
            prepare_read_query("SELECT ( FROM fact_order", self.policy)

    def test_preparation_is_idempotent(self):
        checked = prepare_read_query(
            "SELECT SUM(order_amount) FROM fact_order", self.policy
        )
        self.assertEqual(prepare_read_query(checked, self.policy), checked)

    def test_filters_join_and_window_query(self):
        for sql in [
            "SELECT r.region_name, SUM(o.order_amount) FROM fact_order o JOIN dim_region r ON o.region_id=r.region_id WHERE o.date_id BETWEEN 20250101 AND 20250331 AND r.region_name='华北' GROUP BY r.region_name",
            "SELECT order_id, ROW_NUMBER() OVER (ORDER BY order_amount DESC) AS ranking FROM fact_order",
            "SELECT * FROM fact_order WHERE region_id='R001' OR region_id='R002'",
        ]:
            with self.subTest(sql=sql):
                self.assertTrue(
                    prepare_read_query(sql, self.policy).endswith("LIMIT 200")
                )

    def test_correlated_exists_and_not_exists_preserve_cohort_semantics(self):
        policy = SQLPolicy(frozenset({"fact_order", "dim_product", "dim_region"}))
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE dim_product (product_id TEXT, category TEXT);
                CREATE TABLE dim_region (region_id TEXT, region_name TEXT);
                CREATE TABLE fact_order (
                    customer_id TEXT, product_id TEXT, region_id TEXT, date_id INTEGER
                );
                INSERT INTO dim_product VALUES ('P1','手机数码'),('P2','食品饮料');
                INSERT INTO dim_region VALUES ('R1','华北'),('R2','华南');
                INSERT INTO fact_order VALUES
                    ('C1','P1','R1',20250601),
                    ('C1','P1','R1',20250630),
                    ('C1','P1','R2',20250701),
                    ('C1','P2','R1',20250731),
                    ('C2','P1','R1',20250630),
                    ('C2','P1','R1',20250701),
                    ('C3','P1','R2',20250601);
            """)
            # C1 七月在别的地区、别的品类的购买都不能被算作目标复购。
            for sql, expected in (
                (NOT_EXISTS_QUERY, [("C1",)]),
                (NOT_EXISTS_QUERY.replace("NOT EXISTS", "EXISTS"), [("C2",)]),
            ):
                with self.subTest(expected=expected):
                    checked = prepare_read_query(sql, policy)
                    self.assertEqual(connection.execute(checked).fetchall(), expected)
                    self.assertTrue(checked.endswith("LIMIT 200"))
                    self.assertEqual(prepare_read_query(checked, policy), checked)
        finally:
            connection.close()

    def test_exists_can_reference_ctes_and_nested_read_queries(self):
        queries = [
            "WITH recent AS (SELECT customer_id FROM fact_order) "
            "SELECT o.customer_id FROM fact_order o WHERE EXISTS "
            "(SELECT 1 FROM recent r WHERE r.customer_id=o.customer_id)",
            "SELECT EXISTS (SELECT 1 FROM fact_order WHERE NOT EXISTS "
            "(SELECT 1 FROM dim_region)) AS found",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertTrue(
                    prepare_read_query(sql, self.policy).endswith("LIMIT 200")
                )

    def test_exists_does_not_bypass_subquery_policy(self):
        subqueries = [
            "SELECT 1 FROM mysql.user",
            "SELECT 1 FROM meta.fact_order",
            "SELECT 1 FROM secrets",
            "SELECT 1 FROM fact_order WHERE SLEEP(1)=0",
            "SELECT LOAD_FILE('/tmp/x') FROM fact_order",
            "SELECT 1 FROM fact_order FOR UPDATE",
            "SELECT @x:=1 FROM fact_order",
            "WITH fact_order AS (SELECT * FROM mysql.user) SELECT 1 FROM fact_order",
            "SELECT 1 FROM fact_order WHERE EXISTS (SELECT 1 FROM mysql.user)",
        ]
        for predicate in ("EXISTS", "NOT EXISTS"):
            for subquery in subqueries:
                with self.subTest(predicate=predicate, subquery=subquery):
                    with self.assertRaises(SQLPolicyError):
                        prepare_read_query(
                            f"SELECT customer_id FROM fact_order WHERE {predicate} ({subquery})",
                            self.policy,
                        )


class RepositoryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_sql_never_reaches_database(self):
        session = AsyncMock()
        repo = DWMySQLRepository(session, SQLPolicy(frozenset({"fact_order"})))
        with self.assertRaises(SQLPolicyError):
            await repo.run("DELETE FROM fact_order")
        session.execute.assert_not_called()

    async def test_unsafe_exists_never_reaches_database(self):
        session = AsyncMock()
        repo = DWMySQLRepository(session, SQLPolicy(frozenset({"fact_order"})))
        with self.assertRaises(SQLPolicyError):
            await repo.run(
                "SELECT customer_id FROM fact_order WHERE NOT EXISTS "
                "(SELECT 1 FROM mysql.user)"
            )
        session.execute.assert_not_called()

    async def test_database_sql_errors_are_repairable(self):
        session = AsyncMock()
        session.execute.side_effect = [
            None,
            ProgrammingError("sql", {}, Exception(1054, "Unknown column")),
        ]
        repo = DWMySQLRepository(session, SQLPolicy(frozenset({"fact_order"})))
        with self.assertRaises(SQLValidationError):
            await repo.validate("SELECT missing FROM fact_order")

    async def test_connection_errors_are_not_repairable(self):
        session = AsyncMock()
        session.execute.side_effect = OperationalError(
            "sql", {}, Exception(2003, "Cannot connect")
        )
        repo = DWMySQLRepository(session, SQLPolicy(frozenset({"fact_order"})))
        with self.assertRaises(OperationalError):
            await repo.validate("SELECT * FROM fact_order")


if __name__ == "__main__":
    unittest.main()
