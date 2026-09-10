package cn.beaconmfg.app.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

data class AppSettings(
    val presetId: String = "deepseek",
    val baseUrl: String = "https://api.deepseek.com/v1",
    val model: String = "deepseek-chat",
    val apiKey: String = "",
    /** 数据源根地址（manifest / 分片都挂在这个前缀下），可换成镜像或自建 CDN。
     *  默认走 fastly 的 jsDelivr：国内实测 0.8s，比 raw.githubusercontent（8s+，常超时）稳。
     *  即便这个源也挂了，RemoteSource 会自动回退到内置的其他镜像。 */
    val dataBase: String = "https://fastly.jsdelivr.net/gh/eiry16/beacon-mfg@main/",
    /**
     * L1 能力卡 / L2 厂商 skill 的托管根地址（Cloudflare Pages）。
     *
     * **为什么不能复用 dataBase**：这两个地址上的内容是两拨东西——
     *   - `dataBase`（jsDelivr → GitHub）：L0 指纹分片、`data/gb/` 完整档案、manifest
     *   - `capabilityBase`（Pages）：`full/`、`slim/`、`skills/vendors/{id}/SKILL.md`
     *
     * 后两者**有意不进 Git**（L2 是厂商自述，半私有；分片是构建产物），
     * 所以 jsDelivr 上根本不存在，反过来 Pages 上也没有 L0 指纹。
     * 混用 = 一边全 404。谁改成一个，另一边立刻静默失效。
     */
    val capabilityBase: String = "https://beacon-mfg.pages.dev/",
    val autoUpdate: Boolean = true,
    /**
     * 界面/对话语言。存 code 字符串（"zh"/"en"，见 i18n.Lang），不存序号。
     * 切这个会同时换：UI 文案、给模型的系统提示词、工具回显与回灌给模型的标签。
     * 不变的是数据本身——公司名/地址/工艺名来自企业公开资料，翻译即编造。
     */
    val lang: String = "zh",
    /**
     * 当前身份：`buyer`（找供应商）/ `supplier`（认领自家企业）。
     *
     * **为什么要有这一项**：买家侧只有只读工具，供应商侧有写工具（认领、采集）。
     * 混在一套里，采购对话中误触发写入会**污染数据**——所以两套工具 + 两套提示词，
     * 一次只加载一套。存 code 不存序号，同 [lang]。
     */
    val role: String = "buyer",
    /**
     * 平台服务端根地址（供应商侧用）。**留空是合法状态**——
     * 留空时供应商侧功能显示为「未开通」，而不是静默失败。
     *
     * 为什么与 [dataBase]/[capabilityBase] 分开：那两个是**只读静态托管**（Edge/CDN），
     * 这个是**有写操作的后端**。合在一起会让"换 CDN 镜像"顺手把写通道也换掉。
     *
     * 开发时填 `http://127.0.0.1:8000` + `adb reverse tcp:8000 tcp:8000` 即可真机联调。
     */
    val apiBase: String = "",
)

/**
 * 设置持久化。API key 走 EncryptedSharedPreferences：
 * 值用 AES-256-GCM 加密，主密钥存在 Android Keystore 里，root 之外的手段取不出来。
 *
 * Keystore 在个别机型上不可用（尤其是被刷过机 / 无硬件-backed 的老设备）。
 * 这时**降级到普通 SharedPreferences 并如实标记**，让用户知道自己的 key 是明文存的，
 * 由用户决定要不要继续用——而不是静默降级假装很安全。
 */
class SettingsRepo(context: Context) {

    private val plain: SharedPreferences =
        context.getSharedPreferences("bmfg_settings_plain", Context.MODE_PRIVATE)

    private val secure: SharedPreferences? = try {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "bmfg_settings_secure",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (e: Exception) {
        null
    }

    val secureStorageAvailable: Boolean get() = secure != null

    fun load(): AppSettings {
        val p = secure ?: plain
        return AppSettings(
            presetId = p.getString("preset_id", "deepseek") ?: "deepseek",
            baseUrl = p.getString("base_url", "https://api.deepseek.com/v1")
                ?: "https://api.deepseek.com/v1",
            model = p.getString("model", "deepseek-chat") ?: "deepseek-chat",
            apiKey = p.getString("api_key", "") ?: "",
            dataBase = p.getString("data_base", AppSettings().dataBase) ?: AppSettings().dataBase,
            capabilityBase = p.getString("capability_base", AppSettings().capabilityBase)
                ?: AppSettings().capabilityBase,
            autoUpdate = p.getBoolean("auto_update", true),
            lang = p.getString("lang", "zh") ?: "zh",
            role = p.getString("role", "buyer") ?: "buyer",
            apiBase = p.getString("api_base", "") ?: "",
        )
    }

    fun save(s: AppSettings) {
        (secure ?: plain).edit()
            .putString("preset_id", s.presetId)
            .putString("base_url", s.baseUrl.trim())
            .putString("model", s.model.trim())
            .putString("api_key", s.apiKey.trim())
            .putString("data_base", s.dataBase.trim())
            .putString("capability_base", s.capabilityBase.trim())
            .putBoolean("auto_update", s.autoUpdate)
            .putString("lang", s.lang)
            .putString("role", s.role)
            .putString("api_base", s.apiBase.trim())
            .apply()
    }

    fun hasKey(): Boolean = (secure ?: plain).getString("api_key", "").orEmpty().isNotBlank()
}
