# 寻源内核（需求侧接客引擎）

BeaconMFG 的**需求侧接客引擎**：把客户的自然语言采购需求，归一成一份标准询价单（RFQ），
再拿它去匹配 BeaconMFG 的供应商能力卡。协议版本 `rfq/v1`。

它是供给数据（能力卡）的**下游消费者**，不替代能力卡。

## 目录

```
skills/rfq-kernel/
  SKILL.md                        # agent 侧 skill 说明，含与能力卡的关系
  demo.py                         # 端到端演示（示例场景 + 认证强校验 + 真实卡接入）
  schema/
    rfq.schema.json               # 标准询价单 RFQ
    envelope.schema.json          # 工厂响应统一信封
    supplier_card.schema.json     # 标准化供应商卡（含认证对象强校验）
  industry/                       # 行业 pack（11 个）：drinkware / mattress / sheet_metal /
                                  #   machining / injection / die_casting / electronics /
                                  #   surface_treatment / fasteners / raw_material /
                                  #   material_handling（输送/物流搬运）
  audience/                       # 客户视图：intl_buyer / domestic_downstream
  suppliers/                      # 示例供应商卡
  src/
    kernel.py                     # 协议内核 + cert_satisfied
    intake.py                     # 需求侧流程（解析/召回/澄清/精筛/投影）
    beacon_adapter.py             # beacon-mfg L1 能力卡 → 标准供应商卡（单向）
```

## 三个设计要点

1. **宽召回 → 分布驱动澄清 → 精筛 → 标准 RFQ**
   先松后紧；澄清选项来自**真实供给分布**，绝不预设空枚举。

2. **认证必须可验证**
   `certifications` 为对象 `{code, cert_no, issuer, valid_until, verified}`。
   `cert_satisfied()` 的判定是——**只有 `verified=true` 且 `cert_no` 非空才「作数」**。
   ISO9001 这类认证需要上传证书号并被核验才算数，裸声称等同没有
   （见 demo 的「认证强校验单元」）。

3. **它和能力卡不是一回事，不能直接替代**
   - beacon-mfg 能力卡 = **供给数据**（工厂能做什么），由采集/认领流水线生成。
   - 寻源内核 = **需求解析 + 匹配引擎**，能力卡是它的输入。
   - 两者互为上下游，经 `src/beacon_adapter.py` 单向对接：真实卡转成标准供应商卡后，
     内核零改动即可匹配。
   - 能力卡 `limits` 为空时**匹配优雅降级**——交期/认证标「需向工厂确认」，
     而非编造数字。这就是「静态优先、动态降级」的设计。

## 验证

```bash
cd skills/rfq-kernel && PYTHONIOENCODING=utf-8 python demo.py
```

预期：示例场景 3 家可承接 / 2 家结构化拒单；认证强校验三档（满足/未核验/缺失）；
真实卡经适配器接入后工艺匹配但硬指标全空 → 降级不中断；
跨行业 mattress 复用同一内核。

## 企业怎么建立自己的供给信息

- **认领后自填即可**：框架就是能力卡本身。企业认领自己的卡后，在卡上补全
  `limits` / `materials` / `quality.certifications` / `rfq` 块，就能进入结构化匹配。
- **RFQ 可达性 = 能力卡上的 `rfq` 块**（`{schema:"rfq/v1", protocol, endpoint}`）。
  只有带这个块的企业才能接收结构化询价。
- `schema/supplier.schema.json` 的 `agent.capabilities`（含 `rfq`）是 L0 记录上的预留字段，
  当前未启用；**生效的 RFQ 能力位是能力卡上的 `rfq` 块**。
