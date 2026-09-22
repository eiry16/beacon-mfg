# rfq-kernel（已移植进 beacon-mfg）

原 `rfq-kernel/` 原型（需求侧接客引擎）整体移植到 beacon-mfg，作为
`skills/rfq-kernel/`。后续修改都在 beacon-mfg 项目内进行。

## 目录

```
skills/rfq-kernel/
  SKILL.md                        # agent 侧 skill 说明，含与能力卡的关系
  demo.py                         # 端到端演示（示例场景 + 认证强校验 + 真实卡接入）
  schema/
    rfq.schema.json               # 标准询价单 RFQ
    envelope.schema.json          # 工厂响应统一信封
    supplier_card.schema.json     # 标准化供应商卡（含认证对象强校验）
  industry/                       # 行业 pack：drinkware / mattress / sheet_metal
  audience/                       # 客户视图：intl_buyer / domestic_downstream
  suppliers/                      # 示例供应商卡（认证已改为对象格式）
  src/
    kernel.py                     # 协议内核 + cert_satisfied
    intake.py                     # 需求侧流程（解析/召回/澄清/精筛/投影）
    beacon_adapter.py             # beacon-mfg L1 能力卡 → 标准供应商卡（单向）
```

## 三个关键结论（对应主人的三问）

1. **固化接入**：rfq-kernel 现在住在 beacon-mfg 内，通过 `beacon_adapter.py`
   单向消费 beacon-mfg 的真实能力卡，不重写、不替代它。

2. **认证必须三方可验证**：`certifications` 改为对象 `{code, cert_no, issuer,
   valid_until, verified}`。`cert_satisfied()` 规定——**只有 `verified=true`
   且 `cert_no` 非空才「作数」**。ISO9001 这类认证需要上传证书号并被核验才算数，
   裸声称等同没有（见 demo 的「认证强校验单元」）。

3. **它和能力卡不是一回事，不能直接替代**：
   - beacon-mfg 能力卡 = 供给数据（工厂能做什么），由采集/认领流水线生成。
   - rfq-kernel = 需求解析 + 匹配引擎，能力卡是它的输入。
   - **能跑通**：`beacon_adapter` 把真实卡转成标准供应商卡后，内核零改动即可匹配
     （新增 `sheet_metal` pack 验证）。但今天 beacon-mfg 自动卡 `limits` 全 null、
     `certifications` 为 null，**匹配会优雅降级**——交期/认证标「需向工厂确认」，
     而非编造数字。这正是「静态优先、动态降级」的设计。

## 验证

```bash
cd skills/rfq-kernel && PYTHONIOENCODING=utf-8 python demo.py
```

预期：示例场景 3 家可承接 / 2 家结构化拒单；认证强校验三档（满足/未核验/缺失）；
真实卡 `CN-MFG-0029509` 经适配器接入后工艺匹配但硬指标全空 → 降级不中断；
跨行业 mattress 复用同一内核。

## 生产接线（怎么真正跑起来）

两层协作，互不替代：

| 层 | 位置 | 职责 |
|---|---|---|
| 需求侧匹配 | `skills/rfq-kernel/`（本技能） | 模糊需求 → 宽召回 → 澄清 → 精筛 → 结构化 RFQ 信封 + 候选 |
| 供给侧投递 | `server/routers/rfq.py`（`POST /v1/rfq`） | 读候选卡的 `rfq` 块拿入口 → 投递 → 收讫确认 → 审计存证 |

- **适配器读权威卡路径**：`beacon_adapter.load_real_card()` 优先读
  `skills/registry/capability/{id}.json`（与 `rfq.py` 投递读取同源），`skills/vendors/{id}/`
  兜底。生产环境不要再 grep `data/gb` 分片。
- **适配器携带三个关键信息**：能力卡上的 `rfq` 块（`endpoint`/`protocol`，决定能否投递）、
  `claim.status`（信任等级锚点）、`quality.certifications`（按 `evidence` + `number`
  判定 `verified`）。
- **客户 Agent 流程**：rfq-kernel 匹配出候选 → 对带 `rfq` 块的候选调用 `POST /v1/rfq`
  投递（平台中转：强制 Bearer 凭证、审计存证、非本机地址默认不真实投递）。

## 企业怎么"按新方案建立 skill"（空 skill 无意义 / 认领入口已存在）

- **不用建空 skill**：框架就是能力卡本身。自动采集的卡 `claim.status=unclaimed`、
  limits 全 null，正是 14000+ 家爬来企业的现状——再写空 skill 只是重复现状。
- **正确做法 = 认领后自填**：走 `server/routers/claim.py` 微信验证码认领流
  （App 端入口，对接 `docs/vendor-onboarding-design.html`），`claim.status` 由
  `unclaimed→claimed→verified`，随后在卡上补全 `limits` / `materials` /
  `quality.certifications` / `rfq` 块即可。
- **RFQ 可达性 = 能力卡上的 `rfq` 块**（`{schema:"rfq/v1", protocol, endpoint}`）。
  全库已有 7 家声明了 `rfq` 块，含两家非采集真实企业：苏州赤兔 `CN-I-0000001`、
  耐特斯 `CN-MFG-0020317`。
- `schema/supplier.schema.json` 的 `agent.capabilities`（含 `rfq`）是 L0 记录上的
  Phase-1 预留字段，当前未启用；**生效的 RFQ 能力位是能力卡上的 `rfq` 块**。

## 双源重叠已修复（mattress）

床垫 pack 曾把 `BS5852/CA TB117/GB8624` 同时放进 `fire_retardant`（行业属性）和
`certifications`（内核认证），造成两套真相。已砍掉：

- `fire_retardant` 改为**物理阻燃特性**（`有阻燃处理` / `无阻燃处理` / `未指定`，`late` 档），
  不再含标准码。
- 标准合规（`BS5852/CA TB117/GB8624/CertiPUR/OEKO-TEX`）只留在 `certifications`，
  作为三方可验证的合规真相源。
- 示例卡 `suppliers/SUP-MT-001.json` 的 `industry_ext.fire_retardant` 同步改为 `["有阻燃处理"]`。
- demo 验证：`certifications_required` 出现 `BS5852`，`fire_retardant` 不再出现标准码。
