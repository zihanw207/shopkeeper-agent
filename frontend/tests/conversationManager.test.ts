import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { test } from "node:test";
import { ConversationManager, conversationItems, type ConversationApi } from "../src/lib/conversationManager";
import type { AgentEvent, Conversation, ConversationDetail, ResultAnalysis, ChatMessage } from "../src/types/agent";
import { applyAgentEvent, restoreMessages } from "../src/lib/conversationMessages";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const conversation = (id: string): Conversation => ({ id, title: id, created_at: 1, updated_at: 1 });
const detail = (id: string, running = false): ConversationDetail => ({
  ...conversation(id), turns: [{
    id: `turn-${id}`, query: `问题${id}`, status: running ? "running" : "completed",
    resolved_query: `完整问题${id}`, context: null, created_at: 1,
    outcome: running ? null : { type: "result", data: [{ name: id }] },
  }],
});

const analysis: ResultAnalysis = {
  status: "partial",
  summary: { text: "2 月销售额比 1 月下降约 68.1%。", evidence_ids: ["E1"] },
  findings: [{ text: "还需补查订单数与客单价。", evidence_ids: ["E1"] }],
  limitations: ["仅凭总额不能确认下降原因。"], next_steps: ["按品类比较两个时期。"],
  evidence: [{ id: "E1", question: "月销售额", sql: "SELECT 1", data: [{ month: 1 }], row_count: 1, preview_truncated: false, possibly_limited: false }],
  followup_count: 0,
};

test("实时回复展示分析正文，历史恢复保留证据、限制与原始表格", () => {
  const original: ChatMessage = { id: "x", role: "assistant", content: "正在查询", createdAt: 1, status: "streaming" };
  const outcome = { type: "result" as const, data: [{ month: 1 }], analysis };
  const live = applyAgentEvent(original, outcome);
  const saved = detail("A");
  saved.turns[0].outcome = outcome;
  const restored = restoreMessages(saved)[1];
  assert.equal(live.content, analysis.summary.text);
  assert.equal(restored.content, live.content);
  assert.deepEqual(restored.analysis, analysis);
  assert.deepEqual(restored.result, live.result);
});

test("分析超时保留成功查询的数据，并终止遗留的运行中步骤", () => {
  const original: ChatMessage = {
    id: "x", role: "assistant", content: "正在补查", createdAt: 1, status: "streaming",
    steps: [{ step: "补查 1 · 执行SQL", status: "running", updatedAt: 1 }],
  };
  const done = applyAgentEvent(original, { type: "result", data: [1], analysis });
  assert.equal(done.status, "done");
  assert.equal(done.analysis?.status, "partial");
  assert.equal(done.error, undefined);
  assert.equal(done.steps?.[0].status, "error");
  assert.deepEqual(done.result, [1]);
});

test("旧会话没有分析字段时仍能恢复原来的结果提示", () => {
  const restored = restoreMessages(detail("legacy"))[1];
  assert.equal(restored.analysis, undefined);
  assert.match(restored.content, /查询完成/);
  assert.deepEqual(restored.result, [{ name: "legacy" }]);
});

function harness() {
  let serial = 0;
  const requests: { query: string; options: Parameters<ConversationApi["streamQuery"]>[1]; done: ReturnType<typeof deferred<void>> }[] = [];
  const reads: string[] = [];
  const api: ConversationApi = {
    createConversation: async () => conversation(`server-${++serial}`),
    listConversations: async () => [],
    getConversation: async (id) => { reads.push(id); return { ...conversation(id), turns: [] }; },
    streamQuery: (query, options) => {
      const done = deferred<void>();
      requests.push({ query, options, done });
      options.signal?.addEventListener("abort", () => done.reject(new DOMException("Stopped", "AbortError")), { once: true });
      return done.promise;
    },
  };
  const manager = new ConversationManager(api);
  const current = () => manager.getSnapshot().sessions[manager.getSnapshot().selectedKey];
  const session = (key: string) => manager.getSnapshot().sessions[key];
  const finish = (index: number, data: unknown) => {
    requests[index].options.onEvent({ type: "result", data });
    requests[index].done.resolve();
  };
  return { api, manager, requests, reads, current, session, finish };
}

test("A 与 B 并行、事件交错和完成乱序时，消息与完成标记仍归属于各自会话", async () => {
  const h = harness();
  const a = h.manager.getSnapshot().selectedKey;
  const runA = h.manager.startQuery("问题A");
  await setImmediate();
  h.manager.newConversation();
  const b = h.manager.getSnapshot().selectedKey;
  const runB = h.manager.startQuery("问题B");
  await setImmediate();
  h.requests[0].options.onEvent({ type: "progress", step: "A的步骤", status: "running" });
  h.requests[1].options.onEvent({ type: "progress", step: "B的步骤", status: "running" });
  assert.equal(h.session(a).messages[1].steps?.[0].step, "A的步骤");
  assert.equal(h.current().messages[1].steps?.[0].step, "B的步骤");
  assert.equal(h.requests[0].options.signal?.aborted, false);
  h.finish(1, [{ result: "B" }]);
  await runB;
  assert.equal(h.session(a).running, true);
  assert.equal(h.session(b).running, false);
  h.finish(0, [{ result: "A" }]);
  await runA;
  assert.equal(h.manager.getSnapshot().selectedKey, b);
  assert.deepEqual(h.current().messages[1].result, [{ result: "B" }]);
  assert.equal(h.session(a).unread, true);
  await h.manager.open(a);
  assert.deepEqual(h.current().messages[1].result, [{ result: "A" }]);
  assert.equal(h.current().unread, false);
  assert.deepEqual(h.reads, []); // 切回本页运行过的会话不会重新加载旧快照。
});

test("同一会话双击发送被拦截，不妨碍另一个会话启动", async () => {
  const h = harness();
  const runA = h.manager.startQuery("A");
  await h.manager.startQuery("重复A");
  h.manager.newConversation();
  const runB = h.manager.startQuery("B");
  await setImmediate();
  assert.deepEqual(h.requests.map((request) => request.query), ["A", "B"]);
  h.finish(0, []); h.finish(1, []);
  await Promise.all([runA, runB]);
});

test("停止 B 只中止 B 的连接，A 保持运行且继续接收结果", async () => {
  const h = harness();
  const a = h.manager.getSnapshot().selectedKey;
  const runA = h.manager.startQuery("A");
  await setImmediate();
  h.manager.newConversation();
  const runB = h.manager.startQuery("B");
  await setImmediate();
  h.manager.stopQuery();
  await runB;
  assert.equal(h.requests[1].options.signal?.aborted, true);
  assert.equal(h.requests[0].options.signal?.aborted, false);
  assert.equal(h.session(a).running, true);
  h.requests[1].options.onEvent({ type: "result", data: "停止后到达的旧事件" });
  h.finish(0, [{ amount: 12 }]);
  await runA;
  assert.deepEqual(h.session(a).messages[1].result, [{ amount: 12 }]);
  assert.notEqual(h.current().messages[1]?.result, "停止后到达的旧事件");
});

test("创建 A 尚未返回就切到 B，A 后到的 ID 不会抢回选择或发错 conversation_id", async () => {
  const h = harness();
  const createA = deferred<Conversation>();
  let creates = 0;
  h.api.createConversation = () => ++creates === 1 ? createA.promise : Promise.resolve(conversation("B"));
  const a = h.manager.getSnapshot().selectedKey;
  const runA = h.manager.startQuery("问题A");
  h.manager.newConversation();
  const b = h.manager.getSnapshot().selectedKey;
  const runB = h.manager.startQuery("问题B");
  await setImmediate();
  assert.equal(conversationItems(h.manager.getSnapshot()).find((item) => item.key === a)?.running, true);
  createA.resolve(conversation("A"));
  await setImmediate();
  assert.equal(h.manager.getSnapshot().selectedKey, b);
  assert.equal(h.current().conversationId, "B");
  assert.equal(h.session(a).conversationId, "A");
  assert.deepEqual(h.requests.map((request) => [request.query, request.options.conversationId]), [["问题B", "B"], ["问题A", "A"]]);
  h.api.listConversations = async () => [conversation("A"), conversation("B")];
  await h.manager.refreshList();
  assert.equal(conversationItems(h.manager.getSnapshot()).length, 2);
  h.finish(0, []); h.finish(1, []);
  await Promise.all([runA, runB]);
});

test("快速切换历史时，慢返回的 A 不覆盖当前的 B", async () => {
  const h = harness();
  const a = deferred<ConversationDetail>();
  const b = deferred<ConversationDetail>();
  h.api.getConversation = (id) => id === "A" ? a.promise : b.promise;
  const openA = h.manager.open("A");
  const openB = h.manager.open("B");
  b.resolve(detail("B")); await openB;
  a.resolve(detail("A")); await openA;
  assert.equal(h.manager.getSnapshot().selectedKey, "B");
  assert.equal(h.current().messages[0].content, "问题B");
  await h.manager.open("A");
  assert.equal(h.current().messages[0].content, "问题A");
});

test("切换会话保留各自尚未发送的草稿", async () => {
  const h = harness();
  const a = h.manager.getSnapshot().selectedKey;
  h.manager.setDraft("A的草稿");
  h.manager.newConversation();
  const b = h.manager.getSnapshot().selectedKey;
  h.manager.setDraft("B的草稿");
  await h.manager.open(a);
  assert.equal(h.current().draft, "A的草稿");
  await h.manager.open(b);
  assert.equal(h.current().draft, "B的草稿");
});

test("A 网络失败和随后的恢复请求失败都不会清除 B 的连接、结果或错误状态", async () => {
  const h = harness();
  h.api.getConversation = async () => { throw new Error("同步A失败"); };
  const a = h.manager.getSnapshot().selectedKey;
  const runA = h.manager.startQuery("A");
  await setImmediate();
  h.manager.newConversation();
  const runB = h.manager.startQuery("B");
  await setImmediate();
  h.requests[0].done.reject(new Error("A断网"));
  await runA;
  await setImmediate();
  assert.equal(h.session(a).messages[1].error, "A断网");
  assert.equal(h.session(a).error, "同步A失败");
  assert.equal(h.current().running, true);
  assert.equal(h.current().error, "");
  h.finish(1, [{ name: "B" }]);
  await runB;
  assert.deepEqual(h.current().messages[1].result, [{ name: "B" }]);
});

test("没有收到流式终态时只恢复对应会话，不能把 A 的服务端结果放到 B", async () => {
  const h = harness();
  h.api.getConversation = async (id) => detail(id);
  const a = h.manager.getSnapshot().selectedKey;
  const runA = h.manager.startQuery("A");
  await setImmediate();
  h.manager.newConversation();
  h.manager.setDraft("B尚未发送");
  h.requests[0].done.resolve();
  await runA;
  await setImmediate();
  assert.deepEqual(h.session(a).messages[1].result, [{ name: "server-1" }]);
  assert.equal(h.current().draft, "B尚未发送");
  assert.equal(h.current().messages.length, 0);
});

test("恢复的外部运行会话切到后台后仍同步状态，并显示新回复", async () => {
  const h = harness();
  let running = true;
  h.api.getConversation = async (id) => detail(id, running);
  await h.manager.open("A");
  await h.manager.startQuery("不能和A重叠");
  assert.equal(h.requests.length, 0);
  h.manager.newConversation();
  h.manager.setDraft("B的草稿");
  running = false;
  await h.manager.syncRunning();
  assert.equal(h.session("A").externalRunning, false);
  assert.equal(h.session("A").unread, true);
  assert.equal(h.current().draft, "B的草稿");
});

test("StrictMode 的重复初始化和慢历史列表都不抢回新会话", async () => {
  const h = harness();
  const list = deferred<Conversation[]>();
  h.api.listConversations = () => list.promise;
  h.manager.initialize("A");
  h.manager.newConversation();
  const b = h.manager.getSnapshot().selectedKey;
  h.manager.initialize("A");
  list.resolve([conversation("A")]);
  await setImmediate();
  assert.equal(h.manager.getSnapshot().selectedKey, b);
  assert.deepEqual(h.reads, ["A"]);
});

test("已经收到成功终态时，流随后断连不能把成功改成失败", async () => {
  const h = harness();
  const run = h.manager.startQuery("A");
  await setImmediate();
  const event: AgentEvent = { type: "result", data: [{ amount: 1 }] };
  h.requests[0].options.onEvent(event);
  h.requests[0].done.reject(new Error("end of stream"));
  await run;
  assert.equal(h.current().messages[1].status, "done");
  assert.deepEqual(h.current().messages[1].result, [{ amount: 1 }]);
  assert.equal(h.current().externalRunning, false);
});
