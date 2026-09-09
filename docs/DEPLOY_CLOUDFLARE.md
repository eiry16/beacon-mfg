# Cloudflare 部署：账号申请与授权指引

> 面向「主人自己动手注册 CF、把 Token 交给 Agent」的场景。
> 最后更新：2026-09-09

---

## 0. 为什么要 Cloudflare（不解决会卡在哪）

| 层 | 内容 | 现在的状态 |
|---|---|---|
| L0 指纹 | 23698 家，107 分片 | ✅ 进 Git，jsdelivr 可拉 |
| L1 能力卡 | 4136 张，41 分片 | ❌ **不进 Git** → CDN 上不存在 |
| L2 厂商自述 | `skills/vendors/{id}/SKILL.md` | ❌ **不进 Git** → CDN 上不存在 |

`.gitignore` 主动排除了 L1/L2（有意为之：厂商自述属于半私有内容，不该进公开仓库）。
结果是「云端按需拉取」这句话目前没有云端——**App 现在能看到工艺位，靠的是内置副本；
要按需拉完整卡、要打开厂商 Skill，必须有独立托管。**

一句话：**没有 CF，L1/L2 就永远只能靠 App 内置的精简版，厂商 Skill 打不开。**

---

## 1. 注册账号

**https://dash.cloudflare.com/sign-up**

- 邮箱 + 密码即可，免费计划（Free）够用：**Pages 每月 500 次构建、R2 10 GB 存储、Workers 10 万请求/天**
- 不需要买域名，也不需要把域名迁到 CF —— `*.pages.dev` 子域名直接能访问
- **不用绑信用卡**（R2 免费额度内不收费；超了才需要）

---

## 2. 创建 API Token

**https://dash.cloudflare.com/profile/api-tokens** → `Create Token` → 最下方 `Create Custom Token` → `Get started`

> 不要用上面那些模板（Edit zone DNS 之类）——模板普遍给多了权限，
> 一个只用来传文件的 Token 拿到全 Zone 写权限，泄漏就是事故。

### 最小权限表（照抄即可）

| Scope | Resource | Level | 用途 | 必需 |
|---|---|---|---|---|
| Account | Cloudflare Pages | Edit | 部署 L1/L2 静态分片 | ✅ 必需 |
| Account | Workers R2 Storage | Edit | 厂商上传图纸/证书、大对象 | 可选 |
| Account | Cloudflare Workers Scripts | Edit | 鉴权网关、RFQ 接口（服务端算灯牌） | 可选 |
| Account | Account Analytics | Read | 看流量/用量 | 可选 |

**不需要任何 Zone 权限**（用 `pages.dev` 域名时）。等以后绑自定义域名，再加 `Zone > Workers Routes: Edit`。

### 三个可选项，建议都设上

- **Account Resources** → `Include` → 选你自己的账号（防止 Token 被用于别的账号）
- **TTL** → 设个结束日期，比如 90 天后（到期重新发，逼自己轮换）
- **Client IP Address Filtering** → 家里 IP 会变，**本地开发先别设**，等部署跑顺了再考虑

Token 只显示一次，关掉页面就没了。

---

## 3. Account ID 在哪拿

进 https://dash.cloudflare.com ，右侧栏最下方就有 **Account ID**（32 位十六进制）。
Workers / R2 部署要用；只做 Pages 的话，wrangler 也能自己查，但给了省事。

---

## 4. 把什么交给 Agent

```
CLOUDFLARE_ACCOUNT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CLOUDFLARE_API_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**安全约定（重要）**

- 写进项目根目录 `.env`（已在 `.gitignore` 里，不会进 Git）
- 不要贴在任何会被提交的文件、README、或本文档里
- 如果 Token 曾经出现在聊天记录或提交里，直接 Roll 掉重新发一个：
  Token 页面 `...` 菜单里有 `Roll`

---

## 5. 三条部署路线（建议按顺序，别一步到位）

### 路线 A：Pages 静态托管（先跑通，推荐）

`gen_capability_shards.py` 已经产出好目录了，整个 `dist/capability/` 传上去即可。

```
dist/capability/
  manifest.json                    ← 客户端入口（分片清单 + SHA1）
  full/gb/C/34/3484.json           ← 完整能力卡，按需拉取
  slim/gb/C/33/3399.json           ← 精简版，App 内置那份的源头
```

产物：**4136 张卡 / 41 片 / full 4.61 MB / slim 1.19 MB**

客户 Agent 的用法：拿国标码 3525 → 读 `manifest.json` → 拉 `full/gb/C/35/3525.json`。
一次请求拿到该小类全部能力卡，不用扫全库。

### 路线 B：加 R2（厂商要上传文件才需要）

图纸、材质报告、认证扫描件。当前 `server/` 里**没有任何文件上传端点**
（`materials` 现在只是 `{type, ref}` 占位），要真支持上传得先写这个接口。

### 路线 C：加 Workers（要鉴权/写接口才需要）

灯牌（L1/L2/L3）必须在服务端算——放客户端等于让用户自己给自己发灯牌。
RFQ 投递、厂商认领审核同理。

---

## 6. 部署之后要改的一处配置

App 的数据源默认是 jsdelivr（走 Git，只能拿 L0）：

```kotlin
// APK/app/src/main/java/cn/beaconmfg/app/data/SettingsRepo.kt
val dataBase: String = "https://fastly.jsdelivr.net/gh/eiry16/beacon-mfg@main/"
```

CF Pages 上线后，详情页里「厂商 Skill → 打开」按钮会拼 `dataBase + skillPath`。
把 `dataBase` 换成 Pages 域名，L2 才真正可点开。

---

## 7. ⚠ 先说风险：国内访问

**Cloudflare 的 `pages.dev` 域名在国内访问不稳定**（时延高、部分地区不通）。
而 L0 现在走的 jsdelivr 同样有这个问题——这是已知的存量风险，不是 CF 引入的。

对「客户的 Agent 来拉数据」这个场景：

- Agent 大多跑在境外或云上 → CF 没问题，反而更快
- 人用手机 App 直连 → 建议 App 侧保留「内置数据优先、联网只做增量」的现状（现在就是这样设计的）

如果以后国内客户是主力，再考虑国内 CDN 镜像，别在第一步就为它做架构妥协。

---

## 8. 拿到 Token 后我会做

1. `wrangler` 初始化 + `.env` 落盘（Token 只进 `.env`）
2. 部署 `dist/capability/` 到 Pages → 拿到域名
3. 把域名写进 `SKILL.md` / `SKILL_EN.md` 的 L1 检索入口
4. App 的 `dataBase` 默认值改成 Pages 域名，重新构建
5. 跑一次端到端验证：Agent 按国标码拉分片 → 命中 → 打开厂商 Skill
