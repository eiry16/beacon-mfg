# Beacon-MFG 只读 MCP 服务

让任意支持 MCP 的主流 agent（Claude Desktop / Cline / Continue / 常见 MCP 客户端 等）能够**检索与调用**
已经发布到 Cloudflare Pages（或 GitHub）的灯塔工厂供应商数据，**而无需 clone 整个仓库、无需任何密钥**。

> 设计边界（用户 2026-09-18 明确约定）：MCP 只做**只读检索**。后端数据采集（`批量采集`）、
> 英文翻译（`en_backfill`）、库维护（`派生重建` / `gb_store` / `sync_assets`）等流水线**不进 MCP**、
> 不暴露、不持密钥；MCP 也不会落在任何写路径上。

## 提供的 tool（8 个，全部只读）

| tool | 用途 | 关键参数 |
|------|------|----------|
| `search_vendors` | 按 关键词 / 城市 / 国标码 检索，返回精简档案 | `query` `city` `gb` `limit` `offset` |

> **搜索语义（v1.5.5 起：原话直传）**：
> - **`query` 直接传用户原话即可**，不必自己拆词。服务端会先归一化：
>   ① **抽城市**（「找一下**苏州**做AI的企业」→ `city=苏州`，城市词不再参与关键词匹配）；
>   ② **剔填充词**（的 / 找 / 一下 / 企业 / 有没有 …）；
>   ③ **认词条**（国标别名 / 国标类名 / **能力词** 如 AI·人工智能·机器学习·大模型）→ 走定向召回；
>   剩下的部分才作为通用关键词做 AND 匹配。返回体用 `query_parsed` 回显解析结果。
>   实测：`search_vendors(query="苏州做AI的企业")` → 命中 `赤兔智能工业（苏州）有限公司`（之前是 **0 条**）。
> - 仍需注意的是**未被识别的连写词**：`query="精密加工"`（四字连写、不在别名表也不在类名里）
>   仍按**连续出现**匹配，召回少于 `"精密 加工"`（`city=苏州` 实测 2 条 vs 26 条）。
>   遇到这种生词，正确做法是**先 `suggest_filters`** 拿候选国标码再改用 `gb=`，
>   而不是反复试同义词 —— 服务端会在命中 <5 条时用 `hint` / `suggested_gb` 主动提示这条路。
> - **检索架构（v1.2.0 起：按需拉取，不再全量首次加载）**：
>   早期实现每次查询都要把**全部 fp 分片**拉下来在客户端建索引，复杂度 与命中量相关 —— 11.8 万条约 5.8s，线性外推千万级约 **491s / 2.2GB 内存**，不可用。
>   v1.2.0 改为 **与命中量相关**：索引在**发布侧预构建**（`scripts/gen_search_index.py`，随 `派生重建` 的 `searchindex` 步骤产出 `skills/数据目录/index/`），
>   MCP 只拉 `meta` + 城市表 + 命中的 1~N 个词桶（512 桶，平均每桶 ~46KB）→ 求交得到候选分片 → **只拉这些分片**做精确过滤。
>   **实测**：`贵阳+宾馆` 扫 9/267 片 1.1s、`苏州+精密加工` 扫 3/267 片 1.2s；结果与此前的全量扫描**逐条等价**（53 / 54 / 2 / 26 / 57 全部一致）。
>   索引缺失、版本不符或落后于数据时**自动回退**全量扫描（返回值里 `via_index` 标明本次是否走索引）。
>   千万量级下只需增大桶数（如 4096）并按城市对大分片二级切分，首次查询耗时仍与总量基本无关。
| `get_vendor` | 按 id（+国标码）取完整中文档案 | `id`（必填） `gb`（可选，自动反查） |
| `get_capability_card` | 按 id 取能力卡（工艺/设备/产能/认证等） | `id`（必填） |
| `suggest_filters` | **用户原话 → 候选国标小类码**（拆词 + 别名 + 类名 + **能力域**），供改用 `gb=` | `query`（必填） `city` `limit` |
| `list_industries` | **国标小类货架**（只列有数据的类，附企业条数），供按语义自己挑码 | `keyword` `parent` `limit` `offset` |
| `start_sourcing` | 【客户 agent 自动触发】识别找厂/代工/采购/询价意图 → 品类识别→宽召回→需求解析→生成 1~2 轮澄清问题，返回 `session_id` | `demand_text`（必填） `audience_id` |
| `answer_sourcing` | 续接澄清轮次：写回客户答案，返回下一轮澄清问题或初选供应商列表 | `session_id`（必填） `answers` |
| `refine_sourcing` | 推荐轮次交互：`details`(看详情与 RFQ 入口) / `more`(看更多) / `best`(看最匹配一家) | `session_id`（必填） `action` `value` |

后三个构成「采购寻源」多轮会话链路 `start_sourcing → answer_sourcing → refine_sourcing`，
同样只读，不落任何写路径。

所有返回都是 JSON 文本。找不到时返回 `{ "error": ... }` 或 `has_card:false`，不会抛协议错。

### 用户原话 → 国标码：给 agent 的调用协议（v1.5.5）

**为什么需要**：`query` 是**字面子串**匹配，而名录按国标小类归档 —— 客户说「机加工」，
库里写的是「机械零部件加工」，字面不通。实测（`city=嘉兴`）：

| 调用 | 命中 |
|---|---|
| `query="机加工"` | 1 条 |
| `gb="3484"`（机械零部件加工） | **350 条** |

**不可能靠人工扩别名表覆盖所有说法**（精密件 / 手机壳 / 来料加工…），所以把
「原话 → 国标码」这一步交给**客户端 LLM 自己**（服务端不调 LLM、不需要密钥）。

推荐 agent 按这三步走：

1. 先正常检索：`search_vendors(query=<用户原话>, city=<城市>)`。
2. **命中 < 5 条（含 0）就换码**：返回体里的 `suggested_gb`（或 `hint`）已经给了候选码；
   候选不够味就调 `suggest_filters(query, city)` 拿更多候选 —— 它按
   `alias`（人工策展）> `gb_name`（类名被完整说出）> `substring` > `segmented`（拆词）
   加权，并**优先「类名里含用户原话」的候选**（「轴承」→ 3451 滚动轴承制造）。
3. 用 `search_vendors(gb=<码>, city=...)` 重试（只扫对应分片，更快）。多个码的结果
   自行合并去重。**连候选都没有**（如「手机壳」）→ 调 `list_industries` 看货架
   （287 个有数据的类，附条数）自己挑 1~3 个码。

可直接放进 agent 系统提示词的一段：

> 检索灯塔工厂名录时，`search_vendors` 的 `query` 是字面子串匹配。若命中数少于 5 条，
> 说明用户说的是口语/采购词：先看返回的 `suggested_gb`，或调 `suggest_filters` 拿到
> 候选国标小类码，再用 `gb=` 重试；仍无候选则调 `list_industries` 按语义挑码。
> 不要因为命中少就回答「没有这类企业」。

## 安装 / 接入

服务是**零第三方依赖**的单文件（`server.py`，仅用 Python 标准库），只需 Python 3.8+。

**方式 0 — npm 安装（推荐：无需 clone、无需填路径）**

```bash
npm i -g beacon-mfg-mcp
```

macOS / Linux：

```jsonc
{
  "mcpServers": {
    "beacon-mfg": {
      "command": "beacon-mfg-mcp",
      "args": [],
      "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Windows（**必须走 `cmd /c`**）：

```jsonc
{
  "mcpServers": {
    "beacon-mfg": {
      "command": "cmd",
      "args": ["/c", "beacon-mfg-mcp"],
      "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```
> **Windows 上 `"command": "beacon-mfg-mcp"` 会 ENOENT**：npm 全局装出来的是
> `beacon-mfg-mcp.cmd`（`.cmd` 包装器），无 shell 的 `spawn()` 解析不了它，客户端只会
> 表现成「连不上」。`npx -y beacon-mfg-mcp` 同理（`npx` 自身也是 `.cmd`）。
> 2026-10-06 实测：`beacon-mfg-mcp` ❌ / `npx` ❌ / `cmd /c beacon-mfg-mcp` ✅（8 工具）。
>
> `env` 里的两个变量**不必填**——启动器（`bin/beacon-mfg-mcp.js`）已默认注入，写出仅为显式/可覆盖；
> `server.py` 另有 UTF-8 兜底，所以**即使宿主把子进程直接拉起来、没带任何环境变量**也不会乱码或崩。

不填 `BEACON_REPO` 时默认走 `https://beacon-mfg.pages.dev`，即开即用。

> **v1.3.0 起 npm 包自带 `寻源内核`**：`start_sourcing` / `answer_sourcing` / `refine_sourcing`
> 三个寻源 tool 依赖该桥接模块。`prepack` 会把仓库平级的 `skills/寻源内核` 打进包内，
> 所以 `npm i -g beacon-mfg-mcp`（≥1.3.0）后 6 个 tool 全部可用。
> ⚠️ **早于此版本的 npm 包不含 寻源内核**，那 3 个寻源 tool 会返回「桥接未就绪」——
> 要么升级到 1.3.0+，要么改用「方式 A / 方式 B」源码接入。

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

本地模式通过 `git show HEAD:<path>` 读取已提交内容——这是隐藏态手机号（138\*\*\*\*0000），
且不会触发 `maskphone` 的 smudge、不会碰 git index 锁。

> **闭合检索缺口（v1.3.0 新增）**：默认 `BEACON_WORKTREE=1`，本地模式会**优先读本地副本文件**
> （含尚未提交 / 刚修改的数据），只在文件缺失或单对象 JSON 解析失败（cron 写入中途的半成品）时
> 退回 `git show HEAD` 已提交快照，再否则走 HTTP。这样 cron 刚写入、尚未 `git commit` 的记录也能被
> `get_vendor` / `search_vendors` 检索到。设 `BEACON_WORKTREE=0` 可恢复「只读已提交快照」的严格行为
> （仍不触发 smudge / 不碰 index 锁）。

**方式 B — Cloudflare Pages 公开端点（最省事，需联网）**

不设置 `BEACON_REPO` 即可，默认基址 `https://beacon-mfg.pages.dev`。可改：

```jsonc
{ "env": { "BEACON_SOURCE": "https://你的自定义域名/" } }
```

HTTP 模式带正确 `User-Agent` 与 `ETag` 304 缓存（与 App 同款策略），并落盘 `~/.cache/beacon-mcp-cache`，
重复查询零传输。CF 部署版手机号可能是全号——隐私敏感场景请用方式 A。

> **CDN 兜底镜像（v1.3.2 新增）**：默认源 `beacon-mfg.pages.dev` 不可达（抖动/被墙）时，
> MCP 会**自动回退**到 GitHub 兜底镜像拉 `清单.json` 与分片——`cdn.jsdelivr.net/gh/eiry16/beacon-mfg@main`
> 与 `raw.githubusercontent.com/eiry16/beacon-mfg/main`（与 Android App 同策略）。因此「免 clone 检索」
> 在单一 CDN 失效时仍可工作。如需自定义/追加镜像，设 `BEACON_MIRRORS`（逗号分隔的基址列表）。
> 注意：能力卡（`get_capability_card`）不在 git 内、仅存于 CF/R2，镜像兜底覆盖的是 清单 + 分片 + 档案这类主数据；
> 在中国大陆等直连 GitHub 困难的网络，给 MCP 进程设 `HTTPS_PROXY=http://127.0.0.1:7890`（或你的代理）可让镜像链路走代理。

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
  （兜底镜像）拉 `清单.json` → 分片，用 ETag/304 增量更新。它**不调用 MCP、不共享进程/内存/文件**，
  MCP 是 agent 侧独立进程（stdio 拉起），App 根本感知不到它存在。
- 唯一共享的外部资源是 Cloudflare CDN。只要 MCP 遵守两条（发正确 UA + 复用 ETag 缓存，重活走本地 git），
  就不会触发 Cloudflare Bot Fight Mode（error 1010）——而那正是 App 更新能否成功的前提。服务已内置 UA 与缓存。

### 2) 对 cron / GUI 流水线 — 无影响（前提：遵守两条硬规则）
cron / GUI 是**写方**：`批量采集` / `派生重建` 改写 `data/gb`、`data/en`、`data/phone-index.jsonl`
（经 maskphone clean 过滤隐私处理），再发布到 R2 / Pages / GitHub（`批量采集` 还持有仓库级 PID 锁
`.批量采集.lock`）。MCP 是**只读**且不调用任何后端脚本，因此：

- **硬规则 1 — 默认只读已提交快照，本地副本读取受控**：MCP 只从「CF 公开端点」或「`git show HEAD:` 已提交内容」
  读取；`BEACON_WORKTREE=1`（默认）时额外优先读本地副本文件以闭合检索缺口，但**仅直接 `open()` 本地副本、
  不触发 `git checkout`/smudge、不碰 index 锁**，因此不会重隐藏手机号，也只在单对象 JSON 解析失败
  （半成品）时退回 HEAD，避免读到损坏内容。`BEACON_WORKTREE=0` 则恢复纯「只读已提交快照」。
- **硬规则 2 — 绝不 `git checkout` / `git restore` 本仓库**：这是历史「42578 个手机号被无声隐藏」事故的
  根因——smudge 过滤器会把 index 里的隐私处理内容写回本地副本，且 `git status` 仍显干净。本服务**只**用
  `git show HEAD:`（不触发 smudge、不碰 index 锁），从根上避开。
- 端口/进程：MCP 走 stdio（无监听端口），与后端 FastAPI（认领/RFQ）端口、cron 的 PID 锁互不冲突。
- GitHub 推送 / R2 / Pages：`step_git` 用 L0 白名单 + 隐私处理安全闸门，MCP 的存在不改变任何发布行为。

**结论**：在两条硬规则下，MCP 对 App、cron、GUI 全部零影响；这两条已写进 `server.py` 的实现与注释，
无法被误用成写操作。

## 数据源映射（与 App 完全一致）

| 数据 | 路径（相对仓库根） | 说明 |
|------|-------------------|------|
| 分片清单 | `data/清单.json` | App 增量更新的指针，已为「免 clone 检索」设计 |
| 摘要分片(fp) | `skills/数据目录/fingerprint/gb/{门}/{码}.jsonl` | 精简记录，供 `search_vendors` |
| 中文全量(zh) | `data/gb/{门}/{码前2}/{码}.json` | 完整档案，供 `get_vendor` |
| 能力卡 | `skills/数据目录/capability/{id}.json` | 不进 git，经 R2 按需提供，供 `get_capability_card` |

## 分发 / 版本发布

MCP 服务有两种发布渠道（详见 `mcp/RELEASE.md`）：

- **GitHub Release（已配 CI）**：推送 `mcp-v*` 标签即由 `.github/workflows/mcp-release.yml`
  自动打包 `mcp/` 目录为 `beacon-mfg-mcp-mcp-vX.Y.Z.tar.gz` 并创建 Release。
- **npm 包（已发布 ✅）**：`beacon-mfg-mcp` 已发布到 npm 公共仓库，当前版本 **`1.3.2`**
  （<https://www.npmjs.com/package/beacon-mfg-mcp>）。第三方可直接 `npm i -g beacon-mfg-mcp`
  或 `npx beacon-mfg-mcp`，客户端配置 `"command": "beacon-mfg-mcp"` 即可，无需 clone、无需填路径。
  **1.3.0 起 npm 包自带 寻源内核**，安装 → MCP 握手 → `tools/list` 暴露 6 个 tool 且全部可用
  （含 `start/answer/refine_sourcing` 三个寻源 tool；更早版本不含该桥接，那 3 个 tool 会降级）。

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
   npm config set //数据目录.npmjs.org/:_authToken "$NPM_TOKEN"
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
