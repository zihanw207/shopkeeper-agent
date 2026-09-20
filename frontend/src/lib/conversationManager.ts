/**
 * 页面级会话管理器。选择哪个会话只影响展示，不影响其他会话的 SSE 连接。
 * 异步回调捕获稳定的本地 key；后端 ID 创建完成、乱序返回都不会改变消息归属。
 */
import type { AgentEvent, ChatMessage, Conversation, ConversationDetail } from "../types/agent";
import { applyAgentEvent, restoreMessages } from "./conversationMessages";

export type ConversationApi = {
  createConversation: () => Promise<Conversation>;
  listConversations: () => Promise<Conversation[]>;
  getConversation: (id: string) => Promise<ConversationDetail>;
  streamQuery: (query: string, options: {
    conversationId?: string;
    signal?: AbortSignal;
    onEvent: (event: AgentEvent) => void;
  }) => Promise<void>;
};

export type SessionState = {
  conversationId: string | null;
  title: string;
  updatedAt: number;
  messages: ChatMessage[];
  draft: string;
  loading: boolean;
  loaded: boolean;
  running: boolean;
  externalRunning: boolean;
  unread: boolean;
  error: string;
};

type Snapshot = {
  selectedKey: string;
  sessions: Record<string, SessionState>;
  conversations: Conversation[];
  listError: string;
};

const makeId = () => crypto.randomUUID();
const emptySession = (): SessionState => ({
  conversationId: null, title: "新会话", updatedAt: Date.now(), messages: [], draft: "",
  loading: false, loaded: true, running: false, externalRunning: false, unread: false, error: "",
});

export class ConversationManager {
  private state: Snapshot;
  private listeners = new Set<() => void>();
  private controllers = new Map<string, AbortController>();
  private readVersions = new Map<string, number>();
  private reads = new Map<string, Promise<void>>();
  private listVersion = 0;
  private initialized = false;
  private disposed = false;

  constructor(private api: ConversationApi) {
    const key = `draft:${makeId()}`;
    this.state = { selectedKey: key, sessions: { [key]: emptySession() }, conversations: [], listError: "" };
  }

  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private publish(state: Snapshot) {
    if (this.disposed) return;
    this.state = state;
    this.listeners.forEach((listener) => listener());
  }

  private update(key: string, changes: Partial<SessionState>) {
    const session = this.state.sessions[key];
    if (session) this.publish({ ...this.state, sessions: { ...this.state.sessions, [key]: { ...session, ...changes } } });
  }

  private changeMessage(key: string, id: string, change: (message: ChatMessage) => ChatMessage) {
    this.update(key, { messages: this.state.sessions[key].messages.map((message) => message.id === id ? change(message) : message) });
  }

  initialize(savedId: string | null) {
    // React StrictMode 重放 effect 时不重复恢复，更不会覆盖后来选择的会话。
    if (this.initialized) return;
    this.initialized = true;
    if (savedId) void this.open(savedId);
    void this.refreshList();
  }

  async refreshList() {
    const version = ++this.listVersion;
    try {
      const conversations = await this.api.listConversations();
      if (version === this.listVersion) this.publish({ ...this.state, conversations, listError: "" });
    } catch {
      if (version === this.listVersion) this.publish({ ...this.state, listError: "会话列表暂时无法更新，请检查后端连接。" });
    }
  }

  setDraft(value: string) {
    this.update(this.state.selectedKey, { draft: value });
  }

  newConversation() {
    const current = this.state.sessions[this.state.selectedKey];
    if (!current.conversationId && !current.running && !current.messages.length && !current.draft) return;
    const key = `draft:${makeId()}`;
    this.publish({ ...this.state, selectedKey: key, sessions: { ...this.state.sessions, [key]: emptySession() } });
  }

  async open(idOrKey: string) {
    const key = this.state.sessions[idOrKey] ? idOrKey
      : Object.keys(this.state.sessions).find((key) => this.state.sessions[key].conversationId === idOrKey) ?? idOrKey;
    if (!this.state.sessions[key]) {
      const item = this.state.conversations.find((item) => item.id === idOrKey);
      this.publish({ ...this.state, sessions: { ...this.state.sessions, [key]: {
        ...emptySession(), conversationId: idOrKey, title: item?.title ?? "历史会话", loaded: false,
      } } });
    }
    this.publish({ ...this.state, selectedKey: key });
    this.update(key, { unread: false });
    const session = this.state.sessions[key];
    // 已缓存的运行会话直接展示本地流状态，不用 GET 的旧快照覆盖它。
    if (!session.loaded || session.externalRunning) await this.load(key);
  }

  private async load(key: string) {
    const session = this.state.sessions[key];
    if (!session?.conversationId || this.controllers.has(key)) return;
    if (this.reads.has(key)) return this.reads.get(key);
    const version = (this.readVersions.get(key) ?? 0) + 1;
    this.readVersions.set(key, version);
    this.update(key, { loading: !session.loaded });
    const request = (async () => {
      try {
        const detail = await this.api.getConversation(session.conversationId!);
        if (this.controllers.has(key) || this.readVersions.get(key) !== version) return;
        const running = detail.turns.some((turn) => turn.status === "running");
        const previous = this.state.sessions[key];
        this.update(key, {
          conversationId: detail.id, title: detail.title, updatedAt: detail.updated_at * 1000,
          messages: restoreMessages(detail), loaded: true, loading: false, externalRunning: running, error: "",
          unread: previous.unread || (previous.externalRunning && !running && this.state.selectedKey !== key),
        });
      } catch (error) {
        if (this.readVersions.get(key) === version) this.update(key, {
          loading: false, error: error instanceof Error ? error.message : String(error),
        });
      }
    })();
    this.reads.set(key, request);
    try { await request; } finally { if (this.reads.get(key) === request) this.reads.delete(key); }
  }

  async syncRunning() {
    // 刷新后失去原 SSE 的会话单独同步；也同步未选中的会话，不让状态一直停在运行中。
    await Promise.all(Object.keys(this.state.sessions)
      .filter((key) => this.state.sessions[key].externalRunning && !this.controllers.has(key))
      .map((key) => this.load(key)));
  }

  async startQuery(rawQuery?: string) {
    const key = this.state.selectedKey;
    const session = this.state.sessions[key];
    const query = (rawQuery ?? session.draft).trim();
    if (!query || this.controllers.has(key) || session.loading || !session.loaded || session.externalRunning) return;
    const controller = new AbortController();
    this.controllers.set(key, controller); // 同步加锁，连续点击发送也只会启动一次。
    this.readVersions.set(key, (this.readVersions.get(key) ?? 0) + 1);
    const assistantId = makeId();
    this.update(key, {
      draft: "", running: true, error: "", unread: false, updatedAt: Date.now(),
      title: session.messages.length ? session.title : query.slice(0, 40),
      messages: [...session.messages,
        { id: makeId(), role: "user", content: query, createdAt: Date.now() },
        { id: assistantId, role: "assistant", content: "正在连接问数智能体...", createdAt: Date.now(), status: "streaming", steps: [] },
      ],
    });
    let terminalReceived = false;
    let needsSync = false;
    let wasAborted = false;

    try {
      let id = session.conversationId;
      if (!id) {
        const created = await this.api.createConversation();
        id = created.id;
        // 仅更新发起请求的会话；即使此时已切到 B，也不能抢回当前选择。
        this.update(key, { conversationId: id });
      }
      if (controller.signal.aborted) throw new DOMException("Stopped", "AbortError");
      await this.api.streamQuery(query, {
        conversationId: id, signal: controller.signal,
        onEvent: (event) => {
          if (this.controllers.get(key) !== controller || controller.signal.aborted) return;
          if (["result", "error", "clarification", "unsupported"].includes(event.type)) terminalReceived = true;
          if (event.type === "error" && event.code === "conversation_busy") needsSync = true;
          this.changeMessage(key, assistantId, (message) => applyAgentEvent(message, event));
        },
      });
      if (!terminalReceived) {
        needsSync = true;
        this.changeMessage(key, assistantId, (message) => ({
          ...message, status: "error", content: "连接已结束，正在核对本轮查询状态。",
        }));
      }
    } catch (error) {
      wasAborted = controller.signal.aborted;
      // 终态已收到时，随后断连不应把成功结果改写成失败。
      if (!terminalReceived) {
        needsSync = Boolean(this.state.sessions[key].conversationId);
        this.changeMessage(key, assistantId, (message) => ({
          ...message, status: wasAborted ? "done" : "error",
          content: wasAborted ? "已停止本次查询。" : "无法连接问数接口。",
          error: wasAborted ? undefined : error instanceof Error ? error.message : String(error),
        }));
      }
    } finally {
      if (this.controllers.get(key) === controller) {
        this.controllers.delete(key);
        this.update(key, {
          running: false, externalRunning: needsSync, updatedAt: Date.now(),
          unread: !wasAborted && this.state.selectedKey !== key,
        });
        if (needsSync) void this.load(key);
        void this.refreshList();
      }
    }
  }

  stopQuery() {
    this.controllers.get(this.state.selectedKey)?.abort();
  }

  dispose() {
    this.disposed = true;
    this.controllers.forEach((controller) => controller.abort());
    this.controllers.clear();
    this.listeners.clear();
  }
}

export function conversationItems(state: Snapshot) {
  const items = new Map<string, { key: string; title: string; updatedAt: number; running: boolean; unread: boolean }>();
  for (const conversation of state.conversations) {
    items.set(conversation.id, { key: conversation.id, title: conversation.title, updatedAt: conversation.updated_at * 1000, running: false, unread: false });
  }
  for (const [key, session] of Object.entries(state.sessions)) {
    if (!session.conversationId && !session.messages.length && !session.draft && key !== state.selectedKey) continue;
    items.set(session.conversationId ?? key, {
      key, title: session.title, updatedAt: session.updatedAt,
      running: session.running || session.externalRunning, unread: session.unread,
    });
  }
  return [...items.values()].sort((a, b) => b.updatedAt - a.updatedAt);
}
