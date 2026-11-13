# 远程 MCP 端点（POST /mcp）

> 2026-11-13 上线。给「不想装 Node / 不会装 Node」的人一条接入路径。

## 1. 为什么要有它

在此之前分发只有 `packages.stdio` 一条：想用的人必须

```
node ≥ 18  →  npx beacon-mfg-mcp  →  客户端里配一个本地进程
```

而 Registry 首页 100 条里 **93 条**声明了 `remotes`。我们不但没有，还在用最重的那条路径。

结果是可预测的：真正想查供应商的人（采购、外贸、货代）不会为了查 20 家公司去装开发者环境。
2026-11-13 的暴露面审计显示「注册后三天，真实外部使用 0」—— 不是流量问题，是**接入路径只有一条，
而且没人走**。

远程端点把门槛从「装 Node 环境」降到「粘一个链接」。它修的是路径，不是流量。

## 2. 端点契约

```
POST https://beacon-mfg.pages.dev/mcp
Content-Type: application/json
```

传输是 **streamable-http 的无状态子集**：

| 项 | 实现 |
|---|---|
| 传输 | 单次 POST ↔ 单个 JSON 响应（`application/json`，不升级 SSE） |
| 会话 | **无** `Mcp-Session-Id`。每请求自带完整上下文 |
| `GET /mcp` | 405 + `Allow: POST`（无状态实现没有服务端推送流，不假装支持） |
| 批量 | 不支持，明确返回 `batch_not_supported`（不静默只处理第一个） |
| 通知（无 `id`） | 202 + 空 body（`notifications/initialized` 走这里） |
| 协议版本 | 认识 `2025-06-18` / `2025-03-26` / `2024-11-05`，其余回落最新 |

支持的方法：`initialize`、`ping`、`tools/list`、`tools/call`。其余 `-32601`。

### 为什么无状态

有状态要 Durable Object，而 Pages Functions 只有一个无状态 worker。
而 `tools/call` 本来就是**一问一答**，没有会话照样正确。
代价只有一个：服务端主动推送（SSE 通知、sampling）做不了 —— 那就明说 405，而不是挂一个假的 GET 长连接。

## 3. 身份与配额：纯 IP

不要求注册、不认 `X-Beacon-Cid`、不发令牌。aid 由**出口 IP** 派生：

```
ipk = base64url(sha256("ip|" + salt + "|" + ip))[:16]
aid = hex(sha256("mcp|" + ipk))[:16]      // 16 位 hex，满足 agentId 的格式要求
```

于是额度、审计、逐条计量**全部复用 `/v1` 那套**，不需要新表、新维度、新报表：

| 闸 | 值 | 说明 |
|---|---|---|
| `V1_DAILY.anon` | 1,000 条 / 24h / IP | 一条记录算一条；重复看同一家不重复计 |
| `V1_IP_DAILY` | 20,000 条 / 24h / IP | 防「同 IP 换 aid 消耗」，两条闸量同一个池子 |
| `IP_DAILY_OP` | 5,000 次操作 / 24h / IP | 防「同 IP 换 cid 刷」。**操作数与记录数是两条尺子**，混算的后果见下 |

一个真实买家一轮「搜一次 + 翻十家 + 看十张卡」≈ 21 条，1,000 条 ≈ 47 轮/天。

**换 IP 才能换额度，换 header 无效。** 顺带实测确认：外部伪造 `cf-connecting-ip` 会被
Cloudflare 直接拒（403 / error code 1000），所以这条闸没有「客户端自己声明 IP」的漏洞。

> **2026-11-13 修的一处真事故**：`beacon_ip_op` 里一行代表**一条记录**（`ipChargeV1` 按条写），
> 而门禁原来用 `COUNT(*)` 同时当「操作数」用 —— 一次检索回 200 条就抵掉 200 次操作。
> 实测本机 IP 一天取 7,351 条，把 `IP_DAILY_OP`(5000) 撞满，之后所有受门禁保护的路径
> （`/data/manifest.json`、`/skills/registry/capability/*`）全 429，部署校验读成
> 「6/19 项未通过」。现在两条尺子分开：记录数按行数（上限 20000），操作数按
> `#` 前缀归并（上限 5000），429 回执里带 `dimension: ops | records` 指明是哪一条。

### 审计口径

`logUsage` 的 `tool` 带 `mcp.` 前缀（`mcp.search_vendors` 等），`layer` 取 handler 给的
（`index` / `record` / `capability`）。这样 D1 报表里能把**远程 MCP** 与**本地 stdio / App** 分开 ——
否则「远程端点到底有没有人来」永远答不了，而那正是做这件事的原因。

工具执行抛异常时**也写一行**（status 500）。第一版在 catch 里直接 return，等于最严重的那类失败静默。

## 4. 已上线的工具（7 个：3 个取数 + 4 个寻源/指路）

| 工具 | 映射到 | 说明 |
|---|---|---|
| `search_vendors` | `handleSearch` → `/v1/search` | 关键词/城市/国标码检索 |
| `get_vendor` | `handleRec` → `/v1/rec/<token>` | 单家完整档案 |
| `get_capability_card` | `handleCap` → `/v1/cap/<token>` | 能力卡；`found:false` 是正常结果，不算错误 |
| `start_sourcing` | `handleSearch` + D1 会话 | 含糊需求 → 宽召回 + 1~2 个澄清问题，返回 `session_id` |
| `answer_sourcing` | 会话续接 + 重检 | 把澄清答案写回，**重新检索**（不是在旧结果里二次过滤） |
| `refine_sourcing` | 会话快照 | `best` / `more` / `filter` / `details` |
| `vendor_onboarding` | 纯静态文案 | 入驻/认领指路（控制台深链 + Agent HTTP 契约） |

**复用而不是重写**：检索打分、脱敏、逐条计量都在原 handler 里。在这里重写一份必然与 stdio 版分叉，
而分叉的检索结果比慢一点糟得多。实现方式见 `pages/_worker.js` 的 `mcpInnerRequest()`。

### 4.1 多轮寻源：会话在哪、活多久、谁的

协议层的「无状态」没变（仍然不签 `Mcp-Session-Id`）。多轮状态是**业务状态**，落在 D1 的
`beacon_mcp_session` 表，用 `session_id` 显式续接：

| 项 | 值 |
|---|---|
| TTL | 2 小时（`expires`），过期即读不到、并在下一次 `start_sourcing` 时被清 |
| 归属 | `aid`（由出口 IP 派生）。SQL 里带 `AND aid = ?`，别的 IP 拿到 `sid` 也读不到 |
| 容量 | 单 IP 最多 20 个活会话（按最后使用时间淘汰），一轮最多存 40 家候选 |
| 存什么 | 一句话 `demand` + 候选的**筛选字段**（名称/城市/区县/国标码/工艺/材料/认证/是否有电话/灯牌档） |
| **不存什么** | 电话、坐标、来源 URL。电话是「一条一个额度」的取证动作（`get_vendor`），存进会话等于开第二条免费取号通道 |

**城市先从需求里认。** `start_sourcing("东莞注塑代工，最好有 ISO9001")` 里的「东莞」会被
认成 `city` 过滤条件（词表直接用检索侧的 `catalog/tree.json → cities`，所以认得就一定查得到），
回执里用 `city` 字段明说是哪一座 —— 否则 agent 没法解释「为什么结果全是东莞的」。
认不出来才由澄清问题去问。

> 这条是线上实测补的：第一版没做提取，同一句需求返回的第一名是**重庆**的公司。
> 它不是「问得浅」，是**方向错了** —— 比少问一句严重得多。

**澄清问题由候选集自己生成，不是背行业知识表。** 只问三件事：城市、工艺、认证 ——
选项全部取自这一轮候选，所以不会出现「选项里没有这个城市」，也不会问名录里没有的字段
（交期、数量、开票方式一律不问）。这比本地 stdio 版（`skills/rfq-kernel/industry/*.json`
的 10 个行业 pack）问得浅，是刻意的取舍：**与其问得像人却答不出，不如只问能兑现的**。

### 4.2 没上的 4 类，以及为什么

| 工具 | 缺什么 |
|---|---|
| `read_notes` / `report_note` | 语义是「本地笔记」，远程端点没有本地盘（要么改语义，要么不做） |
| `suggest_filters` / `list_industries` | 依赖国标货架名称表（`industry-index.json` 6MB），要单独做缓存层 |

**没上的工具不出现在 `tools/list` 里** —— 让 agent 看不到，比让它调了才报错好。

### 4.3 `search_vendors` 的返回体：四个必须读的字段

检索是**字面子串**匹配，所以「结果里有货」不等于「有你要的货」。返回体里带四个信号，
agent 必须据此决定「答有 / 答无 / 答未覆盖」——只念 `total_matched` 会说错话。

| 字段 | 含义 | agent 该怎么用 |
|---|---|---|
| `total_matched` | **强匹配**条数（已被 `city`/`gb` 过滤） | 这是「有多少家」，不是 `limit` 窗口大小 |
| `weak_matched` | 只被**单字**撞上的候选数，**已从 `hits` 剔除** | 别念给用户；它说明「有共同的字，但不是这个行业」 |
| `head_missing` | 查询首词（如「律师」）**全库零记录** | 答「名录暂未收录该品类」，别说「查无此企业」 |
| `head_missing_local` | 首词全库有、但本轮 `city` 范围内**一家都没有** | 答「该地区暂未收录」，并说明另有 `partial_matched` 条被扣下 |

`coverage` 是把上面两个信号翻译成的枚举：`missing_head` / `missing_local` / `none`，
同时 `hint` 给可执行的下一步（换同义词、按 `gb` 定向、让企业自助建档）。

> **为什么必须有这四个。** 2026-11-13 实测：`search_vendors("律师事务所", city="黄浦")`
> 返回 964 条，前五名是设计事务所、健身房、星巴克、咖啡店、糕点店 —— 因为分词器把
> 「律师事务所」切成 5 个单字 + 4 个双字，而单字「事」「师」「律」在库里 df 极大
> （星巴克**久事**大厦店、鲍**师**傅）。agent 读到 964 会如实回答「黄浦有 964 家律所」。
> **真相是 0 家**：gb 7231（律师及相关法律服务）全库仅 515 家可见，集中在苏州(175)/贵阳，
> 上海一家都没有。修法有两半：排序侧「单字命中不进结果窗口」（`weak_matched` 单独报数），
> 覆盖侧「首词在本轮范围内零命中就整页扣下」（`head_missing_local`）。
>
> 同批实测的对照组：`search_vendors("盈科", city="苏州")` → 前三名正是盈科在苏州/太仓/常熟的
> 三家分所，`gb=7231`。**排序本身没坏**，坏在拿不到货时用单字凑数。

## 5. 验收清单

```bash
B=https://beacon-mfg.pages.dev

# 握手
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  $B/mcp

# 工具清单（应恰好 7 个）
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' $B/mcp

# 检索
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_vendors","arguments":{"query":"输送线","city":"上海"}}}' \
  $B/mcp

# 多轮寻源：start 拿 session_id，再 answer / refine（两条命令要在**同一个出口 IP** 下执行）
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"start_sourcing","arguments":{"demand_text":"东莞注塑代工，最好有 ISO9001"}}}' \
  $B/mcp

# 方向性检查：GET 必须 405，不是 200 也不是 404
curl -s -o /dev/null -w "%{http_code}\n" $B/mcp
```

**覆盖信号的两条反向断言**（应分别得到 `coverage=missing_local` 和 0 条结果）：

```bash
# 1) 品类有、地区没有：上海黄浦区律所 —— 全库有律师，黄浦 0 家
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"search_vendors","arguments":{"query":"律师事务所","city":"黄浦","limit":10}}}' \
  $B/mcp

# 2) 品类根本没有：返回 total_matched=0 且 coverage=missing_head
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"search_vendors","arguments":{"query":"律务","limit":5}}}' \
  $B/mcp
```

> 在 1.12.7 之前，上面两条分别返回 **964 条**和 **931 条**「结果」。
> 反向断言比正向断言重要：正向只能证明「有货时能取到」，证明不了「没货时不说假话」。

真正的客户端验收（比 curl 可信）：

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP    URL: https://beacon-mfg.pages.dev/mcp
```

## 6. 被发现：服务卡（SEP-2127）

端点存在，不等于 agent 知道它存在。**MCP 没有服务搜索引擎**：客户端启动时读的是本地
配置里已经写好的连接列表，不会联网去找有什么服务。注册到 Registry 的作用是让**人**在
目录站/客户端面板里看到条目，再点一下写进配置 —— 链条上永远有一个人。

唯一不靠人转述的机制是服务卡：

```bash
curl -s https://beacon-mfg.pages.dev/.well-known/mcp.json | jq '{name, version, remotes}'
```

```json
{
  "name": "io.github.eiry16/beacon-mfg-mcp",
  "version": "1.12.7",
  "remotes": [{ "type": "streamable-http", "url": "https://beacon-mfg.pages.dev/mcp" }]
}
```

| 项 | 值 |
|---|---|
| 路径 | `/.well-known/mcp.json`；别名 `/.well-known/mcp/server-card.json`（同一份 body） |
| 生成方式 | **由 `_worker.js` 生成**，不是静态文件；`version` / `protocolVersion` 直接引用端点常量 |
| 字段集 | SEP-2127 收敛后的范围：身份 + 远端端点。**不列工具、不声明 auth**（工具是运行时 `server/discover` 拿的） |
| 配额 | 不消耗。路由在门禁**之前** —— 发现阶段被自己的闸拦一次，等于把这条路堵死 |

**为什么两个路径都发**：草案期从 `/.well-known/mcp.json` 换到
`/.well-known/mcp/server-card.json`，目前仍在 Extensions Track 里演进。只发一个等于
赌客户端读哪个，赌错的症状是「什么都对，就是没人连」—— 没有任何报错。

**为什么不用静态文件**：① 点开头的目录在 Pages 上行为不保证（Workers Sites 明确忽略
hidden files）；② 静态文件的 version 迟早与端点漂移，而这张卡正是给「不会来问的人」看的，
编错了也没人报错。`scripts/deploy_pages.py --verify` 会把卡片版本与 `initialize` 自报
版本对比，不一致即红。

`llms.txt` 里同时给出了端点 URL 与卡片地址 —— 那是「会读本站的 agent」唯一会看的一份说明。
在补之前它只写了 `npx` 安装，于是「不想装东西」的 agent 读完也不知道该怎么连。

## 7. 已知边界

1. **无状态 = 无推送**。需要 sampling / elicitation / 服务端通知的客户端功能用不了。
2. **一次性工具调用没做去重**。客户端重试 = 重复计量（与 `/v1` 同口径，不为重试开豁免）。
3. **`initialize` 的版本号** `MCP_SERVER_VERSION` 硬编码在 `_worker.js`，与 `mcp/package.json`
   必须同步。不一致只影响回执里显示的数字，但会让「线上是不是新版」变成猜谜 —— 已纳入
   `mcp/check_release.py` 闸门。
4. **preview 环境跑不了**：Pages 的 preview 不继承 secrets，`BEACON_JWT_SECRET` 为空
   → 端点 fail-closed 返回 503 `server_misconfigured`。这是刻意的（签不出令牌就用不了），
   所以远程端点只能在生产验证。
