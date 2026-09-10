package cn.beaconmfg.app.data

/**
 * 检索证据档位。
 *
 * 为什么要分档：别名扩展是「整类扩展」而不是同义词扩展。搜「齿轮」命中 3453（6 家）
 * 的同时也会把 3484 机械零部件加工（3415 家）整类带进来。不标注的话，
 * LLM 会把「这家只是被归在这一类」说成「这家做齿轮」——那就是编造。
 *
 * 三档与 scripts/query.py 的 _alias_rank 一一对应，不要各改各的。
 */
enum class Evidence(val code: Int) {
    LITERAL(0),
    ALIAS_PRIMARY(1),
    ALIAS_SECONDARY(2);

    companion object {
        fun of(code: Int): Evidence = entries.firstOrNull { it.code == code } ?: ALIAS_SECONDARY
    }

    /** 档位名的文案放在 Strings 里（zh/en 两套），这里不硬编码。 */
    fun label(s: cn.beaconmfg.app.i18n.Strings): String = s.evidenceLabel(code)
    fun hint(s: cn.beaconmfg.app.i18n.Strings): String = s.evidenceHint(code)
}

/** L0 指纹层的一条记录。字段刻意用短键，这一层要被全量装载进内存。 */
data class Fingerprint(
    val id: String,
    val co: String,          // 公司名 + 关键词（拼接成一行，便于字面匹配）
    val city: String,
    val gb: String,          // 国标码（4 位小类 / 3 位中类），无码为空串
    val mf: Boolean,         // 是否制造商
    val proc: List<String>,  // 工艺码
    val mat: List<String>,   // 材料
    val cert: List<String>,  // 认证名
    val cl: String,          // 认证灯牌 L0/L1/L2/L3
    val pv: String,          // 来源 auto / vendor_claimed / derived / fixture
    val sc: Int,             // 能力画像分（无卡为 0）
    val tel: Boolean,        // 是否有可用电话
    /**
     * 电话号码。指纹层刻意不存它（L0 要保持最小、可被 Agent 全量扫描），
     * 由 DataStore 在装载时用内置索引 assets/index/phone-index.jsonl 补上
     * （构建期从 data/gb 完整档案抽出，见 tools/sync_assets.py）。
     * 空串 = 源数据里是「待核实」占位值，按红线留空、不猜号。
     */
    val phone: String = "",
) {
    /** 公司名：co 的第一段。指纹层为省字节把关键词也拼进去了。 */
    val name: String get() = co.substringBefore(' ').ifBlank { co }
    val gbName: String get() = GbIndex.nameOf(gb)
}

data class Hit(
    val fp: Fingerprint,
    val evidence: Evidence,
)

/**
 * 供应商认证等级（对外叫「灯牌」）。等级定义见 docs/CERTIFICATION_V1.md §1。
 *
 * 客户端的两条红线和服务端一样：
 *  1. **等级只能来自数据**。它是服务端 `evaluate` 算出来的（没有 set_badge 这种接口），
 *     App 不许自己升级、也不许「看着像 L2」就美化：认不出 / 缺失的值一律按 L0 展示。
 *  2. **灯牌 ≠ 评级**。它说的是「这家企业的信息被核验到什么程度」，不说这家厂好不好。
 *     所以凡有徽章的地方都要能点到看到这句限定（详情页给出提示行）。
 */
enum class CertTier(val code: String, val rank: Int) {
    /** 自动收录的公开名录，未经任何核验 */
    L0("L0", 0),

    /** 主体已认领，信息由企业自述 */
    L1("L1", 1),

    /** 营业执照核验 + 材料齐备 + 人工复核 */
    L2("L2", 2),

    /** 第三方实地验厂或客户案例佐证 */
    L3("L3", 3),
    ;

    companion object {
        fun of(raw: String?): CertTier {
            val c = raw?.trim()?.uppercase() ?: ""
            return entries.firstOrNull { it.code == c } ?: L0
        }

        /** 可接自动询价的最低等级（CERTIFICATION_V1 §1：L2 起）。 */
        const val AUTO_RFQ_RANK = 2
    }

    fun label(s: cn.beaconmfg.app.i18n.Strings): String = s.tierLabel(code)
    fun hint(s: cn.beaconmfg.app.i18n.Strings): String = s.tierHint(code)

    val acceptsAutoRfq: Boolean get() = rank >= AUTO_RFQ_RANK
}

/**
 * 认证存证的公开摘要（来自完整档案的 `certification` 块）。
 *
 * 字段可能整体缺失（未认证的企業就没有这个块）——那是常态不是错误，
 * UI 按「没 Certification 对象」处理，不许反推出一个假的默认值。
 */
data class Certification(
    val appId: String,
    val badge: String,
    val issuedAt: String,
    val expiresAt: String,
    val reviewer: String,
    val completeness: Double?,
) {
    val tier: CertTier get() = CertTier.of(badge)

    /**
     * 有效期是否已过。日期是 ISO yyyy-MM-dd，字典序即时间序，直接比字符串。
     * 注意：**过期不等于降级**——降级由服务端规则做，App 只是把事实说出来，
     * 让用户知道这份材料是哪一年的。
     */
    fun expired(todayIso: String): Boolean =
        expiresAt.isNotBlank() && todayIso.isNotBlank() && todayIso > expiresAt
}

data class SearchParams(
    val keyword: String? = null,
    val city: String? = null,
    val industryCode: String? = null,
    val cert: String? = null,
    /**
     * 认证等级下限："L1" / "L2" / "L3"。null = 不限。
     * 灯牌由规则算出、绝大多数企业目前是 L0，所以筛 L2 常常只有个位数结果——
     * 检索会如实返回 0 并告知放宽条件，绝不为了给结果悄悄降级。
     */
    val minBeacon: String? = null,
    val manufacturerOnly: Boolean = false,
    val withPhoneOnly: Boolean = false,
    val limit: Int = 10,
)

data class SearchOutcome(
    val hits: List<Hit>,
    val total: Int,
    val literal: Int,
    val aliasPrimary: Int,
    val aliasSecondary: Int,
    /** 收敛把结果砍成 0 时，「放宽能拿多少家」。为 0 表示不放宽也够用。 */
    val relaxed: Int = 0,
)

/**
 * 一条工艺。level 分三档，来自能力卡的 processes[].level：
 *  primary 主营 / secondary 兼营 / outsourced 外协。
 * 不显示 level 的话，客户会以为「外协」也是这家厂自己做——那是误导。
 */
data class ProcItem(
    val code: String,
    val name: String,
    val level: String,
) {
    /** 主营/兼营/外协 → 随界面语言切换。不显示的话客户会把「外协」当成自家产能。 */
    fun levelLabel(s: cn.beaconmfg.app.i18n.Strings): String = s.levelLabel(level)
}

/**
 * L1 能力卡（精简版，内置在 assets/capability/）。
 *
 * 字段与 scripts/gen_capability_shards.py 的 slim 版一一对应。
 * **改这里必须同步改脚本**，否则 App 读到的全是空值且不报错。
 *
 * ⚠ 硬指标（limits）几乎全空是诚实结果：4136 张卡里只有 6 张有实质值，
 * 其余 4130 张是全 null 空壳。所以 limits 为空 map 时 UI 显示「未填报」，
 * 绝不显示 0——那等于告诉客户这家厂公差能做到 0。
 */
data class CapabilityCard(
    val id: String,
    val company: String,
    val gb: String,
    val gbName: String,
    val city: String,
    val province: String,
    val processes: List<ProcItem>,
    val materials: List<String>,
    /** 硬指标，只含有值的键。空 map = 企业未填报。 */
    val limits: Map<String, String>,
    val badge: String,
    /** auto 平台自动整理 / vendor_claimed 厂商自述 */
    val provenance: String,
    val hasPhone: Boolean,
    /** 厂商 skill 相对路径（如 skills/vendors/CN-MFG-0000005/SKILL.md） */
    val skillPath: String,
    val skillVerified: Boolean,
) {
    val isSelfReported: Boolean get() = provenance == "vendor_claimed"

    /** 把硬指标翻成展示行。没填的键不出现，不编造。文案随界面语言切换。 */
    fun limitLines(s: cn.beaconmfg.app.i18n.Strings): List<String> {
        val out = ArrayList<String>()
        limits["tol"]?.let { out.add(s.limitTol(it)) }
        limits["size"]?.let { out.add(s.limitSize(it)) }
        limits["moq"]?.let { out.add(s.limitMoq(it)) }
        limits["lt"]?.let { out.add(s.limitLt(it)) }
        limits["load"]?.let { out.add(s.limitLoad(it)) }
        if (limits["rush"] == "true") out.add(s.limitRush)
        return out
    }
}

/** 供应商完整档案（按需从 data/gb/ 下的小类分片拉取，不内置） */
data class SupplierDetail(
    val id: String,
    val company: String,
    val city: String,
    val province: String,
    val address: String,
    val phone: String,
    val website: String,
    val keywords: List<String>,
    val gb: String,
    val gbName: String,
    val gbPath: String,
    val certs: List<String>,
    val status: String,
    val isManufacturer: Boolean,
    val verifiedAt: String,
    /**
     * 认证等级（灯牌 L0/L1/L2/L3）。来自完整档案的 `cl` 字段；
     * 读不到就按 L0「未认领」展示——宁可显示得低，也不猜。
     */
    val beacon: String = "",
    /** 认证存证摘要。未认证的企业没有这个块，保持 null。 */
    val certification: Certification? = null,
    /**
     * 存证是否已过期（解析当天判一次，UI 直接用）。
     * 过期 ≠ 降级：降级由服务端规则做，这里只标个事实。
     */
    val certExpired: Boolean = false,
    /**
     * L1 能力卡。**内置在 APK 里**，不依赖网络——断网也能看到工艺位。
     * 为 null 表示这家厂还没有能力卡（23698 家里只有 4136 家有，约 17.5%）。
     */
    val cap: CapabilityCard? = null,
    val offline: Boolean = false,
)
