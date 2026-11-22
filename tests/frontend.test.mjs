// 预演页纯逻辑自测（node --test tests/frontend.test.mjs）
// 这里测“首屏先结论、展开才补明细”的渲染，以及换表/过期明细的所有权规则，
// 全部不依赖浏览器。

import test from "node:test";
import assert from "node:assert/strict";

import {
  AFFECTED,
  createStore,
  detailKey,
  isStaleDetail,
  sortRounds,
  summaryHtml,
  tableHtml,
} from "../app/static/app.mjs";

const round = (verdict, service, idx = 1) => ({
  service, round_index: idx, verdict, delay_seconds: verdict === "delayed" ? 600 : null,
  started_at: "2026-11-21T10:00:00Z", ended_at: null, alert_ids: ["a"],
  before: {
    first_call_at: verdict === "still_not_called" ? null : "2026-11-21T10:10:00Z",
    called_alert_ids: ["a"], still_waiting_at_cutoff: verdict === "suppressed",
    scan: { events_total: 4, events_scanned: 2, stopped_early: true },
  },
  after: {
    first_call_at: verdict === "suppressed" || verdict === "still_not_called" ? null : "2026-11-21T10:20:00Z",
    called_alert_ids: ["a"], still_waiting_at_cutoff: verdict === "suppressed",
    scan: { events_total: 6, events_scanned: 6, stopped_early: false },
  },
});

const payload = (verdicts) => ({
  simulation_id: "sim-x",
  summary: {
    total_rounds: verdicts.length,
    affected: verdicts.filter(v => AFFECTED.has(v)).length,
    before_called: 1, after_called: 1, delayed: 0, suppressed: 0, unchanged: 0,
    earlier: 0, newly_called: 0, still_not_called: 0, not_called_after: 0,
    before_waiting_at_cutoff: 0, after_waiting_at_cutoff: 0,
  },
  ignored_alerts_after_cutoff: [],
  rounds: verdicts.map((v, i) => round(v, "svc" + i)),
});

const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

test("受影响轮次排在前面且保持原有相对顺序", () => {
  const rows = sortRounds(payload(["unchanged", "delayed", "still_not_called", "suppressed"]).rounds);
  assert.deepEqual(rows.map(r => r.verdict), ["delayed", "suppressed", "unchanged", "still_not_called"]);
});

test("首屏汇总含受影响轮数与截止时等待状态", () => {
  const html = summaryHtml(payload(["delayed", "unchanged"]));
  assert.match(html, /首呼受影响/);
  assert.match(html, /截止时仍在等待/);
});

test("首屏每轮直接给出两套首呼、随呼告警与扫描是否提前结束", () => {
  const html = tableHtml("sim-1", payload(["delayed"]));
  assert.match(html, /10:10:00 UTC/);       // 加静默前首呼
  assert.match(html, /10:20:00 UTC/);       // 加静默后首呼
  assert.match(html, /随呼告警：a/);
  assert.match(html, /首呼已确定/);          // stopped_early 的提示
  assert.match(html, /展开未静默区间/);
  // 首屏不带区间，展开才补算
  assert.equal(html.includes("[2026-11-21 10:00:00 UTC → "), false);
});

test("未首呼时给出截止时等待状态", () => {
  const html = tableHtml("sim-1", payload(["suppressed"]));
  assert.match(html, /截止时仍在等待，未首呼/);
});

test("过期判定的边界", () => {
  const table = { sim: "old" };
  assert.equal(isStaleDetail({ startedGeneration: 3, generation: 3, table, simId: "old" }), false);
  assert.equal(isStaleDetail({ startedGeneration: 3, generation: 4, table: { sim: "new" }, simId: "old" }), true);
  // generation 相同但表换了（防御性）
  assert.equal(isStaleDetail({ startedGeneration: 4, generation: 4, table: { sim: "new" }, simId: "old" }), true);
  assert.equal(isStaleDetail({ startedGeneration: 0, generation: 0, table: null, simId: "old" }), true);
});

test("预演成功才换表；失败保留旧表并给出原因", async () => {
  const calls = [];
  const store = createStore(async (url) => {
    calls.push(url);
    return jsonResponse({ detail: "静默规则 S1 的终点必须晚于起点" }, 422);
  });
  store.applyTable("old", payload(["unchanged"]));
  const oldTable = store.table;

  const result = await store.simulate({ alerts: [] });
  assert.equal(result.ok, false);
  assert.match(result.error, /终点必须晚于起点/);
  assert.equal(store.table, oldTable);            // 表没换
  assert.equal(store.generation, 1);

  const ok = createStore(async () => jsonResponse({ ...payload(["delayed"]), simulation_id: "new" }));
  ok.applyTable("old", payload(["unchanged"]));
  const applied = await ok.simulate({ alerts: [] });
  assert.equal(applied.ok, true);
  assert.equal(ok.table.sim, "new");
  assert.equal(ok.generation, 2);
});

test("等待期间旧表仍可展开；换表后旧表在途明细被丢弃", async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const store = createStore(async (url, opts) => {
    if (url !== "/api/round-details") throw new Error("unexpected " + url);
    return gate;
  });
  store.applyTable("old", payload(["delayed"]));
  const pending = store.loadDetail("old", "svc0", 1);

  // 预演成功换表
  store.applyTable("new", payload(["unchanged"]));
  // 旧明细此刻才返回
  release(jsonResponse({
    simulation_id: "old", service: "svc0", round_index: 1,
    before: { unsilenced_stretches: [] }, after: { unsilenced_stretches: [] },
  }));
  const result = await pending;
  assert.equal(result.stale, true);
  assert.equal(store.cache.has(detailKey("old", "svc0", 1)), false);
  assert.equal(store.cache.size, 0);              // 换表时缓存已清空，旧明细也未写入
});

test("同一张表内明细可缓存复用；失败不写入缓存", async () => {
  let n = 0;
  const store = createStore(async (url, opts) => {
    n += 1;
    const body = JSON.parse(opts.body);
    assert.equal(body.simulation_id, "sim-1");
    assert.equal(body.round_index, 1);
    return jsonResponse({
      simulation_id: "sim-1", service: body.service, round_index: 1,
      before: { unsilenced_stretches: [] }, after: { unsilenced_stretches: [] },
    });
  });
  const data = payload(["delayed"]);
  store.applyTable("sim-1", data);

  const first = await store.loadDetail("sim-1", "svc0", 1);
  assert.equal(first.stale, false);
  const second = await store.loadDetail("sim-1", "svc0", 1);
  assert.equal(second.stale, false);
  assert.equal(n, 1);                              // 命中缓存，没有重复请求

  const bad = createStore(async () => jsonResponse({ detail: "该轮次不属于这次预演结果" }, 404));
  bad.applyTable("sim-1", data);
  const failed = await bad.loadDetail("sim-1", "nope", 1);
  assert.equal(failed.stale, false);
  assert.match(failed.error, /不属于这次预演结果/);
  assert.equal(bad.cache.size, 0);
});

test("尚无结果表时展开不会发请求", async () => {
  let called = 0;
  const store = createStore(async () => { called += 1; return jsonResponse({}); });
  const result = await store.loadDetail("sim-1", "svc0", 1);
  assert.equal(result.stale, true);
  assert.equal(called, 0);
});
