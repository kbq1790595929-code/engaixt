from __future__ import annotations

from translators.deepseek import DeepSeekTranslator


class QwenTranslator(DeepSeekTranslator):
    name = "qwen"
    label = "通义千问 (DashScope/OpenAI 兼容)"
    _BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    MODEL = "qwen3.7-plus"
    API_KEY_FIELDS = ("qwen_api_key",)
    MODEL_CONFIG_FIELD = "qwen_model"
    EXTRA_BODY = None
    MAX_CONCURRENCY = 24
    REQUEST_TIMEOUT_SECONDS = 60


class ZhipuTranslator(DeepSeekTranslator):
    name = "zhipu"
    label = "智谱 GLM (OpenAI 兼容)"
    _BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    MODEL = "glm-4.7-flash"
    API_KEY_FIELDS = ("zhipu_api_key",)
    MODEL_CONFIG_FIELD = "zhipu_model"
    EXTRA_BODY = None
    MAX_CONCURRENCY = 16
    REQUEST_TIMEOUT_SECONDS = 60


class MoonshotTranslator(DeepSeekTranslator):
    name = "moonshot"
    label = "Kimi / Moonshot (OpenAI 兼容)"
    _BASE_URL = "https://api.moonshot.cn/v1"
    MODEL = "kimi-k2.6"
    API_KEY_FIELDS = ("moonshot_api_key",)
    MODEL_CONFIG_FIELD = "moonshot_model"
    EXTRA_BODY = None
    MAX_CONCURRENCY = 6
    MAX_INPUT_TOKENS = 2400
    MAX_COMPLEX_INPUT_TOKENS = 1600
    MAX_CHOICE_BATCH = 40
    MAX_MESSAGE_BATCH = 20
    MAX_COMPLEX_BATCH = 6
    REQUEST_TIMEOUT_SECONDS = 90
    RETRY_MAX_RETRIES = 4
    RETRY_BASE_DELAY_SECONDS = 3.0


class DoubaoTranslator(DeepSeekTranslator):
    name = "doubao"
    label = "火山方舟 / 豆包 (OpenAI 兼容)"
    _BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
    MODEL = "doubao-seed-2-1-turbo-260628"
    API_KEY_FIELDS = ("doubao_api_key",)
    MODEL_CONFIG_FIELD = "doubao_model"
    EXTRA_BODY = None
    MAX_CONCURRENCY = 32
    REQUEST_TIMEOUT_SECONDS = 60
