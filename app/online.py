"""
online.py —— 12306 联网层（移植 railway/self/server.py 反爬体系 + 票价接口）

余票：kyfw.12306.cn/otn/leftTicket/queryZ（railway 全套成熟机制）
  - 全局唯一 Session + 全局串行锁（任何时刻仅一个在途请求，防风控）
  - UA 池随机 + init 页取 Cookie + 反爬识别（403/429/关键词/HTML 错误页）
  - MAX_QUERY_ATTEMPTS=10 + 指数退避 + 主动轮换会话（每 60 次成功）
票价：kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice（benchmark_travelplan 移植）
  - 角→元 ÷10；G/D 取 ze/zy/swz，普速取 yz/yw/rw（缺失兜底再探高铁字段）
"""

import json
import logging
import os
import random
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import requests

from . import database as db

log = logging.getLogger("tripai.online")

API = {
    "home": "https://kyfw.12306.cn/otn/",
    "init": "https://kyfw.12306.cn/otn/leftTicket/init",
    "query_ticket": "https://kyfw.12306.cn/otn/leftTicket/queryZ",
    "query_price": "https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice",
    "station_name": "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js",
}

HEADERS = {
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# 全局串行（railway 结论：线程本地 Session 反复重建 Cookie 更易触发风控）
_crawl_lock = threading.Lock()
_sess: Optional[requests.Session] = None
_sess_lock = threading.Lock()

PROACTIVE_ROTATE_EVERY = 60
MAX_QUERY_ATTEMPTS = 10
_query_count = 0

STRATEGY_INTERVALS = {
    "fast": (0.2, 0.5),
    "stable": (1.2, 2.5),
    "conservative": (3.0, 6.0),
}
DEFAULT_STRATEGY = "stable"

ANTICRAWL_KEYWORDS = (
    "验证码", "captcha", "CAPTCHA", "访问太频繁", "请求太频繁",
    "访问被拒绝", "Access Denied", "Too Many Requests", "触发风控",
)

# queryZ 席别字段位 → 页面事实席别名（12306 余票页每列可能多名共用一个数值，可能为空）
# 实测字段位：f[20]=优选一等座（智能动车组，G3 有值）、f[32]/31/30、f[21]/23/33、f[28]/24/29/26
SEAT_FIELDS = [
    (32, "商务座/特等座"), (20, "优选一等座"), (31, "一等座"),
    (30, "二等座/二等包座"),
    (21, "高级软卧"), (23, "软卧/一等卧"), (33, "动卧"),
    (28, "硬卧/二等卧"), (24, "软座"), (29, "硬座"), (26, "无座"),
]
# 同值组：组名 → 组内成员名（任一成员均可命中该列的数值）
SEAT_GROUPS = {
    "商务座/特等座": ("商务座", "特等座", "商务座/特等座"),
    "优选一等座": ("优选一等座",),
    "一等座": ("一等座",),
    "二等座/二等包座": ("二等座", "二等包座", "二等座/二等包座"),
    "高级软卧": ("高级软卧",),
    "软卧/一等卧": ("软卧", "一等卧", "软卧/一等卧"),
    "动卧": ("动卧",),
    "硬卧/二等卧": ("硬卧", "二等卧", "硬卧/二等卧"),
    "软座": ("软座",),
    "硬座": ("硬座",),
    "无座": ("无座",),
}
# 票价存储/查询用主名（成员名 → 组名主词）
SEAT_CANONICAL = {
    "特等座": "商务座/特等座", "商务座": "商务座/特等座",
    "二等包座": "二等座/二等包座", "二等座": "二等座/二等包座",
    "一等卧": "软卧/一等卧", "软卧": "软卧/一等卧",
    "二等卧": "硬卧/二等卧", "硬卧": "硬卧/二等卧",
    "优选一等座": "优选一等座", "高级软卧": "高级软卧", "动卧": "动卧",
    "软座": "软座", "硬座": "硬座", "无座": "无座",
}


def _match_seat(seats: Dict[str, str], seat_type: str):
    """同值组席别匹配：指定席别名 → all_seats 中的事实键与数值。
    例：指定"特等座"可命中键"商务座/特等座"。返回 (实际键, 值) 或 (None, None)。"""
    if seat_type in seats:
        return seat_type, seats[seat_type]
    for key, val in seats.items():
        if seat_type in SEAT_GROUPS.get(key, (key,)):
            return key, val
    for key, val in seats.items():          # 反向：指定名是某键的主成员
        if key in SEAT_GROUPS.get(seat_type, ()):            # noqa
            return key, val
    return None, None


def _seat_count(raw) -> int:
    """余票原始值 → 可购数（"有"/"充足"≈99，数字直取，其余 0）"""
    if raw in ("有", "充足"):
        return 99
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _match_any_seat(seats: Dict[str, str]):
    """不限席别：按档次高→低返回第一个可购席（余票>0 或"有"）；
    全部无票时返回（第一个存在席, 原值）供展示 no_seat；空字典返回 (None, None)。"""
    fallback = (None, None)
    for _, name in SEAT_FIELDS:
        raw = seats.get(name)
        if raw is None:
            continue
        if _seat_count(raw) > 0:
            return name, raw
        if fallback[0] is None:
            fallback = (name, raw)
    return fallback
# 票价接口字段 → 实际席别名（按车型取对应组）
PRICE_FIELDS_GD = (("ze_price", "二等座"), ("zy_price", "一等座"), ("swz_price", "商务座"))
PRICE_FIELDS_PUSU = (("yz_price", "硬座"), ("yw_price", "硬卧"), ("rw_price", "软卧"))

# JSONL 爬取日志（反爬排查）
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_log_lock = threading.Lock()


def _write_log(entry: dict):
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        fn = os.path.join(_LOG_DIR, f"ticket_{entry.get('date', '')[:10] or 'na'}.log")
        with _log_lock, open(fn, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------
# 会话管理
# ------------------------------------------------------------

def get_session() -> requests.Session:
    global _sess
    with _sess_lock:
        if _sess is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            s.headers["User-Agent"] = random.choice(USER_AGENTS)
            s.get(API["home"], timeout=15)
            s.get(API["init"], timeout=15)
            _sess = s
        return _sess


def reset_session():
    global _sess
    with _sess_lock:
        _sess = None


def _is_anticrawl(r: requests.Response) -> bool:
    """HTTP 403/429，或短响应体含反爬关键词（HTML 错误页落入 JSON 解析异常分支）"""
    if r is None:
        return False
    if r.status_code in (403, 429):
        return True
    if r.status_code == 200 and len(r.text) < 1000:
        body = r.text.lower()
        return any(kw.lower() in body for kw in ANTICRAWL_KEYWORDS)
    return False


def _wait_before_next(attempt: int) -> float:
    return min(2 ** attempt, 8) + random.uniform(1, 3)


def _sleep_interval(strategy: str = DEFAULT_STRATEGY):
    lo, hi = STRATEGY_INTERVALS.get(strategy, STRATEGY_INTERVALS[DEFAULT_STRATEGY])
    time.sleep(random.uniform(lo, hi))


# ------------------------------------------------------------
# 余票（queryZ）
# ------------------------------------------------------------

def query_tickets(date: str, from_code: str, to_code: str) -> Tuple[List[Dict], Optional[str]]:
    """查询某日某区间全部车次余票。返回 (trains, error)。
    trains: [{code, train_no, from_time, to_time, duration, seats{中文名: 状态}}]"""
    with _crawl_lock:
        return _query_tickets_locked(date, from_code, to_code)


def _query_tickets_locked(date, from_code, to_code):
    attempt = 0
    global _query_count
    while True:
        attempt += 1
        if attempt > MAX_QUERY_ATTEMPTS:
            _write_log({"date": date, "from": from_code, "to": to_code,
                        "status": "giveup", "time": time.strftime("%H:%M:%S")})
            return [], f"联网查询失败（已尝试 {MAX_QUERY_ATTEMPTS} 次，可能触发反爬）"
        sess = get_session()
        try:
            r = sess.get(API["query_ticket"], params={
                "leftTicketDTO.train_date": date,
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                "purpose_codes": "ADULT",
            }, timeout=20)
        except Exception as e:
            reset_session()
            time.sleep(_wait_before_next(attempt))
            continue

        if _is_anticrawl(r):
            reset_session()
            time.sleep(min(0.8 * attempt, 5) + random.uniform(0.5, 1.5))
            continue

        if not r.text:
            reset_session()
            time.sleep(_wait_before_next(attempt))
            continue

        try:
            data = r.json()
        except Exception:
            reset_session()
            _write_log({"date": date, "from": from_code, "to": to_code, "status": "html",
                        "preview": r.text[:120], "time": time.strftime("%H:%M:%S")})
            time.sleep(_wait_before_next(attempt))
            continue

        if not data.get("status"):
            reset_session()
            time.sleep(_wait_before_next(attempt))
            continue

        results = (data.get("data") or {}).get("result", [])
        if not results:
            # 无车次数据（status=true）——非失败，正常返回空
            _query_count += 1
            if _query_count >= PROACTIVE_ROTATE_EVERY:
                _query_count = 0
                reset_session()
            return [], None

        break

    _query_count += 1
    if _query_count >= PROACTIVE_ROTATE_EVERY:
        _query_count = 0
        reset_session()

    trains = []
    for res in results:
        f = res.split("|")
        if len(f) < 35:
            continue
        seats = {}
        for idx, cn in SEAT_FIELDS:
            if idx < len(f) and f[idx]:
                seats[cn] = f[idx]
        trains.append({
            "code": f[3], "train_no": f[2],
            "from_time": f[8], "to_time": f[9], "duration": f[10],
            "seats": seats,
        })
    _write_log({"date": date, "from": from_code, "to": to_code, "status": "ok",
                "trains": len(trains), "time": time.strftime("%H:%M:%S")})
    return trains, None


def match_train(trains: List[Dict], train_num: str, train_no: Optional[str]) -> Optional[Dict]:
    """在 queryZ 结果中匹配方案车次：显示码相同 或 train_no 相同（改号车兜底）"""
    for t in trains:
        if t["code"].upper() == train_num.upper():
            return t
        if train_no and t.get("train_no") and t["train_no"] == train_no:
            return t
    return None


# ------------------------------------------------------------
# 票价（queryAllPublicPrice）
# ------------------------------------------------------------

def query_prices(date: str, from_code: str, to_code: str,
                 want: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, float]]:
    """查询某日某区间各车次票价。返回 {显示码: {席别名: 元}}。
    want: {train_num: train_no} 期望车次表——同向改号车在区间内可能以别名码出现，
    DTO 的 train_no 与期望一致时归一到期望显示码下（railway match_train 同源思路）。"""
    sess = get_session()
    params = {
        "leftTicketDTO.train_date": date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    headers = {
        "Referer": "https://kyfw.12306.cn/otn/view/queryPublicIndex.html",
        "X-Requested-With": "XMLHttpRequest",
    }
    for attempt in range(1, 4):
        try:
            r = sess.get(API["query_price"], params=params, headers=headers, timeout=20)
            if _is_anticrawl(r):
                log.warning("[price] 第%d次疑似反爬 HTTP %s", attempt, r.status_code)
                reset_session()
                sess = get_session()
                time.sleep(min(0.8 * attempt, 5) + random.uniform(0.5, 1.5))
                continue
            try:
                data = r.json()
            except Exception:
                log.warning("[price] 第%d次非JSON响应 len=%s 预览=%s",
                            attempt, len(r.text), r.text[:120].replace("\n", " "))
                reset_session()
                sess = get_session()
                time.sleep(1 + attempt)
                continue
            if data.get("status") is not True:
                log.warning("[price] 第%d次 status=%s", attempt, data.get("status"))
                time.sleep(1 + attempt)
                continue
            out: Dict[str, Dict[str, float]] = {}
            dto_by_tno: Dict[str, Dict[str, float]] = {}
            for item in data.get("data") or []:
                dto = item.get("queryLeftNewDTO") or {}
                code = dto.get("station_train_code", "")
                if not code:
                    continue
                cls = dto.get("train_class_name", "") or ""
                mapping = PRICE_FIELDS_GD if cls in ("高速", "动车") else PRICE_FIELDS_PUSU
                prices = {}
                for key, seat_cn in mapping:
                    val = dto.get(key)
                    if val not in (None, ""):
                        try:
                            prices[seat_cn] = round(float(val) / 10.0, 1)   # 角 → 元
                        except (ValueError, TypeError):
                            pass
                if not prices and cls in ("高速", "动车", ""):
                    # 兜底：车型字段缺失/取值变化时按高铁字段再探一次
                    for key, seat_cn in PRICE_FIELDS_GD:
                        val = dto.get(key)
                        if val not in (None, ""):
                            try:
                                prices[seat_cn] = round(float(val) / 10.0, 1)
                            except (ValueError, TypeError):
                                pass
                if prices:
                    out[code] = prices
                    tno = dto.get("train_no", "")
                    if tno:
                        dto_by_tno[tno] = prices
            # 改号车归一：期望显示码不在列表、但 DTO train_no 命中期望 train_no → 归一
            if want:
                for want_num, want_tno in want.items():
                    if want_num not in out and want_tno and want_tno in dto_by_tno:
                        out[want_num] = dto_by_tno[want_tno]
            return out
        except Exception:
            log.warning("[price] 第%d次异常: %s", attempt, traceback.format_exc(limit=2))
            reset_session()
            sess = get_session()
            time.sleep(1 + attempt)
    return {}


# ------------------------------------------------------------
# 方案核实（区间合并查询）
# ------------------------------------------------------------

def _load_station_names() -> Dict[str, str]:
    """电报码 → 站名全表（约 3400 条，进程缓存）"""
    global _NAME_CACHE
    if _NAME_CACHE is None:
        conn = db.get_railway_conn()
        try:
            _NAME_CACHE = {r[0]: r[1] for r in conn.execute("SELECT station_id, station_name FROM stations")}
        finally:
            conn.close()
    return _NAME_CACHE


_NAME_CACHE: Optional[Dict[str, str]] = None


def default_date() -> str:
    """缺省查询日期：预售习惯取今天+3（railway 同款）"""
    return (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")


def normalize_date(d: Optional[str]) -> str:
    """校验/规范化日期：支持 YYYY-MM-DD / YYYYMMDD；空则默认今天+3；
    超出 [今天, 今天+14] 抛 ValueError"""
    if not d:
        return default_date()
    d = d.strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", d):
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError(f"日期格式不合法: {d}（需 YYYY-MM-DD）")
    today = datetime.now().date()
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"日期不存在: {d}")
    if dt < today or dt > today + timedelta(days=14):
        raise ValueError(f"日期需在今天~14 天预售期内: {d}")
    return d


def _pair_key(from_id: str, to_id: str) -> Tuple[str, str]:
    return (from_id, to_id)


def check_solutions(solutions: List[Dict], date: str, seat_type: str = "class2",
                    people_count: int = 1,
                    on_progress: Optional[Callable[[int, int, str], None]] = None,
                    strategy: str = DEFAULT_STRATEGY) -> Dict:
    """对给定方案列表联网核实余票+票价（按购买区间合并请求，反爬友好）。

    回填每个 solution：
      checked=True, check_date=date,
      segments[i] += {tickets(数字或'有'/None), price, ticket_status: ok/few/none/unknown}
      can_buy: bool（全部段目标席别有票且 >= people_count）
      check_note: 摘要
    on_progress(current, total, message) 用于向前端推送进度。
    返回 {"results": [...], "pairs_checked": n, "error": ...}
    """
    date = normalize_date(date)
    results = [dict(s) for s in solutions]

    # 1) 收集去重的购买区间
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for s in results:
        for seg in s.get("segments", []):
            k = _pair_key(seg["from_station_id"], seg["to_station_id"])
            if k not in seen:
                seen.add(k)
                pairs.append(k)
    total = len(pairs)
    done = 0
    if on_progress:
        on_progress(0, total, f"待核实 {total} 个区间")

    errors = []
    names = _load_station_names()
    # 每区间的期望车次表（改号车归一：DTO train_no 命中期望即归到期望显示码下）
    want_by_pair: Dict[Tuple[str, str], Dict[str, str]] = {}
    for s in results:
        for seg in s.get("segments", []):
            k = _pair_key(seg["from_station_id"], seg["to_station_id"])
            want_by_pair.setdefault(k, {})[seg["train_num"]] = seg.get("train_no") or ""
    # 逐区间：余票 + 票价（串行+限速，railway 反爬纪律）
    tickets_by_pair: Dict[Tuple[str, str], List[Dict]] = {}
    prices_by_pair: Dict[Tuple[str, str], Dict[str, Dict[str, float]]] = {}
    for i, (fid, tid) in enumerate(pairs):
        if on_progress:
            on_progress(done, total,
                        f"核实 {names.get(fid, fid)}→{names.get(tid, tid)}（{i + 1}/{total}）")
        trains, err = query_tickets(date, fid, tid)
        if err:
            errors.append(err)
        tickets_by_pair[(fid, tid)] = trains
        _sleep_interval(strategy)
        prices_by_pair[(fid, tid)] = query_prices(date, fid, tid,
                                                  want=want_by_pair.get((fid, tid)))
        _sleep_interval(strategy)
        done += 1
        if on_progress:
            on_progress(done, total, f"已完成 {done}/{total}")

    # 3) 回填方案
    for s in results:
        all_ok = True
        notes = []
        for seg in s.get("segments", []):
            pair = (seg["from_station_id"], seg["to_station_id"])
            trains = tickets_by_pair.get(pair, [])
            prices = prices_by_pair.get(pair, {})
            t = match_train(trains, seg["train_num"], seg.get("train_no"))
            seg["ticket_status"] = "unknown"
            seg["tickets"] = None
            seg["price"] = None
            if t is None:
                seg["ticket_status"] = "none"     # 该区间 12306 无此车（可能无票停售/不经此区间）
                all_ok = False
                notes.append(f"{seg['train_num']} 未查到")
                continue
            seats = t.get("seats", {})
            seg["all_seats"] = seats   # 该车全部实际席位（同值组名，可能为空=该车无此席）
            # 主判定席别："不限"取任一可购席；否则同值组匹配（指定"特等座"可命中"商务座/特等座"列）
            if seat_type == "不限":
                used_cn, raw = _match_any_seat(seats)
            else:
                used_cn, raw = _match_seat(seats, seat_type)
            seg["seat_cn"] = used_cn
            if raw is None:
                seg["ticket_status"] = "no_seat" if seats else "unknown"
                all_ok = False
                if not seats:
                    notes.append(f"{seg['train_num']} 余票字段缺失")
                else:
                    notes.append(f"{seg['train_num']} 无{seat_type}席（实际有：{','.join(seats.keys())}）")
                continue
            seg["tickets_raw"] = raw
            if raw in ("有", "充足"):
                seg["tickets"] = 99
                seg["ticket_status"] = "ok"
            elif raw in ("候补", "无", "--"):
                seg["tickets"] = 0
                seg["ticket_status"] = "none"
                all_ok = False
            else:
                try:
                    n = int(raw)
                    seg["tickets"] = n
                    seg["ticket_status"] = "ok" if n >= people_count else "few"
                    if n < people_count:
                        all_ok = False
                except ValueError:
                    seg["tickets"] = None
                    seg["ticket_status"] = "unknown"
            # 票价：显示码匹配；席别归一到实际命中席的存储主名
            p = prices.get(t["code"])
            seg["price"] = p.get(SEAT_CANONICAL.get(used_cn, used_cn)) if p and used_cn else None

        s["checked"] = True
        s["check_date"] = date
        s["can_buy"] = all_ok
        s["check_note"] = "；".join(notes) if notes else \
            ("全部区间有票" if all_ok else "部分区间票不足")
        if on_progress:
            on_progress(total, total, "核实完成")

    return {"results": results, "pairs_checked": done, "date": date,
            "errors": errors[:3]}
