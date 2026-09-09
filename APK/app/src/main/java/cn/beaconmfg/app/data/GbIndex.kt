package cn.beaconmfg.app.data

import org.json.JSONObject

/**
 * 国标层级索引（data/gb-index.json）。
 *
 * App 端只从它取两样东西：
 *  1. **每个小类有多少家**（supply）—— 别名扩展的 max_supply 收敛要靠它，
 *     不继承这条规则的话，手机用户会替桌面端的噪音买单（一次误命中 3.9MB/3415 家）。
 *  2. **小类中文名与层级路径** —— 给 LLM 回灌结果时展示用。
 */
object GbIndex {

    data class Category(val code: String, val name: String, val count: Int, val path: String)

    private val supply = HashMap<String, Int>()
    private val names = HashMap<String, String>()
    private val paths = HashMap<String, String>()
    private val categories = ArrayList<Category>()
    private var loaded = false
    /** 只存时间戳，文案在 summary() 里按当前语言拼——切语言时不用重新 load 索引。 */
    private var generatedAt = ""

    fun load(json: JSONObject) {
        if (loaded) return
        val tree = json.optJSONObject("tree") ?: return
        val meta = json.optJSONObject("metadata")
        generatedAt = meta?.optString("generated_at").orEmpty()

        val gates = tree.keys()
        while (gates.hasNext()) {
            val gate = gates.next()
            val g = tree.optJSONObject(gate) ?: continue
            val gName = g.optString("name")
            val divs = g.optJSONObject("divisions") ?: continue
            val dk = divs.keys()
            while (dk.hasNext()) {
                val div = dk.next()
                val d = divs.optJSONObject(div) ?: continue
                val dName = d.optString("name")
                val groups = d.optJSONObject("groups") ?: continue
                val mk = groups.keys()
                while (mk.hasNext()) {
                    val grp = mk.next()
                    val m = groups.optJSONObject(grp) ?: continue
                    val mName = m.optString("name")
                    val classes = m.optJSONObject("classes") ?: continue
                    val ck = classes.keys()
                    while (ck.hasNext()) {
                        val code = ck.next()
                        val c = classes.optJSONObject(code) ?: continue
                        val name = c.optString("name")
                        val count = c.optInt("count", 0)
                        supply[code] = count
                        names[code] = name
                        paths[code] = "$gate $gName > $div $dName > $grp $mName"
                        if (count > 0) categories.add(Category(code, name, count, paths[code]!!))
                    }
                }
            }
        }
        categories.sortByDescending { it.count }
        loaded = true
    }

    /** 小类中文名。查不到返回空串——宁可留空也不要编一个。 */
    fun nameOf(code: String): String = names[code] ?: ""

    fun pathOf(code: String): String = paths[code] ?: ""

    /** 该小类下有多少家企业。拿不到返回 0，调用方据此「不过滤」而不是「全过滤」。 */
    fun supplyOf(code: String): Int = supply[code] ?: 0

    fun isLoaded(): Boolean = loaded
    fun summary(s: cn.beaconmfg.app.i18n.Strings): String =
        if (generatedAt.isEmpty()) s.gbIndexNoMeta else s.gbIndexGeneratedAt(generatedAt)

    /**
     * 列出有货的小类。parent 可以是门类（C）、大类（34）、中类（343）或空（全部）。
     * 用于 LLM 的 list_categories 工具：先看清库里有什么，再决定怎么搜。
     */
    fun categories(parent: String?, limit: Int = 30): List<Category> {
        val p = parent?.trim().orEmpty()
        if (p.isEmpty()) return categories.take(limit)
        val byCode = categories.filter { it.code.startsWith(p) }
        if (byCode.isNotEmpty()) return byCode.take(limit)
        // 传的是中文名（如「模具」）时按名称子串匹配
        return categories.filter { it.name.contains(p) }.take(limit)
    }
}

/** 别名解析用的小工具：把 JSONObject 里可能为 JSON null 的字符串安全取出来。 */
internal fun JSONObject.safeString(key: String): String {
    if (!has(key) || isNull(key)) return ""
    return optString(key, "")
}
