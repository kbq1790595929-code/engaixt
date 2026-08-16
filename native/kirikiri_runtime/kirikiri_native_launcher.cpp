#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <shellapi.h>

#include <string>
#include <vector>

namespace {

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
    MessageBoxW(nullptr, message.c_str(), L"KiriKiri Native Launcher", MB_ICONERROR | MB_OK);
}

bool InjectDll(HANDLE process, const std::wstring &dllPath) {
    size_t bytes = (dllPath.size() + 1) * sizeof(wchar_t);
    void *remote = VirtualAllocEx(process, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote) return false;
    if (!WriteProcessMemory(process, remote, dllPath.c_str(), bytes, nullptr)) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        return false;
    }
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
    auto loadLibraryW = reinterpret_cast<LPTHREAD_START_ROUTINE>(GetProcAddress(kernel, "LoadLibraryW"));
    HANDLE thread = CreateRemoteThread(process, nullptr, 0, loadLibraryW, remote, 0, nullptr);
    if (!thread) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        return false;
    }
    WaitForSingleObject(thread, 15000);
    DWORD remoteModule = 0;
    GetExitCodeThread(thread, &remoteModule);
    CloseHandle(thread);
    VirtualFreeEx(process, remote, 0, MEM_RELEASE);
    return remoteModule != 0;
}

std::wstring HookReadyEventName(DWORD processId) {
    return L"Local\\EngAixt_KiriKiriHookReady_" + std::to_wstring(processId);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    std::wstring selfDir = GetSelfDir();
    std::wstring exePath = GetExeFromArgs();
    bool waitForGame = HasArg(L"--wait");
    if (exePath.empty()) {
        ErrorBox(L"Game executable not found.\nPass the exe path to kirikiri_native_launcher.exe.");
        return 1;
    }
    if (!FileExists(exePath)) {
        ErrorBox(L"Game executable not found.");
        return 1;
    }

    std::wstring dllPath = JoinPath(selfDir, L"kirikiri_native_hook.dll");
    if (!FileExists(dllPath)) {
        dllPath = JoinPath(JoinPath(selfDir, L"_translation_meta"), L"kirikiri_native_hook.dll");
    }
    if (!FileExists(dllPath)) {
        ErrorBox(L"kirikiri_native_hook.dll not found.");
        return 1;
    }

    std::wstring gameDir = DirName(exePath);
    std::wstring commandLine = QuoteArg(exePath);
    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};
    std::vector<wchar_t> cmd(commandLine.begin(), commandLine.end());
    cmd.push_back(0);

    if (!CreateProcessW(exePath.c_str(), cmd.data(), nullptr, nullptr, FALSE, CREATE_SUSPENDED, nullptr, gameDir.c_str(), &si, &pi)) {
        ErrorBox(L"Failed to start game executable.");
        return 1;
    }

    std::wstring readyEventName = HookReadyEventName(pi.dwProcessId);
    HANDLE readyEvent = CreateEventW(nullptr, TRUE, FALSE, readyEventName.c_str());
    if (!readyEvent) {
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        ErrorBox(L"Failed to create KiriKiri hook readiness event.");
        return 1;
    }

    if (!InjectDll(pi.hProcess, dllPath)) {
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(readyEvent);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        ErrorBox(L"Failed to inject kirikiri_native_hook.dll.");
        return 1;
    }

    DWORD readyResult = WaitForSingleObject(readyEvent, 15000);
    CloseHandle(readyEvent);
    if (readyResult != WAIT_OBJECT_0) {
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        ErrorBox(L"KiriKiri hook initialization timed out before the game was resumed.");
        return 1;
    }
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    if (waitForGame) {
        WaitForSingleObject(pi.hProcess, INFINITE);
    }
    CloseHandle(pi.hProcess);
    return 0;
}
