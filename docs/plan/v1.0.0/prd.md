# dear-job-seeker — PRD v1.0.0

> **SUPERSEDED by [v1.1.0](../v1.1.0/prd.md)**（服务端 + 本地拉取方案已取消，改为单进程本地客户端）。仅留作决策历史。

Baseline：由两个内部爬虫项目抽取而来（队列/worker/出口池/provider 思路，Boss 扫码登录思路）；本仓库为独立重写的精简版。

图：`architecture.drawio`（3 页：架构 / 本地抓取时序 / 数据库），Pencil 需求板见仓库根 `untitled.pen`。

## 目标
教学/分享用的私有仓库：**小，但 clone 后能跑通**。

## 范围（is）
1. **服务端 MCP**（Python，Streamable HTTP `/mcp`）：建任务、查任务、取职位、设频次、设出口 IP、看本地执行端。
2. **服务端抓取**：nowcoder / iguopin / chinahr 三个免登录 provider，只抓 **职位列表 + 详情**。
3. **频次 + IP**：provider 级 interval/jitter；出口池（direct / http 代理），冷却与健康度。
4. **本地委派**：provider 标 `needs_local`（Boss）→ 任务进 `pending_local`；本地 agent 出站长轮询 claim → 执行 → report。
5. **本地 MCP**（stdio）：`boss_login`（扫码，独立登录）、`boss_login_status`、`boss_search`、`boss_job_detail`、`boss_logout`；cookie 仅存本机。
6. 一条命令起服务；README + smoke 脚本。

## 不做（isn't）
与本项目无关的业务系统、公司/企业信息抓取、关键词外部接口、归一化 1000 行 normalize、其余 12 个 provider、Postgres 默认部署、Web 控制台、桌面 GUI。

## 关键决策（assumed，可翻）
| # | 决策 | 理由 |
|---|---|---|
| D1 | 全 Python（`mcp` SDK + httpx + playwright + sqlite3） | 单语言，教学门槛低；crawler 本就是 Python |
| D2 | 默认 SQLite，SQL 保持 PG 兼容写法 | clone 即跑；`FOR UPDATE SKIP LOCKED` 在 SQLite 用事务 + `UPDATE…RETURNING` 替代 |
| D3 | 本地委派用 **local 主动拉取**，不是服务端回调 | 用户机器在 NAT 后，服务端无法直连；"调用本地 MCP" 的语义 = 派发给其 agent |
| D4 | 本地 Boss 用 HTTP+cookie（`wapi/zpgeek/search/joblist.json`、`job/detail.json`），不做 v2 纯 RPA | 够用、轻；风控/`code=37` 直接停并上报，不做回退 |
| D5 | agent 鉴权：`AGENT_TOKEN`（服务端只存哈希） | 最小可用 |
| D6 | 不带入：yupao/job51 签名密钥、`.env*`、`local.db`、spool、`.liepin-device-id` | 仓库将上 GitHub |

## 风险
- Boss 风控：本机 IP + 个人账号有封号风险 → 默认间隔 ≥5s、单并发、非 0 code 即停；README 写明合规与自担。
- Boss 接口/字段可能已变：真机手测为准，自动测试只覆盖解析与派发（用录制样本）。
- 已确认授权，可公开。

## 验证计划
1. 单测：provider 解析（录制样本）、队列 claim 并发、pacer、egress 冷却、鉴权。
2. 集成：本地 mock 站 → worker 全链路；mock agent 走 claim/report 全链路（含 need_login → 续跑）。
3. 冒烟：真实 nowcoder 抓 1 页；MCP 客户端（`mcp` SDK client）调用全部 server tools。
4. 手测：Boss 扫码登录 + 抓 1 页（需用户在场）。
