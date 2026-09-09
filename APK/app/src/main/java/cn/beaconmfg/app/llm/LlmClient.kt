package cn.beaconmfg.app.llm

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
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
class LlmClient(private val cfg: LlmConfig) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)   // 流式：读取超时交给 SSE 心跳
        .build()

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
                            code == 401 -> "API key 无效或已过期（HTTP 401）"
                            code == 429 -> "触发限流（HTTP 429），稍后再试"
                            code != null -> "请求失败 HTTP $code：${detail ?: ""}"
                            else -> "网络错误：${t?.message ?: "未知"}"
                        }
                    )
                )
            }

            override fun onClosed(eventSource: EventSource) {
                val list = calls.values.mapNotNull {
                    val name = it["name"].orEmpty()
                    if (name.isEmpty()) null
                    else ToolCall(it["id"].orEmpty(), name, it["args"].orEmpty())
                }
                done.complete(LlmResult(text.toString(), list))
            }
        }

        EventSources.createFactory(client).newEventSource(request, listener)
        return@withContext done.await()
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
                    "连通正常（HTTP 200，模型 ${cfg.model}）"
                } else {
                    "失败 HTTP ${resp.code}：" + (resp.body?.string()?.take(200) ?: "")
                }
            }
        } catch (e: Exception) {
            "连接失败：${e.message ?: e.javaClass.simpleName}"
        }
    }
}
