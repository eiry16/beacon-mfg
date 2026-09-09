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
    val autoUpdate: Boolean = true,
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
            autoUpdate = p.getBoolean("auto_update", true),
        )
    }

    fun save(s: AppSettings) {
        (secure ?: plain).edit()
            .putString("preset_id", s.presetId)
            .putString("base_url", s.baseUrl.trim())
            .putString("model", s.model.trim())
            .putString("api_key", s.apiKey.trim())
            .putString("data_base", s.dataBase.trim())
            .putBoolean("auto_update", s.autoUpdate)
            .apply()
    }

    fun hasKey(): Boolean = (secure ?: plain).getString("api_key", "").orEmpty().isNotBlank()
}
