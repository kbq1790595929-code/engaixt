#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <fstream>
#include <string>
#include <vector>

namespace {

std::wstring DirName(const std::wstring &path) {
    const size_t pos = path.find_last_of(L"\\/");
    return pos == std::wstring::npos ? L"." : path.substr(0, pos);
}

std::wstring JoinPath(const std::wstring &a, const std::wstring &b) {
    if (a.empty()) return b;
    if (a.back() == L'\\' || a.back() == L'/') return a + b;
    return a + L"\\" + b;
}

std::wstring SelfDir() {
    wchar_t buffer[32768] = {};
    const DWORD size = GetModuleFileNameW(nullptr, buffer, 32768);
    return size ? DirName(std::wstring(buffer, size)) : L".";
}

bool Exists(const std::wstring &path) {
    const DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

std::wstring ReadTarget(const std::wstring &gameDir) {
    const std::wstring config = JoinPath(JoinPath(gameDir, L"_translation_meta"), L"kirikiri_launcher_target.txt");
    std::ifstream file(config.c_str(), std::ios::binary);
    if (!file) return {};
    std::string raw((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    while (!raw.empty() && (raw.back() == '\r' || raw.back() == '\n' || raw.back() == ' ' || raw.back() == '\t')) raw.pop_back();
    if (raw.empty()) return {};
    int size = MultiByteToWideChar(CP_UTF8, 0, raw.data(), static_cast<int>(raw.size()), nullptr, 0);
    if (size <= 0) return {};
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, raw.data(), static_cast<int>(raw.size()), out.data(), size);
    return out;
}

std::wstring QuoteArg(const std::wstring &value) {
    std::wstring out = L"\"";
    for (wchar_t ch : value) {
        if (ch == L'\"') out += L'\\';
        out += ch;
    }
    out += L"\"";
    return out;
}

void ErrorBox(const wchar_t *message) {
    MessageBoxW(nullptr, message, L"EngAixt KiriKiri 汉化", MB_ICONERROR | MB_OK);
}

bool Is32Bit(const std::wstring &path) {
    DWORD type = 0;
    return GetBinaryTypeW(path.c_str(), &type) && type == SCS_32BIT_BINARY;
}

std::wstring ResolveTarget(const std::wstring &gameDir) {
    std::wstring target = ReadTarget(gameDir);
    if (!target.empty()) {
        if (target.size() > 1 && target[1] == L':') return target;
        return JoinPath(gameDir, target);
    }
    WIN32_FIND_DATAW data = {};
    HANDLE find = FindFirstFileW(JoinPath(gameDir, L"*.exe").c_str(), &data);
    if (find == INVALID_HANDLE_VALUE) return {};
    std::wstring result;
    do {
        const std::wstring name = data.cFileName;
        if (_wcsicmp(name.c_str(), L"启动汉化版.exe") == 0 ||
            _wcsicmp(name.c_str(), L"kirikiri_native_launcher.exe") == 0 ||
            !Is32Bit(JoinPath(gameDir, name))) continue;
        result = JoinPath(gameDir, name);
        break;
    } while (FindNextFileW(find, &data));
    FindClose(find);
    return result;
}

int RunNative(const std::wstring &gameDir, const std::wstring &target) {
    const std::wstring native = JoinPath(JoinPath(gameDir, L"_translation_meta"), L"kirikiri_native_launcher.exe");
    if (!Exists(target)) return 1;
    // Keep the package usable when an older installation is missing the
    // native helper.  A direct launch is a safe fallback; newly generated
    // packages always include the helper and therefore still use the full
    // display hook and translation map.
    if (!Exists(native)) {
        STARTUPINFOW startup = {};
        startup.cb = sizeof(startup);
        PROCESS_INFORMATION process = {};
        std::wstring command = QuoteArg(target);
        std::vector<wchar_t> buffer(command.begin(), command.end());
        buffer.push_back(L'\0');
        if (!CreateProcessW(target.c_str(), buffer.data(), nullptr, nullptr, FALSE, 0, nullptr,
                            gameDir.c_str(), &startup, &process)) {
            return 1;
        }
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        return 0;
    }

    // The standalone package is the stable offline replacement route. The
    // native hook reads the full translation map from _translation_meta and
    // replaces KiriKiriZ UTF-8 text in the game process.
    SetEnvironmentVariableW(L"KIRIKIRI_NATIVE_HOOK_PROFILE", L"display");
    SetEnvironmentVariableW(L"KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE", L"1");
    SetEnvironmentVariableW(L"KIRIKIRI_EMBED_WAIT_MS", L"0");
    SetEnvironmentVariableW(L"KIRIKIRI_CAPTURE_HOOKS", L"kag,zx,embed,z2,kr2");
    SetEnvironmentVariableW(L"KIRIKIRI_LUNA_CAPTURE_HOOKS", L"kag,zx,embed,z2,kr2");
    SetEnvironmentVariableW(L"KIRIKIRI_EXPERIMENTAL_TEXT_HOOKS", L"1");
    SetEnvironmentVariableW(L"KIRIKIRI_EXPERIMENTAL_LUNA_HOOKS", L"1");

    std::wstring command = QuoteArg(native) + L" " + QuoteArg(target) + L" --wait";
    std::vector<wchar_t> buffer(command.begin(), command.end());
    buffer.push_back(L'\0');
    STARTUPINFOW startup = {};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process = {};
    if (!CreateProcessW(native.c_str(), buffer.data(), nullptr, nullptr, FALSE, 0, nullptr,
                        gameDir.c_str(), &startup, &process)) {
        return 1;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    return static_cast<int>(code);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    const std::wstring gameDir = SelfDir();
    const std::wstring target = ResolveTarget(gameDir);
    if (target.empty()) {
        ErrorBox(L"未找到 KiriKiri 游戏主程序。请保留游戏目录结构并重新生成汉化包。");
        return 1;
    }
    if (!RunNative(gameDir, target)) return 0;
    ErrorBox(L"汉化运行组件启动失败。请查看 _translation_meta\\kirikiri_native_launcher.log。");
    return 1;
}
