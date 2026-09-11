package cn.beaconmfg.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 平台服务端返回的结构化错误。
 *
 * FastAPI 侧是统一格式 `{"error": {"code", "message", "details"}}`
 * （见 server/main.py 的异常处理器）。**`code` 要原样带出来**——
 * 「必填未齐」和「网络不通」对供应商是两件完全不同的事，糊成一句"失败"等于没说。
 *
 * [details] 同样必须带出来：`code` 只说"必填未齐"，而 `details.missing_labels`
 * 才说得清**缺哪几项**。少了它，调用方只能反复重试一个必然失败的请求——
 * 2026-09-11 真机卡死就是这么来的（App 把 details 丢了，模型永远不知道该补什么）。
 */
class PlatformApiException(
    val code: String,
    override val message: String,
    val details: JSONObject? = null,
) : Exception(message)

/**
 * 平台服务端（`server/`，FastAPI）的最小客户端。
 *
 * 为什么单独抽一个类：App 的**买家侧完全是本地的**（只读内置/已下载数据，不依赖任何服务端），
 * 只有供应商侧（认领、采集）需要后端。所以地址做成一个**可留空**的设置项，
 * 留空时供应商侧明确显示未开通，**而不是假装成功**——
 * 这个项目在「静默成功」上已经栽过 11 次（见工作日志），不差再多一个。
 *
 * 认证：认领成功后拿到 claim_token，采集接口要求 `Authorization: Bearer <token>`
 * （server/routers/collect.py 的 require_operator 现阶段只做可追溯）。
 *
 * 路由以 `server/routers` 下的**实际代码**为准。注意 docs 里写的
 * `/v1/claim/verify-start` 是错的，真实前缀是 `/claim`（claim.py:20 无 /v1）。
 */
class PlatformApi(
    /** 平台根地址，**每次调用时取**——设置里填完立刻生效，不用重启。留空 = 未开通。 */
    private val base: () -> String,
    /** 当前认领凭证。没认领就是 null（此时采集类调用会拿到 401，如实报错）。 */
    private val token: () -> String? = { null },
) {

    companion object {
        /**
         * 写操作要落库，给足时间，但**必须**有上限：无限等待会把界面卡死
         * （LlmClient 的 readTimeout(0) 已经踩过一次，见工作日志）。
         */
        private val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    val configured: Boolean get() = base().trim().isNotBlank()

    private suspend fun call(
        method: String,
        path: String,
        body: JSONObject? = null,
        auth: Boolean = false,
    ): JSONObject = withContext(Dispatchers.IO) {
        if (!configured) {
            throw PlatformApiException("NOT_CONFIGURED", "平台接口地址未配置")
        }
        val builder = Request.Builder().url(base().trim().trimEnd('/') + path)
        if (auth) token()?.takeIf { it.isNotBlank() }?.let { builder.addHeader("Authorization", "Bearer $it") }
        if (body != null) {
            builder.addHeader("Content-Type", "application/json")
            builder.method(method, body.toString().toRequestBody("application/json".toMediaType()))
        } else {
            builder.method(method, null)
        }

        val resp = try {
            client.newCall(builder.build()).execute()
        } catch (e: Exception) {
            throw PlatformApiException("NETWORK", e.message ?: e.javaClass.simpleName)
        }

        resp.use {
            val text = resp.body?.string().orEmpty()
            val obj = try {
                if (text.isBlank()) JSONObject() else JSONObject(text)
            } catch (e: Exception) {
                // 返回的不是 JSON（网关页、反代错误页…）→ 原样带一段出来，别吞掉
                throw PlatformApiException("BAD_RESPONSE", "HTTP ${resp.code}：${text.take(200)}")
            }
            if (!resp.isSuccessful) {
                val err = obj.optJSONObject("error")
                throw PlatformApiException(
                    err?.optString("code").orEmpty().ifBlank { "HTTP_${resp.code}" },
                    err?.optString("message").orEmpty().ifBlank { "HTTP ${resp.code}" },
                )
            }
            obj
        }
    }

    // ── 认领（server/routers/claim.py，Phase 0 stub）────────────────────────

    /** POST /claim/verify-start → {session_id, qrcode_url} */
    suspend fun claimStart(supplierId: String): JSONObject =
        call("POST", "/claim/verify-start", JSONObject().put("supplier_id", supplierId))

    /** POST /claim/send-code → {sent}。手机号必须 11 位（服务端 min/max_length=11）。 */
    suspend fun claimSendCode(sessionId: String, phone: String): JSONObject =
        call(
            "POST", "/claim/send-code",
            JSONObject().put("session_id", sessionId).put("phone", phone),
        )

    /** POST /claim/verify-code → {success, supplier_id, claim_token}。code 必须 6 位数字。 */
    suspend fun claimVerifyCode(sessionId: String, code: String): JSONObject =
        call(
            "POST", "/claim/verify-code",
            JSONObject().put("session_id", sessionId).put("code", code),
        )

    /** POST /claim/confirm → {status, can_add_skill} */
    suspend fun claimConfirm(claimToken: String): JSONObject =
        call("POST", "/claim/confirm", JSONObject().put("claim_token", claimToken))

    // ── 对话式采集（server/routers/collect.py，前缀 /v1/collect）────────────

    /** POST /v1/collect/session → {resumed, next_question, state} */
    suspend fun collectSession(supplierId: String): JSONObject =
        call("POST", "/v1/collect/session", JSONObject().put("supplier_id", supplierId), auth = true)

    /** POST /v1/collect/{sid}/autofill → {filled, count, next_question, state} */
    suspend fun collectAutofill(supplierId: String): JSONObject =
        call("POST", "/v1/collect/$supplierId/autofill", JSONObject(), auth = true)

    /** POST /v1/collect/{sid}/turn → {action, extracted, next_question, state} */
    suspend fun collectTurn(supplierId: String, text: String): JSONObject =
        call("POST", "/v1/collect/$supplierId/turn", JSONObject().put("text", text), auth = true)

    /** POST /v1/collect/{sid}/turn（skip=true）→ 跳过当前题，字段留空 */
    suspend fun collectSkip(supplierId: String): JSONObject =
        call(
            "POST", "/v1/collect/$supplierId/turn",
            JSONObject().put("skip", true), auth = true,
        )

    /** GET /v1/collect/{sid}/state → 完整度 / 缺失必填 / 冲突 / storage */
    suspend fun collectState(supplierId: String): JSONObject =
        call("GET", "/v1/collect/$supplierId/state", auth = true)

    /**
     * POST /v1/collect/{sid}/fix-missing → 把流程**退回第一个必填空缺的题**，返回该题。
     *
     * 用途单一：`collectConfirm` 被 MISSING_REQUIRED 拒了之后的补救。
     * 会话已经走到末尾时 `turn` 一律回 COLLECT_FINISHED（本题不能重答），
     * `back` 又只能一次退一步 —— 缺的字段可能在任意位置，所以必须由服务端定位。
     */
    suspend fun collectFixMissing(supplierId: String): JSONObject =
        call("POST", "/v1/collect/$supplierId/fix-missing", JSONObject(), auth = true)

    /** POST /v1/collect/{sid}/confirm → 生成能力卡。门禁不过会抛 MISSING_REQUIRED / UNRESOLVED_CONFLICTS。 */
    suspend fun collectConfirm(supplierId: String, overwrite: Boolean = false): JSONObject =
        call(
            "POST", "/v1/collect/$supplierId/confirm",
            JSONObject().put("overwrite_existing", overwrite), auth = true,
        )

    // ── 企业注册 / 建档（server/routers/certification.py，前缀 /v1/certify）────

    /**
     * 企业主动注册建档。名录里**没有**这家企业时走这条 —— 平台新分配一个 supplier_id
     * 并建立认证档案；名录里**已经有**时服务端会复用既有 ID 并回 `matched_existing=true`，
     * 那种情况其实该走认领（claim），不是注册（调用方要按这个字段分流）。
     *
     * 服务端要求：`company` ≥2 字、`claimed_address` ≥4 字，且**名录未命中时 category 必填**
     * （否则回 CATEGORY_REQUIRED）。这三个门禁在 App 侧先拦一道，避免白跑一趟网络。
     *
     * `public_address_records` 有意不传：服务端用它比对「自报地址 vs 公开展示地址」，
     * 而 App 手上没有公开地址数据。传空数组的语义是「平台没有可比对的公开记录」，
     * 这是事实，不是省略。
     */
    suspend fun certifyApply(
        company: String,
        claimedAddress: String,
        category: String? = null,
        contactName: String? = null,
        contactPhone: String? = null,
    ): JSONObject {
        val body = JSONObject()
            .put("company", company)
            .put("claimed_address", claimedAddress)
        category?.takeIf { it.isNotBlank() }?.let { body.put("category", it) }
        contactName?.takeIf { it.isNotBlank() }?.let { body.put("contact_name", it) }
        contactPhone?.takeIf { it.isNotBlank() }?.let { body.put("contact_phone", it) }
        return call("POST", "/v1/certify/apply", body, auth = true)
    }

    // ── 认证与灯牌（server/routers/certification.py，前缀 /v1/certify）────────
    //
    // 为什么要有这几条：App 原先接完 `apply` 就断了 —— 企业走了认领/注册，
    // 资料也采了，但**没有人把结果交给认证链路**，于是灯牌永远停在「未认领」，
    // 而界面上看不出到底缺什么。这一段就是补上主体核验、能力登记与灯牌查询。
    //
    // ⚠ `review`（人工复核）**故意不接**：它要求审核员身份，是平台侧的人工动作，
    // 企业端能自己批自己，灯牌就一文不值了。

    /**
     * POST /v1/certify/ensure → 确保这家企业有一份认证档案（**幂等**，已有就复用）。
     *
     * 认领名录里已收录的企业时没有「注册」那一步，App 手上只有 supplier_id ——
     * 而核验结果必须挂在档案上，否则无处可写。
     */
    suspend fun certifyEnsure(
        supplierId: String,
        company: String,
        category: String? = null,
        claimedAddress: String? = null,
    ): JSONObject {
        val body = JSONObject().put("supplier_id", supplierId).put("company", company)
        category?.takeIf { it.isNotBlank() }?.let { body.put("category", it) }
        claimedAddress?.takeIf { it.isNotBlank() }?.let { body.put("claimed_address", it) }
        return call("POST", "/v1/certify/ensure", body, auth = true)
    }

    /**
     * POST /v1/certify/{app_id}/identity → 主体核验。
     *
     * `materials` 里登记营业执照时**必须带 self_declared=true**：平台拿到的是企业
     * 自报的号码，没有影像件、也没核验过原件。这根标记决定了灯牌能不能说实话。
     */
    suspend fun certifyIdentity(
        appId: String,
        uscc: String? = null,
        licenseCompanyName: String? = null,
        legalPerson: String? = null,
        businessNature: String? = null,
        employeeCount: Int? = null,
    ): JSONObject {
        val body = JSONObject()
        uscc?.takeIf { it.isNotBlank() }?.let { body.put("uscc", it) }
        licenseCompanyName?.takeIf { it.isNotBlank() }?.let { body.put("license_company_name", it) }
        legalPerson?.takeIf { it.isNotBlank() }?.let { body.put("legal_person", it) }
        businessNature?.takeIf { it.isNotBlank() }?.let { body.put("business_nature", it) }
        employeeCount?.let { body.put("employee_count", it) }
        if (uscc != null && uscc.isNotBlank()) {
            body.put(
                "materials",
                JSONArray().put(
                    JSONObject()
                        .put("type", "business_license")
                        .put("ref", "USCC-$uscc")
                        // 如实标注：企业自报，未见影像件
                        .put("self_declared", true)
                )
            )
        }
        return call("POST", "/v1/certify/$appId/identity", body, auth = true)
    }

    /** GET /v1/certify/{app_id}/badge → 当前灯牌 + 还差什么（blockers/warnings）。 */
    suspend fun certifyBadge(appId: String): JSONObject =
        call("GET", "/v1/certify/$appId/badge", auth = true)

    /**
     * POST /v1/certify/{app_id}/capability/from-collect → 把**已定稿**的能力卡登记为认证材料。
     *
     * 不带 card 参数：那份数据服务端本来就有，App 转手一遍只会多一个能丢字段的环节。
     * 没定稿会被服务端拒（CAPABILITY_NOT_FOUND）——草稿不参与灯牌判定。
     */
    suspend fun certifyCapabilityFromCollect(appId: String): JSONObject =
        call("POST", "/v1/certify/$appId/capability/from-collect", JSONObject(), auth = true)
}
