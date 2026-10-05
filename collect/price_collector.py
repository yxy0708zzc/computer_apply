# -*- coding: utf-8 -*-
"""
collect/price_collector.py —— 票价爬取（与 travel2 step1_collect prices 子命令同款）

接口：https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice（按区间查，角→元 ÷10）
策略（travel2 同款）：
    1. 按车次爬**相邻站对**（每车 N-1 次请求），[爬取]/✓/⟳/✗ 站对级实时打印
    2. 非相邻段票价离线累加推算（A→C = A→B + B→C，各席别独立）→ 全区间参考价（📐 打印）
    3. 候选日期逐日轮换（+0/+1/+2/+3/+9/+13），生效日期记住（同车后续站对沿用）
    4. 站对重试：每日期 3 轮 × 每轮 5 次（travel2 同款结构）
    5. 反爬：403/429/非JSON/关键词 → price 档退避 min(10,1.5×次)+0.5~2s + 换 UA；
       非200/异常 min(6,次)+0.3~1.2s（每轮都睡）
    6. 断点续爬（默认开，--force 全量重爬）：站对完整性 C(经停,2) 校验，
       不全的车次整趟重爬；失败段写 data/failed_pairs.log
并发（travel2 同款）：
    - --workers N（默认 3）多线程爬取，每线程独立 Session+prices 连接
    - RateLimiter 全局共享（加锁），所有线程合计保持 ≥min_interval 间隔
    - Ctrl+C 优雅退出（第一次停止派发，再按强制退出）；已入库数据保留
席别：字段驱动（哪个接口字段有值就映射为该字段的事实席别），存实际席别组合名
与 travel2 的差异：某段各日期均无数据时**跳段继续**（travel2 整车放弃）——
    12306 对市内超短段不发售，跳段保住整车其余站对与非相邻推算；跳过段记 failed_pairs.log

用法（项目根下）：
    .venv/Scripts/python.exe -m collect.price_collector --train G1              # 单趟
    .venv/Scripts/python.exe -m collect.price_collector --resume                # 全量断点续爬
    .venv/Scripts/python.exe -m collect.price_collector --resume --workers 15   # 15 并发
    .venv/Scripts/python.exe -m collect.price_collector --stats                 # 库规模
"""

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database
from database import get_conn, get_prices_conn, init_db, init_prices_db, PRICES_DB
from cleanup import cleanup_incomplete   # noqa: E402

CONFIG = {
    # 候选日期：票价列表按日期返回当日开行车次，非每日车须逐日兜底（今天命中率最高）
    "query_dates": [(datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in (0, 1, 5, 6)],
    "request_timeout": 15,
    "max_retries": 5,
    "min_interval": 0.02,            # 全局共享间隔（RateLimiter 口径，所有线程合计）
    "attempts_per_date": 5,          # 每日期尝试次数（travel2：3 轮 x 5 次）
    "rounds": 3,                     # 站对重试轮数（travel2 同款）
    "default_workers": 3,            # 默认并发线程（travel2 常用 --workers 3）
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


class RateLimiter:
    """相邻请求间隔 ≥ min_interval（monotonic 时钟）。
    加锁：--workers>1 时多线程共享实例，判定+更新原子化，
    否则并发可同时通过间隔检查，全局请求间隔被击穿、易触发反爬（travel2 同款）。"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if self._last > 0 and now - self._last < self.min_interval:
                time.sleep(self.min_interval - (now - self._last))
            self._last = time.monotonic()


_shutdown = False


def _handle_sigint(sig, frame):
    global _shutdown
    if _shutdown:
        print("\n⚠️  强制退出")
        sys.exit(1)
    _shutdown = True
    print("\n⏳ 正在优雅退出（再按一次 Ctrl+C 强制退出）...")


signal.signal(signal.SIGINT, _handle_sigint)


def is_anticrawl(resp) -> bool:
    if resp is None:
        return False
    if resp.status_code in (403, 429):
        return True
    if resp.status_code == 200 and len(resp.text) < 1000:
        body = resp.text.lower()
        return any(kw.lower() in body for kw in ANTICRAWL_KEYWORDS)
    return False


class PriceCollector:
    """票价爬取（travel2 同款：RateLimiter 全局共享 + 每线程独立 Session/prices 连接）"""

    def __init__(self, resume: bool = True, target_train: str = None, max_workers: int = 3):
        self.resume = resume                      # False=全量重爬（--force）
        self.target_train = target_train
        self.max_workers = max_workers
        self.rate = RateLimiter(CONFIG["min_interval"])
        self.session = requests.Session()          # 主会话（Cookie 模板，线程复制用）
        self.session.headers.update({
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        })
        # 失败明细（travel2 同款：prices-db 同目录 failed_pairs.log，车次\t起点\t终点\t原因）
        self.failed_log = os.path.join(os.path.dirname(PRICES_DB), "failed_pairs.log")
        os.makedirs(os.path.dirname(self.failed_log), exist_ok=True)
        # 解析已知不发售段计数（resume 完整性校验用：跳段车不再被反复重爬）
        self._failed_counts: Dict[str, int] = {}
        try:
            with open(self.failed_log, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 2 and parts[0]:
                        self._failed_counts[parts[0]] = \
                            self._failed_counts.get(parts[0], 0) + 1
        except FileNotFoundError:
            pass
        with open(self.failed_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        self.total_pairs = self.success_count = self.fail_count = 0
        self._stats_lock = threading.Lock()
        self._log_lock = threading.Lock()

    # ---------- 会话 ----------
    def init_session(self) -> bool:
        """访问 leftTicket/init 获取 Cookie（travel2 同款：失败则终止）"""
        resp = self._get(INIT_URL, headers={"Referer": "https://kyfw.12306.cn/otn/leftTicket/"})
        print("[Session] 会话初始化" + ("成功" if resp else "失败"))
        return resp is not None

    def _thread_session(self) -> requests.Session:
        """线程独立 Session：headers/cookies 从主会话复制
        （travel2 修复：并发线程无 Cookie 可能大面积查询失败）"""
        s = requests.Session()
        s.headers.update(self.session.headers)
        s.cookies.update(self.session.cookies)
        return s

    @staticmethod
    def _rotate_ua(session: requests.Session):
        session.headers["User-Agent"] = random.choice(_USER_AGENTS)

    def _log_fail(self, line: str):
        with self._log_lock:
            with open(self.failed_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ---------- 带退避重试的 GET（travel2 price 档口径） ----------
    def _get(self, url: str, params: dict = None, headers: dict = None,
             session: requests.Session = None) -> Optional[requests.Response]:
        """限速 + 三路退避重试（反爬/非200/网络异常），全部失败返回 None。
        反爬 min(10,1.5×次)+0.5~2s 换 UA；非200/异常 min(6,次)+0.3~1.2s（每轮都睡）。"""
        used = session or self.session
        for attempt in range(1, CONFIG["max_retries"] + 1):
            try:
                self.rate.wait()
                hdrs = used.headers.copy()
                if headers:
                    hdrs.update(headers)
                resp = used.get(url, params=params, headers=hdrs,
                                timeout=CONFIG["request_timeout"])
                if is_anticrawl(resp):
                    print(f"⚠️ 反爬 [尝试 {attempt}/{CONFIG['max_retries']}] "
                          f"HTTP {resp.status_code}")
                    self._rotate_ua(used)
                    if attempt < CONFIG["max_retries"]:
                        time.sleep(min(10.0, 1.5 * attempt) + random.uniform(0.5, 2.0))
                    continue
                if resp.status_code != 200:
                    print(f"[重试 {attempt}/{CONFIG['max_retries']}] HTTP {resp.status_code}")
                    time.sleep(min(6.0, 1.0 * attempt) + random.uniform(0.3, 1.2))
                    continue
                return resp
            except requests.RequestException as e:
                print(f"[重试 {attempt}/{CONFIG['max_retries']}] 请求异常: {e}")
                time.sleep(min(6.0, 1.0 * attempt) + random.uniform(0.3, 1.2))
        return None

    # ---------- 票价查询（单站对单次；重试由 _get/调用方负责，travel2 同款分层） ----------
    def query_price(self, from_id: str, to_id: str, train_num: str,
                    session: requests.Session, query_date: str,
                    expected_train_no: str = None) -> Optional[Dict[str, float]]:
        """查站对某日指定车次各席别票价（角→元）。返回 {席别组合名: 元} 或 None。
        同向改号车：DTO 显示码不等于 train_num 但 train_no 与期望一致时同样命中
        （railway match_train 同源思路）。"""
        resp = self._get(PRICE_URL, params={
            "leftTicketDTO.train_date": query_date,
            "leftTicketDTO.from_station": from_id,
            "leftTicketDTO.to_station": to_id,
            "purpose_codes": "ADULT"},
            headers={"Referer": "https://kyfw.12306.cn/otn/view/queryPublicIndex.html",
                     "X-Requested-With": "XMLHttpRequest"},
            session=session)
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if data.get("status") is not True:
            return None                       # 接口明确无数据（非反爬）
        for item in (data.get("data") or []):
            dto = item.get("queryLeftNewDTO") or {}
            dto_code = dto.get("station_train_code", "")
            dto_tno = dto.get("train_no", "")
            if dto_code != train_num and (
                    not expected_train_no or dto_tno != expected_train_no):
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
            if prices:
                return prices
            break   # 找到目标车次但无价格数据，不遍历其余车次（travel2 同款）
        return None                            # 区间内无该车

    # ---------- 单车次（travel2 同款流程） ----------
    def _crawl_single_train(self, train_num: str, already: set,
                            shared_p_conn=None) -> Dict[str, int]:
        """爬一趟车：resume 时校验站对完整性（不全则重爬）；成功且零失败后补算非相邻段。
        并发线程独立 Session + prices 连接（shared_p_conn 供单线程复用）。"""
        if _shutdown:
            return {"pair_ok": 0, "pair_fail": 0}
        own = shared_p_conn is None
        rw = get_conn()
        p_conn = shared_p_conn or get_prices_conn()
        local = None
        if own:   # 并发线程独立 Session 并同步主 Cookie（只复制 headers 会大面积查询失败）
            local = self._thread_session()
        try:
            stops = [dict(zip(("station_id", "station_name"), r)) for r in rw.execute(
                "SELECT station_id, station_name FROM train_stops "
                "WHERE train_num = ? ORDER BY stop_no", (train_num,))]
            tno_row = rw.execute("SELECT train_no FROM trains WHERE train_num = ?",
                                 (train_num,)).fetchone()
            expected_tno = tno_row[0] if tno_row else None   # 改号车匹配用
            if len(stops) < 2:
                print(f"[跳过] {train_num} 经停站不足 2 个")
                self._log_fail(f"{train_num}\t-\t-\t经停站不足2个")
                return {"pair_ok": 0, "pair_fail": 0}

            if self.resume and train_num in already:
                expected = len(stops) * (len(stops) - 1) // 2
                n_pairs = len(stops) - 1
                # 主键点查逐站对计数（WITHOUT ROWID 主键 from,to,train 无 train 前缀索引）
                actual = sum(
                    1 for i in range(n_pairs)
                    if p_conn.execute(
                        "SELECT 1 FROM prices WHERE from_station_id = ? "
                        "AND to_station_id = ? AND train_num = ?",
                        (stops[i]["station_id"], stops[i + 1]["station_id"],
                         train_num)).fetchone())
                failed_n = self._failed_counts.get(train_num, 0)   # 已知不发售段数
                if actual >= expected or actual + failed_n >= n_pairs:
                    print(f"[跳过] {train_num} 已爬取 ({actual}/{expected}，"
                          f"含已知不发售段 {failed_n}）")
                    return {"pair_ok": 0, "pair_fail": 0}
                print(f"[续爬] {train_num} 数据不全 ({actual}/{expected})，重新爬取")

            print(f"[爬取] {train_num} ...")
            # 删旧数据：主键序 (from,to,train) 无 train 前缀，逐站对删走主键前缀
            for i in range(len(stops) - 1):
                p_conn.execute(
                    "DELETE FROM prices WHERE from_station_id = ? AND to_station_id = ? "
                    "AND train_num = ?",
                    (stops[i]["station_id"], stops[i + 1]["station_id"], train_num))
            p_conn.commit()
            ok, fail = self._crawl_pairs(p_conn, train_num, stops, expected_tno,
                                         session=local if own else None)
            if ok > 0:
                print(f"[完成] {train_num}: {ok}/{ok + fail} 站对成功")
                if fail == 0:
                    self._compute_pairs(p_conn, train_num, stops)
            else:
                print(f"[失败] {train_num}: 全部 {fail} 个站对均无数据")
            return {"pair_ok": ok, "pair_fail": fail}
        finally:
            rw.close()
            if own:
                p_conn.close()

    def _crawl_pairs(self, p_conn, train_num: str, stops: List[dict],
                     expected_tno: str = None,
                     session: requests.Session = None) -> Tuple[int, int]:
        """相邻站对逐一爬取（travel2 同款重试结构）：当前生效日期起逐日重试 ×
        每日期 3 轮 × 每轮 5 次；生效日期记住（同车后续站对沿用）。
        全日期失败：打印 ✗、记 failed_pairs.log、跳段继续——travel2 为整车放弃，
        tripai 保留跳段（12306 对市内超短段不发售，跳段保住其余站对与非相邻推算）。"""
        pair_ok = pair_fail = 0
        date_idx = 0
        today = datetime.now().strftime("%Y-%m-%d")
        for i in range(len(stops) - 1):
            if _shutdown:
                break
            f, t = stops[i], stops[i + 1]
            prices = None
            for di in range(date_idx, len(CONFIG["query_dates"])):
                crawl_date = CONFIG["query_dates"][di]
                if di != date_idx:
                    print(f"  ⟳ 换日期 {crawl_date} 重试 {f['station_name']}→{t['station_name']}")
                for round_i in range(CONFIG["rounds"]):
                    for attempt in range(CONFIG["attempts_per_date"]):
                        prices = self.query_price(f["station_id"], t["station_id"], train_num,
                                                  session=session, query_date=crawl_date,
                                                  expected_train_no=expected_tno)
                        if prices:
                            break
                        print(f"  ⟳ {f['station_name']}→{t['station_name']} "
                              f"[{train_num}] 第{attempt + 1}次无数据")
                    if prices:
                        break
                    if round_i < CONFIG["rounds"] - 1:
                        print(f"  ⟳ 第{round_i + 1}轮无果，进入下一轮")
                if prices:
                    date_idx = di
                    break

            if prices is None:
                print(f"  ✗ {f['station_name']}→{t['station_name']} "
                      f"各日期均无数据，跳过该段 {train_num}")
                self._log_fail(f"{train_num}\t{f['station_name']}({f['station_id']})\t"
                               f"{t['station_name']}({t['station_id']})\t全日期无数据")
                pair_fail += 1
                continue

            # 紧凑表形 v2：WITHOUT ROWID 主键(from,to,train)，席别短键 JSON 整行覆盖
            p_conn.execute(
                "INSERT OR REPLACE INTO prices "
                "(from_station_id, to_station_id, train_num, seats, crawl_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (f["station_id"], t["station_id"], train_num,
                 json.dumps({database.SEAT_SHORT[k]: v for k, v in prices.items()
                             if k in database.SEAT_SHORT},
                            ensure_ascii=False, separators=(",", ":")), today))
            p_conn.commit()
            pair_ok += 1
            print(f"  ✓ {f['station_name']}→{t['station_name']} {prices}")
        return pair_ok, pair_fail

    def _compute_pairs(self, p_conn, train_num: str, stops: List[dict]) -> int:
        """非相邻段票价 = 相邻段逐席别累加（相邻段不齐则跳过，返回补算条数）。
        紧凑表形：相邻段一行多席（seats JSON），非相邻段按席别独立累加后仍存为一行。"""
        adj = {(r["from_station_id"], r["to_station_id"]):
               {database.SEAT_SHORT_INV.get(k, k): v
                for k, v in json.loads(r["seats"]).items()}
               for r in p_conn.execute(
                   "SELECT from_station_id, to_station_id, seats FROM prices "
                   "WHERE train_num = ?", (train_num,))}
        all_adj = {(stops[i]["station_id"], stops[i + 1]["station_id"])
                   for i in range(len(stops) - 1)}
        if all_adj - set(adj):
            return 0
        seats_all = sorted({k for m in adj.values() for k in m})   # 该车出现过的全部席别
        n, computed = len(stops), 0
        crawl_date = datetime.now().strftime("%Y-%m-%d")
        for i in range(n):
            for j in range(i + 2, n):
                f_id, t_id = stops[i]["station_id"], stops[j]["station_id"]
                merged: Dict[str, float] = {}
                for seat in seats_all:
                    total, valid = 0.0, True
                    for k in range(i, j):
                        m = adj.get((stops[k]["station_id"], stops[k + 1]["station_id"]), {})
                        if seat not in m:
                            valid = False
                            break
                        total += m[seat]
                    if valid:
                        merged[seat] = round(total, 1)
                if merged:
                    p_conn.execute(
                        "INSERT OR REPLACE INTO prices "
                        "(from_station_id, to_station_id, train_num, seats, crawl_date) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (f_id, t_id, train_num,
                         json.dumps({database.SEAT_SHORT[k]: v for k, v in merged.items()
                                     if k in database.SEAT_SHORT},
                                    ensure_ascii=False, separators=(",", ":")),
                         crawl_date))
                    computed += 1
        p_conn.commit()
        if computed:
            print(f"  📐 已计算 {computed} 条非相邻段票价")
        return computed

    # ---- 汇总报告（travel2 同款） ----
    def _report(self):
        print("=" * 50)
        print(f"爬取完成：总站对 {self.total_pairs}，成功 {self.success_count}，失败 {self.fail_count}"
              + (f"，成功率 {self.success_count / self.total_pairs * 100:.1f}%"
                 if self.total_pairs else ""))
        print(f"失败明细：{self.failed_log}")


def main():
    ap = argparse.ArgumentParser(description="tripai 票价爬取（travel2 同款）")
    ap.add_argument("--train", type=str, default=None, help="仅爬指定车次，如 G1")
    ap.add_argument("--resume", action="store_true",
                    help="断点续爬（默认行为，跳过站对完整的车次；开关为显式声明便于脚本串联）")
    ap.add_argument("--force", action="store_true", help="全量重爬（忽略已爬数据）")
    ap.add_argument("--workers", type=int, default=CONFIG.get("default_workers", 3),
                    help="并发线程数（默认 3）")
    ap.add_argument("--limit", type=int, default=None, help="本次最多爬取车次数")
    ap.add_argument("--stats", action="store_true", help="查看票价库规模")
    ap.add_argument("--cleanup", action="store_true", help="已禁用（删除功能下线），保留参数兼容脚本")
    ap.add_argument("--cleanup-all", action="store_true", help="已禁用（删除功能下线），保留参数兼容脚本")
    args = ap.parse_args()

    if args.cleanup or args.cleanup_all:
        init_db()
        init_prices_db()
        print("[cleanup] 删除功能已禁用：失败段为 12306 不发售，删了重爬也无法补全。"
              "只读体检请用 python -m collect.cleanup；单趟重爬用 --train 车次号 --force")
        return
    if args.stats:
        init_prices_db()
        conn = get_prices_conn()
        try:
            n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            t = conn.execute("SELECT COUNT(DISTINCT train_num) FROM prices").fetchone()[0]
            d = conn.execute("SELECT MAX(crawl_date) FROM prices").fetchone()[0]
            print(f"prices 共 {n} 站对 / {t} 车 / 最新爬取日 {d}")
            print(f"数据库: {PRICES_DB}")
        finally:
            conn.close()
        return

    init_db()
    init_prices_db()
    print("[DB] prices 表已就绪")
    pc = PriceCollector(resume=not args.force, target_train=args.train,
                        max_workers=max(1, args.workers))
    if not pc.init_session():
        sys.exit("❌ 会话初始化失败，终止")

    conn = get_conn()
    try:
        train_list = ([args.train] if args.train else
                      [r[0] for r in conn.execute(
                          "SELECT train_num FROM trains ORDER BY train_num")])
    finally:
        conn.close()
    if args.limit:
        train_list = train_list[:args.limit]

    already = set()
    if pc.resume:
        p = get_prices_conn()
        already = {r[0] for r in p.execute("SELECT DISTINCT train_num FROM prices")}
        p.close()
        print(f"[续爬] 已有 {len(already)} 趟车次已爬取，跳过完整车次")

    workers = pc.max_workers if not args.train else 1     # 单趟不并发（travel2 同款）
    if workers > 1:
        print(f"[并发] {workers} 线程 × {len(train_list)} 车次")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(pc._crawl_single_train, tn, already): tn
                    for tn in train_list if not _shutdown}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                    with pc._stats_lock:
                        pc.total_pairs += r["pair_ok"] + r["pair_fail"]
                        pc.success_count += r["pair_ok"]
                        pc.fail_count += r["pair_fail"]
                except Exception as e:
                    print(f"[异常] {futs.get(fut, '?')}: {e}")
    else:
        p_conn = get_prices_conn()
        try:
            for tn in train_list:
                if _shutdown:
                    print("[中止] 检测到退出信号")
                    break
                r = pc._crawl_single_train(tn, already, shared_p_conn=p_conn)
                pc.total_pairs += r["pair_ok"] + r["pair_fail"]
                pc.success_count += r["pair_ok"]
                pc.fail_count += r["pair_fail"]
        finally:
            p_conn.close()
    pc._report()


if __name__ == "__main__":
    main()
