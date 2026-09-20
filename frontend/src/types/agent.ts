/**
 * 智能体类型定义
 * 定义问数智能体前端使用的 SSE 事件、流程步骤和聊天消息类型
 */
export type ProgressStatus = "running" | "success" | "error";

export type ProgressEvent = {
  type: "progress";
  step: string;
  status: ProgressStatus;
};

export type ResultEvent = {
  type: "result";
  data: unknown;
  sql?: string;
  max_rows?: number;
  sql_repair_count?: number;
  analysis?: ResultAnalysis;
};

export type AnalysisFinding = { text: string; evidence_ids: string[] };

export type AnalysisEvidence = {
  id: string;
  question: string;
  sql: string;
  data: unknown[];
  row_count: number;
  preview_truncated: boolean;
  possibly_limited: boolean;
};

export type ResultAnalysis = {
  status: "complete" | "partial";
  summary: AnalysisFinding;
  findings: AnalysisFinding[];
  limitations: string[];
  next_steps: string[];
  evidence: AnalysisEvidence[];
  followup_count: number;
};

export type ErrorEvent = {
  type: "error";
  message: string;
  code?: string;
};

export type QueryContext = {
  metrics: string[];
  time_range: string;
  dimensions: string[];
  filters: string[];
};

export type AgentEvent = ProgressEvent | ResultEvent | ErrorEvent
  | { type: "conversation"; conversation_id: string; turn_id: string }
  | { type: "context"; resolved_query: string; context: QueryContext }
  | { type: "clarification" | "unsupported"; message: string };

export type Conversation = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
};

export type ConversationTurn = {
  id: string;
  query: string;
  status: "running" | "completed" | "clarification" | "unsupported" | "error" | "cancelled";
  resolved_query: string | null;
  context: QueryContext | null;
  outcome: ResultEvent | ErrorEvent | { type: "clarification" | "unsupported"; message: string } | null;
  created_at: number;
};

export type ConversationDetail = Conversation & { turns: ConversationTurn[] };

export type StepState = {
  step: string;
  status: ProgressStatus;
  updatedAt: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status?: "streaming" | "done" | "error";
  steps?: StepState[];
  result?: unknown;
  error?: string;
  resolvedQuery?: string;
  analysis?: ResultAnalysis;
};
