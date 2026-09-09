package cn.beaconmfg.app.llm

/** 支持的服务商预设。全部是 OpenAI 兼容的 /chat/completions 端点，key 由用户自己填。 */
enum class Preset(
    val id: String,
    val label: String,
    val endpoint: String,
    val models: List<String>,
    val keyHint: String,
) {
    DEEPSEEK(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1",
        listOf("deepseek-chat", "deepseek-reasoner"), "sk- 开头"
    ),
    QWEN(
        "qwen", "通义千问（阿里云百炼）",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        listOf("qwen-plus", "qwen-turbo", "qwen-max"), "sk- 开头"
    ),
    ZHIPU(
        "zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
        listOf("glm-4-flash", "glm-4-air", "glm-4-plus"), "形如 xxxxx.yyyyy"
    ),
    OPENAI(
        "openai", "OpenAI", "https://api.openai.com/v1",
        listOf("gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"), "sk- 开头"
    ),
    CUSTOM(
        "custom", "自定义（OpenAI 兼容）", "",
        emptyList(), "按你的服务商要求填写"
    );

    companion object {
        fun of(id: String): Preset = entries.firstOrNull { it.id == id } ?: DEEPSEEK
    }
}

data class LlmConfig(
    val presetId: String = Preset.DEEPSEEK.id,
    val baseUrl: String = Preset.DEEPSEEK.endpoint,
    val model: String = Preset.DEEPSEEK.models.first(),
    val apiKey: String = "",
) {
    fun ready(): Boolean = apiKey.isNotBlank() && baseUrl.isNotBlank() && model.isNotBlank()
}
