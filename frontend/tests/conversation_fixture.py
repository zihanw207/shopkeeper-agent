"""浏览器并发验收专用：真实会话 API/SSE/SQLite，替换模型与数据查询。

从项目根目录执行：python frontend/tests/conversation_fixture.py
仅绑定本机 18001 端口；A 开头的问题延迟 35 秒，其余延迟 4 秒。
不连接模型或业务数据库，临时会话随测试服务退出删除。
"""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.agent.conversation import ConversationDecision  # noqa: E402
from app.agent.result_analysis import AnalysisDecision, ResultAnalyzer  # noqa: E402
from app.api.dependencies import get_query_service  # noqa: E402
from app.api.routers.conversation_router import conversation_router  # noqa: E402
from app.api.routers.query_router import query_router  # noqa: E402
from app.repositories.conversation_store import (  # noqa: E402
    ConversationStore,
    get_conversation_store,
)
from app.services.query_service import QueryService  # noqa: E402


class Resolver:
    async def resolve(self, query, history):
        return ConversationDecision(action="query", resolved_query=query)


class FixturePlanner:
    async def decide(
        self, question, resolved_query, evidence, issues, remaining_queries
    ):
        return AnalysisDecision.model_validate(
            {
                "action": "finish",
                "report": {
                    "status": "partial",
                    "summary": {
                        "text": "模拟分析：查询已完成，以下展示本会话的数据依据。",
                        "evidence_ids": ["E1"],
                    },
                    "findings": [
                        {
                            "text": "这是离线页面验收数据，不代表真实经营情况。",
                            "evidence_ids": ["E1"],
                        }
                    ],
                    "limitations": ["未连接真实模型与业务数据库。"],
                    "next_steps": ["真实分析需要查询相关指标与维度。"],
                },
            }
        )


class FixtureService(QueryService):
    async def _graph_events(self, query):
        for _ in range(35 if query.startswith("A") else 4):
            yield {"type": "progress", "step": "执行SQL", "status": "running"}
            await asyncio.sleep(1)
        yield {"type": "progress", "step": "执行SQL", "status": "success"}
        yield {
            "type": "result",
            "data": [{"测试会话": query, "说明": "模拟数据，仅验证前端并发"}],
        }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="shopkeeper-browser-test-") as directory:
        store = ConversationStore(Path(directory) / "conversations.sqlite3")
        service = FixtureService(
            None,
            None,
            None,
            None,
            None,
            None,
            conversation_store=store,
            conversation_resolver=Resolver(),
            result_analyzer=ResultAnalyzer(planner=FixturePlanner()),
        )
        app = FastAPI()
        app.include_router(conversation_router)
        app.include_router(query_router)
        app.dependency_overrides[get_conversation_store] = lambda: store
        app.dependency_overrides[get_query_service] = lambda: service
        uvicorn.run(app, host="127.0.0.1", port=18001)
