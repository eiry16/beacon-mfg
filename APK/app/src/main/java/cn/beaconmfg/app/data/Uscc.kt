package cn.beaconmfg.app.data

/**
 * 统一社会信用代码（USCC）解析与校验。
 *
 * 算法移植自 `server/routers/certification.py` 的 `uscc_check()`，
 * 两边必须保持一致——否则会出现"App 说合法、服务端说非法"这种最伤信任的不一致。
 *
 * ## 一个必须反复强调的边界
 *
 * **校验位通过 ≠ 企业真实存在。**
 * GB 32100-2015 的校验位只保证"这串号码在编码规则上没写错"，
 * 任何人都能随手构造出一串校验位正确的假号码。
 * 要证明"这确实是这家企业"，只能靠登记机关的权威数据（电子营业执照系统接入，
 * 见 docs/ 下的接入说明）。这里不做、也做不了那一步。
 */
object Uscc {

    /** 31 个字符，刻意不含 I O S V Z（避免与 1/0 混淆）。 */
    private const val CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"

    private val WEIGHTS = intArrayOf(1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)

    private const val LEN = 18

    /** 18 位连续合法字符。用于从扫码原文里捞号码。 */
    private val CANDIDATE = Regex("[0-9A-HJ-NPQRTUWXY]{18}")

    /**
     * 号码里常见的分组分隔符。
     *
     * 空格类交给 `isWhitespace()`（覆盖半角/全角空格、tab、换行），
     * 这里只列非空白的那些：号码从文档里复制出来时经常带连字符或中点。
     *
     * **刻意不剥 `.`**：URL 里点太多（www.gsxt.gov.cn），剥掉容易把不相干的内容
     * 粘成一个假号码。带点的号码分组本身也罕见，不值得冒这个险。
     */
    private val SEPARATORS = setOf('-', '－', '—', '·', '\u00A0')

    /**
     * 校验一段文本是不是合法的 USCC。
     *
     * 返回 (是否合法, 说明)。说明会直接给用户看，所以要把"错在哪"讲清楚。
     */
    fun check(raw: String?): Pair<Boolean, String> {
        if (raw.isNullOrBlank()) return false to "未提供统一社会信用代码"
        val s = raw.uppercase().filter { !it.isWhitespace() }
        if (s.length != LEN) return false to "长度应为 18 位，实际 ${s.length} 位"
        val bad = s.filter { it !in CHARSET }
        if (bad.isNotEmpty()) {
            return false to "含非法字符 ${bad.toSet().joinToString("")}（USCC 不含 I/O/S/V/Z）"
        }
        val total = (0 until LEN - 1).sumOf { CHARSET.indexOf(s[it]) * WEIGHTS[it] }
        val mod = total % 31
        val expect = if (mod == 0) 0 else 31 - mod
        val actual = CHARSET.indexOf(s[LEN - 1])
        if (actual != expect) {
            return false to "校验位错误（应为 ${CHARSET[expect]}，实际是 ${s[LEN - 1]}）"
        }
        return true to "格式与校验位正确"
    }

    /**
     * 从任意文本里提取 USCC。
     *
     * 接受的场景：
     * - 扫码直接解出 18 位号码
     * - 扫出的是 URL，号码藏在路径或查询参数里
     * - 用户手输时前面带了空格、换行、"统一社会信用代码：" 之类前缀
     * - 用户按 4-4-4-4-2 分组打了空格或连字符（手输 18 位时的常见做法）
     *
     * **必须先剥掉分隔符再匹配**：早期版本直接拿原文匹配，结果
     * `9131 0000 7664 9225 6E` 和 `9131-0000-7664-9225-6E` 一条都提不出来——
     * 而手机上这么打的人很多，从文档里复制的人也很多。
     * 剥掉分隔符不会误连无关内容：中文、标点都不在字符集里，天然断开。
     *
     * 多个候选时**优先返回校验位通过的那个**——扫码/手输都可能带噪声，
     * 能过校验位的那个几乎一定是真的。
     */
    fun extract(text: String?): String? {
        if (text.isNullOrBlank()) return null
        val flat = text.uppercase().filter { !it.isWhitespace() && it !in SEPARATORS }
        val cands = CANDIDATE.findAll(flat).map { it.value }.toList()
        if (cands.isEmpty()) return null
        return cands.firstOrNull { check(it).first } ?: cands.first()
    }

    /**
     * USCC 第 3–8 位是**登记管理机关行政区划码**，前两位即省级。
     *
     * 这个值只用作**回显给用户自检**（"你扫的这家登记在上海市，对吗？"），
     * 不参与任何判定：行政区划码相同的企业有几百万家，它证明不了任何归属关系。
     */
    fun regionOf(uscc: String): String? {
        val s = uscc.uppercase().filter { !it.isWhitespace() }
        if (s.length < 4) return null
        return PROVINCE[s.substring(2, 4)]
    }

    /** 省级行政区划前两位。值取自 GB/T 2260。 */
    private val PROVINCE = mapOf(
        "11" to "北京市", "12" to "天津市", "13" to "河北省", "14" to "山西省", "15" to "内蒙古自治区",
        "21" to "辽宁省", "22" to "吉林省", "23" to "黑龙江省",
        "31" to "上海市", "32" to "江苏省", "33" to "浙江省", "34" to "安徽省",
        "35" to "福建省", "36" to "江西省", "37" to "山东省",
        "41" to "河南省", "42" to "湖北省", "43" to "湖南省",
        "44" to "广东省", "45" to "广西壮族自治区", "46" to "海南省",
        "50" to "重庆市", "51" to "四川省", "52" to "贵州省", "53" to "云南省",
        "54" to "西藏自治区", "61" to "陕西省", "62" to "甘肃省", "63" to "青海省",
        "64" to "宁夏回族自治区", "65" to "新疆维吾尔自治区",
        "71" to "中国台湾省", "81" to "中国香港特别行政区", "82" to "中国澳门特别行政区",
    )
}
