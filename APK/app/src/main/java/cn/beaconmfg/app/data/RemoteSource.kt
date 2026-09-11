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

    /** 今天（ISO yyyy-MM-dd），给认证有效期判断用。 */
    private fun todayIso(): String =
        SimpleDateFormat("yyyy-MM-dd", Locale.CHINA).format(Date())

    /**
     * 解析认证存证块（data/gb 完整档案里的 `certification`）。
     *
     * 多数企业没有这块 —— 返回 null，UI 里就当作「未认证」处理。
     * completeness（硬指标完成度）缺失时留 null：**没算出来就不显示**，不填 0，
     * 显示 0% 会让人以为这家厂硬指标一项没过关。
     */
    private fun certOf(o: JSONObject?): Certification? {
        if (o == null || o.length() == 0) return null
        val appId = o.safeString("app_id")
        if (appId.isEmpty()) return null
        val comp = o.opt("completeness")
        val completeness = when (comp) {
            is Number -> comp.toDouble()
            is String -> comp.trim().toDoubleOrNull()
            else -> null
        }
        return Certification(
            appId = appId,
            badge = o.safeString("badge").ifEmpty { o.safeString("cl") },
            issuedAt = o.safeString("issued_at"),
            expiresAt = o.safeString("expires_at"),
            reviewer = o.safeString("reviewer"),
            completeness = completeness,
        )
    }

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

    /** 探活要快：地址可能有好几个，一个个试不能让用户干等。 */
    private val probeHttp = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .callTimeout(10, TimeUnit.SECONDS)
        .build()

    /** 取地址的结果。[tried] 为空 = 根本没拿到指针；非空但 [url] 为 null = 地址都探活失败。 */
    data class EndpointResult(val url: String?, val tried: List<String>)

    /**
     * 取平台接口地址（apiBase）。
     *
     * 指针 = 数据源上的 `data/endpoint.json`，由 `scripts/endpoint_watch.py` 维护：
     * PC 端隧道一换地址就写进去并推到仓库。这里多源拉取指针，再把「当前地址 +
     * 历史地址」逐个 `/health` 探活，挑第一个真能用的。
     *
     * 为什么要探活而不是直接采信：指针经 CDN 分发，可能还是几分钟前的旧值，
     * 而旧地址在隧道重启后就死了 —— 不验就直接填，用户会以为刷新成功了其实没通。
     *
     * @return 探活通过的地址放在 [EndpointResult.url]；拿不到指针或全都不通时为 null
     */
    suspend fun fetchEndpoint(base: String): EndpointResult = withContext(Dispatchers.IO) {
        val found = ArrayList<String>()
        for (root in candidates(base)) {
            val url = (if (root.endsWith("/")) root else "$root/") + "data/endpoint.json"
            val txt = runCatching {
                http.newCall(Request.Builder().url(url).build()).execute().use { r ->
                    if (!r.isSuccessful) null else r.body?.string()
                }
            }.getOrNull() ?: continue
            val o = runCatching { JSONObject(txt) }.getOrNull() ?: continue
            o.safeString("base_url").takeIf { it.isNotBlank() }?.let { found += it }
            o.optJSONArray("history")?.let { arr ->
                for (i in 0 until arr.length()) {
                    arr.optJSONObject(i)?.safeString("url")
                        ?.takeIf { it.isNotBlank() }?.let { found += it }
                }
            }
            if (found.isNotEmpty()) break // 第一个给出地址的源就够了，不必每个源都拉一遍
        }
        val tried = found.map { it.trim().trimEnd('/') }.distinct()
        EndpointResult(tried.firstOrNull { probe(it) }, tried)
    }

    /** 后端 `/health` 探活。 */
    private fun probe(base: String): Boolean = runCatching {
        probeHttp.newCall(Request.Builder().url("$base/health").build())
            .execute().use { it.isSuccessful }
    }.getOrDefault(false)

    /**
     * 强制核对时，这个源说「一个分片都不用下」。
     *
     * 这通常不是真的没变，而是这一层 CDN 还在给旧 manifest
     * （实测 fastly / cdn / raw 三家的边缘缓存彼此独立，新旧常常不一致）。
     * 抛出后由 updateFingerprints 换下一个源再核一次。
     */
    private class NoChangeOnForce : IOException()

    /**
     * 这一层的 manifest 与它自己给出的分片内容自相矛盾：按它给的 h 去下载，
     * 一个都校验不过。典型是 manifest 是边缘节点上的旧缓存、分片文件已是新内容
     * （2026-09-12 实测：108 个分片全部 SHA1 校验失败）。换下一个源重来。
     */
    private class ManifestStale : IOException()

    /**
     * 按 manifest 增量更新指纹分片。逐个候选源尝试，第一个能连通的就用。
     *
     * 只比对 SHA1：内置分片的 SHA1 写在 index/builtin.json 里（由 sync_assets.py 生成），
     * 已更新分片的 SHA1 记在 updates/meta.json。两边都对不上才下载。
     *
     * [force] = 强制全量核对，不发送 If-None-Match。用于用户手点「立即更新」、
     * 以及本机刚写入过数据（认领/注册/采集）之后——这两种情况下「服务端说没变」
     * 是不可信的：CDN 边缘节点缓存可能还停在旧版本。
     */
    suspend fun updateFingerprints(
        base: String,
        s: Strings,
        force: Boolean = false,
        onProgress: (String) -> Unit = {},
    ): UpdateResult = withContext(Dispatchers.IO) {
        val roots = candidates(base)
        val errors = ArrayList<String>()

        var noChange = false
        roots.forEachIndexed { i, root ->
            if (i > 0) onProgress(s.switchMirror(hostOf(root)))
            val r = runCatching { trySource(root, s, onProgress, force) }
                .getOrElse { e ->
                    when (e) {
                        is NoChangeOnForce -> noChange = true
                        is ManifestStale ->
                            errors += "${hostOf(root)}（manifest 与分片内容不一致，已跳过）"
                        else -> errors += "${hostOf(root)}（${friendly(e, s)}）"
                    }
                    null
                }
            if (r != null) return@withContext r
        }

        // 强制核对时**所有**源都说「没有要下的分片」，那才是真的已是最新。
        if (noChange) {
            return@withContext UpdateResult(true, message = s.alreadyLatest(hostOf(roots.last())))
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
        force: Boolean = false,
    ): UpdateResult {
        // 只有本机真的落地过 manifest，才允许用 ETag 做条件请求。
        // 否则本机一个分片都没有，服务端/CND 回 304 会被误读成「你已是最新」→ 永远不更新。
        val canUseEtag = !force && store.manifestApplied()
        val req = Request.Builder()
            .url("${root}data/manifest.json")
            .apply { if (canUseEtag) store.manifestEtag()?.let { header("If-None-Match", it) } }
            .build()

        http.newCall(req).execute().use { resp ->
            if (resp.code == 304) {
                if (!canUseEtag) {
                    // 没发条件请求却回 304：这一层缓存不可信，换下一个源。
                    throw IOException("manifest 意外 304（未发送 If-None-Match）")
                }
                store.setLastUpdateCheck(now())
                return UpdateResult(true, message = st.manifestUnchanged)
            }
            if (!resp.isSuccessful) throw IOException("manifest HTTP ${resp.code}")

            val etag = resp.header("ETag")
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

            if (force && fps.isEmpty()) {
                throw NoChangeOnForce()
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
            // 一个都没下成、且全军覆没 = 这份 manifest 不可信（多半是边缘节点上的旧缓存，
            // 与它自己指向的新分片内容对不上）。换下一个源重来，别死磕这一层。
            if (fps.isNotEmpty() && downloaded == 0 && failed == fps.size) {
                throw ManifestStale()
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
            // ETag 与「已落地」标记只在这一次 manifest 真正处理完、且没有分片失败时才写。
            // 原来是在读 body 之前就存 ETag——一旦后面的分片下载超时/校验失败，
            // 下次启动带着这个 ETag 会被 304 挡回去，缺口永远补不上。
            if (force) {
                // 强制核对的结果不产生可信 ETag（这一层可能还在给旧 manifest），
                // 直接清掉，免得下次被一个陈旧 ETag 用 304 挡回去。
                store.setManifestEtag(null)
                if (failed == 0) store.setManifestApplied(true)
            } else if (failed == 0) {
                if (etag != null) store.setManifestEtag(etag)
                store.setManifestApplied(true)
            } else {
                store.setManifestEtag(null)
                store.setManifestApplied(false)
            }
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
            val certification = certOf(o.optJSONObject("certification"))
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
                // 认证等级 / 存证。**绝大多数企业没有这个块**——没认证就是没有，
                // 缺失时留空（beacon=""）与 null，UI 按 L0 处理，不许客户端补默认值。
                beacon = o.safeString("cl"),
                certification = certification,
                certExpired = certification?.expired(todayIso()) ?: false,
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
