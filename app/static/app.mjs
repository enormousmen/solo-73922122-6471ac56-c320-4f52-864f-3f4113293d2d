// 预演页交互：首屏先给结论，展开某轮时才补算该轮的未静默活动区间。
//
// 结果表的所有权规则：
// * 只有新预演成功才 applyTable（generation +1），失败/等待期间保留旧表；
// * 每次展开带 generation 与 simulation_id 发起请求，返回时若已换表则直接丢弃。

export const VERDICT_TEXT = {
  delayed: "首呼推迟", suppressed: "新增静默后收不到首呼", unchanged: "无变化",
  earlier: "首呼提前", newly_called: "新增静默后反而首呼", still_not_called: "前后均未首呼",
};
export const AFFECTED = new Set(["delayed", "suppressed", "earlier", "newly_called"]);

export function fmt(t) {
  return t ? t.replace("T", " ").replace("Z", " UTC") : "—";
}
export function fmtDelay(sec) {
  if (sec === null || sec === undefined) return "—";
  if (sec === 0) return "0 分钟";
  const m = Math.round(sec / 60 * 10) / 10;
  return (m > 0 ? "+" : "") + m + " 分钟";
}
export function sortRounds(rows) {
  // 值班主任先看受影响的轮次；其余保持服务/轮次顺序
  return rows
    .map((r, i) => ({r, i}))
    .sort((a, b) =>
      (AFFECTED.has(b.r.verdict) ? 1 : 0) - (AFFECTED.has(a.r.verdict) ? 1 : 0)
      || a.i - b.i)
    .map(x => x.r);
}
// 换表后到达的旧明细必须丢弃；同一张表内仍可写入
export function isStaleDetail({startedGeneration, generation, table, simId}) {
  return startedGeneration !== generation || !table || table.sim !== simId;
}

// --------------------------------------------------------------------------
// 结果表控制器：不碰 DOM，便于在 Node 里验证“谁的表、谁的明细”
// --------------------------------------------------------------------------

export function detailKey(simId, service, roundIndex) {
  return simId + "|" + service + "|" + roundIndex;
}

export function createStore(fetchImpl) {
  let table = null;        // { sim, data, generation }
  let generation = 0;
  const cache = new Map();

  return {
    get table() { return table; },
    get generation() { return generation; },
    get cache() { return cache; },

    // 只有预演成功才会调用；一次换表让所有在途明细过期
    applyTable(simId, data) {
      generation += 1;
      cache.clear();
      table = { sim: simId, data, generation };
      return generation;
    },

    async loadDetail(simId, service, roundIndex) {
      const key = detailKey(simId, service, roundIndex);
      if (!table || table.sim !== simId) return { stale: true };
      if (cache.has(key)) return { stale: false, data: cache.get(key) };

      const startedGeneration = generation;
      const r = await fetchImpl("/api/round-details", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          simulation_id: simId, service, round_index: Number(roundIndex),
        }),
      });
      const data = await r.json();
      if (!r.ok) {
        const msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
        // 失败响应同样不能写进已经换掉的表
        if (isStaleDetail({startedGeneration, generation, table, simId})) {
          return { stale: true };
        }
        return { stale: false, error: msg };
      }
      // 换表后到达的旧明细直接丢弃，不写入缓存、不渲染
      if (isStaleDetail({startedGeneration, generation, table, simId})) {
        return { stale: true };
      }
      cache.set(key, data);
      return { stale: false, data };
    },

    // 预演成功才换表；失败返回原因且保留旧表
    async simulate(body) {
      let response;
      try {
        response = await fetchImpl("/api/simulate", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
      } catch (e) {
        return { ok: false, error: e.message || String(e) };
      }
      let data;
      try { data = await response.json(); }
      catch (e) { return { ok: false, error: "响应不是合法 JSON：" + e.message }; }
      if (!response.ok) {
        return {
          ok: false,
          error: typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail),
        };
      }
      this.applyTable(data.simulation_id, data);
      return { ok: true, data };
    },
  };
}

function callCell(res) {
  if (!res.first_call_at) {
    const note = res.still_waiting_at_cutoff
      ? '<span class="waiting-note">截止时仍在等待，未首呼</span>'
      : '<span class="waiting-note">未首呼（未静默持续不足或全程被屏蔽）</span>';
    return '<span class="notcalled">未首呼</span>' + note
      + '<div class="alerts-cell" style="margin-top:4px">随呼告警：—</div>';
  }
  return '<strong>' + fmt(res.first_call_at) + '</strong>'
    + '<div class="alerts-cell" style="margin-top:4px">随呼告警：'
    + (res.called_alert_ids.join(", ") || "—") + '</div>';
}
function scanNote(res) {
  const s = res.scan;
  if (!s) return "";
  return s.stopped_early
    ? '<span class="hint">首屏扫描：检查 ' + s.events_scanned + '/' + s.events_total
      + ' 个时点后首呼已确定</span>'
    : '<span class="hint">首屏扫描：检查 ' + s.events_scanned + '/' + s.events_total + ' 个时点</span>';
}
function stretchesHtml(res) {
  if (!res.unsilenced_stretches || !res.unsilenced_stretches.length)
    return '<div class="stretch">无</div>';
  return res.unsilenced_stretches.map(s =>
    '<div class="stretch">[' + fmt(s.start) + ' → ' + fmt(s.end)
    + (s.open_ended ? "（截止截断）" : "") + ')</div>').join("");
}
export function summaryHtml(data) {
  const s = data.summary;
  const pills = [
    ["故障轮次", s.total_rounds],
    ["首呼受影响", s.affected, "var(--warn)", true],
    ["加静默前首呼", s.before_called],
    ["加静默后首呼", s.after_called],
    ["推迟", s.delayed, "var(--warn)"],
    ["被压掉", s.suppressed, "var(--bad)"],
    ["无变化", s.unchanged, "var(--ok)"],
    ["前后均未首呼", s.still_not_called],
    ["加后未首呼合计", s.not_called_after, "var(--bad)"],
  ];
  let html = '<div class="summary">' + pills.map(p =>
    '<span class="pill' + (p[3] ? " hot" : "") + '">' + p[0]
    + '<b style="color:' + (p[2] || "var(--text)") + '">' + p[1] + '</b></span>'
  ).join("") + '</div>';
  html += '<p class="legend">截止时仍在等待：加静默前 ' + s.before_waiting_at_cutoff
    + ' 轮 / 加静默后 ' + s.after_waiting_at_cutoff + ' 轮。'
    + '受影响轮次排在前面；展开某轮才向服务端补算该轮的未静默活动区间（沿用本表生成时的输入）。</p>';
  return html;
}
export function rowHtml(r, simId) {
  const key = simId + "|" + r.service + "|" + r.round_index;
  const affected = AFFECTED.has(r.verdict);
  return '<tr class="' + (affected ? "" : "row-still") + '">'
    + '<td>' + r.service + '<br><span class="hint">第 ' + r.round_index + ' 轮 · '
    + fmt(r.started_at) + ' → ' + fmt(r.ended_at) + (r.ended_at ? '' : '（未结束）') + '</span></td>'
    + '<td><span class="badge v-' + r.verdict + '">' + VERDICT_TEXT[r.verdict] + '</span></td>'
    + '<td>' + (affected ? fmtDelay(r.delay_seconds) : "—") + '</td>'
    + '<td>' + callCell(r.before) + scanNote(r.before) + '</td>'
    + '<td>' + callCell(r.after) + scanNote(r.after) + '</td>'
    + '<td><details class="detail" data-key="' + key + '"><summary>展开未静默区间</summary>'
    + '<div class="detail-body"><div class="detail-msg">展开时加载…</div></div></details></td>'
    + '</tr>';
}
export function tableHtml(simId, data) {
  let html = '<div class="card">' + summaryHtml(data);
  if (data.ignored_alerts_after_cutoff.length)
    html += '<p class="hint">以下告警在观察截止之后才触发，已忽略：'
      + data.ignored_alerts_after_cutoff.join(", ") + '</p>';
  html += '<table><thead><tr>'
    + '<th>服务 / 轮次</th><th>判定</th><th>推迟量</th>'
    + '<th>加静默前：首呼 + 随呼未静默活动告警 + 截止状态</th>'
    + '<th>加静默后：首呼 + 随呼未静默活动告警 + 截止状态</th>'
    + '<th>逐轮明细</th></tr></thead><tbody>';
  html += sortRounds(data.rounds).map(r => rowHtml(r, simId)).join("");
  return html + '</tbody></table></div>';
}
export function detailHtml(d) {
  return '<div class="detail-msg">该轮明细按生成此表时的输入计算。</div>'
    + '<h4>加静默前 · 未静默活动区间（' + d.before.unsilenced_stretches.length + ' 段）</h4>'
    + stretchesHtml(d.before)
    + '<h4>加静默后 · 未静默活动区间（' + d.after.unsilenced_stretches.length + ' 段）</h4>'
    + stretchesHtml(d.after);
}

// --------------------------------------------------------------------------
// 浏览器装配（Node 单元测试只导入上面的纯函数，不会执行到这里）
// --------------------------------------------------------------------------

export function init(win = globalThis) {
  const doc = win.document;
  const $ = (id) => doc.getElementById(id);
  let running = false;
  const store = createStore(win.fetch.bind(win));

  function showError(msg) {
    $("error").textContent = msg || "";
    $("error").style.display = msg ? "block" : "none";
  }
  function showStatus(msg) {
    $("status").textContent = msg || "";
    $("status").className = msg ? "on" : "";
  }
  function parseArea(id, name) {
    const raw = $(id).value.trim();
    if (!raw) return [];
    try { return JSON.parse(raw); }
    catch (e) { throw new Error(name + " 不是合法 JSON：" + e.message); }
  }

  async function loadDetail(detailsEl) {
    const body = detailsEl.querySelector(".detail-body");
    const key = detailsEl.dataset.key;
    const [simId, service, roundIdx] = key.split("|");
    if (store.cache.has(key)) {
      body.innerHTML = detailHtml(store.cache.get(key));
      return;
    }
    body.innerHTML = '<div class="detail-msg">正在补算该轮未静默活动区间…</div>';
    const result = await store.loadDetail(simId, service, roundIdx);
    if (result.stale) return;         // 换表后到达的旧明细：直接丢弃
    if (!detailsEl.isConnected) return;
    body.innerHTML = result.error
      ? '<div class="detail-msg" style="color:var(--bad)">明细加载失败：'
        + result.error + '</div>'
      : detailHtml(result.data);
  }
  function bindDetails() {
    doc.querySelectorAll("details.detail").forEach(el => {
      el.addEventListener("toggle", () => { if (el.open) loadDetail(el); });
    });
  }
  function applyTable(simId, data) {
    store.applyTable(simId, data);
    $("results").innerHTML = tableHtml(simId, data);
    bindDetails();
  }

  async function run() {
    if (running) return;
    showError("");
    let body;
    try {
      body = {
        alerts: parseArea("alerts", "告警数据"),
        current_silences: parseArea("current", "现行静默规则"),
        proposed_silences: parseArea("proposed", "拟新增静默规则"),
        wait_minutes: parseFloat($("wait").value),
        cutoff_at: $("cutoff").value.trim(),
        summary_only: true,   // 首屏只要结论；明细在展开时按 simulation_id 补算
      };
    } catch (e) {
      showError(e.message);
      return;
    }
    running = true;
    $("run").disabled = true;
    $("run").textContent = "预演中…";
    showStatus(store.table
      ? "正在预演新结果；期间旧表仍可展开明细，成功后才换表。"
      : "正在预演…");
    try {
      const result = await store.simulate(body);
      if (!result.ok) {
        // 失败不换表：旧表与它的明细继续可用，这里只说明原因
        showError("预演失败，未替换当前结果表：" + result.error);
        return;
      }
      showStatus("");
      applyTable(result.data.simulation_id, result.data);
    } finally {
      showStatus("");
      running = false;
      $("run").disabled = false;
      $("run").textContent = "运行预演";
    }
  }

  async function loadSample() {
    const r = await win.fetch("/api/sample");
    const s = await r.json();
    $("alerts").value = JSON.stringify(s.alerts, null, 2);
    $("current").value = JSON.stringify(s.current_silences, null, 2);
    $("proposed").value = JSON.stringify(s.proposed_silences, null, 2);
    $("wait").value = s.wait_minutes;
    $("cutoff").value = s.cutoff_at;
  }

  $("run").onclick = run;
  $("loadSample").onclick = loadSample;
  $("alertFile").onchange = async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    $("alerts").value = await f.text();
  };
  loadSample();
  return {store, run, applyTable, loadDetail, loadSample};
}

if (typeof document !== "undefined") {
  init();
}
