package cn.beaconmfg.app.search

import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.Fingerprint
import cn.beaconmfg.app.data.GbIndex
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.SearchOutcome
import cn.beaconmfg.app.data.SearchParams

/**
 * 检索内核 —— scripts/query.py:search 的 Kotlin 移植。
 *
 * 三条红线在这里落地，改动前先读：
 *  1. **字面 > 别名首位码 > 行业推断**，三档必须标注，排序按档位。
 *     弱档结果「企业未确认」，不能让 LLM 说成「这家做 XX」。
 *  2. **收敛砍成 0 时不静默放宽**。直接返回 0 会让用户以为「上海没有齿轮厂」，
 *     真相是「没有一家被归类为齿轮制造，只有 N 家行业推断」。所以返回 relaxed 计数 + 提示。
 *  3. **status=template 的占位数据默认排除**，但真实 POI 企业（unverified_poi）必须保留——
 *     指纹层已经在生成时过滤过，这里不再重复判定。
 */
class SearchEngine(
    private val store: DataStore,
    private val alias: AliasIndex,
) {

    private fun norm(s: String): String =
        s.replace("省", "").replace("市", "").replace("自治区", "")

    fun search(params: SearchParams): SearchOutcome {
        val all = store.fingerprints()
        val words = params.keyword?.trim()?.split(Regex("\\s+"))?.filter { it.isNotBlank() }
            ?: emptyList()
        val rankCache = HashMap<String, Map<String, Int>>()

        fun pass(p: SearchParams, broad: Boolean): ArrayList<Hit> {
            val out = ArrayList<Hit>()
            for (fp in all) {
                if (p.manufacturerOnly && !fp.mf) continue
                if (p.withPhoneOnly && !fp.tel) continue
                if (!p.city.isNullOrBlank() && norm(fp.city) != norm(p.city)) continue
                if (!p.industryCode.isNullOrBlank()) {
                    // 支持任意层级前缀：34 大类 / 343 中类 / 3434 小类
                    if (fp.gb.isEmpty() || !fp.gb.startsWith(p.industryCode)) continue
                }
                if (!p.cert.isNullOrBlank() && fp.cert.none { it.contains(p.cert) }) continue

                var worst = 0
                var ok = true
                for (w in words) {
                    var rank = 0
                    var hit = fp.co.contains(w, ignoreCase = true)
                    if (!hit) {
                        val ranks = rankCache.getOrPut(w) { alias.rankFor(w, broadMode = broad) }
                        rank = ranks[fp.gb] ?: 0
                        hit = rank > 0
                    }
                    if (!hit) {
                        ok = false
                        break
                    }
                    // 多词 AND 时取最弱的一环：整条结果的可信度由短板决定
                    if (rank > worst) worst = rank
                }
                if (!ok) continue
                out.add(Hit(fp, Evidence.of(worst)))
            }
            // 强命中排前面；同档内按能力画像分、有无电话
            out.sortWith(
                compareBy<Hit> { it.evidence.code }
                    .thenByDescending { it.fp.sc }
                    .thenByDescending { it.fp.tel }
            )
            return out
        }

        val hits = pass(params, broad = false)
        if (hits.isEmpty() && words.isNotEmpty()) {
            val relaxed = pass(params, broad = true).size
            return SearchOutcome(emptyList(), 0, 0, 0, 0, relaxed)
        }

        val literal = hits.count { it.evidence == Evidence.LITERAL }
        val primary = hits.count { it.evidence == Evidence.ALIAS_PRIMARY }
        val secondary = hits.count { it.evidence == Evidence.ALIAS_SECONDARY }
        val limited = if (params.limit > 0) hits.take(params.limit) else hits
        return SearchOutcome(limited, hits.size, literal, primary, secondary)
    }

    fun byId(id: String): Fingerprint? = store.fingerprints().firstOrNull { it.id == id }

    fun categories(parent: String?, limit: Int): List<GbIndex.Category> =
        GbIndex.categories(parent, limit)

    /** 给 LLM 回灌的精简字段。字段刻意少——传多了费 token，也更容易被模型添油加醋。 */
    fun toBrief(h: Hit): String {
        val fp = h.fp
        return buildString {
            append(fp.id).append(" | ").append(fp.name)
            append(" | ").append(fp.city.ifEmpty { "城市未知" })
            if (fp.gb.isNotEmpty()) append(" | ").append(fp.gb).append(" ").append(GbIndex.nameOf(fp.gb))
            append(" | 证据=").append(h.evidence.label)
            if (fp.cert.isNotEmpty()) append(" | 认证=").append(fp.cert.joinToString("、"))
            append(" | 电话=").append(if (fp.tel) "有" else "无")
            append(" | 灯牌=").append(fp.cl)
        }
    }
}
