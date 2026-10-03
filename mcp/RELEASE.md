# Beacon-MFG 只读 MCP 服务 · v1.4.2

让任意支持 MCP 的主流 agent（Claude Desktop / Cline / Continue / WorkBuddy 等）能够
**检索与调用**已发布到 Cloudflare Pages（或 GitHub）的灯塔工厂供应商数据，
**无需 clone 整个仓库、无需任何密钥**。

## 设计边界
MCP 只做**只读检索**。后端数据采集、英文翻译、库维护等流水线**不进 MCP、不暴露、不持密钥**，
也不会落在任何写路径上。对 Android App 与 cron/GUI 流水线**零影响**（详见 `mcp/README.md`）。

## 六个只读 tool
| tool | 用途 | 关键参数 |
|------|------|----------|
| `search_vendors` | 按 关键词 / 城市 / 国标码 检索，返回精简档案 | `query` `city` `gb` `limit` `offset` |
| `get_vendor` | 按 id（+国标码）取完整中文档案 | `id`（必填） `gb`（可选） |
| `get_capability_card` | 按 id 取能力卡（工艺/设备/产能/认证） | `id`（必填） |
| `start_sourcing` | 【客户 agent 自动触发】识别找厂/代工/采购意图 → 品类识别→宽召回→澄清，返回 `session_id` | `demand_text`（必填） `audience_id` |
| `answer_sourcing` | 续接澄清轮次：写回客户答案，返回下一轮澄清或初选列表 | `session_id`（必填） `answers` |
| `refine_sourcing` | 推荐轮次交互：`details`/`more`/`best` | `session_id`（必填） `action` `value` |

后三个构成「采购寻源」多轮会话链路 `start_sourcing → answer_sourcing → refine_sourcing`，
依赖仓库平级的 `skills/rfq-kernel` 桥接模块。

## 本次更新（v1.4.2）
- **自动升级（自更新）**：新增 `bin/selfupdate.js`，MCP 启动时以**独立 detached 子进程**拉起（绝不阻塞 MCP 启动），
  节流查 npm registry `latest`（默认 24h 一次，可用 `BEACON_MCP_UPDATE_INTERVAL_H` 调），发现新版本即执行
  `npm i -g beacon-mfg-mcp@<新版本>`（Windows 上正确走 `npm.cmd + shell`）。
- **跳过条件**：设 `BEACON_MCP_NO_UPDATE=1`、或已设 `BEACON_SOURCE` / `BEACON_REPO`（离线 / 本地仓库用户）时**不联网升级**，避免对本地仓库用户添乱。
- **并发保护**：原子目录锁 + 残留锁 10 分钟过期自愈（踩过死锁坑——残留锁会让之后所有启动永久跳过自更新）。
- **测试**：`test_update.js` 单元测试 23/23 通过，并以 fake-npm 拦截验证安装命令形态正确（`npm.cmd i -g beacon-mfg-mcp@1.4.2`）。
- **前向生效说明**：自更新是「向前生效」的——已装的 ≤1.4.1 旧版代码里没有 `selfupdate.js`，需**一次性手动** `npm i -g beacon-mfg-mcp@latest` 进入自动升级基线；之后永久自动。`npx beacon-mfg-mcp` 用户天然永远最新。

## 本次更新（v1.4.1，已发布）
- **直连优先 + 死代理容错**：修复「失效代理导致检索归零」——默认源不可达时直连兜底镜像，且报错信息直指真因（是代理挂了而非数据问题），`npm i -g` 装一次即享受（无需重启客户端）。

## 本次更新（v1.4.0，已发布）
- **标准分帧适配层 `mcp_adapter.py`**：补齐「严格分帧」客户端接入路径，与「换行 stdio」「Streamable HTTP」三条传输链路统一。
- **协议回归测试 `test_protocol.py`**：一条命令覆盖三条链路（握手 + 6 工具齐全 + 三城市真实检索 `via_index`），发版前必跑。

## 本次更新（v1.3.3，已发布）
- **本地检索免提交、免重启即时可见**：这是 v1.3.1「worktree 兜底」的收尾。v1.3.1 只让**单文件读**走了工作树，
  但关键词/城市检索的**召回**走的是**批量读** `_read_many_text()`——它在 git 模式下恒走 `git archive HEAD`
  （已提交快照），于是**改完数据、重建索引后，关键词检索仍读旧索引、必须 commit 才可见**。
  本次把批量读也改为「**工作树优先 → `git archive HEAD` 兜底 → HTTP**」。
- **进程内 + 磁盘缓存按文件新鲜度失效**：新增 `_wt_mtime` / `_cache_stale`，各 loader
  （manifest / 别名 / 国标名表 / cap 索引 / city / 索引桶 / fp 分片 / 能力词）改为
  **按工作树文件 mtime 判新**——派生层一重建，MCP 立刻重读，**无需重启进程**；
  磁盘索引缓存（`idx-*`）加 **mtime 印记**（`repr` 精确往返），同秒重写也能识别。
- **可选 TTL**：`BEACON_CACHE_TTL`（秒，默认 `0`=关闭）可让进程内缓存周期性刷新；
  非工作树模式（`BEACON_WORKTREE=0`）保持旧的「提交后 / 重启后生效」行为不变。
- 已用**真实注入实验**验证：单进程、不 commit、只 sleep，`data/gb` 新记录经本地重建后
  关键词检索由 `0` 命中变 `1` 命中。

## 本次更新（v1.3.2，已发布）
- **npm 启动器锁定 UTF-8**：`bin/beacon-mfg-mcp.js` 拉起 python 时注入 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，
  修复中文 Windows 上 python 按 GBK 写 stdout、被客户端按 UTF-8 解码导致的工具描述/结果乱码。
- **HTTP 兜底镜像**：默认源 `beacon-mfg.pages.dev` 不可达时，自动回退到 GitHub 镜像
  `cdn.jsdelivr.net/gh/eiry16/beacon-mfg@main` 与 `raw.githubusercontent.com/eiry16/beacon-mfg/main`
  （与 Android App 同策略），保证「免 clone 检索」在单一 CDN 失效时仍可工作；可用 `BEACON_MIRRORS` 追加。
- **serverInfo 版本对齐**：服务端自报版本改为读 `package.json`，不再硬编码，消除与 npm 包版本漂移。

## 本次更新（v1.3.1，已发布）
- **npm 包自带 rfq-kernel**：`prepack` 会把 `skills/rfq-kernel` 打进包内，因此 `npm i -g beacon-mfg-mcp`
  安装后 6 个 tool 全部可用（早于此版本的 npm 包不含该桥接，3 个寻源 tool 会降级）。
  GitHub Release tarball 同步包含 `rfq-kernel/`。
- **闭合检索缺口**：本地 git 模式默认 `BEACON_WORKTREE=1`，优先读工作树文件（含未提交/刚修改的数据），
  仅在文件缺失或单对象 JSON 解析失败（半成品）时退回 `git show HEAD`，再否则走 HTTP。
  设 `BEACON_WORKTREE=0` 可恢复「只读已提交快照」的严格行为。

## 安装（三选一）

### 1. 从 GitHub Release 下载（推荐，零依赖）
下载本 Release 的 `beacon-mfg-mcp-mcp-v1.4.2.tar.gz`，解压后：
```jsonc
{
  "mcpServers": {
    "beacon-mfg": {
      "command": "python",
      "args": ["/解压目录/server.py"],
      "env": { "BEACON_REPO": "/你的/beacon-mfg仓库路径" }   // 可选：离线 + 隐私安全
    }
  }
}
```

### 2. npm（需先 `npm login`，仓库已配 `NPM_TOKEN` 后自动发布）
```bash
npm install -g beacon-mfg-mcp
# 或一次性运行：
npx beacon-mfg-mcp
```
装好后客户端配置：
```jsonc
{
  "mcpServers": {
    "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [], "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" } }
  }
}
```

### 3. 从源码（git clone）
```bash
git clone https://github.com/eiry16/beacon-mfg.git
# 客户端 command 指向 beacon-mfg/mcp/server.py
```

## 环境变量
- `BEACON_REPO`：本地 git 仓库路径；设了就用 `git show HEAD:` + 工作树 读（推荐：离线 + 掩码态隐私安全）。
- `BEACON_SOURCE`：HTTP 基址，默认 `https://beacon-mfg.pages.dev`（可改自定义域名）。
- `BEACON_WORKTREE`：本地模式是否优先读工作树（默认 `1`；设 `0` 恢复「只读已提交快照」）。
- `BEACON_MIRRORS`：HTTP 模式追加的兜底镜像基址（逗号分隔）；默认源不可达时除内置的 jsDelivr/raw 外，可在此追加自有镜像。
- `HTTPS_PROXY` / `HTTP_PROXY`：python 子进程的代理；在中国大陆等直连 GitHub 困难的网络，设 `HTTPS_PROXY=http://127.0.0.1:7890` 可让镜像链路走代理。

## 许可
MIT
