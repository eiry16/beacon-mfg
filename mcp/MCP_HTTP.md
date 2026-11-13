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

一个真实买家一轮「搜一次 + 翻十家 + 看十张卡」≈ 21 条，1,000 条 ≈ 47 轮/天。

**换 IP 才能换额度，换 header 无效。** 顺带实测确认：外部伪造 `cf-connecting-ip` 会被
Cloudflare 直接拒（403 / error code 1000），所以这条闸没有「客户端自己声明 IP」的漏洞。

### 审计口径

`logUsage` 的 `tool` 带 `mcp.` 前缀（`mcp.search_vendors` 等），`layer` 取 handler 给的
（`index` / `record` / `capability`）。这样 D1 报表里能把**远程 MCP** 与**本地 stdio / App** 分开 ——
否则「远程端点到底有没有人来」永远答不了，而那正是做这件事的原因。

工具执行抛异常时**也写一行**（status 500）。第一版在 catch 里直接 return，等于最严重的那类失败静默。

## 4. 已上线的工具（3 个）

| 工具 | 映射到 | 说明 |
|---|---|---|
| `search_vendors` | `handleSearch` → `/v1/search` | 关键词/城市/国标码检索 |
| `get_vendor` | `handleRec` → `/v1/rec/<token>` | 单家完整档案 |
| `get_capability_card` | `handleCap` → `/v1/cap/<token>` | 能力卡；`found:false` 是正常结果，不算错误 |

**复用而不是重写**：检索打分、脱敏、逐条计量都在原 handler 里。在这里重写一份必然与 stdio 版分叉，
而分叉的检索结果比慢一点糟得多。实现方式见 `pages/_worker.js` 的 `mcpInnerRequest()`。

### 没上的 4 类，以及为什么

| 工具 | 缺什么 |
|---|---|
| `start_sourcing` / `answer_sourcing` / `refine_sourcing` | 有状态多轮，会话要先落到 D1 |
| `vendor_onboarding` | 写操作，远程需要防滥用（现在只开了读） |
| `read_notes` / `report_note` | 语义是「本地笔记」，远程端点没有本地盘 |
| `suggest_filters` / `list_industries` | 依赖国标货架名称表（`industry-index.json` 6MB），要单独做缓存层 |

**没上的工具不出现在 `tools/list` 里** —— 让 agent 看不到，比让它调了才报错好。

## 5. 验收清单

```bash
B=https://beacon-mfg.pages.dev

# 握手
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  $B/mcp

# 工具清单（应恰好 3 个）
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' $B/mcp

# 检索
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_vendors","arguments":{"query":"输送线","city":"上海"}}}' \
  $B/mcp

# 方向性检查：GET 必须 405，不是 200 也不是 404
curl -s -o /dev/null -w "%{http_code}\n" $B/mcp
```

真正的客户端验收（比 curl 可信）：

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP    URL: https://beacon-mfg.pages.dev/mcp
```

## 6. 已知边界

1. **无状态 = 无推送**。需要 sampling / elicitation / 服务端通知的客户端功能用不了。
2. **一次性工具调用没做去重**。客户端重试 = 重复计量（与 `/v1` 同口径，不为重试开豁免）。
3. **`initialize` 的版本号** `MCP_SERVER_VERSION` 硬编码在 `_worker.js`，与 `mcp/package.json`
   必须同步。不一致只影响回执里显示的数字，但会让「线上是不是新版」变成猜谜 —— 已纳入
   `mcp/check_release.py` 闸门。
4. **preview 环境跑不了**：Pages 的 preview 不继承 secrets，`BEACON_JWT_SECRET` 为空
   → 端点 fail-closed 返回 503 `server_misconfigured`。这是刻意的（签不出令牌就用不了），
   所以远程端点只能在生产验证。
