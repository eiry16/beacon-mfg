package cn.beaconmfg.app

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import cn.beaconmfg.app.data.AppSettings
import cn.beaconmfg.app.data.DataStore
import cn.beaconmfg.app.data.GbIndex
import cn.beaconmfg.app.data.CapabilityCard
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.RemoteSource
import cn.beaconmfg.app.data.SettingsRepo
import cn.beaconmfg.app.data.SupplierDetail
import cn.beaconmfg.app.llm.ChatMsg
import cn.beaconmfg.app.llm.LlmClient
import cn.beaconmfg.app.llm.LlmConfig
import cn.beaconmfg.app.llm.Preset
import cn.beaconmfg.app.llm.ToolBox
import cn.beaconmfg.app.search.AliasIndex
import cn.beaconmfg.app.search.SearchEngine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * 对话 + 数据装载 + 设置的总控。
 *
 * 数据红线在这里执行：**供应商数据只来自本地检索**，LLM 只负责理解需求与组织语言。
 * 回灌给模型的结果一律带证据档位，系统提示词里也写死禁止编造。
 */
class MainViewModel(app: Application) : AndroidViewModel(app) {

    enum class Role { USER, ASSISTANT, SYSTEM }

    data class UiMessage(
        val id: Long,
        val role: Role,
        val text: String,
        val hits: List<Hit> = emptyList(),
        val detail: SupplierDetail? = null,
        val streaming: Boolean = false,
    )

    private val store = DataStore(app)
    private val alias = AliasIndex(store)
    private val engine = SearchEngine(store, alias)
    private val remote = RemoteSource(store)
    private val repo = SettingsRepo(app)
    private val toolBox = ToolBox(engine, store, remote) { _settings.value.dataBase }

    private var seq = 0L

    private val _messages = MutableStateFlow<List<UiMessage>>(emptyList())
    val messages: StateFlow<List<UiMessage>> = _messages.asStateFlow()

    private val _status = MutableStateFlow("正在装载本地索引…")
    val status: StateFlow<String> = _status.asStateFlow()

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    private val _settings = MutableStateFlow(repo.load())
    val settings: StateFlow<AppSettings> = _settings.asStateFlow()

    private val _dataInfo = MutableStateFlow("")
    val dataInfo: StateFlow<String> = _dataInfo.asStateFlow()

    val secureStorageAvailable: Boolean = repo.secureStorageAvailable

    companion object {
        const val SYSTEM_PROMPT = """
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
5. 回答用中文，简洁。默认只推荐 3–5 家，给出公司名、城市、主营、证据档位；用户要看更多再补充。
6. 搜索结果已带电话号码，可直接引用；完整地址/官网需调用 get_supplier_detail 获取。
   结果里写「号码待核实」的，就如实告诉用户号码待核实，**不要编造或猜测电话号码**。
"""
    }

    init {
        viewModelScope.launch {
            boot()
        }
    }

    private suspend fun boot() = withContext(Dispatchers.IO) {
        _status.value = "装载国标索引…"
        store.readAsset("index/gb-index.json")?.let {
            runCatching { GbIndex.load(JSONObject(it)) }
        }
        _status.value = "装载采购词别名表…"
        alias.load()
        _status.value = "装载全量指纹…"
        val n = store.fingerprints().size
        refreshDataInfo()
        _status.value = "就绪：本地 $n 家供应商，可直接离线检索"
        if (_settings.value.autoUpdate) refreshData()
    }

    /**
     * 给搜索结果列表项找 L1 能力卡：按 (id, 国标码) 定位分片。
     * 命中就在 SupplierCard 里直接显示工艺 chips，采购扫一眼就知道这家能做什么，
     * 不用再让模型调一次 get_supplier_detail。
     */
    fun capOf(h: Hit): CapabilityCard? =
        if (h.fp.id.isEmpty()) null else store.capabilityOf(h.fp.id, h.fp.gb)

    private fun refreshDataInfo() {
        _dataInfo.value = listOf(
            store.builtinFingerprintSummary(),
            "内置于 ${store.builtinAt()}",
            store.phoneIndexSummary(),
            "已更新分片 ${store.updatedShardCount()} 个",
            "上次检查 ${store.lastUpdateCheck()}",
            if (GbIndex.isLoaded()) GbIndex.summary() else "国标索引未加载",
        ).joinToString("\n")
    }

    fun refreshData() {
        viewModelScope.launch {
            _status.value = "检查数据更新…"
            val r = remote.updateFingerprints(_settings.value.dataBase) { msg ->
                _status.value = msg
            }
            refreshDataInfo()
            if (r.ok) {
                _status.value = "数据更新完成：${r.message}"
            } else {
                // 数据源不通不是 App 坏了——内置的离线库照样能检索。
                // 顶部只给一句人话，详细原因放设置页的 dataInfo 里，不吓人。
                _status.value = "数据源暂不可达，内置 ${store.fingerprints().size} 家可离线检索"
                _dataInfo.value = _dataInfo.value + "\n\n【最近一次更新】\n" + r.message
            }
        }
    }

    fun pingData(onDone: (String) -> Unit) {
        viewModelScope.launch {
            onDone(remote.ping(_settings.value.dataBase))
        }
    }

    fun updateSettings(s: AppSettings) {
        _settings.value = s
        repo.save(s)
    }

    fun testLlm(onDone: (String) -> Unit) {
        val s = _settings.value
        viewModelScope.launch {
            onDone(LlmClient(LlmConfig(s.presetId, s.baseUrl, s.model, s.apiKey)).test())
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
    }

    fun send(raw: String) {
        val text = raw.trim()
        if (text.isEmpty() || _busy.value) return
        viewModelScope.launch {
            _busy.value = true
            append(UiMessage(seq++, Role.USER, text))
            try {
                runTurn(text)
            } finally {
                _busy.value = false
            }
        }
    }

    private suspend fun runTurn(userText: String) {
        val s = _settings.value
        val cfg = LlmConfig(s.presetId, s.baseUrl, s.model, s.apiKey)
        if (!cfg.ready()) {
            append(
                UiMessage(
                    seq++, Role.SYSTEM,
                    "还没配置 API key。到「设置」里选服务商、填自己的 key 就能用——" +
                        "key 只存在本机（Keystore 加密），不会发给除你所选端点以外的任何地方。"
                )
            )
            return
        }

        val history = ArrayList<ChatMsg>()
        history.add(ChatMsg("system", SYSTEM_PROMPT))
        _messages.value.filter { it.role != Role.SYSTEM }.takeLast(8).forEach {
            history.add(ChatMsg(if (it.role == Role.USER) "user" else "assistant", it.text))
        }
        history.add(ChatMsg("user", userText))

        val tools = toolBox.definitions()
        var rounds = 0
        var answerId: Long? = null
        var anyHits: List<Hit> = emptyList()
        var anyDetail: SupplierDetail? = null

        while (rounds < 4) {
            rounds++
            if (answerId == null) {
                val id = seq++
                answerId = id
                append(UiMessage(id, Role.ASSISTANT, "", streaming = true))
            }
            val result = LlmClient(cfg).chat(history, tools) { piece ->
                bump(answerId!!, piece)
            }
            if (result.error != null) {
                remove(answerId)
                append(UiMessage(seq++, Role.SYSTEM, "模型调用失败：${result.error}"))
                return
            }
            if (result.toolCalls.isEmpty()) {
                // 最终回答
                setText(answerId, result.content.ifBlank { "（模型没有返回内容）" }, anyHits, anyDetail)
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
                val tr = withContext(Dispatchers.IO) { toolBox.run(call.name, call.arguments) }
                if (tr.hits.isNotEmpty()) anyHits = tr.hits
                tr.detail?.let { anyDetail = it }
                append(
                    UiMessage(
                        seq++, Role.SYSTEM,
                        "调用 ${call.name}：" + tr.text.lineSequence().first(),
                        hits = tr.hits,
                        detail = tr.detail,
                    )
                )
                history.add(ChatMsg("tool", content = tr.text, toolCallId = call.id))
            }
        }
        append(UiMessage(seq++, Role.SYSTEM, "工具调用轮次已达上限，已停止。可以换个更具体的说法再问。"))
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

    private fun setText(id: Long, text: String, hits: List<Hit>, detail: SupplierDetail?) {
        _messages.value = _messages.value.map {
            if (it.id == id) it.copy(text = text, hits = hits, detail = detail, streaming = false)
            else it
        }
    }

    /** 直接本地检索（不经过 LLM）。用于「设置」页的自检，以及没配 key 时的兜底。 */
    fun localSearch(keyword: String, city: String? = null): String {
        val out = engine.search(
            cn.beaconmfg.app.data.SearchParams(keyword = keyword, city = city, limit = 5)
        )
        if (out.hits.isEmpty()) return "本地检索 0 家${if (out.relaxed > 0) "（放宽可得 ${out.relaxed} 家行业推断）" else ""}"
        return out.hits.joinToString("\n") { engine.toBrief(it) }
    }

    fun aliasExplain(word: String): String = alias.explain(word)
    fun aliasSize(): Int = alias.size()
}
