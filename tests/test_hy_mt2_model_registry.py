import time

from config import Config
from translators.hy_mt2 import HyMt2Translator
from translators import hy_mt2_component
from translators.hy_mt2_component import model_path
from translators.hy_mt2_models import (
    DEFAULT_MODEL_NAME,
    HyMt2ModelSpec,
    MODEL_SPECS,
    model_choices,
    model_spec,
    resolve_model_name,
)
from translators.hy_mt2_runtime import HyMt2Runtime, LocalCompletion


def test_hy_mt2_offline_models_include_lightweight_and_quality_variants():
    names = [spec.name for spec in model_choices()]

    assert names == [
        "Hy-MT2-1.8B-Q4_K_M",
        "HY-MT2-7B-Q4_K_M",
        "HY-MT2-7B-Q8_0",
        "EngAixt-7B-Q4_K_M",
    ]
    assert model_spec("HY-MT2-7B-Q4_K_M").size == 4_624_648_896
    assert model_spec("HY-MT2-7B-Q4_K_M").sha256 == (
        "9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b"
    )
    assert model_spec("HY-MT2-7B-Q8_0").size == 7_981_928_896
    assert model_spec("HY-MT2-7B-Q8_0").sha256 == (
        "58b3ad55dd6f6fa08c695cddc34fb5f8f708a844f78ae10508071914b0ed67c0"
    )
    assert model_spec("HY-MT2-7B-Q8_0").source_url.endswith("HY-MT2-7B-Q8_0.gguf")


def test_engaixt_7b_model_spec_is_downloadable():
    """EngAixt-7B 已发布到 ModelScope，必须能下载，而不是只认本地文件。

    指向第 128 步（控制符强化版）的导出文件；更长的训练会在第 128 步之后过拟合，
    实测留出集整批 JSON 契约从 15/15 掉到 1/15，所以这里锁的是峰值权重。
    """
    spec = model_spec("EngAixt-7B-Q4_K_M")

    assert spec.filename == "EngAixt-7B-Q4_K_M.gguf"
    assert spec.size == 4_624_648_800
    assert spec.sha256 == (
        "4f8aeb8e0a41e6c0e8438686863fd9f7af390d4c8c2a1c8cb1e38b45a659c551"
    )
    assert spec.source_url.startswith("https://www.modelscope.cn/models/ENGAOXT/")
    assert spec.source_url.endswith("/" + spec.filename)
    assert spec.license_url.startswith("https://www.modelscope.cn/models/ENGAOXT/")
    # 本地模型不能成为默认值，否则新用户开箱即失败
    assert DEFAULT_MODEL_NAME != "EngAixt-7B-Q4_K_M"


def test_every_model_source_url_matches_its_declared_filename():
    """下载直链末尾的文件名必须与 filename 逐字一致（含大小写）。

    ModelScope 的对象路径区分大小写，URL 与 filename 漂移会让用户下载 404，
    而这类错误在只检查 endswith("...Q8_0.gguf") 的旧断言下是查不出来的。
    """
    for name, spec in MODEL_SPECS.items():
        if not spec.source_url:
            continue
        assert spec.source_url.rsplit("/", 1)[-1] == spec.filename, (
            f"{name}: URL 文件名与 filename 不一致，下载会失败"
        )


def test_no_model_url_duplicates_a_modelscope_namespace():
    """防止把账号名拼到已含账号名的常量后面（会拼出 .../Tencent-Hunyuan/ENGAOXT/...）。"""
    for name, spec in MODEL_SPECS.items():
        url = spec.source_url
        if not url:
            continue
        assert url.count("/models/") == 1, f"{name}: 直链里 /models/ 出现多次"
        tail = url.split("/models/", 1)[1]
        assert tail.count("/") >= 2, f"{name}: 直链缺少 账号/仓库 两段"
        owner, repo = tail.split("/")[:2]
        assert owner.count("-") <= 2 and "Tencent-Hunyuan" not in repo, (
            f"{name}: 疑似账号名重复拼接 -> {owner}/{repo}"
        )


def test_engaixt_7b_description_claims_only_measured_numbers():
    """描述里的指标必须是实测到的，不能出现未经验证的宣称。"""
    description = model_spec("EngAixt-7B-Q4_K_M").description

    assert "212/212" in description
    assert "99.71%" in description
    assert "99.17%" in description
    # 旧文案里未经证实的“保留率 100%”与已失效的本地-only 说法不得复现
    assert "保留率 100%" not in description
    assert "无下载地址" not in description


def test_hy_mt2_models_have_distinct_files_and_invalid_config_falls_back():
    light = model_spec("Hy-MT2-1.8B-Q4_K_M")
    quality = model_spec("HY-MT2-7B-Q8_0")

    assert model_path(light.name).name != model_path(quality.name).name
    assert light.filename != quality.filename
    assert resolve_model_name("unknown-model") == DEFAULT_MODEL_NAME
    assert DEFAULT_MODEL_NAME in MODEL_SPECS


def test_hy_mt2_discovers_matching_external_model_without_copying(tmp_path, monkeypatch):
    spec = HyMt2ModelSpec(
        name="external-test",
        label="External test",
        filename="external-test.gguf",
        size=4,
        sha256="3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
        source_url="https://example.invalid/model.gguf",
        license_url="https://example.invalid/license",
        description="test model",
    )
    external = tmp_path / "hymt2" / "models" / spec.filename
    external.parent.mkdir(parents=True)
    external.write_bytes(b"data")
    monkeypatch.setattr(hy_mt2_component, "_local_model_search_roots", lambda: (tmp_path,))
    hy_mt2_component._local_model_discovery_cache.clear()

    row = hy_mt2_component._model_status(spec, {})

    assert row["ready"] is False
    assert row["candidate_path"] == str(external.resolve())
    manifest = {"models": {spec.name: {"sha256": spec.sha256, "path": str(external)}}}
    registered = hy_mt2_component._model_status(spec, manifest)
    assert registered["ready"] is True
    assert registered["path"] == str(external)


def test_hy_mt2_reuses_existing_cuda_runtime_without_manifest(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("llama-server.exe", "ggml-cuda.dll", "cudart64_12.dll"):
        (runtime / name).write_bytes(b"runtime")
    monkeypatch.setattr(hy_mt2_component, "component_dir", lambda: tmp_path)
    monkeypatch.setattr(
        hy_mt2_component,
        "detect_graphics_adapters",
        lambda: [{"name": "NVIDIA Tesla T10", "pnp_device_id": "PCI\\VEN_10DE"}],
    )

    status = hy_mt2_component.component_status("HY-MT2-7B-Q8_0")

    assert status["runner"] == "cuda-12.4"
    assert status["runtime_ready"] is True
    assert status["hardware_matches"] is True


def test_hy_mt2_model_selection_changes_translator_cache_identity():
    translator = HyMt2Translator()

    assert translator._model(Config(hy_mt2_model="Hy-MT2-1.8B-Q4_K_M")) == "Hy-MT2-1.8B-Q4_K_M"
    assert translator._model(Config(hy_mt2_model="HY-MT2-7B-Q8_0")) == "HY-MT2-7B-Q8_0"
    assert translator._model(Config(hy_mt2_model="invalid")) == DEFAULT_MODEL_NAME


def test_hy_mt2_deployment_verification_generates_and_releases_runtime(monkeypatch):
    runtime = HyMt2Runtime()
    stopped = []

    def start():
        runtime._active_model = "HY-MT2-7B-Q8_0"
        runtime._active_backend = "cuda"
        runtime._port = 43123

    monkeypatch.setattr(runtime, "ensure_started", start)
    monkeypatch.setattr(
        runtime,
        "_request",
        lambda _messages, _max_tokens: LocalCompletion(
            "READY", prompt_tokens=4, completion_tokens=1, elapsed_seconds=0.2
        ),
    )
    monkeypatch.setattr(runtime, "stop", lambda: stopped.append(True))

    result = runtime.verify_selected_model()

    assert result["model"] == "HY-MT2-7B-Q8_0"
    assert result["backend"] == "cuda"
    assert result["completion_tokens"] == 1
    assert stopped == [True]


def test_hy_mt2_runtime_reads_streaming_sse_and_reports_deltas():
    class StreamResponse:
        def __init__(self):
            self.lines = iter([
                b'data: {"choices":[{"delta":{"content":"\\u4f60\\u597d"},"finish_reason":null}]}\n',
                b'data: {"choices":[{"delta":{"content":"\\uff0c\\u4e16\\u754c"},"finish_reason":null}]}\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4}}\n',
                b'data: [DONE]\n',
            ])

        def readline(self):
            return next(self.lines, b"")

    deltas = []
    result = HyMt2Runtime._read_stream_response(
        StreamResponse(),
        time.perf_counter(),
        deltas.append,
    )

    assert result.content == "你好，世界"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 4
    assert deltas == [1, 1]
