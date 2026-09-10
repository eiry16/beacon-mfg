package cn.beaconmfg.app.llm

import cn.beaconmfg.app.data.CertTier
import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.Fingerprint
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.PlatformApi
import cn.beaconmfg.app.data.PlatformApiException
import cn.beaconmfg.app.data.Uscc
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Strings
import org.json.JSONArray
import org.json.JSONObject

/**
 * 供应商侧的会话态：认领到哪一步了。
 *
 * 为什么单独存而不是塞进 DataStore/磁盘：认领凭证是**短时凭据**，
 * 进程退出就该作废（claim.py 的 Phase 0 stub 里 session 也在内存里）。
 * 落盘会让「认领到一半的凭证」在下次开 App 时仍然有效——那不是我们要的语义。
 */
class VendorSession {
    /** `/claim/verify-start` 返回的 session_id，提交验证码时要用。 */
    var claimSessionId: String? = null

    /** `/claim/verify-code` 返回的 claim_token。同时是采集接口的 Bearer。 */
    var claimToken: String? = null

    /** 正在认领的企业 ID。 */
    var supplierId: String? = null

    /**
     * 对方提供的统一社会信用代码（18 位），校验位已通过。
     *
     * 现在它**不能用来检索名录**——名录 2.3 万条里还没有 USCC 字段。
     * 记下来的意义是：它是唯一且权威的法律主体锚点，等名录补上 USCC 索引后
     * 就能做 100% 精确匹配，彻底消掉"名字报不准"这一整类问题。
     * 在此之前它随认领一起提交，作为主体凭证。
     */
    var uscc: String? = null

    /**
     * 认证申请号（`/v1/certify/apply` 返回，形如 `CERT-20260910-0317`）。
     * 只有**本次注册进来的**企业才有——认领名录里已收录的企业时它是空的。
     */
    var appId: String? = null

    /**
     * 这家企业是本次对话里**新注册**的（名录原本没有），而不是认领来的。
     *
     * 为什么要区分：两者之后要交代的话不一样 —— 认领是「接上已有档案」，
     * 注册是「新建了档案，名录原来没有这家」。说混了，对方会以为平台早就收录过自己。
     */
    var registeredHere: Boolean = false

    /**
     * 平台**当前正等着回答的那一题**（null = 没在采集，或已经问完了）。
     *
     * 为什么要记这个：模型（tool_choice=auto）经常**不调工具直接作答**——
     * 实测它就干过「好，80 人记下了」然后自己编下一题，而平台那边一步没动。
     * 用户以为记下了，其实什么都没提交。所以 App 必须自己记住"现在该问哪题"，
     * 每轮把它塞回提示词里，并在这一轮真没提交时如实说出来。
     */
    var pending: PendingQuestion? = null

    val claimed: Boolean get() = !claimToken.isNullOrBlank()

    /** 验证码已发出、还没验过。用来告诉模型"现在该等对方报码"。 */
    val claimPending: Boolean get() = !claimed && !claimSessionId.isNullOrBlank()

    fun reset() {
        claimSessionId = null
        claimToken = null
        supplierId = null
        uscc = null
        appId = null
        registeredHere = false
        pending = null
    }
}

/** 平台当前的问题。存结构化字段而不是拼好的句子——界面语言可能在会话中途被切。 */
data class PendingQuestion(
    val index: Int,
    val total: Int,
    val path: String,
    val question: String,
    val hint: String,
    val required: Boolean,
)

/**
 * 供应商侧工具集：**认领 + 对话式采集**。
 *
 * 与 [BuyerToolBox] 的关键区别是**这里有写操作**。所以：
 *  - 只在身份=供应商时装载这一个实例，买家的 ToolBox 同时被丢掉；
 *  - 写到哪一步、问到哪一题，全部由**服务端状态机**说了算（嘴脑分离，
 *    见 docs/vendor-onboarding-design.html §4.1）。LLM 只负责把题面说成口语、
 *    把口述抽成一句话。**不让 LLM 自己管 30 题**：会漏题、会重复、上下文爆炸，
 *    而且把归一化交给概率等于违反「不编造数字」的红线。
 *  - 失败必须**显式失败**。接口没配 / 不通，回的是标红的错误行，不是一句"好的"。
 *
 * 工具与端点的对应关系以 `server/routers` 下的实际代码为准：
 *   claim.py  前缀 /claim       collect.py  前缀 /v1/collect
 * （设计文档里写的 `/v1/claim/verify-start` 是错的，照那个写会直接 404。）
 *
 * 未实现：证件上传（`upload_material`）。它要 multipart + 私有对象存储的签名 URL，
 * 而后端这条通道还没通（R2 已建但 Pages 侧的绑定未配）。宁可不给工具，
 * 也不给一个点了没反应的按钮。
 */
class VendorToolBox(
    private val store: DataStore,
    private val api: PlatformApi,
    private val session: VendorSession,
    /** 语言惰性取值，切语言立刻生效。 */
    private val lang: () -> Lang = { Lang.ZH },
) : ToolSet {

    private fun str(): Strings = Strings(lang())
    private fun en(): Boolean = lang() == Lang.EN
    private fun t(zh: String, e: String) = if (en()) e else zh

    // ── 工具定义 ───────────────────────────────────────────────────────────

    override fun definitions(): JSONArray {
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

        fun s(desc: String) = JSONObject().put("type", "string").put("description", desc)

        return JSONArray()
            .put(
                fn(
                    "find_my_company",
                    t(
                        "在名录里查找供应商**自己的**企业，拿到它的供应商 ID。认领前必须先拿到 ID。" +
                            "这一步完全在本地完成，不需要联网。",
                        "Look up the supplier's OWN company in the directory to get its supplier ID. " +
                            "The ID is required before claiming. Runs fully on-device."
                    ),
                    JSONObject().put(
                        "keyword",
                        s(
                            t(
                                "公司名或其片段，如「耐特斯」「上海耐特斯传输设备」。" +
                                    "也可以是供应商 ID（形如 CN-MFG-0020317），或 18 位统一社会信用代码" +
                                    "（扫码或手输均可）。给号码时会先校验格式，但**名录暂不支持按号码定位**，" +
                                    "拿到号码后仍须再要一次公司全称。",
                                "Company name or a fragment of it. A supplier ID (e.g. CN-MFG-0020317) or " +
                                    "an 18-digit USCC (scanned or typed) also works. Numbers are validated " +
                                    "for format, but the directory **cannot be searched by USCC yet** — " +
                                    "you still need the full legal name afterwards."
                            )
                        )
                    ),
                    listOf("keyword")
                )
            )
            .put(
                fn(
                    "register_company",
                    t(
                        "把**名录里还没有的**企业登记进来，平台会分配一个新的供应商 ID。" +
                            "必须先调 find_my_company，**确实搜不到才用它**——名录里已经有的企业走认领，不走注册。" +
                            "需要三样，缺一不可：营业执照上的公司全称、经营地址、主品类。" +
                            "成功之后照常发验证码核验手机号，再开始采集资料。",
                        "Register a company the directory does not have yet; the platform assigns a new " +
                            "supplier ID. **Call find_my_company first — only use this when it truly finds " +
                            "nothing.** Companies already in the directory go through claiming instead. " +
                            "Needs three things, none optional: the full legal name on the licence, the " +
                            "business address, and the main category. Afterwards verify their phone by SMS."
                    ),
                    JSONObject()
                        .put(
                            "company",
                            s(t("营业执照上的企业全称，一个字都不能省、不能改", "Full legal name exactly as on the licence"))
                        )
                        .put(
                            "address",
                            s(t("对方申报的经营地址（至少 4 个字，写到门牌/园区）", "The business address they state (at least 4 characters)"))
                        )
                        .put(
                            "category",
                            s(t("主品类，用中文，如「精密钣金」「注塑」「齿轮加工」。太泛的「做五金」不行", "Main category, in Chinese, e.g. 精密钣金 / 注塑 / 齿轮加工"))
                        )
                        .put("contact_name", s(t("联系人姓名，可留空", "Contact name; optional")))
                        .put("contact_phone", s(t("对方提供的 11 位手机号，可留空", "Their 11-digit mobile number; optional"))),
                    listOf("company", "address", "category")
                )
            )
            .put(
                fn(
                    "claim_start",
                    t(
                        "发起认领：确认这确实是本人在操作，并向登记的手机号发验证码。" +
                            "需要企业已在名录中（先调 find_my_company 拿 ID）。" +
                            "**必须先向对方要手机号**，不能替他把号码编出来。",
                        "Start a claim: confirm the person is operating on their own behalf and send " +
                            "an SMS code to their registered phone. The company must already be in " +
                            "the directory (call find_my_company first). **Ask them for the phone " +
                            "number** — never guess one."
                    ),
                    JSONObject()
                        // supplier_id 不必填：App 记着刚查到的那家，模型漏了就由 App 补。
                        // （模型抄错 ID 会把资料写到别人家里去，这条优先级搞反过一次。）
                        .put("supplier_id", s(t("供应商 ID，形如 CN-MFG-0020317；不确定可以不传", "Supplier ID, e.g. CN-MFG-0020317; omit if unsure")))
                        .put("phone", s(t("对方提供的 11 位手机号", "The 11-digit phone number they gave you"))),
                    listOf("phone")
                )
            )
            .put(
                fn(
                    "claim_verify",
                    t(
                        "提交对方收到的 6 位验证码。通过后就能开始录资料了。" +
                            "（会话凭证由 App 自己保管，你不用管，也不要问我。）",
                        "Submit the 6-digit code they received. On success you can start on the profile. " +
                            "(The session handle is kept by the app — you neither need it nor should ask.)"
                    ),
                    JSONObject().put("code", s(t("6 位数字验证码", "The 6-digit code"))),
                    listOf("code")
                )
            )
            .put(
                fn(
                    "collect_begin",
                    t(
                        "开始/继续资料采集，并带上系统自动预填。返回第一个问题和整体进度。" +
                            "**预填不等于对方确认过**，预填的字段要逐条念给他核对。",
                        "Start or resume profile collection and run the platform auto-fill. Returns the " +
                            "first question and overall progress. **Auto-filled is not confirmed** — " +
                            "read each pre-filled field back for confirmation."
                    ),
                    // 无参数：认领了哪家由 App 记着，模型不需要（也不该）参与。
                    JSONObject()
                )
            )
            .put(
                fn(
                    "collect_answer",
                    t(
                        "提交对方对当前这一题的口述回答。**原话照录**，不要替他换算成数字。" +
                            "他说答不上来/跳过，就把 text 留空，系统会把该字段留空而不是填默认值。" +
                            "返回下一题。",
                        "Submit the person's spoken answer to the current question. Record it **verbatim** — " +
                            "do not convert units yourself. If they can't answer, leave `text` empty: the " +
                            "field stays blank rather than getting a default. Returns the next question."
                    ),
                    JSONObject()
                        .put("text", s(t("他的原话；留空表示跳过这一题（字段留空）", "Their words; leave empty to skip (field stays blank)")))
                )
            )
            .put(
                fn(
                    "collect_progress",
                    t(
                        "查填写进度：完成度、还缺哪些必填、有没有互相矛盾的答案、会话存在哪。",
                        "Check progress: completeness, which required fields are still missing, whether any " +
                            "answers conflict, and where the session is stored."
                    ),
                    JSONObject()
                )
            )
            .put(
                fn(
                    "collect_confirm",
                    t(
                        "定稿：生成能力卡。**必填未齐或存在 error 级冲突时会被拒绝**，" +
                            "拒绝时如实把缺的东西列给对方，不要换个说法说成已完成。",
                        "Finalise and generate the capability card. **Rejected if required fields are missing " +
                            "or conflicts remain** — when rejected, list exactly what is missing; do not " +
                            "rephrase it as done."
                    ),
                    JSONObject()
                        .put(
                            "overwrite_existing",
                            JSONObject().put("type", "boolean")
                                .put(
                                    "description",
                                    t(
                                        "该企业已有认主/人工卡时，是否覆盖。默认 false；不确定就不要传 true。",
                                        "Whether to overwrite an existing claimed/manual card. Default false."
                                    )
                                )
                        )
                )
            )
    }

    // ── 执行 ───────────────────────────────────────────────────────────────

    override suspend fun run(name: String, arguments: String): ToolResult {
        val args = try {
            if (arguments.isBlank()) JSONObject() else JSONObject(arguments)
        } catch (e: Exception) {
            JSONObject()
        }
        return try {
            when (name) {
                "find_my_company" -> findMyCompany(args)
                "register_company" -> registerCompany(args)
                "claim_start" -> claimStart(args)
                "claim_verify" -> claimVerify(args)
                "collect_begin" -> collectBegin(args)
                "collect_answer" -> collectAnswer(args)
                "collect_progress" -> collectProgress(args)
                "collect_confirm" -> collectConfirm(args)
                else -> ToolResult(t("未知工具：$name", "Unknown tool: $name"), failed = true)
            }
        } catch (e: PlatformApiException) {
            // 把服务端给的 code 一起带出来：「必填未齐」和「网络不通」对供应商是两件事
            failed(apiErrorText(e))
        }
    }

    private fun apiErrorText(e: PlatformApiException): String = when (e.code) {
        "NOT_CONFIGURED" -> str().vendorApiMissing
        else -> str().vendorUnreachable + "${e.code}：${e.message}"
    }

    private fun failed(text: String) = ToolResult(text, failed = true)

    /** 本地名录匹配。**不联网**，所以接口地址没配时它照样能用。 */
    private fun findMyCompany(a: JSONObject): ToolResult {
        val kw = a.optString("keyword", "").trim()
        val s = str()

        // 直接给供应商 ID 也算命中。为什么单独认这一条：运营/客服在后台看到的就是 ID，
        // 让他们再去找一次公司名纯属多余；顺带也绕开了"手机输入法打不出中文"的场景。
        if (kw.matches(Regex("(?i)^CN-MFG-\\d+$"))) {
            val fp = store.fingerprints().firstOrNull { it.id.equals(kw, ignoreCase = true) }
                ?: return ToolResult(
                    t(
                        "名录里没有 ID 为 $kw 的企业。检查一下号码，或者给公司全称。",
                        "No company with ID $kw in the directory. Check the ID, or give the full name."
                    )
                )
            session.supplierId = fp.id
            return ToolResult(
                "${fp.id} | ${fp.name} | ${fp.city.ifEmpty { s.cityUnknown }}" +
                    " | ${s.briefBeacon}=${CertTier.of(fp.cl).label(s)}",
                hits = listOf(Hit(fp, Evidence.LITERAL)),
                echo = s.toolVendorMatch(1),
            )
        }

        // 统一社会信用代码（18 位）：可能是扫码解出来的，也可能是一串手输的号码。
        //
        // 这里必须如实交代一个反直觉的事实：**名录里现在还没有 USCC 字段**，
        // 所以就算号码是对的，也定位不到企业。但号码本身有独立价值——
        // 它是唯一且权威的法律主体锚点，先记下来随认领提交；等名录补上 USCC 索引
        // 就能做精确匹配，消掉"名字报不准"这一整类问题。
        // 绝不假装能搜：搜不到就说搜不到，并把原因讲清楚。
        val uscc = Uscc.extract(kw)
        if (uscc != null) {
            val (ok, why) = Uscc.check(uscc)
            if (!ok) {
                return failed(
                    t(
                        "这串统一社会信用代码不对：$why。请对照营业执照重新输入。",
                        "This USCC is invalid: $why. Please re-enter it from the business licence."
                    )
                )
            }
            session.uscc = uscc
            val region = Uscc.regionOf(uscc)
            val sb = StringBuilder()
            sb.append(t("统一社会信用代码 $uscc 校验通过。", "USCC $uscc passed the checksum.")).append('\n')
            if (region != null) {
                sb.append(
                    t(
                        "登记机关在「$region」。请对方确认对得上——拿错执照就会认领到别家去。",
                        "Registration authority is in \"$region\". Ask them to confirm it matches — " +
                            "the wrong licence means claiming someone else's company."
                    )
                ).append('\n')
            }
            sb.append(
                t(
                    "注意：名录目前还没有统一社会信用代码索引，**光凭这个号码定位不到企业**。"
                        + "请再给一次营业执照上的公司全称，我用名字找；号码已经记下，会随认领一起提交。",
                    "Note: the directory has no USCC index yet, so **this number alone cannot locate the "
                        + "company**. Please also give the full legal name from the licence — the number "
                        + "is recorded and will be submitted with the claim."
                )
            )
            return ToolResult(
                sb.toString().trimEnd(),
                echo = t("已记录统一社会信用代码", "USCC recorded")
            )
        }

        if (kw.length < 2) {
            return failed(t("公司名太短，再多给几个字。", "The company name is too short — give a few more characters."))
        }

        // 三层匹配，从严到宽。实测踩到的真实情况：名录里是「耐特斯传输设备（上海）有限公司」，
        // 而老板报的是「上海耐特斯传输设备有限公司」——**括号位置和词序一变，子串匹配就废了**。
        // 而"搜不到自己的企业"是这条链路里最不能出的错（主人反复强调的那句：
        // 不能让名录搜不到），所以一旦严格匹配失败，必须继续往下退，并且**给出候选让他自己认**。
        val all = store.fingerprints()
        val nk = normName(kw)
        val strict = all.filter { fp ->
            val nn = normName(fp.name)
            (nn.contains(nk) || nk.contains(nn)) && minOf(nn.length, nk.length) >= 4
        }
        if (strict.isNotEmpty()) return matchResult(strict, confident = true)

        // 第二层：按名称 2-gram 覆盖率。对"词序不同/多一个括号/少一个『市』"这类差异很稳。
        val kwGrams = bigrams(nk)
        val ranked = all.map { fp ->
            fp to overlap(bigrams(normName(fp.name)), kwGrams)
        }.sortedByDescending { it.second }
        val similar = ranked.filter { it.second >= 0.6 }.map { it.first }.take(10)
        if (similar.isNotEmpty()) return matchResult(similar, confident = false)

        // 全都不中时也**不能只说"没有"**：给三条最像的让人眼过一遍，
        // 否则老板会以为"我们厂在炫招灯塔上不存在"。
        val nearest = ranked.filter { it.second > 0.3 }.map { it.first }.take(3)
        val sb = StringBuilder()
        sb.append(
            t(
                "名录里没有匹配「$kw」的企业。",
                "No company matching \"$kw\" in the directory."
            )
        ).append('\n')
        if (nearest.isNotEmpty()) {
            sb.append(
                t(
                    "名称最接近的几条是（**都不是确认命中**，仅供核对）：",
                    "Closest names (none of them is a confirmed match — for cross-checking only):"
                )
            ).append('\n')
            nearest.forEach { sb.append(brief(it)).append('\n') }
        }
        sb.append(
            t(
                "让对方核对营业执照上的全称（尤其是括号位置、有没有省/市前缀、" +
                    "「有限公司」还是「股份有限公司」），或者直接给 18 位统一社会信用代码。",
                "Ask them to check the full legal name on the licence (bracket position, province/city " +
                    "prefix, 有限公司 vs 股份有限公司), or provide the 18-digit USCC."
            )
        )
        return ToolResult(sb.toString().trimEnd(), echo = t("没找到，已给出最相近的候选", "Not found — closest candidates listed"))
    }

    /**
     * 命中结果的统一回灌格式。
     *
     * `confident=false`（相似匹配）时**不挂 hits**：挂了卡片，界面上看起来就跟确认命中一样，
     * 而"名字像"离"这是我家"差着一次人工确认。宁可不给卡片，让模型逐条念给对方听。
     */
    private fun matchResult(hits: List<Fingerprint>, confident: Boolean): ToolResult {
        val s = str()
        val top = hits.take(10)
        // 唯一确认命中 → 记下 ID。后面认领时模型就算没带 supplier_id 也能接上
        // （这是我们自己刚查到的事实，替它补上不等于编造）。
        if (confident && top.size == 1) session.supplierId = top[0].id
        val head = if (confident) {
            t("名录里找到 ${top.size} 家匹配：", "Found ${top.size} matching companies:")
        } else {
            t(
                "名录里没有完全一致的名字，但有 ${top.size} 家按名称相似度匹配上（**需对方确认是不是同一家**）：",
                "No exact name match, but ${top.size} names are similar (**ask them to confirm it's theirs**):"
            )
        }
        val tail = if (confident) {
            t(
                "注意：这是名录里的原始记录，**对方还没认领**。" +
                    "确认是本人企业后，向他要手机号，再发起认领。不要因为名字像就认定是同一家。",
                "Note: these are raw directory records — **none of them is claimed yet**. Once the person " +
                    "confirms which one is theirs, ask for their phone number and start the claim. Do not " +
                    "assume a similar name means the same company."
            )
        } else {
            t(
                "名字只是「像」，**不够发起认领**。先把这几家的公司名逐条念给对方，让他指认；" +
                    "他确认了哪一家，再用那个供应商 ID 往下走。",
                "A similar name is **not enough to start a claim**. Read these names back to the person and " +
                    "let them point at theirs; then continue with that supplier ID."
            )
        }
        return ToolResult(
            text = (listOf(head) + top.map { brief(it) } + tail).joinToString("\n"),
            hits = if (confident) top.map { Hit(it, Evidence.LITERAL) } else emptyList(),
            echo = if (confident) s.toolVendorMatch(top.size) else s.toolVendorMatchSimilar(top.size),
        )
    }

    private fun brief(fp: Fingerprint): String {
        val s = str()
        return "${fp.id} | ${fp.name} | ${fp.city.ifEmpty { s.cityUnknown }}" +
            " | ${s.briefBeacon}=${CertTier.of(fp.cl).label(s)}"
    }

    /** 去掉括号、空格、连接符等噪声，只留字母数字与汉字。 */
    private fun normName(x: String): String = x.filter { it.isLetterOrDigit() }

    private fun bigrams(s: String): Set<String> =
        if (s.length < 2) setOf(s)
        else (0..s.length - 2).mapTo(HashSet()) { s.substring(it, it + 2) }

    private fun overlap(a: Set<String>, b: Set<String>): Double =
        if (b.isEmpty()) 0.0 else a.count { it in b }.toDouble() / b.size

    private fun requireClaim(): ToolResult? = if (session.claimed) null else failed(str().vendorNotClaimed)

    /**
     * 该对哪家企业动手。
     *
     * **优先用 App 记着的那家**，模型传进来的只作兜底：把资料写到错的企业上是这条链路上
     * 最严重的错误（污染数据 + 冒名），而模型手上的 ID 是从对话文本里抄的，抄错一点都不稀奇。
     * 我们自己认领过哪家是确定的事实，就该以它为准。
     */
    private fun targetId(a: JSONObject): String =
        session.supplierId.orEmpty().ifBlank { a.optString("supplier_id", "").trim() }

    /**
     * 登记名录里还没有的企业。
     *
     * 与认领的**根本区别**：认领是「名录里已经收录了这家，你来认领」，
     * 注册是「名录里本来没有，现在给它建一条」。服务端 `POST /v1/certify/apply`
     * 同时接住了两者 —— 它内部先查名录：命中就复用既有 ID 并回 `matched_existing=true`，
     * 那说明**该走认领而不是注册**，我们会如实让模型退回去走认领，
     * 而不是把"名录里本来就有"说成"帮你注册好了"。
     */
    private suspend fun registerCompany(a: JSONObject): ToolResult {
        val company = a.optString("company", "").trim()
        val address = a.optString("address", "").trim()
        val category = a.optString("category", "").trim()

        // 三个门禁在本地先拦一道：服务端同样会拒（company≥2 / address≥4 / 未命中时 category 必填），
        // 但白跑一趟网络再报错，对方要干等十几秒，不如当场说清楚缺什么。
        if (company.length < 2) {
            return failed(
                t(
                    "公司全称至少要 2 个字。要营业执照上的完整名称。",
                    "The legal name needs at least 2 characters — use the full name on the licence."
                )
            )
        }
        if (address.length < 4) {
            return failed(
                t(
                    "经营地址太短。要写到门牌或园区名（至少 4 个字）。",
                    "The business address is too short — include the street or park name (at least 4 characters)."
                )
            )
        }
        if (category.isEmpty()) {
            return failed(
                t(
                    "还缺主品类。注册得先知道这家厂做什么才能归到正确的行业下面——" +
                        "「做五金」这种太泛，要具体到「精密钣金」「注塑」这一层。",
                    "The main category is missing. Registration needs to know what they make so the company " +
                        "lands in the right industry — \"metalwork\" is too broad; be specific."
                )
            )
        }

        // 手机号洗不出 11 位就不传（不猜）。服务端 contact_phone 本来就是可选字段。
        val phone = a.optString("contact_phone", "").trim().takeIf { it.isNotBlank() }?.let { cnPhone(it) }
        val name = a.optString("contact_name", "").trim().ifBlank { null }

        val r = api.certifyApply(company, address, category, name, phone)
        val sid = r.optString("supplier_id").takeIf { it.isNotBlank() }
        val matched = r.optBoolean("matched_existing", false)

        // ── 名录里本来就有：这不是注册，退回去走认领 ──
        if (matched) {
            if (sid != null) session.supplierId = sid
            return ToolResult(
                t(
                    "这家企业在名录里**原本就有**（平台复用了已有档案" +
                        (sid?.let { "：$it" } ?: "") + "），所以这不是新注册。" +
                        "请按认领流程走：向对方要登记的手机号，发验证码核验后再采集资料。",
                    "This company **already exists** in the directory (the platform reused its record" +
                        (sid?.let { ": $it" } ?: "") + "), so this is not a new registration. Go through " +
                        "claiming instead: ask for their registered phone, verify the SMS code, then collect."
                ),
                echo = t("名录里已有这家，转认领", "Already in the directory — switch to claiming"),
            )
        }

        session.supplierId = sid
        session.appId = r.optString("app_id").takeIf { it.isNotBlank() }
        session.registeredHere = true

        val sb = StringBuilder()
        sb.append(
            t(
                "已登记：这家企业名录里原来没有，平台新建了档案。",
                "Registered: the directory did not have this company, so a new record was created."
            )
        ).append('\n')
        if (sid != null) {
            sb.append(t("供应商 ID：$sid", "Supplier ID: $sid")).append('\n')
        }
        if (session.appId != null) {
            sb.append(t("认证申请号：${session.appId}", "Application ID: ${session.appId}")).append('\n')
        }

        // ── 地址比对差异：如实带出来，但**不替平台定性** ──
        // compare_address 在"没有可比对的公开记录"时也会给一条 level=info 的说明，
        // 那说的是「无数据可比」，不是「地址有问题」。混为一谈会冤枉对方。
        val disc = r.optJSONArray("discrepancies")
        if (disc != null && disc.length() > 0) {
            val items = (0 until disc.length()).mapNotNull { disc.optJSONObject(it) }
            val comparable = items.filter { !it.optString("message").contains("未提供") }
            val noData = items.size - comparable.size

            if (comparable.isNotEmpty()) {
                sb.append(
                    t(
                        "平台比对了申报地址与公开记录，有 ${comparable.size} 处不一致（**只提示，不代表申报有假**）：",
                        "The platform compared the stated address with public records and found " +
                            "${comparable.size} difference(s) (**informational — not a finding of falsehood**):"
                    )
                ).append('\n')
                comparable.forEach { d ->
                    sb.append("  - [").append(d.optString("level")).append("] ")
                        .append(d.optString("message")).append('\n')
                }
                sb.append(
                    t(
                        "要把这几条念给对方听，让他自己确认哪边是对的；平台不判定谁真谁假。",
                        "Read these back to the person and let them confirm which side is right; the " +
                            "platform does not decide who is correct."
                    )
                ).append('\n')
            }
            if (noData > 0) {
                sb.append(
                    t(
                        "另有 $noData 条是「平台手上没有可交叉比对的公开地址记录」——" +
                            "这说明**暂时核不了**，不等于地址有问题。不要把它说成地址不一致。",
                        "$noData item(s) say the platform has no public address record to cross-check " +
                            "against — that means **it cannot be checked yet**, not that the address is " +
                            "wrong. Do not describe it as a mismatch."
                    )
                ).append('\n')
            }
        }

        sb.append(
            t(
                "下一步照常核验身份：向对方要他名下的手机号，发验证码。核验通过后再开始采集资料。" +
                    "**登记不等于已核验**——刚建的那条档案，还没有任何一项被证实过。",
                "Next, verify identity as usual: ask for their mobile number and send a code. Only after " +
                    "that succeeds should collection begin. **Registering is not verification** — nothing " +
                    "in the new record has been proven yet."
            )
        )
        return ToolResult(
            sb.toString().trimEnd(),
            echo = t("已登记新企业 $company", "Registered $company"),
        )
    }

    private suspend fun claimStart(a: JSONObject): ToolResult {
        // 模型可能不带 supplier_id（它觉得"刚查过"就够了）。查到过就替它补上——
        // 这是我们自己知道的事实，不是编造。
        val id = targetId(a)
        if (id.isEmpty()) {
            return failed(t("不知道是哪家企业。先查一下公司名。", "Which company? Look up the name first."))
        }
        val phone = cnPhone(a.optString("phone"))
        if (phone == null) {
            return failed(
                t(
                    "手机号要 11 位（拿到的是「${a.optString("phone")}」）。请向对方要登记的手机号。",
                    "The phone number must be 11 digits (got \"${a.optString("phone")}\"). Ask them for it."
                )
            )
        }
        val r = api.claimStart(id)
        val sid = r.optString("session_id")
        session.claimSessionId = sid
        session.supplierId = id
        session.claimToken = null
        return ToolResult(
            t(
                "已向 $phone 发出验证码。让对方向你读一下这 6 位数字。",
                "A verification code has been sent to $phone. Ask them to read you the 6 digits."
            )
        )
    }

    /**
     * 把平台给的 `next_question` 记成待答状态（null = 问完了）。
     * 三处出口（begin / answer / progress）都走这一条，避免只有一条路径更新、
     * 另外两条留着过期的题——那比不记更糟。
     */
    private fun rememberPending(q: JSONObject?) {
        session.pending = q?.let {
            PendingQuestion(
                index = it.optInt("index"),
                total = it.optInt("total"),
                path = it.optString("path"),
                question = it.optString("question"),
                hint = it.optString("hint"),
                required = it.optBoolean("required", false),
            )
        }
    }

    /**
     * 手机号标准化。**只做确定性的清洗，不做猜测**：
     * 去掉非数字字符、剥掉 +86 / 86 国码前缀。洗不出 11 位就返回 null（如实报错），
     * 不会"补一个看着像的号"——号码是要发验证码的，猜错等于发给别人。
     */
    private fun cnPhone(raw: String): String? {
        var d = raw.filter { it.isDigit() }
        if (d.length > 11 && d.startsWith("86")) d = d.substring(2)
        return d.takeIf { it.length == 11 }
    }

    private suspend fun claimVerify(a: JSONObject): ToolResult {
        // **只认 App 自己记的 session**，不接受模型传进来的值。
        // 曾经把 session_id 作为参数暴露给模型，结果它拿不到值就自己猜了一个
        // （把 supplier_id 当 session_id 传了回来），服务端回 INVALID_SESSION。
        // 这类"内部句柄"根本不该出现在模型的工具参数里——状态归状态机管。
        val sid = session.claimSessionId.orEmpty()
        if (sid.isEmpty()) {
            return failed(
                t(
                    "还没有发起认领。先要对方的企业名和手机号，发了验证码才能验。",
                    "No claim in progress yet. Get the company name and phone number first."
                )
            )
        }
        val code = a.optString("code", "").trim().filter { it.isDigit() }
        if (code.length != 6) return failed(t("验证码是 6 位数字。", "The code is 6 digits."))
        val r = api.claimVerifyCode(sid, code)
        val token = r.optString("claim_token").takeIf { it.isNotBlank() }
        if (!r.optBoolean("success", false) || token == null) {
            return failed(t("验证没通过，让对方重新确认验证码。", "Verification failed — ask them to check the code."))
        }
        session.claimToken = token
        session.supplierId = r.optString("supplier_id").takeIf { it.isNotBlank() } ?: session.supplierId
        return ToolResult(
            t(
                "手机号验证通过，可以开始录资料了。先用「开始采集」把已有的信息带出来。",
                "Phone verified — we can start on the profile now. Begin collection to pull in what we already have."
            )
        )
    }

    private suspend fun collectBegin(a: JSONObject): ToolResult {
        requireClaim()?.let { return it }
        val id = targetId(a)
        if (id.isEmpty()) return failed(t("缺供应商 ID。", "Missing supplier ID."))
        val ses = api.collectSession(id)
        // 预填是设计流程的一部分（docs §5 步骤 3），但预填 ≠ 自述，提示词里已写死要逐条核对
        val fill = runCatching { api.collectAutofill(id) }.getOrNull()
        val sb = StringBuilder()
        if (ses.optBoolean("resumed", false)) {
            sb.append(t("接上了之前没答完的会话。", "Resumed an earlier unfinished session.")).append('\n')
        }
        fill?.optJSONArray("filled")?.let { arr ->
            if (arr.length() > 0) {
                sb.append(
                    t(
                        "系统按公司名/名录预填了 ${arr.length()} 项（**未经对方确认**）：",
                        "The platform pre-filled ${arr.length()} items from the name/directory (**not confirmed**):"
                    )
                ).append('\n')
                for (i in 0 until arr.length()) {
                    val f = arr.optJSONObject(i) ?: continue
                    sb.append("  - ").append(f.optString("path")).append(" = ")
                        .append(f.optString("value")).append('\n')
                }
            }
        }
        sb.append(questionBlock(ses))
        return ToolResult(sb.toString().trimEnd())
    }

    private suspend fun collectAnswer(a: JSONObject): ToolResult {
        requireClaim()?.let { return it }
        val id = targetId(a)
        if (id.isEmpty()) return failed(t("缺供应商 ID。", "Missing supplier ID."))
        val text = a.optString("text", "").trim()
        val r = if (text.isEmpty()) api.collectSkip(id) else api.collectTurn(id, text)
        val sb = StringBuilder()
        r.optJSONObject("extracted")?.let { ex ->
            val path = ex.optString("path")
            sb.append(
                if (ex.optBoolean("parsed", false)) {
                    t("已记下「$path」。", "Recorded \"$path\".")
                } else {
                    t(
                        "「$path」这一题没能从话里读出可用数值，先留空，后面可以回头补。",
                        "\"$path\" could not be parsed from their words — left blank for now; can be revisited."
                    )
                }
            ).append('\n')
        }
        sb.append(questionBlock(r))
        return ToolResult(sb.toString().trimEnd())
    }

    private suspend fun collectProgress(a: JSONObject): ToolResult {
        requireClaim()?.let { return it }
        val id = targetId(a)
        if (id.isEmpty()) return failed(t("缺供应商 ID。", "Missing supplier ID."))
        return ToolResult(stateBlock(api.collectState(id)))
    }

    private suspend fun collectConfirm(a: JSONObject): ToolResult {
        requireClaim()?.let { return it }
        val id = targetId(a)
        if (id.isEmpty()) return failed(t("缺供应商 ID。", "Missing supplier ID."))
        val r = api.collectConfirm(id, a.optBoolean("overwrite_existing", false))
        if (r.optString("status") != "generated") {
            return failed(t("定稿没有成功：", "Finalising did not succeed: ") + r.toString())
        }
        // 定稿了就没有"待答的题"了，别再把旧题塞回提示词
        session.pending = null
        val sb = StringBuilder()
        sb.append(t("能力卡已生成。", "The capability card has been generated.")).append('\n')
        r.optJSONObject("completeness")?.let { c ->
            sb.append(t("完整度 ", "Completeness ")).append(c.opt("score")).append("%\n")
        }
        sb.append(t("供应商口述确认 ", "Vendor-declared fields: ")).append(r.optInt("self_declared_count"))
            .append(t(" 项", "")).append('\n')
        val inferred = r.optJSONArray("inferred_fields")
        if (inferred != null && inferred.length() > 0) {
            val names = (0 until inferred.length()).map { inferred.optString(it) }
            sb.append(
                t(
                    "另有 ${inferred.length()} 项是平台预填、对方没确认过（${names.joinToString("、")}），" +
                        "这些**不算企业自述**，已单独标记。",
                    "${inferred.length()} fields were platform pre-filled and never confirmed " +
                        "(${names.joinToString(", ")}) — these are **not self-declared** and are flagged as such."
                )
            ).append('\n')
        }
        if (r.isNull("validated") || r.opt("validated") == null) {
            sb.append(
                t(
                    "⚠ 这次没有做 Schema 校验（服务端缺 jsonschema），不要说成「已校验通过」。",
                    "⚠ No schema validation ran this time (jsonschema missing on the server). " +
                        "Do not claim it was validated."
                )
            ).append('\n')
        }
        return ToolResult(sb.toString().trimEnd())
    }

    // ── 回灌文本 ───────────────────────────────────────────────────────────

    /** 当前问题 + 进度。**这一块是 LLM 唯一该据以提问的东西**，它不自己管题序。 */
    private fun questionBlock(r: JSONObject): String {
        val q = r.optJSONObject("next_question")
        if (q == null) {
            // 问完了：清掉待答状态，别再往提示词里塞"当前这一题"
            session.pending = null
            return t(
                "所有问题都问完了。可以先汇总一次进度，确认没有要改的，再定稿。",
                "All questions are done. Summarise progress, confirm nothing needs changing, then finalise."
            )
        }
        // 记下待答的那一题：MainViewModel 每轮把它塞回系统提示词，
        // 并在这一轮没提交时如实告诉用户（模型漏调工具时唯一的兜底）
        rememberPending(q)
        return buildString {
            append(t("进度 ", "Progress ")).append(q.optInt("index") + 1).append("/")
                .append(q.optInt("total")).append('\n')
            append(t("当前这一题（照这个意思问，用你自己的话）", "Ask this (in your own words):")).append('\n')
            append("  ").append(q.optString("question")).append('\n')
            if (q.optString("hint").isNotBlank()) {
                append(t("  提示：", "  hint: ")).append(q.optString("hint")).append('\n')
            }
            append(t("  字段：", "  field: ")).append(q.optString("path"))
            if (q.optBoolean("required", false)) append(t("（必填）", " (required)"))
            q.optJSONArray("enum")?.let { e ->
                if (e.length() > 0) {
                    append('\n').append(t("  可选值：", "  allowed: "))
                        .append((0 until e.length()).joinToString(" / ") { e.optString(it) })
                }
            }
        }.trimEnd()
    }

    /** 进度/把关。**必填与冲突要原样带出来**——门禁不过就是不过，不美化。 */
    private fun stateBlock(st: JSONObject): String {
        // 进度里也带 next_question，顺手把待答状态对齐（否则 begin 之后又查一次进度，
        // 待答的题可能和最新进度不一致）
        rememberPending(st.optJSONObject("next_question"))
        val sb = StringBuilder()
        st.optJSONObject("completeness")?.let { c ->
            sb.append(t("完整度：", "Completeness: ")).append(c.opt("score")).append("%")
                .append("  ").append(c.optString("note").ifBlank { "" }).append('\n')
        }
        val miss = st.optJSONArray("missing_required")
        sb.append(
            if (miss == null || miss.length() == 0) {
                t("必填项已齐。", "All required fields are filled.") + '\n'
            } else {
                val names = (0 until miss.length()).map { miss.optString(it) }
                t("还缺 ${miss.length()} 项必填：${names.joinToString("、")}", "Missing ${miss.length()} required: ${names.joinToString(", ")}") + '\n'
            }
        )
        st.optJSONArray("conflicts")?.let { cs ->
            if (cs.length() > 0) {
                val lines = (0 until cs.length()).mapNotNull { cs.optJSONObject(it)?.let { c ->
                    "  - [${c.optString("level")}] ${c.optString("path")}：${c.optString("message")}"
                } }
                sb.append(t("有 ${cs.length()} 处互相矛盾：", "${cs.length()} conflicting answers:")).append('\n')
                lines.forEach { sb.append(it).append('\n') }
            }
        }
        st.optJSONArray("prefilled_unconfirmed")?.let { p ->
            if (p.length() > 0) {
                val names = (0 until p.length()).map { p.optString(it) }
                sb.append(
                    t(
                        "${p.length()} 项是平台预填、对方未确认（${names.joinToString("、")}），" +
                            "**不算企业自述**。",
                        "${p.length()} platform pre-filled, unconfirmed fields (${names.joinToString(", ")}) — " +
                            "**not self-declared**."
                    )
                ).append('\n')
            }
        }
        sb.append(t("可以定稿：", "Ready to finalise: "))
            .append(if (st.optBoolean("ready_to_confirm", false)) t("是", "yes") else t("否", "no"))
        // storage=d1 才是云端真源；local 是降级到服务端本地磁盘，必须说出来
        val storage = st.optString("storage")
        if (storage.isNotBlank()) {
            sb.append('\n').append(t("会话存储：", "Session storage: ")).append(storage)
            if (storage != "d1") {
                sb.append(t("（⚠ 降级了，没上云）", " (⚠ degraded, not on cloud)"))
            }
        }
        return sb.toString().trimEnd()
    }
}
