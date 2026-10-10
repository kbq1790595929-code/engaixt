import base64
import struct
import queue
from pathlib import Path

from config import Config
from core.translation_overlay_window import (
    append_dialogue_history,
    decode_overlay_payload,
    format_dialogue_history_entry,
    runtime_dialogue_parts,
    split_speaker_text,
    TranslationOverlayWindow,
)


def test_split_speaker_text_from_kag_visible_quote() -> None:
    assert split_speaker_text("外村善治「今日はいい天気だ」") == ("外村善治", "今日はいい天気だ")


def test_split_speaker_text_from_colon_line() -> None:
    assert split_speaker_text("メルヴィ：もう大丈夫") == ("メルヴィ", "もう大丈夫")


def test_split_speaker_text_leaves_plain_dialogue_unchanged() -> None:
    assert split_speaker_text("「これは普通の台詞」") == ("", "「これは普通の台詞」")


def test_runtime_dialogue_parts_translate_body_only_for_dialogue_lines() -> None:
    assert runtime_dialogue_parts("外村善治「今日はいい天気だ」") == (
        "外村善治",
        "今日はいい天気だ",
        "今日はいい天気だ",
    )


def test_runtime_dialogue_parts_prefers_explicit_native_speaker() -> None:
    assert runtime_dialogue_parts("外村善治「今日はいい天気だ」", "真　鈴") == (
        "真鈴",
        "今日はいい天気だ",
        "今日はいい天気だ",
    )


def test_runtime_dialogue_parts_keeps_plain_dialogue_as_translation_key() -> None:
    assert runtime_dialogue_parts("「これは普通の台詞」", "翼") == (
        "翼",
        "「これは普通の台詞」",
        "「これは普通の台詞」",
    )


def _payload(seq: int, original: str, translated: str = "", speaker: str = "") -> bytes:
    original_bytes = original.encode("utf-8")
    translated_bytes = translated.encode("utf-8")
    speaker_bytes = speaker.encode("utf-8")
    data = bytearray(65536)
    offset = 0
    struct.pack_into("I", data, offset, seq)
    offset += 4
    struct.pack_into("I", data, offset, len(original_bytes))
    offset += 4
    data[offset:offset + len(original_bytes)] = original_bytes
    offset += len(original_bytes)
    struct.pack_into("I", data, offset, len(translated_bytes))
    offset += 4
    data[offset:offset + len(translated_bytes)] = translated_bytes
    offset += len(translated_bytes)
    if speaker:
        struct.pack_into("I", data, offset, len(speaker_bytes))
        offset += 4
        data[offset:offset + len(speaker_bytes)] = speaker_bytes
    return bytes(data)


def test_decode_overlay_payload_v1_without_speaker() -> None:
    assert decode_overlay_payload(_payload(7, "こんにちは", "你好")) == (7, "こんにちは", "你好", "")


def test_decode_overlay_payload_v2_with_speaker() -> None:
    assert decode_overlay_payload(_payload(8, "こんにちは", "你好", "真　鈴")) == (8, "こんにちは", "你好", "真鈴")


def test_dialogue_history_updates_pending_line_with_translation() -> None:
    history: list[dict] = []
    assert append_dialogue_history(
        history,
        {"speaker": "真　鈴", "original": "こんにちは", "translated": "", "timestamp": 1},
        limit=10,
    )
    assert append_dialogue_history(
        history,
        {"speaker": "真鈴", "original": "こんにちは", "translated": "你好", "timestamp": 2},
        limit=10,
    )

    assert len(history) == 1
    assert history[0]["speaker"] == "真鈴"
    assert history[0]["translated"] == "你好"


def test_dialogue_history_skips_immediate_duplicate_capture() -> None:
    history: list[dict] = []
    first = {"speaker": "", "original": "同じ行", "translated": "同一行", "timestamp": 1}
    duplicate = {"speaker": "", "original": "同じ行", "translated": "同一行", "timestamp": 2}

    assert append_dialogue_history(history, first, limit=10)
    assert not append_dialogue_history(history, duplicate, limit=10)

    assert len(history) == 1
    assert history[0]["timestamp"] == 2


def test_format_dialogue_history_entry_contains_original_and_translation() -> None:
    text = format_dialogue_history_entry(
        {"speaker": "真鈴", "original": "こんにちは", "translated": "你好", "timestamp": 1}
    )

    assert "真鈴" in text
    assert "原文：こんにちは" in text
    assert "译文：你好" in text


def test_overlay_live_translation_uses_active_translator_before_deepseek_fallback() -> None:
    source = Path("core/translation_overlay_window.py").read_text(encoding="utf-8")

    assert "active_translator" in source
    assert "create_translator(active)" in source
    assert 'create_translator("deepseek")' in source
    assert "live_translator_provider" in source
    assert "translate_realtime_text" in source
    assert "RollingTranslationContext" in source
    assert '"context": self._translation_context.snapshot()' in source


def test_overlay_embedding_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())

    overlay = TranslationOverlayWindow("测试游戏", "game.exe")

    assert overlay._embedded is False


def test_overlay_skips_invalid_persistent_translation_map(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    game = tmp_path / "game.exe"
    game.write_bytes(b"")
    meta = game.parent / "_translation_meta"
    meta.mkdir()

    def pair(source: str, translated: str) -> str:
        return "\t".join(
            (
                base64.b64encode(source.encode("utf-8")).decode("ascii"),
                base64.b64encode(translated.encode("utf-8")).decode("ascii"),
            )
        )

    (meta / "kirikiri_native_map.tsv").write_text(
        "# base64_utf8_original\tbase64_utf8_translated\n"
        + pair("こんにちは", "こんにちは")
        + "\n"
        + pair("今日はいい天気です", "今天天气很好")
        + "\n",
        encoding="ascii",
    )

    overlay = TranslationOverlayWindow("测试游戏", "game.exe", str(game))

    assert overlay.local_translations == {"今日はいい天気です": "今天天气很好"}


def test_overlay_keeps_ellipsis_without_request_or_polluted_cache(monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    overlay = TranslationOverlayWindow("game", "game.exe")
    overlay.local_translations["『…………』"] = "garbage"
    overlay._enqueue_text("『…………』", "garbage", 7)
    item = overlay.text_queue.get_nowait()
    assert item["translated"] == "『…………』"
    assert overlay.translate_queue.empty()
    assert overlay._translation_context.snapshot() == []
    overlay._request_live_translation("……")
    assert overlay.translate_queue.empty()


def test_overlay_late_result_updates_history_not_newer_silence(monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    overlay = TranslationOverlayWindow("game", "game.exe")
    overlay._enqueue_text("あれは何だろう。", "", 7)
    overlay._enqueue_text("『…………』", "", 8)
    overlay._enqueue_text("あれは何だろう。", "那是什么呢。", 8, translation_result=True)
    items = [overlay.text_queue.get_nowait() for _ in range(3)]
    assert items[-1]["history_only"] is True
    assert [item for item in items if not item["history_only"]][-1]["original"] == "『…………』"


def test_overlay_repeated_native_capture_accepts_current_result(monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    overlay = TranslationOverlayWindow("game", "game.exe")
    overlay._enqueue_text("あれは何だろう。", "", 7)
    overlay._enqueue_text("あれは何だろう。", "", 20)
    overlay._enqueue_text("あれは何だろう。", "那是什么呢。", 20, translation_result=True)
    items = [overlay.text_queue.get_nowait() for _ in range(3)]
    assert items[-1]["history_only"] is False


def test_overlay_worker_ignores_queued_punctuation(monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    overlay = TranslationOverlayWindow("game", "game.exe")
    overlay.running = True
    overlay.translate_queue.put({"original": "……"})
    requests = []
    monkeypatch.setattr(overlay, "_translate_live_text", lambda *args, **kwargs: requests.append(args))
    real_get = overlay.translate_queue.get
    def get_once(*args, **kwargs):
        try:
            return real_get(block=False)
        except queue.Empty:
            overlay.running = False
            raise
    monkeypatch.setattr(overlay.translate_queue, "get", get_once)
    overlay._translation_worker_loop()
    assert requests == []


def test_overlay_skips_polluted_punctuation_map(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    meta = tmp_path / "_translation_meta"
    meta.mkdir()
    source = base64.b64encode("『…………』".encode()).decode()
    translated = base64.b64encode("『嗯嗯嗯』".encode()).decode()
    (meta / "kirikiri_native_map.tsv").write_text(f"{source}\t{translated}\n", encoding="ascii")
    overlay = TranslationOverlayWindow("game", "game.exe", str(tmp_path / "game.exe"))
    assert overlay.local_translations == {}


def test_overlay_follows_runtime_child_instead_of_original_exe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    target = tmp_path / "kirikiri_runtime_target.tsv"
    monkeypatch.setenv("KIRIKIRI_RUNTIME_TARGET_FILE", str(target))
    overlay = TranslationOverlayWindow("游戏", "entry.exe", str(tmp_path / "entry.exe"))
    overlay.game_pid = 7
    overlay.game_hwnd = 123
    monkeypatch.setattr(overlay, "_game_process_alive", lambda: True)
    target.write_text(f"42\trunning\t{tmp_path / 'game.exe'}\n", encoding="utf-8")

    assert overlay._runtime_target_running() is True
    assert overlay.game_pid == 42
    assert overlay.game_hwnd is None


def test_overlay_waits_for_runtime_child_during_handoff(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    target = tmp_path / "kirikiri_runtime_target.tsv"
    monkeypatch.setenv("KIRIKIRI_RUNTIME_TARGET_FILE", str(target))
    overlay = TranslationOverlayWindow("游戏", "entry.exe", str(tmp_path / "entry.exe"))
    target.write_text(f"7\trunning\t{tmp_path / 'entry.exe'}\n", encoding="utf-8")
    monkeypatch.setattr(overlay, "_game_process_alive", lambda: False)
    clock = [100.0]
    monkeypatch.setattr("core.translation_overlay_window.time.monotonic", lambda: clock[0])

    assert overlay._runtime_target_running() is True
    clock[0] += 2.0
    assert overlay._runtime_target_running() is True
    target.write_text(f"42\trunning\t{tmp_path / 'game.exe'}\n", encoding="utf-8")
    monkeypatch.setattr(overlay, "_game_process_alive", lambda: True)
    assert overlay._runtime_target_running() is True
    assert overlay.game_pid == 42


def test_overlay_closes_only_after_runtime_session_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    target = tmp_path / "kirikiri_runtime_target.tsv"
    monkeypatch.setenv("KIRIKIRI_RUNTIME_TARGET_FILE", str(target))
    overlay = TranslationOverlayWindow("游戏", "entry.exe", str(tmp_path / "entry.exe"))
    target.write_text(f"0\tclosed\t{tmp_path / 'entry.exe'}\n", encoding="utf-8")

    assert overlay._runtime_target_running() is False


def test_overlay_rejects_runtime_target_outside_game_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.translation_overlay_window.get_config", lambda: Config())
    target = tmp_path / "kirikiri_runtime_target.tsv"
    monkeypatch.setenv("KIRIKIRI_RUNTIME_TARGET_FILE", str(target))
    overlay = TranslationOverlayWindow("游戏", "entry.exe", str(tmp_path / "game" / "entry.exe"))
    target.write_text(f"42\trunning\t{tmp_path / 'other' / 'game.exe'}\n", encoding="utf-8")

    assert overlay._runtime_target_running() is None
    assert overlay.game_pid is None
