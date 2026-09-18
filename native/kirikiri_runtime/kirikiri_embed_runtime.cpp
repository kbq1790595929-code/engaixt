#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "kirikiri_embed_runtime.h"

#include <algorithm>
#include <cwctype>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace kirikiri_embed {
namespace {

constexpr DWORD kReloadIntervalMs = 400;

struct FileState {
    std::wstring path;
    ULONGLONG size = 0;
    FILETIME writeTime = {};
    std::string trailingLine;
    bool loaded = false;
};

std::mutex gMutex;
bool gEnabled = false;
DWORD gWaitTimeoutMs = 0;
DWORD gLastReloadCheck = 0;
LogCallback gLogCallback = nullptr;
FileState gFile;
std::vector<std::wstring> gCandidates;
std::unordered_map<std::string, std::shared_ptr<const std::string>> gTranslations;
std::unordered_map<std::string, std::shared_ptr<const std::wstring>> gWideTranslations;
std::unordered_map<std::string, DWORD> gWaitStarted;
std::unordered_map<std::string, DWORD> gWideWaitStarted;
// Returned c_str() pointers remain valid after later map reloads.
std::vector<std::shared_ptr<const std::string>> gStableStrings;
std::vector<std::shared_ptr<const std::wstring>> gStableWideStrings;
volatile LONG gReplacementCount = 0;
volatile LONG gMissCount = 0;
volatile LONG gWideReplacementCount = 0;
volatile LONG gWideMissCount = 0;
volatile LONG gReloadCount = 0;
volatile LONG gReloadFailureCount = 0;
volatile LONG gWaitHitCount = 0;
volatile LONG gWaitTimeoutCount = 0;

void Log(const std::wstring &message) {
    if (gLogCallback) gLogCallback(message);
}

std::wstring JoinPath(const std::wstring &left, const std::wstring &right) {
    if (left.empty()) return right;
    if (left.back() == L'\\' || left.back() == L'/') return left + right;
    return left + L"\\" + right;
}

bool SameFileTime(const FILETIME &left, const FILETIME &right) {
    return left.dwLowDateTime == right.dwLowDateTime &&
           left.dwHighDateTime == right.dwHighDateTime;
}

bool QueryFile(const std::wstring &path, ULONGLONG *size, FILETIME *writeTime) {
    WIN32_FILE_ATTRIBUTE_DATA attrs = {};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &attrs)) return false;
    if (attrs.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) return false;
    if (size) {
        *size = (static_cast<ULONGLONG>(attrs.nFileSizeHigh) << 32) |
                attrs.nFileSizeLow;
    }
    if (writeTime) *writeTime = attrs.ftLastWriteTime;
    return true;
}

bool ReadFileRange(
    const std::wstring &path,
    ULONGLONG offset,
    ULONGLONG fileSize,
    std::string *output) {
    if (!output || fileSize < offset || fileSize - offset > 256ull * 1024ull * 1024ull) return false;
    HANDLE file = CreateFileW(
        path.c_str(),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;

    LARGE_INTEGER position = {};
    position.QuadPart = static_cast<LONGLONG>(offset);
    bool ok = SetFilePointerEx(file, position, nullptr, FILE_BEGIN) != FALSE;
    ULONGLONG remaining = fileSize - offset;
    output->clear();
    output->reserve(static_cast<size_t>(remaining));
    char buffer[64 * 1024];
    while (ok && remaining > 0) {
        DWORD wanted = static_cast<DWORD>(std::min<ULONGLONG>(remaining, sizeof(buffer)));
        DWORD read = 0;
        if (!ReadFile(file, buffer, wanted, &read, nullptr)) {
            ok = false;
            break;
        }
        if (read == 0) break;
        output->append(buffer, read);
        remaining -= read;
    }
    CloseHandle(file);
    return ok && remaining == 0;
}

int Base64Value(unsigned char value) {
    if (value >= 'A' && value <= 'Z') return value - 'A';
    if (value >= 'a' && value <= 'z') return value - 'a' + 26;
    if (value >= '0' && value <= '9') return value - '0' + 52;
    if (value == '+') return 62;
    if (value == '/') return 63;
    return -1;
}

bool DecodeBase64(const std::string &input, std::string *output) {
    if (!output) return false;
    output->clear();
    int value = 0;
    int bits = -8;
    bool sawData = false;
    for (unsigned char ch : input) {
        if (ch == '=') break;
        int decoded = Base64Value(ch);
        if (decoded < 0) return false;
        sawData = true;
        value = (value << 6) + decoded;
        bits += 6;
        if (bits >= 0) {
            output->push_back(static_cast<char>((value >> bits) & 0xff));
            bits -= 8;
        }
    }
    return sawData;
}

bool IsValidUtf8(const std::string &text) {
    if (text.empty() || text.find('\0') != std::string::npos) return false;
    return MultiByteToWideChar(
               CP_UTF8,
               MB_ERR_INVALID_CHARS,
               text.data(),
               static_cast<int>(text.size()),
               nullptr,
               0) > 0;
}

bool Utf8ToWide(const std::string &text, std::wstring *output) {
    if (!output || text.empty()) return false;
    int count = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), nullptr, 0);
    if (count <= 0) return false;
    output->resize(static_cast<size_t>(count));
    return MultiByteToWideChar(
               CP_UTF8,
               MB_ERR_INVALID_CHARS,
               text.data(),
               static_cast<int>(text.size()),
               output->data(),
               count) == count;
}

bool WideToUtf8(const std::wstring &text, std::string *output) {
    if (!output || text.empty()) return false;
    int count = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    if (count <= 0) return false;
    output->resize(static_cast<size_t>(count));
    return WideCharToMultiByte(
               CP_UTF8,
               WC_ERR_INVALID_CHARS,
               text.data(),
               static_cast<int>(text.size()),
               output->data(),
               count,
               nullptr,
               nullptr) == count;
}

size_t ParseMapLines(const std::string &data, bool allowTrailingLine) {
    size_t loaded = 0;
    size_t start = 0;
    while (start < data.size()) {
        size_t end = data.find('\n', start);
        if (end == std::string::npos && !allowTrailingLine) break;
        if (end == std::string::npos) end = data.size();
        std::string line = data.substr(start, end - start);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        start = end == data.size() ? data.size() : end + 1;
        if (line.empty() || line[0] == '#') continue;
        size_t tab = line.find('\t');
        if (tab == std::string::npos) continue;

        std::string source;
        std::string translated;
        if (!DecodeBase64(line.substr(0, tab), &source) ||
            !DecodeBase64(line.substr(tab + 1), &translated) ||
            !IsValidUtf8(source) ||
            !IsValidUtf8(translated) ||
            source == translated) {
            continue;
        }
        std::wstring wideTranslated;
        if (!Utf8ToWide(translated, &wideTranslated)) continue;
        auto stable = std::make_shared<const std::string>(std::move(translated));
        auto stableWide = std::make_shared<const std::wstring>(std::move(wideTranslated));
        gStableStrings.push_back(stable);
        gStableWideStrings.push_back(stableWide);
        gTranslations[source] = std::move(stable);
        gWideTranslations[std::move(source)] = std::move(stableWide);
        ++loaded;
    }
    return loaded;
}

bool SelectMapFile(std::wstring *path, ULONGLONG *size, FILETIME *writeTime) {
    for (const auto &candidate : gCandidates) {
        if (QueryFile(candidate, size, writeTime)) {
            if (path) *path = candidate;
            return true;
        }
    }
    return false;
}

void ReloadIfChangedLocked(bool force) {
    DWORD now = GetTickCount();
    if (!force && now - gLastReloadCheck < kReloadIntervalMs) return;
    gLastReloadCheck = now;

    std::wstring path;
    ULONGLONG size = 0;
    FILETIME writeTime = {};
    if (!SelectMapFile(&path, &size, &writeTime)) return;
    if (!force && gFile.loaded && path == gFile.path && size == gFile.size &&
        SameFileTime(writeTime, gFile.writeTime)) {
        return;
    }

    bool appendOnly = gFile.loaded && path == gFile.path && size > gFile.size;
    ULONGLONG offset = appendOnly ? gFile.size : 0;
    std::string data;
    if (!ReadFileRange(path, offset, size, &data)) {
        InterlockedIncrement(&gReloadFailureCount);
        Log(L"[kirikiri embed] map reload failed");
        return;
    }

    if (!appendOnly) {
        gTranslations.clear();
        gWideTranslations.clear();
        gFile.trailingLine.clear();
    }
    if (!gFile.trailingLine.empty()) {
        data.insert(0, gFile.trailingLine);
        gFile.trailingLine.clear();
    }
    size_t lastNewline = data.find_last_of('\n');
    bool completeTail = lastNewline != std::string::npos && lastNewline + 1 == data.size();
    if (!completeTail && lastNewline != std::string::npos) {
        gFile.trailingLine = data.substr(lastNewline + 1);
        data.resize(lastNewline + 1);
    } else if (!completeTail && lastNewline == std::string::npos && appendOnly) {
        gFile.trailingLine = data;
        data.clear();
    }
    size_t loaded = ParseMapLines(data, !appendOnly && lastNewline == std::string::npos);

    gFile.path = path;
    gFile.size = size;
    gFile.writeTime = writeTime;
    gFile.loaded = true;
    InterlockedIncrement(&gReloadCount);
    Log(L"[kirikiri embed] map reloaded entries=" +
        std::to_wstring(gTranslations.size()) +
        L" new=" + std::to_wstring(loaded) +
        (appendOnly ? L" mode=append" : L" mode=full"));
}

}  // namespace

void Initialize(const std::wstring &gameDir, bool enabled, DWORD waitTimeoutMs, LogCallback logCallback) {
    std::lock_guard<std::mutex> lock(gMutex);
    gEnabled = enabled;
    gWaitTimeoutMs = std::min<DWORD>(waitTimeoutMs, 60000);
    gLogCallback = logCallback;
    std::wstring meta = JoinPath(gameDir, L"_translation_meta");
    gCandidates = {
        JoinPath(meta, L"kirikiri_native_map.tsv"),
        JoinPath(meta, L"kirikiri_runtime_map.tsv"),
    };
    gLastReloadCheck = 0;
    gFile = FileState{};
    gTranslations.clear();
    gWideTranslations.clear();
    gWaitStarted.clear();
    gWideWaitStarted.clear();
    if (gEnabled) ReloadIfChangedLocked(true);
    Log(gEnabled ? L"[kirikiri embed] UTF-8 replacement enabled"
                 : L"[kirikiri embed] UTF-8 replacement disabled");
    if (gEnabled) {
        Log(L"[kirikiri embed] first-text wait timeout ms=" + std::to_wstring(gWaitTimeoutMs));
    }
}

bool PassesEmbedKrkrZFilter(const std::string &text) {
    if (text.empty() || text.size() > 2000) return false;
    bool hasNonAscii = false;
    for (unsigned char ch : text) {
        if (ch >= 0x80) {
            hasNonAscii = true;
            break;
        }
    }
    if (!hasNonAscii) return false;
    static const char *flags[] = {
        u8"（", u8"）", u8"。", u8"「", u8"」", u8"『", u8"』", u8"？", u8"！", u8"、", u8"―",
    };
    for (const char *flag : flags) {
        if (text.find(flag) != std::string::npos) return true;
    }
    return false;
}

bool IsNumericPercentToken(const std::string &token) {
    if (token.size() < 2 || token[0] != '%') return false;
    for (size_t index = 1; index < token.size(); ++index) {
        unsigned char ch = static_cast<unsigned char>(token[index]);
        if (ch != '?' && ch != '+' && ch != '-' && (ch < '0' || ch > '9')) return false;
    }
    return true;
}

bool IsHexColorToken(const std::string &token) {
    if (token.size() < 2 || token[0] != '#') return false;
    for (size_t index = 1; index < token.size(); ++index) {
        unsigned char ch = static_cast<unsigned char>(token[index]);
        if (!((ch >= '0' && ch <= '9') ||
              (ch >= 'a' && ch <= 'f') ||
              (ch >= 'A' && ch <= 'F'))) {
            return false;
        }
    }
    return true;
}

std::string NormalizeEmbedKrkrZText(const std::string &text) {
    std::string output;
    output.reserve(text.size());
    for (size_t index = 0; index < text.size();) {
        if (text[index] == '[') {
            size_t close = text.find(']', index + 1);
            if (close != std::string::npos) {
                index = close + 1;
                continue;
            }
        }
        if (text[index] == '%' || text[index] == '#') {
            size_t close = text.find(';', index + 1);
            if (close != std::string::npos) {
                std::string token = text.substr(index, close - index);
                bool formatting =
                    (token.size() >= 2 && token[0] == '%' &&
                     (token[1] == 'p' || token[1] == 'f')) ||
                    IsNumericPercentToken(token) ||
                    IsHexColorToken(token);
                if (formatting) {
                    index = close + 1;
                    continue;
                }
            }
        }
        output.push_back(text[index++]);
    }
    return output;
}

bool PassesEmbedKrkr2WideFilter(const std::wstring &text) {
    if (text.empty() || text.size() > 1200) return false;
    size_t start = 0;
    while (start < text.size() && iswspace(text[start])) ++start;
    if (start == text.size()) return false;
    wchar_t first = text[start];
    if (first == L'[' || first == L'@' || first == L'*' || first == L';') return false;
    if (text.find(L'[') != std::wstring::npos || text.find(L']') != std::wstring::npos) return false;

    bool hasJapanese = false;
    for (wchar_t ch : text) {
        if ((ch >= 0x3040 && ch <= 0x30ff) ||
            (ch >= 0x31f0 && ch <= 0x31ff) ||
            (ch >= 0x3400 && ch <= 0x9fff) ||
            (ch >= 0xf900 && ch <= 0xfaff)) {
            hasJapanese = true;
            break;
        }
    }
    return hasJapanese;
}

// Reinsert U+3000 (ideographic space) break markers into the translation at the
// same proportional positions they appear in the source. KiriKiri uses U+3000
// as an in-line line break, so a translation that drops them renders as one
// long line and the game refuses to embed it as multiline text.
std::wstring ReattachWideSpaceBreaks(
    const std::wstring &source, const std::wstring &translated) {
    if (source.empty() || translated.empty()) return translated;
    // Collect proportional break positions (0..1) from the source.
    std::vector<float> breaks;
    size_t total = 0;
    for (wchar_t ch : source) {
        if (ch == 0x3000) breaks.push_back(static_cast<float>(total));
        ++total;
    }
    if (breaks.empty() || total == 0) return translated;

    std::wstring out;
    out.reserve(translated.size() + breaks.size());
    std::wstring rest = translated;
    size_t tLen = rest.size();
    for (size_t i = 0; i < breaks.size(); ++i) {
        float ratio = breaks[i] / static_cast<float>(total);
        size_t pos = static_cast<size_t>(ratio * static_cast<float>(tLen));
        if (pos > tLen) pos = tLen;
        // Insert at each proportional position, in source order.
        out.append(rest, 0, pos);
        out.push_back(0x3000);
        rest = rest.substr(pos);
        tLen = rest.size();
    }
    out += rest;
    return out;
}

const char *ResolveUtf8(
    const char *original,
    const std::string &source,
    const std::string &visibleSource) {
    if (!original || source.empty()) return original;
    DWORD waitStarted = 0;
    bool waited = false;
    while (true) {
        {
            std::lock_guard<std::mutex> lock(gMutex);
            if (!gEnabled) return original;
            ReloadIfChangedLocked(false);
            auto found = !visibleSource.empty()
                ? gTranslations.find(visibleSource)
                : gTranslations.end();
            if (found == gTranslations.end() && (visibleSource.empty() || visibleSource == source)) {
                found = gTranslations.find(source);
            }
            if (found != gTranslations.end() && found->second && !found->second->empty()) {
                gWaitStarted.erase(source);
                InterlockedIncrement(&gReplacementCount);
                if (waited) InterlockedIncrement(&gWaitHitCount);
                std::string translated = *found->second;
                bool sourceHasBreak = source.find("\xE3\x80\x80") != std::string::npos;
                bool sourceHasN = source.find("%n;") != std::string::npos ||
                                  source.find("%n\xEF\xBC\x9B") != std::string::npos;
                // Restore any model-drifted %n； back to %n;.
                if (translated.find("%n\xEF\xBC\x9B") != std::string::npos) {
                    std::string cleaned;
                    cleaned.reserve(translated.size());
                    for (size_t i = 0; i < translated.size();) {
                        if (translated[i] == '%' && translated[i + 1] == 'n' &&
                            i + 5 <= translated.size() &&
                            static_cast<unsigned char>(translated[i + 2]) == 0xEF &&
                            static_cast<unsigned char>(translated[i + 3]) == 0xBC &&
                            static_cast<unsigned char>(translated[i + 4]) == 0x9B) {
                            cleaned += "%n;";
                            i += 5;
                        } else {
                            cleaned += translated[i++];
                        }
                    }
                    translated = cleaned;
                }
                // Reinsert U+3000 line breaks the translation dropped, so the game
                // still embeds multiline dialogue as multiline text.
                if (sourceHasBreak || sourceHasN) {
                    std::wstring srcW, translatedW;
                    if (Utf8ToWide(source, &srcW) && Utf8ToWide(translated, &translatedW)) {
                        std::wstring withBreaks = ReattachWideSpaceBreaks(srcW, translatedW);
                        std::string outUtf8;
                        if (WideToUtf8(withBreaks, &outUtf8) && !outUtf8.empty()) {
                            auto stable = std::make_shared<const std::string>(std::move(outUtf8));
                            gStableStrings.push_back(stable);
                            return stable->c_str();
                        }
                    }
                }
                // Keep the (possibly cleaned) translation alive via stable storage.
                auto stable = std::make_shared<const std::string>(std::move(translated));
                gStableStrings.push_back(stable);
                return stable->c_str();
            }
            if (gWaitTimeoutMs == 0) {
                InterlockedIncrement(&gMissCount);
                return original;
            }
            DWORD now = GetTickCount();
            auto inserted = gWaitStarted.emplace(source, now);
            waitStarted = inserted.first->second;
            if (now - waitStarted >= gWaitTimeoutMs) {
                InterlockedIncrement(&gMissCount);
                if (waited) InterlockedIncrement(&gWaitTimeoutCount);
                return original;
            }
            waited = true;
        }
        Sleep(50);
    }
}

const wchar_t *ResolveWide(
    const wchar_t *original,
    const std::wstring &source,
    const std::wstring &visibleSource) {
    if (!original || source.empty()) return original;
    std::string sourceUtf8;
    std::string visibleUtf8;
    if (!WideToUtf8(source, &sourceUtf8)) return original;
    if (!visibleSource.empty() && !WideToUtf8(visibleSource, &visibleUtf8)) return original;

    DWORD waitStarted = 0;
    bool waited = false;
    while (true) {
        {
            std::lock_guard<std::mutex> lock(gMutex);
            if (!gEnabled) return original;
            ReloadIfChangedLocked(false);
            auto found = !visibleUtf8.empty()
                ? gWideTranslations.find(visibleUtf8)
                : gWideTranslations.end();
            if (found == gWideTranslations.end() && (visibleUtf8.empty() || visibleUtf8 == sourceUtf8)) {
                found = gWideTranslations.find(sourceUtf8);
            }
            if (found != gWideTranslations.end() && found->second && !found->second->empty()) {
                const std::wstring &replacement = *found->second;
                if (replacement.find(L'[') != std::wstring::npos ||
                    replacement.find(L']') != std::wstring::npos) {
                    InterlockedIncrement(&gWideMissCount);
                    return original;
                }
                gWideWaitStarted.erase(sourceUtf8);
                InterlockedIncrement(&gWideReplacementCount);
                if (waited) InterlockedIncrement(&gWaitHitCount);
                return replacement.c_str();
            }
            if (gWaitTimeoutMs == 0) {
                InterlockedIncrement(&gWideMissCount);
                return original;
            }
            DWORD now = GetTickCount();
            auto inserted = gWideWaitStarted.emplace(sourceUtf8, now);
            waitStarted = inserted.first->second;
            if (now - waitStarted >= gWaitTimeoutMs) {
                InterlockedIncrement(&gWideMissCount);
                if (waited) InterlockedIncrement(&gWaitTimeoutCount);
                return original;
            }
            waited = true;
        }
        Sleep(50);
    }
}

Stats GetStats() {
    std::lock_guard<std::mutex> lock(gMutex);
    Stats stats;
    stats.enabled = gEnabled ? 1 : 0;
    stats.mapEntries = static_cast<long>(gTranslations.size());
    stats.replacements = gReplacementCount;
    stats.misses = gMissCount;
    stats.wideReplacements = gWideReplacementCount;
    stats.wideMisses = gWideMissCount;
    stats.reloads = gReloadCount;
    stats.reloadFailures = gReloadFailureCount;
    stats.waitHits = gWaitHitCount;
    stats.waitTimeouts = gWaitTimeoutCount;
    return stats;
}

}  // namespace kirikiri_embed
