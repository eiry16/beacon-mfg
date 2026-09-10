package cn.beaconmfg.app

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import cn.beaconmfg.app.data.AppSettings
import cn.beaconmfg.app.data.CapabilityCard
import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.GbIndex
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.PlatformApi
import cn.beaconmfg.app.data.RemoteSource
import cn.beaconmfg.app.data.SearchParams
import cn.beaconmfg.app.data.SettingsRepo
import cn.beaconmfg.app.data.SupplierDetail
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Role
import cn.beaconmfg.app.i18n.Strings
import cn.beaconmfg.app.i18n.systemPrompt
import cn.beaconmfg.app.llm.BuyerToolBox
import cn.beaconmfg.app.llm.ChatMsg
import cn.beaconmfg.app.llm.LlmClient
import cn.beaconmfg.app.llm.LlmConfig
import cn.beaconmfg.app.llm.Preset
import cn.beaconmfg.app.llm.ToolResult
import cn.beaconmfg.app.llm.ToolSet
import cn.beaconmfg.app.llm.VendorSession
import cn.beaconmfg.app.llm.VendorToolBox
import cn.beaconmfg.app.search.AliasIndex
import cn.beaconmfg.app.search.SearchEngine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.json.JSONObject

/**
 * 对话 + 数据装载 + 设置的总控。
 *
 * 数据红线在这里执行：**供应商数据只来自本地检索**，LLM 只负责理解需求与组织语言。
 * 回灌给模型的结果一律带证据档位，系统提示词里也写死禁止编造。
 */
class MainViewModel(app: Application) : AndroidViewModel(app) {

    /** 一轮对话的总时限（毫秒）。本地检索 + 最多 4 次模型调用都得在这个框里跑完。 */
    private val TURN_TIMEOUT_MS = 180_000L

    /**
     * 排障日志。tag 统一 BeaconMFG，用 `adb logcat -s BeaconMFG` 只看这些：
     * 一轮对话里「模型调用/工具执行」每一步的耗时都会打出来，
     * 卡住时能直接看出卡在哪一步（这是实测卡死后加的，别删）。
     */
    private fun log(msg: String) = Log.d("BeaconMFG", msg)

    /**
     * **消息的发出方**（谁说的）。别和 [cn.beaconmfg.app.i18n.Role]（使用者身份：采购/供应商）
     * 搞混——那是"这个人是谁"，这是"这条消息是谁发的"。
     */
    enum class Sender { USER, ASSISTANT, SYSTEM }

    data class UiMessage(
        val id: Long,
        val sender: Sender,
        val text: String,
        val hits: List<Hit> = emptyList(),
        val detail: SupplierDetail? = null,
        val streaming: Boolean = false,
        /**
         * 工具回显行。**只显示一行状态，不再跟着渲染卡片**——
         * 之前工具消息和最终回答各渲染一遍，同一次搜索会看到两轮一模一样的灯牌。
         */
        val isTool: Boolean = false,
        /**
         * 本条消息命中的能力卡（id → 卡）。在 IO 线程预先解析好再交给 UI，
         * 不在 Compose 组合期读 assets。
         */
        val caps: Map<String, CapabilityCard> = emptyMap(),
        /** 本次会话第一次搜索：能力卡默认展开，让「能力卡在哪」一眼可见。 */
        val autoOpenCap: Boolean = false,
        /** 卡片来自本地直检兜底（模型这一轮没调检索）。UI 会如实标注，不让用户误以为是模型筛的。 */
        val fallback: Boolean = false,
        /**
         * 工具那一步**没成功**（接口未配置 / 不通 / 服务端拒绝）。回显行标红，
         * 不让失败伪装成一句灰色的「已记录」。
         */
        val toolFailed: Boolean = false,
    )

    private val store = DataStore(app)
    private val alias = AliasIndex(store)
    private val engine = SearchEngine(store, alias)
    private val remote = RemoteSource(store)
    private val repo = SettingsRepo(app)

    /**
     * 买家侧工具（三个只读）。**惰性构造**——身份是供应商时它根本不会被实例化。
     */
    private val buyerTools: ToolSet by lazy {
        BuyerToolBox(
            engine, store, remote,
            dataBase = { _settings.value.dataBase },
            lang = { Lang.of(_settings.value.lang) },
        )
    }

    /** 供应商侧的认领凭证，进程内短时有效（不落盘，见 [VendorSession] 注释）。 */
    private val vendorSession = VendorSession()

    private val vendorApi = PlatformApi(
        base = { _settings.value.apiBase },
        token = { vendorSession.claimToken },
    )

    /**
     * 供应商侧工具（认领 + 采集，**含写操作**）。
     *
     * 与买家侧**同时只存在一套**：切身份会把另一套丢掉重来。
     * 这不是省内存，是安全边界——见 [ToolSet] 的注释。
     */
    private val vendorTools: ToolSet by lazy {
        VendorToolBox(store, vendorApi, vendorSession, lang = { Lang.of(_settings.value.lang) })
    }

    /** 当前身份。设置里存的是 code，认不出按买家处理（fail-safe：宁可只读）。 */
    private fun role(): Role = Role.of(_settings.value.role)

    private fun toolSet(): ToolSet = if (role() == Role.SUPPLIER) vendorTools else buyerTools

    private var seq = 0L
    /** 会话内第几次提问。只影响「首次搜索默认展开能力卡」。 */
    private var turnIndex = 0

    private val _messages = MutableStateFlow<List<UiMessage>>(emptyList())
    val messages: StateFlow<List<UiMessage>> = _messages.asStateFlow()

    private val _status = MutableStateFlow(Strings(Lang.ZH).booting)
    val status: StateFlow<String> = _status.asStateFlow()

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    private val _settings = MutableStateFlow(repo.load())
    val settings: StateFlow<AppSettings> = _settings.asStateFlow()

    private val _dataInfo = MutableStateFlow("")
    val dataInfo: StateFlow<String> = _dataInfo.asStateFlow()

    val secureStorageAvailable: Boolean = repo.secureStorageAvailable

    /** 当前界面语言。切语言后所有 Composable 靠它重算文案。 */
    private fun strings(): Strings = Strings(Lang.of(_settings.value.lang))

    init {
        viewModelScope.launch {
            boot()
        }
    }

    private suspend fun boot() = withContext(Dispatchers.IO) {
        val s = strings()
        _status.value = s.loadGb
        store.readAsset("index/gb-index.json")?.let {
            runCatching { GbIndex.load(JSONObject(it)) }
        }
        _status.value = s.loadAlias
        alias.load()
        _status.value = s.loadFp
        val n = store.fingerprints().size
        refreshDataInfo()
        _status.value = s.ready(n)
        if (_settings.value.autoUpdate) refreshData()
    }

    /**
     * 给搜索结果列表项找 L1 能力卡：按 (id, 国标码) 定位分片。
     * 命中就在 SupplierCard 里直接显示工艺位，采购扫一眼就知道这家能做什么。
     * **必须在 IO 线程调用**——首次会读 assets 分片。
     */
    fun capOf(h: Hit): CapabilityCard? =
        if (h.fp.id.isEmpty()) null else store.capabilityOf(h.fp.id, h.fp.gb)

    private suspend fun resolveCaps(hits: List<Hit>): Map<String, CapabilityCard> =
        withContext(Dispatchers.IO) {
            val m = HashMap<String, CapabilityCard>()
            for (h in hits) {
                val c = runCatching { capOf(h) }.getOrNull() ?: continue
                m[h.fp.id] = c
            }
            m
        }

    private fun refreshDataInfo() {
        val s = strings()
        _dataInfo.value = listOf(
            store.builtinFingerprintSummary(s),
            s.builtinAtLabel(store.builtinAt(s)),
            store.phoneIndexSummary(s),
            s.updatedShards(store.updatedShardCount()),
            s.lastCheck(store.lastUpdateCheck(s)),
            if (GbIndex.isLoaded()) GbIndex.summary(s) else s.gbIndexNotLoaded,
        ).joinToString("\n")
    }

    fun refreshData() {
        viewModelScope.launch {
            val s = strings()
            _status.value = s.checkingUpdate
            val r = remote.updateFingerprints(_settings.value.dataBase, s) { msg ->
                _status.value = msg
            }
            refreshDataInfo()
            if (r.ok) {
                _status.value = s.updateDone(r.message)
            } else {
                // 数据源不通不是 App 坏了——内置的离线库照样能检索。
                // 顶部只给一句人话，详细原因放设置页的 dataInfo 里，不吓人。
                _status.value = s.sourceDownN(store.fingerprints().size)
                _dataInfo.value = _dataInfo.value + "\n\n" + s.lastUpdateNote + "\n" + r.message
            }
        }
    }

    fun pingData(onDone: (String) -> Unit) {
        viewModelScope.launch {
            onDone(remote.ping(_settings.value.dataBase, strings()))
        }
    }

    fun updateSettings(s: AppSettings) {
        val langChanged = s.lang != _settings.value.lang
        val roleChanged = Role.of(s.role) != Role.of(_settings.value.role)
        _settings.value = s
        repo.save(s)
        // 数据信息卡是生成好的字符串，不跟着 recompose——切语言要手动重算一次
        if (langChanged) refreshDataInfo()
        if (roleChanged) switchRole(Role.of(s.role))
    }

    /**
     * 切身份 = **换一个人格 + 开一段新对话**。
     *
     * 为什么必须清空会话与凭证：
     *  - 买家侧和供应商侧的工具集、提示词、乃至"该不该出现供应商卡片"都不同，
     *    把上一段的上下文带过去，模型会拿采购的语气继续干供应商的事；
     *  - 认领凭证是短时凭据，切走就该作废，否则「认领到一半」的状态会跟着漂。
     *
     * 这里**只在真的换了身份时**才动，切语言不会清空对话。
     */
    private fun switchRole(r: Role) {
        vendorSession.reset()
        _messages.value = emptyList()
        // 清空后下一次提问又算「第一次搜索」，能力卡重新默认展开
        turnIndex = 0
        val s = Strings(Lang.of(_settings.value.lang))
        val label = if (r == Role.SUPPLIER) s.roleBadgeVendor else s.roleBadgeBuyer
        append(UiMessage(seq++, Sender.SYSTEM, s.roleSwitched(label)))
    }

    fun testLlm(onDone: (String) -> Unit) {
        val s = _settings.value
        viewModelScope.launch {
            val str = Strings(Lang.of(s.lang))
            onDone(LlmClient(LlmConfig(s.presetId, s.baseUrl, s.model, s.apiKey), str).test())
        }
    }

    fun applyPreset(preset: Preset) {
        val cur = _settings.value
        updateSettings(
            cur.copy(
                presetId = preset.id,
                baseUrl = preset.endpoint,
                model = preset.models.firstOrNull().orEmpty(),
            )
        )
    }

    fun clearChat() {
        _messages.value = emptyList()
        // 清空后下一次提问又算「第一次搜索」，能力卡重新默认展开
        turnIndex = 0
    }

    fun send(raw: String) {
        val text = raw.trim()
        if (text.isEmpty() || _busy.value) return
        viewModelScope.launch {
            _busy.value = true
            append(UiMessage(seq++, Sender.USER, text))
            val str = Strings(Lang.of(_settings.value.lang))
            val t0 = System.currentTimeMillis()
            log("turn start: $text")
            try {
                // 总闸：一轮对话（含最多 4 次模型调用 + 本地检索）超过上限就整体放弃。
                // 没有这道闸，任何一处挂起都会让 _busy 永久为 true——界面一直转圈、
                // Send 一直禁用，用户只能杀进程。宁可如实报错，也不能静默卡死。
                withTimeout(TURN_TIMEOUT_MS) { runTurn(text) }
            } catch (e: TimeoutCancellationException) {
                log("turn TIMEOUT after ${System.currentTimeMillis() - t0}ms")
                append(UiMessage(seq++, Sender.SYSTEM, str.turnTimeout))
            } finally {
                log("turn end: ${System.currentTimeMillis() - t0}ms, busy=false")
                _busy.value = false
            }
        }
    }

    private suspend fun runTurn(userText: String) {
        val s = _settings.value
        val lang = Lang.of(s.lang)
        val str = Strings(lang)
        val cfg = LlmConfig(s.presetId, s.baseUrl, s.model, s.apiKey)
        if (!cfg.ready()) {
            append(UiMessage(seq++, Sender.SYSTEM, str.noKey))
            return
        }

        val history = ArrayList<ChatMsg>()
        // 提示词按身份取：买家侧是检索助手，供应商侧是认领/采集助手，人格不同（见 systemPrompt）。
        // 供应商侧还要**每轮附上平台当前那一题**：模型会漏调提交工具，
        // 把"现在该问哪题、必须提交"写进系统提示词，是把它从可选项变成这一轮的作业。
        history.add(ChatMsg("system", systemPromptWithVendorState(role(), lang, str)))
        _messages.value.filter { it.sender != Sender.SYSTEM }.takeLast(8).forEach {
            history.add(ChatMsg(if (it.sender == Sender.USER) "user" else "assistant", it.text))
        }
        history.add(ChatMsg("user", userText))

        // **一次只加载一套工具**：供应商模式下买家那三个只读工具不存在，
        // 买家模式下认领/采集这些写工具更不存在。
        val set = toolSet()
        val tools = set.definitions()
        val autoOpen = turnIndex == 0
        turnIndex++

        var rounds = 0
        var answerId: Long? = null
        var hits: List<Hit> = emptyList()
        var detail: SupplierDetail? = null
        /** 本轮有没有发生过写操作。供应商侧用它判断"模型是不是说了却没提交"。 */
        var wrote = false
        // 一轮里最多 4 次模型调用，客户端只建一次（内部共用 OkHttpClient）
        val llm = LlmClient(cfg, str)

        while (rounds < 4) {
            rounds++
            if (answerId == null) {
                val id = seq++
                answerId = id
                append(UiMessage(id, Sender.ASSISTANT, "", streaming = true))
            }
            val t1 = System.currentTimeMillis()
            log("round $rounds: chat start")
            val result = llm.chat(history, tools) { piece ->
                bump(answerId!!, piece)
            }
            log(
                "round $rounds: chat done ${System.currentTimeMillis() - t1}ms, " +
                    "error=${result.error}, toolCalls=${result.toolCalls.size}"
            )
            if (result.error != null) {
                remove(answerId)
                append(UiMessage(seq++, Sender.SYSTEM, str.callFailed(result.error)))
                return
            }
            if (result.toolCalls.isEmpty()) {
                // 最终回答：卡片只在这里渲染一次
                val fb = hits.isEmpty()
                val finalHits = finalizeHits(userText, hits)
                val caps = resolveCaps(finalHits)
                setText(
                    answerId,
                    result.content.ifBlank { str.emptyAnswer },
                    finalHits, detail, caps, autoOpen,
                    fallback = fb && finalHits.isNotEmpty(),
                )
                warnIfNothingSubmitted(str, wrote)
                return
            }

            history.add(
                ChatMsg(
                    "assistant",
                    content = result.content.ifBlank { null },
                    toolCalls = result.toolCalls,
                )
            )
            remove(answerId)
            answerId = null

            for (call in result.toolCalls) {
                val t2 = System.currentTimeMillis()
                log("round $rounds: tool ${call.name} start args=${call.arguments.take(200)}")
                val tr = withContext(Dispatchers.IO) { set.run(call.name, call.arguments) }
                log(
                    "round $rounds: tool ${call.name} done " +
                        "${System.currentTimeMillis() - t2}ms, hits=${tr.hits.size}, failed=${tr.failed}" +
                        // 失败时把原因也打出来：只说 failed=true 等于没查（这一条是
                        // 「本地校验拦住、模型却猜成网络问题」那次加的）
                        if (tr.failed) " :: ${tr.text.take(200)}" else ""
                )
                hits = mergeHits(hits, tr.hits)
                tr.detail?.let { detail = it }
                if (!tr.failed && isWriteTool(call.name)) wrote = true
                // 回显只给一行人话：**不出现内部函数名**；失败标红，不伪装成成功
                append(
                    UiMessage(
                        seq++, Sender.SYSTEM, toolLabel(call.name, tr, str),
                        isTool = true, toolFailed = tr.failed,
                    )
                )
                history.add(ChatMsg("tool", content = tr.text, toolCallId = call.id))
            }
        }
        val fb = hits.isEmpty()
        val finalHits = finalizeHits(userText, hits)
        val caps = resolveCaps(finalHits)
        if (answerId != null) {
            setText(
                answerId, str.roundLimit, finalHits, detail, caps, autoOpen,
                fallback = fb && finalHits.isNotEmpty(),
            )
        } else {
            // 最后一轮是工具调用：answerId 已被置空，这里必须**新追加**一条。
            // 用 `answerId ?: seq++` 会去 update 一个不存在的 id，消息被静默丢掉。
            append(
                UiMessage(
                    seq++, Sender.SYSTEM, str.roundLimit,
                    hits = finalHits, detail = detail, caps = caps,
                    autoOpenCap = autoOpen, fallback = fb && finalHits.isNotEmpty(),
                )
            )
        }
        warnIfNothingSubmitted(str, wrote)
    }

    /**
     * 合并多轮工具命中，按 id 去重、取证据更强的一档。
     *
     * 之前是 `if (tr.hits.isNotEmpty()) anyHits = tr.hits`——最后一次调用直接覆盖前面的结果。
     * 于是「先 search 再 get_supplier_detail」时，8 家结果被 1 家档案顶掉，卡片时有时无。
     */
    private fun mergeHits(base: List<Hit>, add: List<Hit>): List<Hit> {
        if (add.isEmpty()) return base
        val m = LinkedHashMap<String, Hit>()
        base.forEach { m[it.fp.id] = it }
        add.forEach { h ->
            val prev = m[h.fp.id]
            if (prev == null || h.evidence.code < prev.evidence.code) m[h.fp.id] = h
        }
        return m.values.toList()
    }

    /**
     * 兜底：模型这一轮压根没调工具（DeepSeek 在 auto 模式下经常直接作答），
     * 用户就只看到一段文字、看不到任何灯牌。
     * 这里用原始提问在本地直检一次，能检到就附上——**检不到就不硬凑**。
     *
     * **供应商模式下禁用。** 供应商说的话是「我要认领我的企业」这类，
     * 拿它去做采购词检索会检出一堆不相干的厂，而卡片会被当成"系统给我的结果"——
     * 那比没有卡片更糟。供应商侧要出现企业卡片，只能来自他主动查自己的公司。
     */
    private suspend fun finalizeHits(userText: String, hits: List<Hit>): List<Hit> {
        if (hits.isNotEmpty()) return hits
        if (role() == Role.SUPPLIER) return hits
        val q = userText.trim()
        if (q.length < 2) return hits
        return withContext(Dispatchers.IO) {
            runCatching {
                engine.search(SearchParams(keyword = q, limit = 5)).hits
            }.getOrDefault(emptyList())
        }
    }

    /**
     * 系统提示词 = 身份人格 + （供应商侧）平台当前状态。
     *
     * 供应商侧**每轮都附状态**，不只在有当前题的时候附：模型会跳步骤
     * （实测跳过验证码直接调采集，被拒了也不重试），所以"认领走到哪一步了"
     * 同样得摆在它面前。见 [Strings.vendorStateHeader]。
     */
    private fun systemPromptWithVendorState(role: Role, lang: Lang, str: Strings): String {
        val base = systemPrompt(role, lang)
        if (role != Role.SUPPLIER) return base
        val sb = StringBuilder(base)
        sb.append("\n\n").append(str.vendorStateHeader)
        sb.append('\n').append(
            when {
                vendorSession.claimed -> str.vsClaimDone
                vendorSession.claimPending -> str.vsClaimPending
                else -> str.vsClaimNone
            }
        )
        val q = vendorSession.pending
        if (q == null) {
            sb.append('\n').append(str.vsCollectNone)
        } else {
            sb.append('\n').append(
                str.vendorTurnContext(q.index, q.total, q.path, q.question, q.hint, q.required)
            )
        }
        return sb.toString()
    }

    /** 会改变平台数据的工具。用于判断"这一轮到底提交没提交"。 */
    private fun isWriteTool(name: String): Boolean = name in setOf(
        "claim_start", "claim_verify", "collect_begin", "collect_answer", "collect_confirm",
    )

    /**
     * 供应商侧兜底：平台还等着答题，但这一轮一个写操作都没发生 → **如实说出来**。
     *
     * 为什么不能不管：模型（tool_choice=auto）会不调工具直接说「记下了」然后自己编下一题，
     * 用户以为答完了，平台那边一步没动——这是会丢数据的假成功。
     * 不去替它重试（重试可能把同一句话提交两遍），而是把状态如实摆出来让人再说一次。
     */
    private fun warnIfNothingSubmitted(str: Strings, wrote: Boolean) {
        if (role() != Role.SUPPLIER) return
        if (wrote || vendorSession.pending == null) return
        append(
            UiMessage(
                seq++, Sender.SYSTEM, str.vendorNothingSubmitted,
                isTool = true, toolFailed = true,
            )
        )
    }

    /**
     * 工具回显文案。**绝不能出现函数名**——那是内部实现，用户不需要知道。
     * 优先用工具自己给的 [ToolResult.echo]：有些工具的结果不是"命中了几家企业"
     * （相似度匹配、只给了相近候选），按 hits.size 猜会谎报成「没有匹配」。
     */
    private fun toolLabel(name: String, tr: ToolResult, s: Strings): String {
        // 失败时**如实说失败的原因**，不要贴"已发起认领"这种成功文案——
        // 红底 + 成功文案比不标红更误导（真的发生过一次：本地校验拦下了，
        // 回显写「已发起认领」，模型只好自己猜成"网络问题"）。
        if (tr.failed) return tr.text.lineSequence().firstOrNull().orEmpty().take(160)
        return tr.echo ?: when (name) {
            "search_suppliers" ->
                if (tr.hits.isEmpty()) s.toolSearchNone else s.toolSearch(tr.hits.size)
            "get_supplier_detail" -> s.toolDetail
            "list_categories" -> s.toolCats
            // ── 供应商侧 ──
            "find_my_company" ->
                if (tr.hits.isEmpty()) s.toolVendorMatchNone else s.toolVendorMatch(tr.hits.size)
            "claim_start" -> s.toolClaimStart
            "claim_verify" -> s.toolClaimCode
            "collect_begin" -> s.toolCollectBegin
            "collect_answer" -> s.toolCollectAnswer
            "collect_progress" -> s.toolCollectProgress
            "collect_confirm" -> s.toolCollectConfirm
            else -> if (role() == Role.SUPPLIER) s.toolVendorDone else s.toolDone
        }
    }

    // ── 消息列表的小工具 ────────────────────────────────────────────────────
    private fun append(m: UiMessage) {
        _messages.value = _messages.value + m
    }

    private fun remove(id: Long) {
        _messages.value = _messages.value.filterNot { it.id == id }
    }

    private fun bump(id: Long, piece: String) {
        _messages.value = _messages.value.map {
            if (it.id == id) it.copy(text = it.text + piece) else it
        }
    }

    private fun setText(
        id: Long,
        text: String,
        hits: List<Hit>,
        detail: SupplierDetail?,
        caps: Map<String, CapabilityCard>,
        autoOpenCap: Boolean,
        fallback: Boolean = false,
    ) {
        _messages.value = _messages.value.map {
            if (it.id == id) {
                it.copy(
                    text = text, hits = hits, detail = detail,
                    caps = caps, autoOpenCap = autoOpenCap, streaming = false,
                    fallback = fallback,
                )
            } else it
        }
    }

    /** 直接本地检索（不经过 LLM）。用于「设置」页的自检，以及没配 key 时的兜底。 */
    fun localSearch(keyword: String, city: String? = null): String {
        val s = strings()
        val out = engine.search(SearchParams(keyword = keyword, city = city, limit = 5))
        if (out.hits.isEmpty()) {
            return if (s.lang == Lang.EN) {
                "0 local hits" + (if (out.relaxed > 0) " (relaxing would give ${out.relaxed} industry inferences)" else "")
            } else {
                "本地检索 0 家" + (if (out.relaxed > 0) "（放宽可得 ${out.relaxed} 家行业推断）" else "")
            }
        }
        return out.hits.joinToString("\n") { engine.toBrief(it, s) }
    }

    fun aliasExplain(word: String): String = alias.explain(word)
    fun aliasSize(): Int = alias.size()
}
