# -*- coding: utf-8 -*-
"""
collect/stations.py —— 12306 全国车站表采集（借鉴 railway/stations.py）

数据源：https://kyfw.12306.cn/otn/resources/js/framework/station_name.js
解析：var station_names='@bjb|北京北|VAP|beijingbei|bjb|0|...@...' 按 @ 分条、| 切列：
      简拼|站名|电报码|全拼|首字母|序号
入库：stations(station_id=电报码, station_name=站名)

用法：.venv/Scripts/python.exe -m collect.stations
"""

import os
import re
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import DB_PATH, get_conn, init_db   # noqa: E402

STATION_JS_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
}


def fetch_station_js() -> str:
    """下载 station_name.js（先访问 init 页拿 Cookie）"""
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        sess.get(INIT_URL, timeout=15)
    except Exception:
        pass
    r = sess.get(STATION_JS_URL, timeout=20)
    r.raise_for_status()
    return r.text


def parse_station_js(text: str):
    """解析出 [(电报码, 站名), ...]"""
    m = re.search(r"var station_names\s*=\s*'([^']+)'", text)
    if not m:
        return []
    out = []
    for entry in m.group(1).split("@"):
        parts = entry.split("|")
        # 简拼|站名|电报码|全拼|首字母|序号
        if len(parts) >= 3 and parts[1] and parts[2]:
            out.append((parts[2], parts[1]))
    return out


def main():
    init_db()
    print("[车站] 下载 12306 station_name.js ...")
    text = fetch_station_js()
    stations = parse_station_js(text)
    if not stations:
        print("[车站] 解析失败（响应中无 station_names 数据）")
        sys.exit(1)
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO stations (station_id, station_name) VALUES (?, ?)",
            stations)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    finally:
        conn.close()
    print(f"[车站] 解析 {len(stations)} 条，入库完成，stations 表共 {n} 条")
    print(f"[车站] 数据库: {DB_PATH}")


if __name__ == "__main__":
    main()
