#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <shellapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <fstream>
#include <set>
#include <string>
#include <vector>

namespace {

std::wstring gLogPath;
std::wstring gTargetPath;

std::string Utf8(const std::wstring &value) {
    int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string out(size, '\0');
    if (size) WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size, nullptr, nullptr);
    return out;
}

void Log(const std::wstring &value) {
    HANDLE file = CreateFileW(gLogPath.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    std::string bytes = Utf8(value + L"\n");
    DWORD written = 0;
    WriteFile(file, bytes.data(), static_cast<DWORD>(bytes.size()), &written, nullptr);
    CloseHandle(file);
}

void WriteTarget(DWORD pid, const std::wstring &path, const wchar_t *state) {
    std::wstring temp = gTargetPath + L".tmp";
    HANDLE file = CreateFileW(temp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    std::string bytes = Utf8(std::to_wstring(pid) + L"\t" + state + L"\t" + path + L"\n");
    DWORD written = 0;
    WriteFile(file, bytes.data(), static_cast<DWORD>(bytes.size()), &written, nullptr);
    CloseHandle(file);
    MoveFileExW(temp.c_str(), gTargetPath.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
}

std::wstring ProcessPath(HANDLE process) {
    wchar_t buffer[32768] = {};
    DWORD size = 32768;
    if (!QueryFullProcessImageNameW(process, 0, buffer, &size)) return {};
    return std::wstring(buffer, size);
}

std::wstring QuoteArg(const std::wstring &arg) {
    std::wstring out = L"\"";
    for (wchar_t ch : arg) {
        if (ch == L'"') out += L'\\';
        out += ch;
    }
    out += L"\"";
    return out;
}

std::wstring DirName(const std::wstring &path) {
    size_t pos = path.find_last_of(L"\\/");
    if (pos == std::wstring::npos) return L".";
    return path.substr(0, pos);
}

std::wstring JoinPath(const std::wstring &a, const std::wstring &b) {
    if (a.empty()) return b;
    if (a.back() == L'\\' || a.back() == L'/') return a + b;
    return a + L"\\" + b;
}

std::wstring GetSelfDir() {
    wchar_t buf[MAX_PATH * 4] = {};
    DWORD n = GetModuleFileNameW(nullptr, buf, static_cast<DWORD>(MAX_PATH * 4));
    if (!n) return L".";
    return DirName(std::wstring(buf, n));
}

bool FileExists(const std::wstring &path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

std::wstring GetExeFromArgs() {
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    std::wstring result;
    if (argv && argc >= 2) result = argv[1];
    if (argv) LocalFree(argv);
    return result;
}

bool HasArg(const wchar_t *needle) {
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    bool found = false;
    if (argv) {
        for (int i = 2; i < argc; ++i) {
            if (lstrcmpiW(argv[i], needle) == 0) {
                found = true;
                break;
            }
        }
        LocalFree(argv);
    }
    return found;
}

void ErrorBox(const std::wstring &message) {
    Log(L"launcher_error: " + message);
    if (HasArg(L"--quiet")) return;
    MessageBoxW(nullptr, message.c_str(), L"KiriKiri Native Launcher", MB_ICONERROR | MB_OK);
}

bool InjectDll(HANDLE process, const std::wstring &dllPath) {
    size_t bytes = (dllPath.size() + 1) * sizeof(wchar_t);
    void *remote = VirtualAllocEx(process, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote) return false;
    if (!WriteProcessMemory(process, remote, dllPath.c_str(), bytes, nullptr)) {
        DWORD error = GetLastError();
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        SetLastError(error);
        return false;
    }
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
    auto loadLibraryW = reinterpret_cast<LPTHREAD_START_ROUTINE>(GetProcAddress(kernel, "LoadLibraryW"));
    HANDLE thread = CreateRemoteThread(process, nullptr, 0, loadLibraryW, remote, 0, nullptr);
    if (!thread) {
        DWORD error = GetLastError();
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        SetLastError(error);
        return false;
    }
    DWORD result = WaitForSingleObject(thread, 15000);
    if (result != WAIT_OBJECT_0) {
        // The remote thread may still use the argument buffer after a timeout.
        CloseHandle(thread);
        SetLastError(result == WAIT_TIMEOUT ? ERROR_TIMEOUT : ERROR_GEN_FAILURE);
        return false;
    }
    DWORD remoteModule = 0;
    GetExitCodeThread(thread, &remoteModule);
    CloseHandle(thread);
    VirtualFreeEx(process, remote, 0, MEM_RELEASE);
    if (!remoteModule) SetLastError(ERROR_DLL_INIT_FAILED);
    return remoteModule != 0 && remoteModule != STILL_ACTIVE;
}

std::wstring HookReadyEventName(DWORD processId) {
    return L"Local\\EngAixt_KiriKiriHookReady_" + std::to_wstring(processId);
}

bool HookProcess(HANDLE process, DWORD pid, const std::wstring &dllPath) {
    HANDLE readyEvent = CreateEventW(nullptr, TRUE, FALSE, HookReadyEventName(pid).c_str());
    if (!readyEvent) {
        Log(L"ready_event_failed pid=" + std::to_wstring(pid) + L" win32=" + std::to_wstring(GetLastError()));
        return false;
    }
    if (!InjectDll(process, dllPath)) {
        Log(L"injection_failed pid=" + std::to_wstring(pid) + L" win32=" + std::to_wstring(GetLastError()));
        CloseHandle(readyEvent);
        return false;
    }
    DWORD readyResult = WaitForSingleObject(readyEvent, 15000);
    CloseHandle(readyEvent);
    if (readyResult != WAIT_OBJECT_0) {
        Log(L"KiriKiri hook initialization timed out before the game was resumed.");
        Log(L"hook_ready_timeout pid=" + std::to_wstring(pid) + L" wait_result=" + std::to_wstring(readyResult));
        return false;
    }
    Log(L"hook_ready pid=" + std::to_wstring(pid) + L" exe=" + ProcessPath(process));
    return true;
}

bool IsKirikiriChild(const std::wstring &path, const std::wstring &gameDir) {
    if (_wcsicmp(DirName(path).c_str(), gameDir.c_str()) != 0) return false;
    DWORD type = 0;
    if (!GetBinaryTypeW(path.c_str(), &type) || type != SCS_32BIT_BINARY) return false;
    // Only hook engine descendants, not crash reporters or unrelated helper programs.
    std::ifstream file(path.c_str(), std::ios::binary);
    if (!file) return false;
    std::string data(32 * 1024 * 1024, '\0');
    file.read(data.data(), static_cast<std::streamsize>(data.size()));
    data.resize(static_cast<size_t>(file.gcount()));
    for (const char *marker : {"TVP(KIRIKIRI)", "tTJS", "Kirikiri"}) {
        if (data.find(marker) != std::string::npos) return true;
        std::string wide;
        for (const char *p = marker; *p; ++p) {
            wide += *p;
            wide += '\0';
        }
        if (data.find(wide) != std::string::npos) return true;
    }
    return false;
}

struct TrackedProcess {
    DWORD pid;
    HANDLE handle;
    std::wstring path;
};

int FollowGame(PROCESS_INFORMATION &pi, const std::wstring &exePath, const std::wstring &dllPath) {
    std::vector<TrackedProcess> games = {{pi.dwProcessId, pi.hProcess, exePath}};
    std::set<DWORD> lineage = {pi.dwProcessId};
    std::set<DWORD> inspected = {pi.dwProcessId};
    DWORD targetPid = pi.dwProcessId;
    bool failed = false;
    ULONGLONG emptySince = 0;
    while (true) {
        HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        std::vector<PROCESSENTRY32W> entries;
        if (snapshot != INVALID_HANDLE_VALUE) {
            PROCESSENTRY32W entry = {};
            entry.dwSize = sizeof(entry);
            if (Process32FirstW(snapshot, &entry)) {
                do { entries.push_back(entry); } while (Process32NextW(snapshot, &entry));
            }
            CloseHandle(snapshot);
        }
        // Resolve the whole descendant tree even if a parent exits between polls.
        bool changed;
        do {
            changed = false;
            for (const auto &entry : entries) {
                if (lineage.count(entry.th32ParentProcessID) && !lineage.count(entry.th32ProcessID)) {
                    lineage.insert(entry.th32ProcessID);
                    changed = true;
                }
            }
        } while (changed);
        for (const auto &entry : entries) {
            DWORD pid = entry.th32ProcessID;
            if (!lineage.count(pid) || inspected.count(pid)) continue;
            inspected.insert(pid);
            HANDLE query = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
            if (!query) continue;
            std::wstring path = ProcessPath(query);
            CloseHandle(query);
            if (!IsKirikiriChild(path, DirName(exePath))) {
                continue;
            }
            HANDLE process = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ | SYNCHRONIZE, FALSE, pid);
            if (!process) {
                Log(L"child_open_failed pid=" + std::to_wstring(pid) + L" win32=" + std::to_wstring(GetLastError()) + L" exe=" + path);
                failed = true;
                continue;
            }
            Log(L"child_detected pid=" + std::to_wstring(pid) + L" parent=" +
                std::to_wstring(entry.th32ParentProcessID) + L" exe=" + path);
            bool hooked = HookProcess(process, pid, dllPath);
            failed = failed || !hooked;
            games.push_back({pid, process, path});
            if (hooked) {
                targetPid = pid;
                WriteTarget(pid, path, L"running");
                Log(L"target_changed pid=" + std::to_wstring(pid) + L" exe=" + path);
            }
        }
        bool alive = false;
        for (auto it = games.begin(); it != games.end();) {
            if (WaitForSingleObject(it->handle, 0) == WAIT_OBJECT_0) {
                DWORD code = 0;
                GetExitCodeProcess(it->handle, &code);
                Log(L"process_exit pid=" + std::to_wstring(it->pid) + L" exit_code=" + std::to_wstring(code));
                if (code != 0) failed = true;
                CloseHandle(it->handle);
                it = games.erase(it);
            } else {
                alive = true;
                ++it;
            }
        }
        if (alive) {
            emptySince = 0;
            auto current = std::find_if(games.begin(), games.end(),
                [targetPid](const TrackedProcess &game) { return game.pid == targetPid; });
            if (current == games.end()) {
                targetPid = games.back().pid;
                WriteTarget(targetPid, games.back().path, L"running");
            }
        } else {
            if (!emptySince) emptySince = GetTickCount64();
            if (GetTickCount64() - emptySince >= 2000) break;
        }
        Sleep(100);
    }
    WriteTarget(0, exePath, L"closed");
    Log(L"session_end result=" + std::wstring(failed ? L"hook_failed" : L"game_closed"));
    return failed ? 2 : 0;
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    std::wstring selfDir = GetSelfDir();
    gLogPath = JoinPath(selfDir, L"kirikiri_native_launcher.log");
    gTargetPath = JoinPath(selfDir, L"kirikiri_runtime_target.tsv");
    DeleteFileW(gLogPath.c_str());
    DeleteFileW(JoinPath(selfDir, L"kirikiri_native_hook.log").c_str());
    std::wstring exePath = GetExeFromArgs();
    bool waitForGame = HasArg(L"--wait");
    if (exePath.empty()) {
        WriteTarget(0, L"", L"failed");
        ErrorBox(L"Game executable not found.\nPass the exe path to kirikiri_native_launcher.exe.");
        return 1;
    }
    if (!FileExists(exePath)) {
        WriteTarget(0, exePath, L"failed");
        ErrorBox(L"Game executable not found.");
        return 1;
    }

    std::wstring dllPath = JoinPath(selfDir, L"kirikiri_native_hook.dll");
    if (!FileExists(dllPath)) {
        dllPath = JoinPath(JoinPath(selfDir, L"_translation_meta"), L"kirikiri_native_hook.dll");
    }
    if (!FileExists(dllPath)) {
        WriteTarget(0, exePath, L"failed");
        ErrorBox(L"kirikiri_native_hook.dll not found.");
        return 1;
    }

    std::wstring gameDir = DirName(exePath);
    Log(L"session_start exe=" + exePath);
    std::wstring commandLine = QuoteArg(exePath);
    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};
    std::vector<wchar_t> cmd(commandLine.begin(), commandLine.end());
    cmd.push_back(0);

    if (!CreateProcessW(exePath.c_str(), cmd.data(), nullptr, nullptr, FALSE, CREATE_SUSPENDED, nullptr, gameDir.c_str(), &si, &pi)) {
        Log(L"create_process_failed win32=" + std::to_wstring(GetLastError()) + L" exe=" + exePath);
        WriteTarget(0, exePath, L"failed");
        ErrorBox(L"Failed to start game executable.");
        return 1;
    }

    WriteTarget(pi.dwProcessId, exePath, L"starting");
    if (!HookProcess(pi.hProcess, pi.dwProcessId, dllPath)) {
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        WriteTarget(0, exePath, L"failed");
        ErrorBox(L"Failed to initialize kirikiri_native_hook.dll. See kirikiri_native_launcher.log.");
        return 1;
    }

    if (ResumeThread(pi.hThread) == static_cast<DWORD>(-1)) {
        Log(L"resume_failed pid=" + std::to_wstring(pi.dwProcessId) + L" win32=" + std::to_wstring(GetLastError()));
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        WriteTarget(0, exePath, L"failed");
        return 1;
    }
    WriteTarget(pi.dwProcessId, exePath, L"running");
    Log(L"game_resumed pid=" + std::to_wstring(pi.dwProcessId));
    CloseHandle(pi.hThread);
    if (waitForGame) {
        return FollowGame(pi, exePath, dllPath);
    }
    CloseHandle(pi.hProcess);
    return 0;
}
