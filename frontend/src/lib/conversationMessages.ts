import type { AgentEvent, ChatMessage, ConversationDetail } from "../types/agent";
import { summarizeResult } from "./format";

export function applyAgentEvent(message: ChatMessage, event: AgentEvent): ChatMessage {
  if (event.type === "context") return { ...message, resolvedQuery: event.resolved_query };
  if (event.type === "progress") return {
    ...message,
    content: event.status === "running" ? `正在执行：${event.step}` : message.content,
    steps: [
      ...(message.steps ?? []).filter((step) => step.step !== event.step),
      { step: event.step, status: event.status, updatedAt: Date.now() },
    ],
  };
  if (event.type === "result") return {
    ...message, status: "done", result: event.data,
    analysis: event.analysis,
    steps: message.steps?.map((step) => step.status === "running"
      ? { ...step, status: event.analysis?.status === "partial" ? "error" : "success" }
      : step),
    content: event.analysis?.summary.text ?? summarizeResult(event.data) + (event.max_rows ? ` 单次最多返回 ${event.max_rows} 行。` : ""),
  };
  if (event.type === "clarification" || event.type === "unsupported") {
    return { ...message, status: "done", content: event.message };
  }
  if (event.type === "error") return {
    ...message, status: "error", content: "这次查询没有成功。", error: event.message,
  };
  return message;
}

export function restoreMessages(conversation: ConversationDetail): ChatMessage[] {
  return conversation.turns.flatMap((turn): ChatMessage[] => {
    const outcome = turn.outcome;
    const running = turn.status === "running";
    const failed = turn.status === "error" || turn.status === "cancelled";
    return [
      { id: `${turn.id}-user`, role: "user", content: turn.query, createdAt: turn.created_at * 1000 },
      {
        id: turn.id, role: "assistant", createdAt: turn.created_at * 1000,
        status: running ? "streaming" : failed ? "error" : "done",
        content: outcome?.type === "result"
          ? outcome.analysis?.summary.text ?? summarizeResult(outcome.data) + (outcome.max_rows ? ` 单次最多返回 ${outcome.max_rows} 行。` : "")
          : running ? "这轮查询尚未结束，正在同步状态…"
          : outcome && "message" in outcome ? outcome.message : "本轮未完成，请重新提问。",
        result: outcome?.type === "result" ? outcome.data : undefined,
        analysis: outcome?.type === "result" ? outcome.analysis : undefined,
        resolvedQuery: turn.resolved_query || undefined,
      },
    ];
  });
}
