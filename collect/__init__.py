# -*- coding: utf-8 -*-
"""
collect —— tripai 本地列车数据采集包（借鉴 C:/vscode_py/railway 的采集逻辑）

采集 12306 车站 / 车次 / 经停（覆盖 K G C T Z D 及纯数字车次），
入库为 tripai 自身 schema（与 app/database.py 一致）：

    stations(station_id 电报码 PK, station_name)
    trains(train_num PK, train_no UNIQUE)
    train_stops(train_num, stop_no, station_id, station_name, stop_time=发车时刻)
    station_trains(station_id, station_name, data JSON 物化视图)

用法（在项目根 C:/vscode_py/tripai 下）：
    .venv/Scripts/python.exe -m collect.stations              # 1. 采集车站表
    .venv/Scripts/python.exe -m collect.collector             # 2. 采集全部车次+经停
    .venv/Scripts/python.exe -m collect.collector --letters G D K   # 只采集指定字头
    .venv/Scripts/python.exe -m collect.collector --limit 20  # 小规模实测
"""
