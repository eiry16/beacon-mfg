# 安卓 App 构想 · 可行性评估与实施方案

> 目标：手机端对话式 Agent —— 用三方 LLM API 理解用户需求 → 检索供应商；
> 同时作为供应商认证、上传资料的客户端。
>
> 本文所有数字均为本机实测，不是估算。评估日期 2026-09-09。

---

## 0. 结论先行

**构想成立，且比预想的更有基础——因为仓库已经为"按需取数"改造完了。**
但有三个硬约束不解决就做不成，按严重度排序：

| # | 硬约束 | 严重度 | 现状 |
|---|---|---|---|
| 1 | **API key 不能放进客户端** | 🔴 致命 | 无对策，必须先定方案 |
| 2 | **`server/` 未部署** | 🔴 阻塞 | 认证/上传没有后端可连 |
| 3 | **数据源在 GitHub raw，国内不稳** | 🟠 高 | 已有 ETag 缓存，但首拉仍可能失败 |

好消息是数据侧的条件非常好（见 §3），**这恰好是仓库架构改造埋下的伏笔**。

---

## 1. 本机构建能力（已实测）

| 依赖 | 状态 | 位置 |
|---|---|---|
| Android SDK | ✅ | `~/AppData/Local/Android/Sdk`，build-tools 34/36，platform android-35/37 |
| JDK | ✅ 25.0.2 | `C:\Program Files\Android\Android Studio\jbr` |
| Gradle | ✅ 9.3.0 已缓存 | `~/.gradle/wrapper/dists`；`services.gradle.org` 可达（HTTP 200） |
| Android Studio | ✅ 已安装 | 可用它打开项目一键 Build APK |

**结论：本机可以出 APK。** 唯一需要注意 JDK 25 与 Android Gradle Plugin 的版本兼容性——
AGP 8.x 对 JDK 25 的支持需实测，不行就降级用 AS 自带的其他 JDK 或指定 `org.gradle.java.home`。

---

## 2. 技术选型

| 方案 | 评估 |
|---|---|
| **原生 Kotlin + Jetpack Compose** ⭐ 推荐 | 对话流 + 列表用 Compose 开发快；能直接调 Android Keystore 存 key；本机环境齐备 |
| Flutter | 需装 Dart/Flutter SDK（本机 **没有**），多一层环境依赖 |
| React Native | 需 Node + 原生工具链，包体更大，调试链更长 |
| Chaquopy（Android 内跑 Python） | ❌ **不建议**：APK 增重 10–30MB，且商业授权；检索逻辑本身很轻，不值得背一个 Python 运行时 |

**推荐：原生 Kotlin + Jetpack Compose**，检索内核用 Kotlin 重写（理由见 §5）。

---

## 3. 数据方案 —— 最大的有利条件

仓库已经完成分片化改造，这让"小包体 + 低流量"成为现实：

### 内置进 APK 的核心索引：仅 **1.30 MB**

| 文件 | 大小 | 作用 |
|---|---|---|
| `data/manifest.json` | 102 KB | 分片清单，定位"该下载哪一片" |
| `data/industry-index.json` | 614 KB | 国标小类 → 企业 ID 列表 |
| `data/region-index.json` | 536 KB | 城市 → 供应商 ID 列表 |
| `data/gb-alias.json` + `gb-alias-curated.json` | 51 KB | 采购词 → 国标码（**两层都要内置**） |
| `data/gb-index.json` | 29 KB | 行业层级树与计数 |

### 按需下载的分片

- 指纹分片 **103 个 / 合计 4.54 MB / 20265 条**，中位 **4.4 KB**
- 全部 412 个分片合计 42.92 MB（其中能力卡 38.38 MB）——**绝不能内置**

### 典型一次检索的开销

```
命中 1–3 个分片  →  几 KB ~ 几十 KB
实测冷启动       →  19.5 KB 线上 / 97 KB 解压 / 1.2s
热缓存（ETag）   →  0 B
```

### ⚠️ 一个和 max_supply 直接相关的坑

**最大分片 `3484 机械零部件加工` 有 3.99 MB**（2929 塑料 2.59 MB、3360 表面处理 1.87 MB）。
一次误命中就会让手机用户白等 4MB 流量。

这正是刚做的 `max_supply=500` 收敛的**第二个价值**：它不只是减噪，
还让 App 不会去拉这些巨型分片。实测「齿轮」从拉 3484（3.99 MB）变成拉 3453（几 KB）。
**App 端必须继承同一套收敛规则**，否则手机用户会替桌面端的噪音买单。

### 建议

- 首次启动：内置索引 + 拉 manifest 更新（102 KB，可增量）
- 检索：本地解析采购词 → 定位分片 → 下载 → 本地过滤
- 缓存：SQLite 或文件缓存 + ETag，二次打开零流量
- 预取：WiFi 下后台预拉 TOP20 大分片

---

## 4. LLM 接入 —— 致命问题在这里

### 🔴 为什么不能把 API key 打进 APK

APK 可以被反编译，字符串常量一搜就有。把平台 key 放进去等于**公开**，
任何人都能拿去刷 token，账单是你的。

### 两个方案

**方案 A：BYOK（用户自带 key）⭐ 推荐先做**

- 用户在设置里填自己的 key，存 **Android Keystore**（硬件级加密，取不出来）
- 支持 OpenAI / DeepSeek / 通义 / 智谱 等兼容 OpenAI 格式的端点
- 优点：零成本、零合规风险、当天就能跑
- 缺点：用户门槛（得自己申请 key）

**方案 B：平台代理层（中期做）**

```
App → 平台 server（持有 key）→ LLM
```

- 优点：用户零配置，可限流、可审计、可换模型
- 缺点：**依赖 §0 的第 2 个硬约束——server 必须先部署**

**建议路径：先做 A 跑通，server 部署后加 B，两者共存让用户选。**

### Function Calling 设计

不要让 LLM 直接"背诵"供应商数据（会编造），而是让它**调用工具**：

```json
tools: [
  { "name": "search_suppliers",
    "parameters": { "keyword": "string", "city": "string",
                    "industry_code": "string", "cert": "string", "limit": "int" } },
  { "name": "get_supplier_detail", "parameters": { "id": "string" } },
  { "name": "list_categories",     "parameters": { "parent": "string" } }
]
```

流程：用户说话 → LLM 决定调哪个工具 → **App 本地执行真实检索** →
结果回灌 LLM → LLM 组织成自然语言。

> ⚠️ 回灌时必须只给精简字段（公司名/城市/主营/电话），且要带上
> **证据档位标记**（字面命中 / 行业推断）。否则 LLM 会把"行业推断"的结果
> 说成"这家做齿轮"，那就违背了项目的数据红线。

Token 成本可控：10 条 × 约 50 token = 500 token/次。

### 端侧跑模型？不建议

1.7B 量化模型约 1.2 GB，手机内存与速度都不现实。老实用三方 API。

---

## 5. 检索内核移植

`scripts/query.py` 是 Python，但它的逻辑其实很轻：

- 读 JSON（manifest / 两张别名表 / 索引）
- 国标码前缀匹配（查 3484 要带上 348 / 34）
- 别名表查词（两层合并 + max_supply 收敛）
- 字段过滤（city / cert / is_manufacturer）

**用 Kotlin 重写的工作量约 2–3 天**，换来：包体不增重、无授权问题、性能好。
建议直接把 `query.py` 当规格说明书逐条移植，并用同一批采购词做对拍测试
（`scripts/audit_alias.py` 的第 7 节 105 词就是现成的测试集）。

---

## 6. 供应商端（认证 + 上传）—— 最大阻塞点

### 现有可复用的资产（很可观）

| 能力 | 代码 | 手机端适配度 |
|---|---|---|
| 手机号验证码认领 | `server/routers/claim.py` | ⭐ 天然适合手机 |
| USCC 校验位（GB 32100-2015） | `certification.py:uscc_check` | ⭐ 可完全本地算，秒级反馈 |
| 地址差异比对 | `certification.py:compare_address` | ⭐ 可本地预校验 |
| **对话式硬指标采集** | `server/routers/collect.py` | ⭐⭐ **这是手机端杀手锏** |
| 灯牌规则引擎 | `certification.py:evaluate_badge` | 需服务端（防绕过） |

**对话式采集 + 手机 = 绝配。** 老板在车间里用语音回答 15 分钟，
比坐电脑前填 40 个字段的表单现实得多。这是 PC 端做不到、只有手机能做到的场景。

### 缺什么

1. **文件上传端点：完全没有。** 现在 `materials` 只是 `{type, ref}` 引用，
   `python-multipart` 已在 requirements 里但没用上。需要新增
   `POST /v1/certify/{app_id}/materials`（营业执照/厂房照片/证书扫描件）。
2. **对象存储**：COS / OSS / S3 之一。
3. **服务端部署**：`server/` 目前**没有任何部署配置**（无 Dockerfile、无 CI 部署）。
4. **推送/短信**：验证码与审核结果通知（阿里云/腾讯云 SMS）。

### 灯牌必须由服务端算

`evaluate_badge()` 不能放客户端——否则改一下 APK 就能自己给自己发灯牌。
**这条红线在 App 上同样成立，甚至更关键。**

---

## 7. 建议的分阶段路线

| 阶段 | 目标 | 产出 | 前置 |
|---|---|---|---|
| **P0 原型** | 本地检索跑通，无 LLM | APK：内置索引 + 关键词检索 + 结果列表 | 无 |
| **P1 对话检索** | BYOK + function calling | 用户填自己的 key，用自然语言找供应商 | P0 |
| **P2 供应商采集** | 对话式硬指标采集 + 本地校验 | 老板手机填能力卡 | P1 + server 部署 |
| **P3 认证闭环** | 材料上传 + 审核 + 灯牌展示 | 完整认证流程 | P2 + 对象存储 + SMS |
| **P4 平台代理** | 服务端持有 key，用户零配置 | 去掉 BYOK 门槛 | P3 |

**建议先做 P0。** 它不依赖任何未决事项（不需要 key、不需要 server），
一两周就能出包，能立刻验证"手机上检索到底好不好用"这个最核心的假设。
如果 P0 做完发现手机端检索体验不成立，后面的投入就都省了。

---

## 8. 风险清单

| 风险 | 影响 | 对策 |
|---|---|---|
| API key 泄露 | 账单被盗刷 | BYOK + Keystore；平台 key 绝不下发 |
| GitHub raw 国内不通 | 首拉失败，App 变砖 | 加 CDN / 镜像源；内置全量指纹做兜底（+4.54MB 包体） |
| 巨型分片 3.99MB | 流量与等待 | 继承 max_supply 收敛；分片级进度提示 |
| LLM 编造供应商 | 数据红线被破 | 只让 LLM 组织语言，数据一律来自本地检索；回灌带证据档位 |
| 灯牌被客户端伪造 | 信任体系崩塌 | 灯牌只由服务端 `evaluate_badge()` 计算 |
| JDK 25 / AGP 兼容 | 构建失败 | 指定 `org.gradle.java.home` 指向兼容 JDK |

---

## 9. 待主人拍板

1. **API key 走 BYOK 还是平台代理？**（决定 P1 的形态）
2. **server 部署在哪？**（决定 P2/P3 能否启动——这是目前最大的未知）
3. **是否接受"内置全量指纹 4.54MB"换取离线可用？**（应对 GitHub 不稳）
4. **先做 P0 原型验证，还是直接按 P1 目标投入？**

---

## 附：APK 目录建议结构

```
APK/
├── README.md              # 本文档
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── assets/index/  # 内置核心索引（1.30MB）
│       └── java/.../
│           ├── search/    # 检索内核（query.py 的 Kotlin 移植）
│           ├── llm/       # LLM 客户端 + function calling
│           ├── vendor/    # 供应商认证/采集
│           └── ui/        # Compose 对话界面
└── docs/
    └── E2E_TESTS.md       # 与 audit_alias.py 第 7 节对拍的用例
```
