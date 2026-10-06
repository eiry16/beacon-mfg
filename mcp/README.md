# Beacon-MFG 只读 MCP 服务

让任意支持 MCP 的主流 agent（Claude Desktop / Cline / Continue / 常见 MCP 客户端 等）能够**检索与调用**
灯塔工厂供应商名录，**无需 clone 整个仓库、无需任何密钥、无需第三方依赖**。

> **边界**：本服务只做**只读检索**，不参与交易、不写任何数据、不持有任何密钥。

## 提供的 tool（8 个，全部只读）

| tool | 用途 | 关键参数 |
|------|------|----------|
| `search_vendors` | 按 关键词 / 城市 / 国标码 检索，返回精简档案 | `query` `city` `gb` `limit` `offset` |
| `get_vendor` | 按 id（+国标码）取完整中文档案 | `id`（必填） `gb`（可选，自动反查） |
| `get_capability_card` | 按 id 取能力卡（工艺/设备/产能/认证等） | `id`（必填） |
| `suggest_filters` | **把用户原话映射成候选国标小类码**，供改用 `gb=` | `query`（必填） `city` `limit` |
| `list_industries` | **国标小类货架**（只列有数据的类，附企业条数），供按语义自己挑码 | `keyword` `parent` `limit` `offset` |
| `start_sourcing` | 【客户 agent 自动触发】识别找厂/代工/采购/询价意图 → 品类识别→宽召回→需求解析→生成 1~2 轮澄清问题，返回 `session_id` | `demand_text`（必填） `audience_id` |
| `answer_sourcing` | 续接澄清轮次：写回客户答案，返回下一轮澄清问题或初选供应商列表 | `session_id`（必填） `answers` |
| `refine_sourcing` | 推荐轮次交互：`details`(看详情与 RFQ 入口) / `more`(看更多) / `best`(看最匹配一家) | `session_id`（必填） `action` `value` |

后三个构成「采购寻源」多轮会话链路 `start_sourcing → answer_sourcing → refine_sourcing`，同样只读。

所有返回都是 JSON 文本。找不到时返回 `{ "error": ... }` 或 `has_card:false`，不会抛协议错。

## 搜索语义

**`query` 直接传用户原话即可**，不必自己拆词。服务端会先归一化：

1. **抽城市** —— 「找一下**苏州**做AI的企业」→ `city=苏州`，城市词不再参与关键词匹配；
2. **剔填充词** —— 的 / 找 / 一下 / 企业 / 有没有 …；
3. **认词条** —— 国标别名 / 国标类名 / **能力词**（如 AI·人工智能·机器学习·大模型）→ 走定向召回；
4. 剩下的部分才作为通用关键词做 **AND** 匹配。

返回体用 `query_parsed` 回显解析结果（抽走了哪个城市、剔了哪些词、认出了哪些词条），
便于调用方判断「命中少」到底是没这类企业、还是这句话没被读懂。

**结果顺序**：`results` 先按**召回来源**分档（`via=text` 字面命中 → `via=cap_text` 只撞能力词面
→ `via=alias` 别名定向 → `via=cap` 能力定向），**同一档内**再按「关键词在**厂名**里成片出现」优先。

> `score` 是**能力画像分**（有已发布能力卡才有分，无卡为 0），**不是相关度**，不要拿它反推排序；
> 返回体的 `order_note` 里写死了这个口径。

**遇到生词（命中很少）怎么办**：`query` 是字面子串匹配，而名录按国标小类归档 ——
客户说「机加工」，库里写的是「机械零部件加工」，字面不通。

1. 先正常检索：`search_vendors(query=<用户原话>, city=<城市>)`。
2. **命中 < 5 条（含 0）就换码**：返回体里的 `suggested_gb`（或 `hint`）已给出候选码；
   不够味就调 `suggest_filters(query, city)` 拿更多候选。
3. 用 `search_vendors(gb=<码>, city=...)` 重试。**连候选都没有**（如「手机壳」）→
   调 `list_industries` 看货架自己挑 1~3 个码。

> 可直接放进 agent 系统提示词：*检索灯塔工厂名录时，若命中数少于 5 条，说明用户说的是口语/采购词：
> 先看返回的 `suggested_gb`，或调 `suggest_filters` 拿候选国标小类码，再用 `gb=` 重试；
> 仍无候选则调 `list_industries` 按语义挑码。**不要因为命中少就回答「没有这类企业」**。*

## 安装 / 接入

服务是**零第三方依赖**的单文件（`server.py`，仅用 Python 标准库），只需 Python 3.8+。

### 方式一 · npm 安装（推荐：无需 clone、无需填路径）

```bash
npm i -g beacon-mfg-mcp
```

**macOS / Linux**：

```jsonc
{
  "mcpServers": {
    "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] }
  }
}
```

**Windows（必须走 `cmd /c`）**：

```jsonc
{
  "mcpServers": {
    "beacon-mfg": { "command": "cmd", "args": ["/c", "beacon-mfg-mcp"] }
  }
}
```

> ⚠️ **Windows 上写 `"command": "beacon-mfg-mcp"` 会 ENOENT**：npm 全局装出来的是
> `beacon-mfg-mcp.cmd`（`.cmd` 包装器），不经过 shell 的进程启动方式解析不了它，
> 客户端只会表现成「连不上 / 工具不出现」。`npx -y beacon-mfg-mcp` 同理（`npx` 自身也是 `.cmd`）。
> 用上表的 `cmd /c` 写法即可。

> 中文 Windows 无需额外设置编码：启动器已为 Python 子进程注入 UTF-8，`server.py` 另有兜底，
> 即使宿主没带任何环境变量也不会乱码或崩。

不填 `BEACON_REPO` 时默认走 `https://beacon-mfg.pages.dev`，即开即用。

### 方式二 · 本地 git 仓库（离线可用）

```jsonc
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

### 方式三 · 远程 HTTP 端点（Streamable HTTP）

`server.py` 本体是 stdio；`http_server.py` 是它的 HTTP 传输适配层（同一套业务逻辑）。

```bash
MCP_PORT=8787 python http_server.py      # 默认 0.0.0.0:8787，端点 /mcp，健康检查 /health
```

```jsonc
{ "mcpServers": { "beacon-mfg": { "type": "http", "url": "https://<你的地址>/mcp" } } }
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `BEACON_REPO` | 指定本地仓库根，改走本地只读模式（离线可用） |
| `BEACON_SOURCE` | 覆盖默认数据源基址（HTTP 模式） |
| `BEACON_MIRRORS` | 追加备用基址（逗号分隔），主源不可达时自动回退 |
| `BEACON_MASK_PHONE` | 置 `1` 时对手机号隐私处理输出 |

## 分发与版本

两种发布渠道，版本号一致：

- **npm**：`beacon-mfg-mcp`（<https://www.npmjs.com/package/beacon-mfg-mcp>）
- **GitHub Release**：<https://github.com/eiry16/beacon-mfg/releases>（`mcp-v*` 标签）

变更说明见本目录 `RELEASE.md`。
