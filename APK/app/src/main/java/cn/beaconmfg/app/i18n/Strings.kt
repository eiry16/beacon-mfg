package cn.beaconmfg.app.i18n

/**
 * 界面语言。**持久化只存 code（"zh"/"en"），不存枚举序号**——
 * 将来插一门语言时，存序号会让老用户一夜之间变成别的语言。
 */
enum class Lang(val code: String, val label: String) {
    ZH("zh", "中文"),
    EN("en", "English"),
    ;

    companion object {
        fun of(code: String?): Lang = entries.firstOrNull { it.code == code } ?: ZH
        fun isEn(code: String?): Boolean = of(code) == EN
    }
}

/**
 * 使用者身份：买家（找供应商）/ 供应商（认领自家企业）。
 *
 * **为什么要物理隔离成两套**：买家侧是只读检索，供应商侧要写入（认领、提交材料）。
 * 混在一套 ToolBox 里，模型会在采购对话中误触发写入动作 —— 那是会污染数据的。
 * 所以一次只加载一套工具 + 一套提示词，切身份等于换一个人格。
 *
 * 和 Lang 一样**只存 code 字符串**，不存序号。
 */
enum class Role(val code: String) {
    BUYER("buyer"),
    SUPPLIER("supplier"),
    ;

    companion object {
        fun of(code: String?): Role = entries.firstOrNull { it.code == code } ?: BUYER
    }
}

/**
 * 双语文案表。
 *
 * 边界（想清楚再改）：**只有「界面语言」和「给模型看的提示词/工具返回」跟着切**，
 * 数据本身不翻译——公司名、地址、工艺名来自企业公开资料，翻成英文就是编造，
 * 客户拿去也搜不到。所以英文模式下公司名/城市名仍是中文原文，
 * 变的是标签、证据档位、按钮和模型的回答语言。
 */
class Strings(val lang: Lang) {

    private val en: Boolean get() = lang == Lang.EN

    private fun t(zh: String, enText: String): String = if (en) enText else zh

    // ── 顶栏 / 导航 ────────────────────────────────────────────────────────
    val appTitle get() = t("炫招灯塔", "BeaconMFG")
    val appSubtitle get() = t("——让AI世界看见", " — be seen by AI")
    val actionClear get() = t("清空", "Clear")
    val actionCancel get() = t("取消", "Cancel")
    val actionSettings get() = t("设置", "Settings")
    val actionChat get() = t("对话", "Chat")

    // ── 对话页 ─────────────────────────────────────────────────────────────
    val inputHint get() = t("想找什么供应商？", "Which supplier do you need?")
    val send get() = t("发送", "Send")

    fun samples(): List<String> = if (en) {
        listOf(
            "Conveyor line makers in Shanghai",
            "Small gear shops in Dongguan, with phone",
            "Die casting factories in Ningbo",
            "304 stainless sheet metal, Shenzhen",
        )
    } else {
        listOf(
            "上海有没有做输送线的厂",
            "东莞做齿轮的小厂，要有电话",
            "找宁波的压铸厂",
            "304 不锈钢钣金加工，深圳",
        )
    }

    // ── 供应商卡片 ─────────────────────────────────────────────────────────
    val cityUnknown get() = t("城市未知", "City unknown")
    fun beacon(cl: String) = t("灯牌 $cl", "Beacon $cl")

    fun phone(number: String) = "☎ $number"
    val phonePending
        get() = t(
            "☎ 有电话，但源数据为「待核实」，号码未收录",
            "☎ A phone exists, but the source marks it \"to be verified\" — number not published"
        )
    val phoneNone get() = t("☎ 未收录电话", "☎ No phone on record")
    val certLabel get() = t("认证：", "Certs: ")
    val procLabel get() = t("工艺：", "Processes: ")
    fun procMore(n: Int) = t(" 等 $n 项", " +$n more")

    // ── 能力卡 ─────────────────────────────────────────────────────────────
    val capOpen get() = t("打开", "Open")
    val capHide get() = t("收起", "Hide")
    fun capCount(n: Int) = t("能力卡 · $n 项工艺", "Capability card · $n processes")
    val capEmpty get() = t("能力卡 · 暂无工艺位", "Capability card · no process data")
    val capNoCard get() = t("能力卡：暂无（未进 L1 层）", "Capability card: none (not in L1 yet)")
    val capNoneInDetail
        get() = t(
            "能力卡：暂无（23698 家里只有 4136 家进了 L1 层）",
            "Capability card: none yet (only 4,136 of 23,698 suppliers reached L1)"
        )
    val capSelf get() = t("能力卡 · 厂商自述", "Capability card · Vendor-declared")
    val capAuto
        get() = t(
            "能力卡 · 平台自动整理（工艺由企业名称推断，未获企业确认）",
            "Capability card · Auto-compiled (processes inferred from the company name, " +
                "not confirmed by the vendor)"
        )
    val matLabel get() = t("材料：", "Materials: ")
    val limLabel get() = t("硬指标：", "Hard specs: ")
    val limEmpty get() = t("未填报（留空，不填 0）", "Not declared (left blank — never shown as 0)")
    val skillLabel get() = t("厂商 Skill：", "Vendor skill: ")
    val verified get() = t("已核实", "verified")
    val unverified get() = t("未核实", "unverified")
    val openSkill get() = t("打开", "Open")
    val skillChecking get() = t("检查中…", "Checking…")
    /**
     * 点了「打开」但网页端没有这份自述。
     * L2 内容不进 Git、靠单独部署，新卡没部署就是没有——这句要说清是"没发布"，
     * 不能让采购以为是自己网络坏了或者这家厂没能力。
     */
    val skillUnreachable get() = t(
        "这份完整自述还没发布到网页端（L2 内容不进 Git，需单独部署）",
        "This write-up isn't published to the web yet (L2 content isn't in Git; it needs a separate deploy)"
    )

    // ── 完整档案 ───────────────────────────────────────────────────────────
    val detailPhone get() = t("电话：", "Phone: ")
    val detailSite get() = t("官网：", "Website: ")
    val detailCerts get() = t("认证：", "Certs: ")

    // ── 工具回显（对用户的文案，**不暴露内部函数名**）────────────────────────
    fun toolSearch(n: Int) = t("检索到 $n 家供应商", "Found $n suppliers")
    val toolSearchNone get() = t("没有匹配的供应商", "No supplier matched")
    val toolDetail get() = t("已取到完整档案", "Full profile retrieved")
    val toolCats get() = t("已列出可检索的行业小类", "Industry categories listed")
    val toolDone get() = t("检索完成", "Search finished")

    // ── 身份切换 ───────────────────────────────────────────────────────────
    val secRole get() = t("我的身份", "My role")
    val roleBuyer get() = t("我是采购", "I'm a buyer")
    val roleVendor get() = t("我是供应商", "I'm a supplier")
    val roleNote
        get() = t(
            // 注意：这段话直接进 Compose 的 Text，**不渲染 Markdown**——
            // 写 **xx** 会原样显示成星号（已经踩过一次），强调只能用「」。
            "采购侧只能检索；供应商侧才能认领企业、提交资料。两边用的是两套工具和两套提示词，" +
                "「切换身份会开始一段新对话」——避免采购对话里误触发写入动作。",
            "Buyers can only search; suppliers can claim their company and submit materials. " +
                "The two sides use separate tools and prompts, and switching starts a new " +
                "conversation, so a write action can never fire inside a sourcing chat."
        )
    fun roleSwitched(r: String) =
        t("已切换到「$r」，已开始新对话", "Switched to $r — a new conversation has started")
    val roleBadgeBuyer get() = t("采购模式", "Buyer mode")
    val roleBadgeVendor get() = t("供应商模式", "Supplier mode")

    // ── 供应商侧 / 认领与采集 ───────────────────────────────────────────────
    val vendorInputHint
        get() = t(
            "说「我要登记我的企业」，或者直接报公司名",
            "Say \"register my company\" or just name it"
        )
    fun vendorSamples(): List<String> = if (en) {
        listOf(
            "I want to register my company",
            "Is my company in the directory? 上海耐特斯传输设备有限公司",
            "My company isn't listed — help me register it",
            "What's still missing in my profile?",
        )
    } else {
        listOf(
            "我要登记我的企业",
            "帮我找找 上海耐特斯传输设备有限公司 在不在名录里",
            "我家企业不在名录里，帮我登记进来",
            "我的资料还缺什么？",
        )
    }
    val vendorApiMissing
        get() = t(
            "平台接口地址还没配置，认领和资料采集暂时用不了。" +
                "到「设置 → 平台接口地址」填上服务端地址再试。" +
                "（本地自检/检索不受影响）",
            "The platform API address isn't configured yet, so claiming and profile collection " +
                "are unavailable. Set it under Settings → Platform API address. " +
                "(Local search is unaffected.)"
        )
    val vendorUnreachable get() = t("平台接口没连上：", "Platform API unreachable: ")
    val vendorNotClaimed
        get() = t(
            "这家企业还没被认领。认领需要企业实名的凭证，走不了捷径。",
            "This company hasn't been claimed yet. Claiming requires a real business credential."
        )

    /**
     * 每轮塞进系统提示词的「平台当前状态」标题。
     *
     * 为什么必须每轮塞：模型（tool_choice=auto）会**不调工具直接作答**，也会**跳步骤**
     * （实测：跳过验证码直接调采集，被平台拒了还不重试）。把状态和"下一步该干什么"
     * 每轮摆在它面前，比只在提示词里写一遍流程管用得多。
     */
    // ── 扫码认领 ─────────────────────────────────────────────────────────────

    /**
     * 扫码按钮的文案里**不提「电子营业执照」**。
     *
     * 原因：新版营业执照上的企业码，只有电子营业执照小程序（或已获接入资质的系统）
     * 才能解出照面信息；我们用通用扫码器扫，可能只拿到一串内部 ID。
     * 说"扫电子营业执照"会让对方以为扫完就完成了身份核验，那是**误导**。
     * 所以只说"扫营业执照上的码"，拿到什么算什么，解不出就让他手输。
     */
    val scanButton get() = t("扫码", "Scan")
    val scanTitle get() = t("扫描营业执照上的码", "Scan the code on the licence")
    val scanHint
        get() = t(
            "对准营业执照上的二维码。能识别出 18 位统一社会信用代码就自动填入。",
            "Point at the QR code on the business licence. If an 18-digit USCC is found, it fills in automatically."
        )
    val scanPermissionNeeded
        get() = t("需要相机权限才能扫码", "Camera permission is needed to scan")
    val scanPermissionDenied
        get() = t(
            "相机权限被拒绝了。可在系统设置里打开，或者直接手输 18 位统一社会信用代码。",
            "Camera permission was denied. Enable it in system settings, or just type the 18-digit USCC."
        )
    val scanNoUscc
        get() = t(
            "这个码里没有 18 位统一社会信用代码。",
            "No 18-digit USCC found in this code."
        )
    val scanRawPrefix get() = t("扫到的内容：", "Scanned content: ")
    val scanWhyNot
        get() = t(
            "新版营业执照的「企业码」只有电子营业执照小程序能解出照面信息，通用扫码器读不出。" +
                "所以扫不出来是**正常情况**，不是坏了——直接手输 18 位统一社会信用代码就行。",
            "The new-style licence code can only be decoded by the official e-licence mini-program; " +
                "a generic scanner can't read it. Not finding anything is **normal**, not a bug — " +
                "just type the 18-digit USCC."
        )
    val scanManualHint
        get() = t("手动输入 18 位统一社会信用代码", "Type the 18-digit USCC manually")
    val scanNoCamera
        get() = t("这台设备上没有可用的相机。", "No usable camera on this device.")
    val cameraFailed get() = t("相机启动失败。", "Failed to start the camera.")
    /**
     * 扫码成功后替用户发出的那句话。
     *
     * 为什么要走"发一句话"而不是直接调工具：整条供应商链路都是对话驱动的，
     * 状态由平台掌管。直接塞结果会绕过平台，也会让用户看不懂"刚才发生了什么"。
     * 发一句他能看懂的话，再由模型去调工具，用户所见即所得。
     */
    fun scanSubmit(code: String) =
        t("我的统一社会信用代码是 $code", "My USCC is $code")
    val usccLabel get() = t("统一社会信用代码", "USCC")

    val vendorStateHeader
        get() = t(
            "【平台当前状态】（每次回答前先看这里，按它决定下一步动作）",
            "[Platform state] (check this before every reply; it decides your next action)"
        )

    val vsClaimNone
        get() = t(
            "认领：还没开始。顺序是固定的——先查企业 → 向对方要手机号发起认领 → 收到验证码并验证 → " +
                "**验证通过后才能开始录资料**。跳过任何一步都会被平台拒绝。",
            "Claim: not started. The order is fixed — find the company → ask for the phone number " +
                "and start the claim → verify the code → **only then can you collect the profile**. " +
                "Skipping a step gets rejected by the platform."
        )

    val vsClaimPending
        get() = t(
            "认领：验证码已发出，**正在等对方报这 6 位数字**。拿到后必须用「验证」把码提交上去。",
            "Claim: the code has been sent — **waiting for them to read the 6 digits**. Once you have " +
                "it, submit it with the verify tool."
        )

    val vsClaimDone
        get() = t("认领：已通过。可以做资料采集了。", "Claim: verified. Profile collection is available.")

    val vsCollectNone
        get() = t(
            "采集：还没开始。认领通过后用「开始采集」把已有信息带出来。",
            "Collection: not started. Once the claim is verified, start collection to pull in what " +
                "we already have."
        )

    /**
     * 采集进行中时的那一题。
     *
     * 为什么必须每轮塞：模型会不调工具直接说「记下了」然后自己编下一题，而平台一步没动。
     * 把当前题面和"必须提交"写进提示词，是让工具调用从"可选项"变成"这一轮的作业"。
     */
    fun vendorTurnContext(
        index: Int,
        total: Int,
        path: String,
        question: String,
        hint: String,
        required: Boolean,
    ): String = t(
        "采集：进度 ${index + 1}/$total（字段 $path）。当前这一题：$question" +
            (if (hint.isBlank()) "" else "（${hint}）") +
            (if (required) "。这是必填项。" else "。这是选填项，答不上来可以跳过。") +
            "\n对方的下一条消息就是这道题的回答：**必须调用提交工具把他的话交上去**" +
            "（答不上来就传空 text 表示跳过）。**不提交就等于没记下**——" +
            "绝对不要自己说「记下了」然后接着问下一题，平台那边一步都没动。",
        "Collection: progress ${index + 1}/$total (field $path). Current question: " +
            "$question" + (if (hint.isBlank()) "" else " ($hint)") +
            (if (required) " This one is required." else " This one is optional; they may skip it.") +
            "\nTheir next message answers this question: **you must call the submit tool** " +
            "(pass empty text to skip). **Not submitting means nothing was recorded** — never say " +
            "\"noted\" and move on by yourself; the platform will not have moved."
    )

    val vendorNothingSubmitted
        get() = t(
            "这一轮没有任何内容提交到平台（进度没变）。如果刚才是在回答问题，麻烦再说一次。",
            "Nothing was submitted to the platform this turn (no progress). If you were answering " +
                "the question, please send it again."
        )

    // ── 供应商侧工具回显 ───────────────────────────────────────────────────
    fun toolVendorMatch(n: Int) =
        t("在名录里找到 $n 家匹配的企业", "Matched $n companies in the directory")
    fun toolVendorMatchSimilar(n: Int) =
        t(
            "没有完全一致的名字，按相似度列出 $n 家候选",
            "No exact name match — listed $n similar candidates"
        )
    val toolVendorMatchNone get() = t("名录里没有匹配的企业", "No matching company in the directory")
    val toolClaimStart get() = t("已发起认领", "Claim started")
    val toolClaimCode get() = t("验证通过", "Code verified")
    val toolCollectBegin get() = t("已开始资料采集", "Profile collection started")
    val toolCollectAnswer get() = t("已记录这一题", "Answer recorded")
    val toolCollectProgress get() = t("已取到填写进度", "Progress retrieved")
    val toolCollectConfirm get() = t("能力卡已生成", "Capability card generated")
    val toolVendorDone get() = t("完成", "Done")

    // ── 运行状态 ───────────────────────────────────────────────────────────
    val booting get() = t("正在装载本地索引…", "Loading local index…")
    val loadGb get() = t("装载国标索引…", "Loading industry-code index…")
    val loadAlias get() = t("装载采购词别名表…", "Loading sourcing-term aliases…")
    val loadFp get() = t("装载全量指纹…", "Loading full fingerprints…")
    fun ready(n: Int) = t("就绪：本地 $n 家供应商，可直接离线检索", "Ready: $n suppliers on-device, offline search works")
    fun sourceDownN(n: Int) = t(
        "数据源暂不可达，内置 $n 家可离线检索",
        "Source unreachable — $n suppliers still searchable offline"
    )
    val checkingUpdate get() = t("检查数据更新…", "Checking for data updates…")
    fun updateDone(msg: String) = t("数据更新完成：$msg", "Data update finished: $msg")
    val noKey
        get() = t(
            "还没配置 API key。到「设置」里选服务商、填自己的 key 就能用——" +
                "key 只存在本机（Keystore 加密），不会发给除你所选端点以外的任何地方。",
            "No API key yet. Open Settings, pick a provider and paste your own key. " +
                "The key stays on this device (Keystore-encrypted) and is only ever sent " +
                "to the endpoint you chose."
        )
    fun callFailed(e: String) = t("模型调用失败：$e", "Model call failed: $e")

    // ── 网络/端点错误（LlmClient 用；英文模式下不能蹦中文）─────────────────
    val err401 get() = t("API key 无效或已过期（HTTP 401）", "Invalid or expired API key (HTTP 401)")
    val err429 get() = t("触发限流（HTTP 429），稍后再试", "Rate limited (HTTP 429) — try again later")
    fun errHttp(code: Int, detail: String) = t(
        "请求失败 HTTP $code：$detail",
        "Request failed — HTTP $code: $detail"
    )
    fun errNetwork(msg: String) = t("网络错误：$msg", "Network error: $msg")
    fun errTimeout(sec: Int) = t(
        "模型 ${sec} 秒没有响应，已放弃这次请求（可以再说一次）。",
        "The model did not respond within ${sec}s — request abandoned. You can ask again."
    )
    val turnTimeout
        get() = t(
            "这一轮处理超时，已停止。可以换个更具体的说法再问一次。",
            "This turn timed out and was stopped. Try a more specific wording."
        )
    val errNoBytes get() = t(
        "模型连接长时间没有数据，已断开。",
        "The model connection went silent for too long and was dropped."
    )
    fun testOk(model: String) = t("连通正常（HTTP 200，模型 $model）", "Connected (HTTP 200, model $model)")
    fun testFail(code: Int, detail: String) = t(
        "失败 HTTP $code：$detail",
        "Failed — HTTP $code: $detail"
    )
    fun testConnErr(msg: String) = t("连接失败：$msg", "Connection failed: $msg")
    val emptyAnswer get() = t("（模型没有返回内容）", "(the model returned nothing)")
    val roundLimit
        get() = t(
            "工具调用轮次已达上限，已停止。可以换个更具体的说法再问。",
            "Reached the tool-round limit and stopped. Try a more specific wording."
        )
    val localFallback
        get() = t(
            "以下为本地直检结果（模型本次没有调用检索）",
            "Below are direct local-search results (the model did not call search this turn)"
        )
    val lastUpdateNote get() = t("【最近一次更新】", "[Last update attempt]")

    // ── 供应商认证等级（灯牌，定义见 docs/CERTIFICATION_V1.md §1）──────────
    fun tierLabel(code: String): String = when (code) {
        "L1" -> t("已认领", "Claimed")
        "L2" -> t("已认证", "Verified")
        "L3" -> t("已验厂", "Audited")
        else -> t("未核验", "Unverified")
    }

    /** 徽章旁边那句「被核验到什么程度」。含核验范围，也含没核验的部分。 */
    fun tierHint(code: String): String = when (code) {
        "L1" -> t("企业已认领，信息由企业自述，平台未核验",
            "Claimed by the company — details are self-declared, not verified by the platform")
        "L2" -> t("营业执照已核验 + 材料齐备 + 人工复核",
            "Business licence checked, materials complete, human-reviewed")
        "L3" -> t("第三方实地核验或客户案例佐证",
            "Verified on site by a third party, or backed by client references")
        else -> t("公开名录自动收录，未经任何核验",
            "Auto-listed from public records — nothing verified")
    }

    /** 灯牌不是评级。这句限定必须跟着徽章出现，不能只在文档里写。 */
    val beaconNote get() = t(
        "灯牌说明的是「信息被核验到什么程度」，不代表这家厂好不好——平台不做评级。",
        "A beacon says how far the information was verified — not whether the factory " +
            "is any good. The platform does not rate suppliers."
    )
    val beaconExpired get() = t(
        "存证有效期已过（降级与否以平台为准）",
        "Certification expired (downgrades are decided by the platform)"
    )
    fun beaconIssued(x: String) = t("签发于 $x", "Issued $x")
    fun beaconValidUntil(x: String) = t("有效期至 $x", "Valid until $x")
    fun beaconCompleteness(pct: String) = t("硬指标完成度 ${pct}%", "Hard-spec completeness ${pct}%")
    val beaconAutoRfq get() = t("可接自动询价", "Accepts automated RFQ")
    val detailBeacon get() = t("认证：", "Beacon: ")
    val briefBeacon get() = t("认证", "Beacon")

    // ── 设置页 ─────────────────────────────────────────────────────────────
    val secLanguage get() = t("语言", "Language")
    val langNote
        get() = t(
            "切换后界面、模型回答与工具回显都会变；已发出的消息不会回译。",
            "Switching changes the UI, the model's replies and tool echoes. " +
                "Messages already on screen are not retro-translated."
        )

    val secProvider get() = t("模型服务商（BYOK）", "Model provider (BYOK)")
    val keyNote
        get() = t(
            "key 只保存在本机 Keystore 加密区，请求直连你选的服务商，不经过本项目任何服务器。",
            "The key is stored only in this device's Keystore-encrypted area. " +
                "Requests go straight to the provider you pick — never through our servers."
        )
    val keystoreWarn
        get() = t(
            "⚠ 本机 Keystore 不可用，key 将明文存储。建议换一台设备再填。",
            "⚠ Keystore is unavailable here — the key would be stored in plain text. " +
                "Consider using another device."
        )
    val labelEndpoint get() = t("API 端点（OpenAI 兼容）", "API endpoint (OpenAI-compatible)")
    val labelModel get() = t("模型名", "Model name")
    val labelApiKey get() = t("API Key", "API Key")
    val save get() = t("保存", "Save")
    val testConn get() = t("测试连接", "Test connection")
    val saved get() = t("已保存", "Saved")
    val testing get() = t("测试中…", "Testing…")

    val secData get() = t("数据源与更新", "Data source & updates")
    val labelDataBase get() = t("数据源根地址", "Data source root URL")
    val updateNow get() = t("立即更新", "Update now")
    val ping get() = t("检测连通性", "Check connectivity")
    val autoUpdate get() = t("联网时自动更新指纹", "Auto-update fingerprints when online")

    val secProbe get() = t("本地自检（不经过 LLM）", "Local self-check (no LLM)")
    val probeHint get() = t("输入采购词，如 齿轮 / 输送线", "Enter a sourcing term, e.g. gear / conveyor")
    val probeRun get() = t("本地检索", "Search locally")
    fun aliasCount(n: Int) = t("别名表 $n 条", "Alias index: $n entries")

    // ── 供应商侧后端地址 ───────────────────────────────────────────────────
    val secVendorApi get() = t("供应商功能（平台接口）", "Supplier features (platform API)")
    val labelApiBase get() = t("平台接口地址", "Platform API address")
    val apiBaseNotConfigured
        get() = t(
            "未配置 —— 供应商侧的认领与资料采集不可用（点开会显示未开通，不会假装成功）。",
            "Not configured — claiming and profile collection are unavailable " +
                "(attempts report it instead of pretending to succeed)."
        )
    val apiBaseNote
        get() = t(
            "认领企业、提交资料要走平台服务端（本机检索不经过它）。留空则供应商侧功能显示为未开通，" +
                "不会假装成功。开发时可填 http://127.0.0.1:8000 并用 adb reverse 转发。",
            "Claiming and submitting materials go through the platform server (local search does " +
                "not). Leave it blank and supplier features show as unavailable — they never " +
                "pretend to succeed. For development use http://127.0.0.1:8000 with adb reverse."
        )

    // ── 数据层标签（卡片、给模型回灌的文本都用）─────────────────────────────
    fun evidenceLabel(code: Int): String = when (code) {
        0 -> t("字面命中", "Exact match")
        1 -> t("别名首位码", "Top alias class")
        else -> t("行业推断", "Industry inference")
    }

    fun evidenceHint(code: Int): String = when (code) {
        0 -> t("企业自己写了这个词", "The company's own profile contains this word")
        1 -> t("按国标小类匹配，语义最贴近", "Matched via national-standard subclass — closest in meaning")
        else -> t("按国标行业推断，企业未确认", "Inferred from the industry code — not confirmed by the company")
    }

    fun levelLabel(level: String): String = when (level) {
        "primary" -> t("主营", "Primary")
        "secondary" -> t("兼营", "Secondary")
        "outsourced" -> t("外协", "Outsourced")
        else -> ""
    }

    fun limitTol(v: String) = t("公差 ±${v}mm", "Tolerance ±${v}mm")
    fun limitSize(v: String) = t("最大件 $v", "Max part $v")
    fun limitMoq(v: String) = t("起订 $v", "MOQ $v")
    fun limitLt(v: String) = t("交期 $v", "Lead time $v")
    /** 天数。交期可能是对象（样品/批量分开报），拆开后每档都要带单位。 */
    fun days(n: String) = t("${n} 天", "${n} d")
    /**
     * 交期里的一档。`key` 是数据里的原字段名：
     * 认得出的（sample / batch_100 / batch_1000）翻成人话，认不出的原样给——不猜含义。
     */
    fun leadPart(key: String, n: String): String = when (key) {
        "sample" -> t("样品 ${days(n)}", "Sample ${days(n)}")
        "batch_100" -> t("100 件 ${days(n)}", "100 pcs ${days(n)}")
        "batch_1000" -> t("1000 件 ${days(n)}", "1000 pcs ${days(n)}")
        else -> t("${key} ${days(n)}", "$key ${days(n)}")
    }
    fun limitLoad(v: String) = t("当前负荷 ${v}%", "Current load ${v}%")
    val limitRush get() = t("可接急单", "Rush orders accepted")

    // ── 回灌给模型的字段标签（英文模式下模型要拿到英文标签才会用英文答）──────
    val briefEvidence get() = t("证据", "Evidence")
    val briefCert get() = t("认证", "Certs")
    val briefPhone get() = t("电话", "Phone")
    val phoneOnFile get() = t("有（号码待核实）", "on file (to be verified)")
    val phoneNo get() = t("无", "none")

    // ── 数据源 / 更新 / 自检（RemoteSource、DataStore、GbIndex 共用）──────────
    val connTimeout get() = t("连接超时", "timeout")
    val dnsFail get() = t("域名解析失败", "DNS lookup failed")
    val connRefused get() = t("连接被拒绝", "connection refused")
    val tlsFail get() = t("TLS 握手失败", "TLS handshake failed")
    fun switchMirror(host: String) = t("主源不通，换备用源 $host…", "Primary down, trying mirror $host…")
    val manifestUnchanged get() = t("manifest 未变更，无需下载", "manifest unchanged, nothing to download")
    fun updatedShardsMsg(n: Int, kb: Long) =
        t("更新 $n 片（${kb} KB）", "Updated $n shards (${kb} KB)")
    fun updatedShardsFailed(n: Int) = t("，失败 $n 片", ", $n failed")
    fun alreadyLatest(host: String) = t("已是最新（$host）", "Already up to date ($host)")
    fun allSourcesDown(n: Int, detail: String) = t(
        "$n 个数据源均不可达：$detail。内置数据不受影响，可继续离线检索。",
        "All $n sources unreachable: $detail. Built-in data is unaffected — offline search still works."
    )
    fun downloadingShard(i: Int, n: Int, rel: String) =
        t("下载分片 $i/$n：$rel", "Downloading shard $i/$n: $rel")
    val updatingPhoneIndex get() = t("更新号码索引…", "Updating phone index…")
    fun pingOk(host: String, code: Int, ms: Long) =
        t("✓ $host：HTTP $code（${ms}ms）", "✓ $host: HTTP $code (${ms}ms)")
    fun pingFail(host: String, err: String) = t("✗ $host：$err", "✗ $host: $err")

    val builtinFpUnknown
        get() = t(
            "内置指纹：未知（跑 APK/tools/sync_assets.py 生成）",
            "Built-in fingerprints: unknown (run APK/tools/sync_assets.py)"
        )
    fun builtinFp(n: Int, mb: String) = t("内置指纹 $n 片 / $mb MB", "Built-in fingerprints: $n shards / $mb MB")
    fun builtinAtLabel(x: String) = t("内置于 $x", "Built in at $x")
    val unknown get() = t("未知", "unknown")
    val never get() = t("从未", "never")
    fun phoneIndexMsg(n: Int) = t(
        "号码索引 $n 家（其余源数据为占位值，留空）",
        "Phone index: $n companies (the rest are placeholders in the source — left blank)"
    )
    fun updatedShards(n: Int) = t("已更新分片 $n 个", "Updated shards: $n")
    fun lastCheck(x: String) = t("上次检查 $x", "Last check: $x")
    val gbIndexNotLoaded get() = t("国标索引未加载", "Industry index not loaded")
    val gbIndexNoMeta get() = t("国标索引（无生成时间）", "Industry index (no build timestamp)")
    fun gbIndexGeneratedAt(x: String) = t("索引生成于 $x", "Index built at $x")

    val sep: String get() = t("、", ", ")
    val dotSep: String get() = " · "
}

/**
 * 按身份取系统提示词。**两套提示词人格不同，一次只给一套**——
 * 理由见 `Role` 的注释，以及 docs/vendor-onboarding-design.html §3.1。
 */
fun systemPrompt(role: Role, lang: Lang): String = when (role) {
    Role.SUPPLIER -> vendorSystemPrompt(lang)
    Role.BUYER -> systemPrompt(lang)
}

/**
 * 买家侧：检索助手。
 * 切语言必须换这一份，否则英文模式下模型还在用中文答。
 */
fun systemPrompt(lang: Lang): String = if (lang == Lang.EN) {
    """
You are the search assistant inside the BeaconMFG app. You help manufacturing buyers
find suppliers in a Chinese manufacturing directory.

Hard rules (breaking any of them counts as a wrong answer):
1. Every supplier fact must come from tool output. **Never invent** a company name,
   phone number, city, certification or capacity from memory or guesswork.
2. Results carry an evidence tier. Report it honestly:
   - Exact match: the company's own profile contains that word;
   - Top alias class: matched by national-standard subclass, closest in meaning;
   - Industry inference: inferred from the industry code only, **not confirmed by the company**.
     Say it is an industry inference; never say "this company makes XX".
3. If nothing matches, say so and suggest a next step (reword / widen the region /
   list available industries). Do not pad the answer with invented suppliers.
4. Translate casual buyer language into search parameters first
   (e.g. "conveyor line makers in Shanghai" -> keyword=conveyor line, city=Shanghai).
   **The directory is Chinese. Always convert the buyer's term into Chinese before
   calling search_suppliers** (gear -> 齿轮, conveyor line -> 输送线, die casting -> 压铸,
   sheet metal -> 钣金, injection molding -> 注塑). Searching in English returns 0 hits.
   Report company names in their original Chinese — do not transliterate them.
5. Answer in English. Keep it short. Recommend 3-5 suppliers by default, giving
   company name, city, main business and evidence tier; add more only if asked.
   **Plain text only — the app does not render Markdown.** Never emit `**`, `#` or
   `-` list markers; use blank lines and plain numbering instead.
6. Search results already include phone numbers — quote them directly; full address
   and website need get_supplier_detail. If a result says the number is "to be
   verified", say exactly that and **never invent or guess a phone number**.
7. Never mention tool names, function names, JSON, IDs of internal calls, or the fact
   that you called something. Do not write things like "I called search_suppliers".
   Just present the findings in plain language.
""".trimIndent()
} else {
    """
你是「供应商灯塔」App 的检索助手，帮制造业采购人员在中国制造业名录里找供应商。

硬规则（违反即为错误回答）：
1. 所有供应商信息必须来自工具返回的真实数据。**严禁凭记忆或推测编造**公司名、电话、城市、认证、产能。
2. 检索结果带「证据」档位，必须如实转述：
   - 字面命中：企业自己的资料里写了这个词；
   - 别名首位码：按国标小类匹配，语义最贴近；
   - 行业推断：只是按国标行业推断，**企业未确认**。说的时候必须带「按行业推断，企业未确认」，
     不许说成「这家做 XX」。
3. 查不到就直说查不到，并给出下一步建议（换说法 / 放宽地区 / 看有哪些行业）。不要硬凑。
4. 用户的口语要先翻译成检索条件：如「上海有没有做输送线的」→ keyword=输送线, city=上海。
4b. 结果里的**认证等级（灯牌）**说的是「这家企业的信息被核验到什么程度」：
   L0 未核验（公开名录自动收录）/ L1 已认领（企业自述）/ L2 已认证（执照已核验）/
   L3 已验厂。必须按这个口径如实转述：L0 就说是未核验，**不许升级说成已认证/已验厂**，
   也不许据此推断这家厂质量好不好——灯牌不是评级。绝大多数企业目前是 L0。
5. 回答用中文，简洁。默认只推荐 3–5 家，给出公司名、城市、主营、证据档位；用户要看更多再补充。
   **只用纯文本，App 不渲染 Markdown**：不要出现 `**`、`#` 这类标记，列点用换行和「1. 2.」。
6. 搜索结果已带电话号码，可直接引用；完整地址/官网需调用 get_supplier_detail 获取。
   结果里写「号码待核实」的，就如实告诉用户号码待核实，**不要编造或猜测电话号码**。
7. **永远不要提及工具名、函数名、JSON 或「我调用了某某工具」这类话**
   （比如不许出现「调用 search_suppliers」「我已调用检索工具」）。直接用自然语言给出结论。
""".trimIndent()
}

/**
 * 供应商侧：认领 + 资料采集助手。
 *
 * 与买家侧**人格完全不同**——买家侧怕它多说，供应商侧怕它少问。
 * 但两条底线两边一样：不编造数字、灯牌不是评级。
 *
 * 这里刻意写死「自动预填 ≠ 企业自述」：服务端 autofill 会用公司名/名录 POI 预填字段，
 * 模型很容易顺手说成「已确认」。那会把平台推断洗成企业自述，违反「弱证据不覆盖强证据」。
 */
fun vendorSystemPrompt(lang: Lang): String = if (lang == Lang.EN) {
    """
You are the supplier-side assistant inside the BeaconMFG app. The person you are talking to
is the owner or an employee of a Chinese manufacturing company. They are here to register or
claim their own company and fill in its profile.

Who you are / who you are not:
- You are NOT a sourcing assistant. They are not looking for suppliers — they are registering
  themselves. Never recommend other suppliers to them.
- Your one job: help them complete their profile by talking (roughly 30 questions), then
  produce a capability card. Factory owners will not write JSON or fill a 40-field form,
  but they will answer questions.

There are two paths — work out which one applies first:
- **Claiming**: the company IS already in the directory → find its ID, verify an SMS code,
  then collect.
- **Registering**: the company is NOT in the directory (new factory, renamed, or an uncommon
  name) → call register_company to create the record and get a newly assigned ID, then verify
  the SMS code and collect.
- When a lookup finds nothing, **never just say "not found" and stop**: ask "would you like to
  register your company now?" This is the most common fork in the road — the directory is built
  from public data, so plenty of real companies are simply not in it.
- Registration needs three things, none optional: the full legal name on the licence, the
  business address, and the main category (specific enough — "precision sheet metal", not
  "metalwork"). If one is missing, ask for it; **never guess it**.
- **Registering is not verification**: the SMS check still happens afterwards. Do not say the
  company is "verified" before that succeeds.

Hard rules:
1. **Never invent a number.** Tolerance, capacity, lead time and MOQ may only come from what
   the owner actually says. If they didn't say it, leave it blank — do not fill in an
   "industry typical" value. Blank is honest; inventing is a red line.
2. **Record what they said, don't interpret it.** Terms like "one silk" (0.01 mm) are
   normalised by the system, which has a deterministic word list. Don't do the conversion
   yourself and don't guess the value.
3. **Auto-filled is not self-declared.** The system may pre-fill city, address or processes
   from the company name or the directory record. Read each one back for confirmation; if
   the owner corrects it, their version wins. Never call a pre-filled field "confirmed".
4. **Claiming and registering both go through verification**: the company's identity must be
   verified with a real business credential. **"I am the owner" is not proof.** Don't promise a
   tier or a go-live date before that.
5. Tiers: L1 claimed (self-declared) / L2 verified (licence checked) / L3 audited.
   You can only help them submit; **the tier is computed by platform rules** — you cannot
   set it and must not promise a specific one.
6. Ask one question at a time and wait. If they can't answer, skip it — a skip means blank,
   never a default value. They may walk away; the session resumes across days.
7. Answer in English, in a colleague-like tone, not customer-service boilerplate.
   **Plain text only — the app does not render Markdown.** Never emit `**`, `#` or
   `-` list markers.
8. **Never mention tool names, function names, JSON, or that you called anything.**
   Say "I found…" or "noted".
9. Keep each reply short. If something is missing, name exactly what's missing —
   never paper over it with "your profile is complete".
""".trimIndent()
} else {
    """
你是「炫招灯塔」App 的供应商侧助手。对面是**制造业企业的老板或员工**，
他来登记自己的企业、把资料补全。

你是谁、你不是谁：
- 你**不是采购助手**。他不是来找供应商的，是来登记自己的。不要给他推荐别的供应商。
- 你只有一件事：帮他**用说话的方式**把企业资料补全（大概 30 个问题），最后生成一张能力卡。
  制造业老板不会写 JSON、也不会填 40 个字段的表单，但他会回答问题。把填表成本从 2 小时
  压到 15 分钟对话，这件事才有意义。

登记有两条路，先分清走哪一条：
- **认领**：名录里**已经有**这家企业 → 查到它的 ID，发验证码核验，然后采集。
- **注册**：名录里**没有**这家企业（新开的厂、改过名、名字生僻都会这样）→ 先用
  register_company 登记建档、拿到新分配的 ID，再发验证码核验、采集。
- 查不到时**绝不能只说一句"没有这家"就结束**：要主动问「要不要现在把贵公司登记进来」。
  这是最常见的岔路——名录是公开数据整理的，大量真实企业本来就不在里面。
- 登记要三样，缺一不可：营业执照上的公司全称、经营地址、主品类（具体到「精密钣金」
  这一层，「做五金」太泛不行）。少一样就先问清楚，**不要凭猜测补齐**。
- **注册 ≠ 已核验**：登记完照样要发验证码核验手机号。核验之前不许说"已经认证好了"。

硬规则（违反即为错误回答）：
1. **绝不编造任何数字。** 公差、产能、交期、起订量只能来自老板亲口说的。他没说就留空，
   不许按"行业惯例"填一个。留空是诚实的，编造是红线。
2. **录他说的原话，不要替他换算。** "一丝"、"一个道"、"头发丝的三分之一"这类说法
   由系统用确定性词表归一化。你不要自己换算成数字，更不要猜。
3. **自动预填 ≠ 企业自述。** 系统可能根据公司名或名录记录预填了城市、地址、工艺。
   这些必须**逐条念给老板确认**，他改口就按他说的算。没确认过的字段，
   绝不要说成"已确认"——那会把平台推断洗成企业自述，违反"弱证据不覆盖强证据"。
4. **认领和注册都要走核验流程**：企业身份要用**企业实名的凭证**核验，
   不能因为对方说"我就是老板"就通过。核验通过之前，不要承诺任何等级或上架时间。
5. 等级口径：L1 已认领（企业自述）/ L2 已认证（执照已核验）/ L3 已验厂。
   你只能帮他把资料补齐、提交，**等级由平台规则算出**，不由你也不由他说了算，
   不许承诺具体等级。
6. 一次只问一个问题，等他答完再问下一题。他答不上来就跳过——**跳过就是留空，不是默认值**。
   他聊到一半去车间了也没关系，会话能跨天接着答。
7. 回答用中文，语气像同事，不像客服。别用"尊敬的客户"这类话。
   **按 App 当前语言回答，不要跟着历史消息的语言走**——对方可能中途切了语言，
   这时历史消息是一种语言、界面已经是另一种。不要照抄历史消息的语言。
   **只用纯文本，App 不渲染 Markdown**：不要出现 `**`、`#` 这类标记。
8. **永远不要提及工具名、函数名、JSON 或「我调用了某某工具」这类话。**
   直接说"我查到了"、"已经记下了"。
9. 每轮回答尽量短。资料有缺就直说缺哪一项，别用"资料已经完善"糊过去。
""".trimIndent()
}
