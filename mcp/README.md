# Beacon-MFG 只读 MCP 服务

让任意支持 MCP 的主流 agent（Claude Desktop / Cline / Continue / WorkBuddy 等）能够**检索与调用**
已经发布到 Cloudflare Pages（或 GitHub）的灯塔工厂供应商数据，**而无需 clone 整个仓库、无需任何密钥**。

> 设计边界（用户 2026-09-18 明确约定）：MCP 只做**只读检索**。后端数据采集（`fetch_batch`）、
> 英文翻译（`en_backfill`）、库维护（`postfetch` / `gb_store` / `sync_assets`）等流水线**不进 MCP**、
> 不暴露、不持密钥；MCP 也不会落在任何写路径上。

## 三个只读 tool

| tool | 用途 | 关键参数 |
|------|------|----------|
| `search_vendors` | 按 关键词 / 城市 / 国标码 检索，返回精简档案 | `query` `city` `gb` `limit` `offset` |

> **搜索语义（v1.1.0）**：
> - `query` 支持**多词空格分词、AND 匹配**——`query="上海 酒店"` 会命中「城市=上海 且 含'酒店'」的记录（不再要求整串连续子串）。单关键词行为不变。
> - 给了 `gb`（国标码）只扫对应分片（最快，亚秒级）；**不给 `gb` 时走进程内全量索引**（首次构建后常驻内存，git 模式还按 HEAD 缓存到磁盘，重启也秒开）。彻底消除了早期「逐分片 `git show` 全扫 267 分片 ≈ 100s」的卡顿。
> - 推荐 agent 优先用 `gb` 缩小范围；不确定类别时再用关键词全量搜。
| `get_vendor` | 按 id（+国标码）取完整中文档案 | `id`（必填） `gb`（可选，自动反查） |
| `get_capability_card` | 按 id 取能力卡（工艺/设备/产能/认证等） | `id`（必填） |

所有返回都是 JSON 文本。找不到时返回 `{ "error": ... }` 或 `has_card:false`，不会抛协议错。

## 安装 / 接入

服务是**零第三方依赖**的单文件（`server.py`，仅用 Python 标准库），只需 Python 3.8+。

**方式 A — 本地 git 仓库（推荐：离线 + 隐私安全 + 零 CF 流量）**

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

MCP 服务已具备两种发布渠道（详见 `mcp/RELEASE.md`）：

- **GitHub Release（已配 CI）**：推送 `mcp-v*` 标签即由 `.github/workflows/mcp-release.yml`
  自动打包 `mcp/` 目录为 `beacon-mfg-mcp-mcp-vX.Y.Z.tar.gz` 并创建 Release。
- **npm 包（已备好）**：`mcp/package.json` + `mcp/bin/beacon-mfg-mcp.js`（Node 启动器，自动探测 Python）。
  npm 发布走独立**手动**工作流 `.github/workflows/npm-publish.yml`（GitHub Actions 页面手动触发，
  需先在仓库 Secrets 配置 `NPM_TOKEN`）；或本地 `npm login && npm publish`（在 `mcp/` 目录）。
  装好后客户端直接用 `"command": "beacon-mfg-mcp"` 即可。

### 客户端接入速查
```jsonc
// 方式一：GitHub Release / 源码 —— 直接指向 server.py
{ "mcpServers": { "beacon-mfg": { "command": "python",
    "args": ["/路径/beacon-mfg/mcp/server.py"],
    "env": { "BEACON_REPO": "/路径/beacon-mfg" } } } }

// 方式二：npm 安装后
{ "mcpServers": { "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] } } }
```
