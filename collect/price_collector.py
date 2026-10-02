# -*- coding: utf-8 -*-
"""
collect/price_collector.py —— 票价爬取（借鉴 travel2/benchmark_travelplan 的 price_collector）

接口：https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice（按区间查，角→元 ÷10）
策略（travel2 同款）：
    1. 按车次爬**相邻站对**（每车 N-1 次请求）
    2. 非相邻段票价离线累加推算（_compute_pairs：A→C = A→B + B→C，各席别独立）
       → 任意区间都有参考价，供 cheapest 排序使用
    3. 候选日期逐日轮换（+3/+9/+13），当前生效日期记住（同一车次后续站对沿用）
    4. 反爬：403/429/非JSON/关键词 → 退避 + 换 UA + 重试
    5. 断点续爬 --resume（按站对完整性校验，不全的车次重爬）
席别：存 12306 实际席别名（二等座/一等座/商务座/硬座/硬卧/软卧）

用法（项目根下）：
    .venv/Scripts/python.exe -m collect.price_collector --train G1108   # 单趟
    .venv/Scripts/python.exe -m collect.price_collector --resume        # 断点续爬全量
    .venv/Scripts/python.exe -m collect.price_collector --limit 5       # 前 5 趟
"""

import argparse
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import get_conn, get_prices_conn, init_db, DB_PATH, PRICES_DB
from cleanup import cleanup_incomplete   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tripai.price")

CONFIG = {
    # 候选日期：票价列表按日期返回当日开行车次，非每日车须逐日兜底（今天命中率最高）
    "query_dates": [(datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in (0, 1, 2, 3, 9, 13)],
    "request_timeout": 15,
    "max_retries": 5,
    "min_interval": (0.3, 0.8),      # 票价接口反爬敏感，间隔比建库大
    "attempts_per_date": 2,          # 每日期尝试次数
}

PRICE_URL = "https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice"
INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"

ANTICRAWL_KEYWORDS = ["验证码", "captcha", "访问太频繁", "请求太频繁", "触发风控",
                      "Access Denied", "Too Many Requests"]

# 票价接口字段 → 席别存储名（同值组主名，与余票页列名一致：每列可能多名共用一个数值）
SEAT_GD = (("ze_price", "二等座/二等包座"), ("zy_price", "一等座"), ("swz_price", "商务座/特等座"))
SEAT_PUSU = (("yz_price", "硬座"), ("yw_price", "硬卧/二等卧"), ("rw_price", "软卧/一等卧"))
SEAT_ALL = SEAT_GD + SEAT_PUSU

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


class PriceCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        })
        self._req_lock = threading.Lock()
        self._last = 0.0
        self.stats = {"pair_ok": 0, "pair_fail": 0, "trains_ok": 0, "trains_fail": 0}

    # ---------- 会话 / 限速 / 反爬 ----------
    def init_session(self) -> bool:
        try:
            self.session.get(INIT_URL, timeout=CONFIG["request_timeout"])
            logger.info("[会话] Cookie 获取成功")
            return True
        except Exception as e:
            logger.warning(f"[会话] 初始化失败（继续）: {e}")
            return False

    def _rotate_ua(self):
        self.session.headers["User-Agent"] = random.choice(_USER_AGENTS)

    def _reinit(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        self.init_session()

    @staticmethod
    def _is_anticrawl(resp) -> bool:
        if resp is None:
            return False
        if resp.status_code in (403, 429):
            return True
        if resp.status_code == 200 and len(resp.text) < 1000:
            body = resp.text.lower()
            return any(kw.lower() in body for kw in ANTICRAWL_KEYWORDS)
        return False

    def _rate(self):
        with self._req_lock:
            elapsed = time.time() - self._last
            low, high = CONFIG["min_interval"]
            wait = random.uniform(low, high)
            if elapsed < wait:
                time.sleep(wait - elapsed)
            self._last = time.time()

    # ---------- 票价查询（单站对） ----------
    def query_price(self, from_id: str, to_id: str, train_num: str,
                    query_date: str) -> Optional[Dict[str, float]]:
        """查询某站对某日指定车次的各席别票价。返回 {实际席别名: 元} 或 None。"""
        params = {
            "leftTicketDTO.train_date": query_date,
            "leftTicketDTO.from_station": from_id,
            "leftTicketDTO.to_station": to_id,
            "purpose_codes": "ADULT",
        }
        headers = {
            "Referer": "https://kyfw.12306.cn/otn/view/queryPublicIndex.html",
            "X-Requested-With": "XMLHttpRequest",
        }
        for attempt in range(1, CONFIG["max_retries"] + 1):
            self._rate()
            try:
                resp = self.session.get(PRICE_URL, params=params, headers=headers,
                                        timeout=CONFIG["request_timeout"])
                if self._is_anticrawl(resp):
                    wait = min(10.0, 1.5 * attempt) + random.uniform(0.5, 2.0)
                    logger.warning(f"  ⚠ 反爬 [{attempt}/{CONFIG['max_retries']}]，换 UA，{wait:.0f}s 后重试")
                    self._rotate_ua()
                    self._reinit()
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    time.sleep(min(6.0, attempt) + random.uniform(0.3, 1.2))
                    continue
                data = resp.json()
                if data.get("status") is not True:
                    return None                       # 接口明确无数据（非反爬）
                for item in data.get("data") or []:
                    dto = item.get("queryLeftNewDTO") or {}
                    if dto.get("station_train_code", "") != train_num:
                        continue
                    # 字段驱动席别：哪个接口字段有值就映射为该字段的事实席别
                    # （实测 K1002 class="快速" 仅 yz/yw/rw 有值；class 值多变不作依据）
                    prices: Dict[str, float] = {}
                    for key, seat_cn in SEAT_ALL:
                        val = dto.get(key)
                        if val not in (None, ""):
                            try:
                                prices[seat_cn] = round(float(val) / 10.0, 1)
                            except (ValueError, TypeError):
                                pass
                    return prices or None
                return None                            # 区间内无该车
            except Exception:
                time.sleep(min(6.0, attempt) + random.uniform(0.3, 1.2))
        return None

    # ---------- 单趟车：相邻站对爬取 ----------
    def crawl_train(self, train_num: str, force: bool = False) -> bool:
        """爬取一趟车的相邻站对票价并入库（含非相邻段推算）。返回是否成功。
        force=True 时忽略完整性校验强制重爬。"""
        conn = get_conn()
        try:
            stops = [dict(zip(("station_id", "station_name"), r))
                     for r in conn.execute(
                         "SELECT station_id, station_name FROM train_stops "
                         "WHERE train_num = ? ORDER BY stop_no", (train_num,))]
        finally:
            conn.close()
        if len(stops) < 2:
            logger.warning(f"[跳过] {train_num} 经停不足 2 站")
            return False

        # 断点续爬：站对完整性校验（不完整则重爬整趟；force 强制重爬）
        conn = get_prices_conn()
        try:
            n_pairs = len(stops) - 1
            have = conn.execute(
                "SELECT COUNT(DISTINCT from_station_id || '|' || to_station_id) "
                "FROM prices WHERE train_num = ?", (train_num,)).fetchone()[0]
        finally:
            conn.close()
        if have >= n_pairs and not force:
            logger.info(f"[跳过] {train_num} 已完整（{have}/{n_pairs} 站对）")
            return True
        logger.info(f"[爬取] {train_num}（{len(stops)} 站，{n_pairs} 相邻站对，已有 {have}）...")

        conn = get_prices_conn()
        try:
            conn.execute("DELETE FROM prices WHERE train_num = ?", (train_num,))
            conn.commit()
            pair_ok = pair_fail = 0
            date_idx = 0
            for i in range(len(stops) - 1):
                f_id, f_name = stops[i]["station_id"], stops[i]["station_name"]
                t_id, t_name = stops[i + 1]["station_id"], stops[i + 1]["station_name"]
                prices = None
                for di in range(date_idx, len(CONFIG["query_dates"])):
                    crawl_date = CONFIG["query_dates"][di]
                    for attempt in range(CONFIG["attempts_per_date"]):
                        prices = self.query_price(f_id, t_id, train_num, crawl_date)
                        if prices:
                            break
                    if prices:
                        date_idx = di                 # 后续站对沿用生效日期
                        break
                if prices is None:
                    # 该段 12306 不发售（常见于市内超短段）→ 跳过继续，非相邻推算自动回避
                    logger.warning(f"  ✗ {f_name}→{t_name} 无票价数据，跳过该段")
                    pair_fail += 1
                    continue
                for seat_cn, price in prices.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO prices "
                        "(train_num, from_station_id, to_station_id, seat, price, crawl_date) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (train_num, f_id, t_id, seat_cn, price,
                         datetime.now().strftime("%Y-%m-%d")))
                conn.commit()
                pair_ok += 1

            # 非相邻段离线累加
            computed = self._compute_pairs(conn, train_num, stops)
            logger.info(f"[完成] {train_num}: 相邻 {pair_ok} 对（跳过 {pair_fail}），"
                        f"推算非相邻 {computed} 条")
            self.stats["pair_ok"] += pair_ok
            self.stats["pair_fail"] += pair_fail
            self.stats["trains_ok"] += 1 if pair_ok else 0
            return pair_ok > 0
        finally:
            conn.close()

    def _compute_pairs(self, conn, train_num: str, stops: List[dict]) -> int:
        """非相邻段票价 = 相邻段累加（各席别独立）；相邻段不全则跳过"""
        existing = {(r["from_station_id"], r["to_station_id"])
                    for r in conn.execute(
                        "SELECT DISTINCT from_station_id, to_station_id FROM prices "
                        "WHERE train_num = ?", (train_num,))}
        all_adj = {(stops[i]["station_id"], stops[i + 1]["station_id"])
                   for i in range(len(stops) - 1)}
        if all_adj - existing:
            return 0
        adj = {(r["from_station_id"], r["to_station_id"], r["seat"]): r["price"]
               for r in conn.execute(
                   "SELECT from_station_id, to_station_id, seat, price FROM prices "
                   "WHERE train_num = ?", (train_num,))}
        seats = [r[0] for r in conn.execute(
            "SELECT DISTINCT seat FROM prices WHERE train_num = ?", (train_num,))]
        n, computed = len(stops), 0
        crawl_date = datetime.now().strftime("%Y-%m-%d")
        for i in range(n):
            for j in range(i + 2, n):
                f_id, t_id = stops[i]["station_id"], stops[j]["station_id"]
                for seat in seats:
                    total, valid = 0.0, True
                    for k in range(i, j):
                        key = (stops[k]["station_id"], stops[k + 1]["station_id"], seat)
                        if key not in adj:
                            valid = False
                            break
                        total += adj[key]
                    if valid:
                        conn.execute(
                            "INSERT OR REPLACE INTO prices "
                            "(train_num, from_station_id, to_station_id, seat, price, crawl_date) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (train_num, f_id, t_id, seat, round(total, 2), crawl_date))
                        computed += 1
        conn.commit()
        return computed


def main():
    ap = argparse.ArgumentParser(description="tripai 票价爬取（借鉴 travel2 price_collector）")
    ap.add_argument("--train", type=str, default=None, help="仅爬指定车次，如 G1108")
    ap.add_argument("--force", action="store_true", help="强制重爬（忽略完整性跳过）")
    ap.add_argument("--cleanup", action="store_true",
                    help="清理数据不全车次的票价（下次 --resume 自动重爬）")
    ap.add_argument("--cleanup-all", action="store_true", help="全量清空票价库")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="断点续爬：完整的车次跳过、不全的重爬（默认开）")
    ap.add_argument("--limit", type=int, default=None, help="本次最多爬取车次数")
    ap.add_argument("--stats", action="store_true", help="查看票价库规模")
    args = ap.parse_args()

    if args.cleanup:
        init_db()
        n = cleanup_incomplete()
        logger.info(f"[cleanup] 已清理 {n} 个数据不全车次的票价（下次 --resume 自动重爬）")
        return
    if args.cleanup_all:
        init_db()
        n = cleanup_incomplete(threshold_all=True)
        logger.info(f"[cleanup-all] 已全量清空 {n} 个车次的票价")
        return
    if args.stats:
        conn = get_prices_conn()
        try:
            n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            t = conn.execute("SELECT COUNT(DISTINCT train_num) FROM prices").fetchone()[0]
            d = conn.execute("SELECT MAX(crawl_date) FROM prices").fetchone()[0]
            new = conn.execute(
                "SELECT COUNT(*) FROM prices WHERE LENGTH(seat) > 6").fetchone()[0]
            print(f"prices 共 {n} 条 / {t} 车 / 最新爬取日 {d}")
            print(f"其中新口径（组合席别名）{new} 条")
            print(f"数据库: {PRICES_DB}")
        finally:
            conn.close()
        return

    init_db()
    pc = PriceCollector()
    pc.init_session()

    conn = get_conn()
    try:
        if args.train:
            train_list = [args.train]
        else:
            train_list = [r[0] for r in conn.execute(
                "SELECT train_num FROM trains ORDER BY train_num")]
    finally:
        conn.close()
    if args.limit:
        train_list = train_list[:args.limit]
    logger.info(f"待爬取 {len(train_list)} 趟车次")

    for idx, train_num in enumerate(train_list, 1):
        ok = pc.crawl_train(train_num, force=args.force)
        if not ok:
            pc.stats["trains_fail"] += 1
        if idx % 20 == 0:
            logger.info(f"[进度] {idx}/{len(train_list)}（成功 {pc.stats['trains_ok']}，"
                        f"失败 {pc.stats['trains_fail']}）")

    logger.info(f"[汇总] 车次成功 {pc.stats['trains_ok']} / 失败 {pc.stats['trains_fail']}；"
                f"相邻站对成功 {pc.stats['pair_ok']} / 失败 {pc.stats['pair_fail']}")
    logger.info("提示：cheapest 排序使用 prices.db 参考价；实时票价仍走联网核实")


if __name__ == "__main__":
    main()
