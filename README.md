# BeaconMFG · 供应商灯塔

面向 Agent 的中国**全门类、全行业**企业与商户数据基座，按 GB/T 4754-2017 国民经济行业分类归档。
只提供经营主体的公开记录与经营联系方式，不参与交易。

## 产品定位（勿再按「制造业」理解）

本项目的定位是**服务所有行业门类的数据基座**，制造业只是其中一个门类。

- 按门类看，名录里生活服务业（餐饮、住宿、美容、维修、批发零售等）占比高于制造业。
  **这是正确的开发方向，不是数据偏差，也不是需要"纠正"的设计问题。**
- 判断数据质量的标准不是"制造业占多少"，而是**每个国标小类里的商户是否被准确归档、字段是否可用**。
- 「BeaconMFG / 供应商灯塔」是历史命名，不构成业务边界。对外描述时用「企业与商户数据基座」，
  不用「制造业供应商名录」。

## 仓库定位（重要：GitHub 上只放最小集）

**本仓库只承载 MCP 客户端能跑起来所需的最小代码集**，不是项目的完整源码仓库。

| 目录 | 是否入 GitHub | 说明 |
|---|---|---|
| `mcp/` | ✅ | MCP 服务端（分发主体） |
| `skills/rfq-kernel/` | ✅ | RFQ 内核（MCP 依赖的分发件） |
| `README / LICENSE / DATA_LICENSE` | ✅ | 许可与接入说明 |
| `server/` | ❌ | FastAPI 后端，只在本地/云主机 |
| `scripts/` | ❌ | 采集、富化、发布流水线 |
| `pages/` | ❌ | 边缘 Worker 与 Web 页面 |
| `APK/` | ❌ | 安卓 App |
| `docs/` | ❌ | 设计文档与运营材料 |
| `data/` `skills/registry/` `server/.data/` | ❌ | 数据面与运行时数据（含 PII） |

这条规则已经反复确认过多次，**不要"顺手"把非分发目录 git add 进来**。
如果确实需要给某个后端/脚本留历史，在其自身目录内单独 `git init`，不要混进这个分发仓库。

## 脱敏边界（同样容易搞错）

号码**按原样保留**是既定设计，不是遗漏。

- 数据面（Cloudflare Worker）才是号码的访问控制点：tier 门禁 + 按条计费 + 分页配额。
  传输层做字段级脱敏会同时废掉 App 拨号与客户 Agent 的直连需求，因此**不做**。
- 脱敏只适用于**对外开源分发的那一小块**（即上表 ✅ 的部分），由 `scripts/mask_phones.py`
  作为 git clean filter 实现——这是 GitHub 分发时期的遗留要求。
- 相关历史教训见 `docs/`；不要再给 `/suppliers` 之类的业务接口加字段级掩码。

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
