"""数据扩充质量与防覆盖检查；只用内存数据库，不连接线上服务。"""

import copy
import json
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.evaluation.warehouse_fixture import (
    ROOT,
    TABLE_COLUMNS,
    content_hash,
    expanded_connection,
    expected_rows,
    extension_rows,
    manifest,
    original_rows,
)
from app.scripts.extend_warehouse import check_offline, pending_rows, write_private_json


class WarehouseFixtureTests(unittest.TestCase):
    def test_fixture_matches_frozen_version_and_is_reproducible(self):
        frozen = json.loads(
            (ROOT / "evals/fixtures/warehouse_extension_v1_manifest.json").read_text()
        )
        self.assertEqual(content_hash(extension_rows()), frozen["extension_sha256"])
        self.assertEqual(manifest()["final_counts"], frozen["final_counts"])
        self.assertEqual(extension_rows(), extension_rows())

    def test_original_q1_rows_preserved_and_no_new_orders_in_that_period(self):
        original, combined = original_rows(), expected_rows()
        for table, columns in TABLE_COLUMNS.items():
            by_key = {row[columns[0]]: row for row in combined[table]}
            for row in original[table]:
                self.assertEqual(by_key[row[columns[0]]], row)
        q1 = [
            row
            for row in combined["fact_order"]
            if 20250101 <= row["date_id"] <= 20250331
        ]
        self.assertEqual(len(q1), 115)
        self.assertEqual(sum(row["order_amount"] for row in q1), 279159.5)

    def test_keys_references_calendar_boundaries_and_nullable_measures(self):
        rows = expected_rows()
        for table, columns in TABLE_COLUMNS.items():
            self.assertEqual(
                len(rows[table]), len({row[columns[0]] for row in rows[table]})
            )
        dates = {row["date_id"] for row in rows["dim_date"]}
        self.assertEqual(len(dates), 731)
        self.assertIn(20240229, dates)
        for row in rows["dim_date"]:
            self.assertEqual(
                int(date(row["year"], row["month"], row["day"]).strftime("%Y%m%d")),
                row["date_id"],
            )
            self.assertEqual(row["quarter"], f"Q{(row['month'] - 1) // 3 + 1}")
        orders = rows["fact_order"]
        self.assertEqual(len({row["date_id"] // 100 for row in orders}), 24)
        self.assertEqual(min(row["date_id"] for row in orders), 20240101)
        self.assertEqual(max(row["date_id"] for row in orders), 20251231)
        self.assertTrue(any(row["date_id"] == 20240229 for row in orders))
        for table in ("dim_region", "dim_customer", "dim_product", "dim_date"):
            column = TABLE_COLUMNS[table][0]
            valid = {row[column] for row in rows[table]}
            self.assertTrue(all(row[column] in valid for row in orders))
        self.assertTrue(all(row["order_quantity"] > 0 for row in orders))
        self.assertTrue(
            all(
                row["order_amount"] is None or row["order_amount"] >= 0
                for row in orders
            )
        )
        self.assertEqual(sum(row["order_amount"] is None for row in orders), 2)
        self.assertEqual(sum(row["order_amount"] == 0 for row in orders), 10)

    def test_hand_reviewed_scenario_answers(self):
        self.assertEqual(check_offline(), 7)

    def test_q1_east_monthly_baseline_still_matches_previous_smoke(self):
        connection = expanded_connection()
        try:
            actual = [
                tuple(row)
                for row in connection.execute(
                    "SELECT d.month,SUM(o.order_amount) FROM fact_order o JOIN dim_date d ON d.date_id=o.date_id JOIN dim_region r ON r.region_id=o.region_id WHERE r.region_name='华东' AND d.year=2025 AND d.quarter='Q1' GROUP BY d.month ORDER BY d.month"
                )
            ]
            self.assertEqual(actual, [(1, 54924.0), (2, 17508.0), (3, 34941.0)])
        finally:
            connection.close()

    def test_repeat_apply_plan_is_empty(self):
        before = pending_rows(original_rows())
        self.assertEqual(len(before["fact_order"]), 9725)
        self.assertEqual(len(before["dim_date"]), 641)
        self.assertFalse(any(pending_rows(expected_rows()).values()))

    def test_existing_changes_and_foreign_rows_are_rejected_not_overwritten(self):
        for table in ("fact_order", "dim_product", "dim_date"):
            current = copy.deepcopy(original_rows())
            column = TABLE_COLUMNS[table][-1]
            current[table][0][column] = "changed"
            with (
                self.subTest(table=table),
                self.assertRaisesRegex(ValueError, "未覆盖"),
            ):
                pending_rows(current)
        current = original_rows()
        current["fact_order"].append(
            {**current["fact_order"][0], "order_id": "OTHER_DATA"}
        )
        with self.assertRaises(ValueError):
            pending_rows(current)

    def test_missing_original_or_changed_extension_is_rejected(self):
        current = original_rows()
        current["fact_order"].pop()
        with self.assertRaisesRegex(ValueError, "缺少原始"):
            pending_rows(current)
        current = expected_rows()
        current["fact_order"][0]["order_amount"] = -1
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            pending_rows(current)

    def test_backup_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "before.json"
            write_private_json(path, {"amount": 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                write_private_json(path, {"amount": 2})
            self.assertEqual(json.loads(path.read_text()), {"amount": 1})
