# -*- coding: utf-8 -*-
"""
collect/collector.py —— 12306 车次发现 + 经停采集（借鉴 railway/collector.py）

发现（search.12306.cn/search/v1/train/search）：
    对 K/G/C/T/Z/D 每个字母：两位数前缀 {L}10~{L}99 逐条查询（返回 <200 即完整，
    达上限自动扩展三位数子前缀），个位 {L}1~{L}9 补充精确车次；纯数字车次同理。
经停（kyfw.12306.cn/otn/czxx/queryByTrainNo）：
    按 train_no 查全线经停；普速(K/T/Z)发车时刻在 start_time、高速(G/D/C)在
    depart_time，此处兼容；入库取发车时刻为 stop_time（tripai schema）。

入库（tripai schema）：
    trains(train_num=显示码, train_no)；同一 train_no 的后续改号码跳过（UNIQUE 约束）
    train_stops(train_num, stop_no, station_id, station_name, stop_time)
    已有经停的车次默认跳过（断点续爬）；完成后自动重建 station_trains。

用法：
    .venv/Scripts/python.exe -m collect.collector                       # 全字头 K G C T Z D + 数字
    .venv/Scripts/python.exe -m collect.collector --letters G D K       # 指定字头
    .venv/Scripts/python.exe -m collect.collector --limit 20            # 小规模实测
    .venv/Scripts/python.exe -m collect.collector --stats               # 查看库规模
"""

import argparse
import concurrent.futures
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import get_conn, init_db, refresh_station_trains, stats   # noqa: E402

# ============================================================
# 配置（借鉴 railway 建库阶段参数：无重反爬、短间隔、增强重试）
# ============================================================
CONFIG = {
    "query_date": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
    # 经停查询候选日期：车次非每日开行，某日无数据则逐日重试。
    # 今天/明天命中率最高（当前运行图有效），再加 +3/+9/+13 覆盖非每日车。
    "query_dates": [(datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in (0, 1, 2, 3, 9, 13)],
    "min_interval": (0.05, 0.15),    # 每次请求随机抖动延时范围（秒），顺利时短间隔
    "request_timeout": 15,
    "max_retries": 10,
    "search_retries": 10,
    "retry_sleep": (0.5, 1.5),
    "block_base": 3,                 # 限流退避基数（秒），指数增长（原 6 减半）
    "block_cap": 30,                 # 限流退避上限（秒）（原 60 减半）
    "letter_retries": 3,
    "collect_workers": 10,
}

TRAIN_LETTERS = ("K", "G", "C", "T", "Z", "D")
DISCOVER_DIGIT_TRAINS = True
MAX_RESULTS = 200

API = {
    "init": "https://kyfw.12306.cn/otn/leftTicket/init",
    "search": "https://search.12306.cn/search/v1/train/search",
    "query_route": "https://kyfw.12306.cn/otn/czxx/queryByTrainNo",
}

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

_sess_local = threading.local()
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _norm_time(v) -> str:
    """只保留有效 HH:MM；占位符(----)/空返回空串"""
    if v and _TIME_RE.match(str(v).strip()):
        return str(v).strip()
    return ""


class Collector12306:
    def __init__(self):
        self.stats = {"queried": 0, "success": 0, "failed": 0}
        self._stats_lock = threading.Lock()
        self._name_to_code: Optional[Dict[str, str]] = None

    def _inc(self, key: str, n: int = 1):
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    # ---------- 限速 / 请求 ----------
    def _session(self) -> requests.Session:
        sess = getattr(_sess_local, "sess", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(BASE_HEADERS)
            _sess_local.sess = sess
            try:
                sess.get(API["init"], timeout=CONFIG["request_timeout"])
            except Exception:
                pass
        return sess

    def _reset_session(self):
        _sess_local.sess = None
        try:
            self._session()
        except Exception:
            pass

    def _rate_limit(self):
        last = getattr(_sess_local, "last_time", 0.0)
        low, high = CONFIG["min_interval"]
        delay = random.uniform(low, high)
        if time.time() - last < delay:
            time.sleep(delay - (time.time() - last))
        _sess_local.last_time = time.time()

    def _block_wait(self, attempt: int) -> float:
        return min(CONFIG["block_base"] * (2 ** (attempt - 1)), CONFIG["block_cap"]) \
            + random.uniform(1, 3)

    def _request(self, url: str, params: dict, retries: Optional[int] = None) -> Optional[dict]:
        max_attempts = retries if retries is not None else CONFIG["max_retries"]
        for attempt in range(1, max_attempts + 1):
            self._rate_limit()
            self._inc("queried")
            try:
                resp = self._session().get(url, params=params,
                                           timeout=CONFIG["request_timeout"])
                if resp.status_code != 200 or \
                        ("json" not in resp.headers.get("Content-Type", "")
                         and "javascript" not in resp.headers.get("Content-Type", "")):
                    if attempt < max_attempts:
                        self._reset_session()
                        wait = self._block_wait(attempt)
                        print(f"  ⚠ 疑似限流 (HTTP {resp.status_code})，重建会话 "
                              f"{wait:.0f}s 后重试 ({attempt}/{max_attempts})")
                        time.sleep(wait)
                        continue
                    self._inc("failed")
                    return None
                data = resp.json()
                if data.get("status") is True:
                    self._inc("success")
                    return data
                if attempt < max_attempts:
                    low, high = CONFIG["retry_sleep"]
                    time.sleep(random.uniform(low, high) * attempt)
                    continue
                self._inc("failed")
                return None
            except Exception:
                if attempt < max_attempts:
                    self._reset_session()
                    time.sleep(self._block_wait(attempt))
                    continue
                self._inc("failed")
                return None
        return None

    # ---------- 车次发现 ----------
    def _search_api(self, keyword: str) -> List[dict]:
        params = {"keyword": keyword, "date": datetime.now().strftime("%Y%m%d")}
        data = self._request(API["search"], params, retries=CONFIG["search_retries"])
        if not data:
            return []
        return data.get("data", []) or []

    def _discover_prefix(self, letter: str, prefix: str, found: Dict[str, str]):
        keyword = letter + prefix
        items = self._search_api(keyword)
        if len(items) >= MAX_RESULTS:
            for it in items:
                code = it.get("station_train_code", "")
                if code == keyword and it.get("train_no"):
                    found[code] = it["train_no"]
            for digit in "0123456789":
                self._discover_prefix(letter, prefix + digit, found)
        else:
            for it in items:
                code = it.get("station_train_code", "")
                if code and it.get("train_no"):
                    if not letter and not code[0].isalpha():
                        found[code] = it["train_no"]
                    elif letter:
                        found[code] = it["train_no"]

    def _search_exact(self, code: str) -> Optional[dict]:
        for it in self._search_api(code):
            if it.get("station_train_code") == code and it.get("train_no"):
                return it
        return None

    def discover_trains(self, letters=TRAIN_LETTERS, digit: bool = True) -> Dict[str, str]:
        found: Dict[str, str] = {}
        self._milestone = 0            # 每满 100 车次输出一条进度

        def _tick(tag: str):
            if len(found) - self._milestone >= 100:
                print(f"  [发现] {tag} 累计 {len(found)}")
                self._milestone = len(found)

        def run_group(tag: str, keywords_fn):
            retry = 0
            while True:
                before, fail_before = len(found), self.stats["failed"]
                keywords_fn()
                count = len(found) - before
                had_fail = self.stats["failed"] > fail_before
                if count == 0 and had_fail and retry < CONFIG["letter_retries"]:
                    retry += 1
                    wait = self._block_wait(retry)
                    print(f"  ⚠ {tag} 发现可能被限流，{wait:.0f}s 后整组重试 "
                          f"({retry}/{CONFIG['letter_retries']})")
                    time.sleep(wait)
                    continue
                break
            print(f"  [发现] {tag}: 新增 {count}，累计 {len(found)}")

        if digit and "" in letters:
            def digit_fn():
                for tens in range(1, 10):
                    for ones in range(0, 10):
                        self._discover_prefix("", f"{tens}{ones}", found)
                        _tick("数字")
                for d in range(1, 10):
                    exact = self._search_exact(str(d))
                    if exact:
                        found[exact["station_train_code"]] = exact["train_no"]
            run_group("数字车次", digit_fn)

        for letter in [x for x in letters if x]:
            def letter_fn(letter=letter):
                for tens in range(1, 10):
                    for ones in range(0, 10):
                        self._discover_prefix(letter, f"{tens}{ones}", found)
                        _tick(letter)
                for d in range(1, 10):
                    exact = self._search_exact(letter + str(d))
                    if exact:
                        found[exact["station_train_code"]] = exact["train_no"]
            run_group(f"{letter} 车次", letter_fn)

        return found

    # ---------- 经停采集 ----------
    def _get_station_map(self) -> Dict[str, str]:
        if self._name_to_code is None:
            conn = get_conn(readonly=True)
            try:
                self._name_to_code = {
                    r["station_name"]: r["station_id"]
                    for r in conn.execute("SELECT station_id, station_name FROM stations")
                }
            finally:
                conn.close()
        return self._name_to_code

    def query_route_stations(self, train_no: str, date: str) -> List[dict]:
        """按 train_no 查全线经停；出发时刻兼容 depart_time（高速）与 start_time（普速）。
        候选日期逐日重试（+3/+9/+13）：车次当日不开行时返回空。"""
        params = {"train_no": train_no, "from_station_telecode": "BJP",
                  "to_station_telecode": "SHH"}
        for date in dict.fromkeys([date] + CONFIG["query_dates"]):   # 去重保序
            params["depart_date"] = date
            data = self._request(API["query_route"], params)
            if not data:
                continue
            raw = data.get("data")
            if not isinstance(raw, dict):
                continue
            items = raw.get("data", []) or []
            if not items:
                continue          # 该日期不开行 → 试下一个候选日期
            name_to_code = self._get_station_map()
            stops = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                station_name = item.get("station_name", "")
                depart_time = _norm_time(item.get("depart_time")) or _norm_time(item.get("start_time"))
                stops.append({
                    "station_no": item.get("station_no", 0),
                    "station_id": name_to_code.get(station_name, ""),
                    "station_name": station_name,
                    "stop_time": depart_time,
                })
            return stops
        return []


# ============================================================
# 入库（tripai schema）
# ============================================================

def save_discovered(conn, discovered: Dict[str, str]) -> tuple:
    """写入 trains 表；同 train_no 的后续改号码跳过（train_no UNIQUE）。
    返回 (待采集 [(code, train_no)], 冲突跳过数)"""
    known = {r["train_num"]: r["train_no"]
             for r in conn.execute("SELECT train_num, train_no FROM trains")}
    taken = set(known.values())          # 已被占用的 train_no
    pending, seen, conflicts = [], set(), 0
    for code, train_no in discovered.items():
        if known.get(code) == train_no:
            pass                          # 已知且一致
        elif code in known:
            continue                      # 该码已登记过别的 train_no（跨天改号），跳过
        elif train_no in taken:
            conflicts += 1                # 同 train_no 改号车：tripai schema 只留一码
            continue
        else:
            conn.execute("INSERT OR REPLACE INTO trains (train_num, train_no) VALUES (?, ?)",
                         (code, train_no))
            known[code] = train_no
            taken.add(train_no)
        if train_no in seen:
            continue
        seen.add(train_no)
        pending.append((code, train_no))
    conn.commit()
    return pending, conflicts


def collect_stops(collector: Collector12306, pending: List, limit: Optional[int]):
    """并发采集经停并入库（tripai schema：train_num + stop_no + stop_time）"""
    if limit:
        pending = pending[:limit]
    total = len(pending)
    print(f"  待采集 {total} 个车次的经停")
    if total <= 5:
        print(f"  待采清单: {[c for c, _ in pending]}")
    if not total:
        return
    collector._get_station_map()      # 主线程预热站名映射
    collected = no_stops = done = 0
    no_stop_codes = []
    lock = threading.Lock()

    conn = get_conn()
    try:
        def _work(item):
            code, tno = item
            return item, collector.query_route_stations(tno, CONFIG["query_date"])

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=CONFIG["collect_workers"]) as ex:
            for future in concurrent.futures.as_completed(
                    [ex.submit(_work, it) for it in pending]):
                (code, tno), stops = future.result()
                with lock:
                    if stops:
                        conn.execute("DELETE FROM train_stops WHERE train_num = ?", (code,))
                        for s in stops:
                            conn.execute(
                                "INSERT OR REPLACE INTO train_stops "
                                "(train_num, stop_no, station_id, station_name, stop_time) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (code, s["station_no"], s["station_id"],
                                 s["station_name"], s["stop_time"]))
                        conn.commit()
                        collected += 1
                    else:
                        no_stops += 1
                        no_stop_codes.append(code)
                    done += 1
                    if done % 20 == 0 or done == total:
                        print(f"  进度 {done}/{total}（成功 {collected}，无经停 {no_stops}）")
    finally:
        conn.close()
    print(f"  [经停] 完成 {done}：入库 {collected}，无经停/失败 {no_stops}")
    if no_stop_codes:
        print(f"  无经停车次: {no_stop_codes[:20]}{'...' if len(no_stop_codes) > 20 else ''}")


def main():
    ap = argparse.ArgumentParser(description="tripai 列车数据采集（借鉴 railway）")
    ap.add_argument("--letters", nargs="*", default=list(TRAIN_LETTERS),
                    help="车次字头（默认 K G C T Z D）")
    ap.add_argument("--digit", action="store_true", default=True,
                    help="包含纯数字车次（默认开）")
    ap.add_argument("--no-digit", dest="digit", action="store_false")
    ap.add_argument("--limit", type=int, default=None, help="本次最多采集车次数（实测用）")
    ap.add_argument("--stats", action="store_true", help="仅查看库规模")
    args = ap.parse_args()

    if args.stats:
        for k, v in stats().items():
            print(f"{k}: {v}")
        return

    init_db()
    collector = Collector12306()
    collector._session()
    print(f"[采集] 发现车次（字头: {' '.join(args.letters)}"
          f"{' + 数字' if args.digit else ''}）...")
    discovered = collector.discover_trains(letters=args.letters, digit=args.digit)
    print(f"[采集] 共发现 {len(discovered)} 个车次")

    conn = get_conn()
    try:
        pending, conflicts = save_discovered(conn, discovered)
    finally:
        conn.close()
    if conflicts:
        print(f"  ⚠ 跳过 {conflicts} 个同 train_no 改号码（tripai schema 每车一码）")

    existing = 0
    conn = get_conn(readonly=True)
    try:
        have = {r[0] for r in conn.execute("SELECT DISTINCT train_num FROM train_stops")}
    finally:
        conn.close()
    pending = [(c, t) for c, t in pending if c not in have]
    existing = len(have)
    print(f"  库中已有经停 {existing} 车，跳过重复")

    collect_stops(collector, pending, args.limit)

    n = refresh_station_trains()
    print(f"[完成] station_trains 已重建（{n} 站）")
    for k, v in stats().items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
