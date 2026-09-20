/**
 * 前端应用主组件
 * 负责聊天会话状态、SSE 事件消费和整体页面布局
 */
import {
  Activity,
  BarChart3,
  History,
  Leaf,
  LoaderCircle,
  MessageSquarePlus,
  Server,
} from "lucide-react";
import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { Composer } from "./components/Composer";
import { EmptyState } from "./components/EmptyState";
import { MessageBubble } from "./components/MessageBubble";
import { createConversation, getConversation, listConversations, streamQuery } from "./lib/agentApi";
import { cn } from "./lib/format";
import { ConversationManager, conversationItems } from "./lib/conversationManager";

const examples = [
  "统计 2025 年第一季度各大区的 GMV，并按 GMV 从高到低排序",
  "统计 2025 年 3 月各商品品类的销量和销售额",
  "查询华东地区 2025 年第一季度销售额最高的前 5 个商品",
  "按会员等级统计 2025 年第一季度的订单数和销售额",
];

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "Vite /api proxy";
const CONVERSATION_KEY = "shopkeeper.conversation";

function rememberConversation(id: string | null) {
  try {
    if (id) localStorage.setItem(CONVERSATION_KEY, id);
    else localStorage.removeItem(CONVERSATION_KEY);
  } catch { /* 禁用浏览器存储时仍可在当前页面对话。 */ }
}

// 管理器属于整个页面，切换会话不会销毁它或中止后台连接。
const manager = new ConversationManager({ createConversation, getConversation, listConversations, streamQuery });
if (import.meta.hot) import.meta.hot.dispose(() => manager.dispose());

export default function App() {
  const state = useSyncExternalStore(manager.subscribe, manager.getSnapshot);
  const current = state.sessions[state.selectedKey];
  const { messages, draft, loading, externalRunning } = current;
  const isStreaming = current.running;
  const sessionError = current.error || state.listError;
  const canSubmit = draft.trim().length > 0 && current.loaded && !isStreaming && !loading && !externalRunning;
  const conversations = useMemo(() => conversationItems(state), [state]);
  const backgroundCount = conversations.filter((item) => item.key !== state.selectedKey && item.running).length;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const completedCount = messages.filter((message) => message.role === "assistant" && message.status === "done").length;

  useEffect(() => {
    let saved: string | null = null;
    try { saved = localStorage.getItem(CONVERSATION_KEY); } catch { /* 可无本地存储 */ }
    manager.initialize(saved);
    const timer = setInterval(() => { void manager.syncRunning(); }, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => { rememberConversation(current.conversationId); }, [current.conversationId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, state.selectedKey]);

  const startQuery = (query?: string) => { void manager.startQuery(query); };
  const setDraft = (value: string) => manager.setDraft(value);
  const clearConversation = () => manager.newConversation();
  const openConversation = (key: string) => { void manager.open(key); };
  const stopQuery = () => manager.stopQuery();

  return (
    <div className="h-dvh overflow-hidden bg-parchment text-ink">
      <div className="pointer-events-none fixed inset-0 bg-[linear-gradient(90deg,rgba(32,32,29,0.045)_1px,transparent_1px),linear-gradient(rgba(32,32,29,0.035)_1px,transparent_1px)] bg-[size:48px_48px]" />
      <div className="pointer-events-none fixed inset-0 grain" />

      <div className="relative grid h-full min-h-0 overflow-hidden lg:grid-cols-[300px_minmax(0,1fr)]">
        <aside className="hidden min-h-0 border-r border-ink/10 bg-[#efe6d8]/85 backdrop-blur lg:flex lg:flex-col">
          <div className="border-b border-ink/10 px-5 py-5">
            <div className="flex items-center gap-3">
              <div className="grid h-10 w-10 place-items-center bg-ink text-parchment">
                <BarChart3 className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <div className="text-base font-semibold tracking-[0.02em]">电商问数</div>
                <div className="text-xs text-ink/50">shopkeeper-agent</div>
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4">
            <button
              type="button"
              onClick={clearConversation}
              className="flex h-11 w-full items-center justify-center gap-2 bg-ink text-sm font-semibold text-parchment transition hover:bg-soot disabled:cursor-not-allowed disabled:bg-ink/35"
            >
              <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
              新会话
            </button>

            {conversations.length > 0 && <section>
              <div className="mb-2 px-1 text-xs font-semibold text-ink/45">历史会话</div>
              <div className="max-h-60 space-y-1 overflow-y-auto">
                {conversations.map((item) => <button
                  key={item.key} type="button" onClick={() => openConversation(item.key)}
                  className={cn("flex w-full items-center gap-2 border px-3 py-2 text-left text-sm",
                    item.key === state.selectedKey ? "border-moss/30 bg-moss/10" : "border-transparent hover:bg-white/60")}
                  title={item.title}
                >
                  <span className="min-w-0 flex-1 truncate">{item.title}</span>
                  {item.running ? <span className="inline-flex shrink-0 items-center gap-1 text-xs text-moss"><LoaderCircle className="h-3 w-3 animate-spin" />运行中</span>
                    : item.unread ? <span className="shrink-0 text-xs font-medium text-moss">有新回复</span> : null}
                </button>)}
              </div>
            </section>}

            <section>
              <div className="mb-2 flex items-center gap-2 px-1 text-xs font-semibold uppercase tracking-[0.16em] text-ink/45">
                <History className="h-3.5 w-3.5" aria-hidden="true" />
                样例
              </div>
              <div className="space-y-2">
                {examples.map((example) => (
                  <button
                    key={example}
                    type="button"
                    disabled={!current.loaded || isStreaming || loading || externalRunning}
                    onClick={() => startQuery(example)}
                    className="w-full border border-ink/10 bg-white/42 px-3 py-3 text-left text-sm leading-5 text-ink/75 transition hover:border-moss/35 hover:bg-white/75 disabled:cursor-not-allowed disabled:opacity-55"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </section>
          </div>

          <div className="border-t border-ink/10 p-4">
            <div className="grid gap-2 text-xs text-ink/55">
              <div className="flex items-center justify-between gap-3">
                <span className="inline-flex items-center gap-2">
                  <Server className="h-3.5 w-3.5" aria-hidden="true" />
                  API
                </span>
                <span className="truncate font-mono">{API_BASE_URL}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="inline-flex items-center gap-2">
                  <Activity className="h-3.5 w-3.5" aria-hidden="true" />
                  完成
                </span>
                <span>{completedCount}</span>
              </div>
            </div>
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-col overflow-hidden">
          <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/10 bg-parchment/88 px-4 backdrop-blur lg:px-6">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid h-9 w-9 shrink-0 place-items-center bg-moss text-white lg:hidden">
                <BarChart3 className="h-4 w-4" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-ink">智能数据分析 Agent</div>
                <div className="truncate text-xs text-ink/45">支持追问 · 会话自动保存 · 可切换后台查询</div>
              </div>
            </div>
            <div className="flex items-center gap-2">
            <select aria-label="选择历史会话" value={state.selectedKey}
              onChange={(event) => openConversation(event.target.value)}
              className="max-w-40 border border-ink/10 bg-transparent p-1 text-xs lg:hidden">
              {conversations.map((item) => <option key={item.key} value={item.key}>{item.title}{item.running ? " · 运行中" : item.unread ? " · 有新回复" : ""}</option>)}
            </select>
            <button
              type="button"
              onClick={clearConversation}
              className={cn(
                "grid h-9 w-9 place-items-center rounded-full text-ink/55 transition hover:bg-ink/5 hover:text-ink disabled:cursor-not-allowed disabled:opacity-35",
              )}
              title="新会话（其他查询继续执行）"
              aria-label="新会话"
            >
              <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
            </button>
            </div>
          </header>

          {sessionError && <div role="alert" className="border-b border-tomato/20 bg-tomato/10 px-4 py-2 text-sm text-tomato">{sessionError}</div>}

          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            {messages.length === 0 ? (
              <EmptyState examples={examples} onUseExample={(example) => setDraft(example)} />
            ) : (
              <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6 lg:px-8">
                {messages.map((message) => (
                  <MessageBubble key={message.id} message={message} />
                ))}
              </div>
            )}
          </div>

          <div className="border-t border-ink/10 bg-[#efe6d8]/45 px-4 py-2 text-center text-xs text-ink/45">
            <span className="inline-flex items-center gap-2">
              <Leaf className="h-3.5 w-3.5 text-moss" aria-hidden="true" />
              {loading ? "正在读取会话…" : isStreaming ? "当前会话运行中，可以切换或新建会话" : externalRunning ? "正在同步未结束的查询，可切换到其他会话" : "可以继续追问；新会话会独立开始"}
              {backgroundCount > 0 && <span>· 另有 {backgroundCount} 个会话运行中</span>}
            </span>
          </div>
          <Composer
            value={draft}
            disabled={!canSubmit}
            isStreaming={isStreaming}
            onChange={setDraft}
            onSubmit={() => startQuery()}
            onStop={stopQuery}
          />
        </main>
      </div>
    </div>
  );
}
