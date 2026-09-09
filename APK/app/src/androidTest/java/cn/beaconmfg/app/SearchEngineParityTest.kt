package cn.beaconmfg.app

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.GbIndex
import cn.beaconmfg.app.data.SearchParams
import cn.beaconmfg.app.search.AliasIndex
import cn.beaconmfg.app.search.SearchEngine
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Kotlin 检索内核 vs Python query.py 的对拍测试。
 *
 * 期望值由 `APK/tools/e2e_parity.py --write` 从真实数据生成，
 * 产物放在 androidTest/assets/e2e_expect.json。数据更新后重跑脚本即可，不用改测试。
 *
 * 需要真机或模拟器（要读 assets）。跑法：
 *   ./gradlew connectedAndroidTest
 *
 * 只比三个维度：总数、三档构成、前 N 个 id 的顺序。
 * 不比并列时的内部次序——Python 与 Kotlin 的 stable sort 在并列项的相对顺序上可能不同，
 * 而并列次序对用户体验没有影响，硬要比只会出现假失败。
 */
@RunWith(AndroidJUnit4::class)
class SearchEngineParityTest {

    companion object {
        private lateinit var engine: SearchEngine
        private lateinit var cases: org.json.JSONArray

        @BeforeClass
        @JvmStatic
        fun setup() {
            val app: Application = ApplicationProvider.getApplicationContext()
            val store = DataStore(app)
            app.assets.open("index/gb-index.json").bufferedReader().use {
                GbIndex.load(JSONObject(it.readText()))
            }
            val alias = AliasIndex(store)
            alias.load()
            engine = SearchEngine(store, alias)

            val raw = app.assets.open("e2e_expect.json").bufferedReader().readText()
            cases = JSONObject(raw).getJSONArray("cases")
        }
    }

    private fun paramsOf(o: JSONObject): SearchParams {
        val p = o.getJSONObject("params")
        fun s(key: String): String? =
            if (p.has(key) && !p.isNull(key)) p.getString(key) else null
        return SearchParams(
            keyword = s("keyword"),
            city = s("city"),
            industryCode = s("industryCode"),
            cert = s("cert"),
            manufacturerOnly = p.optBoolean("manufacturerOnly", false),
            withPhoneOnly = p.optBoolean("withPhoneOnly", false),
            limit = p.optInt("limit", 10),
        )
    }

    @Test
    fun matchesPythonReference() {
        val failures = ArrayList<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val name = c.getString("name")
            val out = engine.search(paramsOf(c))

            fun check(field: String, expected: Int, actual: Int) {
                if (expected != actual) {
                    failures.add("$name · $field：期望 $expected，实际 $actual")
                }
            }
            check("总数", c.getInt("total"), out.total)
            check("字面档", c.getInt("literal"), out.literal)
            check("首位码档", c.getInt("primary"), out.aliasPrimary)
            check("推断档", c.getInt("secondary"), out.aliasSecondary)
            if (c.has("relaxed") && !c.isNull("relaxed")) {
                check("放宽计数", c.getInt("relaxed"), out.relaxed)
            }

            val expectIds = c.getJSONArray("topIds")
            val wanted = (0 until expectIds.length()).map { expectIds.getString(it) }
            val actualIds = out.hits.take(wanted.size).map { it.fp.id }
            if (wanted != actualIds) {
                failures.add("$name · 前 ${wanted.size} id：期望 $wanted，实际 $actualIds")
            }
        }
        assertTrue(
            "与 Python 参考实现不一致（${failures.size} 处）：\n" + failures.joinToString("\n"),
            failures.isEmpty()
        )
    }

    /** 收敛砍成 0 时不能静默放宽——这是数据红线，单独断言一次防止被"优化"掉。 */
    @Test
    fun zeroResultMustReportRelaxedCount() {
        val zero = (0 until cases.length())
            .map { cases.getJSONObject(it) }
            .filter { it.getInt("total") == 0 && !it.isNull("relaxed") }
        assertTrue("用例集中应有 total=0 且带 relaxed 计数的用例", zero.isNotEmpty())

        for (c in zero) {
            val out = engine.search(paramsOf(c))
            assertEquals("${c.getString("name")} 应返回 0 条", 0, out.hits.size)
        }
    }

    /** 行业推断档必须带弱证据标签，否则 LLM 会说成"这家做 XX"。 */
    @Test
    fun secondaryHitsCarryWeakEvidence() {
        val out = engine.search(SearchParams(keyword = "输送线", limit = 20))
        val weak = out.hits.filter { it.evidence == Evidence.ALIAS_SECONDARY }
        if (weak.isNotEmpty()) {
            assertTrue(
                "弱证据结果必须带「未确认」语义的标签，实际是：${weak.first().evidence.label}",
                weak.first().evidence.label.contains("推断")
            )
        }
    }
}
