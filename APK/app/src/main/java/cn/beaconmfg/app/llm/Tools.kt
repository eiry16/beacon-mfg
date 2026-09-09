package cn.beaconmfg.app.llm

import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.RemoteSource
import cn.beaconmfg.app.data.SearchParams
import cn.beaconmfg.app.data.SupplierDetail
import cn.beaconmfg.app.search.SearchEngine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

data class ToolResult(
    val text: String,
    val hits: List<Hit> = emptyList(),
    val detail: SupplierDetail? = null,
)

/**
 * 给 LLM 的三个工具。
 *
 * 关键设计：**不让 LLM 背数据，只让它调工具**。App 本地执行真实检索，
 * 把精简结果回灌给它组织语言。回灌时必须带证据档位，否则模型会把
 * 「行业推断」说成「这家做齿轮」——那是本项目的数据红线。
 */
class ToolBox(
    private val engine: SearchEngine,
    private val store: DataStore,
    private val remote: RemoteSource,
    private val dataBase: () -> String,
) {

    fun definitions(): JSONArray {
        fun fn(
            name: String,
            desc: String,
            props: JSONObject,
            required: List<String> = emptyList(),
        ): JSONObject = JSONObject()
            .put("type", "function")
            .put(
                "function", JSONObject()
                    .put("name", name)
                    .put("description", desc)
                    .put(
                        "parameters", JSONObject()
                            .put("type", "object")
                            .put("properties", props)
                            .put("required", JSONArray().apply { required.forEach { put(it) } })
                    )
            )

        fun str(desc: String) = JSONObject().put("type", "string").put("description", desc)

        return JSONArray()
            .put(
                fn(
                    "search_suppliers",
                    "按采购需求检索制造业供应商。关键词可以是产品名、工艺名或采购词" +
                        "（如 齿轮、注塑、输送线、阳极氧化）。返回结果按证据强度排序，务必如实说明证据档位。",
                    JSONObject()
                        .put("keyword", str("采购词或产品关键词，多个词用空格分隔表示同时满足"))
                        .put("city", str("城市名，如 上海、东莞、宁波。不带「市」也可以"))
                        .put("industry_code", str("国标行业代码，支持 34 大类 / 343 中类 / 3434 小类"))
                        .put("cert", str("认证名称，如 高新技术企业、ISO9001"))
                        .put(
                            "manufacturer_only",
                            JSONObject().put("type", "boolean")
                                .put("description", "是否只返回生产企业（排除批发贸易）")
                        )
                        .put(
                            "with_phone_only",
                            JSONObject().put("type", "boolean")
                                .put("description", "是否只返回有联系电话的供应商")
                        )
                        .put(
                            "limit",
                            JSONObject().put("type", "integer")
                                .put("description", "返回条数上限，默认 10")
                        ),
                )
            )
            .put(
                fn(
                    "get_supplier_detail",
                    "取某家供应商的完整档案（地址、电话、官网、主营）。需要联网下载该行业分片；" +
                        "离线时只返回索引层已有的摘要字段。",
                    JSONObject().put("id", str("供应商 ID，形如 CN-MFG-0000593")),
                    listOf("id")
                )
            )
            .put(
                fn(
                    "list_categories",
                    "列出库里有货的国标行业小类（带家数）。当用户的需求词检索不到、" +
                        "或需要判断该用哪个行业码时先调它。",
                    JSONObject().put(
                        "parent",
                        str("可选。门类 C / 大类 34 / 中类 343，或中文名子串如「模具」。留空返回最多的通用小类")
                    )
                )
            )
    }

    suspend fun run(name: String, arguments: String): ToolResult = withContext(Dispatchers.IO) {
        val args = try {
            if (arguments.isBlank()) JSONObject() else JSONObject(arguments)
        } catch (e: Exception) {
            JSONObject()
        }
        when (name) {
            "search_suppliers" -> search(args)
            "get_supplier_detail" -> detail(args)
            "list_categories" -> categories(args)
            else -> ToolResult("未知工具：$name")
        }
    }

    private fun search(a: JSONObject): ToolResult {
        val keyword = a.optString("keyword", "").ifBlank { null }
        val city = a.optString("city", "").ifBlank { null }
        val code = a.optString("industry_code", "").ifBlank { null }
        val cert = a.optString("cert", "").ifBlank { null }
        val limit = a.optInt("limit", 10).coerceIn(1, 30)
        val params = SearchParams(
            keyword = keyword,
            city = city,
            industryCode = code,
            cert = cert,
            manufacturerOnly = a.optBoolean("manufacturer_only", false),
            withPhoneOnly = a.optBoolean("with_phone_only", false),
            limit = limit,
        )
        val out = engine.search(params)
        if (out.hits.isEmpty()) {
            if (out.relaxed > 0) {
                return ToolResult(
                    "当前条件下 0 家。放宽别名收敛可得 ${out.relaxed} 家，" +
                        "但全部是按国标行业推断的（企业未确认）——" +
                        "请如实告诉用户「没有一家被归类为该行业，只有行业推断的结果」，不要说成已有对口供应商。"
                )
            }
            return ToolResult(
                "0 家。请换个说法或放宽地区/认证条件；也可先调 list_categories 看库里有哪些行业。"
            )
        }
        val head = "共 ${out.total} 家，返回前 ${out.hits.size} 家。" +
            "构成：字面命中 ${out.literal} · 别名首位码 ${out.aliasPrimary} · 行业推断 ${out.aliasSecondary}。"
        val lines = out.hits.map { engine.toBrief(it) }
        val tail = "注意：标注「行业推断」的是按国标行业推断，企业未确认，" +
            "不要说成「这家做 XX」，应说明是行业推断结果。"
        return ToolResult((listOf(head) + lines + tail).joinToString("\n"), out.hits)
    }

    private suspend fun detail(a: JSONObject): ToolResult {
        val id = a.optString("id", "").trim()
        val fp = engine.byId(id)
            ?: return ToolResult("没有 ID 为 $id 的供应商。请先用 search_suppliers 找到 ID。")
        val d = remote.fetchDetail(dataBase(), fp.gb, id)
        if (d == null) {
            return ToolResult(
                "【离线，仅索引层摘要】$id | ${fp.name} | ${fp.city} | " +
                    "${fp.gb} ${fp.gbName} | 电话=${if (fp.tel) "有" else "无"} | " +
                    "灯牌=${fp.cl}。完整档案（地址/电话/官网）需要联网下载该行业分片。",
                hits = listOf(Hit(fp, Evidence.LITERAL))
            )
        }
        val text = buildString {
            append(d.company).append("（").append(d.province).append("·").append(d.city).append("）\n")
            append("主营：").append(d.keywords.joinToString(" / ").ifEmpty { "未填写" }).append("\n")
            if (d.gb.isNotEmpty()) append("行业：").append(d.gb).append(" ").append(d.gbName).append("\n")
            if (d.gbPath.isNotEmpty()) append("路径：").append(d.gbPath).append("\n")
            append("地址：").append(d.address.ifEmpty { "未填写" }).append("\n")
            append("电话：").append(d.phone.ifEmpty { "未填写" }).append("\n")
            if (d.website.isNotEmpty()) append("官网：").append(d.website).append("\n")
            append("认证：").append(d.certs.joinToString("、").ifEmpty { "无" }).append("\n")
            append("数据来源：公开渠道，核实于 ").append(d.verifiedAt.ifEmpty { "?" })
            if (!d.isManufacturer) append("\n⚠ 批发/贸易类，非生产企业")
        }
        return ToolResult(text, listOf(Hit(fp, Evidence.LITERAL)), d)
    }

    private fun categories(a: JSONObject): ToolResult {
        val parent = a.optString("parent", "").ifBlank { null }
        val list = engine.categories(parent, 30)
        if (list.isEmpty()) return ToolResult("没有匹配「${parent ?: ""}」的行业小类。")
        val text = list.joinToString("\n") { "${it.code} ${it.name}（${it.count} 家）" }
        return ToolResult("有货的国标小类（按家数降序）：\n$text")
    }
}
