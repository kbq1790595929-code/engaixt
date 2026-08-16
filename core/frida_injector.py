"""Frida 进程注入管理 — 挂起启动、注入探针、捕获密钥、终止进程"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from utils.logger import info, warning, debug


# Windows API 常量
CREATE_SUSPENDED = 0x00000004
PROCESS_ALL_ACCESS = 0x001F0FFF
THREAD_ALL_ACCESS = 0x001F03FF


class FridaInjectError(Exception):
    """Frida 注入失败。"""


class FridaTimeoutError(FridaInjectError):
    """等待密钥超时。"""


class FridaProcessInfo:
    """挂起进程的句柄信息。"""
    def __init__(self, pid: int, h_process: int, h_thread: int):
        self.pid = pid
        self.h_process = h_process
        self.h_thread = h_thread


def launch_suspended(exe_path: Path) -> FridaProcessInfo:
    """以 CREATE_SUSPENDED 方式启动进程。返回进程信息。

    进程在内存中已创建但尚未执行任何代码，等待 ResumeThread 后开始运行。
    """
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    # STARTUPINFOW 结构
    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", wintypes.LPBYTE),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()

    exe_str = str(exe_path)
    cwd_str = str(exe_path.parent)

    info(f"挂起启动: {exe_str}")

    ret = kernel32.CreateProcessW(
        None,                                   # lpApplicationName
        ctypes.create_unicode_buffer(exe_str),  # lpCommandLine
        None,                                   # lpProcessAttributes
        None,                                   # lpThreadAttributes
        False,                                  # bInheritHandles
        CREATE_SUSPENDED,                       # dwCreationFlags
        None,                                   # lpEnvironment
        ctypes.create_unicode_buffer(cwd_str),  # lpCurrentDirectory
        ctypes.byref(si),
        ctypes.byref(pi),
    )

    if not ret:
        err = ctypes.get_last_error()
        raise FridaInjectError(f"CreateProcessW 失败 (错误码: {err})")

    info(f"进程已挂起创建: PID={pi.dwProcessId}")
    return FridaProcessInfo(
        pid=pi.dwProcessId,
        h_process=pi.hProcess,
        h_thread=pi.hThread,
    )


def resume_process(proc_info: FridaProcessInfo):
    """恢复挂起进程的主线程。"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    ret = kernel32.ResumeThread(proc_info.h_thread)
    if ret == -1:
        err = ctypes.get_last_error()
        warning(f"ResumeThread 失败 (错误码: {err})")


def terminate_process(proc_info: FridaProcessInfo):
    """强制终止进程并关闭句柄。"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.TerminateProcess(proc_info.h_process, 0)
    kernel32.CloseHandle(proc_info.h_process)
    kernel32.CloseHandle(proc_info.h_thread)
    info(f"进程 PID={proc_info.pid} 已终止")


def inject_and_capture_key(
    proc_info: FridaProcessInfo,
    probe_js_path: Path,
    timeout: float = 60.0,
) -> str | None:
    """向挂起进程注入 Frida 探针，恢复运行，等待密钥捕获。

    返回捕获到的 AES-256 密钥（hex 字符串），超时返回 None。

    实现用户建议的 "零延迟 IPC"：
    - JS 探针捕获密钥后立即 send({type:'KEY', value: hexString})
    - Python 端收到 KEY 消息后立即存值，瞬间 detach + 终止进程
    """
    try:
        import frida
    except ImportError:
        raise FridaInjectError(
            "未安装 frida-tools。请运行: pip install frida-tools\n"
            "Frida 是内存 Hook 框架，用于捕获加密 Godot 游戏的 AES 密钥。"
        )

    probe_js = probe_js_path.read_text(encoding="utf-8")
    captured_key: str | None = None
    candidates: list[str] = []
    key_event = threading.Event()

    info(f"Frida 注入进程 PID={proc_info.pid} ...")
    device = frida.get_local_device()
    session = device.attach(proc_info.pid)
    script = session.create_script(probe_js)

    def on_message(message, data):
        nonlocal captured_key

        if message['type'] == 'send':
            payload = message['payload']
            msg_type = payload.get('type', '')

            if msg_type == 'KEY':
                key = payload.get('value', '')
                source = payload.get('source', 'unknown')
                info(f"密钥捕获! 来源: {source} 密钥: {key}")
                captured_key = key
                key_event.set()

            elif msg_type == 'CANDIDATE':
                val = payload.get('value', '')
                if val and val not in candidates:
                    candidates.append(val)
                # 收集到候选后，再等 5 秒看是否有更确切的 KEY 消息
                if len(candidates) == 1:
                    threading.Timer(5.0, key_event.set).start()

            elif msg_type == 'log':
                debug(f"[Frida] {payload.get('message', '')}")

            elif msg_type == 'READY':
                info("Frida 探针就绪")

        elif message['type'] == 'error':
            warning(f"Frida 脚本错误: {message.get('description', '')}")

    script.on('message', on_message)
    script.load()
    info("Frida 探针已注入，恢复游戏进程...")

    # 恢复进程，游戏开始执行
    resume_process(proc_info)

    # 等待密钥捕获（带超时）
    captured = key_event.wait(timeout=timeout)

    # 无论是否捕获到，先终止进程
    try:
        script.unload()
    except Exception:
        pass
    try:
        session.detach()
    except Exception:
        pass
    terminate_process(proc_info)

    if captured and captured_key:
        info(f"成功捕获密钥: {captured_key}")
        return captured_key

    if candidates:
        # 梯队 4 找到了候选密钥，但梯队 1-3 没命中
        info(f"未通过 API Hook 捕获密钥，但内存扫描发现 {len(candidates)} 个候选")
        # 返回第一个候选密钥供尝试
        return candidates[0]

    warning(f"超时 ({timeout}s) 未捕获到密钥")
    return None
