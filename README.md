# BeaconMFG · 供应商灯塔

**BeaconMFG (Supplier Lighthouse)** is a free, keyless, read-only MCP server that lets any AI agent search **210,000+ Chinese business entities** — suppliers, manufacturers, factories and service providers across all **GB/T 4754** industry divisions — with bilingual CN/EN records, USCC-verified identities, and a built-in RFQ sourcing kernel. Works with Claude Desktop, Cursor, Cline and any MCP-compatible client: **no install** via the hosted Streamable HTTP endpoint `https://beacon-mfg.pages.dev/mcp`, or locally with `npx -y beacon-mfg-mcp`.

*Keywords: MCP server · China suppliers · Chinese manufacturers · supplier directory · sourcing · procurement · vendor search · factory directory · B2B · GB/T 4754 · company registry.*

面向 Agent 的中国**全门类、全行业**企业与商户数据基座，按 GB/T 4754-2017 国民经济行业分类归档，收录 **210,000+ 经营主体**，中英双语。
只提供经营主体的公开记录与经营联系方式，不参与交易。

## 核心能力

- **全门类覆盖**：制造业、生活服务、批发零售、物流、餐饮、信息技术等 GB/T 4754-2017 全部门类；
  每个国标小类独立归档，字段结构统一、可直接机读。
- **Agent 原生**：官方 MCP 服务（已收录 [MCP Registry](https://registry.modelcontextprotocol.io)），
  无需注册、无需 API Key，一行命令即可让任意 MCP 客户端检索与调用。
- **智能寻源**：内置 `rfq/v1` 寻源内核——把自然语言采购需求归一成标准询价单（RFQ），
  按真实供给分布逐轮澄清，再匹配供应商能力卡。
- **认证可验证**：供应商能力卡带认证/产能/设备等结构化字段；认证必须带证书号且核验通过才「作数」，
  未核验项一律如实降级标注，绝不编造。

## 快速接入

### ① 远程端点 —— 无需安装（推荐）

```
https://beacon-mfg.pages.dev/mcp
```

Streamable HTTP，无状态，免费。粘到任何支持远程 MCP 的客户端即可
（Claude、Cursor、Cline、MCP Inspector…）。不用装 Node，不用注册，不用 API Key ——
额度按**出口 IP + 匿名身份**计（匿名 1,000 条记录/24h、绑定手机后 5,000；单 IP 另设 20,000 条硬顶；
一次检索算它返回的条数）。

已验证可用的三个工具：`search_vendors` / `get_vendor` / `get_capability_card`。
远程端点的完整契约与验收清单见 **[mcp/MCP_HTTP.md](mcp/MCP_HTTP.md)**。

### ② 本地进程（stdio）

```bash
npx -y beacon-mfg-mcp
```

客户端配置、完整工具清单（12 个）与环境变量见 **[mcp/README.md](mcp/README.md)**。

| 渠道 | 地址 |
|---|---|
| npm | <https://www.npmjs.com/package/beacon-mfg-mcp> |
| MCP Registry | `io.github.eiry16/beacon-mfg-mcp` |
| 远程端点 | <https://beacon-mfg.pages.dev/mcp> |
| Release | <https://github.com/eiry16/beacon-mfg/releases> |
| 数据门户 | <https://beacon-mfg.pages.dev> |

## 仓库内容

本仓库承载 **MCP 服务端**（[`mcp/`](mcp/)）与 **RFQ 寻源内核**（[`skills/rfq-kernel/`](skills/rfq-kernel/)）代码。
数据经由线上服务分发，不随仓库提供。

## 仓库定位与公开面边界

本仓库是 **MCP 的最小分发面**，不是项目的完整源码仓库。公开仓库里只允许三类文件：

| 类别 | 路径 | 为什么能留 |
| --- | --- | --- |
| MCP 运行时 | `mcp/` | npm 包本体，客户端装上要能跑 |
| 寻源内核 | `skills/rfq-kernel/` | `prepack` 从这里拷贝，缺了三个寻源工具会降级 |
| 发布链 / 法律 / 展示 | `.github/`、`.gitignore`、`LICENSE`、`README.md`、`DATA_LICENSE.md`、`glama.json` | 发版、闸门与对外声明 |

**不入库**（留在本地或私有仓库）：后端、采集/富化脚本、边缘 Worker、App、设计文档、数据面、
运行时数据，以及所有非分发件。

> **备份 ≠ 公开。** 需要版本历史，就在各自目录单独 `git init` 或推到私有仓库。

**这条规则由机器守，不靠人记**：任意分支 push 或开 PR 时，
`repo-boundary.yml` 会跑 `.github/scripts/check_repo_boundary.py`，用白名单核对 `git ls-files`：

- 新增跟踪文件**默认被拦**，必须显式改白名单并在提交里说明理由；
- 派生副本等禁入路径优先级高于白名单（防止宽前缀被借用）；
- `.gitignore` 的关键忽略行被删也会让检查变红。

本机另有一道 `pre-commit` 钩子调用同一个脚本（钩子与 clean filter 一样不随 clone 传播，换机器需重挂）。

## 数据与许可

- 代码：MIT（[LICENSE](LICENSE)）
- 数据：CC BY 4.0（[DATA_LICENSE.md](DATA_LICENSE.md)）

名录数据由 <https://beacon-mfg.pages.dev> 提供。
