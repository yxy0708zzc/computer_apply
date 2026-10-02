"""
tools.py —— Agent 工具定义与执行（结果双路输出：给 AI 的摘要 + 给前端数据面板的完整数据）

5 个工具：
  search_station        站名/拼音/电报码模糊搜索（消歧）
  find_solutions        本地方案引擎（直达/换乘枚举，静态数据，余票未核实）
  check_tickets_online  指定方案联网核实 12306 实时余票+票价（区间合并请求）
  query_train_stops     车次经停表
  query_ticket_variants 无票深挖：补票变体（买短补长/前后多买一站）生成并联网核实
"""

import json
from typing import Callable, Dict, List, Optional, Tuple

from . import database as db
from . import online
from . import planner

TOP_CHECK_LIMIT = 5          # 单次联网核实方案数上限（反爬保护）
AI_BRIEF_LIMIT = 8           # 返回给 AI 的方案数上限 N
AI_ENUM_MULT = 20            # AI 模式：后端枚举 = N × 20
PANEL_MULT = 2               # 详情页展示 = N × 2
MANUAL_ENUM_MULT = 10        # 手动模式：后端枚举 = 要求数 × 10

TOOLS_SCHEMA: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_station",
            "description": "按关键词（站名/城市名/拼音/电报码）模糊搜索车站，返回候选列表。用户给的站名有歧义（如'北京'）时先用本工具列出候选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "关键词，如：北京 / 上海虹桥 / hongqiao / VNP"},
                    "limit": {"type": "integer", "description": "返回数量上限，默认 10"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_solutions",
            "description": "在本地列车数据中枚举出发地→到达地的可行乘车方案（直达 + 最多 2 次换乘），带 0~100 评分排序。注意：结果是本地静态数据，不含实时余票；推荐方案需再调 check_tickets_online 核实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_station": {"type": "string", "description": "出发站（站名或电报码，优先用 search_station 返回的 station_id）"},
                    "to_station": {"type": "string", "description": "到达站（站名或电报码）"},
                    "date": {"type": "string", "description": "出行日期 YYYY-MM-DD（可选，默认最近可售；仅记录，联网时使用）"},
                    "people_count": {"type": "integer", "description": "人数，默认 1"},
                    "seat_type": {"type": "string", "enum": db.SEAT_CN_LIST, "description": "坐席，默认二等座。普速车无二等座，需要时指定硬座/硬卧"},
                    "allow_transfer": {"type": "boolean", "description": "是否允许换乘，默认 true"},
                    "max_transfers": {"type": "integer", "description": "最大换乘次数，默认 1，最多 2"},
                    "prefer_direct": {"type": "boolean", "description": "优先直达（排序加权），默认 true"},
                    "depart_after": {"type": "string", "description": "最早出发时刻 HH:MM（可选）"},
                    "depart_before": {"type": "string", "description": "最晚出发时刻 HH:MM（可选）"},
                    "arrive_after": {"type": "string", "description": "最早到达时刻 HH:MM（可选）"},
                    "arrive_before": {"type": "string", "description": "最晚到达时刻 HH:MM（可选）"},
                    "sort_by": {"type": "string", "enum": ["comprehensive", "fastest", "cheapest", "earliest", "depart_latest", "arrive_earliest"],
                                "description": "排序：comprehensive 按评分（默认）/ fastest 历时最短 / cheapest 价格最低 / earliest 最早出发 / depart_latest 最晚出发 / arrive_earliest 最早到达；非 comprehensive 直接比较排序不评分"},
                    "max_results": {"type": "integer", "description": "返回给你的方案数上限，默认 8；详情页始终展示全量候选"},
                },
                "required": ["from_station", "to_station"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_tickets_online",
            "description": "联网查询 12306 实时余票与票价（对指定方案）。请求有频率限制，单次最多 5 个方案，建议只核实推荐方案。",
            "parameters": {
                "type": "object",
                "properties": {
                    "solution_ids": {"type": "array", "items": {"type": "string"},
                                     "description": "find_solutions 返回的方案编号列表，如 [\"s1\",\"s3\"]"},
                    "date": {"type": "string", "description": "查询日期 YYYY-MM-DD（可选，默认方案生成时的日期）"},
                },
                "required": ["solution_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_train_stops",
            "description": "查询车次完整经停表（站序/到发时刻），用于判断经停站与运行区间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "train_num": {"type": "string", "description": "车次号，如 G25"},
                },
                "required": ["train_num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_ticket_variants",
            "description": "指定车次无票时的深挖：生成补票变体（前后多买一站/买短补长）并联网核实各变体余票票价。返回的方案需向用户明确标注'需车上补票'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "train_num": {"type": "string", "description": "车次号"},
                    "from_station": {"type": "string", "description": "实际出发站（电报码优先）"},
                    "to_station": {"type": "string", "description": "实际到达站（电报码优先）"},
                    "date": {"type": "string", "description": "查询日期 YYYY-MM-DD（可选）"},
                    "seat_type": {"type": "string", "enum": db.SEAT_CN_LIST, "description": "坐席，默认二等座"},
                },
                "required": ["train_num", "from_station", "to_station"],
            },
        },
    },
]


class ToolContext:
    """会话级工具上下文：方案存储（供 check / 手动查票面板共享）"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.solutions: Dict[str, Dict] = {}      # solution_id -> solution（含 checked 状态）
        self.variants: Dict[str, List[Dict]] = {} # key -> variants（补票变体缓存）
        self._seq = 0

    def store(self, sols: List[Dict], date: str) -> List[Dict]:
        """存方案并分配会话内唯一 id（跨多次 find 递增，避免 s1 被覆盖）"""
        out = []
        for s in sols:
            self._seq += 1
            sid = f"s{self._seq}"
            s = dict(s)
            s["solution_id"] = sid
            s["date"] = date
            self.solutions[sid] = s
            out.append(s)
        return out

    def get_many(self, ids: List[str]) -> Tuple[List[Dict], List[str]]:
        found, missing = [], []
        for i in ids:
            s = self.solutions.get(i)
            if s:
                found.append(s)
            else:
                missing.append(i)
        return found, missing


def _resolve_station(conn, val: str, ctx: ToolContext, name: str) -> Optional[str]:
    sid = db.resolve_station_name_or_id(conn, val)
    return sid


def execute_tool(name: str, args: Dict, ctx: ToolContext,
                 on_progress: Optional[Callable[[str, int, int, str], None]] = None
                 ) -> Tuple[Dict, Optional[List[Dict]]]:
    """执行工具。返回 (ai_result, panel_solutions)：
    ai_result 给模型（精简摘要 JSON）；panel_solutions 非空时前端数据面板整体更新。"""
    conn = db.get_railway_conn()
    try:
        if name == "search_station":
            rows = db.search_stations(conn, args.get("keyword", ""), args.get("limit", 10))
            return ({"count": len(rows), "stations": rows}, None), None

        if name == "find_solutions":
            from_v = _resolve_station(conn, args.get("from_station", ""), ctx, "from")
            to_v = _resolve_station(conn, args.get("to_station", ""), ctx, "to")
            if not from_v or not to_v:
                bad = args.get("from_station") if not from_v else args.get("to_station")
                return {"error": f"无法识别车站: {bad}（可用 search_station 搜索确认）"}, None
            date = online.normalize_date(args.get("date"))
            seat = args.get("seat_type") or "二等座"
            ai_max = int(args.get("max_results") or AI_BRIEF_LIMIT)
            mt_raw = args.get("max_transfers")
            # 统一枚举策略：后端枚举 N×20 候选 → 去重打分 → 前 N 给 AI、前 2N 给详情
            enum_total = ai_max * AI_ENUM_MULT
            panel_max = ai_max * PANEL_MULT
            sols = planner.plan_solutions(
                conn, from_v, to_v,
                seat_type=seat,
                allow_transfer=bool(args.get("allow_transfer", True)),
                max_transfers=1 if mt_raw is None else int(mt_raw),
                auto_expand=(mt_raw is None),      # 未指定换乘次数：自动加深挖到足数
                prefer_direct=bool(args.get("prefer_direct", True)),
                depart_after=args.get("depart_after"),
                depart_before=args.get("depart_before"),
                arrive_after=args.get("arrive_after"),
                arrive_before=args.get("arrive_before"),
                sort_by=args.get("sort_by") or "comprehensive",
                max_results=ai_max,
                enum_total=enum_total,
            )
            stored = ctx.store(sols[:panel_max], date)   # 详情页：前 2N
            for s in stored:
                s["seat_type"] = seat
            # AI 摘要：评分前 N（详情页展示前 2N）
            brief = [{
                "solution_id": s["solution_id"], "score": s.get("score"),
                "type": s["type"],
                "depart_time": s["depart_time"], "arrive_time": s["arrive_time"],
                "total_duration": s["total_duration"],
                "trains": [seg["train_num"] for seg in s["segments"]],
                "transfer_station": s.get("transfer_station"),
                "price_est": s.get("price_est"),
            } for s in stored[:ai_max]]
            return ({"date": date, "seat_type": seat,
                     "enum_total": enum_total, "count": len(stored),
                     "returned": len(brief),
                     "note": "后端枚举 N×20 候选去重打分；前 N 为推荐，前 2N 进详情页；"
                             "本地静态方案余票未核实，对推荐方案调 check_tickets_online",
                     "solutions": brief}, stored), stored

        if name == "check_tickets_online":
            ids = [str(x) for x in (args.get("solution_ids") or [])][:TOP_CHECK_LIMIT]
            if not ids:
                return {"error": "solution_ids 为空"}, None
            found, missing = ctx.get_many(ids)
            if missing:
                return {"error": f"方案不存在或已失效: {','.join(missing)}"}, None
            date = online.normalize_date(args.get("date") or found[0].get("date"))
            people = 1

            def prog(cur, total, msg):
                if on_progress:
                    on_progress("check_tickets_online", cur, total, msg)

            res = online.check_solutions(found, date,
                                         seat_type=args.get("seat_type") or _seat_of(found),
                                         people_count=people, on_progress=prog)
            for s in res["results"]:
                ctx.solutions[s["solution_id"]] = s
            panel = res["results"]
            brief = [{
                "solution_id": s["solution_id"], "can_buy": s["can_buy"],
                "check_note": s["check_note"],
                "seats": [{"train": g["train_num"], "seat": g.get("seat_cn"),
                           "tickets": g.get("tickets_raw"), "price": g.get("price"),
                           "status": g.get("ticket_status")} for g in s["segments"]],
            } for s in panel]
            return ({"date": res["date"], "checked": len(panel),
                     "errors": res["errors"], "results": brief}, panel), panel

        if name == "query_train_stops":
            tn = args.get("train_num", "")
            stops = db.get_train_stops(conn, tn)
            if not stops:
                return {"error": f"车次不存在: {tn}"}, None
            return {"train_num": tn, "count": len(stops),
                    "stops": [{"station": s["station_name"], "arrive": s["stop_time"]}
                              for s in stops]}, None

        if name == "query_ticket_variants":
            tn = args.get("train_num", "")
            from_v = _resolve_station(conn, args.get("from_station", ""), ctx, "from")
            to_v = _resolve_station(conn, args.get("to_station", ""), ctx, "to")
            if not from_v or not to_v:
                return {"error": "出发/到达站无法识别"}, None
            date = online.normalize_date(args.get("date"))
            seat = args.get("seat_type") or "二等座"
            variants = planner.build_ticket_variants(conn, tn, from_v, to_v)
            if not variants:
                return {"error": f"{tn} 不经过该区间，无法生成补票变体"}, None
            # 变体转伪方案复用核实管线
            pseudo = [{
                "solution_id": f"v{i+1}", "type": "variant",
                "date": date, "segments": [{
                    "train_num": tn, "train_no": db.get_train_no(conn, tn),
                    "from_station_id": v["buy_from"], "to_station_id": v["buy_to"],
                }],
                "_variant": v,
            } for i, v in enumerate(variants)]

            def prog(cur, total, msg):
                if on_progress:
                    on_progress("query_ticket_variants", cur, total, msg)

            res = online.check_solutions(pseudo, date, seat_type=seat, people_count=1,
                                         on_progress=prog)
            brief = []
            for p in res["results"]:
                seg = p["segments"][0]
                v = p["_variant"]
                brief.append({
                    "variant": v["type"],
                    "buy": f"{v['buy_from_name']}→{v['buy_to_name']}",
                    "ride": f"{v['ride_from_name']}→{v['ride_to_name']}",
                    "seat": seg.get("seat_cn"), "tickets": seg.get("tickets_raw"),
                    "price": seg.get("price"), "status": seg.get("ticket_status"),
                    "note": v["note"],
                })
            ok = [b for b in brief if b["status"] in ("ok", "few")]
            return ({"train_num": tn, "date": date, "seat_type": seat,
                     "note": "补票方案需车上补票；仅推荐 status 为 ok/few 的变体",
                     "buyable_count": len(ok), "variants": brief}, None), None

        return {"error": f"未知工具: {name}"}, None
    except ValueError as e:
        return {"error": str(e)}, None
    except Exception as e:
        return {"error": f"工具执行异常: {e}"}, None
    finally:
        conn.close()


def _seat_of(solutions: List[Dict]) -> str:
    """从方案段推断核实席别（find 时记录在 solution）"""
    for s in solutions:
        st = s.get("seat_type")
        if st:
            return st
    return "二等座"
