# -*- coding: utf-8 -*-
"""
cleanup.py —— 票价数据清理程序（数据不全 → 全量删库 → 重爬补全）

判定"数据不全"：某车次在 prices 中的站对数 < C(经停数, 2)
（经停数取自 railway.db train_stops；相邻+非相邻段应全覆盖，不足即不全，
 非相邻推算无法完整工作，cheapest 排序会失真 → 该车次票价整体删除，
 下次 collect.price_collector --resume 自动重爬补全。）

用法（项目根下）：
    .venv/Scripts/python.exe -m collect.cleanup              # 清理数据不全车次（默认动作）
    .venv/Scripts/python.exe -m collect.cleanup --check      # 只读体检：列出不全清单，不删除
    .venv/Scripts/python.exe -m collect.cleanup --all        # 全量清空票价库（慎用）
"""

import argparse
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import get_conn, get_prices_conn, init_db, PRICES_DB   # noqa: E402


def scan_incomplete() -> List[Dict]:
    """扫描数据不全的车次。返回 [{train_num, have, expected, stops}]（按缺失比例降序）"""
    rc = get_conn(readonly=True)
    try:
        stops_count: Dict[str, int] = {
            r[0]: r[1] for r in rc.execute(
                "SELECT train_num, COUNT(*) FROM train_stops GROUP BY train_num")}
    finally:
        rc.close()
    pc = get_prices_conn()
    try:
        have: Dict[str, int] = {
            r[0]: r[1] for r in pc.execute(
                "SELECT train_num, COUNT(DISTINCT from_station_id || '|' || to_station_id) "
                "FROM prices GROUP BY train_num")}
    finally:
        pc.close()

    bad: List[Dict] = []
    for tn, n_have in have.items():
        n_stops = stops_count.get(tn, 0)
        expected = n_stops * (n_stops - 1) // 2
        if n_stops >= 2 and n_have < expected:
            bad.append({"train_num": tn, "have": n_have,
                        "expected": expected, "stops": n_stops})
    bad.sort(key=lambda x: x["have"] / max(1, x["expected"]))
    return bad


def delete_trains(train_nums: List[str]) -> int:
    """删除指定车次的全部票价记录，返回删除车次数"""
    pc = get_prices_conn()
    try:
        for tn in train_nums:
            pc.execute("DELETE FROM prices WHERE train_num = ?", (tn,))
        pc.commit()
        return len(train_nums)
    finally:
        pc.close()


def cleanup_incomplete(threshold_all: bool = False) -> int:
    """程序化接口（price_collector --cleanup/--cleanup-all 复用）：
    返回清理/清空的车次数"""
    if threshold_all:
        pc = get_prices_conn()
        try:
            pc.execute("DELETE FROM prices")
            pc.commit()
            n = pc.execute("SELECT COUNT(DISTINCT train_num) FROM prices").fetchone()[0]
        finally:
            pc.close()
        return 0                      # 全清后无车次可言
    bad = scan_incomplete()
    return delete_trains([b["train_num"] for b in bad])


def main():
    ap = argparse.ArgumentParser(
        description="票价数据清理：数据不全的车次全量删库，配合 price_collector --resume 重爬")
    ap.add_argument("--check", action="store_true",
                    help="只读体检：列出数据不全清单，不删除")
    ap.add_argument("--all", action="store_true",
                    help="全量清空票价库（慎用）")
    args = ap.parse_args()

    init_db()

    if args.all:
        pc = get_prices_conn()
        try:
            n = pc.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            pc.execute("DELETE FROM prices")
            pc.commit()
            print(f"[全量清空] 已删除 {n} 条票价记录。")
        finally:
            pc.close()
        return

    bad = scan_incomplete()
    if not bad:
        print("[体检] 所有车次票价数据完整，无需清理。")
        return

    print(f"[体检] 发现 {len(bad)} 个数据不全的车次：")
    for b in bad[:30]:
        print(f"  {b['train_num']}: {b['have']}/{b['expected']} 站对（经停 {b['stops']} 站）")
    if len(bad) > 30:
        print(f"  ... 其余 {len(bad) - 30} 个略")

    if args.check:
        print(f"[只读] 共 {len(bad)} 个待清理（未删除）。加 --help 查看清理方式。")
        return

    n = delete_trains([b["train_num"] for b in bad])
    total_rows = 0
    print(f"[清理] 已删除 {n} 个数据不全车次的全部票价记录。")
    print("[下一步] 运行 collect.price_collector --resume 自动重爬补全。")
    _ = total_rows


if __name__ == "__main__":
    main()
