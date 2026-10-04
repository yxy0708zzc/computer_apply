"""
planner.py —— 本地方案引擎（移植 travel2 出题算法的「找解」内核）

复用并改造的 travel2 部件（剥离出题特有的造票/干扰注入/题库写入）：
- get_routes_between 同车直达 SQL
- find_transfer_solutions 式一条 SQL 枚举 (中转站, 换乘车) + TRANSFER_MIN_GAP=20
- _city_key 同城过滤（去"东/南/西/北/站"后缀）
- 「换乘车不经过出发站」全局不变量（杜绝"换乘实则可直达"的伪方案）
- 补票三策略 extra_front / short_buy / extra_rear（买短补长/前后多买一站）

toC 扩展：
- 换乘链式枚举（支持 1~2 次换乘，层间按累计历时截断防组合爆炸）
- 到/发双向时间窗（depart_after~depart_before / arrive_after~arrive_before，支持跨零点）
- 方案评分 score（0~100，随方案返回给 AI 与前端）

「有票」不靠写库保证，而由联网核实（online.py）回填。
"""

import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from . import database as db

TRANSFER_MIN_GAP = 20          # 换乘衔接最短分钟（travel2 同款）
TRANSFER_MAX_FIRST = 400       # 第一程枚举上限（防超大站对全量爆炸）
FRONTIER_CAP = 40              # 换乘链每层保留的路径数（防组合爆炸）
MAX_TRANSFERS_LIMIT = 2        # 引擎支持的最多换乘次数
ENUM_TOTAL_CAP = 1000          # 候选池上限（10:20:200 比例最多尝试 1000 次）
VALID_SORT = {"comprehensive", "fastest", "cheapest", "earliest",
              "depart_latest", "arrive_earliest"}
VALID_TRAIN_TYPE = {"all", "highspeed", "normal"}
# 车型划分（用户定义）：高铁动车 = G/C/D 字头；普速 = 其他（K/T/Z/纯数字）
HIGHSPEED_PREFIX = ("G", "C", "D")


def _match_train_type(sol: Dict, train_type: str) -> bool:
    """方案的全部段均符合所选车型才通过（all 恒过）"""
    if train_type not in ("highspeed", "normal"):
        return True
    for seg in sol.get("segments", []):
        ch = (seg.get("train_num") or " ")[0].upper()
        hs = ch in HIGHSPEED_PREFIX
        if train_type == "highspeed" and not hs:
            return False
        if train_type == "normal" and hs:
            return False
    return True


def _city_key(station_name: str) -> str:
    """站名 → 城市标识（去方位后缀），用于排除与目的地同城的中转站"""
    for suf in ("东", "南", "西", "北", "站"):
        if station_name.endswith(suf) and len(station_name) > len(suf) + 1:
            return station_name[:-len(suf)]
    return station_name


def _valid_hhmm(t: Optional[str]) -> bool:
    return bool(t) and re.fullmatch(r"\d{2}:\d{2}", t) is not None


def _in_window(hhmm: str, after: Optional[str], before: Optional[str]) -> bool:
    """时刻是否落在 [after, before] 窗口内（支持跨零点窗口，如 22:00→06:00）"""
    if not after and not before:
        return True
    if not _valid_hhmm(hhmm):
        return False
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m
    ta = tb = None
    if after and _valid_hhmm(after):
        ha, ma = map(int, after.split(":"))
        ta = ha * 60 + ma
    if before and _valid_hhmm(before):
        hb, mb = map(int, before.split(":"))
        tb = hb * 60 + mb
    if ta is not None and tb is not None:
        return ta <= t <= tb if ta <= tb else (t >= ta or t <= tb)   # 跨零点
    if ta is not None:
        return t >= ta
    if tb is not None:
        return t <= tb
    return True


def _dur_minutes(dur: str) -> int:
    try:
        h, m = map(int, dur.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return 10 ** 6


def _hmm_to_min(hhmm: str) -> int:
    """HH:MM → 分钟（非法值排到当天最后）"""
    if not _valid_hhmm(hhmm):
        return 24 * 60
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


# ------------------------------------------------------------
# 方案构造
# ------------------------------------------------------------

def _make_segment(conn, train_num: str, from_id: str, to_id: str,
                  depart: str, arrive: str, seat_type: str) -> Dict:
    return {
        "train_num": train_num,
        "train_no": db.get_train_no(conn, train_num),
        "from_station_id": from_id, "to_station_id": to_id,
        "from_station_name": db.get_station_name(conn, from_id),
        "to_station_name": db.get_station_name(conn, to_id),
        "depart_time": depart, "arrive_time": arrive,
        "duration": db.calc_duration(depart, arrive),
    }


def _direct_solutions(conn, from_id: str, to_id: str, seat_type: str,
                      win: Dict) -> List[Dict]:
    """直达方案：同车经过 A→B 的每一辆车。
    改号车同组多码只保留码序最小的一份（SQL 已按 train_num 排序，物理车按 train_no 去重）。"""
    out = []
    seen_tno: set = set()
    for r in db.get_routes_between(conn, from_id, to_id):
        if not (_valid_hhmm(r["depart_time"]) and _valid_hhmm(r["arrive_time"])):
            continue    # 脏时刻（空/--）车次跳过
        if not _in_window(r["depart_time"], win.get("depart_after"), win.get("depart_before")):
            continue
        if not _in_window(r["arrive_time"], win.get("arrive_after"), win.get("arrive_before")):
            continue
        tno = db.get_train_no(conn, r["train_num"])
        if tno and tno in seen_tno:
            continue                      # 同物理车的另一显示码，跳过
        if tno:
            seen_tno.add(tno)
        seg = _make_segment(conn, r["train_num"], from_id, to_id,
                            r["depart_time"], r["arrive_time"], seat_type)
        out.append({
            "type": "direct", "transfer_count": 0,
            "depart_time": r["depart_time"], "arrive_time": r["arrive_time"],
            "total_duration": r["duration"], "segments": [seg],
            "transfer_waits": [],
        })
    return out


# ------------------------------------------------------------
# 换乘链式枚举（支持 1~MAX_TRANSFERS_LIMIT 次换乘）
# ------------------------------------------------------------

def _transfer_solutions(conn, from_id: str, to_id: str, seat_type: str,
                        win: Dict, max_transfers: int = 1,
                        quota: int = 60) -> List[Dict]:
    """链式枚举换乘方案。全局不变量（travel2 口径）：
    - 第一程车不经过 B（经过 B 属于直达）
    - 后续程车一律不经过出发站 A（防"换乘实则可直达"伪方案）
    - 非末段车不经过 B（防跳过后续换乘直达终点）
    - 换乘间隔 >= TRANSFER_MIN_GAP；中转站与目的地不同城；当日完成
    层间按累计历时截断 FRONTIER_CAP 条路径，防组合爆炸。"""
    max_transfers = max(1, min(int(max_transfers), MAX_TRANSFERS_LIMIT))
    dest_city = _city_key(db.get_station_name(conn, to_id))

    # ---- 第一程：经过 A、不经过 B、A 之后有中间站 → frontier 路径 ----
    # path = {"segs": [...], "dep_a", "arr_last", "last_id", "trains": set, "waits": []}
    frontier: List[Dict] = []
    first_rows = conn.execute("""
        SELECT ta.train_num, ta.stop_time, tm.station_id, tm.station_name, tm.stop_time
        FROM train_stops ta
        JOIN train_stops tm ON tm.train_num = ta.train_num AND tm.stop_no > ta.stop_no
        WHERE ta.station_id = ?
          AND NOT EXISTS (SELECT 1 FROM train_stops tb
                          WHERE tb.train_num = ta.train_num AND tb.station_id = ?)
          AND ta.stop_time GLOB '[0-9][0-9]:[0-9][0-9]'
          AND tm.stop_time GLOB '[0-9][0-9]:[0-9][0-9]'
        LIMIT ?
    """, (from_id, to_id, TRANSFER_MAX_FIRST)).fetchall()
    seen_first = set()
    for t1, dep_a, m_id, m_name, arr_m in first_rows:
        if _city_key(m_name) == dest_city:
            continue
        if not _in_window(dep_a, win.get("depart_after"), win.get("depart_before")):
            continue
        total = db.time_diff_minutes(dep_a, arr_m)
        if total < 0:
            continue
        key = (t1, m_id)
        if key in seen_first:
            continue
        seen_first.add(key)
        frontier.append({
            "segs": [(t1, from_id, m_id, dep_a, arr_m)],
            "dep_a": dep_a, "arr_last": arr_m, "last_id": m_id,
            "trains": {t1}, "waits": [], "total": total,
        })
    if not frontier:
        return []

    dep_a_cache: Dict[str, Optional[str]] = {}

    def _dep_a_of(t: str) -> Optional[str]:
        if t not in dep_a_cache:
            row = conn.execute(
                "SELECT stop_time FROM train_stops WHERE train_num = ? AND station_id = ?",
                (t, from_id)).fetchone()
            dep_a_cache[t] = row[0] if row and _valid_hhmm(row[0]) else None
        return dep_a_cache[t]

    completed: List[Dict] = []

    for k in range(1, max_transfers + 1):
        is_last = (k == max_transfers)
        # 按累计历时截断前沿（防爆炸）
        frontier.sort(key=lambda p: p["total"])
        frontier = frontier[:FRONTIER_CAP]
        stops = sorted({p["last_id"] for p in frontier})
        if not stops:
            break

        # 从前沿站出发的下一程：(起点站, 车次, 发车, 下车站, 下车名, 到达)
        next_legs: Dict[str, List] = {}
        ph = ",".join("?" * len(stops))
        sql = f"""
            SELECT um.station_id, um.train_num, um.stop_time,
                   uv.station_id, uv.station_name, uv.stop_time
            FROM train_stops um
            JOIN train_stops uv ON uv.train_num = um.train_num AND uv.stop_no > um.stop_no
            WHERE um.station_id IN ({ph})
              AND um.stop_time GLOB '[0-9][0-9]:[0-9][0-9]'
              AND uv.stop_time GLOB '[0-9][0-9]:[0-9][0-9]'
              AND NOT EXISTS (SELECT 1 FROM train_stops xa
                              WHERE xa.train_num = um.train_num AND xa.station_id = ?)
        """
        params = list(stops) + [from_id]
        if not is_last:
            sql += """ AND NOT EXISTS (SELECT 1 FROM train_stops xb
                                       WHERE xb.train_num = um.train_num AND xb.station_id = ?)"""
            params.append(to_id)
        if is_last:
            sql += " AND uv.station_id = ?"
            params.append(to_id)
        # 同组多码最小码先出：前沿名额不被重复码挤占，且去重结果稳定
        sql += " ORDER BY um.train_num, um.stop_time LIMIT 6000"
        for f_id, t, dep_f, nxt_id, nxt_name, arr_n in conn.execute(sql, params).fetchall():
            next_legs.setdefault(f_id, []).append((t, dep_f, nxt_id, nxt_name, arr_n))

        new_frontier: List[Dict] = []
        for path in frontier:
            legs = next_legs.get(path["last_id"], [])
            for t, dep_f, nxt_id, nxt_name, arr_n in legs:
                if t in path["trains"]:
                    continue                      # 同车不连续乘坐
                if not is_last and _city_key(nxt_name) == dest_city:
                    continue                      # 非末段：中转站与目的地同城排除
                gap = db.time_diff_minutes(path["arr_last"], dep_f)
                if gap < TRANSFER_MIN_GAP:
                    continue
                dep_a = path["dep_a"]
                total = db.time_diff_minutes(dep_a, arr_n)
                if total < 0:
                    continue                      # 跨天淘汰（当日完成约束）
                if not _in_window(arr_n, win.get("arrive_after"), win.get("arrive_before")):
                    continue
                new_frontier.append({
                    "segs": path["segs"] + [(t, path["last_id"], nxt_id, dep_f, arr_n)],
                    "dep_a": dep_a, "arr_last": arr_n, "last_id": nxt_id,
                    "trains": path["trains"] | {t},
                    "waits": path["waits"] + [gap],
                    "total": total,
                })
        if is_last:
            for p in new_frontier:
                p["frontier_rank"] = 0
            completed.extend(new_frontier)
            frontier = []
        else:
            frontier = new_frontier

    # ---- 组装 solutions ----
    completed.sort(key=lambda p: p["total"])
    completed = completed[:max(1, int(quota))]
    out = []
    for p in completed:
        segs = [_make_segment(conn, t, f, tt, d, a, seat_type)
                for t, f, tt, d, a in p["segs"]]
        transfer_stations = [db.get_station_name(conn, p["segs"][i][2])
                             for i in range(1, len(p["segs"]))]
        out.append({
            "type": "transfer", "transfer_count": len(p["waits"]),
            "depart_time": p["dep_a"], "arrive_time": p["arr_last"],
            "total_duration": f"{p['total'] // 60:02d}:{p['total'] % 60:02d}",
            "transfer_waits": p["waits"],
            "transfer_station": "、".join(transfer_stations),
            "segments": segs,
        })
    return out


def _price_estimate(conn_unused, sol: Dict, seat_type: str,
                    fallback_seat: Optional[str] = None) -> Optional[float]:
    """历史参考价（换乘=段和；任一段缺失则 None）。
    fallback_seat：指定席别某段查不到价时的回落席别（如 AI 模式二等座→硬座，
    普速段存的是硬座价；段级回落，G+K 换乘方案各自取到可用价）。"""
    total = 0.0
    for seg in sol["segments"]:
        p = db.get_price_estimate(seg["from_station_id"], seg["to_station_id"], seat_type)
        if p is None and fallback_seat:
            p = db.get_price_estimate(seg["from_station_id"], seg["to_station_id"],
                                      fallback_seat)
        if p is None:
            return None
        total += p
    return round(total, 1)


# ------------------------------------------------------------
# 评分（0~100，随方案返回，comprehensive 排序依据）
# ------------------------------------------------------------
# 约束（时间窗 / 换乘间隔 ≥20min / 同城过滤等）在枚举期已硬过滤，不参与打分。
# 打分 = 多维度归一化加权（各维度 0~1，越优越接近 1，权重和归一）：
#   T 时间   = min_dur / dur                      （最快 = 1）
#   P 价格   = min_price / price                  （最便宜 = 1；价格不可估则剔除该维度）
#   C 换乘数 = 1 / (1 + 0.6 × 换乘次数)           （直达 = 1）
#   W 等待   = 20 / (20 + 总换乘等待分钟)          （直达剔除该维度）
#   E 补票   = 1 / (1 + 0.6 × 补票段数)           （无补票 = 1）
# 权重：T=0.35, P=0.25, C=0.15, W=0.10, E=0.15；score = 100 × 加权和 / 有效权重和。

_WEIGHTS = {"time": 0.35, "price": 0.25, "transfer": 0.15, "wait": 0.10, "extra": 0.15}


def score_solutions(sols: List[Dict]) -> List[Dict]:
    """仅 comprehensive 模式调用；fastest/earliest 等直接比较排序，不评分。"""
    if not sols:
        return sols
    durs = [_dur_minutes(s["total_duration"]) for s in sols]
    valid_durs = [d for d in durs if 0 < d < 10 ** 6]
    min_dur = min(valid_durs) if valid_durs else 0
    prices = [s["price_est"] for s in sols if s.get("price_est") is not None]
    min_price = min(prices) if prices else None
    for s, dur in zip(sols, durs):
        parts, total_w = [], 0.0
        if min_dur and 0 < dur < 10 ** 6:
            parts.append(_WEIGHTS["time"] * (min_dur / dur))
            total_w += _WEIGHTS["time"]
        if s.get("price_est") is not None and min_price:
            parts.append(_WEIGHTS["price"] * (min_price / s["price_est"]))
            total_w += _WEIGHTS["price"]
        n_trans = s.get("transfer_count", 0)
        parts.append(_WEIGHTS["transfer"] * (1.0 / (1.0 + 0.6 * n_trans)))
        total_w += _WEIGHTS["transfer"]
        waits = s.get("transfer_waits") or []
        if waits:
            parts.append(_WEIGHTS["wait"] * (20.0 / (20.0 + sum(waits))))
            total_w += _WEIGHTS["wait"]
        n_extra = sum(1 for g in s.get("segments", []) if g.get("seg_type") == "purchase")
        if s.get("type") == "variant":
            n_extra = 1
        parts.append(_WEIGHTS["extra"] * (1.0 / (1.0 + 0.6 * n_extra)))
        total_w += _WEIGHTS["extra"]
        s["score"] = int(round(100.0 * (sum(parts) / total_w))) if total_w else 50
    return sols


# ------------------------------------------------------------
# 主入口
# ------------------------------------------------------------

def plan_solutions(conn: sqlite3.Connection, from_id: str, to_id: str, *,
                   seat_type: str = "二等座",
                   allow_transfer: bool = True, max_transfers: int = 1,
                   prefer_direct: bool = True,
                   depart_after: Optional[str] = None,
                   depart_before: Optional[str] = None,
                   arrive_after: Optional[str] = None,
                   arrive_before: Optional[str] = None,
                   sort_by: str = "comprehensive",
                   max_results: int = 8,
                   auto_expand: bool = True,
                   enum_total: Optional[int] = None,
                   train_type: str = "all",
                   seat_fallback: Optional[str] = None) -> List[Dict]:
    """主入口：枚举直达 + 换乘（链式）方案，评分排序。

    候选池策略（两模式统一比例）：
    - enum_total = 候选池总额（AI 模式=要求数×20，手动模式=要求数×10）
    - 允许换乘时直达与换乘各挖同样数量（enum_total 的一半）
    - 去重 → 全局打分 → 排序，返回前 min(enum_total, …) 条候选池；
      调用方再切分：AI 取前 N、详情取前 2N、手动取前 N

    auto_expand=True（未显式指定换乘次数）时：换乘候选不足配额会自动加深
    挖掘（1 次 → 2 次 → 引擎上限），直到挖到足数或到达上限；
    显式指定 max_transfers 时尊重上限不再加深。"""
    if sort_by not in VALID_SORT:
        sort_by = "comprehensive"
    if train_type not in VALID_TRAIN_TYPE:
        train_type = "all"
    if not enum_total or enum_total < max_results:
        enum_total = max_results * 20
    enum_total = min(int(enum_total), ENUM_TOTAL_CAP)   # 最多尝试 1000 次
    win = {"depart_after": depart_after, "depart_before": depart_before,
           "arrive_after": arrive_after, "arrive_before": arrive_before}

    sols = [s for s in _direct_solutions(conn, from_id, to_id, seat_type, win)
            if _match_train_type(s, train_type)]
    seen: set = set()
    for s in sols:
        seen.add(_sig(s))

    if allow_transfer and max_transfers >= 1:
        quota = max(1, enum_total // 2)          # 换乘与直达查同样的方案数
        level = 1
        pool = 0
        while level <= max_transfers:
            batch = _transfer_solutions(conn, from_id, to_id, seat_type, win, level,
                                        quota=quota)
            fresh = []
            for s in batch:
                sig = _sig(s)
                if sig not in seen:
                    seen.add(sig)
                    if _match_train_type(s, train_type):
                        fresh.append(s)
                    else:
                        seen.discard(sig)        # 被车型过滤的不占用去重位
            sols += fresh
            pool += len(fresh)
            # 挖到足数（换乘配额满足）再停；或已到上限
            if pool >= quota:
                break
            if not auto_expand and level >= max_transfers:
                break
            if auto_expand and level >= MAX_TRANSFERS_LIMIT:
                break
            level += 1

    for s in sols:
        s["price_est"] = _price_estimate(conn, s, seat_type,
                                         fallback_seat=seat_fallback)
        try:
            s["cross_midnight"] = s["arrive_time"] < s["depart_time"]
        except Exception:
            s["cross_midnight"] = False
    if sort_by == "comprehensive":
        score_solutions(sols)                      # 直接比较排序的模式不评分

    sols = sort_solutions(sols, sort_by, prefer_direct=prefer_direct)
    return sols[:max(int(max_results), min(int(enum_total), 400))]


def sort_solutions(sols: List[Dict], sort_by: str,
                   prefer_direct: bool = False) -> List[Dict]:
    """按排序键排序（返回新排序列表）。多候选池合并后（模糊站组合）也可复用：
    先统一 score_solutions 再以 sort_by 调用本函数。"""
    def key(s):
        direct_rank = (0 if s["type"] == "direct" else 1) if prefer_direct else 0
        if sort_by == "fastest":
            return (direct_rank, _dur_minutes(s["total_duration"]))
        if sort_by == "earliest":
            return (direct_rank, _hmm_to_min(s["depart_time"]))
        if sort_by == "depart_latest":
            return (direct_rank, -_hmm_to_min(s["depart_time"]))   # 最晚出发
        if sort_by == "arrive_earliest":
            return (direct_rank, _hmm_to_min(s["arrive_time"]))    # 最早到达
        if sort_by == "cheapest":
            p = s["price_est"] if s["price_est"] is not None else 10 ** 7
            return (direct_rank, p)
        return -s.get("score", 0)          # comprehensive：按评分降序

    return sorted(sols, key=key)


def _sig(s: Dict) -> Tuple:
    """方案去重签名：物理车序列 + 起讫站序列。
    改号车（同 train_no 多显示码）共用同一物理车，按 train_no 去重，
    避免同一方案以两个车次号重复展示（保留码序最小者，SQL 已按 train_num 排序）。"""
    return tuple((g.get("train_no") or g["train_num"], g["from_station_id"], g["to_station_id"])
                 for g in s.get("segments", []))


# ------------------------------------------------------------
# 补票变体（无票深挖：买短补长 / 前后多买一站，travel2 三策略）
# ------------------------------------------------------------

def build_ticket_variants(conn: sqlite3.Connection, train_num: str,
                          from_id: str, to_id: str) -> List[Dict]:
    """对指定车次的 A→B 生成补票变体（联网核实由 online 层执行）：
    - extra_front：往前 1~3 站买 K→B，实际 A 上车
    - extra_rear：往后 1~3 站买 A→D，实际 B 下车
    - short_buy：买 A→M 坐到 B（取首/中/尾三个代表中间站）"""
    stops = db.get_train_stops(conn, train_num)
    ids = [s["station_id"] for s in stops]
    names = {s["station_id"]: s["station_name"] for s in stops}
    if from_id not in ids or to_id not in ids:
        return []
    i, j = ids.index(from_id), ids.index(to_id)
    if i >= j:
        return []
    out = []

    def add(vtype: str, bf: str, bt: str, rf: str, rt: str, note: str):
        out.append({
            "type": vtype,
            "buy_from": bf, "buy_to": bt,
            "buy_from_name": names.get(bf, bf), "buy_to_name": names.get(bt, bt),
            "ride_from": rf, "ride_to": rt,
            "ride_from_name": names.get(rf, rf), "ride_to_name": names.get(rt, rt),
            "note": note,
        })

    for k in range(max(0, i - 3), i):
        add("extra_front", ids[k], to_id, from_id, to_id,
            f"多买 {names.get(ids[k], ids[k])} 上车（提前 {i - k} 站买票）")
    for k in range(j + 1, min(len(ids), j + 4)):
        add("extra_rear", from_id, ids[k], from_id, to_id,
            f"多买到 {names.get(ids[k], ids[k])} 下车（延后 {k - j} 站买票）")
    if j - i >= 2:
        mids = sorted({i + 1, (i + j) // 2, j - 1})
        for m in mids:
            if i < m < j:
                add("short_buy", from_id, ids[m], from_id, to_id,
                    f"买短补长：购 {names.get(ids[m], ids[m])} 票，车上补至目的地")
    return out
