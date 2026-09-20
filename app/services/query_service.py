"""
问数查询服务

负责把 API 层传入的自然语言问题转换成一次 LangGraph 工作流执行：
创建初始 State、组装 Runtime Context、消费 graph.astream 的流式输出，
并统一包装成 SSE 文本返回给路由层。
"""

import asyncio
import json

import anyio
from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.agent.context import DataAgentContext
from app.agent.conversation import ConversationResolver
from app.agent.graph import graph
from app.agent.result_analysis import ResultAnalyzer
from app.agent.state import DataAgentState
from app.repositories.conversation_store import (
    ConversationBusy,
    ConversationNotFound,
    get_conversation_store,
)
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository


class QueryService:
    """封装一次问数查询所需的业务编排逻辑"""

    def __init__(
        self,
        meta_mysql_repository: MetaMySQLRepository,
        embedding_client: HuggingFaceEndpointEmbeddings,
        dw_mysql_repository: DWMySQLRepository,
        column_qdrant_repository: ColumnQdrantRepository,
        metric_qdrant_repository: MetricQdrantRepository,
        value_es_repository: ValueESRepository,
        conversation_store=None,
        conversation_resolver=None,
        result_analyzer=None,
    ):
        # MySQL 仓储分别负责元数据补全和真实数仓环境信息读取
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository

        # 召回链路依赖的向量检索、Embedding 和全文检索能力由依赖层注入
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_es_repository = value_es_repository
        self.conversation_store = conversation_store or get_conversation_store()
        self.conversation_resolver = conversation_resolver or ConversationResolver()
        self.result_analyzer = result_analyzer or ResultAnalyzer()

    async def query(self, query: str, conversation_id: str | None = None):
        """有会话 ID 时解析追问并持久化；省略 ID 时保持原来的单轮行为。"""
        turn = None
        finished = False
        resolved_query = query
        semantic_context = None
        outcome = None

        def sse(event):
            return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        try:
            try:
                # 包括追问理解、检索和 SQL 修正在内的整体预算。
                async with asyncio.timeout(120):
                    deadline = asyncio.get_running_loop().time() + 120
                    if conversation_id:
                        turn = await self.conversation_store.begin_turn(
                            conversation_id, query
                        )
                        yield sse(
                            {
                                "type": "conversation",
                                "conversation_id": conversation_id,
                                "turn_id": turn["id"],
                            }
                        )
                        yield sse(
                            {
                                "type": "progress",
                                "step": "理解追问",
                                "status": "running",
                            }
                        )
                        decision = await self.conversation_resolver.resolve(
                            query, turn["history"]
                        )
                        yield sse(
                            {
                                "type": "progress",
                                "step": "理解追问",
                                "status": "success",
                            }
                        )
                        semantic_context = decision.context.model_dump()
                        resolved_query = decision.resolved_query
                        if decision.action != "query":
                            outcome = {
                                "type": "clarification"
                                if decision.action == "clarify"
                                else "unsupported",
                                "message": decision.message,
                            }
                        else:
                            yield sse(
                                {
                                    "type": "context",
                                    "resolved_query": resolved_query,
                                    "context": semantic_context,
                                }
                            )

                    if outcome is None:
                        async for event in self._answer_events(
                            query, resolved_query, deadline
                        ):
                            # 成功事件先留到图正常结束；持久化成功后才告诉前端已完成。
                            if event.get("type") in {"result", "error"}:
                                outcome = event
                            else:
                                yield sse(event)
                        if outcome is None:
                            outcome = {
                                "type": "error",
                                "code": "incomplete",
                                "message": "查询未返回结果，请重试。",
                            }
            except (ConversationBusy, ConversationNotFound) as exc:
                yield sse(
                    {
                        "type": "error",
                        "code": "conversation_busy"
                        if isinstance(exc, ConversationBusy)
                        else "conversation_not_found",
                        "message": str(exc),
                    }
                )
                return
            except TimeoutError:
                outcome = {
                    "type": "error",
                    "code": "timeout",
                    "message": "查询超过 120 秒，请缩小查询范围后重试。",
                }
            except Exception as exc:
                outcome = {"type": "error", "message": str(exc)}

            if turn:
                status = {
                    "result": "completed",
                    "clarification": "clarification",
                    "unsupported": "unsupported",
                }.get(outcome["type"], "error")
                finished = await self.conversation_store.finish_turn(
                    turn["id"],
                    status,
                    outcome,
                    resolved_query,
                    semantic_context,
                )
                if not finished:
                    outcome = {
                        "type": "error",
                        "code": "lease_expired",
                        "message": "本轮查询已过期，请重新提问。",
                    }
            yield sse(outcome)
        finally:
            if turn and not finished:
                # StreamingResponse 断连会取消任务；仍须释放会话，避免下一轮永远 busy。
                with anyio.CancelScope(shield=True):
                    await self.conversation_store.finish_turn(
                        turn["id"],
                        "cancelled",
                        {"type": "error", "message": "本轮查询已停止，可重新提问。"},
                    )

    async def _answer_events(self, question: str, resolved_query: str, deadline: float):
        """先查数据，再进行有界分析；只发出一个包含分析的最终 result。"""
        primary = None
        async for event in self._graph_events(resolved_query):
            if event.get("type") in {"result", "error"}:
                primary = event
            else:
                yield event
        if primary is None:
            return
        if primary.get("type") != "result":
            yield primary
            return
        # 为终态持久化留下少量余量；补查共享原有 120 秒总预算与 SQL 安全策略。
        remaining = deadline - asyncio.get_running_loop().time() - 2
        async for event in self.result_analyzer.events(
            question, resolved_query, primary, self._graph_events, remaining
        ):
            yield event

    async def _graph_events(self, query: str):
        """每轮重新执行完整检索与 SQL 校验，绝不继承上一轮中间状态。"""

        # State 只放会被图节点读写和合并的业务数据，外部工具对象不塞进 State
        state = DataAgentState(query=query)
        # Context 保存本次图执行需要复用的外部依赖，节点通过 runtime.context 读取
        context = DataAgentContext(
            column_qdrant_repository=self.column_qdrant_repository,
            embedding_client=self.embedding_client,
            metric_qdrant_repository=self.metric_qdrant_repository,
            value_es_repository=self.value_es_repository,
            meta_mysql_repository=self.meta_mysql_repository,
            dw_mysql_repository=self.dw_mysql_repository,
        )
        async for chunk in graph.astream(
            input=state,
            context=context,
            stream_mode="custom",
            config={"recursion_limit": 32},
        ):
            yield chunk
