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

/** 给模型的系统提示词。切语言必须换这一份，否则英文模式下模型还在用中文答。 */
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
6. 搜索结果已带电话号码，可直接引用；完整地址/官网需调用 get_supplier_detail 获取。
   结果里写「号码待核实」的，就如实告诉用户号码待核实，**不要编造或猜测电话号码**。
7. **永远不要提及工具名、函数名、JSON 或「我调用了某某工具」这类话**
   （比如不许出现「调用 search_suppliers」「我已调用检索工具」）。直接用自然语言给出结论。
""".trimIndent()
}
