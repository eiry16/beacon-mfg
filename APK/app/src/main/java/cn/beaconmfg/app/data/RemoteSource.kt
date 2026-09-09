package cn.beaconmfg.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * 联网数据：指纹增量更新 + 完整档案按需下载。
 *
 * 设计前提：**内置数据已经够用**，联网只是让它更新。所以任何一步失败
 * 都必须「保持现状 + 明确告知」，绝不能把失败的更新写成空文件——
 * 那等于把能用的离线库搞坏。
 */
class RemoteSource(private val store: DataStore) {
    data class UpdateResult(
        val ok: Boolean,
        val checked: Int = 0,
        val downloaded: Int = 0,
        val failed: Int = 0,
        val bytes: Long = 0,
        val message: String = "",
    )

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun now(): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA).format(Date())

    /**
     * 按 manifest 增量更新指纹分片。
     *
     * 只比对 SHA1：内置分片的 SHA1 写在 index/builtin.json 里（由 sync_assets.py 生成），
     * 已更新分片的 SHA1 记在 updates/meta.json。两边都对不上才下载。
     */
    suspend fun updateFingerprints(
        base: String,
        onProgress: (String) -> Unit = {},
    ): UpdateResult = withContext(Dispatchers.IO) {
        val root = if (base.endsWith("/")) base else "$base/"
        try {
            val req = Request.Builder()
                .url("${root}data/manifest.json")
                .apply { store.manifestEtag()?.let { header("If-None-Match", it) } }
                .build()
            http.newCall(req).execute().use { resp ->
                when (resp.code) {
                    304 -> {
                        store.setLastUpdateCheck(now())
                        return@withContext UpdateResult(
                            true, message = "manifest 未变更，无需下载（HTTP 304）"
                        )
                    }

                    else -> if (!resp.isSuccessful) {
                        return@withContext UpdateResult(false, message = "manifest 拉取失败：HTTP ${resp.code}")
                    }
                }
                store.setManifestEtag(resp.header("ETag"))
                val body = resp.body?.string() ?: return@withContext UpdateResult(
                    false, message = "manifest 响应为空"
                )
                val shards = JSONObject(body).optJSONArray("shards")
                    ?: return@withContext UpdateResult(false, message = "manifest 格式异常：无 shards")

                val fps = ArrayList<Triple<String, String, String>>() // rel, url, sha1
                for (i in 0 until shards.length()) {
                    val s = shards.optJSONObject(i) ?: continue
                    if (s.safeString("t") != "fp") continue
                    val p = s.safeString("p")
                    val h = s.safeString("h")
                    if (p.isEmpty() || h.isEmpty()) continue
                    val rel = p.removePrefix("skills/registry/fingerprint/")
                    val local = store.localShaOf(rel) ?: store.builtinShaOf(rel)
                    if (local == h) continue
                    fps.add(Triple(rel, root + p, h))
                }

                var downloaded = 0
                var failed = 0
                var bytes = 0L
                fps.forEachIndexed { idx, (rel, url, sha) ->
                    onProgress("下载分片 ${idx + 1}/${fps.size}：$rel")
                    try {
                        val r = Request.Builder().url(url).build()
                        http.newCall(r).execute().use { rr ->
                            if (!rr.isSuccessful) {
                                failed++
                                return@use
                            }
                            val content = rr.body?.string() ?: run { failed++; return@use }
                            if (store.writeShard(rel, content, sha)) {
                                downloaded++
                                bytes += content.length
                            } else {
                                failed++   // SHA1 校验不过：宁可留旧数据
                            }
                        }
                    } catch (e: Exception) {
                        failed++
                    }
                }
                store.setLastUpdateCheck(now())
                UpdateResult(
                    true, checked = fps.size, downloaded = downloaded,
                    failed = failed, bytes = bytes,
                    message = if (downloaded == 0 && failed == 0) "已是最新"
                    else "更新 $downloaded 片（${bytes / 1024} KB）" +
                        if (failed > 0) "，失败 $failed 片" else ""
                )
            }
        } catch (e: Exception) {
            UpdateResult(false, message = "更新失败：${e.message ?: e.javaClass.simpleName}")
        }
    }

    /** 完整档案：按国标码定位 data/gb/ 下的小类分片，下载后缓存。 */
    suspend fun fetchDetail(base: String, code: String, id: String): SupplierDetail? =
        withContext(Dispatchers.IO) {
            val cached = store.cachedDetailContent(code)
            val content = cached ?: run {
                val root = if (base.endsWith("/")) base else "$base/"
                val path = zhPathOf(code) ?: return@withContext null
                try {
                    val req = Request.Builder().url(root + path).build()
                    http.newCall(req).execute().use { resp ->
                        if (!resp.isSuccessful) return@withContext null
                        val body = resp.body?.string() ?: return@withContext null
                        store.cacheDetail(code, body)
                        body
                    }
                } catch (e: Exception) {
                    null
                }
            } ?: return@withContext null
            parseDetail(content, id)
        }

    /** 国标码 → 中文归档分片路径（从内置 manifest 里查，不硬编码目录结构）。 */
    private fun zhPathOf(code: String): String? {
        val manifest = store.readAsset("index/manifest.json") ?: return null
        val shards = JSONObject(manifest).optJSONArray("shards") ?: return null
        for (i in 0 until shards.length()) {
            val s = shards.optJSONObject(i) ?: continue
            if (s.safeString("t") == "zh" && s.safeString("c") == code) return s.safeString("p")
        }
        return null
    }

    private fun parseDetail(json: String, id: String): SupplierDetail? {
        val arr = org.json.JSONArray(json)
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.safeString("id") != id) continue
            val region = o.optJSONObject("region")
            val ind = o.optJSONObject("industry")
            val certs = ArrayList<String>()
            val ca = o.optJSONArray("certifications")
            if (ca != null) {
                for (j in 0 until ca.length()) {
                    val c = ca.opt(j)
                    val name = if (c is JSONObject) c.safeString("name") else c?.toString()
                    if (!name.isNullOrEmpty()) certs.add(name)
                }
            }
            val kws = ArrayList<String>()
            val ka = o.optJSONArray("keywords")
            if (ka != null) for (j in 0 until ka.length()) kws.add(ka.optString(j, ""))
            return SupplierDetail(
                id = id,
                company = o.safeString("company"),
                city = region?.safeString("city") ?: "",
                province = region?.safeString("province") ?: "",
                address = o.safeString("address"),
                phone = o.safeString("contact_phone"),
                website = o.safeString("website"),
                keywords = kws,
                gb = ind?.safeString("code") ?: "",
                gbName = ind?.safeString("name") ?: "",
                gbPath = ind?.safeString("path") ?: "",
                certs = certs,
                status = o.safeString("status"),
                isManufacturer = o.optBoolean("is_manufacturer", true),
                verifiedAt = o.safeString("verified_at"),
            )
        }
        return null
    }

    /** 连通性自检：只拉 manifest 的头部，用来告诉用户「数据源通不通」。 */
    suspend fun ping(base: String): String = withContext(Dispatchers.IO) {
        val root = if (base.endsWith("/")) base else "$base/"
        try {
            val req = Request.Builder().url("${root}data/manifest.json").head().build()
            http.newCall(req).execute().use { "数据源可达（HTTP ${it.code}）" }
        } catch (e: Exception) {
            "数据源不可达：${e.message ?: e.javaClass.simpleName}"
        }
    }
}
