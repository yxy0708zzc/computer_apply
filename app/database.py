"""
database.py —— 本地数据层（travel2 schema 只读查询）

数据库（复制自 travel2）：
  data/railway.db  stations(station_id 电报码, station_name)
                   trains(train_num, train_no)
                   train_stops(train_num, stop_no, station_id, station_name, stop_time HH:MM)
                   station_trains(station_id, station_name, data JSON)
  data/prices.db   prices(train_num, from_station_id, to_station_id, seat, price 元, crawl_date)
                   —— 历史爬取参考价，仅用于 cheapest 排序估算与兜底展示，实时票价走联网
"""

import json
import os
import sqlite3
from typing import Dict, List, Optional

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAILWAY_DB = os.path.join(_BASE_DIR, "data", "railway.db")
PRICES_DB = os.path.join(_BASE_DIR, "data", "prices.db")
SESSIONS_DIR = os.path.join(_BASE_DIR, "sessions")

# 席别体系：全面采用 12306 余票页事实席别名（每列可能多名共用一个数值，可能为空）
TICKET_TABLES = ["class0", "class1", "class2"]
SEAT_CN_LIST = [
    "商务座", "特等座", "优选一等座", "一等座", "二等座", "二等包座",
    "高级软卧", "软卧", "一等卧", "动卧", "硬卧", "二等卧",
    "软座", "硬座", "无座",
]
# 成员名/组合名 → 主组合名（同值组归一：任一成员查同列数值）
SEAT_CANONICAL = {
    "商务座": "商务座/特等座", "特等座": "商务座/特等座", "商务座/特等座": "商务座/特等座",
    "二等座": "二等座/二等包座", "二等包座": "二等座/二等包座", "二等座/二等包座": "二等座/二等包座",
    "软卧": "软卧/一等卧", "一等卧": "软卧/一等卧", "软卧/一等卧": "软卧/一等卧",
    "硬卧": "硬卧/二等卧", "二等卧": "硬卧/二等卧", "硬卧/二等卧": "硬卧/二等卧",
    "优选一等座": "优选一等座", "高级软卧": "高级软卧", "动卧": "动卧",
    "软座": "软座", "硬座": "硬座", "无座": "无座",
}
# 实际席别名 → prices.db 内部存储档位（仅历史参考价查询用，不对外暴露）
SEAT_CN_TO_CLASS = {
    "二等座/二等包座": "class2", "硬座": "class2", "无座": "class2",
    "一等座": "class1", "软座": "class1", "硬卧/二等卧": "class1", "动卧": "class1",
    "商务座/特等座": "class0", "高级软卧": "class0", "软卧/一等卧": "class0",
}


def get_railway_conn() -> sqlite3.Connection:
    if not os.path.exists(RAILWAY_DB):
        raise FileNotFoundError(f"本地列车库不存在: {RAILWAY_DB}")
    conn = sqlite3.connect(f"file:{RAILWAY_DB}?mode=ro", uri=True)   # 只读打开
    conn.row_factory = sqlite3.Row
    return conn


def get_prices_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{PRICES_DB}?mode=ro", uri=True, timeout=10)
    return conn


# ------------------------------------------------------------
# 基础查询
# ------------------------------------------------------------

def get_train_stops(conn: sqlite3.Connection, train_num: str) -> List[Dict]:
    """车次经停站（stop_no 升序）：[{stop_no, station_id, station_name, stop_time}]"""
    rows = conn.execute(
        "SELECT stop_no, station_id, station_name, stop_time FROM train_stops "
        "WHERE train_num = ? ORDER BY stop_no", (train_num,)).fetchall()
    return [dict(zip(("stop_no", "station_id", "station_name", "stop_time"), tuple(r)))
            for r in rows]


def get_train_no(conn: sqlite3.Connection, train_num: str) -> Optional[str]:
    row = conn.execute("SELECT train_no FROM trains WHERE train_num = ?",
                       (train_num,)).fetchone()
    return row[0] if row else None


def get_routes_between(conn: sqlite3.Connection, from_id: str, to_id: str) -> List[Dict]:
    """同车经过 from→to（顺序正确）的直达车：[{train_num, depart_time, arrive_time, duration}]"""
    rows = conn.execute("""
        SELECT s1.train_num, s1.stop_time, s2.stop_time
        FROM train_stops s1
        JOIN train_stops s2 ON s1.train_num = s2.train_num
        WHERE s1.station_id = ? AND s2.station_id = ? AND s1.stop_no < s2.stop_no
        ORDER BY s1.stop_time
    """, (from_id, to_id)).fetchall()
    return [{"train_num": tn, "depart_time": d, "arrive_time": a,
             "duration": calc_duration(d, a)} for tn, d, a in rows]


def calc_duration(depart: str, arrive: str) -> str:
    """HH:MM 差值；负数循环 +1440（跨天）；解析失败返回 --:--"""
    try:
        h1, m1 = map(int, depart.split(":"))
        h2, m2 = map(int, arrive.split(":"))
        mins = (h2 * 60 + m2) - (h1 * 60 + m1)
        while mins < 0:
            mins += 1440
        return f"{mins // 60:02d}:{mins % 60:02d}"
    except (ValueError, AttributeError):
        return "--:--"


def time_diff_minutes(t1: str, t2: str) -> int:
    """t2 - t1 分钟数（仅当日；t2 早于 t1 返回负值=跨天）"""
    try:
        h1, m1 = map(int, t1.split(":"))
        h2, m2 = map(int, t2.split(":"))
        return (h2 * 60 + m2) - (h1 * 60 + m1)
    except (ValueError, AttributeError):
        return 0


def validate_station_exists(conn: sqlite3.Connection, station_id: str) -> bool:
    return conn.execute("SELECT 1 FROM stations WHERE station_id = ?",
                        (station_id,)).fetchone() is not None


def get_station_name(conn: sqlite3.Connection, station_id: str) -> str:
    row = conn.execute("SELECT station_name FROM stations WHERE station_id = ?",
                       (station_id,)).fetchone()
    return row[0] if row else station_id


def resolve_station_name_or_id(conn: sqlite3.Connection, input_str: str) -> Optional[str]:
    """站名/电报码 → 标准 station_id；解析失败返回 None"""
    s = (input_str or "").strip()
    if not s:
        return None
    if validate_station_exists(conn, s):
        return s
    for name in (s, s.replace("站", "")):
        row = conn.execute("SELECT station_id FROM stations WHERE station_name = ?",
                           (name,)).fetchone()
        if row:
            return row[0]
    for name in (s, s.replace("站", "")):
        row = conn.execute("SELECT station_id FROM stations WHERE station_name LIKE ? LIMIT 1",
                           (f"%{name}%",)).fetchone()
        if row:
            return row[0]
    return None


def search_stations(conn: sqlite3.Connection, keyword: str, limit: int = 20) -> List[Dict]:
    """站名/拼音/电报码模糊搜索（消歧用）：[{station_id, station_name}]"""
    kw = f"%{(keyword or '').strip()}%"
    rows = conn.execute(
        "SELECT station_id, station_name FROM stations "
        "WHERE station_id LIKE ? OR station_name LIKE ? ORDER BY LENGTH(station_name) LIMIT ?",
        (kw, kw, max(1, min(int(limit), 50)))).fetchall()
    return [{"station_id": r[0], "station_name": r[1]} for r in rows]


def get_station_trains(conn: sqlite3.Connection, station_id: str) -> Optional[Dict]:
    """车站经停车次（station_trains 物化视图）"""
    row = conn.execute(
        "SELECT station_id, station_name, data FROM station_trains WHERE station_id = ?",
        (station_id,)).fetchone()
    if row is None:
        return None
    return {"station_id": row[0], "station_name": row[1], "trains": json.loads(row[2])}


# ------------------------------------------------------------
# 历史票价（仅排序估算/兜底）
# ------------------------------------------------------------

def get_price_estimate(from_id: str, to_id: str, seat_cn: str) -> Optional[float]:
    """该区间实际席别的历史参考价（取最低价；无数据返回 None）。
    seat_cn 为 12306 席别名（成员名或组合名均可，自动归一）。prices 表三种存储并存：
    新组合名（collect.price_collector）/ 旧单名（早期版本）/ 旧 class 键（travel2），全兼容。"""
    if not os.path.exists(PRICES_DB):
        return None
    cn = (seat_cn or "").strip()
    canonical = SEAT_CANONICAL.get(cn, cn)
    seat_old = SEAT_CN_TO_CLASS.get(canonical)
    if not canonical and not seat_old:
        return None
    names = [canonical]
    for member, c in SEAT_CANONICAL.items():
        if c == canonical and "/" not in member:
            names.append(member)                     # 同组单成员名（早期单名存储）
    if seat_old:
        names.append(seat_old)
    conn = get_prices_conn()
    try:
        ph = ",".join("?" * len(names))
        row = conn.execute(
            f"SELECT MIN(price) FROM prices WHERE from_station_id = ? AND to_station_id = ? "
            f"AND seat IN ({ph})", [from_id, to_id] + names).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
