# 扩展性架构：从 2 万家到 1000 万家

> 2026-09-09 · 本文回答两个问题：客户 Agent 现在要交换多少数据？千万级之后怎么办？

## 一、现状实测（20265 家）

| 指标 | 实测值 |
|---|---|
| `git clone --depth 1` 下载量 | 4.0 MiB |
| 检出后落盘 | 55 MB（data/ 53 MB） |
| 中文档案 `data/gb/` | 22.75 MB（**1177 B/条**） |
| 英文镜像 `data/en/gb/` | 14.33 MB（741 B/条） |
| L0 指纹层 | 4.54 MB（**235 B/条**） |
| 列式分片（Parquet） | 1.29 MB（**67 B/条**） |
| `data/manifest.json`（分片清单） | 101 KB |

## 二、线性外推到 1000 万家（×493.5）

| 通道 | 现在 | 千万级 |
|---|---|---|
| 全量 clone（下载 / 落盘） | 4 MB / 55 MB | **1.9 GB / 27 GB** |
| 只拉一个国标小类 | 2.5 MB | **1.2 GB** |
| `industry-index.json` | 614 KB | **295 MB** |
| 单个大桶（3484） | 4 MB | **2 GB** |

**结论：不改造，百万级即失效。** 而且先爆的不是明细，是索引和单文件体积。

## 三、目标架构：三层按需拉取

```
客户 Agent
   ↓ ① 拉清单          101 KB（千万级约 1.5 MB，缓存后近乎为零）
data/manifest.json      → 全部分片的路径 / 条数 / 字节 / SHA1
   ↓ ② 按国标码挑分片，带 If-None-Match 拉取
fp  L0 指纹  235 B/条   → 初筛（千万级单小类约 1.6 MB）
pq  列式分片  67 B/条   → 列裁剪 + Range 取行组（可选）
zh  中文档案 1177 B/条  → 命中后才拉
   ↓ ③ 详情 API 按需取完整档案（~24 KB / 20 家）
```

实测（`scripts/client_search.py --industry 3525 --city 宁波`）：
**传输 313 KB，对比全量 clone 41.63 MB，省 133 倍。**

## 四、已落地的四步改造

### 1. L0 指纹层国标化 + 100% 覆盖
- 指纹从**归档主记录**生成，不再依赖能力卡：覆盖率 20% → **100%**（4136 → 20265）。
- 分片与归档同构：`skills/registry/fingerprint/gb/{门类}/{大类}/{小类}.jsonl`。
- 有能力卡的 4136 条保留富字段（proc/mat/tol/size/moq/lt/rt），其余标 `pv=derived`。
- `skills/registry/gb-proc-map.json`：国标码 → 工艺码，**由能力卡真实数据反推**（≥3 次才收录）。
- 脚本：`scripts/gen_fingerprint.py`

### 2. manifest 分片清单 + ETag
- `data/manifest.json` 列出全部 412 个分片的路径、条数、字节、SHA1（作 ETag）、更新日期。
- `--check` 模式检测"改了数据忘了重建清单"，可挂 CI。
- 参考客户端 `scripts/client_search.py`：拉清单 → 挑分片 → `If-None-Match` → 本地过滤。

### 3. 归档层自动分片（单文件 ≤ 5000 条）
- `gb_store.MAX_PER_FILE`（环境变量 `GB_MAX_PER_FILE` 可调），超限自动切 `{小类}-p2.json`。
- **逻辑桶不变**（仍是 `C/34/3484`），上层通过 `load_bucket()` 无感知。
- 已验证：阈值 500 时 3484 切成 7 片、103 逻辑桶不变、20265 条守恒；阈值调回能合并回单文件。

### 4. 列式分片 + 对象存储迁移预案
- `scripts/export_parquet.py` → `dist/parquet/`：1177 B/条 → **67 B/条（6%）**。
- 支持列裁剪与 HTTP Range 取行组（row group 5 万行）。

## 五、迁移触发条件与运维手册

| 信号 | 动作 |
|---|---|
| 全库 > 100 万条，或单分片 > 200 MB | 跑 `export_parquet.py`，把 pq 分片上传到对象存储 |
| 客户反馈"拉一个小类太慢" | 同上；并在 manifest 里把 pq 的 `p` 换成 CDN URL |
| 单个逻辑桶 > 5000 条 | 自动分片已生效；确认 `gb_store.py --reshard` 跑过 |
| 每次数据更新后 | 依次跑：`gen_fingerprint.py --apply` → `gen_manifest.py` → `validate.py --strict` |

**Git 的角色收敛**：只保留协议规范、schema、样例、L0 指纹与清单生成器脚本，
体积恒定在 50 MB 以内，与数据量解耦。千万级明细走对象存储 + CDN，不进 Git。

## 六、数据更新后的标准动作

```bash
python scripts/gen_fingerprint.py --apply   # 重建 L0 指纹（100% 覆盖）
python scripts/gen_manifest.py              # 重建分片清单
python scripts/validate.py --strict         # 全量校验（0 错误才允许提交）
```
