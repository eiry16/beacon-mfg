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
enum class Evidence(val code: Int, val label: String, val hint: String) {
    LITERAL(0, "字面命中", "企业自己写了这个词"),
    ALIAS_PRIMARY(1, "别名首位码", "按国标小类匹配，语义最贴近"),
    ALIAS_SECONDARY(2, "行业推断", "按国标行业推断，企业未确认");

    companion object {
        fun of(code: Int): Evidence = entries.firstOrNull { it.code == code } ?: ALIAS_SECONDARY
    }
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

data class SearchParams(
    val keyword: String? = null,
    val city: String? = null,
    val industryCode: String? = null,
    val cert: String? = null,
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
    val offline: Boolean = false,
)
