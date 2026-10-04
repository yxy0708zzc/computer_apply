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
    """程序化接口（price_collector --cleanup/--cleanup-all 复用）。
    ⚠️ 删除功能已禁用：新版爬虫跳段继续后，含不发售段的车永远凑不齐 C(n,2)，
    删了重爬也无法补全（12306 不发售是永久缺失），只会白白损失已爬到的数据。"""
    print("[cleanup] 删除功能已禁用：失败段为 12306 不发售，删了重爬也无法补全。"
          "如需重爬某趟车请用 price_collector --train 车次号 --force")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="票价数据只读体检（删除功能已禁用：跳段车永远不齐 C(n,2)，删了无法补全）")
    ap.add_argument("--check", action="store_true",
                    help="只读体检：列出数据不全清单，不删除（默认动作）")
    ap.add_argument("--all", action="store_true", help="已禁用，无效果")
    args = ap.parse_args()

    init_db()

    if args.all:
        print("[已禁用] --all 全量清空已下线（删了重爬也无法补全，且会损失已爬数据）。")
        return

    bad = scan_incomplete()
    if not bad:
        print("[体检] 所有车次票价数据完整（或无票价数据）。")
        return

    print(f"[体检] 发现 {len(bad)} 个站对数未达 C(n,2) 的车次（多为含 12306 不发售段，属正常，无需处理）：")
    for b in bad[:30]:
        print(f"  {b['train_num']}: {b['have']}/{b['expected']} 站对（经停 {b['stops']} 站）")
    if len(bad) > 30:
        print(f"  ... 其余 {len(bad) - 30} 个略")
    print("[说明] 仅只读展示，不做删除；单趟重爬请用 price_collector --train 车次号 --force")


if __name__ == "__main__":
    main()
