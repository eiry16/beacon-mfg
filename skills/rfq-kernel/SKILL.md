---
name: beacon-mfg-rfq
description: BeaconMFG 客户侧 RFQ 接客引擎（rfq/v1）。当客户 Agent 收到模糊采购需求、需要把自然语言归一成标准询价单、按真实供给分布澄清、再匹配 beacon-mfg 供应商能力卡时使用。触发词：询价 / RFQ / 帮我找工厂 / 哪家能做 / 报价 / 采购需求。该 skill 消费 beacon-mfg 能力卡，不替代它。
agent_created: true
---

# BeaconMFG RFQ 接客引擎（rfq/v1）

把客户的模糊采购需求，变成一份**机器可读、可验证、能直接支撑匹配决策**的标准询价单，
再拿它去匹配 beacon-mfg 的供应商能力卡。它是 beacon-mfg 现有 `agent.capabilities=["rfq"]`
能力位与 `rfq/v1` 协议的具体实现。

## 它是什么、不是什么

- **不是** beacon-mfg 的能力卡（L1 `capability.json`）。能力卡是**供给数据**（工厂能做什么）。
- **是** demand 侧的**需求解析 + 匹配引擎**，能力卡是它的**输入**。两者是上下游，
  不是同一层，不能互相替代——但可以经 `src/beacon_adapter.py` 单向对接。
- 设计原则：**宽召回 → 分布驱动澄清 → 精筛 → 标准 RFQ 分发**。先松后紧，
  澄清选项来自真实供给，绝不预设空枚举。

## 协议内核（跨行业不变）

- `schema/rfq.schema.json` —— 标准询价单（RFQ）。行业 pack / 客户视图都围绕它对齐。
- `schema/envelope.schema.json` —— 工厂响应的统一信封（status / reason_code / confidence / as_of / ttl_days）。
- `schema/supplier_card.schema.json` —— 标准化的供应商卡（能力卡的匹配视图）。
- `src/kernel.py` —— provenance / resolve_term / capacity_state（动态降级）/ cert_satisfied（认证强校验）/ envelope。
- `src/intake.py` —— parse_free_text / wide_recall / suggest_clarifications / narrow / project_view。

## 三层可插拔（N+M，不是 N×M）

- **行业 pack**（`industry/<pack>.json`）：只定义差异——属性、认证取值域、vocab、DFM 规则。
  新增行业只加 pack，不碰 schema / 内核。已含 11 个 pack：
  `drinkware` / `mattress` / `sheet_metal` / `machining` / `injection` / `die_casting` /
  `electronics` / `surface_treatment` / `fasteners` / `raw_material` /
  `material_handling`（输送/物流搬运，2026-09-22 新增）。
- **客户视图**（`audience/<profile>.json`）：字段权重、术语映射、默认值、必填项调整。
  同一份工厂数据投影出多套视图，数据不复制。已含 `intl_buyer` / `domestic_downstream`。
- **认证强校验**：`certifications` 必须是对象 `{code, cert_no, issuer, valid_until, verified}`。
  **一条认证「作数」当且仅当 `verified=true` 且 `cert_no` 非空**——ISO9001 这类认证需要
  上传证书号并被核验才算数，裸声称等同没有。

## 与 beacon-mfg 能力卡的关系（关键）

beacon-mfg 的 L1 `capability.json` 是供给数据。本 skill 通过 `src/beacon_adapter.py`
**单向消费**它：

```
beacon-mfg L1 capability.json (+ 可选 L0 认证串)
        │  src/beacon_adapter.py  adapt_card()
        ▼
rfq-kernel 标准供应商卡（core + industry_ext）
        │  src/intake.py  wide_recall → narrow
        ▼
匹配结果（matched / degraded / rejected + 结构化原因码）
```

- 自动卡 `limits` 全 null、`materials` 空 → 适配器原样保留 null，匹配引擎据此
  **静态优先、动态降级**（交期/认证标「需向工厂确认」），绝不编造硬指标。
- L0 `certifications` 是裸字符串 → 转成 `verified=false` 对象（自报未核验，**不作数**）。
  只有 beacon-mfg 里 `evidence.platform_verified` / `field_audited` 覆盖到认证且带证书号时，
  才应标 `verified=true`。

## 运行演示

```bash
cd skills/rfq-kernel
PYTHONIOENCODING=utf-8 python demo.py
```

## 接入生产（已落地）

- beacon-mfg 根 `SKILL.md` 已新增「RFQ 与自动接客」章节：描述需求侧匹配（本 skill）与
  供给侧投递（`POST /v1/rfq` 中转）的两层关系，以及供应商如何"按新方案建立 skill"。
- **生效的 RFQ 能力位是能力卡上的 `rfq` 块**（`{schema:"rfq/v1", protocol, endpoint}`），
  不是 L0 记录上的 `agent.capabilities`（后者仍是 Phase-1 预留、当前未启用）。全库已有 7 家
  声明了 `rfq` 块（含两家非采集真实企业 苏州赤兔 `CN-I-0000001`、耐特斯 `CN-MFG-0020317`）。
- 客户 Agent 流程：`parse_free_text → suggest_clarifications → narrow` 产出候选 + 信封，
  再对带 `rfq` 块的候选调用 `POST /v1/rfq` 投递（平台中转：强制 Bearer 凭证、审计存证、
  非本机地址默认不真实投递）。
- 适配器在生产环境读 `skills/registry/capability/{id}.json`（与 `rfq.py` 同源），不再 grep `data/gb`。
