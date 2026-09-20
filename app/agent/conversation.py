"""把本轮输入与有限的业务记忆合并；不复用上轮 SQL 或检索中间状态。"""

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.llm import llm

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class QueryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: list[ShortText] = Field(default_factory=list, max_length=8)
    time_range: str = Field(default="", max_length=200)
    dimensions: list[ShortText] = Field(default_factory=list, max_length=8)
    filters: list[ShortText] = Field(default_factory=list, max_length=10)


class ConversationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["query", "clarify", "unsupported"]
    resolved_query: str = Field(default="", max_length=2000)
    message: str = Field(default="", max_length=500)
    context: QueryContext = Field(default_factory=QueryContext)

    @model_validator(mode="after")
    def require_content(self):
        if self.action == "query" and not self.resolved_query:
            raise ValueError("query requires a complete resolved_query")
        if self.action != "query" and not self.message:
            raise ValueError("clarify/unsupported requires a message")
        return self


class ConversationResolver:
    async def resolve(self, query: str, history: list[dict]) -> ConversationDecision:
        prompt = (
            Path(__file__).resolve().parents[2]
            / "prompts"
            / "resolve_conversation.prompt"
        ).read_text()
        # JSON 模式不发送强制 tool_choice，兼容 DeepSeek 的默认思考模式。
        # JSON 模式只保证 JSON 语法，仍需提示完整 schema 并在本地做 Pydantic 校验。
        model = llm.with_structured_output(ConversationDecision, method="json_mode")
        schema = json.dumps(
            ConversationDecision.model_json_schema(), ensure_ascii=False
        )
        return await model.ainvoke(
            [
                SystemMessage(
                    content=prompt
                    + f"\n当前日期：{date.today().isoformat()}\n输出 JSON Schema：\n{schema}"
                ),
                HumanMessage(
                    content=json.dumps(
                        {"history": history, "current_question": query},
                        ensure_ascii=False,
                    )
                ),
            ]
        )
