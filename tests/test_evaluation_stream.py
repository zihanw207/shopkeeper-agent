import unittest

import httpx

from app.scripts.evaluate_agent import collect_response


class EvaluationStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_progress_result_and_request_id(self):
        def handler(request):
            self.assertEqual(request.method, "POST")
            return httpx.Response(
                200,
                headers={"x-request-id": "test-run"},
                content=(
                    'data: {"type":"progress","step":"生成SQL","status":"running"}\r\n\r\n'
                    'data: {"type":"result","data":[{"amount":10}],"sql":"SELECT 10","sql_repair_count":1,"analysis":{"status":"partial"}}\n\n'
                ).encode(),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_response(
                client,
                "http://test",
                {"id": "one", "query": "销售额", "category": "aggregate"},
                1,
            )
        self.assertEqual(result["actual"], [{"amount": 10}])
        self.assertEqual(result["sql_repair_count"], 1)
        self.assertEqual(result["analysis"], {"status": "partial"})
        self.assertEqual(result["request_id"], "test-run")
        self.assertGreaterEqual(result["latency_ms"], 0)

    async def test_error_after_result_is_not_lost(self):
        def handler(request):
            return httpx.Response(
                200,
                content=(
                    'data: {"type":"result","data":[]}\n\n'
                    'data: {"type":"error","code":"execution","sql_repair_count":2}\n\n'
                ).encode(),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_response(
                client, "http://test", {"id": "one", "query": "q", "category": "c"}, 1
            )
        self.assertEqual(result["error_code"], "execution")
        self.assertEqual(result["sql_repair_count"], 2)
