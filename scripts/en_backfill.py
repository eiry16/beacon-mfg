# -*- coding: utf-8 -*-
"""BeaconMFG 英文镜像增量补齐（GLM-4-Flash）

- 只翻译 data/gb/（中文主库，国标四级）中在 data/en/gb/ 里缺失 id 的记录
  （已有翻译保留不动）
- 复用 translate_en.py 的 prompt 与 API 调用方式
- 每个国标桶处理完立即写回（断点续跑：重复执行自动跳过已有 id）
- 用法:
    python scripts/en_backfill.py --limit 2            # 每桶试 2 条
    python scripts/en_backfill.py --workers 3          # 全量并发 3
    python scripts/en_backfill.py --bucket C/34/3484   # 只补一个桶
  API Key 从环境变量 ZHIPU_API_KEY 或 .env 读取

2026-09-09 修正：本脚本原本读 data/suppliers/、写 data/en/<品类>.json——
那是 2026-09-08 已退役的 8 品类布局。目录不存在时 `SRC_DIR.glob()` 返回空列表，
脚本会打印「全部完成：新增英文 0 条」然后**静默成功**，差额永远补不上。
现已改为国标四级布局（与 migrate_en_to_gb.py 同源同规则）。

翻译完记得补一步：python scripts/en_sync_industry.py（补 industry_en 标签）。
"""
import argparse
import atexit
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 阻止系统睡眠（进程运行期间机器不睡）
try:
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "data" / "gb"          # 中文主库：国标四级归档
EN_DIR = ROOT / "data" / "en" / "gb"    # 英文镜像：与中文同构
ENV_FILE = ROOT / ".env"

sys.path.insert(0, str(ROOT / "scripts"))

# 2026-09-19：英文库必须与中文主库用同一条分片阈值，否则两套布局并存会累加出
# 跨分片重复 id（实测 15,989 个）。这个数字的唯一权威来源是 gb_store。
import gb_store  # noqa: E402

MAX_PER_FILE = gb_store.MAX_PER_FILE

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4-flash"
BATCH = 8

# 2026-09-19 二次改造：桶内批次并发度（--bucket-workers / EN_BUCKET_WORKERS）。
# 只按「桶」并发时，尾巴会退化成「最慢那个桶单线程爬」——实测剩 4 个大桶时
# 16 个 worker 里 12 个空转，H/62/6210（剩 4000+ 条）要 4 小时才能爬完。
# 桶内也并发后，真正吃满 EN_RPM 令牌桶（20 请求/分 ≈ 160 条/分）。
# 上限由 EN_MAX_CONCURRENT（默认 20）与 EN_RPM 两个全局闸门兜着，调大不会打爆 API。
BUCKET_WORKERS = int(os.environ.get("EN_BUCKET_WORKERS", "8"))

CATEGORY_EN = {
    "精密机械加工": "Precision Machining", "钣金冲压": "Sheet Metal & Stamping",
    "注塑成型": "Injection Molding", "压铸": "Die Casting",
    "电子元器件": "Electronic Components", "表面处理": "Surface Treatment",
    "标准件": "Standard Parts", "原材料": "Raw Materials",
}
CATEGORY_FILE_EN = {
    "精密机械加工": "precision-machining", "钣金冲压": "sheet-metal",
    "注塑成型": "injection-molding", "压铸": "die-casting",
    "电子元器件": "electronic-components", "表面处理": "surface-treatment",
    "标准件": "standard-parts", "原材料": "raw-materials",
}
REGION_EN = {
    "广东": "Guangdong", "江苏": "Jiangsu", "浙江": "Zhejiang", "福建": "Fujian",
    "山东": "Shandong", "上海": "Shanghai", "北京": "Beijing",
    "深圳": "Shenzhen", "东莞": "Dongguan", "苏州": "Suzhou", "宁波": "Ningbo",
    "佛山": "Foshan", "无锡": "Wuxi", "广州": "Guangzhou", "青岛": "Qingdao",
    "天津": "Tianjin", "重庆": "Chongqing", "成都": "Chengdu",
    # 2026-09-08 补齐：以下城市此前未映射，导致 2228 条英文记录 city 残留中文
    "常州": "Changzhou", "嘉兴": "Jiaxing", "温州": "Wenzhou", "南京": "Nanjing",
    "杭州": "Hangzhou", "绍兴": "Shaoxing", "惠州": "Huizhou", "中山": "Zhongshan",
    "南通": "Nantong", "珠海": "Zhuhai", "台州": "Taizhou", "金华": "Jinhua",
    "嘉兴市": "Jiaxing", "厦门": "Xiamen", "泉州": "Quanzhou", "福州": "Fuzhou",
    "合肥": "Hefei", "芜湖": "Wuhu", "武汉": "Wuhan", "长沙": "Changsha",
    "郑州": "Zhengzhou", "洛阳": "Luoyang", "济南": "Jinan", "潍坊": "Weifang",
    "烟台": "Yantai", "淄博": "Zibo", "石家庄": "Shijiazhuang", "唐山": "Tangshan",
    "廊坊": "Langfang", "保定": "Baoding", "沈阳": "Shenyang", "大连": "Dalian",
    "西安": "Xi'an", "南昌": "Nanchang", "昆明": "Kunming", "贵阳": "Guiyang",
    "南宁": "Nanning", "太原": "Taiyuan", "哈尔滨": "Harbin", "长春": "Changchun",
    "河北": "Hebei", "河南": "Henan", "湖北": "Hubei", "湖南": "Hunan",
    "安徽": "Anhui", "江西": "Jiangxi", "四川": "Sichuan", "陕西": "Shaanxi",
    "辽宁": "Liaoning", "吉林": "Jilin", "黑龙江": "Heilongjiang",
    "广西": "Guangxi", "云南": "Yunnan", "贵州": "Guizhou", "山西": "Shanxi",
    "甘肃": "Gansu", "海南": "Hainan", "内蒙古": "Inner Mongolia",
    "新疆": "Xinjiang", "宁夏": "Ningxia", "青海": "Qinghai", "西藏": "Tibet",
    "香港": "Hong Kong, China", "澳门": "Macao, China", "台湾": "Taiwan, China",
}
SYSTEM_PROMPT = (
    "You are a professional translator for a B2B manufacturing supplier directory "
    "(BeaconMFG). Translate Chinese supplier data into concise, professional English. "
    "Rules: company = company name (transliterate Chinese name into pinyin-style "
    "English if no official English name exists; if a well-known Latin brand name "
    "exists, use it, e.g. 霸王茶姬 -> CHAGEE, 海底捞 -> Haidilao, 万达 -> Wanda); "
    "keywords = product/service terms (short list); address = postal address, "
    "transliterate every Chinese road / town / district name into pinyin and render "
    "unit words in English (号 -> No., 层 -> Floor, 栋/幢 -> Building, 室 -> Room, "
    "附 -> Annex, 步行 -> walk), e.g. 祥符路799号 -> No. 799 Xiangfu Road. "
    # 2026-09-19 补：原先只对 address 声明「不得留中文」，company 没这条约束，
    # 于是模型把中文店名照抄进 company_en（实测 company_en 残留 896 条）。
    "CRITICAL: both company and address MUST contain ZERO Chinese characters. "
    "Never copy Chinese characters from the input; transliterate them instead. "
    "Return ONLY a JSON array matching "
    "the input order, each item: {\"company\":\"\",\"keywords\":[\"\"],\"address\":\"\"}. "
    "No extra text, no markdown."
)

log_lock = threading.Lock()

# ===== 2026-09-15 限流改造：规避 GLM-4-Flash 免费档的「雷群效应」 =====
# 背景：GLM-4-Flash 免费档实测几乎不限流（公开配额 QPS=30 / RPM≈100 / TPM≈50000，
# 第三方横评连发 15 个全中、无 429）。原脚本 --workers 3 + 同步退避，一旦偶发 429，
# 3 个 worker 一起睡 20-80s 又一起重试 → 锁步空烧，吞吐掉到 ~240 条/小时。
# 改造：令牌桶把请求速率压在 TPM 安全线以下（默认 20 次/分），并发信号量封顶在
# QPS=30 之内；429 退避加随机抖动打破锁步。几乎不触发 429，吞吐可达 ~1 万条/小时。
# 可用环境变量覆盖：EN_RPM（请求/分钟）、EN_MAX_CONCURRENT（并发上限）。
import random
EN_RPM = int(os.environ.get("EN_RPM", "20"))            # 请求/分钟上限（按 TPM≈50000、BATCH=8 估算安全值）
EN_MAX_CONCURRENT = int(os.environ.get("EN_MAX_CONCURRENT", "20"))  # 在 QPS=30 内封顶
_pace_lock = threading.Lock()
_last_call_ts = [0.0]
_call_sem = threading.Semaphore(EN_MAX_CONCURRENT)


def _pace(rpm):
    """全局令牌桶：保证相邻请求间隔 >= 60/rpm 秒，且不持锁睡眠（允许 worker 重叠）。"""
    if rpm <= 0:
        return
    interval = 60.0 / rpm
    with _pace_lock:
        now = time.time()
        wait = _last_call_ts[0] + interval - now
        if wait > 0:
            _last_call_ts[0] = now + wait   # 先占位，再释放锁去 sleep
        else:
            _last_call_ts[0] = now
    if wait > 0:
        time.sleep(wait)


def log(msg):
    with log_lock:
        print(msg, flush=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def is_untranslated(rec):
    """判断一条英文记录是否还是「中文兜底」。

    翻译失败时 build_en_record 会用中文原值填 company_en/address_en，
    记录照样写进 en 文件 → 下一轮 re-run 时它在 existing 里，会被当成「已翻译」跳过，
    中文就永久留下了。这个函数把它们识别出来，供 --retranslate 重翻。
    """
    if not isinstance(rec, dict):
        return True
    return bool(CJK_RE.search(str(rec.get("company_en", "") or ""))
                or CJK_RE.search(str(rec.get("address_en", "") or "")))


# ===== 2026-09-19 残留重试：把「英文里夹中文」当场修掉，而不是留给下一轮 =====
# 背景：GLM-4-Flash 会把中文店名/地名照抄进英文字段（实测残留 1,623 条 = 1.26%）。
# 落盘后要等下一轮 --retranslate 才会再碰它，而实测重翻收敛率只有 ~22%
# （拉回 2,095 条只修好 472 条）—— 等于这些记录会长期烂在库里。
# 但不能无脑重翻：`CHAGEE霸王茶姬` 这种「英文品牌名 + 一并保留的中文原名」对检索
# 反而有用，反复重翻只是白烧配额。所以只把「中文占比 >= 阈值」当缺陷去重试。
RETRY_CJK_RATIO = float(os.environ.get("EN_RETRY_CJK_RATIO", "0.2"))
RETRY_ROUNDS = int(os.environ.get("EN_RETRY_ROUNDS", "2"))


def cjk_len(rec):
    """一条记录 company_en + address_en 里的中文字符总数（用于「只留更好的」比较）。"""
    if not isinstance(rec, dict):
        return 0
    return (len(CJK_RE.findall(str(rec.get("company_en") or "")))
            + len(CJK_RE.findall(str(rec.get("address_en") or ""))))


def cjk_ratio(s):
    s = re.sub(r"\s", "", str(s or ""))
    return len(CJK_RE.findall(s)) / len(s) if s else 0.0


def cjk_heavy(rec):
    """真缺陷判定：company_en 或 address_en 的中文占比达到阈值。

    阈值以下（品牌名里夹一两个字、地名用字）视为可接受，不参与重试。
    """
    if not isinstance(rec, dict):
        return True
    return max(cjk_ratio(rec.get("company_en")),
               cjk_ratio(rec.get("address_en"))) >= RETRY_CJK_RATIO


def call_glm(api_key, chunk):
    _pace(EN_RPM)
    with _call_sem:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)},
            ],
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        return json.loads(content)


def translate_batch(api_key, chunk, depth=0):
    """chunk: list of {company,keywords,address,note}; returns list or None after retries

    整批失败（实测多为 JSONDecodeError —— 模型把其中某一条吐坏了）时对半拆开重试：
    失败面从 8 条缩到 4 条，最坏拆到单条，避免一条坏数据拖累整批 8 条都退化成中文兜底。
    返回的列表**要么与入参等长，要么是 None**（2026-09-19 起不再返回短列表：
    短列表会被调用方按尾部补 None，等于碰运气对齐，见下面的条数校验）。
    """
    last_err = None
    for attempt in range(4):
        try:
            out = call_glm(api_key, chunk)
            # 2026-09-19 修复「条数错位」：模型偶尔少返回若干条，旧实现把这个短列表
            # 直接交回调用方、由调用方在**尾部补 None** —— 等于假定「前 N 条对齐」，
            # 错位那几条会整条退化成中文兜底（实测整条未翻 123 条的主因）。
            # 现在条数不符即视为本批失败，走下面的重试 / 拆批逻辑：拆到单条时
            # 「1 进 1 出」天然对齐，不用再猜哪条是哪个。
            if not isinstance(out, list) or len(out) != len(chunk):
                got = len(out) if isinstance(out, list) else type(out).__name__
                raise ValueError("返回条数不符：期望 %d，实得 %s" % (len(chunk), got))
            return out
        except Exception as e:
            last_err = e
            # 429 -> 按服务端给的 Retry-After 退避；没给就拉长指数退避。
            # 免费 GLM-4-Flash 限速很凶，固定 12s 退避会让 3 个 worker 一起卡在
            # 退避-重试的死循环里空烧请求，实测吞吐掉到约 240 条/小时。
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                wait = 0
                try:
                    wait = int(e.headers.get("Retry-After", "0") or 0)
                except Exception:
                    wait = 0
                if wait <= 0:
                    wait = 20 * (attempt + 1)
                wait = min(wait, 90)
                # 抖动 0.5x~1.5x：打破多 worker 的退避锁步（雷群效应）
                wait = wait * (0.5 + random.random())
                log("    [429 限速] 退避 %.0fs 后重试（第 %d/%d 次）"
                    % (wait, attempt + 1, 4))
                time.sleep(wait)
            else:
                time.sleep(3 * (attempt + 1))

    # 整批重试 4 次仍失败 -> 对半拆开重试（depth 上限 3，即 8->4->2->1）
    if len(chunk) > 1 and depth < 3:
        mid = len(chunk) // 2
        log("    [拆批重试] %d 条失败，拆成 %d + %d 重试"
            % (len(chunk), mid, len(chunk) - mid))
        left = translate_batch(api_key, chunk[:mid], depth + 1)
        right = translate_batch(api_key, chunk[mid:], depth + 1)
        if left is not None or right is not None:
            return (left or []) + (right or [])
    log("    batch FAILED after retries: %r" % (last_err,))
    return None


def build_en_record(zh, t, cat, bucket=""):
    region = zh.get("region", {}) or {}
    prov = region.get("province", "")
    city = region.get("city", "")
    phone = str(zh.get("contact_phone", "") or "").strip()
    if phone == "待核实":
        phone = "Pending verification"
    t = t or {}
    rec = {
        "id": zh["id"],
        "company_en": (t.get("company") or zh.get("company", "")).strip(),
        "category": cat,
        "category_en": CATEGORY_EN.get(cat, cat),
        "keywords_en": t.get("keywords") or zh.get("keywords", []),
        "region": {
            "province": REGION_EN.get(prov, prov),
            "city": REGION_EN.get(city, city),
        },
        "address_en": (t.get("address") or zh.get("address") or "").strip(),
        "contact_phone": phone,
        "source": zh.get("source", ""),
        "verified_at": zh.get("verified_at", ""),
        # note 为主库采集模板句（高德POI抓取/公开名录），无业务价值且点名数据源 -> 不翻译，置空
        "note_en": "",
        # industry 是与语言无关的结构化元数据，必须 == ZH 同 id 记录；否则 validate 的
        # bucket_of 会落位失败（EN 镜像缺 industry -> _unclassified），发布闸跳过、云端不更新。
        # 2026-09-16 补：之前漏拷，导致每次回填的新 EN 记录缺 industry，validate 反复失败。
        "industry": (json.loads(json.dumps(zh["industry"])) if zh.get("industry") else None),
        "industry_en": (json.loads(json.dumps(zh["industry_en"])) if zh.get("industry_en") else None),
    }
    if bucket:
        rec["_bucket"] = bucket  # 与 migrate_en_to_gb.py 同口径，便于单条定位归属
    return rec


def _atomic_write_json(path: Path, payload: list) -> None:
    """原子写：先写临时文件再替换，杜绝被 kill 时留下半个 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_en_sharded(en_path: Path, recs: list) -> None:
    """按 gb_store.MAX_PER_FILE 写英文桶（与主库同一套分片布局）。

    为什么要分片：英文库的其它写入方（en_sync_industry）分片，读客也是按
    主文件 + -pN 累加的口径。本模块若整桶写主文件，两套布局并存 → 跨分片
    重复 id。统一后就不再有「写了主文件还得记得删分片」这种脆弱约定。
    """
    step = MAX_PER_FILE
    keep: set[Path] = set()
    if not recs:
        for old in _en_shard_files(en_path):
            try:
                old.unlink()
            except OSError:
                pass
        return
    for i in range(0, len(recs), step):
        tgt = en_path if i == 0 else en_path.parent / (
            "%s-p%d.json" % (en_path.stem, i // step + 1))
        _atomic_write_json(tgt, recs[i:i + step])
        keep.add(tgt)
    # 分片变少时（比如中文桶删了重复），多余的旧片必须删掉，否则残留的旧记录
    # 会与新布局累加出重复 id
    for old in _en_shard_files(en_path):
        if old not in keep:
            try:
                old.unlink()
            except OSError:
                pass


def _en_shard_files(en_path: Path) -> list[Path]:
    """英文桶的主文件 + 所有 -pN 分片。"""
    out = [en_path] if en_path.exists() else []
    i = 2
    while True:
        f = en_path.parent / ("%s-p%d.json" % (en_path.stem, i))
        if not f.exists():
            break
        out.append(f)
        i += 1
    return out


def _family_files(path: Path) -> list[Path]:
    """一个「族」的全部文件：主文件 + 所有 -pN 分片。

    主库和英库都按 MAX_PER_FILE 分片，所以 6210.json 与 6210-p2.json 是
    **同一个逻辑桶**的两片，不是两个桶。用 glob 而非顺序探测，容忍序号不连续。
    """
    out = [path] if path.exists() else []

    def _ord(p: Path) -> int:
        try:
            return int(re.sub(r"\D", "", p.stem.rsplit("-p", 1)[-1]))
        except Exception:
            return 0

    out.extend(sorted(path.parent.glob(path.stem + "-p*.json"), key=_ord))
    return out


def _family_root(zh_path: Path) -> str:
    """把 .../6210-p2.json 归一到族根 bucket 名（如 H/62/6210）。"""
    rel = zh_path.relative_to(SRC_DIR).with_suffix("").as_posix()
    return re.sub(r"-p\d+$", "", rel)


# 2026-09-19 修复：族锁 —— 同一族的两次处理绝不允许并发。
# 事故回放：main() 原来用 rglob("*.json") 枚举桶，把 -pN 分片也当成独立桶丢进
# 16 线程池；于是「族根任务」读整族、按 MAX_PER_FILE 重写整族分片，而「分片任务」
# 也写同一个分片文件 → 两个线程同文件并发写，各自拿开跑时的 existing 快照做
# 整文件覆盖，后写盖掉先写。后果：翻译成果丢失 + 跨分片重复 id（实测 H/62/6210
# 族 2161 条、6232 族 1040、6231 族 455、6291 族 273、C/34/3484 族 29，合计 3958）。
# 进程级 _EN_LOCK 只挡别的**进程**，挡不住本进程内的**线程**，故在此加族锁兜底。
_family_locks: dict[str, threading.Lock] = {}
_family_locks_guard = threading.Lock()


def _family_lock(family: str) -> threading.Lock:
    with _family_locks_guard:
        lk = _family_locks.get(family)
        if lk is None:
            lk = threading.Lock()
            _family_locks[family] = lk
        return lk


def process_bucket(api_key, zh_path, limit=None, retranslate=False, heavy_only=False):
    """处理一个国标桶**族**（主文件 + 全部 -pN 分片）。

    返回 (bucket, translated, total_missing)。族内串行，族间可并发。
    """
    bucket = _family_root(zh_path)
    with _family_lock(bucket):
        return _process_bucket_locked(api_key, zh_path, bucket, limit, retranslate,
                                      heavy_only)


def _process_bucket_locked(api_key, zh_path, bucket, limit=None, retranslate=False,
                           heavy_only=False):
    """处理一个国标桶：翻译缺失 id 并合并写回；返回 (bucket, translated, total_missing)

    retranslate=True 时，除了「en 里没有的 id」，还会把 en 里**仍是中文兜底**的
    记录（上一轮翻译失败留下的）一并重翻 —— 否则它们永远卡在 existing 里不被处理。
    heavy_only=True（配合 --retranslate-heavy）时只捞 cjk_heavy 的「真缺陷」，
    放过「英文品牌名 + 一并保留的中文原名」那类可接受的残留，省配额也不误伤。
    """
    # 2026-09-19 修复：中文主库同样按 MAX_PER_FILE 分片，必须把整族中文分片一起读，
    # 否则 -p2/-p3 里的记录永远轮不到翻译（原实现只读主文件，分片靠被误当独立桶
    # 的另一个任务来翻，正是并发写冲突的来源）。
    zh_recs: list = []
    for zf in _family_files(zh_path):
        try:
            _d = load_json(zf)
        except Exception as e:  # noqa: BLE001
            log("[%s] 警告：中文分片 %s 解析失败，跳过：%r" % (bucket, zf.name, e))
            continue
        if isinstance(_d, list):
            zh_recs.extend(_d)
    if not zh_recs:
        log("[%s] 空桶，跳过" % bucket)
        return bucket, 0, 0
    cat = zh_recs[0].get("category") or ""
    en_path = EN_DIR / (bucket + ".json")
    # 2026-09-16 修正：英文镜像可能已被下游按 MAX_PER_FILE 拆成 -pN 分片。
    # 只读主文件会漏掉 -p2/-p3 里的记录 → 它们被当成「缺失」重翻，且旧 -pN 残留
    # 会与主文件累加出跨分片重复 id（CN-MFG-0088605 类事故根因）。这里把同桶所有
    # 分片都读进来合并成 existing。
    en_shards = sorted(en_path.parent.glob(en_path.stem + "-p*.json"))
    en_files = [en_path] + en_shards
    existing = {}
    for ef in en_files:
        if ef.exists():
            try:
                for r in load_json(ef):
                    if isinstance(r, dict) and r.get("id"):
                        existing[r["id"]] = r
            except Exception as e:
                # 文件损坏（上次被 kill 打断写盘）：丢弃重翻本桶
                log("[%s] 警告：%s 解析失败，从零重翻本桶：%r" % (bucket, ef.name, e))
                try:
                    ef.rename(ef.with_suffix(".corrupt"))
                except Exception:
                    pass
                if ef == en_path:
                    existing = {}
    missing = [r for r in zh_recs if r["id"] not in existing]
    # --retranslate：把上一轮翻译失败、仍残留中文的记录拽回来再翻一遍
    if retranslate:
        _pick = cjk_heavy if heavy_only else is_untranslated
        redo_ids = {rid for rid, rec in existing.items() if _pick(rec)}
        if redo_ids:
            redo = [r for r in zh_recs if r["id"] in redo_ids]
            if redo:
                log("[%s] 其中 %d 条是中文兜底（%s），纳入重翻"
                    % (bucket, len(redo),
                       "仅中文占比过高的真缺陷" if heavy_only else "上轮翻译失败残留"))
            missing = missing + redo
    def idnum(r):
        try:
            return int(re.sub(r"\D", "", r["id"]))
        except Exception:
            return 0

    if not missing:
        log("[%s] 无缺失，已有 %d 条" % (bucket, len(existing)))
        # 2026-09-19 修正：原实现「无缺失就删掉 -pN 分片、假定主文件已持全量」有隐患 ——
        # 主文件未必真的持有分片里的记录，直接删会丢数据。改为一律做规整写回：
        # 把整族按 MAX_PER_FILE 重排，顺带把跨分片重复 id 收敛掉。只有确实存在分片
        # 的族才写（绝大多数桶单片，直接 return，避免无谓的大文件重写）。
        if len(_family_files(en_path)) > 1:
            _write_en_sharded(en_path, sorted(existing.values(), key=idnum))
        return bucket, 0, 0
    if limit:
        missing = missing[:limit]
    log("[%s] 需翻译 %d 条（已有 %d 条）" % (bucket, len(missing), len(existing)))

    def flush(new_recs):
        # 每批写回：被 kill 最多丢 1 批（8 条），重启自动续跑。
        # 必须是 dict 合并而不是列表拼接：--retranslate 时同一 id 会同时存在于
        # existing 和 new_recs，列表拼接会写出重复 id 的记录（去重翻反而更脏）。
        merged_map = dict(existing)
        merged_map.update(new_recs)
        merged = list(merged_map.values())
        # 统一把中文占位"待核实"转为英文占位（含历史已有记录）
        for r in merged:
            if str(r.get("contact_phone", "")).strip() == "待核实":
                r["contact_phone"] = "Pending verification"
        merged.sort(key=idnum)
        # 2026-09-14：门类扩到 7 个后，新门类的英文桶（data/en/gb/H/62/6210.json）
        # 整个目录树都是不存在的 —— 原来直接 open(w) 会 FileNotFoundError，
        # 一个桶挂掉就整个线程炸掉，新门类永远翻不出来（实测 H/O/I/M/R 全挂）。
        en_path.parent.mkdir(parents=True, exist_ok=True)
        # 2026-09-19 修正：整桶塞进主文件 + 删 -pN 分片 是**错的做法**。
        # 全流水线只有英文库这一处这么写，其余（gb_store._write_bucket、
        # en_sync_industry._write_en_bucket）都按 MAX_PER_FILE 分片。
        # 两套布局并存时必然会打架：本轮刚删掉旧 -p2，另一个写入方（或被
        # kill 后重启的本进程）又按分片规则把它写出来 —— 于是主文件与 -p2
        # 各持有一批相同 id，实测跨分片重复 15,989 个 id（占英文库 12%）。
        # 正确解是入乡随俗：按同一 MAX_PER_FILE 写分片，并把不再需要的旧片删掉。
        _write_en_sharded(en_path, merged)

    new_recs = {}
    items = [
        {
            "company": r.get("company", ""),
            "keywords": r.get("keywords", []),
            "address": r.get("address", "") or "",
        }
        for r in missing
    ]
    # 2026-09-19 二次改造：桶内批次也并发（只按桶并发会退化成「最慢桶单线程爬」）。
    # 并发边界：翻译（纯网络）完全并行；**合并写回必须串行** —— _flush_lock 把
    # 「new_recs 累加 + flush()」整段包住，同一族的英文文件写入仍旧严格排队，
    # 不会出现两个线程各拿一份快照互相覆盖（那正是 2026-09-19 事故的形态）。
    _flush_lock = threading.Lock()
    n_batches = (len(items) + BATCH - 1) // BATCH
    done = 0

    def run_batch(i):
        nonlocal done
        chunk = items[i : i + BATCH]
        recs = missing[i : i + BATCH]
        try:
            translated = translate_batch(api_key, chunk)
        except Exception as e:  # noqa: BLE001 —— 单批异常不该带崩整个桶
            log("[%s] 批 %d 异常（本批留中文兜底，下轮 --retranslate 会重翻）：%r"
                % (bucket, i // BATCH + 1, e))
            translated = None
        if translated is None:
            translated = [None] * len(recs)
        if len(translated) < len(recs):
            translated = list(translated) + [None] * (len(recs) - len(translated))
        with _flush_lock:
            for j, rec in enumerate(recs):
                new_recs[rec["id"]] = build_en_record(rec, translated[j], cat, bucket)
            flush(new_recs)  # 增量写回，断点续跑更安全
            done += len(recs)
            log("[%s] 批 %d/%d 完成（%d/%d）"
                % (bucket, i // BATCH + 1, n_batches, done, len(items)))

    if BUCKET_WORKERS > 1 and n_batches > 1:
        # map() 会一次性提交全部批次；异常在迭代结果时抛出，with 退出前等全部跑完，
        # 所以某一批炸掉不会連累后面的批次（与「单桶不拖垮整轮」的口径一致）。
        with ThreadPoolExecutor(max_workers=min(BUCKET_WORKERS, n_batches)) as iex:
            list(iex.map(run_batch, range(0, len(items), BATCH)))
    else:
        for i in range(0, len(items), BATCH):
            run_batch(i)

    # 2026-09-19 新增：残留重试。主批次跑完后，把「英文字段里中文占比 >=
    # RETRY_CJK_RATIO」的记录当场重翻 —— 第 1 轮按 BATCH 批量（省配额），
    # 第 2 轮按单条（1 进 1 出，天然对齐，专治模型照抄输入）。
    # 覆盖规则：只在中文变少时才替换，保证单调收敛、不会越重试越脏。
    if new_recs and RETRY_ROUNDS > 0:
        _zh_of = {r["id"]: r for r in missing}

        def _retry_chunk(ids):
            ch = [
                {
                    "company": _zh_of[i].get("company", ""),
                    "keywords": _zh_of[i].get("keywords", []),
                    "address": _zh_of[i].get("address", "") or "",
                }
                for i in ids
            ]
            try:
                tr = translate_batch(api_key, ch)
            except Exception as e:  # noqa: BLE001
                log("[%s] 残留重试异常：%r" % (bucket, e))
                tr = None
            if tr is None:
                tr = [None] * len(ids)
            if len(tr) < len(ids):
                tr = list(tr) + [None] * (len(ids) - len(tr))
            with _flush_lock:
                for j, i in enumerate(ids):
                    cand = build_en_record(_zh_of[i], tr[j], cat, bucket)
                    if cjk_len(cand) < cjk_len(new_recs[i]):
                        new_recs[i] = cand
                flush(new_recs)

        for rnd in range(RETRY_ROUNDS):
            pend = [i for i, r in new_recs.items()
                    if i in _zh_of and cjk_heavy(r)]
            if not pend:
                break
            size = BATCH if rnd == 0 else 1
            log("[%s] 残留重试 %d/%d 轮：%d 条中文占比>=%.0f%%（%d 条/批）"
                % (bucket, rnd + 1, RETRY_ROUNDS, len(pend),
                   RETRY_CJK_RATIO * 100, size))
            for k in range(0, len(pend), size):
                _retry_chunk(pend[k : k + size])
        left = [i for i in new_recs if i in _zh_of and cjk_heavy(new_recs[i])]
        if left:
            log("[%s] 残留重试后仍有 %d 条中文占比过高（例：%s）"
                % (bucket, len(left), ", ".join(left[:5])))

    log("[%s] 写回 %s：%d 条（新增 %d）"
        % (bucket, en_path.relative_to(ROOT).as_posix(),
           len(existing) + len(new_recs), len(new_recs)))
    return bucket, len(new_recs), len(missing)


# ---------------------------------------------------------------- 单实例锁
# 2026-09-16 补：两个流水线同时回填英文（cron 双开 / cron + GUI），会对同一个
# data/en/gb/<bucket>.json 做「读 → 合并 → **整桶写回**」。后写的那桶会把先写的
# 整桶覆盖 —— 丢翻译还算轻的，并发瞬间还可能写出坏 JSON。
# 抓取那边早就有 msvcrt 咨询锁（fetch_gaode_poi.acquire_fetch_lock），翻译这边
# 一直裸奔，这里补上。刻意**不复用** fetch 那把锁：职责不同，别让翻译把抓取挡在外面。
# 用 msvcrt 咨询锁的好处：进程被强杀时 OS 自动放句柄，锁随之释放，不会死锁。
_EN_LOCK_FD = None
_EN_LOCK_PATH = Path(tempfile.gettempdir()) / "beacon_mfg_en.lock"


def acquire_en_lock() -> bool:
    """拿到锁返回 True；已被别的进程持有返回 False（非阻塞）。"""
    global _EN_LOCK_FD
    if os.environ.get("BMFG_EN_LOCK") == "0":   # 逃生阀：实在要并发时手动关
        return True
    if _EN_LOCK_FD is not None:
        return True
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    try:
        fd = os.open(str(_EN_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return True   # 连锁文件都建不了就算了，别把正事挡在外面
    if msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
    _EN_LOCK_FD = fd
    atexit.register(release_en_lock)
    return True


def release_en_lock() -> None:
    global _EN_LOCK_FD
    if _EN_LOCK_FD is None:
        return
    try:
        import msvcrt
        msvcrt.locking(_EN_LOCK_FD, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        os.close(_EN_LOCK_FD)
    except Exception:
        pass
    _EN_LOCK_FD = None


def main():
    global EN_RPM, BUCKET_WORKERS
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("ZHIPU_API_KEY", ""))
    ap.add_argument("--workers", type=int, default=16,
                    help="并发 worker 数（限流由令牌桶 EN_RPM 主导，这里只管延迟重叠）")
    ap.add_argument("--bucket-workers", type=int, default=BUCKET_WORKERS,
                    help="单个桶**内部**的批次并发度（默认 %d，或环境变量 "
                         "EN_BUCKET_WORKERS）。只按桶并发时，尾巴会退化成「最慢那个桶"
                         "单线程爬」——2026-09-19 实测剩 4 个大桶时 16 个 worker 里 "
                         "12 个空转。总并发仍受 EN_MAX_CONCURRENT / EN_RPM 兜底。"
                         % BUCKET_WORKERS)
    ap.add_argument("--rpm", type=int, default=EN_RPM,
                    help="请求/分钟上限（令牌桶，规避 GLM 免费档限流；可用 EN_RPM 环境变量覆盖）")
    ap.add_argument("--limit", type=int, default=0, help="每桶最多翻译条数（测试用）")
    ap.add_argument("--bucket", default="", help="只处理指定国标桶，如 C/34/3484")
    ap.add_argument("--retranslate", action="store_true",
                    help="把 en 里仍是中文兜底的记录（上轮翻译失败残留）一并重翻；"
                         "默认只补 en 里没有的 id")
    ap.add_argument("--retranslate-heavy", action="store_true",
                    help="只重翻「中文占比 >= EN_RETRY_CJK_RATIO」的真缺陷，放过"
                         "「英文品牌名 + 中文原名」那类可接受的残留（省配额、不误伤）")
    args = ap.parse_args()
    EN_RPM = args.rpm
    BUCKET_WORKERS = max(1, args.bucket_workers)
    api_key = args.key or load_env().get("ZHIPU_API_KEY", "")
    if not api_key:
        print("缺少 API Key：设环境变量 ZHIPU_API_KEY 或 .env 配置")
        sys.exit(1)

    if not acquire_en_lock():
        print("⛔ 另一个英文回填进程正在写 data/en（锁文件：%s），本次退出。" % _EN_LOCK_PATH)
        print("   两个进程同时写一个桶会整桶覆盖对方的翻译，甚至写出坏 JSON。")
        print("   等那一轮结束再跑；确认没有别的进程时删掉锁文件即可。")
        sys.exit(3)

    if not SRC_DIR.exists():
        print("中文主库不存在：%s" % SRC_DIR)
        sys.exit(1)
    EN_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SRC_DIR.rglob("*.json"))
    # 2026-09-19 修复：把 -pN 分片归并到族根，分片不再作为独立任务提交。
    # 否则族根任务与分片任务会并发重写同一个英文分片文件（见 _family_lock 注释）。
    _by_root: dict[str, Path] = {}
    for f in files:
        _root = f.with_name(re.sub(r"-p\d+$", "", f.stem) + f.suffix)
        _by_root.setdefault(_root.as_posix(), _root)
    files = [_by_root[k] for k in sorted(_by_root)]
    # 防线：归并后的族根必须真实存在。若不校验，路径写错会一路走到「空桶，跳过」
    # ——整库静默跳过、退出码 0，看着像成功（2026-09-19 踩过：with_name 忘了拼
    # 回 .json 后缀，271 个桶全部判空桶）。
    _bad_roots = [str(p) for p in files if not p.exists()]
    if _bad_roots:
        print("[错误] 归并族根后存在不存在的路径（疑似命名逻辑出错，拒绝静默跳过）：")
        for _p in _bad_roots[:5]:
            print("   -", _p)
        sys.exit(1)
    if args.bucket:
        want = args.bucket.strip("/").replace("\\", "/")
        want = re.sub(r"-p\d+$", "", want)  # 传了分片名也归到族根
        files = [f for f in files
                 if f.relative_to(SRC_DIR).with_suffix("").as_posix() == want]
        if not files:
            print("没有匹配的桶：%s" % args.bucket)
            sys.exit(1)
    if not files:
        print("[错误] %s 下没有任何分片——数据源路径不对，拒绝静默返回 0 条。" % SRC_DIR)
        sys.exit(1)

    total_new = 0
    failed_buckets = []
    _heavy_only = args.retranslate_heavy
    _redo = args.retranslate or _heavy_only
    if _heavy_only:
        print("模式：--retranslate-heavy（只重翻中文占比 >= %.0f%% 的真缺陷）"
              % (RETRY_CJK_RATIO * 100))
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(process_bucket, api_key, f, args.limit or None,
                      _redo, _heavy_only): f.name
            for f in files
        }
        for fut in futs:
            # 2026-09-14：单个桶炸掉不该让整轮翻译全废 —— 119 个桶里只要有 1 个
            # 抛异常，fut.result() 会把异常一路抛到 main()，前面几个桶的成果虽然
            # 已落盘，但后面的桶全部不跑了，且退出码非 0 看着像「整体失败」。
            # 改成记错继续，末尾汇总失败桶数。
            try:
                _name, added, missing = fut.result()
                total_new += added
            except Exception as e:  # noqa: BLE001
                failed_buckets.append("%s: %s: %s" % (futs[fut], type(e).__name__, e))
                continue
    print("全部完成：新增英文 %d 条" % total_new, flush=True)
    if failed_buckets:
        print("\n[警告] %d 个桶失败（其余已落盘，可重跑本命令续翻）：" % len(failed_buckets))
        for b in failed_buckets[:20]:
            print("   -", b)
    print("下一步：python scripts/en_sync_industry.py（补 industry_en 标签）")


if __name__ == "__main__":
    main()
