"""翻译代理服务器 smoke test — 不调用真实 API。

验证点:
1. ThreadingHTTPServer 并发：慢速 API 调用不阻塞缓存/词典命中请求
2. 内置词典命中（无需 API key）
3. API 失败（返回 None）→ 回退原文且不写缓存
4. 请求去重：同一文本并发只调一次"API"
5. 成功译文写入缓存，二次请求走缓存

用法: python tests/test_proxy_smoke.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.translation_server as ts

PORT = 15999
BASE = f"http://127.0.0.1:{PORT}"

_api_calls: list[str] = []
_api_lock = threading.Lock()


def _fake_translate(text, from_lang, to_lang, provider):
    """替身 API：记录调用并模拟 1 秒延迟。"""
    with _api_lock:
        _api_calls.append(text)
    if text == "FAIL_ME":
        return None  # 模拟 API 故障
    time.sleep(1.0)
    return f"译[{text}]"


def _get(text: str) -> str:
    with urlopen(f"{BASE}/translate?text={quote(text)}&from=ja&to=zh", timeout=10) as r:
        return r.read().decode("utf-8")


def main():
    # 替换 API 层与启动期后台任务，避免真实 API 调用与磁盘缓存干扰
    ts._translate_via_ai = _fake_translate
    ts._batch_pretranslate = lambda: None
    ts._prewarm_session = lambda provider: None
    ts._load_persistent_cache = lambda: None
    ts._save_persistent_cache = lambda: None
    ts._mark_persistent_dirty = lambda: None

    server = ts.TranslationProxyServer(port=PORT, provider="deepseek")
    assert server.start(), "服务器启动失败"
    time.sleep(0.3)

    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"{status}: {name}" + (f" ({detail})" if detail else ""))
        if not cond:
            failures.append(name)

    # 1. health
    with urlopen(f"{BASE}/health", timeout=5) as r:
        check("health 端点", r.read() == b"OK")

    # 2. 内置词典命中（不经过 API）
    check("内置词典命中", _get("設定") == "设置")
    check("词典命中不调 API", len(_api_calls) == 0)

    # 3. 并发：1 个慢速 API 请求 + 8 个词典请求同时发出
    #    单线程服务器下词典请求会被阻塞 ~1s；多线程下应 <0.5s 完成
    slow_done = threading.Event()
    threading.Thread(target=lambda: (_get("ゆっくり文章"), slow_done.set()), daemon=True).start()
    time.sleep(0.1)  # 确保慢请求先进入 API 层
    t0 = time.time()
    threads = [threading.Thread(target=_get, args=("保存",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dict_elapsed = time.time() - t0
    check("慢速 API 不阻塞词典请求", dict_elapsed < 0.5, f"{dict_elapsed:.2f}s")
    slow_done.wait(timeout=5)

    # 4. 成功译文写入缓存：二次请求不再调 API
    n_before = len(_api_calls)
    r1 = _get("ゆっくり文章")
    check("成功译文已缓存", r1 == "译[ゆっくり文章]" and len(_api_calls) == n_before, r1)

    # 5. 请求去重：同一新文本并发 5 个请求只调一次 API
    results = []
    res_lock = threading.Lock()

    def _dedup_get():
        r = _get("新しいテキスト")
        with res_lock:
            results.append(r)

    n_before = len(_api_calls)
    threads = [threading.Thread(target=_dedup_get) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    n_calls = len(_api_calls) - n_before
    check("并发去重只调一次 API", n_calls == 1, f"实际 {n_calls} 次")
    check("去重等待者拿到译文", all(r == "译[新しいテキスト]" for r in results), str(set(results)))

    # 6. API 失败 → 回退原文且不缓存（重试会再次调 API）
    n_before = len(_api_calls)
    r1 = _get("FAIL_ME")
    r2 = _get("FAIL_ME")
    n_calls = len(_api_calls) - n_before
    check("API 失败回退原文", r1 == "FAIL_ME" and r2 == "FAIL_ME", f"{r1!r}/{r2!r}")
    check("失败结果未污染缓存", n_calls == 2, f"两次请求调了 {n_calls} 次 API")

    server.stop()
    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("全部通过！")


if __name__ == "__main__":
    main()
