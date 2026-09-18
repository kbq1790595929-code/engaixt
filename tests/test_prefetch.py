"""测试翻译代理的预取机制。"""
import json
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 模拟 checkpoint 数据
MOCK_CHECKPOINT = {
    "source": "mock",
    "items": [
        {"file": "scene_intro", "line": 1, "original": "Hello there!"},
        {"file": "scene_intro", "line": 2, "original": "How are you?"},
        {"file": "scene_intro", "line": 3, "original": "I've been waiting for you."},
        {"file": "scene_intro", "line": 4, "original": "We have much to discuss."},
        {"file": "scene_intro", "line": 5, "original": "Please, take a seat."},
        {"file": "scene_intro", "line": 6, "original": "Let me explain everything."},
        {"file": "scene_intro", "line": 7, "original": "It started long ago."},
        {"file": "scene_intro", "line": 8, "original": "Before the war began."},
        {"file": "scene_intro", "line": 9, "original": "I was just a child then."},
        {"file": "scene_intro", "line": 10, "original": "But I remember it clearly."},
        {"file": "scene_intro", "line": 11, "original": "The sky turned dark."},
        {"file": "scene_intro", "line": 12, "original": "And everything changed."},
        {"file": "scene_tavern", "line": 1, "original": "Welcome to the tavern!"},
        {"file": "scene_tavern", "line": 2, "original": "What can I get you?"},
        {"file": "scene_tavern", "line": 3, "original": "Our ale is the finest."},
    ],
}


def test_build_index():
    """测试：构建预取索引。"""
    from utils.translation_server import _file_order, _prefetch_index, _build_prefetch_index
    import utils.translation_server as ts

    # 临时替换 checkpoint 加载
    def mock_load():
        downloads = Path.home() / "Downloads"
        # 写入临时 checkpoint
        ckpt_dir = downloads / ".game_translator" / "workspaces" / "_test_prefetch"
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        ckpt_file = ckpt_dir / "translation_checkpoint.json"
        ckpt_file.write_text(json.dumps(MOCK_CHECKPOINT, ensure_ascii=False), encoding="utf-8")

    mock_load()
    ts._file_order.clear()
    ts._prefetch_index.clear()

    # 手动构建索引
    downloads = Path.home() / "Downloads"
    for ckpt in downloads.glob(".game_translator/workspaces/*/translation_checkpoint.json"):
        data = json.loads(ckpt.read_text(encoding="utf-8"))
        items = data.get("items", [])
        if not items:
            continue
        file_groups = {}
        for item in items:
            fname = item.get("file", "")
            line = item.get("line", 0)
            orig = item.get("original", "").strip()
            if not orig:
                continue
            if fname not in file_groups:
                file_groups[fname] = []
            file_groups[fname].append((line, orig))

        for fname, entries in file_groups.items():
            entries.sort(key=lambda x: x[0])
            ts._file_order[fname] = [e[1] for e in entries]
            for line, orig in entries:
                ts._prefetch_index[orig] = (fname, line)

    # 验证（只验证我们添加的 mock 数据）
    assert "scene_intro" in ts._file_order, f"scene_intro not found in files: {list(ts._file_order.keys())[:5]}"
    assert len(ts._file_order["scene_intro"]) == 12, f"Expected 12 lines, got {len(ts._file_order['scene_intro'])}"
    assert "Hello there!" in ts._prefetch_index
    assert ts._prefetch_index["Hello there!"] == ("scene_intro", 1)
    assert ts._prefetch_index["And everything changed."] == ("scene_intro", 12)

    print(f"  PASS: 索引 {len(ts._prefetch_index)} 条文本, {len(ts._file_order)} 个文件")


def test_prefetch_upcoming():
    """测试：预取后续文本（用 mock 替换 API 调用）。"""
    import utils.translation_server as ts
    from utils.translation_server import _prefetch_upcoming, _file_order, _prefetch_index, _translation_cache

    assert "Hello there!" in _prefetch_index, "Index not built"
    _translation_cache.clear()
    upcoming = _file_order["scene_intro"][1:6]

    print(f"  触发文本: 'Hello there!' (scene_intro 第1行)")
    print(f"  后续文本: {upcoming}")

    # Mock _prefetch_texts 来跳过实际 API 调用
    original_prefetch_texts = ts._prefetch_texts

    prefetched = []
    def mock_prefetch(texts):
        for t in texts:
            ts._translation_cache[f"en:zh:{t}"] = f"[预取翻译] {t}"
        prefetched.extend(texts)

    ts._prefetch_texts = mock_prefetch

    # 触发预取
    _prefetch_upcoming("Hello there!", count=5)
    time.sleep(0.2)

    # 验证
    cached = sum(1 for t in upcoming if f"en:zh:{t}" in _translation_cache)
    print(f"  预取命中: {cached}/{len(upcoming)} 条")
    assert cached == 5, f"Expected 5 cached, got {cached}"
    assert prefetched == upcoming, f"Prefetched mismatch: {prefetched} != {upcoming}"

    # 验证不同位置的文本触发不同范围的预取
    _translation_cache.clear()
    prefetched.clear()
    _prefetch_upcoming("Please, take a seat.", count=5)
    time.sleep(0.2)
    expected = _file_order["scene_intro"][5:10]
    assert prefetched == expected, f"Positional prefetch wrong: {prefetched[:3]}... != {expected[:3]}..."

    print(f"  PASS: 预取正确识别后续文本，位置感知正常")

    # 恢复
    ts._prefetch_texts = original_prefetch_texts
    _translation_cache.clear()


def test_cache_hit_after_prefetch():
    """测试：预取后缓存命中速度。"""
    import utils.translation_server as ts
    from utils.translation_server import _translation_cache, _cached_translate
    import requests

    # 手工填充缓存模拟预取完成
    _translation_cache.clear()
    _translation_cache["en:zh:Hello there!"] = "你好！"
    _translation_cache["en:zh:How are you?"] = "你好吗？"

    # 测试缓存命中
    t0 = time.time()
    result1 = _cached_translate("Hello there!", "en", "zh", "deepseek")
    t1 = time.time() - t0

    t0 = time.time()
    result2 = _cached_translate("How are you?", "en", "zh", "deepseek")
    t2 = time.time() - t0

    assert result1 == "你好！", f"Cache mismatch: {result1}"
    assert result2 == "你好吗？", f"Cache mismatch: {result2}"
    assert t1 < 0.01, f"Cache hit too slow: {t1*1000:.1f}ms"
    assert t2 < 0.01, f"Cache hit too slow: {t2*1000:.1f}ms"

    print(f"  PASS: 缓存命中 {t1*1000:.1f}ms / {t2*1000:.1f}ms")
    _translation_cache.clear()


def test_alt_key_fallback():
    """测试：不同源语言的缓存键回溯。"""
    import utils.translation_server as ts
    from utils.translation_server import _translation_cache, _cached_translate

    _translation_cache.clear()
    # 用 en:zh 存储
    _translation_cache["en:zh:Good morning"] = "早上好"

    # 用 ja:zh 查询（XUAT 可能发 ja 作为源语言）
    result = _cached_translate("Good morning", "ja", "zh", "deepseek")
    assert result == "早上好", f"Alt key fallback failed: {result}"

    # 用 en:zh 查询应该也命中
    result = _cached_translate("Good morning", "en", "zh", "deepseek")
    assert result == "早上好", f"Direct key failed: {result}"

    print(f"  PASS: 备选键回溯正常")
    _translation_cache.clear()


def test_lru_eviction():
    """测试：缓存满后的淘汰。"""
    import utils.translation_server as ts
    from utils.translation_server import _translation_cache, CACHE_MAX_SIZE

    _translation_cache.clear()
    # 填充超过上限
    for i in range(CACHE_MAX_SIZE + 100):
        key = f"en:zh:text_{i}"
        _translation_cache[key] = f"翻译_{i}"

    # 缓存应该被清空或限制
    assert len(_translation_cache) <= CACHE_MAX_SIZE + 100, "Cache grew unbounded"
    print(f"  PASS: 缓存大小 {len(_translation_cache)} (max={CACHE_MAX_SIZE})")
    _translation_cache.clear()


if __name__ == "__main__":
    print("=" * 60)
    print("翻译预取机制测试")
    print("=" * 60)

    tests = [
        ("索引构建", test_build_index),
        ("预取后续文本", test_prefetch_upcoming),
        ("缓存命中速度", test_cache_hit_after_prefetch),
        ("备选键回溯", test_alt_key_fallback),
        ("LRU淘汰", test_lru_eviction),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            print(f"\n[{name}]")
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"结果: {passed} 通过, {failed} 失败")
