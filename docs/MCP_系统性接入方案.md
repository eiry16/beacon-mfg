# BeaconMFG MCP · 系统性接入方案

> 目标：**任何 agent、任何 MCP 客户端，从 git 或 MCP 市场装上 beacon-mfg 都能直接用**，
> 不依赖本机 Python、不依赖本地仓库、不需要读文档猜协议。

## 一、现状盘点（2026-10-03 实测）

| 事实 | 证据 |
|---|---|
| `mcp/server.py` 用**换行分隔 JSON**，非标准 MCP `Content-Length` 分帧 | `main()` 里 `for raw in sys.stdin` + `_send()` 写 `json + "\n"` |
| `mcp/http_server.py` **已实现标准 Streamable HTTP**（2025-03-26），实测 6 工具 + 真实搜索全通 | 见下方实测记录 |
| npm 包 `beacon-mfg-mcp@1.3.3` 已发布，入口是 **stdio**（拉起 `server.py`） | `mcp/bin/beacon-mfg-mcp.js` |
| `mcp_adapter.py`（本次新增）把 stdio 转标准分帧，解决本地 stdio 客户端兼容 | `mcp/mcp_adapter.py` |
| 安装指南已存在，但**主推 stdio**，HTTP 只作为「可选」附录 | `BeaconMFG_MCP_安装指南.md:45` |

### 本次 HTTP 端点实测记录

```
initialize      OK   Mcp-Session-Id 正常下发, proto=2024-11-05
tools/list      OK   6 tools: search_vendors / get_vendor / get_capability_card
                      / start_sourcing / answer_sourcing / refine_sourcing
search 苏州/面馆  OK  via_index=True  （西园寺素食-面馆、平阊园苏式面馆、姑苏桥文人苏式面馆…）
search 嘉兴/修车  OK  via_index=True  （京东养车、驰加、途虎、博澳…）
search 贵阳/米粉  OK  via_index=True  （花溪飞碗牛肉粉、老贵阳全牛粉…）
```

## 二、核心判断：为什么应当「HTTP 优先」

一个 MCP 服务要让**陌生 agent 零改造可用**，瓶颈不在功能，而在**安装摩擦**。stdio 的摩擦项有：
本地 Python 3.8+、PATH、编码（中文 Windows 默认 GBK 会乱码）、平台差异、协议分帧差异。
这些每一样都会让一部分 agent 直接失败——本次排查的 WorkBuddy 连不上，就是这条路的典型代价。

Streamable HTTP 把这些摩擦**全部消掉**：agent 侧只需一个 `{"type":"http","url":"…"}`。

因此原则：

> **官方主推 = 远程 HTTP 端点；本地 stdio = 离线/无网用户的备选。**

## 三、目标架构

```
                  ┌─────────────────────────────────┐
   客户 Agent  ──▶│  https://<公网地址>/mcp          │
   (任意 MCP 客户端)│  Streamable HTTP（标准协议）      │
                  │  http_server.py（零依赖 stdlib）  │
                  └───────────────┬─────────────────┘
                                  │ 复用 _dispatch（业务逻辑单一来源）
                                  ▼
                  ┌─────────────────────────────────┐
                  │  server.py  检索内核              │
                  │  倒排索引 / 分片 / 只读          │
                  └───────────────┬─────────────────┘
                                  ▼
                  ┌─────────────────────────────────┐
                  │  数据源（默认公网 CDN）           │
                  │  https://beacon-mfg.pages.dev    │
                  └─────────────────────────────────┘
```

关键设计：**业务逻辑只有一份**。`http_server.py` 与 `server.py` 共用 `_dispatch`/`TOOLS`，
本地与远程两条链路不会分叉，避免「远程能查、本地查不了」这类漂移。

## 四、三种安装方式（面向不同客户）

### 方式 A · 远程 HTTP（推荐，零安装）

客户端配置一行 JSON：

```json
{ "type": "http", "url": "https://<公网地址>/mcp" }
```

适用：Claude Desktop、WorkBuddy、Cursor、Cline、Continue、任何标准 MCP 客户端。
不需要 Python、不需要 clone、不需要环境变量。

### 方式 B · npm（本地 stdio，适合离线/内网）

```bash
npx beacon-mfg-mcp        # 或 npm i -g beacon-mfg-mcp
```

要求本机有 Python 3.8+（启动器自动探测 `python3`/`python`/`py`）。
**注意**：stdio 走的是换行分隔协议；如客户端是严格分帧客户端，请改用方式 A 或方式 C。

### 方式 C · 标准分帧 stdio（严格客户端的本地备选）

```json
{
  "command": "python3",
  "args": ["<repo>/mcp/mcp_adapter.py"]
}
```

`mcp_adapter.py` 对外讲标准 `Content-Length` 分帧、内部转发给 `server.py`，
是「本地 stdio + 严格客户端」场景的桥。**不修改 `server.py` 一行。**

### 方式 D · Git 源码

```bash
git clone https://github.com/eiry16/beacon-mfg.git
python mcp/http_server.py        # 起 HTTP 端点
# 或
python mcp/server.py             # 起 stdio
```

## 五、还需要补的（尚未完成，按优先级）

1. **把 HTTP 端点部署到公网**（当前只在本机 8791 端口验证过）。
   Cloudflare Worker / Pages Functions 均可承载，因 `http_server.py` 只依赖标准库，移植成本低。
2. **改写安装指南**：把 HTTP 提到「方式一」，stdio 降为备选；补上 WorkBuddy/Claude Desktop 的
   实际配置片段（`connectors/<UUID>/mcp.json` 位置或 UI 表单填法）。
3. **MCP 市场提交**：按市场要求的字段（`name`/`description`/`transport`/安装命令）整理，
   目标是让客户点一下就能装。
4. **协议一致性回归测试**：把「HTTP 端点 6 工具 + 三城市真实搜索」固化成脚本，
   每次发布前跑一遍，防止 `server.py` 改动导致远程链路悄悄坏掉。

## 六、给客户 Agent 的固定结果格式

无论走哪种传输，`search_vendors` 返回结构统一为：

```json
{
  "via_index": true,
  "results": [
    { "id": "CN-MFG-0058615", "company": "京东养车(嘉兴会展中心店)",
      "city": "嘉兴", "gb": "8111", "cert_level": null, "has_phone": true }
  ]
}
```

字段口径：`cert_level` 缺失即 `null`（**不编造**，项目红线）；`has_phone` 表示是否有可拨号号码
（仓库侧可能已按 `BEACON_MASK_PHONE` 脱敏）。
