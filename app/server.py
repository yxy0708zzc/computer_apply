"""
server.py —— FastAPI 编排层：配置门 / SSE agent 对话 / 会话落盘 / 数据面板手动查票

SSE 事件协议（harness 形态）：
  round_start     {round}                      轮开始
  thinking_delta  {delta}                      思考流式追加
  thinking_end    {seconds}                    思考结束→前端自动折叠"已思考 N 秒"
  text_delta      {delta}                      正文流式
  tool_call       {id, name, arguments}        工具行插入
  tool_progress   {tool, current, total, message}   联网核实进度
  tool_result     {id, ai_result, panel_solutions}  工具完成（panel 非空=数据面板更新）
  round_end       {round}
  final           {round}                      最后一轮正文=正式输出
  error           {message}
  done            {usage}
"""

import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database as db
from . import llm
from . import online
from . import planner
from . import prompts
from . import tools as tools_mod
from .tools import TOOLS_SCHEMA, ToolContext, execute_tool

app = FastAPI(title="tripai 智能火车出行规划")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(_BASE_DIR, "sessions")
ENV_PATH = os.path.join(_BASE_DIR, ".env")
os.makedirs(SESSIONS_DIR, exist_ok=True)

MAX_ROUNDS = 25
MAX_CONTEXT_MESSAGES = 40       # 发送 LLM 前的 messages 上限（保 system + 最近 N 条）

# ------------------------------------------------------------
# 配置（内存 + .env 持久化）
# ------------------------------------------------------------

CONFIG: Dict[str, str] = {"api_key": "", "model": "", "base_url": ""}
_CONFIG_LOCK = threading.Lock()


def _clean_env_value(v: str) -> str:
    """剥行内注释（' #' 起）与首尾引号/空白——travel2 .env 习惯带注释，防御残留"""
    v = (v or "").strip()
    if " #" in v:
        v = v.split(" #", 1)[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _load_env_config():
    if not os.path.exists(ENV_PATH):
        return
    try:
        with open(ENV_PATH, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "LLM_API_KEY":
                    CONFIG["api_key"] = _clean_env_value(v) or CONFIG["api_key"]
                elif k == "LLM_MODEL":
                    CONFIG["model"] = _clean_env_value(v) or CONFIG["model"]
                elif k == "LLM_BASE_URL":
                    CONFIG["base_url"] = _clean_env_value(v).rstrip("/") or CONFIG["base_url"]
    except Exception:
        pass


def _save_env_config():
    try:
        lines = [
            "# tripai 配置（由设置页自动写入）",
            f'LLM_API_KEY="{CONFIG["api_key"]}"',
            f'LLM_MODEL="{CONFIG["model"]}"',
            f'LLM_BASE_URL="{CONFIG["base_url"]}"',
            "",
        ]
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


_load_env_config()


class ConfigIn(BaseModel):
    api_key: str = ""
    model: str = ""
    base_url: str = ""


@app.get("/api/config")
def api_config_get():
    with _CONFIG_LOCK:
        key = CONFIG["api_key"]
        return {
            "configured": bool(key and CONFIG["model"] and CONFIG["base_url"]),
            "api_key_masked": (key[:6] + "****" + key[-4:]) if len(key) > 12 else ("已设置" if key else ""),
            "model": CONFIG["model"], "base_url": CONFIG["base_url"],
        }


@app.post("/api/config")
def api_config_set(cfg: ConfigIn):
    with _CONFIG_LOCK:
        if cfg.api_key:
            CONFIG["api_key"] = _clean_env_value(cfg.api_key)
        if cfg.model:
            CONFIG["model"] = _clean_env_value(cfg.model)
        if cfg.base_url:
            CONFIG["base_url"] = _clean_env_value(cfg.base_url).rstrip("/")
        _save_env_config()
        snapshot = dict(CONFIG)
    missing = [k for k, v in snapshot.items() if not v]
    return {"ok": not missing, "missing": missing}


@app.post("/api/config/verify")
def api_config_verify():
    """实测一次最小对话验证配置可用"""
    with _CONFIG_LOCK:
        key, model, base = CONFIG["api_key"], CONFIG["model"], CONFIG["base_url"]
    if not (key and model and base):
        raise HTTPException(400, f"配置不完整: {', '.join(k for k, v in {'API-KEY': key, '模型': model, '接口地址': base}.items() if not v)}")
    try:
        got = False
        for ev in llm.stream_chat(key, base, model,
                                  [{"role": "user", "content": "请只回复两个字母: ok"}],
                                  temperature=0.0):
            if ev["type"] in ("content", "thinking") and ev.get("delta"):
                got = True
                break
        return {"ok": got, "message": "验证通过" if got else "模型无响应"}
    except llm.LLMError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------
# 会话（内存 + sessions/*.json 落盘）
# ------------------------------------------------------------

class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.title = "新对话"
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.updated_at = self.created_at
        self.messages: List[Dict] = []       # LLM 上下文（含 system）
        self.trace: List[Dict] = []          # 持久化执行轨迹（前端历史渲染）
        self.toolctx = ToolContext(sid)
        self.stop_event = threading.Event()

    def to_disk(self) -> Dict:
        return {"id": self.id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "messages": self.messages, "trace": self.trace,
                "solutions": self.toolctx.solutions, "tool_seq": self.toolctx._seq}

    @classmethod
    def from_disk(cls, d: Dict) -> "Session":
        s = cls(d["id"])
        s.title = d.get("title", "新对话")
        s.created_at = d.get("created_at", s.created_at)
        s.updated_at = d.get("updated_at", s.updated_at)
        s.messages = d.get("messages", [])
        s.trace = d.get("trace", [])
        s.toolctx.solutions = d.get("solutions", {})
        s.toolctx._seq = int(d.get("tool_seq", 0))
        return s


_SESSIONS: Dict[str, Session] = {}
_SESSIONS_LOCK = threading.Lock()


def _session_path(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return os.path.join(SESSIONS_DIR, f"{safe}.json")


def _persist(sess: Session):
    sess.updated_at = datetime.now().isoformat(timespec="seconds")
    try:
        tmp = _session_path(sess.id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sess.to_disk(), f, ensure_ascii=False)
        os.replace(tmp, _session_path(sess.id))
    except Exception:
        import logging
        logging.getLogger("uvicorn.error").exception(
            "[persist] 会话落盘失败 sid=%s", sess.id)


def get_session(sid: str) -> Session:
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if s:
            return s
        p = _session_path(sid)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    s = Session.from_disk(json.load(f))
                _SESSIONS[sid] = s
                return s
            except Exception:
                pass
        s = Session(sid)
        _SESSIONS[sid] = s
        return s


class ChatIn(BaseModel):
    session_id: str
    message: str


class CheckIn(BaseModel):
    session_id: str
    solution_id: str
    date: Optional[str] = None


class SessionCreateIn(BaseModel):
    title: str = "新对话"


class SessionRenameIn(BaseModel):
    title: str


@app.post("/api/sessions/{sid}/rename")
def api_session_rename(sid: str, body: SessionRenameIn):
    s = get_session(sid)
    t = (body.title or "").strip()
    if not t:
        raise HTTPException(400, "标题不能为空")
    s.title = t[:40]
    _persist(s)
    return {"ok": True, "title": s.title}


@app.post("/api/sessions")
def api_session_create(body: SessionCreateIn):
    sid = uuid.uuid4().hex[:12]
    s = get_session(sid)
    if body.title:
        s.title = body.title
    _persist(s)
    return {"session_id": sid, "title": s.title}


@app.get("/api/sessions")
def api_session_list():
    out = []
    if os.path.isdir(SESSIONS_DIR):
        for fn in os.listdir(SESSIONS_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"id": d["id"], "title": d.get("title", ""),
                            "updated_at": d.get("updated_at", "")})
            except Exception:
                continue
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"sessions": out}


@app.get("/api/sessions/{sid}")
def api_session_get(sid: str):
    s = get_session(sid)
    return s.to_disk()


@app.delete("/api/sessions/{sid}")
def api_session_delete(sid: str):
    with _SESSIONS_LOCK:
        _SESSIONS.pop(sid, None)
    try:
        os.remove(_session_path(sid))
    except OSError:
        pass
    return {"ok": True}


@app.post("/api/solutions/check")
def api_solutions_check(body: CheckIn):
    """数据面板手动查票：直连联网层，不经过 AI。完成后往上下文追加系统备注。"""
    s = get_session(body.session_id)
    sol = s.toolctx.solutions.get(body.solution_id)
    if not sol:
        raise HTTPException(404, f"方案不存在: {body.solution_id}")
    date = online.normalize_date(body.date or sol.get("date"))
    res = online.check_solutions([sol], date, seat_type=sol.get("seat_type", "二等座"),
                                 people_count=1)
    checked = res["results"][0]
    s.toolctx.solutions[body.solution_id] = checked
    # 系统备注进上下文，保证 AI 后续对话知情
    brief = "；".join(
        f"{seg['train_num']} {seg.get('seat_cn') or ''}"
        f"{'有票' if seg.get('ticket_status') == 'ok' else ('票紧' if seg.get('ticket_status') == 'few' else '无票/无此席')}"
        + (f" {seg.get('price')}元" if seg.get("price") else "")
        for seg in checked["segments"])
    s.messages.append({"role": "system",
                       "content": f"[系统备注] 用户刚在数据面板手动核实了方案 {body.solution_id}"
                                  f"（{date}）：{brief or '未查到'}。用户后续提问时以此为准。"})
    _persist(s)
    return {"solution": checked, "date": date}


# ------------------------------------------------------------
# 手动查票模式（全流程不借助 AI，无状态）
# ------------------------------------------------------------

class ManualSearchIn(BaseModel):
    from_station: str
    to_station: str
    date: Optional[str] = None
    seat_type: str = "不限"          # "不限"=估价按二等座档（缺失回落硬座）、核实取任一可购席
    allow_transfer: bool = True
    max_transfers: int = 1
    prefer_direct: bool = True
    train_type: str = "all"
    depart_after: Optional[str] = None
    depart_before: Optional[str] = None
    arrive_after: Optional[str] = None
    arrive_before: Optional[str] = None
    sort_by: str = "comprehensive"
    max_results: int = 30
    fuzzy: bool = False              # 站名模糊：候选站组合逐一查询（前端默认勾选传入）


class CheckBatchIn(BaseModel):
    session_id: str
    solution_ids: List[str]
    date: Optional[str] = None


@app.post("/api/solutions/check_batch")
def api_solutions_check_batch(body: CheckBatchIn):
    """对话详情批量核实：前 N 条方案一次性联网核实（按购买区间合并请求）"""
    s = get_session(body.session_id)
    ids = [str(x) for x in body.solution_ids][:60]
    found, missing = s.toolctx.get_many(ids)
    if not found:
        raise HTTPException(404, f"方案不存在: {','.join(missing)}")
    date = online.normalize_date(body.date or found[0].get("date"))
    seat = found[0].get("seat_type") or "二等座"
    res = online.check_solutions(found, date, seat_type=seat, people_count=1)
    can_buy = 0
    for sol in res["results"]:
        s.toolctx.solutions[sol["solution_id"]] = sol
        if sol.get("can_buy"):
            can_buy += 1
    s.messages.append({
        "role": "system",
        "content": f"[系统备注] 用户在对话详情批量核实了 {len(found)} 个方案（{date}，{seat}）："
                   f"可购 {can_buy} 个、无票/受限 {len(found) - can_buy} 个。用户后续提问时以此为准。"})
    _persist(s)
    return {"solutions": res["results"], "date": date,
            "can_buy": can_buy, "checked": len(res["results"])}


class ManualCheckBatchIn(BaseModel):
    date: str
    seat_type: str = "二等座"
    solutions: List[dict]


@app.post("/api/manual/check_batch")
def api_manual_check_batch(body: ManualCheckBatchIn):
    """手动模式批量核实（无状态）"""
    if not body.solutions:
        raise HTTPException(400, "方案列表为空")
    try:
        date = online.normalize_date(body.date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sols = body.solutions[:60]
    res = online.check_solutions(sols, date, seat_type=body.seat_type, people_count=1)
    return {"solutions": res["results"], "date": date,
            "can_buy": sum(1 for s in res["results"] if s.get("can_buy")),
            "checked": len(res["results"])}


class ManualCheckIn(BaseModel):
    date: str
    seat_type: str = "二等座"
    solution: dict


def _hhmm(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v if re.fullmatch(r"\d{2}:\d{2}", v) else None


@app.post("/api/manual/search")
def api_manual_search(body: ManualSearchIn):
    """手动模式：表单直连本地方案引擎（不经过 AI）。
    fuzzy=True：出发/到达按站名子串展开候选站（如"北京"含北京西/南/丰台…），
    候选对逐一枚举，合并去重后统一评分排序取前 N。"""
    conn = db.get_railway_conn()
    try:
        date = online.normalize_date(body.date)
        if body.seat_type != "不限" and body.seat_type not in db.SEAT_CN_LIST:
            raise HTTPException(400, f"坐席不合法: {body.seat_type}")
        # "不限"席：估价用二等座档（缺失回落硬座）；核实时由 online 按"不限"取任一可购席
        est_seat = "二等座" if body.seat_type == "不限" else body.seat_type
        seat_fb = "硬座" if est_seat == "二等座" else None
        n = max(1, min(int(body.max_results), 60))

        def _plan(f_v, t_v, enum_total):
            return planner.plan_solutions(
                conn, f_v, t_v,
                seat_type=est_seat, seat_fallback=seat_fb,
                allow_transfer=body.allow_transfer,
                max_transfers=body.max_transfers,
                prefer_direct=body.prefer_direct,
                depart_after=_hhmm(body.depart_after),
                depart_before=_hhmm(body.depart_before),
                arrive_after=_hhmm(body.arrive_after),
                arrive_before=_hhmm(body.arrive_before),
                sort_by="comprehensive",          # 统一出评分，合并后按 body.sort_by 重排
                max_results=n,
                auto_expand=True,
                enum_total=enum_total,
                train_type=body.train_type if body.train_type in planner.VALID_TRAIN_TYPE else "all",
            )

        if body.fuzzy:
            def _expand(kw):
                # 中文站名一律展开所有含该词的站（含精确站自身，如"北京"含北京西/南/丰台…）；
                # 电报码（ASCII）走精确单站
                if kw.isascii():
                    v = db.resolve_station_exact(conn, kw)
                    return [v] if v else []
                cands = [c["station_id"] for c in db.search_stations(conn, kw, limit=8)]
                v = db.resolve_station_exact(conn, kw)
                if v and v not in cands:
                    cands.insert(0, v)
                return cands[:8]

            from_c = _expand((body.from_station or "").strip())
            to_c = _expand((body.to_station or "").strip())
            if not from_c or not to_c:
                bad = body.from_station if not from_c else body.to_station
                raise HTTPException(400, f"无法识别车站: {bad}")
            pairs = [(f, t) for f in from_c for t in to_c][:40]   # 组合上限
            per_enum = max(40, n * 4)                             # 每对候选池
            all_sols, seen = [], set()
            for f_v, t_v in pairs:
                for s in _plan(f_v, t_v, per_enum):
                    sig = tuple((g.get("train_no") or g["train_num"],
                                 g["from_station_id"], g["to_station_id"])
                                for g in s.get("segments", []))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    all_sols.append(s)
            planner.score_solutions(all_sols)                     # 跨池统一评分
            sols = planner.sort_solutions(all_sols, body.sort_by)
            from_label = f"{body.from_station}（{len(from_c)} 站）"
            to_label = f"{body.to_station}（{len(to_c)} 站）"
            total_enum = per_enum * len(pairs)
        else:
            from_v = db.resolve_station_name_or_id(conn, body.from_station)
            to_v = db.resolve_station_name_or_id(conn, body.to_station)
            if not from_v or not to_v:
                raise HTTPException(400, f"无法识别车站: "
                                       f"{body.from_station if not from_v else body.to_station}")
            sols = planner.sort_solutions(
                _plan(from_v, to_v, n * tools_mod.MANUAL_ENUM_MULT),
                body.sort_by, prefer_direct=body.prefer_direct)
            from_label = db.get_station_name(conn, from_v)
            to_label = db.get_station_name(conn, to_v)
            total_enum = n * tools_mod.MANUAL_ENUM_MULT

        for i, s in enumerate(sols[:n]):
            s["solution_id"] = f"m{i+1}"
            s["date"] = date
            s["seat_type"] = body.seat_type
            segs = s.get("segments") or []
            if segs and (body.fuzzy or len(segs) > 1):
                s["route_note"] = (f"{segs[0]['from_station_name']}→{segs[-1]['to_station_name']}"
                                   + (f"（经{'、'.join(g['to_station_name'] for g in segs[:-1])}）"
                                      if len(segs) > 1 else ""))
        return {"date": date, "from": from_label,
                "to": to_label,
                "enum_total": total_enum,
                "count": min(len(sols), n), "solutions": sols[:n]}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/manual/check")
def api_manual_check(body: ManualCheckIn):
    """手动模式：对单个方案联网核实（无状态，方案由前端回传）"""
    sol = body.solution
    if not sol or not sol.get("segments"):
        raise HTTPException(400, "方案数据为空")
    try:
        date = online.normalize_date(body.date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res = online.check_solutions([sol], date, seat_type=body.seat_type, people_count=1)
    return {"solution": res["results"][0], "date": date}


# ------------------------------------------------------------
# SSE 聊天（agent 循环）
# ------------------------------------------------------------

def _sse(event: str, data: Dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _run_tool_events(name: str, args: Dict, ctx: ToolContext):
    """在工作线程执行工具，进度经队列实时回传。返回生成器：(progress 事件..., (ai, panel))"""
    q: queue.Queue = queue.Queue()
    box: Dict = {}

    def on_progress(tname, cur, total, msg):
        q.put(("progress", {"tool": tname, "current": cur, "total": total, "message": msg}))

    def worker():
        try:
            box["result"] = execute_tool(name, args, ctx, on_progress)
        except Exception as e:      # 双保险（execute_tool 内已兜底）
            box["result"] = ({"error": f"工具执行异常: {e}"}, None)
        q.put(None)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    while True:
        item = q.get()
        if item is None:
            break
        yield item[1]
    th.join(timeout=5)
    yield box.get("result", ({"error": "工具无返回"}, None))


def chat_stream(sess: Session, user_message: str):
    """SSE 生成器：一轮 = 一次 LLM 请求；无工具调用的轮 = 正式输出"""
    sid = sess.id
    with _CONFIG_LOCK:
        key, model, base = CONFIG["api_key"], CONFIG["model"], CONFIG["base_url"]
    if not (key and model and base):
        yield _sse("error", {"message": "配置不完整，请先在设置页填写 API-KEY / 模型 / 接口地址"})
        yield _sse("done", {"usage": None})
        return

    if not sess.messages:
        sess.messages.append({"role": "system", "content": prompts.build_system_prompt()})
    sess.messages.append({"role": "user", "content": user_message})
    sess.trace.append({"type": "user", "content": user_message,
                       "ts": datetime.now().isoformat(timespec="seconds")})
    if sess.title == "新对话" and user_message:
        sess.title = user_message[:20]
    _persist(sess)

    assistant_entry = {"type": "assistant", "rounds": [], "final_content": "", "usage": None}
    sess.trace.append(assistant_entry)
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    stopped = False

    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            if sess.stop_event.is_set():
                stopped = True
                break
            yield _sse("round_start", {"round": rnd})

            entry = {"round": rnd, "thinking": "", "thinking_seconds": 0,
                     "content": "", "tools": [], "is_final": False}
            assistant_entry["rounds"].append(entry)

            # 上下文截断：system + 最近 N 条
            ctx_msgs = ([sess.messages[0]] + sess.messages[-(MAX_CONTEXT_MESSAGES - 1):]) \
                if len(sess.messages) > MAX_CONTEXT_MESSAGES else sess.messages

            thinking_t0 = None
            pending_tool_calls = None

            try:
                for ev in llm.stream_chat(key, base, model, ctx_msgs, TOOLS_SCHEMA):
                    if sess.stop_event.is_set():
                        stopped = True
                        break
                    t = ev["type"]
                    if t == "thinking":
                        if thinking_t0 is None:
                            thinking_t0 = time.time()
                        entry["thinking"] += ev["delta"]
                        yield _sse("thinking_delta", {"delta": ev["delta"]})
                    elif t == "content":
                        entry["content"] += ev["delta"]
                        yield _sse("text_delta", {"delta": ev["delta"]})
                    elif t == "tool_calls":
                        pending_tool_calls = ev["calls"]
                    elif t == "usage":
                        total_usage["prompt_tokens"] += ev.get("prompt_tokens") or 0
                        total_usage["completion_tokens"] += ev.get("completion_tokens") or 0
                if stopped:
                    break
            except llm.LLMError as e:
                yield _sse("error", {"message": f"模型调用失败: {e}"})
                yield _sse("done", {"usage": total_usage})
                return

            if thinking_t0 is not None:
                entry["thinking_seconds"] = round(time.time() - thinking_t0, 1)
                yield _sse("thinking_end", {"seconds": entry["thinking_seconds"]})

            # ---- 无工具调用 → 正式输出，结束 ----
            if not pending_tool_calls:
                if entry["content"]:
                    sess.messages.append({"role": "assistant", "content": entry["content"]})
                entry["is_final"] = True
                assistant_entry["final_content"] = entry["content"]
                yield _sse("final", {"round": rnd})
                break

            # ---- 有工具调用：逐个执行 → 结果回填 ----
            sess.messages.append({
                "role": "assistant",
                "content": entry["content"] or None,
                "tool_calls": [{
                    "id": c.get("id") or f"call_{rnd}_{i}",
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"},
                } for i, c in enumerate(pending_tool_calls)],
            })

            for i, c in enumerate(pending_tool_calls):
                if sess.stop_event.is_set():
                    stopped = True
                    break
                call_id = c.get("id") or f"call_{rnd}_{i}"
                name = c.get("name", "")
                try:
                    args = json.loads(c.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                yield _sse("tool_call", {"id": call_id, "name": name, "arguments": args})

                tool_t0 = time.time()
                ai_result, panel = {"error": "工具无返回"}, None
                for item in _run_tool_events(name, args, sess.toolctx):
                    if isinstance(item, tuple):
                        ai_result, panel = item
                    else:
                        yield _sse("tool_progress", item)
                duration_ms = int((time.time() - tool_t0) * 1000)

                yield _sse("tool_result", {"id": call_id, "ai_result": ai_result,
                                           "panel_solutions": panel})
                entry["tools"].append({"id": call_id, "name": name, "arguments": args,
                                       "ai_result": ai_result, "duration_ms": duration_ms})
                sess.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "content": json.dumps(ai_result, ensure_ascii=False)})
                _persist(sess)
            yield _sse("round_end", {"round": rnd})

        if stopped:
            yield _sse("error", {"message": "已停止（已完成内容保留）"})
        assistant_entry["usage"] = dict(total_usage)
        _persist(sess)
        yield _sse("done", {"usage": total_usage})
    finally:
        sess.stop_event.clear()


class StopIn(BaseModel):
    session_id: str


@app.post("/api/chat/stop")
def api_chat_stop(body: StopIn):
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(body.session_id)
    if s:
        s.stop_event.set()
    return {"ok": True}


@app.post("/api/chat/stream")
def api_chat_stream(body: ChatIn):
    if not (body.message or "").strip():
        raise HTTPException(400, "消息不能为空")
    sess = get_session(body.session_id)
    return StreamingResponse(chat_stream(sess, body.message.strip()),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ------------------------------------------------------------
# 静态资源
# ------------------------------------------------------------

_STATIC_DIR = os.path.join(_BASE_DIR, "static")


@app.get("/api/meta")
def api_meta():
    return {"seats": db.SEAT_CN_LIST, "default_date": online.default_date(),
            "sort_options": ["comprehensive", "fastest", "cheapest", "earliest"]}


app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("TRIPAI_PORT", "8600"))
    uvicorn.run(app, host="127.0.0.1", port=port)
