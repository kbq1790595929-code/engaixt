from config import Config
from translators.domestic import (
    DoubaoTranslator,
    MoonshotTranslator,
    QwenTranslator,
    ZhipuTranslator,
)
from translators.deepseek import DeepSeekTranslator
from translators.factory import (
    create_translator,
    translator_api_key,
    translator_choices,
    translator_model,
    translator_prompt_version,
)


def test_domestic_translators_are_registered():
    choices = translator_choices()
    for name in ["qwen", "zhipu", "moonshot", "doubao"]:
        assert name in choices
        assert create_translator(name).name == name


def test_domestic_translators_use_openai_compatible_batch_base():
    providers = [
        QwenTranslator(),
        ZhipuTranslator(),
        MoonshotTranslator(),
        DoubaoTranslator(),
    ]
    for provider in providers:
        assert provider._BASE_URL.startswith("https://")
        assert provider.PROMPT_VERSION == "game_ja_zh_batch_map_v3"
        assert provider._chat_extra_body() is None


def test_domestic_translators_respect_configured_concurrency():
    cfg = Config(max_concurrency=100)

    assert QwenTranslator()._configured_concurrency(cfg) == 100
    assert ZhipuTranslator()._configured_concurrency(cfg) == 100
    assert MoonshotTranslator()._configured_concurrency(cfg) == 100
    assert DoubaoTranslator()._configured_concurrency(cfg) == 100
    assert DeepSeekTranslator()._configured_concurrency(Config(max_concurrency=2500)) == 2500
    assert DeepSeekTranslator()._configured_concurrency(Config(max_concurrency=0)) == 1
    assert MoonshotTranslator().REQUEST_TIMEOUT_SECONDS == 90
    assert MoonshotTranslator().MAX_MESSAGE_BATCH == 20
    assert DeepSeekTranslator()._configured_concurrency(
        Config(max_concurrency=2500, deepseek_model="deepseek-v4-pro")
    ) == 500


def test_domestic_translator_config_fields_are_used():
    cfg = Config(
        deepseek_api_key="deepseek-key",
        deepseek_model="deepseek-test",
        qwen_api_key="qwen-key",
        qwen_model="qwen-test",
        zhipu_api_key="zhipu-key",
        zhipu_model="glm-test",
        moonshot_api_key="moonshot-key",
        moonshot_model="moonshot-test",
        doubao_api_key="doubao-key",
        doubao_model="doubao-test",
    )

    assert translator_api_key("deepseek", cfg) == "deepseek-key"
    assert translator_model("deepseek", cfg) == "deepseek-test"
    assert DeepSeekTranslator()._model(cfg) == "deepseek-test"
    assert translator_api_key("qwen", cfg) == "qwen-key"
    assert translator_model("qwen", cfg) == "qwen-test"
    assert translator_api_key("zhipu", cfg) == "zhipu-key"
    assert translator_model("zhipu", cfg) == "glm-test"
    assert translator_api_key("moonshot", cfg) == "moonshot-key"
    assert translator_model("moonshot", cfg) == "moonshot-test"
    assert translator_api_key("doubao", cfg) == "doubao-key"
    assert translator_model("doubao", cfg) == "doubao-test"
    assert translator_prompt_version("doubao") == "game_ja_zh_batch_map_v3"
