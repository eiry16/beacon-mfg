# Beacon-MFG 只读 MCP 服务

面向 Agent 的供应商名录检索服务：**只读**，不参与交易、不写入任何数据、不持有密钥。

## 八个 tool

| tool | 用途 |
|---|---|
| `search_vendors` | 按关键词 / 城市 / 国标码检索 |
| `get_vendor` | 按 id 取完整档案 |
| `get_capability_card` | 按 id 取能力卡 |
| `suggest_filters` | 把口语需求映射成候选国标小类码 |
| `list_industries` | 浏览有数据的国标小类货架 |
| `start_sourcing` / `answer_sourcing` / `refine_sourcing` | 多轮采购寻源 |

## 安装

```bash
npm i -g beacon-mfg-mcp
```

Windows 客户端配置需经 `cmd /c`：

```jsonc
{ "mcpServers": { "beacon-mfg": { "command": "cmd", "args": ["/c", "beacon-mfg-mcp"] } } }
```

macOS / Linux：

```jsonc
{ "mcpServers": { "beacon-mfg": { "command": "beacon-mfg-mcp", "args": [] } } }
```

也可直接用本 Release 的 tarball 解压运行：

```bash
python server.py          # stdio
python http_server.py     # Streamable HTTP，端点 /mcp
```

数据默认取自公开数据源，无需密钥、无需克隆仓库。

## 本次更新

- 稳定性与接入体验改进。
