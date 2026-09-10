package cn.beaconmfg.app.llm

import android.util.Log
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Strings
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class ChatMsg(
    val role: String,
    val content: String? = null,
    val toolCalls: List<ToolCall>? = null,
    val toolCallId: String? = null,
) {
    fun toJson(): JSONObject {
        val o = JSONObject().put("role", role)
        content?.let { o.put("content", it) }
        toolCallId?.let { o.put("tool_call_id", it) }
        toolCalls?.let { list ->
            val arr = JSONArray()
            list.forEach { tc ->
                arr.put(
                    JSONObject()
                        .put("id", tc.id)
                        .put("type", "function")
                        .put(
                            "function",
                            JSONObject().put("name", tc.name).put("arguments", tc.arguments)
                        )
                )
            }
            o.put("tool_calls", arr)
        }
        return o
    }
}

data class ToolCall(val id: String, val name: String, val arguments: String)

data class LlmResult(
    val content: String = "",
    val toolCalls: List<ToolCall> = emptyList(),
    val error: String? = null,
)

/**
 * OpenAI 兼容端点的流式客户端（SSE）。
 *
 * key 只在本进程的 Authorization 头里出现，**不写日志、不落盘**（明文部分由
 * SettingsRepo 用 Keystore 加密存）。这是 BYOK 方案的底线：本 App 永远不持有平台 key，
 * 也不会把用户的 key 发到除用户所选端点以外的任何地方。
 */
class LlmClient(private val cfg: LlmConfig, private val str: Strings) {

    /** 一轮对话的总时限。超时后取消 SSE 并如实报错——否则界面会一直转圈、点发送没反应。 */
    private val chatTimeoutMs = 150_000L

    companion object {
        /**
         * 全局共用一个 OkHttpClient。之前是每个 LlmClient 各建一个——一轮对话里
         * LlmClient 会被新建多次，每个都带自己的连接池与线程池，连接和线程都泄漏。
         */
        private val client = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            // 流式的读取超时**不能设 0**：那是「永不超时」。服务端挂起不返回时，
            // done.await() 会永远等下去，_busy 卡在 true，之后所有发送都被静默丢弃。
            // 90 秒没有任何字节（含心跳）才判定断流，正常推理不会被误杀。
            .readTimeout(90, TimeUnit.SECONDS)
            .build()
    }

    private fun endpoint(): String {
        val b = cfg.baseUrl.trim().trimEnd('/')
        return if (b.endsWith("/chat/completions")) b else "$b/chat/completions"
    }

    suspend fun chat(
        messages: List<ChatMsg>,
        tools: JSONArray?,
        onDelta: (String) -> Unit,
    ): LlmResult = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("model", cfg.model)
            .put("stream", true)
            .put("messages", JSONArray().apply { messages.forEach { put(it.toJson()) } })
        if (tools != null && tools.length() > 0) {
            body.put("tools", tools).put("tool_choice", "auto")
        }

        val request = Request.Builder()
            .url(endpoint())
            .addHeader("Authorization", "Bearer ${cfg.apiKey.trim()}")
            .addHeader("Content-Type", "application/json")
            .addHeader("Accept", "text/event-stream")
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()

        var source: EventSource? = null
        val done = CompletableDeferred<LlmResult>()
        val text = StringBuilder()
        val calls = LinkedHashMap<Int, MutableMap<String, String>>()

        val listener = object : EventSourceListener() {
            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                if (data.trim() == "[DONE]") return
                val o = try {
                    JSONObject(data)
                } catch (e: Exception) {
                    return
                }
                val delta = o.optJSONArray("choices")?.optJSONObject(0)?.optJSONObject("delta")
                    ?: return
                val piece = if (delta.isNull("content")) null else delta.optString("content", "")
                if (!piece.isNullOrEmpty()) {
                    text.append(piece)
                    onDelta(piece)
                }
                val tcs = delta.optJSONArray("tool_calls")
                if (tcs != null) {
                    for (i in 0 until tcs.length()) {
                        val tc = tcs.optJSONObject(i) ?: continue
                        val idx = tc.optInt("index", i)
                        val slot = calls.getOrPut(idx) {
                            LinkedHashMap<String, String>().also {
                                it["id"] = ""
                                it["name"] = ""
                                it["args"] = ""
                            }
                        }
                        tc.optString("id").takeIf { it.isNotBlank() }?.let { slot["id"] = it }
                        val fn = tc.optJSONObject("function")
                        if (fn != null) {
                            fn.optString("name").takeIf { it.isNotBlank() }?.let { slot["name"] = it }
                            slot["args"] = slot["args"] + fn.optString("arguments", "")
                        }
                    }
                }
            }

            override fun onFailure(eventSource: EventSource, t: Throwable?, response: okhttp3.Response?) {
                Log.d("BeaconMFG", "sse onFailure: code=${response?.code} err=${t?.javaClass?.simpleName}")
                val code = response?.code
                val detail = try {
                    response?.body?.string()?.take(300)
                } catch (e: Exception) {
                    null
                }
                done.complete(
                    LlmResult(
                        text.toString(), emptyList(),
                        when {
                            code == 401 -> str.err401
                            code == 429 -> str.err429
                            code != null -> str.errHttp(code, detail ?: "")
                            t is java.net.SocketTimeoutException -> str.errNoBytes
                            else -> str.errNetwork(t?.message ?: "unknown")
                        }
                    )
                )
            }

            override fun onClosed(eventSource: EventSource) {
                Log.d("BeaconMFG", "sse onClosed: ${text.length} chars, ${calls.size} toolCalls")
                val list = calls.values.mapNotNull {
                    val name = it["name"].orEmpty()
                    if (name.isEmpty()) null
                    else ToolCall(it["id"].orEmpty(), name, it["args"].orEmpty())
                }
                done.complete(LlmResult(text.toString(), list))
            }
        }

        Log.d("BeaconMFG", "sse open: ${endpoint()}")
        source = EventSources.createFactory(client).newEventSource(request, listener)
        // 兜底总超时：SSE 既不 onClosed 也不 onFailure 时（服务端挂着不响应），
        // await 会永远挂起。这里到点就取消连接并如实报错，让界面能恢复可用。
        val r = withTimeoutOrNull(chatTimeoutMs) { done.await() }
        if (r == null) {
            Log.d("BeaconMFG", "sse TIMEOUT ${chatTimeoutMs}ms — cancelling")
            source.cancel()
            return@withContext LlmResult(
                text.toString(),
                emptyList(),
                str.errTimeout((chatTimeoutMs / 1000).toInt())
            )
        }
        return@withContext r
    }

    /** 连通性自检：只发一条极短的消息，验证 key + 端点 + 模型是否可用。 */
    suspend fun test(): String = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("model", cfg.model)
            .put("stream", false)
            .put("max_tokens", 16)
            .put(
                "messages",
                JSONArray().put(JSONObject().put("role", "user").put("content", "hi"))
            )
        val request = Request.Builder()
            .url(endpoint())
            .addHeader("Authorization", "Bearer ${cfg.apiKey.trim()}")
            .addHeader("Content-Type", "application/json")
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()
        try {
            client.newCall(request).execute().use { resp ->
                if (resp.isSuccessful) {
                    str.testOk(cfg.model)
                } else {
                    str.testFail(resp.code, resp.body?.string()?.take(200) ?: "")
                }
            }
        } catch (e: Exception) {
            str.testConnErr(e.message ?: e.javaClass.simpleName)
        }
    }
}
