"""持久化、隔离、澄清、失败恢复和并发测试；不调用真实模型/数仓。"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.agent.conversation import ConversationDecision, QueryContext
from app.agent.result_analysis import AnalysisDecision, ResultAnalyzer
from app.api.dependencies import get_query_service
from app.api.routers.conversation_router import conversation_router
from app.api.routers.query_router import query_router
from app.repositories.conversation_store import (
    ConversationBusy,
    ConversationNotFound,
    ConversationStore,
    get_conversation_store,
)
from app.services.query_service import QueryService


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "conversations.sqlite3"
        self.store = ConversationStore(self.path)
        self.conversation = await self.store.create()
        self.cid = self.conversation["id"]

    async def complete(self, query, cid=None, status="completed", context=None):
        turn = await self.store.begin_turn(cid or self.cid, query)
        await self.store.finish_turn(
            turn["id"],
            status,
            {"type": "result", "data": [{"amount": 12}]}
            if status == "completed"
            else {"type": "clarification", "message": "想看什么指标？"},
            query,
            context or {"metrics": ["销售额"], "filters": ["华北"]},
        )
        return turn

    def service(self, decision=None):
        resolver = AsyncMock()
        resolver.resolve.return_value = decision or ConversationDecision(
            action="query",
            resolved_query="统计 2025 年 Q1 华东销售额",
            context=QueryContext(
                metrics=["销售额"],
                time_range="2025-01-01 至 2025-03-31",
                filters=["华东"],
            ),
        )
        service = QueryService(
            None,
            None,
            None,
            None,
            None,
            None,
            conversation_store=self.store,
            conversation_resolver=resolver,
            result_analyzer=ResultAnalyzer(
                planner=AsyncMock(
                    decide=AsyncMock(
                        return_value=AnalysisDecision.model_validate(
                            {
                                "action": "finish",
                                "report": {
                                    "status": "complete",
                                    "summary": {
                                        "text": "测试数据已取得",
                                        "evidence_ids": ["E1"],
                                    },
                                },
                            }
                        )
                    )
                )
            ),
        )

        async def fake_graph(query):
            yield {"type": "progress", "step": "执行SQL", "status": "success"}
            yield {"type": "result", "data": [{"amount": 12}], "sql": "SELECT 12"}

        service._graph_events = fake_graph
        return service

    async def collect(self, service, query="那华东呢", cid=None):
        return [
            json.loads(event.removeprefix("data: "))
            async for event in service.query(query, cid)
        ]

    async def test_restart_restores_result_and_semantic_context(self):
        await self.complete("华北销售额")
        restored = await ConversationStore(self.path).get(self.cid)
        turn = restored["turns"][0]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["outcome"]["data"], [{"amount": 12}])
        self.assertEqual(turn["context"]["filters"], ["华北"])

    async def test_conversation_isolation(self):
        await self.complete("华北销售额")
        other = await self.store.create()
        turn = await self.store.begin_turn(other["id"], "那华东呢")
        self.assertEqual(turn["history"], [])
        self.assertEqual(len((await self.store.get(self.cid))["turns"]), 1)

    async def test_atomic_single_writer_across_store_instances(self):
        results = await asyncio.gather(
            self.store.begin_turn(self.cid, "A"),
            ConversationStore(self.path).begin_turn(self.cid, "B"),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, ConversationBusy) for item in results), 1)
        self.assertEqual(len((await self.store.get(self.cid))["turns"]), 1)
        other = await self.store.create()
        await self.store.begin_turn(other["id"], "另一个会话可以执行")

    async def test_lease_expiry_recovers_and_old_writer_cannot_commit(self):
        old = await self.store.begin_turn(self.cid, "上次执行")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE conversation_turns SET lease_expires=0 WHERE id=?", (old["id"],)
            )
        new = await self.store.begin_turn(self.cid, "重试")
        accepted = await self.store.finish_turn(
            old["id"], "completed", {"type": "result", "data": [99]}
        )
        self.assertFalse(accepted)
        turns = (await self.store.get(self.cid))["turns"]
        self.assertEqual([turn["status"] for turn in turns], ["cancelled", "running"])
        self.assertEqual(turns[-1]["id"], new["id"])

    async def test_history_bounded_and_failed_turn_does_not_replace_context(self):
        for i in range(5):
            await self.complete(f"成功查询{i}")
        for status in ["error", "cancelled", "unsupported"]:
            await self.complete("不要带进上下文", status=status)
        turn = await self.store.begin_turn(self.cid, "追问")
        self.assertEqual(
            [item["query"] for item in turn["history"]],
            ["成功查询2", "成功查询3", "成功查询4"],
        )
        self.assertNotIn("outcome", turn["history"][0])
        self.assertNotIn("sql", turn["history"][0])

    async def test_pending_clarification_included_until_success(self):
        await self.complete("最近表现如何", status="clarification")
        turn = await self.store.begin_turn(self.cid, "看一季度销售额")
        self.assertEqual(turn["history"][0]["clarification"], "想看什么指标？")
        await self.store.finish_turn(
            turn["id"], "completed", {"type": "result", "data": []}, "一季度销售额"
        )
        next_turn = await self.store.begin_turn(self.cid, "那华东呢")
        self.assertEqual(
            [item["status"] for item in next_turn["history"]], ["completed"]
        )

    async def test_resolved_question_reaches_fresh_graph_and_is_committed_before_result(
        self,
    ):
        await self.complete("统计 2025 年 Q1 华北销售额")
        service = self.service()
        seen = []

        async def fake_graph(query):
            seen.append(query)
            yield {"type": "result", "data": []}

        service._graph_events = fake_graph
        async for raw in service.query("那华东呢", self.cid):
            event = json.loads(raw.removeprefix("data: "))
            if event["type"] == "result":
                saved = (await self.store.get(self.cid))["turns"][-1]
                self.assertEqual(saved["outcome"]["analysis"], event["analysis"])
                self.assertEqual(
                    (await self.store.get(self.cid))["turns"][-1]["status"], "completed"
                )
        self.assertEqual(seen, ["统计 2025 年 Q1 华东销售额"])
        history = service.conversation_resolver.resolve.call_args.args[1]
        self.assertEqual(history[-1]["resolved_query"], "统计 2025 年 Q1 华北销售额")

    async def test_cancel_during_analysis_releases_turn_without_saving_success(self):
        service = self.service()
        started = asyncio.Event()

        async def wait_for_analysis(*args):
            started.set()
            await asyncio.Event().wait()

        service.result_analyzer.planner.decide.side_effect = wait_for_analysis
        task = asyncio.create_task(self.collect(service, cid=self.cid))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = (await self.store.get(self.cid))["turns"][-1]
        self.assertEqual(saved["status"], "cancelled")
        await self.store.begin_turn(self.cid, "重试")

    async def test_clarification_then_followup_and_unsupported_skip_graph(self):
        for action, expected in [
            ("clarify", "clarification"),
            ("unsupported", "unsupported"),
        ]:
            service = self.service(
                ConversationDecision(action=action, message="需要明确指标")
            )
            service._graph_events = unittest.mock.Mock(
                side_effect=AssertionError("不能执行图")
            )
            events = await self.collect(service, cid=self.cid)
            self.assertEqual(events[-1]["type"], expected)
        followup = self.service()
        await self.collect(followup, "统计销售额", self.cid)
        history = followup.conversation_resolver.resolve.call_args.args[1]
        self.assertEqual([item["status"] for item in history], ["clarification"])

    async def test_exception_after_provisional_result_is_not_success(self):
        service = self.service()

        async def fake_graph(query):
            yield {"type": "result", "data": [1]}
            raise ConnectionError("stream failed")

        service._graph_events = fake_graph
        events = await self.collect(service, cid=self.cid)
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(any(event["type"] == "result" for event in events))
        self.assertEqual(
            (await self.store.get(self.cid))["turns"][-1]["status"], "error"
        )

    async def test_disconnect_releases_turn(self):
        stream = self.service().query("查询", self.cid)
        await anext(stream)  # 会话已经占用，用户在流式输出期间关闭页面。
        await stream.aclose()
        self.assertEqual(
            (await self.store.get(self.cid))["turns"][-1]["status"], "cancelled"
        )
        await self.store.begin_turn(self.cid, "重试")

    async def test_task_cancellation_releases_turn(self):
        started = asyncio.Event()
        service = self.service()

        async def wait_forever(*args):
            started.set()
            await asyncio.Event().wait()

        service.conversation_resolver.resolve.side_effect = wait_forever
        task = asyncio.create_task(self.collect(service, cid=self.cid))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            (await self.store.get(self.cid))["turns"][-1]["status"], "cancelled"
        )

    async def test_model_timeout_does_not_pollute_history(self):
        await self.complete("华北销售额")
        service = self.service()
        service.conversation_resolver.resolve.side_effect = TimeoutError()
        events = await self.collect(service, cid=self.cid)
        self.assertEqual(events[-1]["code"], "timeout")
        next_turn = await self.store.begin_turn(self.cid, "重试")
        self.assertEqual(len(next_turn["history"]), 1)

    async def test_legacy_stateless_query_skips_memory_and_resolver(self):
        service = self.service()
        events = await self.collect(service, "销售额")
        self.assertEqual(events[-1]["type"], "result")
        service.conversation_resolver.resolve.assert_not_awaited()
        self.assertEqual((await self.store.get(self.cid))["turns"], [])

    async def test_unknown_or_busy_conversation_returns_explicit_sse_error(self):
        with self.assertRaises(ConversationNotFound):
            await self.store.get(str(uuid4()))
        events = await self.collect(self.service(), cid=str(uuid4()))
        self.assertEqual(events[-1]["code"], "conversation_not_found")
        await self.store.begin_turn(self.cid, "占用")
        events = await self.collect(self.service(), cid=self.cid)
        self.assertEqual(events[-1]["code"], "conversation_busy")

    async def test_api_create_query_restore_and_request_validation(self):
        app = FastAPI()
        app.include_router(conversation_router)
        app.include_router(query_router)
        app.dependency_overrides[get_conversation_store] = lambda: self.store
        service = self.service()
        app.dependency_overrides[get_query_service] = lambda: service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/api/conversations")
            self.assertEqual(created.status_code, 201)
            cid = created.json()["id"]
            response = await client.post(
                "/api/query", json={"query": "销售额", "conversation_id": cid}
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn('"type": "context"', response.text)
            restored = (await client.get(f"/api/conversations/{cid}")).json()
            self.assertEqual(restored["turns"][-1]["outcome"]["data"], [{"amount": 12}])
            self.assertEqual((await client.get("/api/conversations")).status_code, 200)
            self.assertEqual(
                (await client.get(f"/api/conversations/{uuid4()}")).status_code, 404
            )
            for invalid in [
                {"query": "  "},
                {"query": "x" * 2001},
                {"query": "销售额", "conversation_id": "bad"},
            ]:
                self.assertEqual(
                    (await client.post("/api/query", json=invalid)).status_code, 422
                )

    async def test_http_concurrent_conversations_finish_independently(self):
        """真实 FastAPI/SSE/会话存储，在内存 HTTP 传输中检查 B 先完成、A 继续。"""
        app = FastAPI()
        app.include_router(conversation_router)
        app.include_router(query_router)
        other = await self.store.create()
        ids = {"A": self.cid, "B": other["id"]}
        started = {label: asyncio.Event() for label in ids}
        release = {label: asyncio.Event() for label in ids}

        class Resolver:
            async def resolve(self, query, history):
                return ConversationDecision(action="query", resolved_query=query)

        service = self.service()
        service.conversation_resolver = Resolver()

        async def graph(query):
            started[query].set()
            yield {"type": "progress", "step": "执行SQL", "status": "running"}
            await release[query].wait()
            yield {"type": "result", "data": [{"label": query}]}

        service._graph_events = graph
        app.dependency_overrides[get_query_service] = lambda: service
        app.dependency_overrides[get_conversation_store] = lambda: self.store
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            tasks = []
            try:
                for label in ("A", "B"):
                    tasks.append(
                        asyncio.create_task(
                            client.post(
                                "/api/query",
                                json={"query": label, "conversation_id": ids[label]},
                            )
                        )
                    )
                    await asyncio.wait_for(started[label].wait(), 2)
                for cid in ids.values():
                    saved = (await client.get(f"/api/conversations/{cid}")).json()
                    self.assertEqual(saved["turns"][-1]["status"], "running")
                release["B"].set()
                response_b = await asyncio.wait_for(tasks[1], 2)
                self.assertIn('"label": "B"', response_b.text)
                self.assertFalse(tasks[0].done())
                saved_a = (await client.get(f"/api/conversations/{ids['A']}")).json()
                self.assertEqual(saved_a["turns"][-1]["status"], "running")
                release["A"].set()
                response_a = await asyncio.wait_for(tasks[0], 2)
                self.assertIn('"label": "A"', response_a.text)
                for label, cid in ids.items():
                    saved = (await client.get(f"/api/conversations/{cid}")).json()
                    self.assertEqual(
                        saved["turns"][-1]["outcome"]["data"], [{"label": label}]
                    )
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


class DecisionTests(unittest.TestCase):
    def test_invalid_model_output_cannot_enter_query_graph(self):
        for values in [
            {"action": "query", "resolved_query": " "},
            {"action": "clarify", "message": ""},
            {"action": "execute_sql", "resolved_query": "SELECT 1"},
            {
                "action": "query",
                "resolved_query": "销售额",
                "context": {"metrics": ["x" * 201]},
            },
        ]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ConversationDecision.model_validate(values)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_passes_bounded_history_as_data_and_uses_typed_output(self):
        from app.agent.conversation import ConversationResolver

        chain = AsyncMock()
        chain.ainvoke.return_value = ConversationDecision(
            action="clarify", message="想查询什么指标？"
        )
        with patch("app.agent.conversation.llm") as model:
            model.with_structured_output.return_value = chain
            await ConversationResolver().resolve("那华东呢", [])
        model.with_structured_output.assert_called_once_with(
            ConversationDecision, method="json_mode"
        )
        messages = chain.ainvoke.call_args.args[0]
        self.assertIn("继承规则", messages[0].content)
        self.assertIn('"required": ["action"]', messages[0].content)
        self.assertEqual(
            json.loads(messages[1].content),
            {"history": [], "current_question": "那华东呢"},
        )

    async def test_real_sdk_uses_json_mode_without_forced_tool_choice(self):
        from langchain_openai import ChatOpenAI

        from app.agent.conversation import ConversationResolver

        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "action": "clarify",
                                        "message": "想查询哪个指标？",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(
                model="deepseek-v4-flash",
                api_key="test",
                base_url="https://example.test/v1",
                http_async_client=client,
                max_retries=0,
            )
            with patch("app.agent.conversation.llm", model):
                decision = await ConversationResolver().resolve("那华东呢", [])
        self.assertIsInstance(decision, ConversationDecision)
        self.assertEqual(decision.action, "clarify")
        self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("tool_choice", requests[0])
        self.assertNotIn("tools", requests[0])
