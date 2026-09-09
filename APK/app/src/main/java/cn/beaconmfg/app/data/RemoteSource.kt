package cn.beaconmfg.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.URI
import java.net.UnknownHostException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit
import cn.beaconmfg.app.i18n.Strings
import javax.net.ssl.SSLException

/**
 * 联网数据：指纹增量更新 + 完整档案按需下载。
 *
 * 设计前提：**内置数据已经够用**，联网只是让它更新。所以任何一步失败
 * 都必须「保持现状 + 明确告知」，绝不能把失败的更新写成空文件——
 * 那等于把能用的离线库搞坏。
 *
 * 另一个现实前提：主源 raw.githubusercontent.com 在国内经常连不上。
 * 所以所有联网动作都走「候选源依次尝试」，第一个能连通的就用，
 * 全挂才如实报错——不让用户因为一个 CDN 抽风就以为 App 坏了。
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

    companion object {
        /**
         * 内置候选源，按国内实测可达性排序（2026-09 实测：fastly 0.8s / cdn 2.7s / raw 8s+）。
         * 用户在设置里填的源永远排在最前。
         */
        val MIRRORS = listOf(
            "https://fastly.jsdelivr.net/gh/eiry16/beacon-mfg@main/",
            "https://cdn.jsdelivr.net/gh/eiry16/beacon-mfg@main/",
            "https://raw.githubusercontent.com/eiry16/beacon-mfg/main/",
        )
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)   // 5s 连不上就换源，别让用户干等
        .readTimeout(20, TimeUnit.SECONDS)
        .callTimeout(60, TimeUnit.SECONDS)
        .build()

    private fun now(): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA).format(Date())

    /** 候选源：用户填的优先，其后是内置镜像，去重。 */
    private fun candidates(base: String): List<String> {
        val root = if (base.endsWith("/")) base else "$base/"
        return (listOf(root) + MIRRORS).distinct()
    }

    private fun hostOf(root: String): String = runCatching { URI(root).host ?: root }.getOrDefault(root)

    private fun friendly(e: Throwable, s: Strings): String = when (e) {
        is SocketTimeoutException -> s.connTimeout
        is UnknownHostException -> s.dnsFail
        is ConnectException -> s.connRefused
        is SSLException -> s.tlsFail
        else -> e.message?.take(60)?.ifEmpty { null } ?: e.javaClass.simpleName
    }

    /**
     * 按 manifest 增量更新指纹分片。逐个候选源尝试，第一个能连通的就用。
     *
     * 只比对 SHA1：内置分片的 SHA1 写在 index/builtin.json 里（由 sync_assets.py 生成），
     * 已更新分片的 SHA1 记在 updates/meta.json。两边都对不上才下载。
     */
    suspend fun updateFingerprints(
        base: String,
        s: Strings,
        onProgress: (String) -> Unit = {},
    ): UpdateResult = withContext(Dispatchers.IO) {
        val roots = candidates(base)
        val errors = ArrayList<String>()

        roots.forEachIndexed { i, root ->
            if (i > 0) onProgress(s.switchMirror(hostOf(root)))
            val r = runCatching { trySource(root, s, onProgress) }
                .getOrElse { e ->
                    errors += "${hostOf(root)}（${friendly(e, s)}）"
                    null
                }
            if (r != null) return@withContext r
        }

        UpdateResult(false, message = s.allSourcesDown(roots.size, errors.joinToString("、")))
    }

    /**
     * 单个源的完整更新流程。
     * 抛异常 = 这个源不通（换下一个）；正常返回 = 源可用（含 HTTP 304 无需更新）。
     */
    private suspend fun trySource(
        root: String,
        st: Strings,
        onProgress: (String) -> Unit,
    ): UpdateResult {
        val req = Request.Builder()
            .url("${root}data/manifest.json")
            .apply { store.manifestEtag()?.let { header("If-None-Match", it) } }
            .build()

        http.newCall(req).execute().use { resp ->
            if (resp.code == 304) {
                store.setLastUpdateCheck(now())
                return UpdateResult(true, message = st.manifestUnchanged)
            }
            if (!resp.isSuccessful) throw IOException("manifest HTTP ${resp.code}")

            store.setManifestEtag(resp.header("ETag"))
            val body = resp.body?.string() ?: throw IOException("manifest 响应为空")
            val shards = JSONObject(body).optJSONArray("shards")
                ?: throw IOException("manifest 格式异常：无 shards")

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
                onProgress(st.downloadingShard(idx + 1, fps.size, rel))
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
            // 号码索引（t=phone）：不按国标归档，是横跨全库的 id→phone 映射，单独一项。
            // 它失败不该连坐整个更新——最坏只是新增企业没号码，不能因此判更新失败。
            runCatching {
                for (i in 0 until shards.length()) {
                    val s = shards.optJSONObject(i) ?: continue
                    if (s.safeString("t") != "phone") continue
                    val sha = s.safeString("h")
                    if (sha.isEmpty() || sha == store.localPhoneSha()) break
                    onProgress(st.updatingPhoneIndex)
                    val pr = Request.Builder().url(root + s.safeString("p")).build()
                    http.newCall(pr).execute().use { rr ->
                        if (!rr.isSuccessful) return@use
                        val body = rr.body?.string() ?: return@use
                        store.writePhoneIndex(body, sha)
                    }
                    break
                }
            }

            store.setLastUpdateCheck(now())
            return UpdateResult(
                true, checked = fps.size, downloaded = downloaded,
                failed = failed, bytes = bytes,
                message = if (downloaded == 0 && failed == 0) st.alreadyLatest(hostOf(root))
                else st.updatedShardsMsg(downloaded, bytes / 1024) +
                    if (failed > 0) st.updatedShardsFailed(failed) else ""
            )
        }
    }

    /** 完整档案：按国标码定位 data/gb/ 下的小类分片，下载后缓存。同样走候选源。 */
    suspend fun fetchDetail(base: String, code: String, id: String): SupplierDetail? =
        withContext(Dispatchers.IO) {
            val cached = store.cachedDetailContent(code)
            val content = cached ?: run {
                val path = zhPathOf(code) ?: return@withContext null
                candidates(base).firstNotNullOfOrNull { root ->
                    try {
                        val req = Request.Builder().url(root + path).build()
                        http.newCall(req).execute().use { resp ->
                            if (!resp.isSuccessful) return@use null
                            val body = resp.body?.string() ?: return@use null
                            store.cacheDetail(code, body)
                            body
                        }
                    } catch (e: Exception) {
                        null
                    }
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
            // 能力卡读的是内置副本，不联网也有。断网时详情页照样能看到工艺位。
            val gbCode = ind?.safeString("code") ?: ""
            return SupplierDetail(
                id = id,
                company = o.safeString("company"),
                city = region?.safeString("city") ?: "",
                province = region?.safeString("province") ?: "",
                address = o.safeString("address"),
                phone = o.safeString("contact_phone"),
                website = o.safeString("website"),
                keywords = kws,
                gb = gbCode,
                gbName = ind?.safeString("name") ?: "",
                gbPath = ind?.safeString("path") ?: "",
                certs = certs,
                status = o.safeString("status"),
                isManufacturer = o.optBoolean("is_manufacturer", true),
                verifiedAt = o.safeString("verified_at"),
                cap = store.capabilityOf(id, gbCode),
            )
        }
        return null
    }

    /** 连通性自检：逐个候选源打一次 HEAD，让用户看到哪个源能用。 */
    suspend fun ping(base: String, s: Strings): String = withContext(Dispatchers.IO) {
        candidates(base).joinToString("\n") { root ->
            val t0 = System.currentTimeMillis()
            try {
                val req = Request.Builder().url("${root}data/manifest.json").head().build()
                http.newCall(req).execute().use {
                    s.pingOk(hostOf(root), it.code, System.currentTimeMillis() - t0)
                }
            } catch (e: Exception) {
                s.pingFail(hostOf(root), friendly(e, s))
            }
        }
    }
}
