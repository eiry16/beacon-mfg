package cn.beaconmfg.app.llm

import cn.beaconmfg.app.data.CertTier
import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.RemoteSource
import cn.beaconmfg.app.data.SearchParams
import cn.beaconmfg.app.data.SupplierDetail
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Strings
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
    /** 语言是**每次调用时取**，不是构造时固定——用户在设置里切完立刻生效，不用重启。 */
    private val lang: () -> Lang = { Lang.ZH },
) {

    private fun str(): Strings = Strings(lang())

    fun definitions(): JSONArray {
        val en = lang() == Lang.EN
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
                    if (en) {
                        "Search manufacturing suppliers by sourcing need. The keyword can be a product " +
                            "name, a process name or a sourcing term (e.g. gear, injection molding, " +
                            "conveyor line, anodizing). Results are ordered by evidence strength — " +
                            "always report the evidence tier honestly."
                    } else {
                        "按采购需求检索制造业供应商。关键词可以是产品名、工艺名或采购词" +
                            "（如 齿轮、注塑、输送线、阳极氧化）。返回结果按证据强度排序，务必如实说明证据档位。"
                    },
                    JSONObject()
                        .put(
                            "keyword",
                            str(
                            if (en) {
                                "Sourcing term or product keyword; space-separated words are ANDed. " +
                                    "The directory is Chinese — translate the buyer's request first " +
                                    "(gear -> 齿轮, conveyor line -> 输送线, die casting -> 压铸)."
                            } else {
                                "采购词或产品关键词，多个词用空格分隔表示同时满足"
                            }
                            )
                        )
                        .put(
                            "city",
                            str(
                                if (en) "City name, e.g. Shanghai, Dongguan, Ningbo"
                                else "城市名，如 上海、东莞、宁波。不带「市」也可以"
                            )
                        )
                        .put(
                            "industry_code",
                            str(
                                if (en) "National-standard industry code: 34 division / 343 group / 3434 class"
                                else "国标行业代码，支持 34 大类 / 343 中类 / 3434 小类"
                            )
                        )
                        .put(
                            "cert",
                            str(
                                if (en) "Certification name, e.g. High-tech Enterprise, ISO9001"
                                else "认证名称，如 高新技术企业、ISO9001"
                            )
                        )
                        .put(
                            "min_beacon",
                            str(
                                if (en) {
                                    "Minimum certification beacon: L1 (claimed) / L2 (verified) / " +
                                        "L3 (audited). Leave empty for no filter. Note most suppliers " +
                                        "are L0 (public listing, nothing verified) — filtering L2+ " +
                                        "usually returns very few or zero results."
                                } else {
                                    "认证等级下限：L1 已认领 / L2 已认证 / L3 已验厂。留空为不限。" +
                                        "注意绝大多数企业是 L0（公开名录、未核验），" +
                                        "筛 L2 以上通常很少甚至 0 家"
                                }
                            )
                        )
                        .put(
                            "manufacturer_only",
                            JSONObject().put("type", "boolean")
                                .put(
                                    "description",
                                    if (en) "Only manufacturers (exclude wholesale/trading)"
                                    else "是否只返回生产企业（排除批发贸易）"
                                )
                        )
                        .put(
                            "with_phone_only",
                            JSONObject().put("type", "boolean")
                                .put(
                                    "description",
                                    if (en) "Only suppliers with a contact phone"
                                    else "是否只返回有联系电话的供应商"
                                )
                        )
                        .put(
                            "limit",
                            JSONObject().put("type", "integer")
                                .put(
                                    "description",
                                    if (en) "Max number of results, default 10"
                                    else "返回条数上限，默认 10"
                                )
                        ),
                )
            )
            .put(
                fn(
                    "get_supplier_detail",
                    if (en) {
                        "Fetch a supplier's full profile (address, phone, website, main business). " +
                            "Requires downloading that industry shard; offline it only returns the " +
                            "summary fields already in the index."
                    } else {
                        "取某家供应商的完整档案（地址、电话、官网、主营）。需要联网下载该行业分片；" +
                            "离线时只返回索引层已有的摘要字段。"
                    },
                    JSONObject().put(
                        "id",
                        str(
                            if (en) "Supplier ID, e.g. CN-MFG-0000593"
                            else "供应商 ID，形如 CN-MFG-0000593"
                        )
                    ),
                    listOf("id")
                )
            )
            .put(
                fn(
                    "list_categories",
                    if (en) {
                        "List national-standard industry subclasses that actually have suppliers " +
                            "(with counts). Call this first when the user's term returns nothing, " +
                            "or when you need to decide which industry code to use."
                    } else {
                        "列出库里有货的国标行业小类（带家数）。当用户的需求词检索不到、" +
                            "或需要判断该用哪个行业码时先调它。"
                    },
                    JSONObject().put(
                        "parent",
                        str(
                            if (en) {
                                "Optional. Division C / group 34 / class 343, or a name substring. " +
                                    "Leave empty for the largest generic subclasses"
                            } else {
                                "可选。门类 C / 大类 34 / 中类 343，或中文名子串如「模具」。留空返回最多的通用小类"
                            }
                        )
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
        val s = str()
        val en = s.lang == Lang.EN
        val keyword = a.optString("keyword", "").ifBlank { null }
        val city = a.optString("city", "").ifBlank { null }
        val code = a.optString("industry_code", "").ifBlank { null }
        val cert = a.optString("cert", "").ifBlank { null }
        val limit = a.optInt("limit", 10).coerceIn(1, 30)
        // 等级认不出（比如模型传了 "L2+"/"已认证" 这种非标准值）就按不限处理，
        // 而不是退化成 L0 下限让搜索结果错得不明不白。
        val minBeacon = a.optString("min_beacon", "").trim().uppercase()
            .takeIf { it.matches(Regex("L[123]")) }
        val params = SearchParams(
            keyword = keyword,
            city = city,
            industryCode = code,
            cert = cert,
            minBeacon = minBeacon,
            manufacturerOnly = a.optBoolean("manufacturer_only", false),
            withPhoneOnly = a.optBoolean("with_phone_only", false),
            limit = limit,
        )
        val out = engine.search(params)
        if (out.hits.isEmpty()) {
            if (out.relaxed > 0) {
                return ToolResult(
                    if (en) {
                        "0 suppliers under these conditions. Relaxing the alias convergence would " +
                            "yield ${out.relaxed}, but all of them are industry inferences " +
                            "(not confirmed by the companies) — tell the user plainly that no " +
                            "company is classified under that industry, only inferred ones. " +
                            "Do not present them as matching suppliers."
                    } else {
                        "当前条件下 0 家。放宽别名收敛可得 ${out.relaxed} 家，" +
                            "但全部是按国标行业推断的（企业未确认）——" +
                            "请如实告诉用户「没有一家被归类为该行业，只有行业推断的结果」，不要说成已有对口供应商。"
                    }
                )
            }
            return ToolResult(
                if (en) {
                    "0 suppliers. Try different wording, or relax the region/certification/" +
                        "beacon filters (most entries are L0 = public listing, nothing verified); " +
                        "you can also call list_categories to see which industries exist."
                } else {
                    "0 家。请换个说法或放宽地区/认证/灯牌等级条件" +
                        "（库里绝大多数是 L0 未核验）；也可先调 list_categories 看库里有哪些行业。"
                }
            )
        }
        val head = if (en) {
            "Total ${out.total}, returning the first ${out.hits.size}. " +
                "Breakdown: exact ${out.literal} · top alias class ${out.aliasPrimary} · " +
                "industry inference ${out.aliasSecondary}."
        } else {
            "共 ${out.total} 家，返回前 ${out.hits.size} 家。" +
                "构成：字面命中 ${out.literal} · 别名首位码 ${out.aliasPrimary} · 行业推断 ${out.aliasSecondary}。"
        }
        val lines = out.hits.map { engine.toBrief(it, s) }
        val tail = if (en) {
            "Note: anything marked industry inference is inferred from the industry code and " +
                "not confirmed by the company — never say \"this company makes XX\"; state that " +
                "it is an industry inference."
        } else {
            "注意：标注「行业推断」的是按国标行业推断，企业未确认，" +
                "不要说成「这家做 XX」，应说明是行业推断结果。"
        }
        return ToolResult((listOf(head) + lines + tail).joinToString("\n"), out.hits)
    }

    private suspend fun detail(a: JSONObject): ToolResult {
        val s = str()
        val en = s.lang == Lang.EN
        val id = a.optString("id", "").trim()
        val fp = engine.byId(id)
            ?: return ToolResult(
                if (en) "No supplier with ID $id. Call search_suppliers first to get an ID."
                else "没有 ID 为 $id 的供应商。请先用 search_suppliers 找到 ID。"
            )
        val d = remote.fetchDetail(dataBase(), fp.gb, id)
        if (d == null) {
            return ToolResult(
                if (en) {
                    "[Offline — index summary only] $id | ${fp.name} | ${fp.city} | " +
                        "${fp.gb} ${fp.gbName} | phone=${if (fp.tel) "yes" else "no"} | " +
                        "beacon=${CertTier.of(fp.cl).label(s)} (${fp.cl}). The full profile " +
                        "(address/phone/website) needs the " +
                        "industry shard to be downloaded."
                } else {
                    "【离线，仅索引层摘要】$id | ${fp.name} | ${fp.city} | " +
                        "${fp.gb} ${fp.gbName} | 电话=${if (fp.tel) "有" else "无"} | " +
                        "认证=${CertTier.of(fp.cl).label(s)}（${fp.cl}）。" +
                        "完整档案（地址/电话/官网）需要联网下载该行业分片。"
                },
                hits = listOf(Hit(fp, Evidence.LITERAL))
            )
        }
        val text = buildString {
            if (en) {
                append(d.company).append(" (").append(d.province).append(" / ").append(d.city).append(")\n")
                append("Main business: ").append(d.keywords.joinToString(" / ").ifEmpty { "not filled" }).append("\n")
                if (d.gb.isNotEmpty()) append("Industry: ").append(d.gb).append(" ").append(d.gbName).append("\n")
                if (d.gbPath.isNotEmpty()) append("Path: ").append(d.gbPath).append("\n")
                append("Address: ").append(d.address.ifEmpty { "not filled" }).append("\n")
                append("Phone: ").append(d.phone.ifEmpty { "not filled" }).append("\n")
                if (d.website.isNotEmpty()) append("Website: ").append(d.website).append("\n")
                append("Certifications: ").append(d.certs.joinToString(", ").ifEmpty { "none" }).append("\n")
                append("Source: public channels, verified at ").append(d.verifiedAt.ifEmpty { "?" })
                if (!d.isManufacturer) append("\n⚠ Wholesale/trading — not a manufacturer")
            } else {
                append(d.company).append("（").append(d.province).append("·").append(d.city).append("）\n")
                append("主营：").append(d.keywords.joinToString(" / ").ifEmpty { "未填写" }).append("\n")
                if (d.gb.isNotEmpty()) append("行业：").append(d.gb).append(" ").append(d.gbName).append("\n")
                if (d.gbPath.isNotEmpty()) append("路径：").append(d.gbPath).append("\n")
                append("地址：").append(d.address.ifEmpty { "未填写" }).append("\n")
                append("电话：").append(d.phone.ifEmpty { "未填写" }).append("\n")
                if (d.website.isNotEmpty()) append("官网：").append(d.website).append("\n")
                append("认证：").append(d.certs.joinToString("、").ifEmpty { "无" }).append("\n")
                beaconLines(d, s, en).let { if (it.isNotEmpty()) append(it) }
                append("数据来源：公开渠道，核实于 ").append(d.verifiedAt.ifEmpty { "?" })
                if (!d.isManufacturer) append("\n⚠ 批发/贸易类，非生产企业")
            }
        }
        return ToolResult(text, listOf(Hit(fp, Evidence.LITERAL)), d)
    }

    /**
     * 回灌给模型的认证信息（灯牌等级 + 含义 + 存证日期）。
     *
     * 为什么要写全：只给一个 "L2"，模型很可能顺着补全成「已验厂并通过质量审核」
     * 这类数据里没有的话。给 label + hint 明确「核验到什么程度」，等于把边界交给它。
     * 注意 beacon 说的是**信息核验程度**，不是这家厂好不好 —— CERTIFICATION_V1 §1 同款红线。
     */
    private fun beaconLines(d: SupplierDetail, s: Strings, en: Boolean): String {
        val tier = CertTier.of(d.beacon)
        val out = StringBuilder()
        if (en) {
            out.append("Beacon: ").append(tier.code).append(" ").append(tier.label(s))
                .append(" — ").append(tier.hint(s)).append("\n")
            d.certification?.let { c ->
                if (c.issuedAt.isNotBlank()) out.append("Issued: ").append(c.issuedAt).append("\n")
                if (c.expiresAt.isNotBlank()) out.append("Valid until: ").append(c.expiresAt).append("\n")
                if (d.certExpired) out.append("⚠ Expired — do not call it verified.\n")
            }
            if (tier.acceptsAutoRfq) out.append("Accepts automated RFQ: yes\n")
            out.append("NB: the beacon is not a quality rating. Do not describe a company as ")
                .append("\"high quality\" / \"audited\" based on it.\n")
        } else {
            out.append("灯牌：").append(tier.code).append(" ").append(tier.label(s))
                .append("——").append(tier.hint(s)).append("\n")
            d.certification?.let { c ->
                if (c.issuedAt.isNotBlank()) out.append("签发：").append(c.issuedAt).append("\n")
                if (c.expiresAt.isNotBlank()) out.append("有效期至：").append(c.expiresAt).append("\n")
                if (d.certExpired) out.append("⚠ 存证已过期，不要再说成「已认证」。\n")
            }
            if (tier.acceptsAutoRfq) out.append("可接自动询价：是\n")
            out.append("注意：灯牌描述的是信息核验程度，不是质量评级，" +
                "不要据此说这家厂「质量好」「验过厂」。\n")
        }
        return out.toString()
    }

    private fun categories(a: JSONObject): ToolResult {
        val s = str()
        val en = s.lang == Lang.EN
        val parent = a.optString("parent", "").ifBlank { null }
        val list = engine.categories(parent, 30)
        if (list.isEmpty()) return ToolResult(
            if (en) "No industry subclass matching \"${parent ?: ""}\"."
            else "没有匹配「${parent ?: ""}」的行业小类。"
        )
        val text = list.joinToString("\n") {
            if (en) "${it.code} ${it.name} (${it.count})" else "${it.code} ${it.name}（${it.count} 家）"
        }
        return ToolResult(
            if (en) "Industry subclasses with suppliers (by count, desc):\n$text"
            else "有货的国标小类（按家数降序）：\n$text"
        )
    }
}
