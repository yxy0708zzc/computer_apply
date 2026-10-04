/* app.js —— tripai 前端 v3
   模式选择（AI 对话 / 手动查票）；agent 执行块（工具调用内嵌思考文字中，随折叠收起）；
   对话详情=行式平铺全量数据；会话右键重命名/删除；手动模式无 AI 直连引擎 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let CURRENT_SID = null;
  let currentCtrl = null;
  let streaming = false;
  let META = { seats: [], default_date: "" };
  let VIEW = "mode";               // mode | chat | detail | manual
  let emptySession = true;         // 当前会话是否为空（禁用新对话）

  /* ═══════════ 基础工具 ═══════════ */
  function md(text) {
    if (!text) return "";
    try {
      const html = marked.parse(text);
      return window.DOMPurify ? DOMPurify.sanitize(html) : html;
    } catch { return esc(text); }
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function briefArgs(args) {
    try {
      const s = JSON.stringify(args);
      return s.length > 50 ? s.slice(0, 50) + "…" : s;
    } catch { return ""; }
  }
  let toastTimer = null;
  function toast(msg, ms) {
    const t = $("toast");
    t.textContent = msg; t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), ms || 2800);
  }
  function chatEl() { return $("chat"); }
  function nearBottom() {
    const c = chatEl();
    return c.scrollHeight - c.scrollTop - c.clientHeight < 130;
  }
  function scrollBottom(force) {
    const c = chatEl();
    if (force || nearBottom()) c.scrollTop = c.scrollHeight;
  }
  function setView(v) {
    VIEW = v;
    for (const id of ["mode-view", "chat-view", "detail-view", "manual-view"])
      $(id).classList.toggle("hidden", id !== v + "-view");
    $("btn-detail").classList.toggle("hidden", v === "detail" || PANEL.order.length === 0);
    $("btn-menu").classList.toggle("hidden", v !== "chat");   // 窄屏抽屉按钮仅对话视图
    closeDrawer();
  }
  function closeDrawer() {
    const sb = document.querySelector(".sidebar");
    if (sb) sb.classList.remove("open");
    const mk = $("drawer-mask");
    if (mk) mk.classList.add("hidden");
  }

  /* ═══════════ 启动 ═══════════ */
  window.addEventListener("DOMContentLoaded", init);

  async function init() {
    try { META = await API.meta(); } catch {}
    bindStatic();
    try {
      const cfg = await API.getConfig();
      if (cfg.configured) await enterMain();
      else $("gate").classList.remove("hidden");
    } catch {
      $("gate").classList.remove("hidden");
    }
  }

  function bindStatic() {
    $("gate-btn").onclick = gateEnter;
    $("btn-home").onclick = () => {
      if (streaming) { toast("对话进行中，请先停止"); return; }
      setView("mode");
    };
    $("card-ai").onclick = () => setView("chat");
    $("card-manual").onclick = () => { initManualForm(); setView("manual"); };
    $("mast-home").onclick = () => { if (!streaming) setView("mode"); };
    $("btn-new-session").onclick = newSession;
    $("btn-send").onclick = send;
    $("btn-stop").onclick = stop;
    $("btn-detail").onclick = () => { drawDetail(); setView("detail"); };
    $("btn-back").onclick = () => setView("chat");
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    $("input").addEventListener("input", autoGrow);
    $("btn-settings").onclick = openSettings;
    $("btn-settings-close").onclick = () => $("modal-settings").classList.add("hidden");
    $("btn-set-save").onclick = () => saveSettings(false);
    $("btn-set-verify").onclick = () => saveSettings(true);
    $("mf-search").onclick = manualSearch;
    $("batch-check-detail").onclick = batchCheckDetail;
    $("batch-check-manual").onclick = () => batchCheckManual();
    // 窄屏：会话抽屉
    $("btn-menu").onclick = () => {
      const sb = document.querySelector(".sidebar");
      const mk = $("drawer-mask");
      const open = sb.classList.toggle("open");
      mk.classList.toggle("hidden", !open);
    };
    $("drawer-mask").onclick = closeDrawer;
    // 右键菜单关闭
    document.addEventListener("click", () => $("ctx-menu").classList.add("hidden"));
    $("ctx-menu").addEventListener("click", (e) => e.stopPropagation());
  }
  function autoGrow() {
    const t = $("input");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 140) + "px";
  }

  /* ═══════════ 配置门页 ═══════════ */
  async function gateEnter() {
    const key = $("gate-key").value.trim();
    const model = $("gate-model").value.trim();
    const base = $("gate-base").value.trim();
    const err = $("gate-err");
    err.classList.add("hidden");
    if (!key || !model || !base) {
      err.textContent = "请填写全部三项（支持 OpenAI 兼容接口）";
      err.classList.remove("hidden");
      return;
    }
    $("gate-btn").disabled = true; $("gate-btn").textContent = "验证中…";
    try {
      await API.setConfig({ api_key: key, model, base_url: base });
      await API.verifyConfig();
      $("gate").classList.add("hidden");
      await enterMain();
      toast("配置验证通过");
    } catch (e) {
      err.textContent = "验证失败：" + e.message;
      err.classList.remove("hidden");
    } finally {
      $("gate-btn").disabled = false; $("gate-btn").textContent = "验证并进入";
    }
  }

  async function enterMain() {
    $("main").classList.remove("hidden");
    $("gate").classList.add("hidden");
    const cfg = await API.getConfig();
    $("model-badge").textContent = cfg.model || "未配置";
    $("set-key").placeholder = cfg.api_key_masked || "";
    $("set-model").value = cfg.model || "";
    $("set-base").value = cfg.base_url || "";
    setView("mode");
    await loadSessions();
    const list = await API.listSessions();
    if (list.sessions.length) await switchSession(list.sessions[0].id, true);
    else await newSession(true);
  }

  /* ═══════════ 设置 ═══════════ */
  function openSettings() {
    $("set-err").classList.add("hidden");
    $("modal-settings").classList.remove("hidden");
  }
  async function saveSettings(verify) {
    const err = $("set-err");
    err.classList.add("hidden");
    try {
      const cfg = {
        api_key: $("set-key").value.trim(),
        model: $("set-model").value.trim(),
        base_url: $("set-base").value.trim(),
      };
      if (cfg.api_key || cfg.model || cfg.base_url) await API.setConfig(cfg);
      if (verify) { await API.verifyConfig(); toast("验证通过"); }
      else toast("已保存");
      const c = await API.getConfig();
      $("model-badge").textContent = c.model || "未配置";
      $("modal-settings").classList.add("hidden");
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
    }
  }

  /* ═══════════ 会话管理 ═══════════ */
  async function loadSessions() {
    const list = await API.listSessions();
    const el = $("session-list");
    el.innerHTML = "";
    for (const s of list.sessions) {
      const div = document.createElement("div");
      div.className = "session-item" + (s.id === CURRENT_SID ? " active" : "");
      div.innerHTML = `<span class="t">${esc(s.title || "未命名")}</span>`;
      div.querySelector(".t").onclick = () => switchSession(s.id);
      div.oncontextmenu = (e) => {
        e.preventDefault();
        showCtxMenu(e, s);
      };
      el.appendChild(div);
    }
  }

  let ctxSession = null;
  function showCtxMenu(e, s) {
    const m = $("ctx-menu");
    ctxSession = s;
    m.classList.remove("hidden");
    const x = Math.min(e.clientX, window.innerWidth - 130);
    const y = Math.min(e.clientY, window.innerHeight - 90);
    m.style.left = x + "px"; m.style.top = y + "px";
    m.querySelector("[data-act=rename]").onclick = () => renameSession(s);
    m.querySelector("[data-act=delete]").onclick = () => deleteSession(s);
  }
  async function renameSession(s) {
    $("ctx-menu").classList.add("hidden");
    const t = prompt("重命名对话：", s.title || "");
    if (t === null) return;
    const title = t.trim();
    if (!title) { toast("标题不能为空"); return; }
    try {
      await fetch(`/api/sessions/${s.id}/rename`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      await loadSessions();
      toast("已重命名");
    } catch (e) { toast("重命名失败：" + e.message); }
  }
  async function deleteSession(s) {
    $("ctx-menu").classList.add("hidden");
    if (!confirm(`删除对话「${s.title || "未命名"}」？`)) return;
    await API.deleteSession(s.id);
    if (s.id === CURRENT_SID) { CURRENT_SID = null; chatEl().innerHTML = ""; await newSession(true); }
    loadSessions();
  }

  function updateNewBtn() {
    $("btn-new-session").disabled = streaming || emptySession;
    $("btn-new-session").title = emptySession ? "当前已是新对话" : "";
  }

  async function newSession(silent) {
    if (streaming) return;
    if (!silent && !emptySession) {
      const r = await API.createSession("新对话");
      CURRENT_SID = r.session_id;
    }
    chatEl().innerHTML = "";
    resetPanel();
    emptySession = true;
    updateNewBtn();
    await loadSessions();
    $("input").focus();
  }

  async function switchSession(sid, silent) {
    if (streaming) { toast("请先停止当前对话"); return; }
    CURRENT_SID = sid;
    setView("chat");
    chatEl().innerHTML = "";
    const d = await API.getSession(sid);
    resetPanel(sid);
    mergePanel(Object.values(d.solutions || {}), "");
    let lastEntry = null;
    for (const item of d.trace || []) {
      if (item.type === "user") addUserMsg(item.content);
      else if (item.type === "assistant") { lastEntry = item; renderTraceAssistant(item); }
    }
    emptySession = !(d.trace || []).length;
    updateNewBtn();
    // 补渲方案总表（总表由前端详情数据直出，不存 trace；有正式输出时恢复）
    if (PANEL.order.length && lastEntry && lastEntry.final_content) {
      const cards = chatEl().querySelectorAll(".ai-card");
      if (cards.length) renderPlanTable(cards[cards.length - 1]);
    }
    loadSessions();
    scrollBottom(true);
  }

  /* ═══════════ 消息渲染（历史静态） ═══════════ */
  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta);
      ta.select(); document.execCommand("copy"); ta.remove();
    }
    if (btn) {
      const old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = old; }, 1500);
    }
  }
  function addCopyBtn(head, getText) {
    const b = document.createElement("button");
    b.className = "btn small copy-btn";
    b.textContent = "复制";
    b.onclick = (e) => { e.stopPropagation(); copyText(getText(), b); };
    head.appendChild(b);
  }
  function collectAnswerText(card) {
    // 正式输出优先；无则拼接各轮正文（排除思考与工具块）
    const fin = card.querySelector(".final-sec .md");
    if (fin) return fin.innerText.trim();
    return [...card.querySelectorAll(".round-sec > .md")]
      .map(e => e.innerText.trim()).filter(Boolean).join("\n\n");
  }

  function addUserMsg(text) {
    const div = document.createElement("div");
    div.className = "msg-user";
    div.innerHTML = `<div class="bubble">${esc(text)}</div><div class="who">我</div>`;
    chatEl().appendChild(div);
  }

  function renderTraceAssistant(entry) {
    const div = document.createElement("div");
    div.className = "msg-ai";
    const card = document.createElement("div");
    card.className = "ai-card";
    card.innerHTML = `<div class="ai-head"><span class="ai-badge">铁程规划</span>` +
      `<span class="ai-badge">${(entry.rounds || []).length} 轮</span></div>`;
    addCopyBtn(card.querySelector(".ai-head"),
      () => entry.final_content || collectAnswerText(card));
    for (const r of entry.rounds || []) {
      const sec = buildRoundSec(r.round);
      const tb = buildThinkBlock(false);
      tb.summary.innerHTML = `<span class="dot"></span>思考 ${r.thinking_seconds || 0} 秒 ▸`;
      tb.body.textContent = r.thinking || "";
      for (const t of r.tools || []) {
        tb.body.appendChild(buildToolItemDone(t));     // 工具内嵌在思考文字中
      }
      sec.appendChild(tb.el);
      const mddiv = document.createElement("div");
      mddiv.className = "md";
      mddiv.innerHTML = md(r.content || "");
      sec.appendChild(mddiv);
      card.appendChild(sec);
    }
    if (entry.final_content) {
      const fs = document.createElement("div");
      fs.className = "final-sec";
      fs.innerHTML = `<div class="md">${md(entry.final_content)}</div>`;
      card.appendChild(fs);
    }
    div.appendChild(card);
    chatEl().appendChild(div);
  }

  /* ═══════════ agent 执行块（实时流） ═══════════ */
  let AI = null;

  function newAiCard() {
    const div = document.createElement("div");
    div.className = "msg-ai";
    div.innerHTML = `<div class="ai-card"><div class="ai-head">` +
      `<span class="ai-badge running">铁程规划 · 规划中</span></div></div>`;
    chatEl().appendChild(div);
    const card = div.firstChild;
    addCopyBtn(card.querySelector(".ai-head"), () => collectAnswerText(card));
    AI = {
      root: div, card: card, head: card.querySelector(".ai-head"),
      sec: null, think: null, mdRaw: "", mdEl: null, curTool: null,
    };
    scrollBottom(true);
  }

  function buildRoundSec(rnd) {
    const sec = document.createElement("div");
    sec.className = "round-sec";
    sec.innerHTML = `<div class="round-label">第 ${rnd} 轮</div>`;
    return sec;
  }

  /** thinking 区块：summary + body（思考文字 + 内嵌工具调用，随折叠收起） */
  function buildThinkBlock(live) {
    const el = document.createElement("div");
    el.className = "think-block" + (live ? " open" : "");
    const summary = document.createElement("div");
    summary.className = "think-summary" + (live ? " thinking" : "");
    summary.innerHTML = live ? `<span class="dot"></span>思考中…` : `思考 ▸`;
    summary.onclick = () => el.classList.toggle("open");
    const body = document.createElement("div");
    body.className = "think-body";
    el.appendChild(summary); el.appendChild(body);
    return { el, summary, body };
  }

  function buildToolItem(id, name, args) {
    const item = document.createElement("div");
    item.className = "tool-item";
    item.innerHTML =
      `<div class="tool-row"><span class="tool-state run">查询中</span>` +
      `<span class="tool-name">${esc(name)}</span><span class="tool-args">${esc(briefArgs(args))}</span></div>` +
      `<div class="tool-detail">` +
      `<div class="sec"><div class="sec-t">输入</div><pre>${esc(JSON.stringify(args, null, 2))}</pre></div>` +
      `<div class="sec"><div class="sec-t">输出</div><pre class="out">…</pre></div>` +
      `<div class="sec prog-sec hidden"><div class="tool-progress"><div class="pbar"><div></div></div><span class="pmsg"></span></div></div>` +
      `</div>`;
    item.querySelector(".tool-row").onclick = () => item.classList.toggle("open");
    return item;
  }
  function buildToolItemDone(t) {
    const item = buildToolItem(t.id, t.name, t.arguments);
    const st = item.querySelector(".tool-state");
    st.textContent = "完成"; st.className = "tool-state done";
    item.querySelector(".out").textContent = JSON.stringify(t.ai_result, null, 2);
    return item;
  }

  function flushMd(force) {
    if (!AI || !AI.mdEl) return;
    const now = Date.now();
    if (!force && now - (AI._lastMd || 0) < 200) return;
    AI._lastMd = now;
    AI.mdEl.innerHTML = md(AI.mdRaw);
    scrollBottom();
  }

  /* ═══════════ 发送 / SSE ═══════════ */
  async function send() {
    if (streaming) return;
    const text = $("input").value.trim();
    if (!text || !CURRENT_SID) return;
    setView("chat");
    $("input").value = ""; autoGrow();
    addUserMsg(text);
    newAiCard();
    emptySession = false;
    updateNewBtn();
    setStreaming(true);
    setStatus("规划中…");

    const ctrl = new AbortController();
    currentCtrl = ctrl;

    API.chatStream(CURRENT_SID, text, {
      round_start: (p) => {
        AI.sec = buildRoundSec(p.round);
        AI.think = buildThinkBlock(true);
        AI.sec.appendChild(AI.think.el);
        AI.card.appendChild(AI.sec);
        AI.mdRaw = ""; AI.mdEl = null; AI.curTool = null;
        setStatus(`第 ${p.round} 轮`);
        scrollBottom(true);
      },
      thinking_delta: (p) => {
        if (!AI.think) return;
        // 顺序追加（工具块在思考结束后才插入，天然位于文字之后，顺序不会反）
        AI.think.body.appendChild(document.createTextNode(p.delta));
        AI.think.summary.innerHTML =
          `<span class="dot"></span>思考中… ${AI.think.body.textContent.length} 字`;
        if (nearBottom()) AI.think.body.scrollTop = AI.think.body.scrollHeight;
      },
      thinking_end: (p) => {
        if (!AI.think) return;
        // 若本轮已有工具调用（内嵌在思考中），保持展开；否则自动折叠
        if (!AI.think.body.querySelector(".tool-item")) {
          AI.think.el.classList.remove("open");
          AI.think.summary.classList.remove("thinking");
          AI.think.summary.innerHTML = `<span class="dot"></span>思考 ${p.seconds} 秒 ▸`;
        } else {
          AI.think.summary.classList.remove("thinking");
          AI.think.summary.innerHTML = `<span class="dot"></span>思考 ${p.seconds} 秒 · 工具执行中 ▸`;
        }
      },
      text_delta: (p) => {
        if (!AI.mdEl) {
          AI.mdEl = document.createElement("div");
          AI.mdEl.className = "md";
          AI.sec.appendChild(AI.mdEl);
        }
        AI.mdRaw += p.delta;
        flushMd();
      },
      tool_call: (p) => {
        flushMd(true);
        if (!AI.think) return;
        // 工具调用真正内嵌进思考文字区块（折叠时随之收起，展开可见）
        const item = buildToolItem(p.id, p.name, p.arguments);
        AI.think.body.appendChild(item);
        AI.think.el.classList.add("open");          // 执行期间保持展开以便看进度
        AI.curTool = { id: p.id, item };
        scrollBottom();
      },
      tool_progress: (p) => {
        if (!AI.curTool) return;
        const sec = AI.curTool.item.querySelector(".prog-sec");
        sec.classList.remove("hidden");
        sec.querySelector(".pbar > div").style.width =
          (p.total ? Math.round(p.current / p.total * 100) : 0) + "%";
        sec.querySelector(".pmsg").textContent = p.message || "";
        setStatus(p.message || "");
        if (AI.think) {
          AI.think.summary.innerHTML = `<span class="dot"></span>${esc(p.message || "工具执行中")}`;
        }
      },
      tool_result: (p) => {
        if (AI.curTool && AI.curTool.id === p.id) {
          const st = AI.curTool.item.querySelector(".tool-state");
          st.textContent = "完成"; st.className = "tool-state done";
          AI.curTool.item.querySelector(".out").textContent =
            JSON.stringify(p.ai_result, null, 2);
          AI.curTool = null;
        }
        if (p.panel_solutions && p.panel_solutions.length) {
          mergePanel(p.panel_solutions, "最近查询");
        }
        scrollBottom();
      },
      round_end: () => {
        flushMd(true);
        if (AI.think) {
          // 轮结束：若思考块仍展开则折叠为摘要
          AI.think.el.classList.remove("open");
          if (AI.think.summary.classList.contains("thinking")) {
            AI.think.summary.classList.remove("thinking");
            AI.think.summary.innerHTML = `<span class="dot"></span>思考 ▸`;
          }
        }
      },
      final: () => {
        AI.head.innerHTML = `<span class="ai-badge">铁程规划</span>`;
        flushMd(true);
        if (AI.mdEl) {
          const wrap = document.createElement("div");
          wrap.className = "final-sec";
          AI.mdEl.parentNode.insertBefore(wrap, AI.mdEl);
          wrap.appendChild(AI.mdEl);
        }
        renderPlanTable(AI.card);
        setStatus("");
      },
      error: (p) => {
        setStatus("");
        const div = document.createElement("div");
        div.className = "alert err";
        div.textContent = p.message || "出错了";
        AI.card.appendChild(div);
        AI.head.innerHTML = `<span class="ai-badge">铁程规划 · 已停止</span>`;
      },
      done: () => { flushMd(true); setStatus(""); },
      _end: () => finishStream(false),
      _abort: () => finishStream(true),
      _error: (e) => {
        setStatus("");
        if (AI) {
          const div = document.createElement("div");
          div.className = "alert err";
          div.textContent = "连接中断: " + e.message;
          AI.card.appendChild(div);
        }
        finishStream(true);
      },
    }, ctrl.signal);
  }

  function finishStream(aborted) {
    setStreaming(false);
    flushMd(true);
    if (AI && aborted) AI.head.innerHTML = `<span class="ai-badge">铁程规划 · 已停止</span>`;
    currentCtrl = null;
    loadSessions();
  }

  function setStreaming(on) {
    streaming = on;
    $("btn-send").disabled = on;
    $("btn-stop").classList.toggle("hidden", !on);
    $("input").disabled = on;
    updateNewBtn();
  }
  function setStatus(text) {
    const el = $("status-line");
    if (text) { el.textContent = text; el.classList.remove("hidden"); }
    else el.classList.add("hidden");
  }
  async function stop() {
    if (CURRENT_SID) {
      try {
        await fetch("/api/chat/stop", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: CURRENT_SID }),
        });
      } catch {}
    }
    if (currentCtrl) currentCtrl.abort();
  }

  /* ═══════════ 对话详情（行式平铺全量数据，严格会话隔离） ═══════════ */
  let PANEL = { sid: null, byId: {}, order: [], meta: "" };

  function resetPanel(sid) {
    PANEL = { sid: sid != null ? sid : CURRENT_SID, byId: {}, order: [], meta: "" };
    updateDetailBadge();
  }
  function addPanelSol(s) {
    if (!s || !s.solution_id) return;
    if (PANEL.sid !== CURRENT_SID) resetPanel();   // 会话切换保险：绝不混入其他对话的方案
    if (!PANEL.byId[s.solution_id]) PANEL.order.push(s.solution_id);
    PANEL.byId[s.solution_id] = s;
  }
  function mergePanel(solutions, meta) {
    for (const s of solutions || []) addPanelSol(s);
    if (meta) PANEL.meta = meta;
    updateDetailBadge();
    if (VIEW === "detail") drawDetail();
  }
  function updateDetailBadge() {
    const n = PANEL.order.length;
    $("btn-detail").classList.toggle("hidden", n === 0);
    $("detail-count").textContent = n || "";
    $("detail-count").classList.toggle("hidden", !n);
  }

  function typeTag(s) {
    if (s.type === "direct") return `<span class="tag direct">直达</span>`;
    if (s.type === "variant") return `<span class="tag variant">补票</span>`;
    return `<span class="tag transfer">${s.transfer_count || 1} 次换乘</span>`;
  }

  function stateTag(s) {
    if (!s.checked) return `<span class="tag unchecked">未核实</span>`;
    return s.can_buy ? `<span class="tag direct">可购</span>` : `<span class="tag variant">无票/受限</span>`;
  }

  function solRowMain(s) {
    const segs = s.segments || [];
    const trains = segs.map(g =>
      `<b class="tn">${esc(g.train_num)}</b> ${esc(g.from_station_name)}→${esc(g.to_station_name)}` +
      ` <span class="tm">${esc(g.depart_time)}–${esc(g.arrive_time)}</span>`).join('<span class="arr"> ⇒ </span>');
    const dur = s.total_duration || "--:--";
    const score = s.score != null ? `<span class="score" title="综合评分">评分 ${s.score}</span>` : "";
    const price = s.checked
      ? (segs.filter(g => g.price != null).map(g => `¥${g.price}`).join(" + ") || "--")
      : (s.price_est != null ? `≈¥${s.price_est}` : "--");
    let seatInfo = "";
    if (s.checked) {
      seatInfo = segs.map(g => {
        const cls = g.ticket_status === "ok" ? "ok" : (g.ticket_status === "few" ? "few" : "none");
        const label = g.ticket_status === "no_seat"
          ? `${g.train_num} 无${s.seat_type || g.seat_cn || "该"}席`
          : `${g.train_num} ${g.seat_cn || ""} ${g.tickets_raw || "?"}${g.price != null ? " ¥" + g.price : ""}`;
        return `<span class="seat-chip ${cls}">${esc(label)}</span>`;
      }).join("");
    } else if (s.price_est != null) {
      seatInfo = `<span class="seat-chip">历史参考 ¥${s.price_est}</span>`;
    }
    return { trains, dur, score, price, seatInfo };
  }

  function buildSolRow(s, container, checkHandler) {
    const m = solRowMain(s);
    const row = document.createElement("div");
    row.className = "sol-row" + (s.checked && s.can_buy ? " reco" : "") + (s.checked && !s.can_buy ? " none" : "");
    row.innerHTML =
      `<div class="sr-main">` +
      `<div class="sr-l1"><span class="sol-id">${esc(s.solution_id || "")}</span>${typeTag(s)}${stateTag(s)}` +
      `<span class="sr-date">${esc(s.date || "")}</span>${m.score}` +
      `<span class="sr-dur">历时 ${esc(m.dur)}</span><span class="sr-price">票价 ${m.price}</span></div>` +
      `<div class="sr-l2">${m.trains}</div>` +
      `<div class="sr-l3">${m.seatInfo || ""}` +
      (s.check_note && s.checked ? `<span class="sol-note">${esc(s.check_note)}</span>` : "") +
      `</div></div>` +
      `<div class="sr-ops">` +
      (checkHandler ? `<button class="btn primary small act-check">联网核实</button>` : "") +
      `<span class="checking hidden"><span class="spin"></span> 核实中…</span>` +
      `</div>`;
    const cb = row.querySelector(".act-check");
    if (cb && checkHandler) cb.onclick = () => checkHandler(row, s);
    container.appendChild(row);
    return row;
  }

  function drawDetail() {
    const body = $("detail-body");
    $("detail-meta").textContent = PANEL.meta;
    if (!PANEL.order.length) {
      body.innerHTML = `<div class="detail-empty">暂无数据。<br>在对话中发起查询后，全部结构化方案按行平铺在此；可对任意方案手动「联网核实」。</div>`;
      return;
    }
    body.innerHTML = "";
    for (const sid of PANEL.order) {
      const s = PANEL.byId[sid];
      buildSolRow(s, body, async (row, sol) => {
        const loading = row.querySelector(".checking");
        const btn = row.querySelector(".act-check");
        btn.disabled = true; loading.classList.remove("hidden");
        try {
          const r = await API.checkSolution(CURRENT_SID, sol.solution_id, null);
          PANEL.byId[sol.solution_id] = r.solution;
          drawDetail();
          toast(`方案 ${sol.solution_id}：${r.solution.can_buy ? "可购" : (r.solution.check_note || "无票")}`);
        } catch (e) {
          toast("核实失败：" + e.message);
          btn.disabled = false; loading.classList.add("hidden");
        }
      });
    }
  }

  /* 批量核实：对话详情前 N 条（后端按购买区间合并，一次联网） */
  async function batchCheckDetail() {
    if (!CURRENT_SID || !PANEL.order.length) { toast("暂无方案"); return; }
    const n = Math.max(1, parseInt($("batch-n-detail").value, 10) || 5);
    const ids = PANEL.order.slice(0, n);
    const btn = $("batch-check-detail"), st = $("batch-status-detail"), rs = $("batch-result-detail");
    btn.disabled = true; st.classList.remove("hidden"); rs.textContent = "";
    try {
      const r = await API.checkBatch(CURRENT_SID, ids, null);
      for (const sol of r.solutions) PANEL.byId[sol.solution_id] = sol;
      drawDetail();
      rs.textContent = `完成：可购 ${r.can_buy} / 核实 ${r.checked}`;
      toast(`批量核实完成：可购 ${r.can_buy}/${r.checked}`);
    } catch (e) {
      rs.textContent = "失败：" + e.message;
      toast("批量核实失败：" + e.message);
    } finally {
      btn.disabled = false; st.classList.add("hidden");
    }
  }

  /* 正式输出内的方案总表（由详情数据直出；target 为空时挂当前流式卡）。
     聊天流中不展示"核实后无票"的方案（对话详情仍显示全部）。 */
  function renderPlanTable(target) {
    if (!PANEL.order.length) return;
    const all = PANEL.order.map(sid => PANEL.byId[sid]);
    const visible = all.filter(s => !(s.checked && !s.can_buy));
    if (!visible.length) {
      const div = document.createElement("div");
      div.className = "alert";
      div.style.background = "var(--err-bg)";
      div.style.borderColor = "var(--err-bd)";
      div.style.color = "var(--err-fg)";
      div.textContent = "本次核实的方案均无票。完整数据（含无票方案）可在「对话详情」中查看，或在对话中让 AI 推荐其他车次。";
      const host = target || (AI ? AI.card : null);
      if (host) host.appendChild(div);
      scrollBottom();
      return;
    }
    const rows = visible.map(sid0 => {
      const sid = sid0.solution_id;
      const s = sid0;
      const seg0 = (s.segments && s.segments[0]) || {};
      const last = (s.segments && s.segments[s.segments.length - 1]) || {};
      let st = `<span class="st-unknown">未核实</span>`;
      if (s.checked) {
        st = s.can_buy ? `<span class="st-ok">可购</span>`
          : `<span class="st-none">${esc(s.check_note || "无票")}</span>`;
      }
      const price = s.checked
        ? (s.segments.filter(g => g.price != null).map(g => `¥${g.price}`).join(" + ") || "--")
        : (s.price_est != null ? `≈¥${s.price_est}` : "--");
      const reco = s.checked && s.can_buy;
      return `<tr class="${reco ? "reco" : ""}">` +
        `<td>${esc(sid)}${reco ? " ★" : ""}</td><td>${s.score != null ? s.score : "--"}</td>` +
        `<td>${s.type === "direct" ? "直达" : (s.type === "variant" ? "补票" : (s.transfer_count || 1) + " 次换乘")}</td>` +
        `<td>${esc(s.segments.map(g => g.train_num).join("+"))}</td>` +
        `<td>${esc(seg0.depart_time || "")} → ${esc(last.arrive_time || "")}</td>` +
        `<td>${esc(s.total_duration || "")}</td><td>${price}</td><td>${st}</td></tr>`;
    }).join("");
    const div = document.createElement("div");
    div.innerHTML =
      `<table class="plan-table"><thead><tr>` +
      `<th>方案</th><th>评分</th><th>类型</th><th>车次</th><th>时刻</th><th>历时</th><th>票价</th><th>余票</th>` +
      `</tr></thead><tbody>${rows}</tbody></table>`;
    const host = target || (AI ? AI.card : null);
    if (host) host.appendChild(div);
    scrollBottom();
  }

  /* ═══════════ 手动查票模式 ═══════════ */
  let manualFormReady = false;
  let MANUAL = null;          // {sols, date, seat}

  async function batchCheckManual() {
    if (!MANUAL || !MANUAL.sols.length) { toast("请先查询方案"); return; }
    const n = Math.max(1, parseInt($("batch-n-manual").value, 10) || 5);
    const targets = MANUAL.sols.slice(0, n);
    const btn = $("batch-check-manual"), st = $("batch-status-manual"), rs = $("batch-result-manual");
    btn.disabled = true; st.classList.remove("hidden"); rs.textContent = "";
    try {
      const r = await API.manualCheckBatch(MANUAL.date, MANUAL.seat, targets);
      for (const sol of r.solutions) {
        const idx = MANUAL.sols.findIndex(x => x.solution_id === sol.solution_id);
        if (idx >= 0) MANUAL.sols[idx] = sol;
      }
      drawManualRefresh(MANUAL.sols, MANUAL.date, MANUAL.seat);
      rs.textContent = `完成：可购 ${r.can_buy} / 核实 ${r.checked}`;
      toast(`批量核实完成：可购 ${r.can_buy}/${r.checked}`);
    } catch (e) {
      rs.textContent = "失败：" + e.message;
      toast("批量核实失败：" + e.message);
    } finally {
      btn.disabled = false; st.classList.add("hidden");
    }
  }

  function initManualForm() {
    if (manualFormReady) return;
    manualFormReady = true;
    const sel = $("mf-seat");
    sel.innerHTML = ["不限"].concat(META.seats || ["二等座"]).map(x => `<option>${esc(x)}</option>`).join("");
    const d = new Date(Date.now() + 3 * 86400000);
    $("mf-date").value = d.toISOString().slice(0, 10);
    $("mf-date").min = new Date().toISOString().slice(0, 10);
  }

  async function manualSearch() {
    const err = $("mf-err");
    err.classList.add("hidden");
    const body = {
      from_station: $("mf-from").value.trim(),
      to_station: $("mf-to").value.trim(),
      date: $("mf-date").value,
      seat_type: $("mf-seat").value,
      allow_transfer: $("mf-transfers").value !== "0",
      max_transfers: parseInt($("mf-transfers").value, 10),
      prefer_direct: true,
      depart_after: $("mf-dep-after").value,
      depart_before: $("mf-dep-before").value,
      arrive_after: $("mf-arr-after").value,
      arrive_before: $("mf-arr-before").value,
      sort_by: $("mf-sort").value,
      train_type: $("mf-ttype") ? $("mf-ttype").value : "all",
      fuzzy: $("mf-fuzzy") ? $("mf-fuzzy").checked : false,
      max_results: parseInt($("mf-max").value, 10) || 20,
    };
    if (!body.from_station || !body.to_station) {
      err.textContent = "请填写出发站与到达站";
      err.classList.remove("hidden");
      return;
    }
    if (!body.date) {
      err.textContent = "出行日期为必填项";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("mf-search");
    btn.disabled = true; btn.textContent = "查询中…";
    // 立即清空右侧并显示查询中：每次点击都有可见的刷新反馈
    $("mr-meta").classList.remove("hidden");
    $("mr-meta").textContent = "查询中…";
    $("mr-body").innerHTML = "";
    try {
      const r = await fetch("/api/manual/search", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      MANUAL = { sols: d.solutions, date: d.date, seat: body.seat_type };
      const ts = new Date().toTimeString().slice(0, 8);
      $("mr-meta").textContent =
        `${d.from} → ${d.to} · ${d.date} · ${body.seat_type} · 枚举 ${d.enum_total} 候选 · 返回 ${d.count} 个 · ${ts}`;
      drawManualRefresh(d.solutions, d.date, body.seat_type);
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
      $("mr-meta").textContent = `查询失败：${e.message}`;
      $("mr-body").innerHTML = "";
      toast("查询失败：" + e.message);
    } finally {
      btn.disabled = false; btn.textContent = "查询方案";
    }
  }

  function drawManualRefresh(sols, date, seat) {
    const body = $("mr-body");
    body.innerHTML = "";
    if (!sols || !sols.length) {
      body.innerHTML = `<div class="detail-empty">该条件下没有找到可行方案。<br>可放宽时间范围或增加换乘次数。</div>`;
      return;
    }
    const checker = makeManualChecker(sols, date, seat);
    for (const s of sols) buildSolRow(s, body, checker);
  }
  function makeManualChecker(sols, date, seat) {
    return async (row, sol) => {
      const loading = row.querySelector(".checking");
      const btn = row.querySelector(".act-check");
      btn.disabled = true; loading.classList.remove("hidden");
      try {
        const r = await fetch("/api/manual/check", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ date, seat_type: seat, solution: sol }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        const idx = sols.findIndex(x => x.solution_id === sol.solution_id);
        if (idx >= 0) sols[idx] = d.solution;
        drawManualRefresh(sols, date, seat);
        toast(`方案 ${sol.solution_id}：${d.solution.can_buy ? "可购" : (d.solution.check_note || "无票")}`);
      } catch (e) {
        toast("核实失败：" + e.message);
        btn.disabled = false; loading.classList.add("hidden");
      }
    };
  }
})();
