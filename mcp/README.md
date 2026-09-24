# Beacon-MFG 只读 MCP 服务

让任意支持 MCP 的主流 agent（Claude Desktop / Cline / Continue / WorkBuddy 等）能够**检索与调用**
已经发布到 Cloudflare Pages（或 GitHub）的灯塔工厂供应商数据，**而无需 clone 整个仓库、无需任何密钥**。

> 设计边界（用户 2026-09-18 明确约定）：MCP 只做**只读检索**。后端数据采集（`fetch_batch`）、
> 英文翻译（`en_backfill`）、库维护（`postfetch` / `gb_store` / `sync_assets`）等流水线**不进 MCP**、
> 不暴露、不持密钥；MCP 也不会落在任何写路径上。

## 提供的 tool（6 个，全部只读）

| tool | 用途 | 关键参数 |
|------|------|----------|
| `search_vendors` | 按 关键词 / 城市 / 国标码 检索，返回精简档案 | `query` `city` `gb` `limit` `offset` |

> **搜索语义（v1.1.1）**：
> - `query` 按**空格分词、要求全部命中（AND）**。例如 `query="精密 加工"` 匹配同时含「精密」和「加工」的记录；而 `query="精密加工"`（连写）只匹配四字**连续出现**的记录，召回更少。
>   **实测对照**：`city=苏州` 时 `"精密加工"` 连写仅 **2** 条，`"精密 加工"` 分词 **26** 条（更贴合「精密加工企业」意图）；单关键词（如 `"宾馆"`）分词与连写等价（`city=贵阳` 命中 57 条）。
> - **建议 agent**：把用户的多个关键词用空格拆开再传（如「上海 酒店」「精密 加工」），避免连写成一个词导致漏召回。
> - **检索架构（v1.2.0 起：按需拉取，不再全量冷启动）**：
>   早期实现每次查询都要把**全部 fp 分片**拉下来在客户端建索引，复杂度 O(数据总量) —— 11.8 万条约 5.8s，线性外推千万级约 **491s / 2.2GB 内存**，不可用。
>   v1.2.0 改为 **O(命中量)**：索引在**发布侧预构建**（`scripts/gen_search_index.py`，随 `postfetch` 的 `searchindex` 步骤产出 `skills/registry/index/`），
>   MCP 只拉 `meta` + 城市表 + 命中的 1~N 个词桶（512 桶，平均每桶 ~46KB）→ 求交得到候选分片 → **只拉这些分片**做精确过滤。
>   **实测**：`贵阳+宾馆` 扫 9/267 片 1.1s、`苏州+精密加工` 扫 3/267 片 1.2s；结果与此前的全量扫描**逐条等价**（53 / 54 / 2 / 26 / 57 全部一致）。
>   索引缺失、版本不符或落后于数据时**自动回退**全量扫描（返回值里 `via_index` 标明本次是否走索引）。
>   千万量级下只需增大桶数（如 4096）并按城市对大分片二级切分，首次查询耗时仍与总量基本无关。
| `get_vendor` | 按 id（+国标码）取完整中文档案 | `id`（必填） `gb`（可选，自动反查） |
| `get_capability_card` | 按 id 取能力卡（工艺/设备/产能/认证等） | `id`（必填） |
| `start_sourcing` | 【客户 agent 自动触发】识别找厂/代工/采购/询价意图 → 品类识别→宽召回→需求解析→生成 1~2 轮澄清问题，返回 `session_id` | `demand_text`（必填） `audience_id` |
| `answer_sourcing` | 续接澄清轮次：写回客户答案，返回下一轮澄清问题或初选供应商列表 | `session_id`（必填） `answers` |
| `refine_sourcing` | 推荐轮次交互：`details`(看详情与 RFQ 入口) / `more`(看更多) / `best`(看最匹配一家) | `session_id`（必填） `action` `value` |

后三个构成「采购寻源」多轮会话链路 `start_sourcing → answer_sourcing → refine_sourcing`，
同样只读，不落任何写路径。

所有返回都是 JSON 文本。找不到时返回 `{ "error": ... }` 或 `has_card:false`，不会抛协议错。

## 安装 / 接入

服务是**零第三方依赖**的单文件（`server.py`，仅用 Python 标准库），只需 Python 3.8+。

**方式 0 — npm 安装（推荐：无需 clone、无需填路径）**

```bash
npm i -g beacon-mfg-mcp      # 或不安装直接用 npx beacon-mfg-mcp
```

```jsonc
{ "mcpServers": { "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] } } }
```

不填 `BEACON_REPO` 时默认走 `https://beacon-mfg.pages.dev`，即开即用。

**方式 A — 本地 git 仓库（离线 + 隐私安全 + 零 CF 流量）**

```jsonc
// Claude Desktop / 其它 MCP 客户端配置
{
  "mcpServers": {
    "beacon-mfg": {
      "command": "python",
      "args": ["/绝对路径/到/beacon-mfg/mcp/server.py"],
      "env": { "BEACON_REPO": "/绝对路径/到/beacon-mfg" }
    }
  }
}
```

本地模式只通过 `git show HEAD:<path>` 读取**已提交**内容——这是掩码态手机号（138\*\*\*\*0000），
且不会触发 `maskphone` 的 smudge、不会碰 git index 锁。

**方式 B — Cloudflare Pages 公开端点（最省事，需联网）**

不设置 `BEACON_REPO` 即可，默认基址 `https://beacon-mfg.pages.dev`。可改：

```jsonc
{ "env": { "BEACON_SOURCE": "https://你的自定义域名/" } }
```

HTTP 模式带正确 `User-Agent` 与 `ETag` 304 缓存（与 App 同款策略），并落盘 `~/.cache/beacon-mcp-cache`，
重复查询零传输。CF 部署版手机号可能是全号——隐私敏感场景请用方式 A。

## 远程 HTTP 端点（Streamable HTTP）

`server.py` 本体是 **stdio**；`http_server.py` 是它的 **HTTP 传输适配层**（复用同一份 `_dispatch`/`TOOLS`，
业务逻辑一行没动，所以 stdio 与 HTTP 两条链路不会分叉）。

```bash
MCP_PORT=8787 python http_server.py      # 默认 0.0.0.0:8787，端点 /mcp，健康检查 /health
```

客户端配置（`type: http` 即 Streamable HTTP）：

```jsonc
{ "mcpServers": { "beacon-mfg": { "type": "http", "url": "https://<你的地址>/mcp" } } }
```

**先用隧道验证**（零成本、今天即可验证协议，但 PC 关机即失效）：

```bash
cloudflared tunnel --url http://localhost:8787     # 拿到 https://xxx.trycloudflare.com
# 客户端填 https://xxx.trycloudflare.com/mcp
```

> ⚠️ **现状说明**：`*.trycloudflare.com` 是本机隧道，**不是 7×24**。
> 若要做成真正常驻的远程端点，需要一台常驻 Python 宿主（云主机 / 容器 PaaS）。
> 注意 `server.py` 用了 `ThreadPoolExecutor` 并行拉分片，因此**不适合** Cloudflare Python Workers（beta，线程受限）。

### 已实测（本地 HTTP 全流程）

`GET /health` 200 → `POST /mcp initialize` 200 并下发 `Mcp-Session-Id` →
`notifications/initialized` **202 空体** → `tools/list` 6 个 tool →
`tools/call search_vendors{city:苏州, query:"精密 加工"}` 返回 **3742 条命中**（`via_index: true`）。

实现要点：`initialize` 响应 `protocolVersion: 2024-11-05`；本服务**不提供** server→client 的 SSE
主动推送（只读检索不需要），故 `GET /mcp` 返回 405 —— 这是 MCP 规范允许的（MAY NOT）。

## 影响评估（对现有系统为零影响）

### 1) 对 Android App（Beacon-MFG）— 无影响
- App 是**纯只读 HTTP 客户端**：运行时只从 `beacon-mfg.pages.dev`（主源）+ jsDelivr / raw.githubusercontent
  （兜底镜像）拉 `manifest.json` → 分片，用 ETag/304 增量更新。它**不调用 MCP、不共享进程/内存/文件**，
  MCP 是 agent 侧独立进程（stdio 拉起），App 根本感知不到它存在。
- 唯一共享的外部资源是 Cloudflare CDN。只要 MCP 遵守两条（发正确 UA + 复用 ETag 缓存，重活走本地 git），
  就不会触发 Cloudflare Bot Fight Mode（error 1010）——而那正是 App 更新能否成功的前提。服务已内置 UA 与缓存。

### 2) 对 cron / GUI 流水线 — 无影响（前提：遵守两条硬规则）
cron / GUI 是**写方**：`fetch_batch` / `postfetch` 改写 `data/gb`、`data/en`、`data/phone-index.jsonl`
（经 maskphone clean 过滤脱敏），再发布到 R2 / Pages / GitHub（`fetch_batch` 还持有仓库级 PID 锁
`.fetch_batch.lock`）。MCP 是**只读**且不调用任何后端脚本，因此：

- **硬规则 1 — 只读已发布快照，不碰实时工作树**：MCP 只从「CF 公开端点」或「`git show HEAD:` 已提交内容」
  读取，**绝不读 cron 正在写的实时工作树**，避免读到半成品 JSON。本服务严格按此实现。
- **硬规则 2 — 绝不 `git checkout` / `git restore` 本仓库**：这是历史「42578 个手机号被无声掩码」事故的
  根因——smudge 过滤器会把 index 里的脱敏内容写回工作树，且 `git status` 仍显干净。本服务**只**用
  `git show HEAD:`（不触发 smudge、不碰 index 锁），从根上避开。
- 端口/进程：MCP 走 stdio（无监听端口），与后端 FastAPI（认领/RFQ）端口、cron 的 PID 锁互不冲突。
- GitHub 推送 / R2 / Pages：`step_git` 用 L0 白名单 + 脱敏安全闸门，MCP 的存在不改变任何发布行为。

**结论**：在两条硬规则下，MCP 对 App、cron、GUI 全部零影响；这两条已写进 `server.py` 的实现与注释，
无法被误用成写操作。

## 数据源映射（与 App 完全一致）

| 数据 | 路径（相对仓库根） | 说明 |
|------|-------------------|------|
| 分片清单 | `data/manifest.json` | App 增量更新的指针，已为「免 clone 检索」设计 |
| 指纹分片(fp) | `skills/registry/fingerprint/gb/{门}/{码}.jsonl` | 精简记录，供 `search_vendors` |
| 中文全量(zh) | `data/gb/{门}/{码前2}/{码}.json` | 完整档案，供 `get_vendor` |
| 能力卡 | `skills/registry/capability/{id}.json` | 不进 git，经 R2 按需提供，供 `get_capability_card` |

## 分发 / 版本发布

MCP 服务有两种发布渠道（详见 `mcp/RELEASE.md`）：

- **GitHub Release（已配 CI）**：推送 `mcp-v*` 标签即由 `.github/workflows/mcp-release.yml`
  自动打包 `mcp/` 目录为 `beacon-mfg-mcp-mcp-vX.Y.Z.tar.gz` 并创建 Release。
- **npm 包（已发布 ✅）**：`beacon-mfg-mcp` 已于 **2026-09-24** 发布到 npm 公共仓库，当前版本 **`0.1.3`**
  （<https://www.npmjs.com/package/beacon-mfg-mcp>）。第三方可直接 `npm i -g beacon-mfg-mcp`
  或 `npx beacon-mfg-mcp`，客户端配置 `"command": "beacon-mfg-mcp"` 即可，无需 clone、无需填路径。
  已实测：安装 → MCP 握手 → `tools/list` 全通（暴露 6 个 tool）。

### 发布到 npm 的步骤（需要 Access Token，不是 2FA 种子）

> ⚠️ **常见误区**：npm 启用 2FA 后给的「密钥 / TOTP 种子」（64 位十六进制串）是给认证器 App 生成动态码用的，
> **它本身不能用于发布**——直接拿它当 token 会返回 `401 Unauthorized`。
> 发布必须用 npm 网站生成的 **Granular Access Token**（形如 `npm_...`）。

1. 登录 <https://www.npmjs.com> → 右上角头像 → **Access Tokens** → **Generate New Token**
   → 选 **Granular Access Token**。
2. 权限选 **Read and write**；如需 CI 自动发布，勾选 **Bypass two-factor authentication**。
3. 生成后立刻复制（只显示一次），写入仓库根 `.env`：
   ```
   NPM_TOKEN=npm_xxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
4. 本地发布（在 `mcp/` 目录）：
   ```bash
   npm config set //registry.npmjs.org/:_authToken "$NPM_TOKEN"
   npm publish --dry-run   # 先预览将要上传的文件
   npm publish
   ```
5. CI 自动发布：把该 token 配进仓库 Secrets 的 `NPM_TOKEN`，之后手动触发
   `.github/workflows/npm-publish.yml` 即可（该工作流已就位）。

### 客户端接入速查

```jsonc
// 方式一：GitHub Release / 源码 —— 直接指向 server.py（当前可用）
{ "mcpServers": { "beacon-mfg": { "command": "python",
    "args": ["/路径/beacon-mfg/mcp/server.py"],
    "env": { "BEACON_REPO": "/路径/beacon-mfg" } } } }

// 方式二：npm 安装（推荐，无需 clone、无需填路径）
{ "mcpServers": { "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] } } }
```
