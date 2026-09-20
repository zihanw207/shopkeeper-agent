"""保留集的答案、判分及采集隔离检查；不调用模型或外部服务。"""

import copy
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx

from app.evaluation.holdout import (
    SUITE,
    check_fixture,
    compare_table,
    grade_turn,
    load_suite,
    parse_events,
    query_payload,
    select_cases,
)
from app.evaluation.warehouse_fixture import expected_rows
from app.scripts.evaluate_holdout import collect


class HoldoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite, cls.goldens, cls.manifest = load_suite()
        cls.turns = {t["id"]: t for c in cls.suite["cases"] for t in c["turns"]}

    def test_frozen_references_and_mutants(self):
        self.assertEqual(
            check_fixture(self.suite, self.goldens),
            {
                "references_checked": 57,
                "mutants_distinguished": 18,
            },
        )
        self.assertEqual(self.manifest["counts"]["scenarios"], 46)
        self.assertEqual(self.manifest["counts"]["turns"], 63)

    def test_dataset_or_answer_drift_is_rejected(self):
        for name in ("cases.json", "goldens.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                target = Path(temp)
                for source in SUITE.glob("*.json"):
                    shutil.copy(source, target / source.name)
                with (target / name).open("a") as output:
                    output.write(" ")
                with self.assertRaisesRegex(ValueError, "冻结测试集已变化"):
                    load_suite(target)

    def test_reference_business_values_with_independent_python_aggregation(self):
        # 不重复执行参考 SQL；直接从原始事实记录核对加权分母和队列集合。
        raw = expected_rows()
        orders = raw["fact_order"]
        selected = [
            o
            for o in orders
            if o["region_id"] == "R004" and 20250401 <= o["date_id"] <= 20250731
        ]
        expected = self.goldens["HSQL04.1/answer"]["rows"][0]
        self.assertEqual(expected["orders"], len(selected))
        self.assertEqual(expected["amount"], sum(o["order_amount"] for o in selected))
        self.assertAlmostEqual(
            expected["aov"], expected["amount"] / len(selected), places=2
        )
        products = {
            p["product_id"] for p in raw["dim_product"] if p["category"] == "手机数码"
        }
        cohorts = [
            {
                o["customer_id"]
                for o in orders
                if o["region_id"] == "R004"
                and o["product_id"] in products
                and o["date_id"] // 100 == month
            }
            for month in (202506, 202507)
        ]
        self.assertEqual(
            cohorts[0] - cohorts[1],
            {r["customer_id"] for r in self.goldens["HSQL07.1/answer"]["rows"]},
        )

    def test_analysis_references_conserve_total_and_distinguish_weighting(self):
        monthly = self.goldens["HA02.1/monthly_facts"]["rows"]
        delta = monthly[1]["amount"] - monthly[0]["amount"]
        self.assertEqual(
            delta,
            sum(
                r["delta"]
                for r in self.goldens["HA02.1/category_contributions"]["rows"]
            ),
        )
        self.assertEqual([r["buyers"] for r in monthly], [20, 20])
        combined = self.goldens["HA03.1/combined"]["rows"]
        self.assertEqual([r["aov"] for r in combined], [100, 87.5])
        self.assertGreater(self.goldens["HA01.1/period_totals"]["rows"][0]["delta"], 0)

    def test_columns_can_reorder_and_extra_columns_do_not_hide_wrong_values(self):
        turn = self.turns["HSQL04.1"]
        expected = self.goldens["HSQL04.1/answer"]["rows"]
        row = expected[0]
        actual = [
            {
                "客单价": str(row["aov"]),
                "说明": "元",
                "销售额": row["amount"],
                "订单数": row["orders"],
            }
        ]
        self.assertEqual(
            compare_table(actual, expected, turn["grading"])["status"], "pass"
        )
        actual[0]["订单数"] = row["orders"] + 1
        self.assertEqual(
            compare_table(actual, expected, turn["grading"])["status"], "fail"
        )

    def test_unknown_or_ambiguous_column_needs_review(self):
        turn = self.turns["HSQL04.1"]
        expected = self.goldens["HSQL04.1/answer"]["rows"]
        for row in (
            {"x": 440, "y": 754807.5, "z": 1715.47},
            {**expected[0], "客单价": 1715.47},
        ):
            self.assertEqual(
                compare_table([row], expected, turn["grading"])["status"],
                "needs_review",
            )

    def test_empty_and_null_are_valid_but_not_zero(self):
        turn = self.turns["HSQL27.1"]
        expected = self.goldens["HSQL27.1/answer"]["rows"]
        self.assertEqual(expected, [{"orders": 0, "aov": None}])
        self.assertEqual(
            compare_table(expected, expected, turn["grading"])["status"], "pass"
        )
        self.assertEqual(
            compare_table([{"orders": 0, "aov": 0}], expected, turn["grading"])[
                "status"
            ],
            "fail",
        )
        turn = self.turns["HSQL28.1"]
        self.assertEqual(
            grade_turn(turn, [{"type": "result", "data": []}], self.goldens)["status"],
            "pass",
        )

    def test_duplicates_order_and_integer_precision(self):
        grading = {
            "columns": [{"key": "v", "aliases": ["v"], "abs_tol": "0"}],
            "ordered": False,
        }
        a, e = [{"v": 1}, {"v": 2}, {"v": 2}], [{"v": 1}, {"v": 1}, {"v": 2}]
        self.assertEqual(compare_table(a, e, grading)["status"], "fail")
        e = [{"v": 1}, {"v": 2}]
        self.assertEqual(compare_table(e[::-1], e, grading)["status"], "pass")
        grading["ordered"] = True
        self.assertEqual(compare_table(e[::-1], e, grading)["status"], "fail")
        self.assertEqual(
            compare_table([{"v": Decimal("1.001")}, {"v": 2}], e, grading)["status"],
            "fail",
        )

    def test_rate_units_are_reviewed_not_guessed(self):
        turn = self.turns["HSQL22.1"]
        expected = self.goldens["HSQL22.1/answer"]["rows"]
        actual = [{k: v / 100 for k, v in expected[0].items()}]
        self.assertEqual(
            compare_table(actual, expected, turn["grading"])["status"], "needs_review"
        )
        self.assertEqual(
            compare_table(actual, expected, turn["grading"], canonical_units=True)[
                "status"
            ],
            "fail",
        )

    def test_clarification_requires_no_sql_and_human_review_of_meaning(self):
        turn = self.turns["HB01.1"]
        events = [{"type": "clarification", "message": "请明确分母"}]
        self.assertEqual(
            grade_turn(turn, events, self.goldens)["status"], "needs_review"
        )
        events.insert(0, {"type": "progress", "step": "执行SQL", "status": "running"})
        self.assertEqual(grade_turn(turn, events, self.goldens)["status"], "fail")

    def test_analysis_requires_report_and_honest_partial_status(self):
        turn = self.turns["HA04.1"]
        event = {"type": "result", "data": []}
        self.assertEqual(grade_turn(turn, [event], self.goldens)["status"], "fail")
        event["analysis"] = {"summary": {"text": "已知结果"}, "status": "complete"}
        self.assertEqual(grade_turn(turn, [event], self.goldens)["status"], "fail")
        event["analysis"]["status"] = "partial"
        self.assertEqual(
            grade_turn(turn, [event], self.goldens)["status"], "needs_review"
        )

    def test_sse_terminal_count_and_request_answer_isolation(self):
        turn = self.turns["HSQL01.1"]
        for events in ([], [{"type": "result"}, {"type": "result"}]):
            self.assertEqual(grade_turn(turn, events, self.goldens)["status"], "fail")
        body = ': keepalive\r\n\r\ndata: {"type":"result",\r\ndata: "data":[]}\r\n\r\n'
        self.assertEqual(parse_events(body), [{"type": "result", "data": []}])
        self.assertEqual(
            query_payload(turn, "abc"),
            {"query": turn["query"], "conversation_id": "abc"},
        )
        with self.assertRaises(ValueError):
            select_cases(self.suite, ["HM07.3"])


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_lanes_are_isolated_and_never_send_answers(self):
        suite, goldens, _ = load_suite()
        cases = select_cases(suite, ["HM07"])
        turns = cases[0]["turns"]
        sent, saved = [], {}

        def respond(request):
            if request.method == "POST" and request.url.path == "/api/conversations":
                cid = f"c{len(saved)}"
                saved[cid] = []
                return httpx.Response(200, json={"id": cid})
            if request.method == "POST":
                payload = json.loads(request.content)
                self.assertEqual(set(payload), {"query", "conversation_id"})
                turn = turns[len(sent)]
                event = (
                    {"type": "clarification", "message": "请补充指标和时间"}
                    if turn["grading"]["mode"] == "behavior"
                    else {
                        "type": "result",
                        "data": goldens[f"{turn['id']}/answer"]["rows"],
                    }
                )
                saved[payload["conversation_id"]].append(
                    {"query": payload["query"], "outcome": event}
                )
                sent.append(payload)
                return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")
            return httpx.Response(
                200, json={"turns": saved[request.url.path.split("/")[-1]]}
            )

        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(respond)
        ) as client:
            records = await collect(client, cases, goldens)
        self.assertEqual([p["conversation_id"] for p in sent], ["c0", "c1", "c0", "c1"])
        self.assertEqual(
            [r["automatic"]["status"] for r in records],
            ["pass", "needs_review", "pass", "pass"],
        )
        self.assertTrue(all(r["persistence_matches"] for r in records))
        self.assertTrue(all(r["human_review"]["status"] == "pending" for r in records))

    async def test_mock_only_cases_make_no_requests(self):
        suite, goldens, _ = load_suite()
        cases = [
            copy.deepcopy(c) for c in suite["cases"] if c["execution"] == "mock_only"
        ]

        def reject(request):
            self.fail("mock_only 不得请求真实后端")

        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(reject)
        ) as client:
            records = await collect(client, cases, goldens)
        self.assertEqual(len(records), 5)
        self.assertTrue(all(r["automatic"]["status"] == "not_run" for r in records))


if __name__ == "__main__":
    unittest.main()
