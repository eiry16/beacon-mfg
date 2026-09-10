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
import cn.beaconmfg.app.data.RemoteSource
import cn.beaconmfg.app.data.SearchParams
import cn.beaconmfg.app.data.SettingsRepo
import cn.beaconmfg.app.data.SupplierDetail
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Strings
import cn.beaconmfg.app.i18n.systemPrompt
import cn.beaconmfg.app.llm.ChatMsg
import cn.beaconmfg.app.llm.LlmClient
import cn.beaconmfg.app.llm.LlmConfig
import cn.beaconmfg.app.llm.Preset
import cn.beaconmfg.app.llm.ToolBox
import cn.beaconmfg.app.llm.ToolResult
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

    enum class Role { USER, ASSISTANT, SYSTEM }

    data class UiMessage(
        val id: Long,
        val role: Role,
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
    )

    private val store = DataStore(app)
    private val alias = AliasIndex(store)
    private val engine = SearchEngine(store, alias)
    private val remote = RemoteSource(store)
    private val repo = SettingsRepo(app)
    private val toolBox = ToolBox(
        engine, store, remote,
        dataBase = { _settings.value.dataBase },
        lang = { Lang.of(_settings.value.lang) },
    )

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
        _settings.value = s
        repo.save(s)
        // 数据信息卡是生成好的字符串，不跟着 recompose——切语言要手动重算一次
        if (langChanged) refreshDataInfo()
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
            append(UiMessage(seq++, Role.USER, text))
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
                append(UiMessage(seq++, Role.SYSTEM, str.turnTimeout))
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
            append(UiMessage(seq++, Role.SYSTEM, str.noKey))
            return
        }

        val history = ArrayList<ChatMsg>()
        history.add(ChatMsg("system", systemPrompt(lang)))
        _messages.value.filter { it.role != Role.SYSTEM }.takeLast(8).forEach {
            history.add(ChatMsg(if (it.role == Role.USER) "user" else "assistant", it.text))
        }
        history.add(ChatMsg("user", userText))

        val tools = toolBox.definitions()
        val autoOpen = turnIndex == 0
        turnIndex++

        var rounds = 0
        var answerId: Long? = null
        var hits: List<Hit> = emptyList()
        var detail: SupplierDetail? = null
        // 一轮里最多 4 次模型调用，客户端只建一次（内部共用 OkHttpClient）
        val llm = LlmClient(cfg, str)

        while (rounds < 4) {
            rounds++
            if (answerId == null) {
                val id = seq++
                answerId = id
                append(UiMessage(id, Role.ASSISTANT, "", streaming = true))
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
                append(UiMessage(seq++, Role.SYSTEM, str.callFailed(result.error)))
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
                log("round $rounds: tool ${call.name} start")
                val tr = withContext(Dispatchers.IO) { toolBox.run(call.name, call.arguments) }
                log(
                    "round $rounds: tool ${call.name} done " +
                        "${System.currentTimeMillis() - t2}ms, hits=${tr.hits.size}"
                )
                hits = mergeHits(hits, tr.hits)
                tr.detail?.let { detail = it }
                // 回显只给一行人话：**不出现内部函数名**
                append(UiMessage(seq++, Role.SYSTEM, toolLabel(call.name, tr, str), isTool = true))
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
                    seq++, Role.SYSTEM, str.roundLimit,
                    hits = finalHits, detail = detail, caps = caps,
                    autoOpenCap = autoOpen, fallback = fb && finalHits.isNotEmpty(),
                )
            )
        }
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
     */
    private suspend fun finalizeHits(userText: String, hits: List<Hit>): List<Hit> {
        if (hits.isNotEmpty()) return hits
        val q = userText.trim()
        if (q.length < 2) return hits
        return withContext(Dispatchers.IO) {
            runCatching {
                engine.search(SearchParams(keyword = q, limit = 5)).hits
            }.getOrDefault(emptyList())
        }
    }

    /** 工具回显文案。**绝不能出现函数名**——那是内部实现，用户不需要知道。 */
    private fun toolLabel(name: String, tr: ToolResult, s: Strings): String = when (name) {
        "search_suppliers" ->
            if (tr.hits.isEmpty()) s.toolSearchNone else s.toolSearch(tr.hits.size)
        "get_supplier_detail" -> s.toolDetail
        "list_categories" -> s.toolCats
        else -> s.toolDone
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
