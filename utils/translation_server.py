"""XUAT 翻译代理服务 — 本地 HTTP 服务器，接收 XUAT CustomTranslate 请求，
转发到云端 API 或本地翻译器，返回翻译结果。

XUAT CustomTranslate 协议:
  POST /translate
  Content-Type: application/json
  {"text": "原文", "from": "ja", "to": "zh"}

  响应: "译文"  (纯文本)

用法:
  python utils/translation_server.py --port 5120 --provider deepseek
  或从 xunity engine 自动启动。
"""

from __future__ import annotations

import json
import os
import queue
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
import threading
import time

from utils.logger import info, warning, error, debug

# RPG Maker hook 数据交换（HTTP 通信，不依赖 require('fs')）
_hook_scan_data: list[dict] = []       # hook POST /_hook_scan 写入
_hook_translation_map: dict[str, str] = {}  # hook GET /_hook_map 读取

# 翻译缓存 — 避免重复调用 API（游戏中同一文本常反复出现）
# LRU 驱逐：记录访问序，满时淘汰最旧条目而非清空全部
# 服务器为多线程（ThreadingHTTPServer），所有缓存/访问序操作必须持 _cache_lock
_translation_cache: dict[str, str] = {}
_cache_access_order: list[str] = []  # 最近使用的在末尾
_cache_lock = threading.RLock()
CACHE_MAX_SIZE = 5000


def _cache_get(key: str) -> str | None:
    """读缓存并更新 LRU 序。未命中返回 None。"""
    with _cache_lock:
        if key not in _translation_cache:
            return None
        try:
            _cache_access_order.remove(key)
        except ValueError:
            pass
        _cache_access_order.append(key)
        return _translation_cache[key]


def _cache_put(key: str, value: str):
    """写缓存（满时先 LRU 驱逐），并标记待持久化。"""
    with _cache_lock:
        while len(_translation_cache) >= CACHE_MAX_SIZE and _cache_access_order:
            oldest = _cache_access_order.pop(0)
            _translation_cache.pop(oldest, None)
        if key in _translation_cache:
            try:
                _cache_access_order.remove(key)
            except ValueError:
                pass
        _translation_cache[key] = value
        _cache_access_order.append(key)
    _mark_persistent_dirty()


def _cache_seed(key: str, value: str):
    """预加载写入（来自磁盘已有译文）：不触发驱逐、不标记持久化。"""
    with _cache_lock:
        if key not in _translation_cache:
            _cache_access_order.append(key)
        _translation_cache[key] = value


def _cache_contains(*keys: str) -> bool:
    """检查任一 key 是否已在缓存（不更新 LRU 序）。"""
    with _cache_lock:
        return any(k in _translation_cache for k in keys)

# 请求去重：防止同一文本并发多次调用 API（缓存击穿）
# key → Event，首个请求持有 None 表示"正在翻译中"，后续请求 wait() 等待
_pending_requests: dict[str, threading.Event] = {}
_pending_lock = threading.Lock()

# 预取索引 — 文本在游戏文件中的顺序
# _prefetch_index: {text: (file, line)} 用于定位
# _file_order: {file: [text1, text2, ...]} 按行号排序的文本列表
_prefetch_index: dict[str, tuple] = {}
_file_order: dict[str, list] = {}

# 运行时文本顺序记录 — 录制玩家遇到的文本，用于下次启动时的预取
# _text_sequence: 按出现顺序记录的文本列表（_text_sequence_seen 为去重索引）
_text_sequence: list[str] = []
_text_sequence_seen: set[str] = set()
_text_sequence_file: Path | None = None
SEQUENCE_PREFETCH_AHEAD = 10  # 预取后续 N 条
MAX_TEXT_SEQUENCE = 20000     # 录制上限，防止长会话内存无界增长


def _record_sequence(text: str):
    """记录文本出现顺序（去重，超过上限不再录制）。"""
    if text in _text_sequence_seen or len(_text_sequence) >= MAX_TEXT_SEQUENCE:
        return
    _text_sequence_seen.add(text)
    _text_sequence.append(text)


# 预取任务队列 — 由单个常驻 worker 串行消费，
# 替代旧版"每个请求 spawn 一个预取线程"（线程无界 + 绕过限速）
_prefetch_queue: "queue.Queue[str]" = queue.Queue(maxsize=200)
_prefetch_worker_started = False
_prefetch_worker_lock = threading.Lock()


def _enqueue_prefetch(text: str):
    """请求处理器调用：把"刚翻译过的文本"丢给预取 worker。队列满则丢弃。"""
    _ensure_prefetch_worker()
    try:
        _prefetch_queue.put_nowait(text)
    except queue.Full:
        pass


def _ensure_prefetch_worker():
    global _prefetch_worker_started
    with _prefetch_worker_lock:
        if _prefetch_worker_started:
            return
        threading.Thread(
            target=_prefetch_worker_loop, daemon=True, name="prefetch-worker"
        ).start()
        _prefetch_worker_started = True


def _prefetch_worker_loop():
    while True:
        text = _prefetch_queue.get()
        try:
            _prefetch_upcoming(text, SEQUENCE_PREFETCH_AHEAD)
        except Exception as e:
            debug(f"[预取] worker 异常: {e}")

# HTTP 连接池 — 复用 TLS 连接减少延迟
_deepseek_session = None

def _make_session() -> "requests.Session":
    """创建不走任何代理的 requests Session。"""
    import requests
    sess = requests.Session()
    sess.trust_env = False
    # 显式禁用所有代理（覆盖环境变量和系统设置）
    sess.proxies.update({"http": None, "https": None, "all": None})
    return sess

# 速率限制 — 防止 XUAT 每帧渲染时短时间大量 API 调用
# Unity 每帧可能有几十个 UI 文本同时触发翻译，需要平滑限速
_rate_limit_lock = threading.Lock()
_rate_limit_window: list[float] = []  # 最近 N 次 API 调用的时间戳
RATE_LIMIT_MAX_CALLS = 10      # 每秒最多 10 次 API 调用
RATE_LIMIT_WINDOW = 1.0        # 滑动窗口 1 秒

# 缓存统计
_cache_hits = 0
_cache_misses = 0
_cache_dedups = 0  # 去重等待命中数（并发请求被合并）
_stats_lock = threading.Lock()

# XUnity.AutoTranslator uses U+180E as an invisible redirected-resource marker.
# Returning it from the proxy makes translated UGUI text identifiable on the
# next Text.set_text hook, which prevents re-entrant translation loops.
_XUAT_REDIRECT_MARKER = "\u180e"


class TranslationProxyServer:
    """翻译代理服务器 — 非阻塞后台运行。"""

    def __init__(self, port: int = 5120, provider: str = "deepseek"):
        self.port = port
        self.provider = provider
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @staticmethod
    def _kill_zombie_on_port(port: int):
        """杀掉占用指定端口的僵尸代理进程（Windows）。

        排除当前进程 PID，避免自杀导致闪退。
        """
        try:
            import subprocess, os
            if os.name != "nt":
                return
            current_pid = str(os.getpid())
            # 找到监听该端口的 PID
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10
            )
            pids: set[str] = set()
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        pid = parts[-1]
                        if pid != current_pid:
                            pids.add(pid)
            # 强杀
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                               capture_output=True, timeout=5)
            if pids:
                debug(f"[翻译代理] 清理了端口 {port} 上的 {len(pids)} 个僵尸进程 (排除自身 PID {current_pid})")
        except Exception:
            pass  # 静默失败，不影响启动

    def start(self):
        """启动服务器（后台线程，随进程存活）。"""
        # 杀掉占用端口的僵尸进程
        self._kill_zombie_on_port(self.port)

        # 从磁盘恢复翻译缓存（避免重启后重复调用 API）
        _load_persistent_cache()
        _start_persistent_save_loop()

        handler = _make_handler(self.provider)
        try:
            # 多线程服务器：缓存命中请求不会被慢速 API 调用阻塞，
            # 请求去重/速率限制机制也因此真正生效
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
            self._server.daemon_threads = True
        except OSError as e:
            warning(f"无法启动翻译代理服务器 (端口 {self.port}): {e}")
            return False

        # 非 daemon 线程，确保进程不退出时代理持续运行
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=False)
        self._thread.start()
        self._running = True

        # 加载运行时录制顺序并启动定期保存（不依赖 API key）
        _load_text_sequence()
        _start_sequence_save_loop()

        # 预热 API 连接（后台线程，不阻塞启动）
        threading.Thread(target=_prewarm_session, args=(self.provider,), daemon=True).start()
        # 批量预翻译游戏文本（后台线程）
        threading.Thread(target=_batch_pretranslate, daemon=True).start()

        info(f"翻译代理服务器已启动: http://127.0.0.1:{self.port}/translate (provider={self.provider})")
        return True

    def stop(self):
        """停止服务器。"""
        if self._server:
            self._server.shutdown()
            self._running = False
            _save_persistent_cache()  # 退出前落盘，避免丢失最近 60 秒的缓存
            info("翻译代理服务器已停止")

    def wait(self):
        """阻塞等待服务器线程结束（通常伴随游戏进程退出）。"""
        if self._thread:
            self._thread.join()


def _load_text_sequence():
    """启动时加载上次录制的文本顺序。"""
    global _text_sequence, _text_sequence_seen, _text_sequence_file
    seq_file = Path.home() / "Downloads" / ".game_translator" / "text_sequence.json"
    _text_sequence_file = seq_file
    if seq_file.exists():
        try:
            data = json.loads(seq_file.read_text(encoding="utf-8"))
            _text_sequence = data.get("sequence", [])[:MAX_TEXT_SEQUENCE]
            _text_sequence_seen = set(_text_sequence)
            info(f"[预取] 加载录制顺序: {len(_text_sequence)} 条")
        except Exception as e:
            debug(f"[预取] 加载录制顺序失败: {e}")


def _start_sequence_save_loop():
    """后台线程：每 30 秒保存一次录制顺序。"""
    def _loop():
        while True:
            time.sleep(30)
            if _text_sequence and _text_sequence_file:
                try:
                    _text_sequence_file.parent.mkdir(parents=True, exist_ok=True)
                    _text_sequence_file.write_text(
                        json.dumps({"sequence": _text_sequence}, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
                    debug(f"[预取] 序列已保存: {len(_text_sequence)} 条")
                except Exception:
                    pass
    threading.Thread(target=_loop, daemon=True, name="seq-saver").start()


def _build_prefetch_index():
    """构建预取索引 — 从 checkpoint JSON 读取文本顺序。"""
    downloads = Path.home() / "Downloads"
    for ckpt in downloads.glob(".game_translator/workspaces/*/translation_checkpoint.json"):
        try:
            data = json.loads(ckpt.read_text(encoding="utf-8"))
            items = data.get("items", [])
            if not items:
                continue

            # 按文件分组，按行号排序
            file_groups: dict[str, list] = {}
            for item in items:
                fname = item.get("file", "")
                line = item.get("line", 0)
                orig = item.get("original", "").strip()
                if not orig:
                    continue
                if fname not in file_groups:
                    file_groups[fname] = []
                file_groups[fname].append((line, orig))

            # 排序并存储
            count = 0
            for fname, entries in file_groups.items():
                entries.sort(key=lambda x: x[0])
                _file_order[fname] = [e[1] for e in entries]
                for line, orig in entries:
                    _prefetch_index[orig] = (fname, line)
                    count += 1

            info(f"[预取] 索引 {count} 条文本 (来自 {len(file_groups)} 个文件)")
            return  # 只处理第一个找到的 checkpoint
        except Exception as e:
            debug(f"[预取] 构建索引失败: {e}")


def _prefetch_upcoming(text: str, count: int = 10):
    """当某个文本被翻译时，预取后续的 N 条文本。

    优先使用 checkpoint 索引（精确文件/行号），
    回退到运行时录制顺序。在预取 worker 线程中同步执行。
    """
    upcoming = []

    # 策略1：从 checkpoint 索引按文件顺序预取
    if text in _prefetch_index:
        fname, line = _prefetch_index[text]
        if fname in _file_order:
            ordered = _file_order[fname]
            try:
                idx = ordered.index(text)
                for i in range(idx + 1, min(idx + 1 + count, len(ordered))):
                    t = ordered[i]
                    if not _cache_contains(t, f"en:zh:{t}"):
                        upcoming.append(t)
            except ValueError:
                pass

    # 策略2：从运行时顺序预取（录制模式）
    if not upcoming and text in _text_sequence_seen:
        try:
            idx = _text_sequence.index(text)
            for i in range(idx + 1, min(idx + 1 + count, len(_text_sequence))):
                t = _text_sequence[i]
                if not _cache_contains(t, f"en:zh:{t}"):
                    upcoming.append(t)
        except ValueError:
            pass

    if upcoming:
        _prefetch_texts(upcoming)


def _prefetch_texts(texts: list):
    """翻译一批文本并写入缓存（预取 worker 内串行执行，与实时请求共享速率限制）。"""
    global _deepseek_session

    # 复用或创建 session
    if _deepseek_session is not None:
        sess = _deepseek_session
    else:
        sess = _make_session()
        config = _load_config()
        if config:
            api_key = config.deepseek_api_key or config.openai_api_key
            if api_key:
                sess.headers.update({
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                })

    translated = 0
    for text in texts:
        cache_key = f"en:zh:{text}"
        if _cache_contains(text, cache_key):
            continue
        try:
            _check_rate_limit()
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "将游戏文本翻译成自然的中文。只输出译文。"},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.15,
                "max_tokens": 128,
            }
            resp = sess.post(
                "https://api.deepseek.com/v1/chat/completions",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            _cache_put(cache_key, result)
            translated += 1
        except Exception:
            pass

    if translated:
        info(f"[预取] 后台翻译 +{translated} 条")


def _batch_pretranslate():
    """后台批量预加载游戏翻译缓存 — 启动后翻译体验全走缓存。"""
    import requests
    config = _load_config()
    api_key = None
    if config:
        api_key = config.deepseek_api_key or config.openai_api_key
    if not api_key:
        return

    # 0. 构建预取索引
    _build_prefetch_index()

    # 1. 加载所有已存在的翻译文件
    downloads = Path.home() / "Downloads"
    for game_dir in downloads.iterdir():
        if not game_dir.is_dir():
            continue
        for zh_file in game_dir.glob("BepInEx/Translation/zh/Translation_zh.txt"):
            try:
                for line in zh_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            en_text, zh_text = parts[0].strip(), parts[1].strip()
                            if en_text and zh_text:
                                _cache_seed(f"en:zh:{en_text}", zh_text.lstrip(_XUAT_REDIRECT_MARKER))
                info(f"[预加载] {zh_file.parent.parent.name}: {len(_translation_cache)} 条缓存")
            except Exception:
                pass

    # 2. 也加载 _AutoGeneratedTranslations.txt
    for game_dir in downloads.iterdir():
        if not game_dir.is_dir():
            continue
        for auto_file in game_dir.glob("BepInEx/Translation/zh/Text/_AutoGeneratedTranslations.txt"):
            try:
                count = 0
                for line in auto_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            en_text, zh_text = parts[0].strip(), parts[1].strip()
                            if en_text and zh_text:
                                _cache_seed(f"en:zh:{en_text}", zh_text.lstrip(_XUAT_REDIRECT_MARKER))
                                count += 1
                if count:
                    info(f"[预加载] AutoGen: +{count} 条")
            except Exception:
                pass

    info(f"[预加载] 总计 {len(_translation_cache)} 条翻译缓存就绪")

    # 3. 后台批量翻译未缓存文本
    untranslated = []
    # 检查 workspace 目录
    for ckpt in downloads.glob(".game_translator/workspaces/*/translation_checkpoint.json"):
        try:
            data = json.loads(ckpt.read_text(encoding="utf-8"))
            for key, val in data.items():
                src = val.get("source", "")
                if src and not _cache_contains(src, f"en:zh:{src}"):
                    untranslated.append(src)
        except Exception:
            pass

    if untranslated:
        info(f"[预加载] 开始后台翻译 {len(untranslated)} 条未缓存文本...")
        _batch_translate_all(untranslated, api_key)
        info(f"[预加载] 后台翻译完成，缓存总计 {len(_translation_cache)} 条")


def _batch_translate_all(texts: list, api_key: str):
    """后台批量翻译文本列表（逐个调用 API，与实时请求共享速率限制）。"""
    sess = _make_session()
    sess.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    translated = 0
    for i, text in enumerate(texts):
        cache_key = f"en:zh:{text}"
        if _cache_contains(text, cache_key):
            continue
        try:
            _check_rate_limit()
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "将游戏文本翻译成自然的中文。只输出译文。"},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.15,
                "max_tokens": 128,
            }
            resp = sess.post(
                "https://api.deepseek.com/v1/chat/completions",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            _cache_put(cache_key, result)
            translated += 1
            if translated % 10 == 0:
                info(f"[预加载] 进度: {translated}/{len(texts)}")
        except Exception:
            pass

    if translated:
        info(f"[预加载] 后台翻译完成: +{translated} 条")


def _prewarm_session(provider: str):
    """启动时预热所选翻译器，不发送实际翻译请求。"""
    global _deepseek_session
    try:
        if provider == "hy_mt2":
            from utils.local_translation_bridge import prewarm_local_provider

            prewarm_local_provider(provider)
            info("[翻译代理] Hy-MT2 本地模型预热完成")
            return
        import requests
        config = _load_config()
        api_key = None
        if config:
            api_key = config.deepseek_api_key or config.openai_api_key
        if not api_key:
            return
        session = requests.Session()
        session.trust_env = False
        session.proxies.update({"http": None, "https": None})
        session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        # 仅 TCP/TLS 握手预热，不发真实请求（避免浪费 token）
        # 通过访问 /v1/models 免费端点来建立连接池
        try:
            session.get(
                "https://api.deepseek.com/v1/models",
                timeout=5,
            )
        except Exception:
            # models 端点失败也不影响，连接池已建立
            pass
        _deepseek_session = session
        info("[翻译代理] API 连接预热完成")
    except Exception as e:
        debug(f"[翻译代理] 预热失败（非致命）: {e}")


def _sanitize_translation_output(text: str) -> str:
    """清洗译文：移除首部所有不可见/控制字符。"""
    # Unicode 类别：Cc=控制字符, Cf=格式字符, Mn=非间距标记
    import unicodedata
    chars = list(text)
    strip_count = 0
    while chars and unicodedata.category(chars[0]) in ('Cc', 'Cf', 'Mn'):
        if chars[0] not in ('\n', '\r', '\t'):
            strip_count += 1
        chars.pop(0)
    if strip_count:
        debug(f"[翻译代理] 清洗了译文首部 {strip_count} 个不可见字符")
    return ''.join(chars)


def _add_u180e(result: str, to_lang: str) -> str:
    """清除译文首部的所有不可见字符（U+180E、零宽空格、BOM 等）。
    XUAT 自己处理重定向检测，代理不加标记。"""
    return _sanitize_translation_output(result)


def _check_rate_limit() -> float:
    """速率限制：如果超限则 sleep 到窗口有空位。返回等待秒数。"""
    global _rate_limit_window
    now = time.time()
    with _rate_limit_lock:
        # 清理过期的时间戳
        cutoff = now - RATE_LIMIT_WINDOW
        _rate_limit_window = [t for t in _rate_limit_window if t > cutoff]
        if len(_rate_limit_window) >= RATE_LIMIT_MAX_CALLS:
            # 需要等待：计算最早过期时间
            wait_until = _rate_limit_window[0] + RATE_LIMIT_WINDOW
            wait = max(0, wait_until - now)
        else:
            wait = 0
        if wait == 0:
            _rate_limit_window.append(now)
    if wait > 0:
        time.sleep(wait)
        # 等待后重新记录
        with _rate_limit_lock:
            _rate_limit_window.append(time.time())
    return wait


def _log_cache_stats():
    """定期输出缓存命中率统计。"""
    with _stats_lock:
        total = _cache_hits + _cache_misses + _cache_dedups
        if total > 0 and total % 50 == 0:  # 每 50 次请求输出一次
            hit_rate = (_cache_hits + _cache_dedups) / total * 100
            info(f"[缓存统计] 总请求:{total} 命中:{_cache_hits} 去重合并:{_cache_dedups} "
                 f"API调用:{_cache_misses} 命中率:{hit_rate:.0f}%")


def _decode_body(raw: bytes) -> str:
    """尝试多种编码解码请求体（XUAT 可能用 Shift-JIS 等非 UTF-8 编码）。"""
    for enc in ["utf-8", "shift-jis", "gbk", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _cached_translate(text: str, from_lang: str, to_lang: str, provider: str) -> str:
    """带缓存的翻译 — 同一文本不重复调用 API。

    三层防护：
    1. 内存 LRU 缓存命中 → 直接返回
    2. 请求去重 → 同一文本并发请求只发一次 API，其余等待
    3. LRU 驱逐 → 满时淘汰最旧条目，不清空全部
    """
    global _cache_hits, _cache_misses, _cache_dedups

    if text.startswith(_XUAT_REDIRECT_MARKER):
        return text

    cache_key = f"{from_lang}:{to_lang}:{text}"

    # 空文本 / 纯空白 → 不翻译
    if not text.strip():
        return _add_u180e(text, to_lang)

    # 快速路径：缓存命中 → 更新 LRU 序并返回
    # 也尝试常见的其他源语言键（预加载缓存可能用 en:zh 存的）
    for key in [cache_key, f"en:zh:{text}", f"ja:zh:{text}", f"ko:zh:{text}"]:
        cached = _cache_get(key)
        if cached is not None:
            with _stats_lock:
                _cache_hits += 1
            _log_cache_stats()
            return _add_u180e(cached, to_lang)

    # ---- 请求去重：防止缓存击穿 ----
    with _pending_lock:
        if cache_key in _pending_requests:
            wait_event = _pending_requests[cache_key]
        else:
            wait_event = threading.Event()
            _pending_requests[cache_key] = wait_event
            wait_event = None  # 标记为首个请求

    if wait_event is not None:
        # 不是首个请求：等待首个完成
        with _stats_lock:
            _cache_dedups += 1
        _log_cache_stats()
        wait_event.wait(timeout=30)
        # 等待完成后从缓存读取；首个请求失败时缓存为空 → 回退原文
        for key in [cache_key, f"en:zh:{text}", f"ja:zh:{text}", f"ko:zh:{text}"]:
            cached = _cache_get(key)
            if cached is not None:
                return _add_u180e(cached, to_lang)
        return _add_u180e(text, to_lang)

    # 首个请求：调用 API（带速率限制）
    try:
        with _stats_lock:
            _cache_misses += 1
        _log_cache_stats()

        # 本地运行器由自身请求锁控制，不受云端 API 速率限制。
        wait = 0.0 if provider == "hy_mt2" else _check_rate_limit()
        if wait > 0.1:
            debug(f"[速率限制] API 调用限速等待 {wait:.1f}s")

        # 记录文本出现顺序（用于下次启动的预取）
        _record_sequence(text)

        result = _translate_via_ai(text, from_lang, to_lang, provider)
        if result is None:
            # API 调用失败（网络/限流/未配置 key）：返回原文但不写缓存，
            # 否则瞬时故障会把"原文当译文"永久固化，该句此后永远不再翻译
            return _add_u180e(text, to_lang)

        # 安全过滤器回退原文时，不写缓存（否则身份映射会永久跳过翻译）
        if result == text:
            return _add_u180e(text, to_lang)

        result = _add_u180e(result, to_lang)
        _cache_put(cache_key, result)
        return result
    finally:
        # 通知所有等待此文本的请求
        with _pending_lock:
            ev = _pending_requests.pop(cache_key, None)
        if ev is not None:
            ev.set()


# 持久化缓存路径 — 代理重启后恢复翻译，避免重新调用 API
_PERSISTENT_CACHE_PATH = Path.home() / "Downloads" / ".game_translator" / "proxy_cache.json"
_persistent_save_lock = threading.Lock()
_persistent_dirty = False


def _load_persistent_cache():
    """启动时从磁盘恢复翻译缓存（自动清理旧版 U+180E 前缀）。"""
    global _cache_access_order, _persistent_dirty
    if _PERSISTENT_CACHE_PATH.exists():
        try:
            data = json.loads(_PERSISTENT_CACHE_PATH.read_text(encoding="utf-8"))
            entries = data.get("entries", {})
            order = data.get("order", [])
            # 清理旧版遗留的 U+180E 前缀
            cleaned = {}
            for k, v in entries.items():
                if isinstance(v, str):
                    v = v.lstrip(_XUAT_REDIRECT_MARKER)
                cleaned[k] = v
            with _cache_lock:
                _translation_cache.update(cleaned)
                _cache_access_order = [k for k in order if k in _translation_cache]
                for k in cleaned:
                    if k not in _cache_access_order:
                        _cache_access_order.append(k)
            info(f"[缓存持久化] 从磁盘恢复 {len(cleaned)} 条翻译缓存")
            _persistent_dirty = False
        except Exception as e:
            warning(f"[缓存持久化] 恢复失败: {e}")


def _save_persistent_cache():
    """将内存缓存写入磁盘（去抖：每 60 秒最多写一次）。"""
    global _persistent_dirty
    with _persistent_save_lock:
        if not _persistent_dirty:
            return
        try:
            _PERSISTENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _cache_lock:
                data = {
                    "entries": dict(_translation_cache),
                    "order": list(_cache_access_order),
                }
            tmp = str(_PERSISTENT_CACHE_PATH) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, str(_PERSISTENT_CACHE_PATH))
            _persistent_dirty = False
        except Exception as e:
            debug(f"[缓存持久化] 写入失败: {e}")


def _mark_persistent_dirty():
    """标记缓存已变更，需要写盘。"""
    global _persistent_dirty
    _persistent_dirty = True


def _start_persistent_save_loop():
    """后台线程：每 60 秒检查并持久化缓存（仅在有变更时写入）。"""
    def _loop():
        while True:
            time.sleep(60)
            _save_persistent_cache()
    threading.Thread(target=_loop, daemon=True, name="cache-saver").start()


def _make_handler(provider: str):
    class _Handler(BaseHTTPRequestHandler):
        provider_name = provider

        def log_message(self, format, *args):
            debug(f"[翻译代理] {format % args}")

        def _handle_hook_scan(self):
            """接收 RPG Maker hook 扫描到的文本数据。"""
            global _hook_scan_data
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(content_length)
                body = _decode_body(raw_body)
                data = json.loads(body)
                items = data.get("items", [])
                game_name = data.get("game", "unknown")
                _hook_scan_data.clear()
                _hook_scan_data.extend(items)
                info(f"[RPG Hook] 收到扫描数据: {len(items)} 条 (游戏: {game_name})")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "count": len(items)}).encode("utf-8"))
            except Exception as e:
                error(f"[RPG Hook] 扫描数据处理失败: {e}")
                self.send_error(500, str(e))

        def _handle_hook_map(self):
            """向 RPG Maker hook 提供翻译映射表。"""
            global _hook_translation_map
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(_hook_translation_map, ensure_ascii=False).encode("utf-8"))

        def do_POST(self):
            # RPG Maker hook scan endpoint
            if self.path == "/_hook_scan":
                self._handle_hook_scan()
                return

            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(content_length)

                # XUAT 可能用 UTF-8、Shift-JIS、或系统 ANSI 编码发送请求体
                body = _decode_body(raw_body)

                # Try JSON first, then form-encoded
                text = ""
                from_lang = "ja"
                to_lang = "zh"
                content_type = self.headers.get("Content-Type", "")

                if "json" in content_type:
                    req = json.loads(body)
                    text = req.get("text", "")
                    from_lang = req.get("from", from_lang)
                    to_lang = req.get("to", to_lang)
                elif "form" in content_type or "x-www-form-urlencoded" in content_type:
                    from urllib.parse import parse_qs
                    params = parse_qs(body)
                    text = params.get("text", [""])[0]
                    from_lang = params.get("from", [from_lang])[0]
                    to_lang = params.get("to", [to_lang])[0]
                else:
                    # Try JSON anyway
                    try:
                        req = json.loads(body)
                        text = req.get("text", "")
                        from_lang = req.get("from", from_lang)
                        to_lang = req.get("to", to_lang)
                    except json.JSONDecodeError:
                        # Treat entire body as text
                        text = body.strip()

                if not text:
                    self.send_error(400, "Missing text")
                    return

                t0 = time.time()
                translated = _cached_translate(text, from_lang, to_lang, self.provider_name)
                elapsed = (time.time() - t0) * 1000
                info(f"[翻译代理] {elapsed:.0f}ms | {text[:40]}... -> {translated[:40]}...")

                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(translated.encode("utf-8"))

                # 触发预取：交给常驻 worker 翻译后续对话
                _enqueue_prefetch(text)

            except Exception as e:
                error(f"[翻译代理] 请求处理失败: {e}")
                import traceback
                traceback.print_exc()
                self.send_error(500, str(e))

        def do_GET(self):
            # RPG Maker hook: serve translation map
            if self.path == "/_hook_map":
                self._handle_hook_map()
                return

            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
                return

            # XUAT CustomTranslate sends GET with query parameters
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                text = params.get("text", [""])[0]
                from_lang = params.get("from", ["ja"])[0]
                to_lang = params.get("to", ["zh"])[0]

                info(f"[翻译代理] GET {self.path} text={text[:80] if text else '(none)'}")

                if not text:
                    self.send_error(400, "Missing text")
                    return

                t0 = time.time()
                translated = _cached_translate(text, from_lang, to_lang, self.provider_name)
                elapsed = (time.time() - t0) * 1000
                info(f"[翻译代理] {elapsed:.0f}ms | {text[:40]}... -> {translated[:40]}...")

                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(translated.encode("utf-8"))

                # 触发预取：交给常驻 worker 翻译后续对话
                _enqueue_prefetch(text)

            except Exception as e:
                error(f"[翻译代理] GET 请求处理失败: {e}")
                import traceback
                traceback.print_exc()
                self.send_error(500, str(e))

    return _Handler


def _translate_via_ai(text: str, from_lang: str, to_lang: str, provider: str) -> str | None:
    """通过 AI API 翻译文本。失败返回 None（调用方不得缓存）。"""

    if provider == "deepseek":
        return _call_deepseek(text, from_lang, to_lang)
    elif provider == "openai":
        return _call_openai(text, from_lang, to_lang)
    elif provider == "anthropic":
        return _call_anthropic(text, from_lang, to_lang)
    elif provider == "hy_mt2":
        try:
            from utils.local_translation_bridge import translate_with_local_provider

            return translate_with_local_provider(text, from_lang, to_lang, provider)
        except Exception as exc:
            error(f"[翻译代理] Hy-MT2 本地翻译失败: {exc}")
            return None
    else:
        return None  # 未知 provider，不做翻译也不缓存


def _load_config():
    """加载全局配置。"""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config import get_config
        return get_config()
    except Exception:
        return None


def _sanitize_translation(text: str, result: str, from_lang: str, to_lang: str) -> str:
    """清洗 AI 译文：去除元标签，检测模型拒译（英文指令替代译文）。"""
    for tag in ["[中文] ", "[Chinese] ", "[ZH] "]:
        result = result.removeprefix(tag)
    # 目标中文但结果几乎全是 ASCII → 模型在发指令而非翻译
    if to_lang.startswith("zh") and len(result) > 5:
        ascii_count = sum(1 for c in result if ord(c) < 128)
        if ascii_count / len(result) > 0.85:
            return text
    return result


def _call_deepseek(text: str, from_lang: str, to_lang: str) -> str | None:
    """调用 DeepSeek API 翻译（复用 HTTP 连接以减少延迟）。失败返回 None。"""
    global _deepseek_session
    config = _load_config()
    api_key = config.deepseek_api_key if config else None
    if not api_key and config:
        api_key = config.openai_api_key
    if not api_key:
        warning("[翻译代理] DeepSeek API key 未配置")
        return None

    lang_map = {"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语"}
    from_name = lang_map.get(from_lang, from_lang)
    to_name = lang_map.get(to_lang, to_lang)

    max_tok = max(64, min(256, len(text) * 3))

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": (
                "你是一个专业游戏本地化翻译引擎。"
                "你的唯一功能：接收日文游戏文本，输出自然流畅的中文译文。"
                "核心要求："
                "译文必须像母语者写的一样自然，读起来完全感觉不到翻译痕迹；"
                "对话口语化、叙述文学化、UI简洁化，匹配原文风格；"
                "准确理解语境，不死译不硬译不逐字对应；"
                "专有名词、人名、地名保留原文不翻译；"
                "只输出纯中文译文，不要任何解释、标注或前缀。"
            )},
            {"role": "user", "content": f"翻译成自然中文：\n\n{text}"},
        ],
        "temperature": 0.15,
        "max_tokens": max_tok,
    }

    if _deepseek_session is None:
        # 预热未完成时降级为临时 session
        import requests
        sess = _make_session()
        sess.trust_env = False  # 不走系统代理
        sess.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
    else:
        sess = _deepseek_session

    try:
        t0 = time.time()
        resp = sess.post(
            "https://api.deepseek.com/v1/chat/completions",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip()
        elapsed = (time.time() - t0) * 1000

        # 校验译文质量：拦截模型拒绝翻译/聊天回复
        result = _sanitize_translation(text, result, from_lang, to_lang)

        info(f"[API] DeepSeek {elapsed:.0f}ms | {text[:30]}... -> {result[:30]}...")
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        error(f"[翻译代理] DeepSeek API 调用失败 ({elapsed:.0f}ms): {e}")
        return None


def _call_openai(text: str, from_lang: str, to_lang: str) -> str | None:
    config = _load_config()
    api_key = config.openai_api_key if config else None
    if not api_key:
        return None

    lang_map = {"ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean"}
    from_name = lang_map.get(from_lang, from_lang)
    to_name = lang_map.get(to_lang, to_lang)

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": f"Translate from {from_name} to {to_name}. Output translation only."},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
    }).encode("utf-8")

    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        error(f"[翻译代理] OpenAI API 调用失败: {e}")
        return None


def _call_anthropic(text: str, from_lang: str, to_lang: str) -> str | None:
    config = _load_config()
    api_key = config.anthropic_api_key if config else None
    if not api_key:
        return None

    lang_map = {"ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean"}
    from_name = lang_map.get(from_lang, from_lang)
    to_name = lang_map.get(to_lang, to_lang)

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4096,
        "system": f"Translate from {from_name} to {to_name}. Output translation only.",
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")

    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result["content"][0]["text"].strip()
    except Exception as e:
        error(f"[翻译代理] Anthropic API 调用失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def start_server(port: int = 5120, provider: str = "deepseek") -> TranslationProxyServer | None:
    """启动翻译代理服务器。失败返回 None。"""
    server = TranslationProxyServer(port=port, provider=provider)
    if server.start():
        return server
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="XUAT 翻译代理")
    parser.add_argument("--port", type=int, default=5120)
    parser.add_argument(
        "--provider",
        default="deepseek",
        choices=["deepseek", "openai", "anthropic", "hy_mt2"],
    )
    args = parser.parse_args()

    print(f"启动 XUAT 翻译代理: http://127.0.0.1:{args.port}/translate (provider={args.provider})")
    server = start_server(args.port, args.provider)
    if server:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
            print("服务器已停止")
