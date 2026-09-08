# rfq/v1 · R1 询价投递与收讫确认

> 状态：实现中 v0.1 · 2026-09-08
> 范围：**R1 = 投递 + 收讫确认**。不做自动报价，不做议价，不下单。
> 上层：`docs/PROPOSAL_VENDOR_SKILL.md §9`（原始 rfq/v1 定义）、`docs/PROPOSAL_A2A_OUTBOUND.md`（传输层）

---

## 1. R1 的边界（先说清楚不做什么）

| 能做 | 不做（留到 R2/R3） |
|---|---|
| 生成标准 RFQ 信封 | 自动报价（`quote`） |
| 投递到厂商声明的入口 | 多轮议价 |
| 收到收讫确认（收没收到、多久回） | 自动下单 / 合同 |
| 查询某条 RFQ 的状态 | 资金、物流、履约 |

**红线：R1 不产生任何有法律效力的承诺。**
投递一条 RFQ 只是"询问"，ack 只是"收到了"，都不构成要约或承诺。
任何涉及价格数字的动作都属于 R2，且必须经人工确认。

---

## 2. 信封

```jsonc
{
  "rfq_version": "1.0",
  "rfq_id": "RFQ-20260908-7F3A2B",        // 幂等键，全局唯一
  "issued_at": "2026-09-08T18:00:00+08:00",
  "expires_at": "2026-09-11T18:00:00+08:00",

  "buyer": {
    "agent_id": "buyer-agent-01",
    "contact": "buyer@example.com",
    "city": "深圳"
  },

  "supplier": {
    "supplier_id": "CN-MFG-0002908",
    "company": "江苏文灿压铸有限公司"
  },

  "items": [{
    "name": "支架-01",
    "process": "die_casting",
    "material": null,
    "qty_tiers": [10, 100, 1000],
    "tolerance_mm": null,
    "surface": null,
    "drawing_url": null,
    "drawing_format": null,
    "target_unit_price_cny": null,
    "need_by": "2026-10-15"
  }],

  "requirements": {
    "nda_required": false,
    "material_certificate": false,
    "first_article_report": false,
    "trade_term": null
  },

  "callback": {
    "protocol": "webhook",
    "to": "http://127.0.0.1:8099/callback",
    "deadline_hours": 48
  },

  "sig": "hmac-sha256=<hex>"      // 见 §4
}
```

**字段纪律（沿用能力卡红线）**：查不到就 `null`，禁止编造。
`target_unit_price_cny` 为 null 表示"未给目标价"，不代表"接受任何价"。

---

## 3. 幂等

`rfq_id` 即幂等键。平台侧对同一 `rfq_id`：

- 首次 → 路由投递，记录状态
- 重投 → 返回首次结果，**不重复通知厂商**

防止客户 Agent 重试时把厂商邮箱灌爆。

`rfq_id` 建议格式：`RFQ-{YYYYMMDD}-{6位随机}`，可读且便于人工在邮件里搜索。

---

## 4. 签名

```python
canonical = json.dumps(rfq_without_sig, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
sig = hmac.new(secret, canonical, sha256).hexdigest()
```

- 密钥：买方 Agent 在平台注册时分配 `buyer_secret`
- 覆盖字段：除 `sig` 外的整个信封
- 厂商侧可验证 `sig`，确认"这条询价确实来自该买方 Agent，未被篡改"
- **R1 阶段签名是可选的**（`sig` 可为 null），R2 涉及价格时强制要求

现阶段没有真实买方注册，签名留了接口但不强制——避免为了填字段而造一个假密钥。

---

## 5. 收讫确认（ack）

厂商（或其 Agent）收到后返回：

```jsonc
{
  "rfq_id": "RFQ-20260908-7F3A2B",
  "status": "acked",                  // acked | declined
  "received_at": "2026-09-08T18:00:12+08:00",
  "eta_hours": 24,                    // 预计多久给正式回复，null = 未承诺
  "reachable": true,
  "note": null
}
```

`eta_hours` 是**预计回复时间**，不是报价承诺。

---

## 6. 状态机（R1 部分）

```
        ┌─────────┐
        │ created │  客户端生成信封、签名
        └────┬────┘
             │ deliver
    ┌────────┴────────┐
    ▼                 ▼
┌────────┐      ┌──────────┐
│failed  │      │ delivered│  已投递到厂商入口，等 ack
└────────┘      └────┬─────┘
                     │ ack
              ┌──────┴──────┐
              ▼             ▼
          ┌───────┐   ┌──────────┐
          │ acked │   │ declined │
          └───────┘   └──────────┘
              │
              ▼ 超过 expires_at 仍无 ack
          ┌─────────┐
          │ expired │  不算失败，只是没回；可人工跟进
          └─────────┘
```

**`expired` 不是错误状态。** 厂商没回不等于拒绝，
呈现给采购方时必须说"未在 X 小时内确认收讫"，不能说"厂商拒绝"。

---

## 7. 通道与降级

`rfq.protocol` 决定怎么投：

| protocol | 投递方式 | R1 支持 |
|---|---|---|
| `webhook` | HTTP POST 到 endpoint | 是 |
| `mcp` | MCP 工具调用 | 规划中 |
| `email` | 渲染邮件正文（**默认不发**） | 渲染，发送需显式开启 |
| `form` | 生成结构化文本供人工粘贴 | 渲染 |

**安全约束（硬性）**：
`rfq.py` 拒绝向非 localhost 的 endpoint 真实投递，除非显式传 `--confirm-real-delivery`。
理由是样本里的 endpoint 是 real 的厂商官网和邮箱——测试时误发等于骚扰真实企业。

---

## 8. 与 outbound 长连的衔接

R1 阶段先走"离线投递"（webhook / email / form）。
`beacon-gw/1` 长连通了之后，同一份 rfq/v1 信封原样封装进 `rfq_deliver` 下发，
客户侧协议完全不变。见 `docs/PROPOSAL_A2A_OUTBOUND.md §8`。

---

## 9. 待确认

1. `buyer_secret` 的发放方式（目前 stub）
2. ack 超时是否要主动重试投递一次（可能变成骚扰，倾向不重试）
3. 一条 RFQ 是否允许同时投多家（倾向允许，但 `rfq_id` 需带供应商后缀区分）
4. 是否记录厂商"长期不 ack"的行为作为可信度信号（倾向记录，但 R1 不用于排序）
