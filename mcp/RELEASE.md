# Beacon-MFG 只读 MCP 服务 · v0.1.0

让任意支持 MCP 的主流 agent（Claude Desktop / Cline / Continue / WorkBuddy 等）能够
**检索与调用**已发布到 Cloudflare Pages（或 GitHub）的灯塔工厂供应商数据，
**无需 clone 整个仓库、无需任何密钥**。

## 设计边界
MCP 只做**只读检索**。后端数据采集、英文翻译、库维护等流水线**不进 MCP、不暴露、不持密钥**，
也不会落在任何写路径上。对 Android App 与 cron/GUI 流水线**零影响**（详见 `mcp/README.md`）。

## 三个只读 tool
| tool | 用途 | 关键参数 |
|------|------|----------|
| `search_vendors` | 按 关键词 / 城市 / 国标码 检索，返回精简档案 | `query` `city` `gb` `limit` `offset` |
| `get_vendor` | 按 id（+国标码）取完整中文档案 | `id`（必填） `gb`（可选） |
| `get_capability_card` | 按 id 取能力卡（工艺/设备/产能/认证） | `id`（必填） |

## 安装（三选一）

### 1. 从 GitHub Release 下载（推荐，零依赖）
下载本 Release 的 `beacon-mfg-mcp-mcp-v0.1.0.tar.gz`，解压后：
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
    "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] }
  }
}
```

### 3. 从源码（git clone）
```bash
git clone https://github.com/eiry16/beacon-mfg.git
# 客户端 command 指向 beacon-mfg/mcp/server.py
```

## 环境变量
- `BEACON_REPO`：本地 git 仓库路径；设了就用 `git show HEAD:` 读（推荐：离线 + 掩码态隐私安全）。
- `BEACON_SOURCE`：HTTP 基址，默认 `https://beacon-mfg.pages.dev`（可改自定义域名）。

## 许可
MIT
