# 铁程 tripai —— 对话式智能火车出行规划

原生 AI 场景的 toC 火车出行规划系统：用户以自然语言对话提出需求 → AI Agent 连续调用本地工具（本地列车库方案枚举）→ 联网核实 12306 实时余票与票价 → 流式输出正式规划报告 + 全屏对话详情（可手动查票）。

---

# 一、环境准备（首次）

```cmd
cd /d C:\vscode_py\tripai
C:\vscode_py\.conda\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple --site
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> ⚠️ 区分 venv：本项目是 `C:\vscode_py\tripai\.venv`；travel2 也有同名 `.venv`。
> 终端 activate 跟随会话走（cd 不切换），**推荐始终用全路径调用**避免混淆。

# 二、启动系统

**方式一（推荐）：双击 `C:\vscode_py\tripai\启动铁程.bat`**——自动启动服务并打开浏览器，关窗即停。

**方式二：命令行**

```cmd
cd /d C:\vscode_py\tripai
.venv\Scripts\python.exe -m app.server
```

> ⚠️ 不能直接 `python app\server.py`——server.py 属于 app 包（相对导入）。

浏览器打开 **http://127.0.0.1:8600**：首次进入填写 API-KEY / 模型名 / 接口地址（OpenAI 兼容）→ 验证进入。配置保存在 `tripai\.env`，顶栏「设置」可改。

---

# 三、数据采集全流程（使用前必做）

**一行命令（推荐）：双击 `C:\vscode_py\tripai\采集全部.bat`**
自动按 车站 → 车次+经停 → 票价 顺序执行；任意一步 Ctrl+C 中断后重跑即续爬。

命令行分步：

```cmd
cd /d C:\vscode_py\tripai

:: ① 车站表（秒级）
.venv\Scripts\python.exe -m collect.stations

:: ② 车次 + 经停（全字头 K G C T Z D + 数字；约 40~60 分钟；可中断续爬）
.venv\Scripts\python.exe -m collect.collector
.venv\Scripts\python.exe -m collect.collector --letters G D K    :: 指定字头
.venv\Scripts\python.exe -m collect.collector --limit 20         :: 小规模试水

:: ③ 票价（相邻站对爬取 + 非相邻推算；约 2~4 小时；可中断续爬）
.venv\Scripts\python.exe -m collect.price_collector --resume
.venv\Scripts\python.exe -m collect.price_collector --train G1   :: 单趟
.venv\Scripts\python.exe -m collect.price_collector --resume --workers 5   :: 指定并发（默认 3）

:: ④ 清理数据不全的车次（站对数 < C(经停,2)），删后重跑 ③ --resume 补全
.venv\Scripts\python.exe -m collect.cleanup --check    :: 只读体检
.venv\Scripts\python.exe -m collect.cleanup            :: 执行清理
:: ⚠ --cleanup-all 全量清空票价库（慎用）

:: ⑤ 状态查看
.venv\Scripts\python.exe -m collect.collector --stats
.venv\Scripts\python.exe -m collect.price_collector --stats

:: 补齐改号车别名码经停（纯 SQL 秒级；正常采集结束会自动执行）
.venv\Scripts\python.exe -m collect.collector --fill-alias
```

**机制说明**：
- ② ③ 均支持 **Ctrl+C 中断续爬**（已完成车次自动跳过）
- 改号车（同 train_no 多显示码）：所有码入库并**共用同一份经停**；票价/余票匹配按 train_no 兜底
- 12306 票价/余票列表按日期返回**当日开行**车次——非每日车自动多日期兜底（+0/+1/+2/+3/+9/+13）
- 反爬等待与 travel2 一致：命中反爬 `sleep(10~20秒 × 尝试次数)`；请求间隔 0.15 秒

---

# 四、使用模式

进入主界面后有两种模式（点左上角印章返回模式选择）：

## AI 对话模式

- 自然语言描述需求，例：*“下周六 3 个人从北京去上海，尽量上午出发，二等座”*
- AI 自动：消歧车站 → 本地枚举方案 → 联网核实余票票价（Top 方案）→ 无票时深挖补票变体
- 每轮显示：**思考流**（自动折叠为「思考 N 秒」，工具调用内嵌其中可展开）→ **正式输出**（规划报告 + 方案总表，只显示可购/未核实方案）
- 连续对话可追问、改日期/坐席/目的地；顶栏「对话详情」按行平铺本次对话全部方案（含无票的），支持逐行/批量联网核实
- 会话：左侧栏右键重命名/删除；历史会话完整回放（含思考链与轨迹）；「复制」按钮复制 AI 回答

## 手动查票模式

表单直填、全程不经 AI：

| 项 | 说明 |
|---|---|
| 出发/到达站 | 站名或电报码 |
| 出行日期 | **必填**，默认今天+3，限 14 天预售期内 |
| 坐席 | 12306 实际席别 15 种（二等座/一等座/商务座/硬卧/软卧/硬座/软座/高级软卧/动卧/无座…） |
| 换乘次数 | 直达 / 最多 1 次 / 最多 2 次 |
| 出发时间从–到 / 到达时间从–到 | 时间范围筛选（支持跨零点） |
| 车型 | 不限 / 高铁动车（G/C/D）/ 普速（K/T/Z/数字） |
| 排序 | 综合评分 / 历时最短 / 价格最低 / 最早出发 / 最晚出发 / 最早到达 |
| 返回数量 | 默认 20（后端实际枚举 10 倍候选，打分后取前 N） |

结果按评分排序逐行平铺，可逐行或批量「联网核实」。

## 评分方法（综合评分排序）

约束（时间窗/换乘间隔≥20分钟等）先硬过滤；评分 = 多维度归一化加权：时间(0.35) + 价格(0.25) + 换乘次数(0.15) + 换乘等待(0.10) + 补票段数(0.15)，各维度归一化 0~1。选其他排序时直接比较、不评分。

## 实时余票/票价

核实数据来自 12306（余票 queryZ + 票价 queryAllPublicPrice），按**购买区间**合并请求（反爬友好）；核实前显示「历史参考价」（来自 prices.db），未核实方案标「未核实」，可在对话详情手动补查。

---

# 五、常见问题

| 问题 | 处理 |
|---|---|
| 页面全白 | 服务窗口是否还开着（关窗即停服）？F12 看 Console 报错截图反馈；浏览器 Ctrl+F5 |
| 端口占用 | `netstat -ano \| findstr :8600` 找 PID → `taskkill /PID xxx /F` |
| pip 装的包不见了 | 终端激活的是别的项目 venv（都叫 .venv）——用全路径 `.venv\Scripts\python.exe` 调用 |
| 方案枚举为空 / 覆盖不全 | 数据未采集完：跑「三、数据采集全流程」；改号车别名区间需新版采集补齐 |
| 某车查不到某席别 | 按实际席别查询（普速车无二等座，用硬座/硬卧）；该车确实无此席时显示 no_seat |
| 票价某段无数据 | 12306 对市内超短段不发售（自动跳段）；或该车该日不开行（多日期兜底已内置） |
| cheapest 参考价失真 | 参考价来自 prices.db 历史爬取——重跑 ③ 刷新；实时价以「联网核实」为准 |
| **data 目录** | **切勿删除**——railway.db + prices.db 是唯一数据存储，删了需重爬数小时 |

---

# 六、架构

```
app\
  server.py    FastAPI 编排：配置门 / SSE agent 循环 / 会话落盘 / 手动查票直连端点
  llm.py       OpenAI 兼容流式客户端（reasoning_content 思考链 / 工具调用聚合 / 反爬兼容）
  prompts.py   旅行规划师系统提示词（信息收集策略 / 工具纪律 / 正式输出契约）
  tools.py     5 个 Agent 工具（结果双路输出：给 AI 的摘要 + 给详情页的全量数据）
  planner.py   本地方案引擎（移植 travel2 出题算法找解内核：直达/换乘链式/补票变体
               + 评分 + 车型筛选 highspeed(G/C/D)/normal + 双向时间窗 + 挖到足数再停）
  online.py    12306 联网层（railway 反爬体系：全局串行会话/UA轮换/退避重试 + 票价接口 + 改号车匹配）
  database.py  本地数据只读查询（railway.db 车站/车次/经停 + prices.db 历史参考价）
collect\      数据采集包（借鉴 railway / travel2 采集逻辑，入库 tripai schema）
  stations.py        采集全国车站表（station_name.js → 电报码/站名）
  collector.py       车次发现（search API 前缀法，K G C T Z D + 数字）+ 经停采集（queryByTrainNo，
                     候选日期 +0/+1/+2/+3/+9/+13 兜底非每日车）+ 改号车同号组共用经停
                     + station_trains 重建
  price_collector.py 票价爬取（queryAllPublicPrice，参考 travel2 price_collector：相邻站对爬取 +
                     非相邻段离线累加推算 + 逐日轮换 + 多线程 --workers（共享 RateLimiter）
                     + 断点续爬 --resume / --force + Ctrl+C 优雅退出）
  cleanup.py         票价数据清理：数据不全（站对数 < C(经停,2)）的车次全量删库 → --resume 重爬补全
                     （--check 只读体检 / --all 全量清空慎用）
  database.py        建表/连接（schema 与 app 一致；railway 与 prices 分库分 schema）
data\
  railway.db   本地列车静态数据（车站/车次/经停；可由 collect 包重新采集刷新）
  prices.db    历史票价参考（仅 cheapest 排序估算，实时票价走联网）
static\        前端（人大红学术风，marked+DOMPurify，SSE）
sessions\      会话落盘（JSON，含完整 agent 轨迹与方案数据）
logs\          12306 爬取日志（JSONL，反爬排查）
docs\
  12306数据规格.md  ★ 12306 各接口返回数据规格、解析规则、入库映射与已知坑（实测核验）
```

## 数据保存说明

| 内容 | 位置 |
|---|---|
| 对话上下文 / 执行轨迹（含思考链）/ 方案数据 | `sessions\{会话id}.json`（每轮原子落盘，重启可恢复） |
| 列车/经停（静态线路图） | `data\railway.db` |
| 历史票价参考 | `data\prices.db` |
| 爬取日志 | `logs\ticket_*.log`（JSONL） |
