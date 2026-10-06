# BeaconMFG MCP 接入指南（Agent 侧）

只读 MCP，让 agent 直接检索 15 万家中国工厂（按公司名 / 工艺 / 产品·材料 / 城市 / 国标码）。
**无需 API Key**。三种接入方式，从「零安装」到「完全离线」任选。

---

## 方式一 · 远程 HTTP（推荐，零安装）

最省事的一档：**不需要 Python、不需要 clone、不需要环境变量**，只填一个 URL。

客户端配置（Claude Desktop / 常见 MCP 客户端 / Cursor / Cline / Continue 等标准 MCP 客户端同形）：

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
npm i -g beacon-mfg-mcp
```

**macOS / Linux**：

```json
{ "mcpServers": { "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] } } }
```

**Windows**（必须走 `cmd /c`）：

```json
{ "mcpServers": { "beacon-mfg": {
    "command": "cmd",
    "args": ["/c", "beacon-mfg-mcp"]
} } }
```

> **Windows 上别写 `"command": "beacon-mfg-mcp"`**。npm 全局装出来的是
> `beacon-mfg-mcp.cmd` 这么一个 **`.cmd` 包装器**，而没有 shell 的
> `spawn('beacon-mfg-mcp')` 在 Windows 上解析不了 `.cmd` → **ENOENT**，客户端只表现为
> 「连不上 / 工具不出现」。`npx -y beacon-mfg-mcp` 同理（`npx` 也是 `.cmd`），
> **别把它当 Windows 的免安装方案**。2026-10-06 实测：
>
> | 写法 | Windows `spawn`（无 shell） |
> |---|---|
> | `beacon-mfg-mcp` | ❌ ENOENT |
> | `npx -y beacon-mfg-mcp` | ❌ ENOENT |
> | `cmd /c beacon-mfg-mcp` | ✅ 正常（8 个工具） |

要求本机有 **Python 3.8+**（启动器自动探测 `python3` / `python` / `py`）。

> **若你的客户端是严格分帧（Content-Length / LSP 风格）实现**，请改用方式一或方式三——
> `server.py` 的 stdio 走的是 MCP 规范的换行分隔 JSON。
> （`mcp_adapter.py` 自 1.5.4 起**两种帧都自动识别**，对外按客户端进来的那种回写，
> 所以方式三对两类客户端都可用。）

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
  "total_matched": 2,
  "results": [
    { "id": "CN-MFG-0058615", "company": "京东养车(嘉兴会展中心店)",
      "city": "嘉兴", "gb": "8111", "badge": "L0", "score": 0,
      "has_phone": true, "via": "text" }
  ],
  "order_note": "results 按召回来源分档排列：via=text → via=cap_text → via=alias → via=cap；score 是**能力画像分**（有已发布能力卡才有分，无卡为 0），**不是相关度**，不要拿它反推排序。"
}
```

- `badge` 是灯牌等级（`L0`/`L2`…）；取不到就是 `null` —— **不编造**。
- `has_phone` 表示是否有可拨号号码（仓库侧可能已按 `BEACON_MASK_PHONE` 隐私处理）。
- `via` 说明这条是**怎么被召回的**，四档纯度递减：`text`（厂名/工艺等字面命中）、
  `cap_text`（厂名里其实没这个词，只是撞上了能力键的中文词面）、`alias`（别名定向）、
  `cap`（能力定向）；只按城市筛选时是 `city`。结果就按这个次序分档排列。
- `score` 是**能力画像分**，不是相关度 —— 详见返回体里的 `order_note`。

`query` 命中偏少时返回体里会带 `hint` / `suggested_gb` / `cap_expanded`，
照它指的方向再调一次即可，不要直接判「没有」。

## 可选参数

- `BEACON_SOURCE`：改 HTTP 基址（自建域名时用），默认 `https://beacon-mfg.pages.dev`。
- `BEACON_REPO`：指向本地仓库，走离线读取。
- `BEACON_MASK_PHONE=1`：手机号隐私处理。

## 验证接入是否成功

问 agent「帮我找做注塑的供应商」或「搜一下嘉兴的修车铺」；返回结构化 JSON 即接入成功。

> 提示：`query` **直接把用户原话传进来即可**（2026-10-06 起）。
> 服务端会先做一次**归一化**：
> 1. **抽城市** —— 「找一下**苏州**做AI的企业」→ `city=苏州`，城市词不再参与关键词匹配；
> 2. **剔填充词** —— 「的 / 找 / 一下 / 企业 / 有没有」这类无信息量的词；
> 3. **认词条** —— 国标别名、国标类名、**能力词**（AI / 人工智能 / 机器学习 / 大模型…）
>    会被切出来走定向召回；剩余部分才作为通用关键词做 AND 匹配。
>
> 返回体里用 **`query_parsed`** 回显它理解成了什么：
>
> ```json
> "query_parsed": { "city": "苏州", "city_from_query": "苏州",
>                   "keywords": [], "recognized": [{ "term": "ai", "source": "cap" }] }
> ```
>
> 所以：**不必预先拆词，也不必把城市硬塞进 `query` 的同时又另传 `city`** ——
> 两种传法都对，重复也无害（显式 `city` 参数优先）。
