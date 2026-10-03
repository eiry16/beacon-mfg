# BeaconMFG MCP 接入指南（Agent 侧）

只读 MCP，让 agent 直接检索 15 万家中国工厂（按公司名 / 工艺 / 产品·材料 / 城市 / 国标码）。
**无需 API Key**。三种接入方式，从「零安装」到「完全离线」任选。

---

## 方式一 · 远程 HTTP（推荐，零安装）

最省事的一档：**不需要 Python、不需要 clone、不需要环境变量**，只填一个 URL。

客户端配置（Claude Desktop / WorkBuddy / Cursor / Cline / Continue 等标准 MCP 客户端同形）：

```json
{ "mcpServers": { "beacon-mfg": {
    "type": "http",
    "url": "https://<公网地址>/mcp"
} } }
```

协议是标准 MCP **Streamable HTTP**（2025-03-26），任何合规客户端都能直连。
自建部署见文末「自建远程端点」。

## 方式二 · npm（本地 stdio，适合离线 / 内网）

```bash
npm i -g beacon-mfg-mcp        # 或免安装：npx beacon-mfg-mcp
```

```json
{ "mcpServers": { "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] } } }
```

要求本机有 **Python 3.8+**（启动器自动探测 `python3` / `python` / `py`）。

> **若你的客户端是严格分帧（Content-Length）实现**，请改用方式一或方式三——
> `server.py` 的 stdio 走换行分隔 JSON，不是 LSP 分帧。

## 方式三 · 标准分帧 stdio（严格客户端的本地备选）

```json
{ "mcpServers": { "beacon-mfg": {
    "command": "python3",
    "args": ["<仓库绝对路径>/mcp/mcp_adapter.py"]
} } }
```

`mcp_adapter.py` 对外讲标准 `Content-Length` 分帧、内部转发给 `server.py`，
不修改 `server.py` 任何代码。适合「必须本地跑、但客户端只认标准分帧」的组合。

## 方式四 · Git 源码（完全离线）

```bash
git clone https://github.com/eiry16/beacon-mfg.git
```

```json
{ "mcpServers": { "beacon-mfg": {
    "command": "python",
    "args": ["<仓库绝对路径>/mcp/server.py"],
    "env": { "BEACON_REPO": "<仓库绝对路径>" }
} } }
```

- `BEACON_REPO` 命中则走本地读取：**离线、零流量**。
- 想省事就把 `env` 整段删掉，退回联网模式（同方式二）。

---

## 自建远程端点

想自己对外提供 HTTP 端点（内网 / 私有化）：

```bash
MCP_PORT=8787 python mcp/http_server.py
# 客户端填 {"type":"http","url":"https://<你的地址>/mcp"}
# 健康检查：GET /health
```

`http_server.py` 只依赖 Python 标准库，复用 `server.py` 的 `_dispatch`，
业务逻辑与本地链路同一份来源。

---

## 工具与结果格式

工具（全部只读）：`search_vendors` / `get_vendor` / `get_capability_card`，
另有采购寻源多轮链路 `start_sourcing → answer_sourcing → refine_sourcing`。

`search_vendors` 返回结构（四种接入方式完全一致）：

```json
{
  "via_index": true,
  "results": [
    { "id": "CN-MFG-0058615", "company": "京东养车(嘉兴会展中心店)",
      "city": "嘉兴", "gb": "8111", "cert_level": null, "has_phone": true }
  ]
}
```

- `cert_level` 缺失即 `null` —— **不编造**。
- `has_phone` 表示是否有可拨号号码（仓库侧可能已按 `BEACON_MASK_PHONE` 脱敏）。

## 可选参数

- `BEACON_SOURCE`：改 HTTP 基址（自建域名时用），默认 `https://beacon-mfg.pages.dev`。
- `BEACON_REPO`：指向本地仓库，走离线读取。
- `BEACON_MASK_PHONE=1`：手机号脱敏。

## 验证接入是否成功

问 agent「帮我找做注塑的供应商」或「搜一下嘉兴的修车铺」；返回结构化 JSON 即接入成功。

> 提示：`query` 按**空格分词、全部命中(AND)**，多词请拆开传（`"精密 加工"` 优于 `"精密加工"`）。
