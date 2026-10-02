# 铁程 tripai —— 对话式智能火车出行规划

原生 AI 场景的 toC 火车出行规划系统：用户以自然语言对话提出需求 → AI Agent 连续调用本地工具（本地列车库方案枚举）→ 联网核实 12306 实时余票与票价 → 流式输出正式规划报告 + 全屏方案详情（可手动查票）。

## 快速开始

```
cd C:\vscode_py\tripai
.venv\Scripts\python.exe -m app.server     # 端口 8600
```

浏览器打开 http://127.0.0.1:8600 → 首次进入填写 API-KEY / 模型名 / 接口地址（OpenAI 兼容）→ 验证进入。

## 架构

```
app\
  server.py    FastAPI 编排：配置门 / SSE agent 循环 / 会话落盘 / 手动查票直连端点
  llm.py       OpenAI 兼容流式客户端（reasoning_content 思考链 / 工具调用聚合 / 反爬兼容）
  prompts.py   旅行规划师系统提示词（信息收集策略 / 工具纪律 / 正式输出契约）
  tools.py     5 个 Agent 工具（结果双路输出：给 AI 的摘要 + 给详情页的全量数据）
  planner.py   本地方案引擎（移植 travel2 出题算法找解内核：直达/换乘/补票变体）
  online.py    12306 联网层（railway 反爬体系：全局串行会话/UA轮换/退避重试 + 票价接口）
  database.py  本地数据只读查询（railway.db 车站/车次/经停 + prices.db 历史参考价）
data\
  railway.db   本地列车静态数据（约 3400 站 / 4764 车次，复制自 travel2）
  prices.db    历史票价参考（仅排序估算，实时票价走联网）
static\        前端（人大红学术风，marked+DOMPurify，SSE）
sessions\      会话落盘（JSON，含完整 agent 轨迹与方案数据）
logs\          12306 爬取日志（JSONL，反爬排查）
```

## Agent 工作流

一轮 = 一次 LLM 请求：思考流（reasoning）→ 工具调用（内嵌思考区展示）→ 结果回填 → 下一轮；无工具调用的轮即**正式输出**（markdown 规划报告 + 方案总表）。上限 25 轮，可随时停止（已完成内容保留）。

| 工具 | 作用 |
|---|---|
| `search_station` | 站名/拼音/电报码模糊搜索（消歧） |
| `find_solutions` | 本地方案枚举：直达 + 一次换乘（时间窗/坐席/排序等参数见下表） |
| `check_tickets_online` | 联网核实方案实时余票+票价（按购买区间合并请求，单次≤5 个方案） |
| `query_train_stops` | 车次经停表 |
| `query_ticket_variants` | 无票深挖：补票变体（买短补长/前后多买一站）生成并核实 |

## find_solutions 输入参数（查票约束）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `from_station` / `to_station` | ✅ | — | 站名或电报码 |
| `date` | 否 | 今天+3 | 出行日期，限今天~14 天预售期 |
| `people_count` | 否 | 1 | 人数 |
| `seat_type` | 否 | 二等座 | 12306 实际席别：二等座/一等座/商务座/硬卧/软卧/硬座/软座/高级软卧/动卧/无座 |
| `allow_transfer` | 否 | true | 是否允许换乘 |
| `max_transfers` | 否 | 1 | 最大换乘次数 |
| `prefer_direct` | 否 | true | 直达优先排序 |
| `depart_after` / `arrive_before` | 否 | — | 出发/到达时间窗 HH:MM（支持跨零点） |
| `sort_by` | 否 | comprehensive | comprehensive/fastest/cheapest/earliest |
| `max_results` | 否 | 8 | 返回方案数上限（≤20） |

换乘约束（引擎内置）：换乘衔接 ≥20 分钟；换乘车不经过出发站；中转站与目的地不同城；当日完成。

## 反爬纪律

- 全局唯一会话 + 全局串行锁（任何时刻仅一个 12306 请求在途）
- UA 池随机 + init 页取 Cookie + 主动轮换（每 60 次成功）
- 反爬识别（403/429/关键词/HTML 错误页）→ 换会话重试，最多 10 次/区间
- 按购买区间合并请求：一次典型对话仅 4~10 个请求；AI 默认只核实 Top 方案，其余标「未核实」，用户可在方案详情页手动补查
