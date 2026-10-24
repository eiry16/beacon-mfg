# BeaconMFG · 供应商灯塔

面向 Agent 的中国企业与商户检索名录，按 GB/T 4754-2017 国民经济行业分类归档。
只提供企业公开的经营联系方式，不参与交易。

## 接入

MCP 服务 `beacon-mfg-mcp`：只读、无需注册、无需 API Key。

```bash
npx -y beacon-mfg-mcp
```

客户端配置、工具清单与环境变量见 **[mcp/README.md](mcp/README.md)**。

- npm：<https://www.npmjs.com/package/beacon-mfg-mcp>
- Release：<https://github.com/eiry16/beacon-mfg/releases>

## 数据与许可

- 代码：MIT（[LICENSE](LICENSE)）
- 数据：CC BY 4.0（[DATA_LICENSE.md](DATA_LICENSE.md)）

名录数据由 <https://beacon-mfg.pages.dev> 提供。本仓库只承载 MCP 服务端代码。
