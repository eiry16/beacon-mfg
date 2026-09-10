package cn.beaconmfg.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 平台服务端返回的结构化错误。
 *
 * FastAPI 侧是统一格式 `{"error": {"code", "message", "details"}}`
 * （见 server/main.py 的异常处理器）。**`code` 要原样带出来**——
 * 「必填未齐」和「网络不通」对供应商是两件完全不同的事，糊成一句"失败"等于没说。
 */
class PlatformApiException(val code: String, override val message: String) : Exception(message)

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

    /** POST /v1/collect/{sid}/confirm → 生成能力卡。门禁不过会抛 MISSING_REQUIRED / UNRESOLVED_CONFLICTS。 */
    suspend fun collectConfirm(supplierId: String, overwrite: Boolean = false): JSONObject =
        call(
            "POST", "/v1/collect/$supplierId/confirm",
            JSONObject().put("overwrite_existing", overwrite), auth = true,
        )
}
