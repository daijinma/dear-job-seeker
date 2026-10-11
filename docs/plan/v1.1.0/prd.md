# dear-job-seeker — PRD v1.1.0

基线：`../v1.0.0/prd.md`。本版只写**变更**；未提及处沿用 v1.0.0（范围 isn't、风险、验证计划、D1/D2/D4/D6）。

图：`architecture.drawio`（架构 / 登录站点抓取时序 / 数据库），Pencil 需求板见仓库根 `untitled.pen`。

## 翻案：去掉服务端，只留一个本地客户端
用户决策：不需要服务端，本地 MCP 即提供全部能力。

| 项 | v1.0.0 | v1.1.0 |
|---|---|---|
| 进程 | server + local-mcp 两个 | **单进程**，stdio MCP |
| 本地委派（D3、D5） | 长轮询 claim/report + AGENT_TOKEN | **删除**；登录 provider 在同进程内执行 |
| 状态 | 含 `pending_local` | 删；保留 `pending/running/waiting_login/done/dead` |
| 表 | 含 `agents` | 删；新增 `provider_settings`（频次覆盖） |
| MCP 传输 | Streamable HTTP + stdio | 仅 stdio |
| 部署 | docker / 本机 | `pip install -e .` + MCP 客户端配置 |

## 工具（MCP）
`crawl_start(provider, keyword, city?, max_pages?)` · `task_list` · `jobs_get(task_id | provider, limit)` · `login(provider)` · `login_status(provider)` · `logout(provider)` · `set_frequency(provider, interval, jitter, max_pages)` · `egress_add(url, provider?)` · `egress_remove` · `egress_list`

## 新决策（assumed）
| # | 决策 | 理由 |
|---|---|---|
| D7 | provider 加 `needs_login` 属性；`needs_login` 且无有效 cookie → 任务 `waiting_login`，`login` 成功后自动回 `pending` | 取代 v1.0.0 的 need_login 上报往返 |
| D8 | 队列 worker 为进程内守护线程，随 MCP 进程启停；任务在库，下次启动续跑 | 无服务端的必然上限；需常驻再加 `djs worker` CLI（不预做） |
| D9 | 登录 provider 的出口默认 `direct`；代理需显式 `egress_add(url, provider="boss")` | 个人 cookie + 异地 IP 更易触发风控 |
| D10 | 仓库名 `dear-job-seeker`（私有），与目录一致 | 用户确认，kebab-case 即 GitHub 惯例 |

## 仍待确认
1. 公司代码上个人 GitHub 的授权（账号/组织）；确认前不 push。
2. 开工 gate 默认：subagent 执行 + 验证 + 改动报告 + smoke。
3. Boss 扫码与真机抓 1 页需用户在场。

## 实现状态与已知风险（2026-10-11）
- 已实现：单进程 stdio MCP（11 个工具）、队列 worker、按平台频次（类默认 → `provider_settings` 覆盖 → `min_interval` 下限）、出口池、nowcoder / iguopin / chinahr、Boss provider + 扫码登录。全量测试 77 通过；真实 nowcoder 冒烟：1 页 20 条入库。
- **Boss 抓取未验证，且有高风险**：内部实测记录——即使 cookie 有效，HTTP 直调 `joblist.json` 仍返回 `code=37 您的环境存在异常`，生产已放弃 XHR 路径，只留 CDP/DOM 人工点击流。当前 Boss provider 走 HTTP，**很可能同样 37**；37 现映射为 `LoginRequired`，会造成"登录→37→再要求登录"的循环。README 须标注 experimental，待真机验证后决定是否改 DOM 流。
- UI：`untitled.pen` 中为**设计稿，未实现**（本版交付形态是 MCP，无 GUI）。面向非技术用户的 4 屏：查找 / 结果 / 登录 BOSS / 设置；频次对用户收敛为"稳妥 / 标准 / 较快"三档，kind/auth/出口 IP 等术语不外露。逻辑图（Task A/B/C）在同一文件。
