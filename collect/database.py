# -*- coding: utf-8 -*-
"""
collect/database.py —— tripai schema 建表与读写连接（采集器专用，写模式）

表结构与 app/database.py 的只读查询完全一致（travel2 口径）：
    stations(station_id TEXT PK, station_name TEXT NOT NULL)
    trains(train_num TEXT PK, train_no TEXT UNIQUE NOT NULL)
    train_stops(id PK, train_num, stop_no, station_id, station_name, stop_time,
                UNIQUE(train_num, stop_no))
    station_trains(station_id TEXT PK, station_name, data TEXT JSON)
"""

import json
import os
import sqlite3
from typing import Dict, List

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_BASE_DIR, "data", "railway.db")
PRICES_DB = os.path.join(_BASE_DIR, "data", "prices.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    station_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trains (
    train_num TEXT PRIMARY KEY,
    train_no TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS train_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    train_num TEXT NOT NULL,
    stop_no INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    station_name TEXT NOT NULL,
    stop_time TEXT NOT NULL,
    FOREIGN KEY (train_num) REFERENCES trains(train_num) ON UPDATE CASCADE,
    FOREIGN KEY (station_id) REFERENCES stations(station_id) ON UPDATE CASCADE,
    UNIQUE(train_num, stop_no)
);
CREATE INDEX IF NOT EXISTS idx_train_stops_train_num ON train_stops(train_num);
CREATE INDEX IF NOT EXISTS idx_train_stops_station_id ON train_stops(station_id);
CREATE TABLE IF NOT EXISTS station_trains (
    station_id TEXT PRIMARY KEY,
    station_name TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    train_num TEXT NOT NULL,
    from_station_id TEXT NOT NULL,
    to_station_id TEXT NOT NULL,
    seat TEXT NOT NULL,
    price REAL NOT NULL,
    crawl_date TEXT NOT NULL,
    UNIQUE(train_num, from_station_id, to_station_id, seat)
);
CREATE INDEX IF NOT EXISTS idx_prices_train_num ON prices(train_num);
CREATE INDEX IF NOT EXISTS idx_prices_station_pair ON prices(from_station_id, to_station_id);
"""


def get_conn(readonly: bool = False) -> sqlite3.Connection:
    """列车库连接（读写采集器用；readonly=True 时只读打开）"""
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def get_prices_conn() -> sqlite3.Connection:
    """票价库连接（读写；表结构与 app 侧一致）"""
    conn = sqlite3.connect(PRICES_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表（已存在则跳过；不删除任何数据）"""
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def refresh_station_trains() -> int:
    """从 train_stops 重建 station_trains 物化视图（采集完成后调用）。
    返回车站数。"""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT ts.station_id, ts.station_name, ts.train_num, ts.stop_time
            FROM train_stops ts ORDER BY ts.train_num
        """).fetchall()
        grouped: Dict[str, Dict] = {}
        for station_id, station_name, train_num, stop_time in rows:
            g = grouped.setdefault(station_id, {"station_name": station_name, "trains": []})
            g["trains"].append({"train_num": train_num, "stop_time": stop_time})
        conn.execute("DELETE FROM station_trains")
        for station_id, data in grouped.items():
            conn.execute(
                "INSERT OR REPLACE INTO station_trains (station_id, station_name, data) "
                "VALUES (?, ?, ?)",
                (station_id, data["station_name"],
                 json.dumps(data["trains"], ensure_ascii=False)))
        conn.commit()
        return len(grouped)
    finally:
        conn.close()


def stats() -> Dict[str, int]:
    """库规模统计"""
    conn = get_conn(readonly=True)
    try:
        return {
            "stations": conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0],
            "trains": conn.execute("SELECT COUNT(*) FROM trains").fetchone()[0],
            "train_stops": conn.execute("SELECT COUNT(*) FROM train_stops").fetchone()[0],
            "station_trains": conn.execute("SELECT COUNT(*) FROM station_trains").fetchone()[0],
        }
    finally:
        conn.close()
