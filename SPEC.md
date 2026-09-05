# BeaconMFG 项目规范（SPEC）

> 状态：v0.1 · 更新：2026-09-05
> 本文件是项目的唯一规范来源，所有决策记录于此。修改需 PR review。

---

## 1. 项目定位

**一句话定位：** 面向 Agent 检索的中国制造业供应商结构化名录，Agent 可编程的 B2B 入口。

**数据模型：** 纯 JSON 文件，无需脚本、无需 API Key、无需联网。Agent 加载 `SKILL.md` 后直接读文件检索。

**核心价值主张：**
- 下游 Agent 一眼看到上游供应商的联系方式与资质
- 供应商认领后可对接官方 Agent Skill，自动处理询价
- 从「可见性」直接转化为「商机」

---

## 2. 数据结构规范

### 2.1 文件布局

```
data/
├── index.json              # 品类索引（品类→关键词→文件路径）
├── region-index.json       # 地区二级索引（城市→供应商ID列表）
├── DATA_STATS.md           # ⚠️ CI 自动生成，唯一权威统计源
└── suppliers/              # 中文数据（按品类拆分，8个文件）
    ├── 精密机械加工.json
    ├── 钣金冲压.json
    ├── 注塑成型.json
    ├── 压铸.json
    ├── 电子元器件.json
    ├── 表面处理.json
    ├── 标准件.json
    └── 原材料.json
data/en/                    # 英文镜像（8个文件，同步维护）
```

### 2.2 品类定义

| 品类 | 文件 | 关键词示例 |
|---|---|---|
| 精密机械加工 | 精密机械加工.json | CNC加工, 车削, 铣削, 五轴, 线切割 |
| 钣金冲压 | 钣金冲压.json | 激光切割, 折弯, 冲压, 焊接, 机箱 |
| 注塑成型 | 注塑成型.json | 注塑, 双色注塑, 模具, 手板 |
| 压铸 | 压铸.json | 铝合金压铸, 锌合金压铸, 压铸模具 |
| 电子元器件 | 电子元器件.json | PCB, PCBA, 连接器, 传感器, 线束 |
| 表面处理 | 表面处理.json | 阳极氧化, 电镀, 喷涂, 热处理 |
| 标准件 | 标准件.json | 轴承, 弹簧, 螺丝, 紧固件, 齿轮 |
| 原材料 | 原材料.json | 不锈钢, 铝合金, 铜材, 塑料粒子 |

**规则：一条供应商记录只能属于一个品类。** 若企业跨多个品类，按主营拆成多条记录（id 不同）。

### 2.3 字段规范（supplier.schema.json）

#### 核心字段（免费层，100% 必填）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | `CN-MFG-{4-7位数字}`，全局唯一 |
| `company` | string | 企业全称（去 POI 噪声：去除「宿舍楼/N栋/园区」等后缀） |
| `category` | string | 精确匹配 index.json categories 列表 |
| `keywords` | string[] | 主营关键词，建议从 index.json 关键词词典选取 |
| `region.province` | string | 省/直辖市 |
| `region.city` | string | 市 |
| `lat` / `lng` | number\|null | 纬度/经度（LBS 检索用） |
| `contact_phone` | string | 公开联系电话，完整展示，无脱敏 |
| `source` | string | `public_directory`（当前）/ `gov_list` / `company_website` / `exhibition` |
| `verified_at` | string | 信息核实日期 YYYY-MM-DD（当前为导入日期，非真实核验） |
| `is_template` | boolean | **废弃**，请用 `status` 字段 |

#### 增值字段（认领后解锁）

| 字段 | 说明 |
|---|---|
| `website` | 企业官网 |
| `contact_email` | 公开邮箱 |
| `address` | 详细地址 |
| `certifications` | 资质标签数组（ISO9001/高企等） |
| `founded` | 成立年份 |
| `scale` | 规模（<50人 / 50-100人 / ...） |
| `note` | 备注（公开信息，不编造） |
| `source_url` | 来源链接 |

#### Phase 1 字段（认主系统）

| 字段 | 说明 |
|---|---|
| `status` | `verified` / `unverified_poi` / `template` |
| `imported_at` | 数据导入日期 |
| `last_verified_at` | 最近核验日期，未核验为 null |
| `claim.status` | `unclaimed` / `claimed` / `verified` |
| `claim.claimed_at` | 认主时间 |
| `claim.verified_by` | 验证方式：`wechat` / `email` / `manual` |
| `agent.skill_url` | 供应商官方 Skill URL |
| `agent.protocol` | `mcp` / `skill` / `a2a` / `native` |
| `agent.capabilities` | `catalog` / `rfq` / `live_chat` / `quote` |
| `agent.verified` | 平台内容审核通过 |

### 2.4 数据状态枚举（status）

| 枚举值 | 说明 | Agent 行为 |
|---|---|---|
| `verified` | 电话已核实，来源可追溯 | 正常展示 |
| `unverified_poi` | 真实企业 POI，电话待核实 | **保留展示**，标注「电话待核实」 |
| `template` | 示例占位数据 | 保留展示，标注「示例数据」 |

> `is_template=true` 的旧数据 → `status: "unverified_poi"`（真实企业，只是电话未核实）
> `is_template=true` 且 company 含「示例/测试」→ `status: "template"`

### 2.5 数据质量规则

1. **id 全局唯一**，无重复
2. **id 格式**：`CN-MFG-{4-7位数字}`
3. **联系方式完整展示**：座机/400/手机无脱敏；缺失显示"待核实"
4. **不编造**：价格、交期、产能、认证——数据里没有的就不展示
5. **POI 名称清洗**：去除「宿舍楼 / N栋 / N号楼 / （园区）/ 分公司」等后缀
6. **跨品类去重**：同一企业同一品类只保留一条记录

---

## 3. 认主与认证规范

### 3.1 认证方式优先级

1. **首选：微信服务号扫码**（企业主体认证，OAuth2）
2. **备选：企业邮箱验证**（contact@企业域名邮箱）
3. **兜底：人工审核**（上传营业执照，1-3 工作日）

### 3.2 认主状态机

```
unclaimed ──[微信扫码+验证码]──► claimed ──[平台审核]──► verified
```

| 状态 | 可做 |
|---|---|
| `unclaimed` | 仅展示地址/电话 |
| `claimed` | 填写/更新联系方式（审核后生效），提交 Skill URL（审核后生效） |
| `verified` | 全部能力：自动询价 / 对话客服 Agent / 平台背书 |

### 3.3 微信认证流程

```
① 供应商访问 beaconn-mfg.com/claim → 输入企业名 + 品类关键词
② 平台显示微信公众号二维码，供应商用企业服务号扫码
③ 服务号推送模板消息（6位验证码，10分钟有效）
④ 供应商输入验证码 → 平台验证通过 → 颁发 claim_token
⑤ claim_token 换取 API Key → 进入管理后台
```

### 3.4 身份验证价值

「认领」卖的不是「展示更完整」，而是「**被 Agent 可验证地信任**」。

- `.well-known/beaconmfg-skill.json` 域名验证：证明 Skill 归该企业所有
- 微信主体认证：证明认主者拥有该企业服务号
- 平台背书：verified 供应商可对接客户 Agent 询价

---

## 4. API 规范（server/）

### 4.1 设计原则

- 面向 Agent 优化：JSON-only、结构化错误、无 HTML
- 无需 Key 即可读（降低集成门槛）
- 写入需认证（API Key / OAuth2）
- REST 风格，版本控制（`/v1/` 前缀）

### 4.2 核心端点

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| GET | `/suppliers` | 无 | 检索+排名（keyword/category/city/lat/lng/claimed/verified/has_skill） |
| GET | `/suppliers/{id}` | 无 | 单条详情 |
| GET | `/suppliers/{id}/skill` | 无 | 获取 Skill（白名单过滤后） |
| POST | `/suppliers/search` | 无 | 复杂布尔查询 |
| GET | `/categories` | 无 | 品类列表 |
| GET | `/regions` | 无 | 地区索引 |
| GET | `/stats` | 无 | 全局统计 |
| POST | `/claim/verify-start` | 无 | 微信认证开始 |
| POST | `/claim/send-code` | 无 | 发送验证码 |
| POST | `/claim/verify-code` | 无 | 验证验证码 |
| POST | `/claim/confirm` | 无 | 确认认主 |
| GET | `/me` | API Key | 当前供应商信息 |
| PATCH | `/me` | API Key | 更新联系信息 |
| POST | `/me/skill` | API Key | 提交 Skill URL |
| DELETE | `/me/skill` | API Key | 下架 Skill |

### 4.3 排名算法

综合得分 = 品类匹配度×0.4 + 认证状态×0.3 + 信息完善度×0.2 + 地理距离×0.1

**品类匹配度**：精确匹配 1.0 分 / 模糊匹配 0.5 分 / 查询关键词命中数 / 关键词总数
**认证状态**：verified=1.0 / claimed=0.6 / unclaimed=0.0
**信息完善度**：8 项核心字段（phone/email/address/website/certifications/note/location/keywords≥3）非空数/8
**地理距离**：max(0, 1 - 距离km/200)，无坐标给 0.5 分

可切换 profile（`X-Ranking-Profile` 请求头）：`default` / `nearest` / `verified` / `complete`

### 4.4 速率限制

| 类型 | 限制 |
|---|---|
| GET /suppliers（公开） | 200次/分钟/IP |
| GET /suppliers（带 Key） | 2000次/分钟/Key |
| 写入操作 | 60次/小时/Key |
| POST /claim | 10次/小时/IP |

### 4.5 错误格式

```json
{
  "error": {
    "code": "INVALID_PARAMS",
    "message": "参数校验失败",
    "details": {}
  }
}
```

---

## 5. CI / 数据治理规范

### 5.1 强制 CI 检查项

每次 `data/` 变更必须通过：

1. **JSON Schema 校验**：所有品类文件满足 `schema/supplier.schema.json`
2. **字段完整性统计**：各字段非空率记录到 `DATA_STATS.md`
3. **数字一致性**：README / index.json / SKILL.md 数字与 `DATA_STATS.md` 一致
4. **id 唯一性与格式**：无重复，格式正确
5. **来源合法性**：真实数据 source 不能为空或 `template`
6. **联系方式格式**：真实数据不含占位符（XXXX/占位/example）

### 5.2 数字权威源

`data/DATA_STATS.md` 是唯一权威统计。README / SKILL.md 的数字由 CI 同步，不手动填写。

### 5.3 数据更新流程

```
PR 提交
  ↓
CI: validate.py --strict
  ↓
CI: JSON Schema 校验
  ↓
CI: 生成 DATA_STATS.md
  ↓
CI: 同步 README 徽章数字
  ↓
PR review + merge
  ↓
main 分支更新
```

---

## 6. 商业模型

### 6.1 三层数据模型

| 层 | 内容 | 收费 | 来源 |
|---|---|---|---|
| **免费层** | 名称、地址、坐标、公开电话 | 免费 | 单方面抓取 |
| **认领层** | 认证、溯源、产品详情、官网、资质 | 认领费 | 企业自填+核验 |
| **对接层** | 官方 Agent Skill、询价对接、客服 Agent | 上架费/订阅 | 企业自上架+审核 |

### 6.2 认主飞轮

```
数据越完整 → 可见性越高 → 企业越愿意认领 → 数据越完整 → …
```

对接层是飞轮加速器：把「可见性」直接转化为「询价/商机」，供应商迁移成本高，形成壁垒。

### 6.3 收入模式

| 模式 | 状态 | 说明 |
|---|---|---|
| 认领费（一次性） | Phase 1 | 解锁增值档案，验证身份 |
| 上架费/订阅（持续） | Phase 1 | 维持官方 Skill 在线 |
| 撮合抽佣 | 暂缓 | 待流量与信任成熟后上 |

---

## 7. 安全规范

### 7.1 Skill 安全白名单

平台对第三方 Skill 内容做白名单过滤：

**允许的工具类型：** `catalog` / `rfq` / `quote` / `live_chat` / `search` / `query`
**禁止的工具类型：** `shell` / `exec` / `system` / `code_interpreter` / `download` / `upload` / `database` / `delete`

### 7.2 信任边界

| claim.status | 开放能力 |
|---|---|
| `unclaimed` | 仅展示 |
| `claimed` | 查看产品详情（只读） |
| `verified` | 自动询价 / 对话客服 Agent |

### 7.3 数据红线

- 不发布个人隐私（法人/股东/个人手机）
- 不批量爬取 B2B 平台（1688 等）
- 不编造价格/交期/产能/认证
- 企业可申诉更正/删除自己的信息

---

## 8. 团队规范

### 8.1 分支策略

- `main`：稳定版本，CI 全绿
- `feature/*`：功能开发
- `data/*`：数据更新（每次 PR 自动触发 CI）

### 8.2 Issue 模板

| 模板 | 用途 |
|---|---|
| `[DATA]` 数据错误报告 | 电话/地址/名称有误 |
| `[CLAIM]` 认主申请 | 企业申请认领记录 |
| `[FEATURE]` 功能建议 | 对平台的功能建议 |

### 8.3 发布规范

- 语义化版本（`v0.x.y` 格式 tag）
- GitHub Release 自动打包
- Changelog 记录 breaking change

---

## 9. 待办事项

### Phase 0（立即，低成本高收益）
- [x] 修正 README/SKILL 数字漂移（CI 自动同步）
- [x] `is_template` → `status` 枚举（schema 已升级）
- [x] `verified_at` 拆分为 `imported_at` + `last_verified_at`
- [x] CI 校验脚本增强 + `DATA_STATS.md` 生成
- [ ] `data/suppliers/*.json` 中 is_template=true 且电话含 XXXX 的记录 → 补全或标记 unverified_poi
- [ ] POI 名称清洗（去除宿舍楼/N栋等噪声）
- [ ] 英文镜像数量同步（当前 7915 vs 中文 7962，差 47 条）

### Phase 1（2-4 周）
- [ ] 微信服务号注册
- [ ] 认证流程 API（verify-start / send-code / verify-code / confirm）
- [ ] 认主字段写入数据（claim / agent）
- [ ] Skill 白名单过滤实现
- [ ] 企业管理后台（更新联系方式 / 提交 Skill）

### Phase 2（MVP 验证）
- [ ] 找 2-3 家供应商完成认主
- [ ] 跑通「检索 → 加载 Skill → 自动询价」链路
- [ ] 验证付费意愿

---

*本文档是项目唯一规范来源，每次重要决策后更新。*
