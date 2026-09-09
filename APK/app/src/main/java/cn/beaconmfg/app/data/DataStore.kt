package cn.beaconmfg.app.data

import android.app.Application
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * 内置数据 + 已更新副本的统一访问层。
 *
 * 两条数据来源：
 *  - **内置**（assets/）：全量指纹 4.7MB + 索引，随 APK 走，装完即可离线检索；
 *  - **已更新**（filesDir/updates/）：联网时按 manifest 增量拉取的分片，同名覆盖内置。
 *
 * 检索永远读「已更新优先、内置兜底」，这样断网时 App 不会退化成空库。
 */
class DataStore(private val app: Application) {

    val updateDir: File get() = File(app.filesDir, "updates")
    val detailDir: File get() = File(app.filesDir, "detail")

    @Volatile
    private var cached: List<Fingerprint>? = null

    @Volatile
    private var builtinMeta: JSONObject = JSONObject()

    @Volatile
    private var updateMeta: JSONObject = JSONObject()

    /** 内置号码索引 id → 电话。构建期由 sync_assets.py 从 data/gb 完整档案抽出。 */
    @Volatile
    private var phoneMap: Map<String, String>? = null

    private val prefs get() = app.getSharedPreferences("bmfg_store", 0)

    // ── 内置索引 ────────────────────────────────────────────────────────────
    fun readAsset(path: String): String? = try {
        app.assets.open(path).bufferedReader().use { it.readText() }
    } catch (e: Exception) {
        null
    }

    fun builtinAt(): String = builtinMeta.optString("builtin_at", "未知")

    fun builtinFingerprintSummary(): String {
        val fp = builtinMeta.optJSONObject("fingerprint")
            ?: return "内置指纹：未知（跑 APK/tools/sync_assets.py 生成）"
        return "内置指纹 %d 片 / %.2f MB".format(
            fp.optInt("shards", 0), fp.optLong("bytes", 0) / 1048576.0
        )
    }

    /** 内置分片的 SHA1。用于和 manifest 的 h 字段比对，判断有没有新版本。 */
    fun builtinShaOf(rel: String): String? {
        val shards = builtinMeta.optJSONObject("fingerprint")?.optJSONObject("shards")
            ?: return null
        return shards.optJSONObject(rel)?.optString("sha1")
    }

    // ── 号码索引（assets/index/phone-index.jsonl，每行 id,phone）─────────────
    /** 约 1.5 万条，第一次装载几十毫秒。必须在 IO 线程调用。 */
    private fun phones(): Map<String, String> {
        phoneMap?.let { return it }
        val m = HashMap<String, String>(17000)
        // 已更新副本优先、内置兜底——和指纹同一套路，断网时不退化成「无号码」
        val f = File(updateDir, "phone-index.jsonl")
        val text = if (f.exists()) f.readText() else readAsset("index/phone-index.jsonl")
        text?.lineSequence()?.forEach { line ->
            if (line.isBlank()) return@forEach
            val i = line.indexOf(',')
            if (i <= 0) return@forEach
            m[line.substring(0, i)] = line.substring(i + 1)
        }
        phoneMap = m
        return m
    }

    /** 取号码。取不到返回空串——源数据是「待核实」占位值时就是这样，不猜号。 */
    fun phoneOf(id: String): String = phones()[id].orEmpty()

    fun phoneIndexSummary(): String = "号码索引 ${phones().size} 家（其余源数据为占位值，留空）"

    /** 已装号码索引的内容 SHA1，用于和 manifest 的 h 比对该不该重下。 */
    fun localPhoneSha(): String? = prefs.getString("phone_sha", null)

    /** 写入更新后的号码索引。SHA1 对不上就不落盘——宁可留旧的，也不能写入半截索引。 */
    fun writePhoneIndex(content: String, expectSha: String): Boolean {
        if (sha1(content.toByteArray()) != expectSha) return false
        updateDir.mkdirs()
        val target = File(updateDir, "phone-index.jsonl")
        val tmp = File(target.absolutePath + ".tmp")
        tmp.writeText(content)
        if (!tmp.renameTo(target)) return false
        prefs.edit().putString("phone_sha", expectSha).apply()
        phoneMap = null          // 下次读取时重载
        return true
    }

    // ── 已更新副本 ──────────────────────────────────────────────────────────
    private fun loadUpdateMeta() {
        val f = File(updateDir, "meta.json")
        if (f.exists()) {
            updateMeta = try {
                JSONObject(f.readText())
            } catch (e: Exception) {
                JSONObject()
            }
        } else {
            updateMeta = JSONObject()
        }
    }

    fun localShaOf(rel: String): String? {
        if (updateMeta.length() == 0) loadUpdateMeta()
        return updateMeta.optJSONObject("shards")?.optString(rel)
    }

    fun lastUpdateAt(): String {
        if (updateMeta.length() == 0) loadUpdateMeta()
        return updateMeta.optString("updated_at", "从未")
    }

    fun updatedShardCount(): Int {
        if (updateMeta.length() == 0) loadUpdateMeta()
        return updateMeta.optJSONObject("shards")?.length() ?: 0
    }

    /** 写入一个更新后的分片，并登记 SHA1。校验不过就不落盘——宁可留旧数据。 */
    fun writeShard(rel: String, content: String, expectSha: String): Boolean {
        if (sha1(content.toByteArray()) != expectSha) return false
        val target = File(updateDir, rel)
        target.parentFile?.mkdirs()
        val tmp = File(target.absolutePath + ".tmp")
        tmp.writeText(content)
        if (!tmp.renameTo(target)) return false
        if (updateMeta.length() == 0) loadUpdateMeta()
        val shards = updateMeta.optJSONObject("shards") ?: JSONObject().also {
            updateMeta.put("shards", it)
        }
        shards.put(rel, expectSha)
        persistUpdateMeta()
        cached = null
        return true
    }

    private fun persistUpdateMeta() {
        updateDir.mkdirs()
        File(updateDir, "meta.json").writeText(updateMeta.toString())
    }

    fun manifestEtag(): String? = prefs.getString("manifest_etag", null)
    fun setManifestEtag(v: String?) = prefs.edit().putString("manifest_etag", v).apply()

    fun lastUpdateCheck(): String = prefs.getString("last_check", "从未") ?: "从未"
    fun setLastUpdateCheck(v: String) = prefs.edit().putString("last_check", v).apply()

    // ── 指纹装载 ────────────────────────────────────────────────────────────
    /** 全量指纹。约 2 万条，第一次装载约 300–600ms，必须在 IO 线程调用。 */
    fun fingerprints(): List<Fingerprint> {
        cached?.let { return it }
        if (builtinMeta.length() == 0) {
            readAsset("index/builtin.json")?.let {
                builtinMeta = try { JSONObject(it) } catch (e: Exception) { JSONObject() }
            }
        }
        if (updateMeta.length() == 0) loadUpdateMeta()

        val out = ArrayList<Fingerprint>(21000)
        for (rel in listBuiltinShards()) {
            val updated = File(updateDir, rel)
            if (updated.exists()) {
                parseLines(updated.readText(), out)
            } else {
                readAsset("fingerprint/$rel")?.let { parseLines(it, out) }
            }
        }
        // 已更新分片里可能有内置没有的新小类（新增行业），补上
        val builtinSet = out.mapTo(HashSet()) { it.id }
        File(updateDir, "gb").walkTopDown().filter { it.isFile && it.extension == "jsonl" }
            .forEach { f ->
                parseLines(f.readText(), out, skipKnown = builtinSet)
            }
        val list = out
        cached = list
        return list
    }

    /** 枚举 assets/fingerprint 下的全部 jsonl 相对路径（如 gb/C/34/3453.jsonl）。 */
    private fun listBuiltinShards(): List<String> {
        val out = ArrayList<String>()
        walkAssets("fingerprint", out)
        return out.map { it.removePrefix("fingerprint/") }
    }

    private fun walkAssets(base: String, out: MutableList<String>) {
        val items = app.assets.list(base)
        if (items.isNullOrEmpty()) {
            out.add(base)
            return
        }
        for (item in items) {
            walkAssets(if (base.isEmpty()) item else "$base/$item", out)
        }
    }

    private fun parseLines(
        text: String,
        out: MutableList<Fingerprint>,
        skipKnown: Set<String>? = null,
    ) {
        text.lineSequence().forEach { line ->
            if (line.isBlank()) return@forEach
            val o = try {
                JSONObject(line)
            } catch (e: Exception) {
                return@forEach
            }
            val id = o.safeString("id")
            if (id.isEmpty()) return@forEach
            if (skipKnown != null && skipKnown.contains(id)) return@forEach
            out.add(
                Fingerprint(
                    id = id,
                    co = o.safeString("co"),
                    city = o.safeString("city"),
                    gb = o.safeString("gb"),
                    mf = o.optInt("mf", 1) != 0,
                    proc = strList(o, "proc"),
                    mat = strList(o, "mat"),
                    cert = strList(o, "cert"),
                    cl = o.safeString("cl").ifEmpty { "L0" },
                    pv = o.safeString("pv"),
                    sc = o.optInt("sc", 0),
                    tel = o.optInt("tel", 0) != 0,
                    phone = phoneOf(id),
                )
            )
        }
    }

    private fun strList(o: JSONObject, key: String): List<String> {
        val arr: JSONArray = o.optJSONArray(key) ?: return emptyList()
        val out = ArrayList<String>(arr.length())
        for (i in 0 until arr.length()) {
            val v = arr.optString(i, "")
            if (v.isNotEmpty()) out.add(v)
        }
        return out
    }

    // ── L1 能力卡（内置 assets/capability/，不依赖网络）─────────────────────
    /**
     * 国标码 → 分片相对路径。由 sync_assets.py 生成，
     * 用它避免为了查一家厂把 41 个分片全读一遍。
     */
    @Volatile
    private var capMap: JSONObject? = null

    /** 分片 → 该片全部能力卡。按片缓存，同小类连续查看只解析一次。 */
    private val capShards = HashMap<String, Map<String, CapabilityCard>>()

    private fun capIndex(): JSONObject {
        capMap?.let { return it }
        val m = readAsset("capability/_map.json")?.let {
            try {
                JSONObject(it)
            } catch (e: Exception) {
                JSONObject()
            }
        } ?: JSONObject()
        capMap = m
        return m
    }

    /**
     * 按 id 取能力卡。gb 是国标小类码（4 位），用来定位分片。
     * 取不到返回 null —— 23698 家里只有 4136 家有卡，没有卡是常态不是错误。
     * 必须在 IO 线程调用（首次会读文件）。
     */
    fun capabilityOf(id: String, gb: String): CapabilityCard? {
        if (id.isEmpty()) return null
        val rel = capIndex().optString(gb, "").ifEmpty {
            // 国标码对不上（3 位中类码或未归类）：退到未归类分片里找一遍
            capIndex().optString("_", "")
        }
        if (rel.isEmpty()) return null
        val shard = capShards[rel] ?: loadCapShard(rel).also { capShards[rel] = it }
        return shard[id]
    }

    private fun loadCapShard(rel: String): Map<String, CapabilityCard> {
        val text = readAsset("capability/$rel") ?: return emptyMap()
        val out = HashMap<String, CapabilityCard>()
        val arr = try {
            org.json.JSONArray(text)
        } catch (e: Exception) {
            return emptyMap()
        }
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val sid = o.safeString("id")
            if (sid.isEmpty()) continue
            val procs = ArrayList<ProcItem>()
            val pa = o.optJSONArray("proc")
            if (pa != null) {
                for (j in 0 until pa.length()) {
                    val p = pa.optJSONObject(j) ?: continue
                    procs.add(
                        ProcItem(
                            code = p.safeString("c"),
                            name = p.safeString("n"),
                            level = p.safeString("l"),
                        )
                    )
                }
            }
            val lim = LinkedHashMap<String, String>()
            val lo = o.optJSONObject("lim")
            if (lo != null) {
                for (k in lo.keys()) {
                    val v = lo.opt(k) ?: continue
                    val s = when (v) {
                        is org.json.JSONArray -> v.toString()
                        else -> v.toString()
                    }
                    if (s.isNotEmpty() && s != "null") lim[k] = s
                }
            }
            val sk = o.optJSONObject("sk")
            out[sid] = CapabilityCard(
                id = sid,
                company = o.safeString("co"),
                gb = o.safeString("gb"),
                gbName = o.safeString("gn"),
                city = o.safeString("city"),
                province = o.safeString("prov"),
                processes = procs,
                materials = strList(o, "mat"),
                limits = lim,
                badge = o.safeString("cl").ifEmpty { "L0" },
                provenance = o.safeString("pv"),
                hasPhone = o.optInt("tel", 0) != 0,
                skillPath = sk?.safeString("u") ?: "",
                skillVerified = sk?.optBoolean("v", false) ?: false,
            )
        }
        return out
    }

    fun capabilitySummary(): String {
        val n = capIndex().length()
        return if (n == 0) "能力卡：未内置（跑 sync_assets.py）"
        else "能力卡：内置 $n 个国标小类分片"
    }

    // ── 详情缓存（按需下载的 data/gb/**.json） ──────────────────────────────
    fun cachedDetailContent(code: String): String? {
        val f = File(detailDir, "$code.json")
        return if (f.exists()) f.readText() else null
    }

    fun cacheDetail(code: String, content: String) {
        detailDir.mkdirs()
        File(detailDir, "$code.json").writeText(content)
    }

    companion object {
        fun sha1(bytes: ByteArray): String {
            val md = MessageDigest.getInstance("SHA-1")
            return md.digest(bytes).joinToString("") { "%02x".format(it) }
        }
    }
}
