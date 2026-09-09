package cn.beaconmfg.app.search

import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.GbIndex
import cn.beaconmfg.app.data.safeString
import org.json.JSONObject

/**
 * 采购词 → 国标小类的别名表（两层合并）。
 *
 * 这是 scripts/gb_store.py:load_alias 的 Kotlin 移植，规则必须逐条对齐：
 *
 *  - **两层**：gb-alias.json（数据推导，有真实命中数）+ gb-alias-curated.json（人工策展）。
 *    分两个文件是因为 rebuild_alias() 会整体覆写前者，混在一起会把策展词静默清空。
 *  - **同词冲突**：data 层的码排在前（证据强），curated 追加在后并去重。弱的不覆盖强的。
 *  - **max_supply 收敛**：策展的**补位码**（i > 0）若目标小类家数超过阈值就剔除，
 *    首位码永不剔除——它是语义核心，砍掉它「钣金」这类词会直接掉到 0 家。
 *  - 拿不到供给计数时**不过滤**：按「供给为 0」去过滤会把所有补位码都清掉，那是错杀。
 */
class AliasIndex(private val store: DataStore) {

    private class Entry(
        val code: String,
        val name: String,
        val hits: Int?,
        val source: String,
        val maxSupply: Int?,
    )

    /** 收敛后的表（默认） */
    private val converged = LinkedHashMap<String, ArrayList<Entry>>()
    /** 未收敛的表：只用于「放宽能拿多少家」的提示，不静默放宽 */
    private val broad = LinkedHashMap<String, ArrayList<Entry>>()

    private val defaultMaxSupply = 500

    fun load(): Int {
        if (converged.isNotEmpty()) return converged.size
        val data = store.readAsset("index/gb-alias.json")
        val curated = store.readAsset("index/gb-alias-curated.json")

        data?.let {
            val map = JSONObject(it).optJSONObject("alias")
            val keys = map?.keys() ?: return@let
            while (keys.hasNext()) {
                val word = keys.next()
                val arr = map.optJSONArray(word) ?: continue
                val list = ArrayList<Entry>()
                for (i in 0 until arr.length()) {
                    val e = arr.optJSONObject(i) ?: continue
                    list.add(
                        Entry(
                            e.safeString("code"), e.safeString("name"),
                            if (e.isNull("hits")) null else e.optInt("hits"),
                            "data", null
                        )
                    )
                }
                converged[word] = list
                broad[word] = ArrayList(list)
            }
        }

        curated?.let {
            val map = JSONObject(it).optJSONObject("alias")
            val keys = map?.keys() ?: return@let
            while (keys.hasNext()) {
                val word = keys.next()
                val spec = map.optJSONObject(word) ?: continue
                val codes = spec.optJSONArray("codes") ?: continue
                val perWordMax = if (spec.isNull("max_supply")) null else spec.optInt("max_supply")
                val targetC = converged.getOrPut(word) { ArrayList() }
                val targetB = broad.getOrPut(word) { ArrayList(targetC) }
                val seen = targetC.mapTo(HashSet()) { it.code }
                for (i in 0 until codes.length()) {
                    val code = codes.optString(i, "")
                    if (code.isEmpty() || code in seen) continue
                    val entry = Entry(
                        code, GbIndex.nameOf(code), null, "curated", perWordMax
                    )
                    // 补位码（非首位）要做供给收敛
                    if (i > 0 && overSupply(entry)) continue
                    seen.add(code)
                    targetC.add(entry)
                    targetB.add(entry)
                }
            }
        }
        return converged.size
    }

    /** 目标小类家数超过阈值 → 不参与别名扩展。 */
    private fun overSupply(e: Entry): Boolean {
        if (!GbIndex.isLoaded()) return false          // 没计数就别过滤
        val limit = e.maxSupply ?: defaultMaxSupply
        if (limit <= 0) return false
        return GbIndex.supplyOf(e.code) > limit
    }

    fun size(): Int = converged.size

    /**
     * 采购词 → {国标码: 证据等级}，等级越小越强。
     * 1 = 别名表首位小类（语义最贴近）；2 = 其余小类（按行业推断）。
     * 0 档（企业自己写了这个词）由检索内核判定，不在这里。
     */
    fun rankFor(keyword: String, broadMode: Boolean = false): Map<String, Int> {
        val table = if (broadMode) broad else converged
        val kw = keyword.trim()
        if (kw.isEmpty()) return emptyMap()
        val low = kw.lowercase()
        val exact = LinkedHashMap<String, Int>()
        val fuzzy = LinkedHashMap<String, Int>()

        for ((word, entries) in table) {
            val wl = word.lowercase()
            val (target, top) = when {
                wl == low -> exact to 3
                wl in low || low in wl -> fuzzy to 1
                else -> continue
            }
            for (i in 0 until minOf(top, entries.size)) {
                val rank = if (i == 0) 1 else 2
                val code = entries[i].code
                val prev = target[code]
                if (prev == null || rank < prev) target[code] = rank
            }
        }
        return if (exact.isNotEmpty()) exact else fuzzy
    }

    /** 给设置页展示用：这个词命中了哪些小类（带来源与家数）。 */
    fun explain(keyword: String): String {
        val kw = keyword.trim()
        if (kw.isEmpty()) return ""
        val ranks = rankFor(kw)
        if (ranks.isEmpty()) return "「$kw」不在别名表里，只能用字面匹配"
        val parts = ranks.entries.sortedBy { it.value }.map { (code, rank) ->
            "$code ${GbIndex.nameOf(code)}（${if (rank == 1) "首位" else "推断"}，" +
                "${GbIndex.supplyOf(code)} 家）"
        }
        return "「$kw」→ " + parts.joinToString("、")
    }
}
