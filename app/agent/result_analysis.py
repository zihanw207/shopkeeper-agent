"""查询后的有界分析循环：模型选下一条问题，程序控制查询预算和证据引用。"""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.llm import llm

ROOT = Path(__file__).resolve().parents[2]
MAX_FOLLOWUPS = 2
MAX_ANALYSIS_SECONDS = 75
MAX_EVIDENCE_ROWS = 40
MAX_EVIDENCE_CHARS = 14000
Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)
]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^E[1-3]$")]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Text
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=3)


class AnalysisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["complete", "partial"]
    summary: Finding
    findings: list[Finding] = Field(default_factory=list, max_length=6)
    limitations: list[Text] = Field(default_factory=list, max_length=6)
    next_steps: list[Text] = Field(default_factory=list, max_length=4)


class AnalysisDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["query", "finish"]
    question: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=300)
    report: AnalysisReport | None = None

    @model_validator(mode="after")
    def require_content(self):
        if self.action == "query" and (not self.question or not self.reason):
            raise ValueError("补充查询必须有独立问题和查询目的")
        if self.action == "finish" and self.report is None:
            raise ValueError("结束分析必须提供报告")
        return self


def evidence_from_result(identifier: str, question: str, result: dict) -> dict:
    """给模型和页面同一份有界预览；明确区分无数据、预览截断和查询行数上限。"""
    data = result.get("data")
    rows = data if isinstance(data, list) else [] if data is None else [data]
    preview = []
    used = 2
    for row in rows[:MAX_EVIDENCE_ROWS]:
        encoded = json.dumps(row, ensure_ascii=False, default=str)
        if used + len(encoded) + 1 > MAX_EVIDENCE_CHARS:
            break
        preview.append(json.loads(encoded))
        used += len(encoded) + 1
    max_rows = result.get("max_rows")
    return {
        "id": identifier,
        "question": question,
        "sql": result.get("sql", ""),
        "data": preview,
        "row_count": len(rows),
        "preview_truncated": len(preview) < len(rows),
        "possibly_limited": isinstance(max_rows, int) and len(rows) >= max_rows,
    }


def schema_catalog() -> list[dict]:
    # 仅使用本项目配置中的表/字段信息，不读取连接配置或凭证。
    # 元数据中的 AOV 描述尚不足以当作正式口径；分析提示明确给出订单数与件数的区别。
    config = yaml.safe_load((ROOT / "conf" / "meta_config.yaml").read_text())
    return [
        {
            "table": table["name"],
            "description": table["description"],
            "columns": [
                {key: column[key] for key in ("name", "role", "description")}
                for column in table["columns"]
            ],
        }
        for table in config["tables"]
    ]


class AnalysisPlanner:
    async def decide(
        self, question, resolved_query, evidence, issues, remaining_queries
    ):
        prompt = (ROOT / "prompts" / "analyze_result.prompt").read_text()
        model = llm.with_structured_output(AnalysisDecision, method="json_mode")
        decision = await model.ainvoke(
            [
                SystemMessage(
                    content=prompt
                    + "\n输出 JSON Schema：\n"
                    + json.dumps(
                        AnalysisDecision.model_json_schema(), ensure_ascii=False
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "original_question": question,
                            "resolved_query": resolved_query,
                            "schema_catalog": schema_catalog(),
                            "evidence": evidence,
                            "issues": issues,
                            "remaining_queries": remaining_queries,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                ),
            ]
        )
        return AnalysisDecision.model_validate(decision)


class ResultAnalyzer:
    def __init__(self, planner=None):
        self.planner = planner or AnalysisPlanner()

    async def events(self, question, resolved_query, primary, execute, budget_seconds):
        """execute 复用完整 SQL 图；所有补查串行运行，避免共享 AsyncSession 并发。"""
        evidence = [evidence_from_result("E1", resolved_query, primary)]
        issues = []
        attempted = 0
        report = None
        seen = {self._key(resolved_query)}
        yield {"type": "progress", "step": "分析结果", "status": "running"}

        try:
            if budget_seconds <= 0:
                raise TimeoutError
            async with asyncio.timeout(
                max(0, min(MAX_ANALYSIS_SECONDS, budget_seconds))
            ):
                for _ in range(MAX_FOLLOWUPS + 1):
                    remaining = 0 if issues else MAX_FOLLOWUPS - attempted
                    decision = await self.planner.decide(
                        question, resolved_query, evidence, issues, remaining
                    )
                    if decision.action == "finish":
                        report = self._validate_report(
                            decision.report, evidence, issues
                        )
                        break
                    if remaining == 0:
                        issues.append("补充查询预算已用完，现有证据仍不足以完成分析。")
                        break
                    key = self._key(decision.question)
                    if key in seen:
                        issues.append("分析请求重复查询已有问题，已停止重复执行。")
                        break
                    seen.add(key)
                    attempted += 1
                    step = f"补充查询 {attempted}：{decision.reason}"
                    yield {"type": "progress", "step": step, "status": "running"}
                    terminal = None
                    try:
                        async for event in execute(decision.question):
                            if event.get("type") in {
                                "result",
                                "error",
                                "clarification",
                                "unsupported",
                            }:
                                terminal = event
                            elif event.get("type") == "progress":
                                yield {
                                    **event,
                                    "step": f"补查 {attempted} · {event['step']}",
                                }
                    except Exception:
                        # 主查询有效，补查失败不可把整个回答和已取得的数据丢掉。
                        terminal = None
                    if terminal and terminal.get("type") == "result":
                        evidence.append(
                            evidence_from_result(
                                f"E{len(evidence) + 1}", decision.question, terminal
                            )
                        )
                        yield {"type": "progress", "step": step, "status": "success"}
                    else:
                        issues.append(
                            f"补充查询未完成：{decision.question}。不能据此推断数值或原因。"
                        )
                        yield {"type": "progress", "step": step, "status": "error"}
        except TimeoutError:
            issues.append(
                "分析达到本轮时间预算，保留已查到的数据，尚未完成的归因不能确认。"
            )
        except Exception:
            issues.append("分析服务未返回可验证的报告，保留已查到的数据。")

        if report is None:
            report = {
                "status": "partial",
                "summary": {
                    "text": "数据已查到，但本轮分析尚未完成，请查看下方结果和限制说明。",
                    "evidence_ids": ["E1"],
                },
                "findings": [],
                "limitations": issues or ["现有证据不足以形成分析结论。"],
                "next_steps": [
                    "可缩小分析范围后重试，或明确要比较的指标与两个时间段。"
                ],
            }
        report["evidence"] = evidence
        report["followup_count"] = attempted
        yield {
            "type": "progress",
            "step": "分析结果",
            "status": "success" if report["status"] == "complete" else "error",
        }
        yield {**primary, "analysis": report}

    @staticmethod
    def _key(question: str) -> str:
        return "".join(question.split()).strip("？?。.").casefold()

    @staticmethod
    def _validate_report(report, evidence, issues):
        valid_ids = {item["id"] for item in evidence}
        for finding in [report.summary, *report.findings]:
            if not set(finding.evidence_ids) <= valid_ids:
                raise ValueError("报告引用了尚未取得的证据")
        output = report.model_dump()
        warnings = list(issues)
        if any(
            item["preview_truncated"] or item["possibly_limited"] for item in evidence
        ):
            warnings.append(
                "部分证据仅为结果预览或可能受行数上限影响，不能据此声称覆盖全部贡献。"
            )
        if warnings:
            output["status"] = "partial"
            output["limitations"] = list(
                dict.fromkeys([*output["limitations"], *warnings])
            )
        return output
