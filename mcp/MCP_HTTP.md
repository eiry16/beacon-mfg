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

## 4. 已上线的工具（9 个：3 个取数 + 2 个检索辅助 + 4 个寻源/指路）

| 工具 | 映射到 | 说明 |
|---|---|---|
| `search_vendors` | `handleSearch` → `/v1/search` | 关键词/城市/国标码/**能力键 `cap`** 检索 |
| `suggest_filters` | 纯计算（内嵌词表） | 原话拆词 → 候选检索词、gb 码与能力域键，**轻量版**，见 §4.6 |
| `list_industries` | 纯计算（内嵌精简货架） | 列有企业的国标小类 + 全库条数，按语义挑码，**轻量版**，见 §4.8 |
| `get_vendor` | `handleRec` → `/v1/rec/<token>` | 单家完整档案 |
| `get_capability_card` | `handleCap` → `/v1/cap/<token>` | 能力卡；`found:false` 是正常结果，不算错误 |
| `start_sourcing` | `handleSearch` + D1 会话 | 含糊需求 → 宽召回 + 1~2 个澄清问题，返回 `session_id` |
| `answer_sourcing` | 会话续接 + 重检 | 把澄清答案写回，**重新检索**（不是在旧结果里二次过滤） |
| `refine_sourcing` | 会话快照 | `best` / `more` / `filter` / `details` |
| `get_onboarding_guide` | 纯静态文案 | 入驻/认领指路（控制台深链 + Agent HTTP 契约） |

> **命名一致性（1.12.8，当时是 7 个工具）**：前 6 个都是 `verb_noun`（search_/get_/start_/answer_/refine_），
> 第 7 个原名 `vendor_onboarding` 是名词短语 —— Glama 评审把这算作一致性扣分项。
> 改成 `get_onboarding_guide` 后与 `get_vendor` / `get_capability_card` 同构：都是「取一份已有的东西」，
> 而它确实是纯静态文案、不发写请求。**旧名留作隐藏别名**（`tools/call` 能命中、`tools/list` 不出现），
> 已按旧名配好白名单的客户端不会突然拿到 `unknown tool`；等审计里旧名归零再删。

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

### 4.2 没上的工具，以及为什么

| 工具 | 缺什么 |
|---|---|
| `read_notes` / `report_note` | 语义是「本地笔记」，远程端点没有本地盘（要么改语义，要么不做） |
| `account` / `identity_status` | 远端是纯 IP 身份，没有「绑手机号升档」的本地凭证可管 |

> `suggest_filters` 与 `list_industries` 都已于 2026-11-14 上**轻量版**：
> 前者拆词 + 别名 + 类名子串（§4.6），后者的缓存层其实就是内嵌词表里那
> 418 个有企业的国标小类（§4.8）。两者都不碰 5.9MB 货架、零运行时 IO，
> 与本地 stdio 依赖完整货架的版本不是一回事，回执统一用 `coverage:"lite"`
> 明说边界。评审里「缺货架只能凭印象猜 0143」的这个代价已经消掉。

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

#### 4.3.1 `cap_auto`：品类词会被自动加权（1.12.10）

`search_vendors` 早就支持 `cap=`，但只有先调过 `suggest_filters` 的调用方才会传。
**原话直检**那条路因此只有字面 + 质量分 —— 同一个词走两条路排出来的顺序完全不一样。

1.12.10 起，不传 `cap`/`caps` 时，服务端用内嵌词表把查询词摊成能力键（最多 6 个），
作为**加权**一起下发：只加 `CAP_BOOST`，**不缩小召回面、不做过滤**，回执里带 `cap_auto`。

| 查询 | 自动解析 | 效果 |
|---|---|---|
| `兰花` / `蝴蝶兰` / `花卉` | `rtl_flowers` | 花卉绿植那批顶到前面，不再被「森兰花园」「兰花广场店」的餐饮住宿占满 |
| `医院` | `med_hospital` | 压过「…(贵州医科大学附属医院店)」这类酒店 |
| `人工智能` | `tech_ai` | 与 `suggest_filters` 给出的 `next_calls` 同口径 |

边界（三条都会在单测里钉住）：调用方显式给了 `cap`/`caps` 就**一律以调用方为准，绝不叠加**；
词表里没有的词（村名「翁贡」）**不硬塞**，宁可纯字面检索；解析异常时退回纯字面，不影响可用性。

所以 agent 读到 `cap_auto` 就该知道：**这页排序被语义加权过**，不是纯字面相关度。

### 4.4 入参别名与未知参数（1.12.9）

工具的参数名是**固定**的（`search_vendors` 用 `query`，不是 `keyword`）。为了让「差一点」
的调用不至于变成一次静默的错误结果，入口做了两件事：

| 情形 | 行为 | 回执里能看到 |
|---|---|---|
| 近义词（`keyword`/`q`/`term`/`name` → `query`；`token`/`vendor_id` → `id`；`query` → `demand_text`…） | 自动归一，调用正常执行 | `arg_aliases: {"keyword":"query"}` |
| 归一不了的陌生参数名，**且该工具的主参数为空** | 拒绝执行：`unknown_argument` + 点名正确参数名 | 不返回任何记录，**不消耗额度** |
| 陌生参数名，但主参数已给 | 放行，结果有效 | `unknown_args: [...]` + `hint` 说明「已忽略」 |
| 正式名与别名同时给 | 正式名优先，别名丢弃 | `arg_aliases` 为空 |

> **为什么必须拦。** 2026-11-14 实测：按 `keyword` 调 `search_vendors`（schema 里叫 `query`），
> 服务端把未知参数静默丢掉，只剩 `city` 成立 → 返回 3000 条与关键词毫无关系的记录
> （洗衣店 / 装饰加工厂 / 私厨），`terms_hit: 0`，**既无 error 也无 warning**。
> 这种「看起来成功」的错比 400 更坏：agent 会把「小白熊洗衣」当成素粉店念给用户。
>
> 同批还修了一个更隐蔽的：查不到的 token 走的是上游纯文本 `Not found`(404)，
> `resp.json()` 一抛就被当成「工具崩了」报 `-32603 tool_failed`（线上原话是
> `Unexpected token 'N', "Not found" is not valid JSON`）。现在统一翻成
> `{"error":"not_found", "hint":"...用 search_vendors 重新取 token..."}`。

### 4.5 回执形状：与本地 stdio 的差异（本地已改，待随下一版发版 / Worker 部署）

远端复用 `/v1/search` 的返回体（短名、压带宽），本地 stdio 用可读长名。**两套形状
没有强行统一**：`/v1` 的字段名同时被 App 与既有客户端依赖，改名等于线上破坏。
改为在远端回执里带 `schema:"v1"` 与 `field_map`，让同一段 agent 代码两边跑时能
自己对齐，而不是静默读到 `undefined`。

| 远端 | 本地 stdio | 说明 |
|---|---|---|
| `hits` | `results` | 结果数组 |
| `returned` | `returned` | 本轮**实际给出**的条数；远端 1.12.9 起补，别用数组长度猜 |
| `total_matched` | `total_matched` | 远端是「词面 ∪ 能力键」并集，不是已核实行业的企业数 |
| `has_phone` | `has_phone` | **远端不再返回 `ph`**，见下 |
| `et`(0–3) | `etype` | 主体经济类型 |
| `cl`(L0–L3) | `badge` | 灯牌档 |
| `co` | `company` | 企业名 |

**电话口径（2026-11-14 修）**：`/v1/search` 的 hits 是 fp 原记录，含 `ph` 完整号码；
远端早期把它原样透出，于是 `search_vendors` 就能免费拿到号码 —— 与 §4.1「电话是
一条一个额度的取证动作、会话快照不存电话」直接冲突。现在 `mcpSearchShape()` 在
`/mcp` 层把 `ph` 换成 `has_phone`，**只改远端回执，不动 `/v1`**（App 仍按原契约取号）。
要号码请 `get_vendor(id)`。

**解释层（同批补）**：`query_parsed`（keywords / terms_hit / head_term）、`order_note`
（排序与 score 口径）、`industry_note`（gb ≠ 已核实行业）、`returned` 都是纯运行时
计算，不需要 6MB 国标货架。`cap_expanded` / `suggested_gb` 仍只有本地 stdio 有：
远端召回层只保留 `cap_matched` 计数，没有逐能力键展开明细，补它要改召回层而不是
加字段（远端改用独立工具 `suggest_filters` 轻量版给候选码，见 §4.6）。
`hint` / `coverage` / `weak_matched` / `head_missing*` 见 §4.3，一直都在。

**空结果护栏**：`total_matched > 0` 但 `returned == 0` 时，两端都返回
`results_window_empty:true` + `warning`，禁止 agent 把上游总量念成「找到 N 家」。

### 4.6 `suggest_filters` 轻量版（2026-11-14，纯计算）

评审里那句「缺货架是猜测，有货架才是证据」是对的，但把 5.9MB 的
`industry-index.json` 搬上 worker 只为了一个提示工具，代价与收益不成比例。
做法是把**真正需要的几张小表**内联进 `_worker.js`（`MCP-GB-LITE` 区块，由
`scripts/gen_mcp_gb_lite.py` 从 `data/gb-index.json`、三层 `gb-alias*.json`、
`skills/registry/index/cap.json` 与 `province-city.json` 生成，约 170 KB，
带 `--check` 防漂移；`scripts/deploy_pages.py` 部署前会自动重跑，见 §7）：

| 表 | 内容 | 体积 |
|---|---|---|
| `MCP_GB_LITE_CLASSES` | 418 个**有企业**的国标小类：`[code, name, shelf_records, path]` | 约 20 KB |
| `MCP_GB_LITE_ALIAS` | 1445 条口语/简称别名 → 候选码 | 约 60 KB |
| `MCP_LITE_CAP_ALIAS` | 1610 个能力域口语词 → `cap` 键 / 中文名 / 全库企业数 | 约 80 KB |
| `MCP_LITE_PROVINCES` | 34 个省级短名 + 该省「索引里真有数据」的城市 | 约 2 KB |

于是零运行时 IO、零新 binding、零部署顺序依赖；词表版本
`MCP_GB_LITE_VERSION` 写进回执，数据陈旧时 agent 能看出来，不会把「候选为空」
误判成「没这个行业」。

**与本地 stdio 版最关键的一处差异：bigram 碎片不映射国标码。** 评审实测本地把
「蝴蝶兰」拆成「蝴蝶 + 兰」后撞出 `6210 / 6232 / 3484` 一串离谱候选 —— agent 会
照着调 `gb=`，拿回正餐/机械零部件加工的企业还以为自己做对了定向。这里碎片只进
`decomposed`（标 `low_confidence`，仅作换词兜底），绝不进 `candidates`；候选只从
整句、已识别词条与**实词**出发。

回执形状：

| 字段 | 含义 |
|---|---|
| `coverage: "lite"` | 与本地完整货架版区分；`lite.boundary` 写明不列全量货架、不给每类完整条数 |
| `query_parsed` | `city` / `city_from_query` / `keywords` / `recognized`（与 `search_vendors` 同字段名），另加 `province` / `province_cities` / `cap_hits` |
| `candidates[]` | `{code, name, why, term, shelf_records, confidence}`；`confidence` 最高 `medium`，**不冒充已核实行业** |
| `cap_candidates[]` + `cap_note` | 命中能力域时才给：`{cap, name, records, word}`。**cap 不是国标码**，别拿去当 `gb=`；用 `search_vendors(cap=…)` |
| `decomposed[]` | 2 字拆词碎片，只用于换词重试 |
| `next_calls[]` | 可直接照抄的 `search_vendors` 参数：最长实词、全部实词 AND、逐词单试、gb 定向、`cap` 定向、按省内城市逐个查、去掉地区限定的全国对照 |
| `hint` | 候选为空时明确「不要自己猜 gb」+「名录未收录 ≠ 企业不存在」；识别到省名时明说「省名不是 `city` 的合法取值」 |

归一顺序与本地 `_normalize_query` 一致：**先消费已识别词 → 再剔停用词 → 丢纯结构字
（的了/厂/店…）→ 把识别词补回 keywords**。顺序反了会把「机加工的厂」削成
「的厂」送去做字面 AND，白撞一次 0 命中。城市识别复用 `start_sourcing` 的
`tree.cities`，所以认出来的城市必然能被 `handleSearch` 当参数用。

**覆盖补强（第六批，2026-11-14）**：评审实测轻量版对「人工智能」「贵州的酒厂」
都给 0 候选，原因是两类缺口而不是词表不够大：

- **技术词在国标里根本没有类**。「人工智能」是能力域标签 `tech_ai`（全库 4 家），
  不是 GB/T 4754 小类。现在给 `cap_candidates` + `cap_note` +
  `next_calls` 里的 `search_vendors(cap="tech_ai")`；`search_vendors` 新增的
  `cap` / `caps` 参数直接透传 `/v1/search` 的能力索引定向（不是词面 AND）。
- **省级原话被当成一整串关键词**。「贵州的酒厂」以前整串进 `keywords`，字面 AND
  必然 0 条；现在剥掉省名（含「省/自治区」后缀）、回显 `province` 与
  `province_cities`，给「按贵阳/遵义逐个查」或「去掉地区限定」的可执行下一步。
  别名层补了策展条目「酒厂」→ `1512/1513/1511/1515`（真实酒类制造小类），
  本地 stdio 与远端共用同一份 `data/gb-alias-curated.json`，不会两边分叉。

**本工具不取企业记录，不消耗 `records` 额度**（仍计入 IP 操作数）。

### 4.7 `quota` 回执（2026-11-14，每个 tools/call 都带）

评审 P2-9：「agent 没法预判 429」。此前只有撞上 429 才知道额度见底，而一轮寻源
十几轮 `tools/call`，撞满时半轮结果已经废了。现在**所有**正常/业务错误回执都带：

```json
"quota": {
  "dimension": "records", "unit": "条",
  "used": 37, "limit": 1000, "remaining": 963,
  "window": "rolling_24h", "as_of": "before_call",
  "reset_at": "2026-11-15T13:02:11.000Z",
  "reset_note": "滚动 24h：没有统一清零时刻；额度随最早的记录满 24h 逐条释放",
  "ip": { "used": 120, "limit": 20000, "remaining": 19880 }
}
```

| 要点 | 为什么 |
|---|---|
| `dimension: records` | §3 里唯一生效的额度（`V1_DAILY.anon`），不是已下线数据面的 op 口径 |
| `ip` 是同池子的兜底闸 | 不是第二份额度，别把两个 `remaining` 相加 |
| `reset_at` = 最早记账行 + 24h | 滚动窗口**没有统一清零时刻**，写「次日 0 点」是错的，会诱导 agent 白等 |
| `as_of: before_call` | 额度在 handler 内按 hits 异步记账，回执是**调用开始时**的快照，不含本轮取数 |
| `remaining <= 50` 给 `hint` | 提前收敛 `limit` / 用 `gb` 定向 / 错峰 |
| 429 的 `error.data` 也带 `quota` | 撞满时仍能看到释放时点 |

**零额外 D1 读**：配额闸门本来就要 `v1Used(env, aid)` 查一次；这里改成
`v1QuotaState()` 读同一张表、同样行数，只是多取一列 `MIN(ts)`。§2051 反对的是
「每个 `/v1` 分片响应都算余量」（一次检索 178 片 → 178 次读），与这里无关。
D1 不可用时 `used / remaining / reset_at` 给 `null`，fail-open 不打断取数。

> **本地 stdio 侧（2026-11-14 起）也带 `quota`，但是另一条通道的缓存快照**：
> `channel:"local"` / `dimension:"ops"` / `precision:"tier_only"` /
> `used`、`remaining` 均为 `null` / `as_of:"local_cache"`。本地拉 `/data/**`
> 静态分片按 **op 计件**（anon 200 / verified 2000 次），只读本机
> `identity.json` 缓存、**不联网**（`/v1/identity/me` 有「无 UA → Cloudflare
> 1010 静默降级」的历史故障，且每个 `tools/call` 多一次 RTT 不划算），因此
> 不提供权威余量；要查权威用量调 `account(status)`（那个才联网）。
> 服务端数据面门禁默认观察模式、只记账不拦，所以 `limit` 也不是「还能调几次」。
>
> **三本账互不共享**：`/mcp` 按出口 IP 计 records（固定 anon 档 1000 条，不看
> 身份档位）；`/v1` 按身份 aid+tier 计 records（anon 1000 / verified 5000 条）；
> 本地按 op 计件。别拿一边的数字解释另一边的回执 —— 两端 `quota.note` /
> `quota.channels_note` 里都写了同一句口径说明。

### 4.8 `list_industries` 轻量货架（2026-11-14，纯计算）

评审 P1-6 要的「20KB 缓存层」不需要另起一份：§4.6 内嵌词表里的
`MCP_GB_LITE_CLASSES` 本身就是 418 个 `[code, name, records, path]`。
所以把它直接开成第 9 个工具，零运行时 IO、零新 binding：

| 项 | 值 |
|---|---|
| 参数 | `keyword`（类名/码/路径子串）、`parent`（门类字母 `C` / 大类 `01` / 中类 `014` / 小类 `0141`）、`limit`（1–200，默认 30）、`offset` |
| 回执 | `total` / `returned` / `industries[]`（`code,name,records,path`）/ `next_calls[]` / `coverage:"lite"` / `lite.boundary` |
| 排序 | `records` 降序 → `code` 升序（与本地 stdio 口径一致） |
| 计量 | **不取记录、不消耗 `records` 额度**；仍计入 IP 操作数，回执照常带 `quota` |

`list_industries(parent="01")` 返回 `0141=3 / 0164=2 / 0190=1`，正是评审对照组里
「贵阳农业总共 6 家、0143 不在货架」的那份**证据**。以前远程 agent 只能凭国标知识
猜 `0143` 再拿回 0 条；现在一步就能确认是「货架里没有这一类」。

⚠ 与本地 stdio 版的两处差异：① 只有**有企业**的精简货架，没有 5.9MB 完整
`industry-index.json`、没有门类中文路径名；② `industries[].records` 是**全库该类
企业总数**，不是本次匹配数，也不是当前 `city` 范围内的条数。挑完码仍必须
`search_vendors(gb=<code>)` 验证，别把类规模当召回结果念给用户。

## 5. 验收清单

```bash
B=https://beacon-mfg.pages.dev

# 握手
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  $B/mcp

# 工具清单（应恰好 9 个；看 quota 字段是否在每个回执里）
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' $B/mcp

# 检索
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_vendors","arguments":{"query":"输送线","city":"上海"}}}' \
  $B/mcp

# suggest_filters：应有 candidates / next_calls / coverage:lite / quota
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"suggest_filters","arguments":{"query":"找一下苏州做机加工的厂"}}}' \
  $B/mcp

# list_industries：应有 0141=3、coverage:lite、quota，且不触 /v1（纯计算）
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"list_industries","arguments":{"parent":"01"}}}' \
  $B/mcp

# 能力域定向：cap 不是国标码，走 /v1/search 的 postings/cap.po
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"search_vendors","arguments":{"cap":"tech_ai","limit":10}}}' \
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

**`suggest_filters` 的反向断言**（评审实测的 bigram 撞词，见 §4.6）：

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"suggest_filters","arguments":{"query":"蝴蝶兰"}}}' \
  $B/mcp
# 期望：candidates 为空或不含 6210/6232/3484；decomposed 有 2 字碎片；
#       hint 含「不要自己猜 gb」；next_calls 仍非空；回执带 quota。
```

**覆盖补强的两条正向断言**（第六批，见 §4.6）：

```bash
# 人工智能：cap_candidates 含 tech_ai、cap_note 明说不是国标码、
#           next_calls 有 search_vendors(cap="tech_ai")，且 candidates 不含 "tech_ai"
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"suggest_filters","arguments":{"query":"人工智能"}}}' \
  $B/mcp

# 贵州的酒厂：query_parsed.province="贵州"、keywords 不含省名/结构助词、
#            candidates 含 1512、next_calls 有按「贵阳」逐个查的对照
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"suggest_filters","arguments":{"query":"贵州的酒厂"}}}' \
  $B/mcp
```

真正的客户端验收（比 curl 可信）：

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP    URL: https://beacon-mfg.pages.dev/mcp
```

### 5.1 Windows 老工具链：curl 排障（验收前先看）

本机 `curl.exe` 走 schannel 时可能报 `SEC_E_NO_CREDENTIALS`，`-k` /
`--ssl-no-revoke` / `--http1.1` 全部无效 —— **这不是证书问题，别往 CA 上查**；
PowerShell 5.1 的 `Invoke-WebRequest` 也可能在 TLS 接收阶段就断。

可用的替代（任选其一）：

```js
// Node ≥ 18（实测 Node 24 可用），存为 probe.mjs 后执行：node probe.mjs
const r = await fetch("https://beacon-mfg.pages.dev/mcp", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }),
});
console.log(r.status, await r.text());
```

```bash
# 或走 MCP Inspector（undici，不受 schannel 影响）
npx @modelcontextprotocol/inspector
```

上面的 `curl` 命令在 Linux / macOS / WSL 里可直接跑，Windows 原生环境请用
Node 或 Inspector 复现同一组断言。

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
  "version": "1.12.10",
  "remotes": [{ "type": "streamable-http", "url": "https://beacon-mfg.pages.dev/mcp" }]
}
```

> 上面 `version` 是 2026-11-14 的线上快照；实际值由 worker 常量生成，
> 每次发版随 `MCP_SERVER_VERSION` 变化，不要照抄这个数字去客户端里做断言。

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
5. **两个检索辅助工具都是轻量版**：`suggest_filters`（拆词/别名/类名子串 +
   能力域词 + 省级表）与 `list_industries`（418 类精简货架）共用一份内嵌词表
   （约 170 KB，由 `scripts/gen_mcp_gb_lite.py` 从 `data/` 与索引生成）。
   没有 5.9MB 完整货架、不列全量行业、不给每类完整条数；候选国标码是**低置信
   推断**，必须用 `search_vendors(gb=)` 验证。数据更新后不必靠人记得重跑：
   `scripts/deploy_pages.py` 在 build / sync_worker **之前**跑生成器的 `--check`，
   漂移就自动重生成并复核，失败直接中止部署；`--verify` / `--dry-run` 只报告不写。
6. **两套 `quota` 回执口径不同，不能互相解释**：远程 `/mcp` 是按出口 IP 的
   records 条数（真实余量，随调用更新）；本地 stdio / 本地 HTTP 是只读缓存的
   op 档位上限（`used`/`remaining` 恒为 `null`、`precision:"tier_only"`、
   `as_of:"local_cache"`），且服务端数据面默认观察模式只记账不拦。本地要权威
   用量仍需 `account(status)`（唯一会联网查身份的入口）。口径说明见 §4.7。
7. **`pages/_worker.js` 与 `scripts/`、`pages/` 都被 `.gitignore` 忽略**（披露管控）：
   这两项改动只在本地 + 部署产物里，`git commit` 不会带上，必须单独部署 Pages。
   `node pages/test_mcp_p1.mjs` 是随 worker 一起维护的回归测试。
