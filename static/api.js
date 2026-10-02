/* api.js —— 后端接口封装（fetch + SSE 流解析） */
(function () {
  "use strict";

  async function jfetch(url, opts) {
    const r = await fetch(url, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.message || `HTTP ${r.status}`);
    return data;
  }

  const API = {
    // ---- 配置 ----
    getConfig: () => jfetch("/api/config"),
    setConfig: (cfg) => jfetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    }),
    verifyConfig: () => jfetch("/api/config/verify", { method: "POST" }),

    // ---- 会话 ----
    listSessions: () => jfetch("/api/sessions"),
    createSession: (title) => jfetch("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title || "新对话" }),
    }),
    getSession: (sid) => jfetch(`/api/sessions/${sid}`),
    deleteSession: (sid) => jfetch(`/api/sessions/${sid}`, { method: "DELETE" }),

    // ---- 手动查票（数据面板直连，不经过 AI） ----
    checkSolution: (sessionId, solutionId, date) => jfetch("/api/solutions/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, solution_id: solutionId, date: date || null }),
    }),
    checkBatch: (sessionId, solutionIds, date) => jfetch("/api/solutions/check_batch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, solution_ids: solutionIds, date: date || null }),
    }),
    manualCheck: (date, seatType, solution) => jfetch("/api/manual/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date, seat_type: seatType, solution }),
    }),
    manualCheckBatch: (date, seatType, solutions) => jfetch("/api/manual/check_batch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date, seat_type: seatType, solutions }),
    }),

    // ---- 元数据 ----
    meta: () => jfetch("/api/meta"),

    // ---- SSE 聊天（POST + ReadableStream，事件协议见 server.py 头注释） ----
    chatStream: function (sessionId, message, handlers, signal) {
      const ctrl = { stopped: false };
      (async () => {
        try {
          const resp = await fetch("/api/chat/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, message }),
            signal,
          });
          if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            throw new Error(d.detail || `HTTP ${resp.status}`);
          }
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const block = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              let event = "message", data = "";
              for (const line of block.split("\n")) {
                if (line.startsWith("event: ")) event = line.slice(7).trim();
                else if (line.startsWith("data: ")) data += line.slice(6);
              }
              if (!data) continue;
              let payload;
              try { payload = JSON.parse(data); } catch { payload = { raw: data }; }
              try {
                if (handlers[event]) handlers[event](payload);
              } catch (e) { console.error("handler error", event, e); }
            }
          }
          if (handlers._end) handlers._end();
        } catch (e) {
          if (e.name === "AbortError") {
            if (handlers._abort) handlers._abort();
          } else {
            if (handlers._error) handlers._error(e);
          }
        }
      })();
      return ctrl;
    },
  };

  window.API = API;
})();
