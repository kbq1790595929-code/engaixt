#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>

#include "kirikiri_embed_runtime.h"
#include "kirikiri_embed_trace.h"
#include "kirikiri_patch_stream.h"
#include "kirikiri_psb_runtime.h"

#include <algorithm>
#include <climits>
#include <csetjmp>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <cstdlib>
#include <initializer_list>
#include <map>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

using tjs_uint = unsigned int;
using tjs_int = int;
using tjs_uint32 = unsigned int;
using tjs_uint64 = unsigned long long;
using tjs_int64 = long long;
using tjs_error = int;
using tjs_char = wchar_t;

constexpr tjs_uint TJS_BS_READ = 0;
constexpr int TJS_BS_SEEK_SET = 0;

#pragma pack(push, 4)
struct tTJSVariantString_S {
    tjs_int RefCount;
    tjs_char *LongString;
    tjs_char ShortString[22];
    tjs_int Length;
    tjs_uint32 HeapFlag;
    tjs_uint32 Hint;
};

struct tTJSString {
    tTJSVariantString_S *Ptr;
    const tjs_char *c_str() const {
        if (!Ptr) return L"";
        return Ptr->LongString ? Ptr->LongString : Ptr->ShortString;
    }
};
#pragma pack(pop)

class tTJSBinaryStream;
class iTVPStorageMedia;
class iTVPStorageLister;

class iTVPFunctionExporter {
public:
    virtual bool __cdecl QueryFunctionsByString(const tjs_char **name, void **function, int count) = 0;
    virtual bool __cdecl QueryFunctionsByNarrowString(const char **name, void **function, int count) = 0;
    virtual bool __cdecl QueryFunctionsByHash(const tjs_uint32 *hash, void **function, int count) = 0;
};

class iTVPStorageMedia {
public:
    virtual void __cdecl AddRef() = 0;
    virtual void __cdecl Release() = 0;
    virtual void __cdecl GetName(tTJSString &name) = 0;
    virtual void __cdecl NormalizeDomainName(tTJSString &name) = 0;
    virtual void __cdecl NormalizePathName(tTJSString &name) = 0;
    virtual bool __cdecl CheckExistentStorage(const tTJSString &name) = 0;
    virtual tTJSBinaryStream *__cdecl Open(const tTJSString &name, tjs_uint32 flags) = 0;
    virtual void __cdecl GetListAt(const tTJSString &name, iTVPStorageLister *lister) = 0;
    virtual void __cdecl GetLocallyAccessibleName(tTJSString &name) = 0;
};

using V2Link_t = HRESULT(__stdcall *)(iTVPFunctionExporter *);
using TVPCreateStream_t = tTJSBinaryStream *(__fastcall *)(const tTJSString &, tjs_uint);
using TVPIsExistentStorage_t = bool(__stdcall *)(const tTJSString &);
using TVPGetAppPath_t = tTJSString(__stdcall *)();
using TVPCreateIStream_t = void *(__stdcall *)(const tTJSString &, tjs_uint32);
using TVPCreateBinaryStreamAdapter_t = tTJSBinaryStream *(__stdcall *)(void *);
using TVPAddAutoPath_t = void(__stdcall *)(const tTJSString &);
using TVPClearStorageCaches_t = void(__stdcall *)();
using TVPRegisterStorageMedia_t = void(__stdcall *)(iTVPStorageMedia *);
using TVPUnregisterStorageMedia_t = void(__stdcall *)(iTVPStorageMedia *);
using TJSAllocVariantString_t = tTJSVariantString_S *(__stdcall *)(const tjs_char *);
using TJSVariantStringRelease_t = void(__stdcall *)(tTJSVariantString_S *);
using GetProcAddress_t = FARPROC(WINAPI *)(HMODULE, LPCSTR);
using TextOutA_t = BOOL(WINAPI *)(HDC, int, int, LPCSTR, int);
using TextOutW_t = BOOL(WINAPI *)(HDC, int, int, LPCWSTR, int);
using ExtTextOutA_t = BOOL(WINAPI *)(HDC, int, int, UINT, const RECT *, LPCSTR, UINT, const INT *);
using ExtTextOutW_t = BOOL(WINAPI *)(HDC, int, int, UINT, const RECT *, LPCWSTR, UINT, const INT *);
using DrawTextA_t = int(WINAPI *)(HDC, LPCSTR, int, LPRECT, UINT);
using DrawTextW_t = int(WINAPI *)(HDC, LPCWSTR, int, LPRECT, UINT);
using CreateFontA_t = HFONT(WINAPI *)(int, int, int, int, int, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPCSTR);
using CreateFontW_t = HFONT(WINAPI *)(int, int, int, int, int, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPCWSTR);
using CreateFontIndirectA_t = HFONT(WINAPI *)(const LOGFONTA *);
using CreateFontIndirectW_t = HFONT(WINAPI *)(const LOGFONTW *);
using MultiByteToWideChar_t = int(WINAPI *)(UINT, DWORD, LPCCH, int, LPWSTR, int);
using WideCharToMultiByte_t = int(WINAPI *)(UINT, DWORD, LPCWCH, int, LPSTR, int, LPCCH, LPBOOL);
using GdipDrawString_t = int(WINAPI *)(void *, const WCHAR *, int, const void *, const void *, const void *, const void *);
using GdipMeasureString_t = int(WINAPI *)(void *, const WCHAR *, int, const void *, const void *, const void *, void *, int *, int *);
using GdipAddPathString_t = int(WINAPI *)(void *, const WCHAR *, int, const void *, int, float, const void *, const void *);
using GdipCreateFontFamilyFromName_t = int(WINAPI *)(const WCHAR *, void *, void **);
using StreamSeek_t = tjs_uint64(__cdecl *)(tTJSBinaryStream *, tjs_int64, int);
using StreamRead_t = tjs_uint(__cdecl *)(tTJSBinaryStream *, void *, tjs_uint);
using StorageMediaOpen_t = tTJSBinaryStream *(__cdecl *)(iTVPStorageMedia *, const tTJSString &, tjs_uint32);

GetProcAddress_t RealGetProcAddress = nullptr;
TextOutA_t RealTextOutA = nullptr;
TextOutW_t RealTextOutW = nullptr;
ExtTextOutA_t RealExtTextOutA = nullptr;
ExtTextOutW_t RealExtTextOutW = nullptr;
DrawTextA_t RealDrawTextA = nullptr;
DrawTextW_t RealDrawTextW = nullptr;
CreateFontA_t RealCreateFontA = nullptr;
CreateFontW_t RealCreateFontW = nullptr;
CreateFontIndirectA_t RealCreateFontIndirectA = nullptr;
CreateFontIndirectW_t RealCreateFontIndirectW = nullptr;
MultiByteToWideChar_t RealMultiByteToWideChar = nullptr;
WideCharToMultiByte_t RealWideCharToMultiByte = nullptr;
GdipDrawString_t RealGdipDrawString = nullptr;
GdipMeasureString_t RealGdipMeasureString = nullptr;
GdipAddPathString_t RealGdipAddPathString = nullptr;
GdipAddPathString_t RealGdipAddPathStringI = nullptr;
GdipCreateFontFamilyFromName_t RealGdipCreateFontFamilyFromName = nullptr;
V2Link_t RealV2Link = nullptr;
TVPIsExistentStorage_t TVPIsExistentStorageNoSearchNoNormalize = nullptr;
TVPGetAppPath_t TVPGetAppPath = nullptr;
TVPCreateIStream_t TVPCreateIStream = nullptr;
TVPCreateIStream_t OriginalTVPCreateIStream = nullptr;
TVPCreateBinaryStreamAdapter_t TVPCreateBinaryStreamAdapter = nullptr;
TVPAddAutoPath_t TVPAddAutoPath = nullptr;
TVPClearStorageCaches_t TVPClearStorageCaches = nullptr;
TVPRegisterStorageMedia_t OriginalTVPRegisterStorageMedia = nullptr;
TVPUnregisterStorageMedia_t OriginalTVPUnregisterStorageMedia = nullptr;
TJSAllocVariantString_t TJSAllocVariantString = nullptr;
TJSVariantStringRelease_t TJSVariantStringRelease = nullptr;

std::wstring gGameDir;
std::wstring gLogPath;
std::wstring gRuntimeCapturePath;
std::vector<std::wstring> gPatchArchives;
std::vector<std::wstring> gPatchEntries;
std::unordered_map<std::wstring, std::wstring> gPatchBasenameEntries;
std::vector<std::wstring> gDumpTargets;
std::vector<std::wstring> gPatchProtocols = {L"arc://", L"archive://"};
bool gPatchNoProtocol = true;
bool gPassiveDumpOnly = false;
bool gActiveExtensionlessDump = false;
bool gLowLevelStreamHooks = false;
std::mutex gHookMutex;
std::mutex gFontMutex;
std::mutex gDumpMutex;
std::mutex gStorageMutex;
std::mutex gInternalTextMutex;
std::mutex gRuntimeCaptureMutex;
std::mutex gOverlayMutex;
std::mutex gSpeakerMutex;
std::mutex gModulePatchMutex;
std::unordered_map<std::wstring, std::wstring> gTranslations;
std::unordered_map<std::wstring, std::wstring> gPlaceholders;
std::unordered_set<std::wstring> gRuntimeCapturedTexts;
std::unordered_map<std::wstring, HFONT> gFontCache;
std::vector<wchar_t> gSjisTunnelTable;
std::unordered_map<iTVPStorageMedia *, StorageMediaOpen_t> gStorageMediaOpenOriginal;
std::unordered_map<void **, StorageMediaOpen_t> gStorageVtableOpenOriginal;
std::map<std::wstring, std::wstring> gInternalDynamicTranslations;
std::map<std::wstring, std::wstring> gTextRenderDynamicTranslations;
BYTE gCreateStreamOriginal[96] = {};
void *gCreateStreamTarget = nullptr;
void *gCreateStreamTrampoline = nullptr;
size_t gCreateStreamPatchSize = 0;
bool gCreateStreamHooked = false;
BYTE gIStreamOriginal[16] = {};
void *gIStreamTarget = nullptr;
size_t gIStreamPatchSize = 0;
bool gIStreamHooked = false;
double gFontHeightScale = 1.0;
volatile LONG gWideDrawDepth = 0;
volatile LONG gFontCreateDepth = 0;
volatile LONG gFontSelectCount = 0;
volatile LONG gOpenHits = 0;
volatile LONG gOpenMisses = 0;
volatile LONG gReadSamples = 0;
volatile LONG gCreateStreamReads = 0;
volatile LONG gCandidateMissSamples = 0;
volatile LONG gCandidateOpenFailSamples = 0;
volatile LONG gSignatureBypassHits = 0;
volatile LONG gDumpHits = 0;
volatile LONG gDumpFailures = 0;
volatile LONG gDumpTargetThreadStarted = 0;
volatile LONG gStorageMediaHookCount = 0;
volatile LONG gStorageOpenSamples = 0;
volatile LONG gStorageRedirectDepth = 0;
volatile LONG gIStreamSamples = 0;
volatile LONG gPatchAutoPathInstalled = 0;
volatile LONG gReplacedCount = 0;
volatile LONG gMissedCount = 0;
volatile LONG gTunnelCount = 0;
volatile LONG gModulePatchCount = 0;
volatile LONG gGdipRenderReplaceCount = 0;
volatile LONG gGdipMeasureReplaceCount = 0;
volatile LONG gGdipFontFamilyCount = 0;
volatile LONG gInternalHookInstallCount = 0;
volatile LONG gInternalReplaceCount = 0;
volatile LONG gInternalMissCount = 0;
volatile LONG gInternalKagEntryHits = 0;
volatile LONG gInternalKagMissSamples = 0;
volatile LONG gInternalKagHits = 0;
volatile LONG gInternalKagReplaceSamples = 0;
volatile LONG gInternalKagSkipSamples = 0;
volatile LONG gInternalKirikiriZ3Hits = 0;
volatile LONG gInternalKirikiriZXHits = 0;
volatile LONG gInternalKrkrZ2Hits = 0;
volatile LONG gInternalEmbedKrkrZHits = 0;
volatile LONG gEmbedKrkrZConverterBypassCount = 0;
volatile LONG gEmbedKrkrZCursorReplaceCount = 0;
volatile LONG gEmbedKrkrZAfterNewCount = 0;
volatile LONG gEmbedKrkrZAfterNewLogCount = 0;
volatile LONG gTextRenderHits = 0;
volatile LONG gTextRenderReplaceCount = 0;
volatile LONG gTextRenderMissCount = 0;
volatile LONG gPsbPostHits = 0;
volatile LONG gPsbPostLogCount = 0;
volatile LONG gInternalKiriKiriZ2Hits = 0;
volatile LONG gInternalEmbedKrkr2Hits = 0;
volatile LONG gInternalEmbedKrkr2ReplaceSamples = 0;
volatile LONG gInternalEmbedKrkr2SkipSamples = 0;
volatile LONG gInternalEmbedKrkr2SlotApplied = 0;
volatile LONG gInternalEmbedKrkr2SlotWriteFailures = 0;
volatile LONG gInternalKrkr2WcsHits = 0;
volatile LONG gRuntimeOverlayMissSamples = 0;
volatile LONG gRuntimeTextCaptureCount = 0;
volatile LONG gRuntimeSpeakerCaptureCount = 0;
volatile LONG gRuntimeCaptureDuplicateSkips = 0;
volatile LONG gOverlayWriteCount = 0;
bool gLegacySystemHooks = false;
bool gV2LinkHooks = false;
bool gDirectCreateStreamHook = false;
bool gPatchArchiveHooks = false;
bool gGetProcAddressHooks = false;
bool gInternalTextHooks = false;
bool gVmTextReplacement = false;
bool gEmbedTextReplacement = false;
DWORD gEmbedTextWaitMs = 0;
HANDLE gOverlaySharedMemory = nullptr;
void *gOverlaySharedMemoryView = nullptr;
HANDLE gEmbedControlSharedMemory = nullptr;
void *gEmbedControlSharedMemoryView = nullptr;
volatile LONG gOverlaySequence = 0;
std::wstring gCurrentSpeakerName;
void *gKagParserHookTarget = nullptr;
void *gKagParserExHookTarget = nullptr;
void *gKagParserEntryHookTarget = nullptr;
void *gExtKagParserEntryHookTarget = nullptr;
void *gKiriKiriZ3HookTarget = nullptr;
void *gKiriKiriZXHookTarget = nullptr;
void *gKrkrZ2HookTarget = nullptr;
void *gKiriKiriZ2HookTarget = nullptr;
void *gEmbedKrkr2HookTarget = nullptr;
void *gKrkr2WcsHookTarget = nullptr;
void *gTextRenderHookTarget = nullptr;
void *gPsbPostHookTarget = nullptr;
std::vector<void *> gEmbedKrkrZHookTargets;
void *gEmbedKrkrZConverterTarget = nullptr;
void *gEmbedKrkrZCursorTarget = nullptr;
std::mutex gEmbedKrkrZCursorMutex;
std::unordered_map<const char **, std::pair<uintptr_t, uintptr_t>> gEmbedKrkrZCursorRanges;
std::mutex gEmbedKrkr2SlotMutex;
std::unordered_map<const wchar_t **, const wchar_t *> gEmbedKrkr2SlotReplacements;
constexpr LONG INITIAL_READ_BYPASS = 32;
constexpr LONG MAX_DUMP_FILES = 4096;
constexpr tjs_uint64 MAX_DUMP_STREAM_BYTES = 32ull * 1024ull * 1024ull;
constexpr const wchar_t *kChineseFontFaceW = L"Microsoft YaHei UI";
constexpr const char *kChineseFontFaceA = "Microsoft YaHei UI";
constexpr BYTE kChineseCharset = GB2312_CHARSET;
constexpr BYTE kFontQuality = CLEARTYPE_QUALITY;
constexpr const wchar_t *kHookBuildTag = L"2026-07-24-krkr-wide-slot-v29";
constexpr const wchar_t *kTunnelAlphabet = L"０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ";
constexpr wchar_t kTunnelPrefix = L'〓';
constexpr int kTunnelAlphabetSize = 36;

std::wstring FilePathToStorageUrl(std::wstring path);
void *__stdcall HookTVPCreateIStream(const tTJSString &name, tjs_uint32 flags);
void *CallOriginalTVPCreateIStream(const tTJSString &name, tjs_uint32 flags);
tTJSBinaryStream *__cdecl HookStorageMediaOpen(iTVPStorageMedia *media, const tTJSString &name, tjs_uint32 flags);
void __stdcall HookTVPRegisterStorageMedia(iTVPStorageMedia *media);
void __stdcall HookTVPUnregisterStorageMedia(iTVPStorageMedia *media);
bool WriteProcessCode(void *target, const void *data, size_t size);
bool InstallIStreamHookAt(void *target);
bool DecodeWidePuaTunnel(const wchar_t *text, int count, std::wstring *out);
bool TryReadTjsString(const tTJSString &source, std::wstring *out);
void WriteToOverlay(const std::wstring &original, const std::wstring &translated, const std::wstring &speaker);
std::wstring ShortLogText(const std::wstring &value, size_t limit);
bool IsEmbedTextReplacementEnabled();

bool DumpCaptureEnabled() {
    return gLowLevelStreamHooks || gDirectCreateStreamHook;
}

bool EnvFlagEnabled(const wchar_t *name) {
    wchar_t value[16] = {};
    DWORD n = GetEnvironmentVariableW(name, value, 16);
    if (n == 0 || n >= 16) return false;
    return wcscmp(value, L"1") == 0 || _wcsicmp(value, L"true") == 0 || _wcsicmp(value, L"yes") == 0;
}

DWORD EnvDword(const wchar_t *name, DWORD fallback, DWORD maximum) {
    wchar_t value[32] = {};
    DWORD n = GetEnvironmentVariableW(name, value, 32);
    if (n == 0 || n >= 32) return fallback;
    wchar_t *end = nullptr;
    unsigned long parsed = wcstoul(value, &end, 10);
    if (end == value || (end && *end)) return fallback;
    return std::min<DWORD>(static_cast<DWORD>(parsed), maximum);
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

std::wstring GetProcessDir() {
    wchar_t buf[MAX_PATH * 4] = {};
    DWORD n = GetModuleFileNameW(nullptr, buf, static_cast<DWORD>(MAX_PATH * 4));
    if (!n) return L".";
    return DirName(std::wstring(buf, n));
}

std::string WideToUtf8(const std::wstring &s) {
    if (s.empty()) return {};
    int bytes = WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0, nullptr, nullptr);
    if (bytes <= 0) return {};
    std::string out(bytes, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), out.data(), bytes, nullptr, nullptr);
    return out;
}

std::wstring Utf8ToWide(const std::string &s) {
    if (s.empty()) return {};
    int chars = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (chars <= 0) return {};
    std::wstring out(chars, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), out.data(), chars);
    return out;
}

bool ContainsEmbedKrkrZChatFlag(const std::wstring &s) {
    static const wchar_t *flags[] = {
        L"（", L"）", L"。", L"「", L"」", L"『", L"』", L"？", L"！", L"、", L"―",
    };
    for (const wchar_t *flag : flags) {
        if (s.find(flag) != std::wstring::npos) return true;
    }
    return false;
}

void Log(const std::wstring &message) {
    if (gLogPath.empty()) return;
    std::string line = WideToUtf8(message + L"\r\n");
    HANDLE h = CreateFileW(gLogPath.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(h, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
    CloseHandle(h);
}

std::string JsonEscapeUtf8(const std::wstring &value) {
    std::string utf8 = WideToUtf8(value);
    std::string out;
    out.reserve(utf8.size() + 16);
    for (unsigned char ch : utf8) {
        switch (ch) {
        case '\\': out += "\\\\"; break;
        case '"': out += "\\\""; break;
        case '\b': out += "\\b"; break;
        case '\f': out += "\\f"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (ch < 0x20) {
                char buf[7] = {};
                snprintf(buf, sizeof(buf), "\\u%04x", ch);
                out += buf;
            } else {
                out.push_back(static_cast<char>(ch));
            }
            break;
        }
    }
    return out;
}

void AppendRuntimeCapture(
    const std::wstring &visibleText,
    const wchar_t *hookName,
    const wchar_t *role = L"text",
    const std::wstring &rawText = std::wstring()) {
    if (gRuntimeCapturePath.empty() || visibleText.empty()) return;
    std::wstring roleValue = role && *role ? role : L"text";
    std::wstring hookValue = hookName && *hookName ? hookName : L"kirikiri_native";
    std::wstring rawValue = rawText.empty() ? visibleText : rawText;
    std::wstring dedupeKey = roleValue + L"\x1f" + visibleText;
    {
        std::lock_guard<std::mutex> lock(gRuntimeCaptureMutex);
        if (!gRuntimeCapturedTexts.insert(dedupeKey).second) {
            InterlockedIncrement(&gRuntimeCaptureDuplicateSkips);
            return;
        }
    }
    if (roleValue == L"speaker") {
        InterlockedIncrement(&gRuntimeSpeakerCaptureCount);
    } else {
        InterlockedIncrement(&gRuntimeTextCaptureCount);
    }
    std::string line = "{\"source\":\"";
    line += JsonEscapeUtf8(hookValue);
    line += "\",\"text\":\"";
    line += JsonEscapeUtf8(visibleText);
    line += "\",\"role\":\"";
    line += JsonEscapeUtf8(roleValue);
    line += "\",\"hook_name\":\"";
    line += JsonEscapeUtf8(hookValue);
    line += "\",\"visible_text\":\"";
    line += JsonEscapeUtf8(visibleText);
    line += "\",\"raw_text\":\"";
    line += JsonEscapeUtf8(rawValue);
    line += "\"}\r\n";
    HANDLE h = CreateFileW(
        gRuntimeCapturePath.c_str(),
        FILE_APPEND_DATA,
        FILE_SHARE_READ,
        nullptr,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(h, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
    CloseHandle(h);
}

bool FileExists(const std::wstring &path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool DirectoryExists(const std::wstring &path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool EnsureDirectory(const std::wstring &path) {
    if (path.empty() || DirectoryExists(path)) return true;
    size_t pos = path.find_last_of(L"\\/");
    if (pos != std::wstring::npos) {
        if (!EnsureDirectory(path.substr(0, pos))) return false;
    }
    if (CreateDirectoryW(path.c_str(), nullptr)) return true;
    return GetLastError() == ERROR_ALREADY_EXISTS && DirectoryExists(path);
}

bool ReadBinary(const std::wstring &path, std::string *out) {
    FILE *f = _wfopen(path.c_str(), L"rb");
    if (!f) return false;
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return false;
    }
    long size = ftell(f);
    if (size < 0) {
        fclose(f);
        return false;
    }
    rewind(f);
    out->assign(static_cast<size_t>(size), '\0');
    if (size > 0) {
        size_t read = fread(out->data(), 1, static_cast<size_t>(size), f);
        if (read != static_cast<size_t>(size)) {
            fclose(f);
            return false;
        }
    }
    fclose(f);
    return true;
}

int Base64Value(unsigned char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

std::string DecodeBase64(const std::string &input) {
    std::string out;
    int val = 0;
    int bits = -8;
    for (unsigned char c : input) {
        if (c == '=') break;
        int v = Base64Value(c);
        if (v < 0) continue;
        val = (val << 6) + v;
        bits += 6;
        if (bits >= 0) {
            out.push_back(static_cast<char>((val >> bits) & 0xFF));
            bits -= 8;
        }
    }
    return out;
}

void LoadTranslations() {
    std::wstring meta = JoinPath(gGameDir, L"_translation_meta");
    std::vector<std::wstring> candidates = {
        JoinPath(meta, L"kirikiri_native_map.tsv"),
        JoinPath(meta, L"kirikiri_runtime_map.tsv"),
    };

    std::string data;
    std::wstring used;
    for (const auto &candidate : candidates) {
        if (ReadBinary(candidate, &data)) {
            used = candidate;
            break;
        }
    }
    if (used.empty()) {
        Log(L"[kirikiri native] translation TSV not found");
        return;
    }

    size_t start = 0;
    size_t count = 0;
    while (start <= data.size()) {
        size_t end = data.find('\n', start);
        if (end == std::string::npos) end = data.size();
        std::string line = data.substr(start, end - start);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        start = end + 1;
        if (line.empty() || line[0] == '#') {
            if (end == data.size()) break;
            continue;
        }
        size_t tab = line.find('\t');
        if (tab == std::string::npos) {
            if (end == data.size()) break;
            continue;
        }
        std::wstring src = Utf8ToWide(DecodeBase64(line.substr(0, tab)));
        std::wstring dst = Utf8ToWide(DecodeBase64(line.substr(tab + 1)));
        if (!src.empty() && !dst.empty() && src != dst) {
            gTranslations[src] = dst;
            ++count;
        }
        if (end == data.size()) break;
    }
    Log(L"[kirikiri native] loaded translations: " + std::to_wstring(count));
}

void LoadPlaceholderMap() {
    gPlaceholders.clear();
    std::wstring path = JoinPath(JoinPath(gGameDir, L"_translation_meta"), L"kirikiri_placeholder_map.tsv");
    std::string data;
    if (!ReadBinary(path, &data)) {
        Log(L"[kirikiri native] placeholder map not found");
        return;
    }
    size_t start = 0;
    size_t count = 0;
    while (start <= data.size()) {
        size_t end = data.find('\n', start);
        if (end == std::string::npos) end = data.size();
        std::string line = data.substr(start, end - start);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        start = end + 1;
        if (line.empty() || line[0] == '#') {
            if (end == data.size()) break;
            continue;
        }
        size_t tab = line.find('\t');
        if (tab == std::string::npos) {
            if (end == data.size()) break;
            continue;
        }
        std::wstring token = Utf8ToWide(line.substr(0, tab));
        std::wstring dst = Utf8ToWide(DecodeBase64(line.substr(tab + 1)));
        if (!token.empty() && !dst.empty()) {
            gPlaceholders[token] = dst;
            ++count;
        }
        if (end == data.size()) break;
    }
    Log(L"[kirikiri native] loaded placeholders: " + std::to_wstring(count));
}

void LoadSjisTunnelTable() {
    gSjisTunnelTable.clear();
    std::wstring meta = JoinPath(gGameDir, L"_translation_meta");
    std::vector<std::wstring> candidates = {
        JoinPath(meta, L"kirikiri_sjis_ext.bin"),
        JoinPath(gGameDir, L"kirikiri_sjis_ext.bin"),
        JoinPath(meta, L"sjis_ext.bin"),
        JoinPath(gGameDir, L"sjis_ext.bin"),
    };

    std::string bytes;
    std::wstring used;
    for (const auto &candidate : candidates) {
        if (ReadBinary(candidate, &bytes) && bytes.size() >= 2) {
            used = candidate;
            break;
        }
    }
    if (used.empty()) {
        Log(L"[kirikiri native] sjis tunnel table not found");
        return;
    }
    for (size_t i = 0; i + 1 < bytes.size(); i += 2) {
        wchar_t ch = static_cast<wchar_t>(static_cast<unsigned char>(bytes[i]) |
                                          (static_cast<unsigned char>(bytes[i + 1]) << 8));
        gSjisTunnelTable.push_back(ch);
    }
    Log(L"[kirikiri native] loaded sjis tunnel chars: " + std::to_wstring(gSjisTunnelTable.size()));
}

bool IsSjisLead(unsigned char b) {
    return (b >= 0x81 && b < 0xA0) || (b >= 0xE0 && b < 0xFD);
}

int SjisTunnelIndex(unsigned char high, unsigned char low) {
    if (high < 0xF0 || high > 0xF9) return -1;
    if (low < 0x40 || low > 0xFC || low == 0x7F) return -1;
    int lowIdx = low < 0x7F ? low - 0x40 : low - 0x41;
    return (high - 0xF0) * 188 + lowIdx;
}

int FullwidthTunnelDigit(wchar_t ch) {
    for (int i = 0; i < kTunnelAlphabetSize; ++i) {
        if (kTunnelAlphabet[i] == ch) return i;
    }
    return -1;
}

std::wstring DecodeCp932Bytes(const std::vector<char> &bytes) {
    if (bytes.empty()) return {};
    MultiByteToWideChar_t fn = RealMultiByteToWideChar ? RealMultiByteToWideChar : MultiByteToWideChar;
    int chars = fn(932, 0, bytes.data(), static_cast<int>(bytes.size()), nullptr, 0);
    if (chars <= 0) chars = fn(CP_ACP, 0, bytes.data(), static_cast<int>(bytes.size()), nullptr, 0);
    if (chars <= 0) return {};
    std::wstring out(chars, L'\0');
    if (fn(932, 0, bytes.data(), static_cast<int>(bytes.size()), out.data(), chars) <= 0) {
        fn(CP_ACP, 0, bytes.data(), static_cast<int>(bytes.size()), out.data(), chars);
    }
    return out;
}

bool DecodeCp932Tunnel(const char *ptr, int byteCount, std::wstring *out) {
    if (!ptr || !out || gSjisTunnelTable.empty()) return false;
    int max = byteCount > 0 ? byteCount : 4096;
    bool found = false;
    std::wstring result;
    std::vector<char> raw;
    auto flushRaw = [&]() {
        if (!raw.empty()) {
            result += DecodeCp932Bytes(raw);
            raw.clear();
        }
    };

    for (int i = 0; i < max; ++i) {
        unsigned char high = static_cast<unsigned char>(ptr[i]);
        if (high == 0 && byteCount <= 0) break;
        if (IsSjisLead(high) && i + 1 < max) {
            unsigned char low = static_cast<unsigned char>(ptr[i + 1]);
            if (low == 0 && byteCount <= 0) break;
            int idx = SjisTunnelIndex(high, low);
            if (idx >= 0 && static_cast<size_t>(idx) < gSjisTunnelTable.size()) {
                flushRaw();
                result.push_back(gSjisTunnelTable[idx]);
                found = true;
                ++i;
                continue;
            }
            raw.push_back(static_cast<char>(high));
            raw.push_back(static_cast<char>(low));
            ++i;
        } else {
            raw.push_back(static_cast<char>(high));
        }
    }
    flushRaw();
    if (!found) return false;
    std::wstring tokenDecoded;
    if (DecodeWidePuaTunnel(result.c_str(), static_cast<int>(result.size()), &tokenDecoded) && !tokenDecoded.empty()) {
        *out = tokenDecoded;
    } else {
        *out = result;
    }
    return true;
}

bool DecodeWidePuaTunnel(const wchar_t *text, int count, std::wstring *out) {
    if (!text || !out || gSjisTunnelTable.empty()) return false;
    int len = count;
    if (len < 0) len = static_cast<int>(wcsnlen(text, 4097));
    if (len <= 0 || len > 4096) return false;
    bool found = false;
    std::wstring result;
    result.reserve(static_cast<size_t>(len));
    for (int i = 0; i < len; ++i) {
        wchar_t ch = text[i];
        if (ch == kTunnelPrefix && i + 2 < len) {
            int hi = FullwidthTunnelDigit(text[i + 1]);
            int lo = FullwidthTunnelDigit(text[i + 2]);
            int idxToken = hi >= 0 && lo >= 0 ? hi * kTunnelAlphabetSize + lo : -1;
            if (idxToken >= 0 && static_cast<size_t>(idxToken) < gSjisTunnelTable.size()) {
                result.push_back(gSjisTunnelTable[idxToken]);
                found = true;
                i += 2;
                continue;
            }
        }
        int idx = static_cast<int>(ch) - 0xE000;
        if (idx >= 0 && static_cast<size_t>(idx) < gSjisTunnelTable.size()) {
            result.push_back(gSjisTunnelTable[idx]);
            found = true;
        } else {
            result.push_back(ch);
        }
    }
    if (!found) return false;
    *out = result;
    return true;
}

bool EncodeWidePuaTunnelToCp932(const wchar_t *text, int count, std::string *out) {
    if (!text || !out || gSjisTunnelTable.empty()) return false;
    int len = count;
    if (len < 0) len = static_cast<int>(wcsnlen(text, 4097)) + 1;
    if (len <= 0 || len > 4096) return false;
    bool found = false;
    std::string result;
    std::wstring raw;
    auto flushRaw = [&]() {
        if (raw.empty()) return;
        WideCharToMultiByte_t fn = RealWideCharToMultiByte ? RealWideCharToMultiByte : WideCharToMultiByte;
        int bytes = fn(932, 0, raw.data(), static_cast<int>(raw.size()), nullptr, 0, nullptr, nullptr);
        if (bytes <= 0) bytes = fn(CP_ACP, 0, raw.data(), static_cast<int>(raw.size()), nullptr, 0, nullptr, nullptr);
        if (bytes > 0) {
            std::string tmp(bytes, '\0');
            if (fn(932, 0, raw.data(), static_cast<int>(raw.size()), tmp.data(), bytes, nullptr, nullptr) <= 0) {
                fn(CP_ACP, 0, raw.data(), static_cast<int>(raw.size()), tmp.data(), bytes, nullptr, nullptr);
            }
            result += tmp;
        }
        raw.clear();
    };

    for (int i = 0; i < len; ++i) {
        wchar_t ch = text[i];
        if (ch == 0 && count < 0) {
            flushRaw();
            result.push_back('\0');
            break;
        }
        int idx = static_cast<int>(ch) - 0xE000;
        if (idx >= 0 && static_cast<size_t>(idx) < gSjisTunnelTable.size()) {
            flushRaw();
            int high = 0xF0 + (idx / 188);
            int lowIdx = idx % 188;
            int low = lowIdx < 0x3F ? 0x40 + lowIdx : 0x41 + lowIdx;
            result.push_back(static_cast<char>(high));
            result.push_back(static_cast<char>(low));
            found = true;
        } else {
            raw.push_back(ch);
        }
    }
    flushRaw();
    if (!found) return false;
    *out = result;
    return true;
}

std::wstring ReadAnsiText(const char *text, int count) {
    if (!text) return {};
    int len = count;
    if (len < 0) len = static_cast<int>(strnlen(text, 4097));
    if (len <= 0 || len > 4096) return {};
    std::wstring tunneled;
    if (DecodeCp932Tunnel(text, len, &tunneled) && !tunneled.empty()) return tunneled;
    std::vector<char> bytes(text, text + len);
    return DecodeCp932Bytes(bytes);
}

std::wstring ReadWideText(const wchar_t *text, int count) {
    if (!text) return {};
    int len = count;
    if (len < 0) len = static_cast<int>(wcsnlen(text, 4097));
    if (len <= 0 || len > 4096) return {};
    std::wstring tunneled;
    if (DecodeWidePuaTunnel(text, len, &tunneled) && !tunneled.empty()) return tunneled;
    return std::wstring(text, text + len);
}

bool ContainsCjk(const std::wstring &s) {
    for (wchar_t ch : s) {
        if ((ch >= 0x3400 && ch <= 0x4DBF) || (ch >= 0x4E00 && ch <= 0x9FFF)) return true;
    }
    return false;
}

bool NeedsTranslate(const std::wstring &s) {
    if (s.empty() || s.size() > 1000) return false;
    for (wchar_t ch : s) {
        if ((ch >= 0x3040 && ch <= 0x30FF) ||
            (ch >= 0x3400 && ch <= 0x4DBF) ||
            (ch >= 0x4E00 && ch <= 0x9FFF)) {
            return true;
        }
    }
    return false;
}

bool ContainsJapanese(const std::wstring &s) {
    for (wchar_t ch : s) {
        if ((ch >= 0x3040 && ch <= 0x30FF) || (ch >= 0xFF66 && ch <= 0xFF9F)) return true;
    }
    return false;
}

bool IsPathLikeText(const std::wstring &s) {
    if (s.find(L".ks") != std::wstring::npos ||
        s.find(L".tjs") != std::wstring::npos ||
        s.find(L".xp3") != std::wstring::npos ||
        s.find(L".dll") != std::wstring::npos ||
        s.find(L"/") != std::wstring::npos ||
        s.find(L"\\") != std::wstring::npos) {
        return true;
    }
    return false;
}

std::wstring TrimWide(const std::wstring &s) {
    size_t begin = 0;
    while (begin < s.size() && iswspace(s[begin])) ++begin;
    size_t end = s.size();
    while (end > begin && iswspace(s[end - 1])) --end;
    return s.substr(begin, end - begin);
}

std::wstring CollapseWideSpaces(const std::wstring &s, bool keepNewlines) {
    std::wstring out;
    bool inSpace = false;
    for (wchar_t ch : s) {
        bool isSpace = ch == L' ' || ch == L'\t' || ch == 0x3000 || ch == L'\r' || ch == L'\n';
        if (!isSpace) {
            out.push_back(ch);
            inSpace = false;
            continue;
        }
        if (!keepNewlines && (ch == L'\r' || ch == L'\n')) {
            if (!inSpace) out.push_back(L' ');
            inSpace = true;
            continue;
        }
        if (keepNewlines && (ch == L'\r' || ch == L'\n')) {
            if (out.empty() || out.back() != L'\n') out.push_back(L'\n');
            inSpace = false;
            continue;
        }
        if (!inSpace) out.push_back(L' ');
        inSpace = true;
    }
    return TrimWide(out);
}

std::wstring StripKagTags(const std::wstring &s, bool keepLineBreaks) {
    std::wstring out;
    for (size_t i = 0; i < s.size();) {
        if (s[i] != L'[') {
            out.push_back(s[i++]);
            continue;
        }
        size_t close = s.find(L']', i + 1);
        if (close == std::wstring::npos) {
            out.push_back(s[i++]);
            continue;
        }
        std::wstring tag = s.substr(i + 1, close - i - 1);
        std::wstring lowered = tag;
        std::transform(lowered.begin(), lowered.end(), lowered.begin(), towlower);
        size_t textPos = lowered.find(L"text=");
        if (lowered.rfind(L"ch ", 0) == 0 && textPos != std::wstring::npos) {
            size_t valueStart = textPos + 5;
            wchar_t quote = 0;
            if (valueStart < tag.size() && (tag[valueStart] == L'"' || tag[valueStart] == L'\'')) {
                quote = tag[valueStart++];
            }
            size_t valueEnd = valueStart;
            if (quote) {
                valueEnd = tag.find(quote, valueStart);
            } else {
                while (valueEnd < tag.size() && !iswspace(tag[valueEnd])) ++valueEnd;
            }
            if (valueEnd != std::wstring::npos && valueEnd > valueStart) {
                out += tag.substr(valueStart, valueEnd - valueStart);
            }
        } else if (keepLineBreaks && (
                       lowered == L"r" ||
                       lowered == L"p" ||
                       lowered == L"er" ||
                       lowered == L"cm" ||
                       lowered == L"ct" ||
                       lowered == L"current" ||
                       lowered == L"clearfix" ||
                       lowered == L"clearhistory")) {
            out.push_back(L'\n');
        }
        i = close + 1;
    }
    return out;
}

std::wstring CompactWideText(const std::wstring &s) {
    std::wstring out;
    for (wchar_t ch : s) {
        if (!iswspace(ch) && ch != 0x3000) out.push_back(ch);
    }
    return out;
}

void AddInternalLookupVariant(std::vector<std::wstring> *variants, const std::wstring &value) {
    if (!variants) return;
    std::wstring trimmed = TrimWide(value);
    if (trimmed.empty()) return;
    for (const auto &existing : *variants) {
        if (existing == trimmed) return;
    }
    variants->push_back(trimmed);
}

std::vector<std::wstring> InternalLookupVariants(const std::wstring &src) {
    std::vector<std::wstring> variants;
    AddInternalLookupVariant(&variants, src);
    AddInternalLookupVariant(&variants, CollapseWideSpaces(src, true));
    AddInternalLookupVariant(&variants, CollapseWideSpaces(src, false));

    std::wstring visibleLines = StripKagTags(src, true);
    AddInternalLookupVariant(&variants, visibleLines);
    AddInternalLookupVariant(&variants, CollapseWideSpaces(visibleLines, true));
    AddInternalLookupVariant(&variants, CollapseWideSpaces(visibleLines, false));

    std::wstring visibleFlat = StripKagTags(src, false);
    AddInternalLookupVariant(&variants, visibleFlat);
    AddInternalLookupVariant(&variants, CollapseWideSpaces(visibleFlat, false));
    AddInternalLookupVariant(&variants, CompactWideText(visibleFlat));

    // 容错：去掉 「」『』 引号包裹，匹配旧静态 key（未注入引号的版本）
    std::wstring unwrapped = TrimWide(visibleFlat);
    if (unwrapped.size() >= 2) {
        wchar_t front = unwrapped.front();
        wchar_t back = unwrapped.back();
        if ((front == L'「' && back == L'」') || (front == L'『' && back == L'』')) {
            unwrapped = unwrapped.substr(1, unwrapped.size() - 2);
            AddInternalLookupVariant(&variants, unwrapped);
            AddInternalLookupVariant(&variants, CompactWideText(unwrapped));
        }
    }
    return variants;
}

const std::wstring *FindTranslation(const std::wstring &text) {
    auto it = gTranslations.find(text);
    if (it == gTranslations.end()) return nullptr;
    return &it->second;
}

const std::wstring *FindTranslationVariant(const std::wstring &src) {
    for (const auto &candidate : InternalLookupVariants(src)) {
        const std::wstring *translated = FindTranslation(candidate);
        if (translated) return translated;
    }
    return nullptr;
}

bool IsKanaCodepoint(wchar_t ch) {
    return (ch >= 0x3040 && ch <= 0x30FF) || (ch >= 0x31F0 && ch <= 0x31FF);
}

bool IsCjkCodepoint(wchar_t ch) {
    return (ch >= 0x3400 && ch <= 0x9FFF) || (ch >= 0xF900 && ch <= 0xFAFF);
}

bool LooksLikeRuntimeMojibake(const std::wstring &text) {
    std::wstring trimmed = TrimWide(text);
    if (trimmed.empty()) return false;
    size_t kana = 0;
    size_t cjk = 0;
    size_t markers = 0;
    size_t visible = 0;
    for (wchar_t ch : trimmed) {
        if (iswspace(ch)) continue;
        ++visible;
        if (IsKanaCodepoint(ch)) ++kana;
        if (IsCjkCodepoint(ch)) ++cjk;
        if (ch == 0xFFFD || ch == 0x951F) ++markers;
    }
    if (markers > 0) return true;
    if (visible >= 24 && kana == 0 && cjk * 100 >= visible * 70) return true;
    return false;
}

bool IsInternalCandidateText(const std::wstring &src) {
    std::wstring trimmed = TrimWide(src);
    if (trimmed.empty() || trimmed.size() > 1200) return false;
    if (!ContainsJapanese(trimmed)) return false;
    if (LooksLikeRuntimeMojibake(trimmed)) return false;
    if (IsPathLikeText(trimmed)) return false;
    if (trimmed[0] == L'[' || trimmed[0] == L'@' || trimmed[0] == L'*' || trimmed[0] == L';') return false;
    if (trimmed.find(L"読み込み") != std::wstring::npos) return false;
    return true;
}

std::wstring StripPercentControls(const std::wstring &s) {
    // KiriKiri percent control sequences: %p-1; %p; %fuser; %50; %n; %#ff0000;
    // They are formatting/line-break directives, not visible dialogue text.
    std::wstring out;
    for (size_t i = 0; i < s.size();) {
        if (s[i] == L'%') {
            size_t close = s.find(L';', i + 1);
            if (close != std::wstring::npos) {
                std::wstring token = s.substr(i + 1, close - i - 1);
                bool control = false;
                if (!token.empty() && token[0] == L'#') {
                    control = token.size() >= 2 && token.find_first_not_of(L"0123456789abcdefABCDEF", 1) == std::wstring::npos;
                } else if (!token.empty() && (token[0] == L'p' || token[0] == L'f')) {
                    control = true;
                } else if (!token.empty() && (token[0] == L'+' || token[0] == L'-' || iswdigit(token[0]))) {
                    control = token.find_first_not_of(L"+?0123456789") == std::wstring::npos;
                }
                if (control) {
                    i = close + 1;
                    continue;
                }
            }
        }
        out.push_back(s[i++]);
    }
    return out;
}

std::wstring RuntimeVisibleText(const std::wstring &src) {
    std::wstring visible = StripKagTags(src, true);
    visible = StripPercentControls(visible);
    visible = CollapseWideSpaces(visible, true);
    if (!IsInternalCandidateText(visible)) {
        visible = CollapseWideSpaces(StripPercentControls(src), true);
    }
    return TrimWide(visible);
}

std::wstring KagParserVisibleText(const std::wstring &src);

void PublishRuntimeOverlayText(
    const std::wstring &sourceText,
    const wchar_t *sourceLabel,
    const std::wstring &visibleOverride = std::wstring()) {
    std::wstring visible = visibleOverride;
    bool sourceIsCommand =
        !sourceText.empty() && (sourceText[0] == L'@' || sourceText[0] == L'[');
    if (sourceIsCommand) {
        visible = KagParserVisibleText(sourceText);
        if (!IsInternalCandidateText(visible)) return;
    }
    if (!IsInternalCandidateText(visible)) {
        visible = RuntimeVisibleText(sourceText);
    }
    if (!IsInternalCandidateText(visible)) return;
    const std::wstring *translated = nullptr;
    if (!visibleOverride.empty()) {
        translated = FindTranslationVariant(visible);
    } else {
        translated = FindTranslationVariant(sourceText);
        if (!translated) translated = FindTranslationVariant(visible);
    }
    if (translated && !translated->empty() && *translated != visible) {
        WriteToOverlay(visible, *translated, L"");
        return;
    }
    AppendRuntimeCapture(visible, sourceLabel ? sourceLabel : L"runtime", L"text", sourceText);
    WriteToOverlay(visible, L"", L"");
    LONG sample = InterlockedIncrement(&gRuntimeOverlayMissSamples);
    if (sample <= 120) {
        Log(std::wstring(L"[kirikiri native] runtime overlay miss(") +
            (sourceLabel ? sourceLabel : L"runtime") + L"): " + ShortLogText(visible, 120));
    }
}

std::wstring ShortLogText(const std::wstring &value, size_t limit = 120) {
    if (value.size() <= limit) return value;
    return value.substr(0, limit) + L"...";
}

std::wstring LowerWide(std::wstring value) {
    std::transform(value.begin(), value.end(), value.begin(), towlower);
    return value;
}

bool IsKagAttrBoundary(const std::wstring &text, size_t pos) {
    if (pos == 0) return true;
    wchar_t before = text[pos - 1];
    return iswspace(before) || before == L'[' || before == L'@';
}

std::wstring ReadKagAttribute(const std::wstring &command, const wchar_t *attr) {
    std::wstring lowered = LowerWide(command);
    std::wstring attrName = LowerWide(std::wstring(attr));
    std::wstring needle = attrName + L"=";
    size_t searchPos = 0;
    while (true) {
        size_t pos = lowered.find(needle, searchPos);
        if (pos == std::wstring::npos) return {};
        searchPos = pos + 1;
        if (!IsKagAttrBoundary(lowered, pos)) continue;
        size_t valueStart = pos + needle.size();
        if (valueStart >= command.size()) return {};
        wchar_t quote = 0;
        if (command[valueStart] == L'\'' || command[valueStart] == L'"') {
            quote = command[valueStart++];
        }
        size_t valueEnd = valueStart;
        if (quote) {
            valueEnd = command.find(quote, valueStart);
            if (valueEnd == std::wstring::npos) return {};
        } else {
            while (valueEnd < command.size() && !iswspace(command[valueEnd]) && command[valueEnd] != L']') {
                ++valueEnd;
            }
        }
        if (valueEnd > valueStart) return command.substr(valueStart, valueEnd - valueStart);
    }
}

std::wstring KagCommandBody(const std::wstring &src) {
    std::wstring text = TrimWide(src);
    if (text.empty()) return {};
    if (text[0] == L'@') return text.substr(1);
    if (text[0] == L'[') {
        size_t close = text.find(L']', 1);
        if (close != std::wstring::npos && close > 1) return text.substr(1, close - 1);
    }
    return {};
}

std::wstring KagCommandVerb(const std::wstring &body) {
    std::wstring trimmed = TrimWide(body);
    size_t end = 0;
    while (end < trimmed.size() && !iswspace(trimmed[end]) && trimmed[end] != L']' && trimmed[end] != L'*') {
        ++end;
    }
    return LowerWide(trimmed.substr(0, end));
}

bool IsSpeakerKagCommandVerb(const std::wstring &verb) {
    return verb == L"voice" ||
           verb == L"name" ||
           verb == L"chara" ||
           verb == L"character" ||
           verb == L"speaker";
}

std::wstring NormalizeSpeakerName(std::wstring speaker) {
    speaker = TrimWide(speaker);
    if (speaker.empty()) return {};
    size_t paren = speaker.find(L'（');
    if (paren != std::wstring::npos) speaker = speaker.substr(0, paren);
    std::wstring out;
    for (wchar_t ch : speaker) {
        if (!iswspace(ch) && ch != 0x3000) out.push_back(ch);
    }
    return TrimWide(out);
}

bool IsNarrationSpeaker(const std::wstring &speaker) {
    return speaker.empty() ||
           speaker == L"ト書き" ||
           speaker == L"地の文" ||
           speaker == L"ナレーション" ||
           speaker == L"narration";
}

bool TryExtractKagSpeakerName(const std::wstring &src, std::wstring *speaker) {
    if (!speaker) return false;
    std::wstring body = KagCommandBody(src);
    if (body.empty()) return false;
    std::wstring verb = KagCommandVerb(body);
    if (!IsSpeakerKagCommandVerb(verb)) return false;
    std::wstring name = NormalizeSpeakerName(ReadKagAttribute(body, L"name"));
    if (name.empty() && (verb == L"name" || verb == L"speaker" || verb == L"chara" || verb == L"character")) {
        name = NormalizeSpeakerName(ReadKagAttribute(body, L"text"));
    }
    if (name.empty()) return false;
    *speaker = IsNarrationSpeaker(name) ? L"" : name;
    return true;
}

void SetCurrentSpeakerName(const std::wstring &speaker) {
    std::lock_guard<std::mutex> lock(gSpeakerMutex);
    gCurrentSpeakerName = speaker;
}

std::wstring CurrentSpeakerName() {
    std::lock_guard<std::mutex> lock(gSpeakerMutex);
    return gCurrentSpeakerName;
}

bool IsSpeakerOnlyKagCommand(const std::wstring &src) {
    std::wstring body = KagCommandBody(src);
    if (body.empty()) return false;
    std::wstring verb = KagCommandVerb(body);
    if (!IsSpeakerKagCommandVerb(verb)) return false;
    if (ReadKagAttribute(body, L"word").empty() == false) return false;
    if (ReadKagAttribute(body, L"text").empty() == false) return false;
    return verb == L"name" || verb == L"chara" || verb == L"character" || verb == L"speaker";
}

bool CaptureRuntimeSpeakerCommand(const std::wstring &src) {
    std::wstring speaker;
    if (!TryExtractKagSpeakerName(src, &speaker)) return false;
    SetCurrentSpeakerName(speaker);
    AppendRuntimeCapture(speaker.empty() ? L"<narration>" : speaker, L"kag_speaker", L"speaker", src);
    LONG sample = InterlockedIncrement(&gInternalKagSkipSamples);
    if (sample <= 80) {
        Log(L"[kirikiri native] runtime speaker: " + (speaker.empty() ? L"<narration>" : speaker));
    }
    return IsSpeakerOnlyKagCommand(src);
}

bool TryReadWideCString(const wchar_t *text, int maxChars, std::wstring *out) {
    if (!out) return false;
    out->clear();
    if (!text || maxChars <= 0) return false;
    wchar_t buffer[4097] = {};
    size_t len = 0;
    int limit = std::min(maxChars, static_cast<int>(sizeof(buffer) / sizeof(buffer[0])));
    __try {
        while (len < static_cast<size_t>(limit) && text[len]) {
            buffer[len] = text[len];
            ++len;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    if (len == 0 || len >= static_cast<size_t>(limit)) return false;
    out->assign(buffer, buffer + len);
    return true;
}

bool TryReadNarrowCString(const char *text, int maxBytes, std::string *out) {
    if (!out) return false;
    out->clear();
    if (!text || maxBytes <= 0) return false;
    char buffer[4097] = {};
    size_t len = 0;
    int limit = std::min(maxBytes, static_cast<int>(sizeof(buffer)));
    __try {
        while (len < static_cast<size_t>(limit) && text[len]) {
            buffer[len] = text[len];
            ++len;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    if (len == 0 || len >= static_cast<size_t>(limit)) return false;
    out->assign(buffer, buffer + len);
    return true;
}

bool TryReadNarrowCursor(const char **cursor, const char **out) {
    if (!cursor || !out) return false;
    __try {
        *out = *cursor;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    return *out != nullptr;
}

bool TryWriteNarrowCursor(const char **cursor, const char *value) {
    if (!cursor || !value) return false;
    __try {
        *cursor = value;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    return true;
}

const wchar_t *ResolveInternalWideReplacement(const wchar_t *text, int maxChars, volatile LONG *hitCounter) {
    if (!text) return text;
    std::wstring src;
    if (!TryReadWideCString(text, maxChars, &src)) return text;
    if (hitCounter) InterlockedIncrement(hitCounter);
    if (CaptureRuntimeSpeakerCommand(src)) {
        return text;
    }
    std::wstring visible = RuntimeVisibleText(src);
    if (!IsInternalCandidateText(visible)) return text;
    const std::wstring *translated = FindTranslationVariant(src);
    if (!translated || translated->empty()) {
        InterlockedIncrement(&gInternalMissCount);
        PublishRuntimeOverlayText(src, L"internal_wide");
        return text;
    }

    std::wstring out = *translated;
    if (src.size() >= 4 && src.substr(src.size() - 4) == L"[np]") out += L"[np]";
    if (src.size() >= 3 && src.substr(src.size() - 3) == L"[r]") out += L"[r]";
    if (out.empty()) return text;

    PublishRuntimeOverlayText(src, L"internal_wide");

    // 检查是否需要嵌入
    bool kagDisplayReplacement =
        IsEmbedTextReplacementEnabled() && hitCounter == &gInternalKagHits;
    if (!gVmTextReplacement && !kagDisplayReplacement) {
        // 不嵌入，但文本已经通过 PublishRuntimeOverlayText 发送到字幕窗口
        return text;
    }
    {
        std::lock_guard<std::mutex> lock(gInternalTextMutex);
        auto inserted = gInternalDynamicTranslations.emplace(src, out);
        InterlockedIncrement(&gInternalReplaceCount);
        return inserted.first->second.c_str();
    }
}

extern "C" __declspec(dllexport) const wchar_t *__stdcall KiriKiriResolveInternalText(
    const wchar_t *text, int maxChars, volatile LONG *hitCounter) {
    return ResolveInternalWideReplacement(text, maxChars, hitCounter);
}

bool TryReadEmbedKrkr2TextSlot(const wchar_t **slot, const wchar_t **value) {
    if (!slot || !value) return false;
    __try {
        *value = *slot;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    return true;
}

bool TryWriteEmbedKrkr2TextSlotRaw(const wchar_t **slot, const wchar_t *replacement) {
    if (!slot || !replacement) return false;
    __try {
        *slot = replacement;
        return *slot == replacement;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

bool IsCurrentEmbedKrkr2SlotReplacement(const wchar_t **slot, const wchar_t *text) {
    if (!slot || !text) return false;
    std::lock_guard<std::mutex> lock(gEmbedKrkr2SlotMutex);
    auto found = gEmbedKrkr2SlotReplacements.find(slot);
    if (found == gEmbedKrkr2SlotReplacements.end()) return false;
    const wchar_t *currentValue = nullptr;
    bool current = TryReadEmbedKrkr2TextSlot(slot, &currentValue) &&
                   found->second == text && currentValue == text;
    if (!current) gEmbedKrkr2SlotReplacements.erase(found);
    return current;
}

bool TryWriteEmbedKrkr2TextSlot(
    const wchar_t **slot,
    const wchar_t *expected,
    const wchar_t *replacement) {
    if (!slot || !expected || !replacement) return false;
    const wchar_t *currentValue = nullptr;
    bool written = TryReadEmbedKrkr2TextSlot(slot, &currentValue) &&
                   currentValue == expected &&
                   TryWriteEmbedKrkr2TextSlotRaw(slot, replacement);
    if (!written) return false;
    std::lock_guard<std::mutex> lock(gEmbedKrkr2SlotMutex);
    if (gEmbedKrkr2SlotReplacements.size() >= 4096) {
        gEmbedKrkr2SlotReplacements.clear();
    }
    gEmbedKrkr2SlotReplacements[slot] = replacement;
    return true;
}

extern "C" __declspec(dllexport) const wchar_t *__stdcall KiriKiriResolveEmbedKrkr2Text(
    const wchar_t *text,
    const wchar_t **slot) {
    if (!text) return text;
    InterlockedIncrement(&gInternalEmbedKrkr2Hits);
    if (IsCurrentEmbedKrkr2SlotReplacement(slot, text)) return text;

    std::wstring src;
    if (!TryReadWideCString(text, 1600, &src)) return text;
    if (CaptureRuntimeSpeakerCommand(src)) return text;
    std::wstring visible = RuntimeVisibleText(src);
    if (!IsInternalCandidateText(visible) ||
        !kirikiri_embed::PassesEmbedKrkr2WideFilter(src)) {
        return text;
    }

    PublishRuntimeOverlayText(src, L"embed_krkr2_wide", visible);

    // 如果嵌入被禁用，仍然发送到 overlay 但不嵌入游戏
    if (!IsEmbedTextReplacementEnabled()) {
        return text;
    }

    const wchar_t *replacement = kirikiri_embed::ResolveWide(text, src, visible);
    if (replacement == text) {
        InterlockedIncrement(&gInternalMissCount);
        return text;
    }
    if (!TryWriteEmbedKrkr2TextSlot(slot, text, replacement)) {
        InterlockedIncrement(&gInternalEmbedKrkr2SlotWriteFailures);
        LONG sample = InterlockedIncrement(&gInternalEmbedKrkr2SkipSamples);
        if (sample <= 80) {
            Log(L"[kirikiri native] EmbedKrkr2 wide slot write failed source=" +
                ShortLogText(visible));
        }
        return text;
    }

    InterlockedIncrement(&gInternalReplaceCount);
    InterlockedIncrement(&gInternalEmbedKrkr2SlotApplied);
    LONG sample = InterlockedIncrement(&gInternalEmbedKrkr2ReplaceSamples);
    if (sample <= 80) {
        Log(L"[kirikiri native] EmbedKrkr2 wide slot applied: " +
            ShortLogText(visible) + L" => " + ShortLogText(replacement));
    }
    return replacement;
}

std::wstring NormalizeTextRenderVisible(const std::wstring &source) {
    std::wstring visible = source;
    size_t position = 0;
    while ((position = visible.find(L"%p", position)) != std::wstring::npos) {
        size_t firstEnd = visible.find(L';', position + 2);
        if (firstEnd == std::wstring::npos) break;
        size_t fontStart = visible.find(L"%f", firstEnd + 1);
        if (fontStart != firstEnd + 1) {
            position = firstEnd + 1;
            continue;
        }
        size_t fontEnd = visible.find(L';', fontStart + 2);
        if (fontEnd == std::wstring::npos) break;
        visible.erase(position, fontEnd - position + 1);
    }
    visible = StripKagTags(visible, true);
    visible = CollapseWideSpaces(visible, true);
    return TrimWide(visible);
}

extern "C" __declspec(dllexport) const wchar_t *__stdcall KiriKiriResolveTextRenderText(
    const wchar_t *text, int maxChars, volatile LONG *hitCounter) {
    if (!text) return text;
    std::wstring source;
    if (!TryReadWideCString(text, maxChars, &source)) return text;
    std::wstring visible = NormalizeTextRenderVisible(source);
    if (!IsInternalCandidateText(visible)) return text;
    if (hitCounter) InterlockedIncrement(hitCounter);

    const std::wstring *translated = FindTranslationVariant(source);
    if (!translated) translated = FindTranslationVariant(visible);
    if (!translated || translated->empty() || *translated == visible) {
        InterlockedIncrement(&gTextRenderMissCount);
        PublishRuntimeOverlayText(source, L"textrender", visible);
        return text;
    }

    WriteToOverlay(visible, *translated, L"");
    if (!IsEmbedTextReplacementEnabled()) return text;
    std::lock_guard<std::mutex> lock(gInternalTextMutex);
    auto inserted = gTextRenderDynamicTranslations.emplace(source, *translated);
    InterlockedIncrement(&gTextRenderReplaceCount);
    InterlockedIncrement(&gInternalReplaceCount);
    return inserted.first->second.c_str();
}

extern "C" __declspec(dllexport) void __stdcall KiriKiriCaptureInternalTjsString(
    const tTJSString *text, volatile LONG *hitCounter) {
    if (!text) return;
    std::wstring src;
    if (!TryReadTjsString(*text, &src)) return;
    if (CaptureRuntimeSpeakerCommand(src)) {
        if (hitCounter) InterlockedIncrement(hitCounter);
        return;
    }
    if (!IsInternalCandidateText(RuntimeVisibleText(src))) return;
    if (hitCounter) InterlockedIncrement(hitCounter);
    PublishRuntimeOverlayText(src, L"tjs_string");
}

extern "C" __declspec(dllexport) const char *__stdcall KiriKiriCaptureInternalUtf8Text(
    const char *text, int maxBytes, volatile LONG *hitCounter) {
    if (!text) return text;
    std::string raw;
    if (!TryReadNarrowCString(text, maxBytes, &raw)) return text;
    if (!kirikiri_embed::PassesEmbedKrkrZFilter(raw)) return text;
    std::wstring wide = Utf8ToWide(raw);
    if (!ContainsEmbedKrkrZChatFlag(wide)) return text;
    if (CaptureRuntimeSpeakerCommand(wide)) {
        if (hitCounter) InterlockedIncrement(hitCounter);
        return text;
    }
    std::string visibleUtf8 = kirikiri_embed::NormalizeEmbedKrkrZText(raw);
    std::wstring visible = RuntimeVisibleText(Utf8ToWide(visibleUtf8));
    if (!IsInternalCandidateText(visible)) return text;
    if (hitCounter) InterlockedIncrement(hitCounter);
    PublishRuntimeOverlayText(wide, L"embed_krkrz_utf8", visible);
    return kirikiri_embed::ResolveUtf8(text, raw, WideToUtf8(visible));
}

extern "C" __declspec(dllexport) int __stdcall KiriKiriConvertEmbeddedUtf8(
    const char *text, wchar_t *destination, volatile LONG *hitCounter) {
    const char *replacement = KiriKiriCaptureInternalUtf8Text(text, 4096, hitCounter);
    if (!replacement || replacement == text) return INT_MIN;
    size_t bytes = strlen(replacement);
    if (bytes == 0 || bytes > INT_MAX) return INT_MIN;
    MultiByteToWideChar_t convert = RealMultiByteToWideChar
        ? RealMultiByteToWideChar
        : &MultiByteToWideChar;
    int required = convert(
        CP_UTF8,
        MB_ERR_INVALID_CHARS,
        replacement,
        static_cast<int>(bytes),
        nullptr,
        0);
    if (required <= 0) return INT_MIN;
    if (destination) {
        int written = convert(
            CP_UTF8,
            MB_ERR_INVALID_CHARS,
            replacement,
            static_cast<int>(bytes),
            destination,
            required);
        if (written != required) return INT_MIN;
    }
    InterlockedIncrement(&gEmbedKrkrZConverterBypassCount);
    return required;
}

extern "C" __declspec(dllexport) void __stdcall KiriKiriReplaceEmbeddedUtf8Cursor(
    const char **cursor, volatile LONG *hitCounter) {
    if (!cursor) return;
    const char *source = nullptr;
    if (!TryReadNarrowCursor(cursor, &source)) return;

    uintptr_t sourceAddress = reinterpret_cast<uintptr_t>(source);
    {
        std::lock_guard<std::mutex> lock(gEmbedKrkrZCursorMutex);
        auto active = gEmbedKrkrZCursorRanges.find(cursor);
        if (active != gEmbedKrkrZCursorRanges.end()) {
            if (sourceAddress >= active->second.first && sourceAddress <= active->second.second) return;
            gEmbedKrkrZCursorRanges.erase(active);
        }
    }

    const char *replacement = KiriKiriCaptureInternalUtf8Text(source, 4096, hitCounter);
    if (!replacement || replacement == source) return;
    size_t replacementBytes = strlen(replacement);
    if (replacementBytes == 0) return;
    if (!TryWriteNarrowCursor(cursor, replacement)) return;
    {
        std::lock_guard<std::mutex> lock(gEmbedKrkrZCursorMutex);
        if (gEmbedKrkrZCursorRanges.size() >= 4096) gEmbedKrkrZCursorRanges.clear();
        uintptr_t begin = reinterpret_cast<uintptr_t>(replacement);
        gEmbedKrkrZCursorRanges[cursor] = {begin, begin + replacementBytes};
    }
    InterlockedIncrement(&gEmbedKrkrZCursorReplaceCount);
}

uint32_t EmbedSourceHash(const std::string &text) {
    uint32_t hash = 2166136261u;
    for (unsigned char value : text) {
        hash ^= value;
        hash *= 16777619u;
    }
    return hash;
}

extern "C" __declspec(dllexport) const char *__stdcall KiriKiriEmbedAfterNewUtf8Text(
    const char *text,
    int maxBytes,
    volatile LONG *hitCounter,
    const void *hookTarget,
    const void *caller,
    const void *parentCaller,
    const void *grandparentCaller,
    const void *destination) {
    const char *replacement = KiriKiriCaptureInternalUtf8Text(text, maxBytes, hitCounter);
    if (!replacement || replacement == text) return text;

    InterlockedIncrement(&gEmbedKrkrZAfterNewCount);
    std::string raw;
    TryReadNarrowCString(text, maxBytes, &raw);
    uint32_t sourceHash = EmbedSourceHash(raw);
    uintptr_t imageBase = reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
    uintptr_t hookRva = reinterpret_cast<uintptr_t>(hookTarget) - imageBase;
    uintptr_t callerRva = reinterpret_cast<uintptr_t>(caller) - imageBase;
    uintptr_t parentCallerRva = reinterpret_cast<uintptr_t>(parentCaller) - imageBase;
    uintptr_t grandparentCallerRva = reinterpret_cast<uintptr_t>(grandparentCaller) - imageBase;
    bool hasDestination = destination != nullptr;
    if (kirikiri_embed_trace::ShouldLog(
            sourceHash,
            hookRva,
            callerRva,
            parentCallerRva,
            grandparentCallerRva,
            hasDestination)) {
        Log(kirikiri_embed_trace::FormatEvent(
            sourceHash,
            hookRva,
            callerRva,
            parentCallerRva,
            grandparentCallerRva,
            reinterpret_cast<uintptr_t>(destination),
            ShortLogText(Utf8ToWide(raw), 100)));
    }
    if (InterlockedIncrement(&gEmbedKrkrZAfterNewLogCount) <= 64) {
        Log(
            L"[kirikiri native] EMBED_AFTER_NEW consumed target=" +
            std::to_wstring(reinterpret_cast<uintptr_t>(hookTarget)) +
            L" raw_ecx=" + std::to_wstring(reinterpret_cast<uintptr_t>(text)) +
            L" replacement_ecx=" + std::to_wstring(reinterpret_cast<uintptr_t>(replacement)) +
            L" source_hash=" + std::to_wstring(EmbedSourceHash(raw)));
    }
    return replacement;
}

extern "C" __declspec(dllexport) void __stdcall KiriKiriPatchPsbWideResult(
    const char *source,
    wchar_t *destination) {
    std::string raw;
    std::wstring before;
    if (!TryReadNarrowCString(source, 4096, &raw) ||
        !TryReadWideCString(destination, 4096, &before)) {
        return;
    }
    std::wstring wideSource = Utf8ToWide(raw);
    std::wstring visible = RuntimeVisibleText(
        Utf8ToWide(kirikiri_embed::NormalizeEmbedKrkrZText(raw)));
    const std::wstring *translated = FindTranslationVariant(wideSource);
    if (!translated) translated = FindTranslationVariant(visible);
    if (!translated || translated->empty()) return;

    InterlockedIncrement(&gPsbPostHits);
    if (InterlockedIncrement(&gPsbPostLogCount) <= 80) {
        Log(
            std::wstring(L"[kirikiri native] psb post conversion readonly=1") +
            L" before=" + ShortLogText(before, 80) +
            L" expected=" + ShortLogText(*translated, 80));
    }
}

std::wstring KagParserVisibleText(const std::wstring &src) {
    std::wstring text = TrimWide(src);
    if (text.empty()) return {};
    if (text[0] == L'@') {
        std::wstring body = KagCommandBody(text);
        std::wstring cmdWord = ReadKagAttribute(body, L"word");
        if (cmdWord.empty()) cmdWord = ReadKagAttribute(body, L"text");
        if (!cmdWord.empty()) return cmdWord;
        return {};
    }
    if (text == L"line offset" || text == L" line offset ") return {};
    if (text[0] == L']') return {};
    if (text[0] == L' ' && text.size() <= 2) return {};
    if (text.size() >= 5 && text.substr(0, 3) == L" : " && text.substr(text.size() - 3) == L" : ") return {};
    if (text.size() >= 2 && text[0] == L'[' && text[text.size() - 1] == L']' &&
        text.rfind(L"[text]", 0) != 0 &&
        !(text.size() >= 3 && text.substr(text.size() - 3) == L"[c]" &&
          (text.rfind(L"[>>]", 0) == 0 || text.rfind(L"[地]", 0) == 0))) {
        return {};
    }

    std::wstring out = text;
    size_t pos = 0;
    while ((pos = out.find(L"[>>]", pos)) != std::wstring::npos) {
        out.replace(pos, 4, L"「");
        pos += 1;
    }
    pos = 0;
    while ((pos = out.find(L"[<<]", pos)) != std::wstring::npos) {
        out.replace(pos, 4, L"」");
        pos += 1;
    }
    out = StripKagTags(out, true);
    out = CollapseWideSpaces(out, true);
    if (out.size() <= 1) return {};
    return out;
}

void InitOverlaySharedMemory() {
    if (gOverlaySharedMemory) return;
    gOverlaySharedMemory = CreateFileMappingW(
        INVALID_HANDLE_VALUE,
        nullptr,
        PAGE_READWRITE,
        0,
        65536,
        L"kirikiri_translation_overlay"
    );
    if (gOverlaySharedMemory) {
        gOverlaySharedMemoryView = MapViewOfFile(
            gOverlaySharedMemory,
            FILE_MAP_WRITE,
            0,
            0,
            65536
        );
        if (!gOverlaySharedMemoryView) {
            DWORD firstError = GetLastError();
            gOverlaySharedMemoryView = MapViewOfFile(
                gOverlaySharedMemory,
                FILE_MAP_WRITE,
                0,
                0,
                0
            );
            if (gOverlaySharedMemoryView) {
                Log(L"[kirikiri native] overlay MapViewOfFile recovered with whole-section map, first_error=" +
                    std::to_wstring(firstError));
            }
        }
        if (gOverlaySharedMemoryView) {
            memset(gOverlaySharedMemoryView, 0, 65536);
        } else {
            Log(L"[kirikiri native] overlay MapViewOfFile failed, error=" + std::to_wstring(GetLastError()));
        }
    } else {
        Log(L"[kirikiri native] overlay CreateFileMapping failed, error=" + std::to_wstring(GetLastError()));
    }

    // 初始化嵌入控制共享内存
    gEmbedControlSharedMemory = CreateFileMappingW(
        INVALID_HANDLE_VALUE,
        nullptr,
        PAGE_READWRITE,
        0,
        4096,
        L"kirikiri_translation_overlay_control"
    );
    if (gEmbedControlSharedMemory) {
        gEmbedControlSharedMemoryView = MapViewOfFile(
            gEmbedControlSharedMemory,
            FILE_MAP_READ,
            0,
            0,
            4096
        );
        if (gEmbedControlSharedMemoryView) {
            Log(L"[kirikiri native] embed control shared memory initialized");
        } else {
            Log(L"[kirikiri native] embed control MapViewOfFile failed, error=" + std::to_wstring(GetLastError()));
        }
    } else {
        Log(L"[kirikiri native] embed control CreateFileMapping failed, error=" + std::to_wstring(GetLastError()));
    }
}

bool IsEmbedTextReplacementEnabled() {
    // 优先读取共享内存控制标志（实时控制）
    if (gEmbedControlSharedMemoryView) {
        unsigned int controlFlag = 0;
        memcpy(&controlFlag, gEmbedControlSharedMemoryView, sizeof(controlFlag));
        // 如果共享内存可用，直接使用共享内存的值
        return controlFlag != 0;
    }

    // 共享内存不可用时，回退到初始环境变量设置
    return gEmbedTextReplacement;
}

void WriteToOverlay(const std::wstring &original, const std::wstring &translated, const std::wstring &speaker = L"") {
    if (!gOverlaySharedMemoryView || original.empty()) return;

    std::string orig_utf8 = WideToUtf8(original);
    std::string trans_utf8 = WideToUtf8(translated);
    std::wstring speakerValue = speaker.empty() ? CurrentSpeakerName() : speaker;
    std::string speaker_utf8 = WideToUtf8(speakerValue);
    if (orig_utf8.empty()) return;

    // 共享内存结构 v2：
    // [4字节序号][4字节原文长度][原文UTF-8][4字节译文长度][译文UTF-8][4字节人名长度][人名UTF-8]
    unsigned int orig_len = (unsigned int)orig_utf8.size();
    unsigned int trans_len = (unsigned int)trans_utf8.size();
    unsigned int speaker_len = (unsigned int)speaker_utf8.size();

    if (orig_len + trans_len + speaker_len + 16 > 65536) return;  // 超出共享内存大小

    std::lock_guard<std::mutex> lock(gOverlayMutex);
    char *view = static_cast<char *>(gOverlaySharedMemoryView);
    int offset = 4;
    memcpy(view + offset, &orig_len, 4); offset += 4;
    memcpy(view + offset, orig_utf8.data(), orig_len); offset += orig_len;
    memcpy(view + offset, &trans_len, 4); offset += 4;
    if (trans_len > 0) {
        memcpy(view + offset, trans_utf8.data(), trans_len);
        offset += trans_len;
    }
    memcpy(view + offset, &speaker_len, 4); offset += 4;
    if (speaker_len > 0) {
        memcpy(view + offset, speaker_utf8.data(), speaker_len);
        offset += speaker_len;
    }
    MemoryBarrier();
    LONG seq = InterlockedIncrement(&gOverlaySequence);
    memcpy(view, &seq, 4);
    InterlockedIncrement(&gOverlayWriteCount);
    FlushViewOfFile(view, static_cast<SIZE_T>(offset));
}

const wchar_t *ResolveKagParserArgument(const wchar_t *text, int maxChars) {
    InterlockedIncrement(&gInternalKagEntryHits);
    if (!text) return text;
    std::wstring src;
    if (!TryReadWideCString(text, maxChars, &src)) return text;
    std::wstring commandBody = KagCommandBody(src);
    if (!commandBody.empty()) {
        std::wstring speaker;
        if (TryExtractKagSpeakerName(src, &speaker)) {
            SetCurrentSpeakerName(speaker);
            AppendRuntimeCapture(
                speaker.empty() ? L"<narration>" : speaker,
                L"kagparser_speaker",
                L"speaker",
                src);
            LONG sample = InterlockedIncrement(&gInternalKagSkipSamples);
            if (sample <= 80) {
                Log(L"[kirikiri native] kag speaker: " + (speaker.empty() ? L"<narration>" : speaker));
            }
            if (ReadKagAttribute(commandBody, L"word").empty()) {
                return text;
            }
        }
    }
    std::wstring visible = KagParserVisibleText(src);
    if (visible.empty() || !IsInternalCandidateText(visible)) {
        LONG sample = InterlockedIncrement(&gInternalKagSkipSamples);
        if (sample <= 40 && !TrimWide(src).empty()) {
            Log(L"[kirikiri native] kag skip: raw=" + ShortLogText(TrimWide(src)) +
                L" visible=" + ShortLogText(visible));
        }
        return text;
    }
    if (!src.empty() && src[0] == L'@') {
        const std::wstring *commandTranslated = FindTranslationVariant(visible);
        if (commandTranslated && !commandTranslated->empty() && *commandTranslated != visible) {
            WriteToOverlay(visible, *commandTranslated);
        } else {
            InterlockedIncrement(&gInternalMissCount);
            AppendRuntimeCapture(visible, L"kagparser_command", L"text", src);
            WriteToOverlay(visible, L"");
            LONG sample = InterlockedIncrement(&gInternalKagMissSamples);
            if (sample <= 80) {
                Log(L"[kirikiri native] kag command miss: " + visible);
            }
        }
        return text;
    }
    const std::wstring *translated = FindTranslationVariant(visible);
    if (!translated || translated->empty()) {
        InterlockedIncrement(&gInternalMissCount);
        AppendRuntimeCapture(visible, L"kagparser", L"text", src);
        LONG sample = InterlockedIncrement(&gInternalKagMissSamples);
        if (sample <= 80) {
            Log(L"[kirikiri native] kag miss: " + visible);
        }
        WriteToOverlay(visible, L"");  // 原文，无译文
        return text;
    }
    std::wstring displayOut = *translated;
    if (displayOut.empty() || displayOut == visible) return text;
    if (!gVmTextReplacement) {
        InterlockedIncrement(&gInternalKagHits);
        LONG sample = InterlockedIncrement(&gInternalKagReplaceSamples);
        if (sample <= 80) {
            Log(L"[kirikiri native] kag display: " + ShortLogText(visible) +
                L" => " + ShortLogText(displayOut));
        }
        WriteToOverlay(visible, displayOut);
        return text;
    }
    std::wstring out = displayOut;
    std::wstring trimmed = TrimWide(src);
    if (trimmed.size() >= 7 && trimmed.rfind(L"[>>]", 0) == 0 && trimmed.find(L"[<<]") != std::wstring::npos) {
        std::wstring inner = TrimWide(out);
        if (inner.size() >= 2 && inner.front() == L'「' && inner.back() == L'」') {
            inner = inner.substr(1, inner.size() - 2);
        }
        out = L"[>>]" + inner + L"[<<]";
        if (trimmed.size() >= 3 && trimmed.substr(trimmed.size() - 3) == L"[c]") out += L"[c]";
    } else if (trimmed.size() >= 6 && trimmed.rfind(L"[地]", 0) == 0) {
        out = L"[地]" + out;
        if (trimmed.size() >= 3 && trimmed.substr(trimmed.size() - 3) == L"[c]") out += L"[c]";
    } else if (src.find(L'[') != std::wstring::npos || src.find(L']') != std::wstring::npos) {
        return text;
    }
    {
        std::lock_guard<std::mutex> lock(gInternalTextMutex);
        auto inserted = gInternalDynamicTranslations.emplace(src, out);
        InterlockedIncrement(&gInternalKagHits);
        LONG sample = InterlockedIncrement(&gInternalKagReplaceSamples);
        if (sample <= 80) {
            Log(L"[kirikiri native] kag replace: " + ShortLogText(visible) +
                L" => " + ShortLogText(out));
        }
        WriteToOverlay(visible, out);
        InterlockedIncrement(&gInternalReplaceCount);
        return inserted.first->second.c_str();
    }
}

extern "C" __declspec(dllexport) const wchar_t *__stdcall KiriKiriResolveKagParserArgument(
    const wchar_t *text, int maxChars) {
    return ResolveKagParserArgument(text, maxChars);
}

bool WriteWideReplacement(const std::wstring &replacement, bool wantsNul, LPWSTR wide, int cch, int *written) {
    if (written) *written = 0;
    const int required = static_cast<int>(replacement.size()) + (wantsNul ? 1 : 0);
    if (!wide || cch == 0) {
        if (written) *written = required;
        return true;
    }
    if (cch < required) {
        return false;
    }
    if (!replacement.empty()) {
        memcpy(wide, replacement.data(), replacement.size() * sizeof(wchar_t));
    }
    if (wantsNul) wide[replacement.size()] = 0;
    if (written) *written = required;
    return true;
}

bool ResolvePlaceholderText(const std::wstring &src, std::wstring *out) {
    if (!out || src.empty() || gPlaceholders.empty()) return false;
    auto exact = gPlaceholders.find(src);
    if (exact != gPlaceholders.end()) {
        *out = exact->second;
        return true;
    }
    if (src.size() > 2048 || src.find(L"GT") == std::wstring::npos) return false;
    std::wstring replaced = src;
    bool changed = false;
    for (const auto &entry : gPlaceholders) {
        const std::wstring &token = entry.first;
        size_t pos = 0;
        while ((pos = replaced.find(token, pos)) != std::wstring::npos) {
            replaced.replace(pos, token.size(), entry.second);
            pos += entry.second.size();
            changed = true;
        }
    }
    if (!changed) return false;
    *out = replaced;
    return true;
}

int ScaleFontHeight(int value) {
    if (value == 0) return value;
    int absValue = value < 0 ? -value : value;
    if (absValue < 8) return value;
    int scaled = static_cast<int>(absValue * gFontHeightScale + 0.5);
    if (scaled < 1) scaled = 1;
    return value < 0 ? -scaled : scaled;
}

void PatchLogFontW(LOGFONTW *lf, bool scaleHeight) {
    if (!lf) return;
    if (scaleHeight) lf->lfHeight = ScaleFontHeight(lf->lfHeight);
    lf->lfCharSet = kChineseCharset;
    lf->lfQuality = kFontQuality;
    lstrcpynW(lf->lfFaceName, kChineseFontFaceW, LF_FACESIZE);
}

void PatchLogFontA(LOGFONTA *lf, bool scaleHeight) {
    if (!lf) return;
    if (scaleHeight) lf->lfHeight = ScaleFontHeight(lf->lfHeight);
    lf->lfCharSet = kChineseCharset;
    lf->lfQuality = kFontQuality;
    lstrcpynA(lf->lfFaceName, kChineseFontFaceA, LF_FACESIZE);
}

std::wstring FontKey(const LOGFONTW &lf) {
    return std::to_wstring(lf.lfHeight) + L":" +
           std::to_wstring(lf.lfWidth) + L":" +
           std::to_wstring(lf.lfWeight) + L":" +
           std::to_wstring(lf.lfItalic) + L":" +
           std::to_wstring(lf.lfCharSet) + L":" +
           lf.lfFaceName;
}

HFONT GetChineseFontForHdc(HDC hdc) {
    if (!hdc || !RealCreateFontIndirectW) return nullptr;
    LOGFONTW lf = {};
    bool copied = false;
    HGDIOBJ current = GetCurrentObject(hdc, OBJ_FONT);
    if (current && GetObjectW(current, sizeof(lf), &lf) > 0) copied = true;
    if (!copied) {
        lf.lfHeight = -18;
        lf.lfWeight = FW_NORMAL;
    }
    PatchLogFontW(&lf, true);
    std::wstring key = FontKey(lf);
    std::lock_guard<std::mutex> lock(gFontMutex);
    auto it = gFontCache.find(key);
    if (it != gFontCache.end() && it->second) return it->second;
    InterlockedIncrement(&gFontCreateDepth);
    HFONT font = RealCreateFontIndirectW(&lf);
    InterlockedDecrement(&gFontCreateDepth);
    if (font) gFontCache[key] = font;
    return font;
}

HGDIOBJ SelectChineseFont(HDC hdc) {
    HFONT font = GetChineseFontForHdc(hdc);
    if (!font) return nullptr;
    InterlockedIncrement(&gFontSelectCount);
    return SelectObject(hdc, font);
}

void RestoreFont(HDC hdc, HGDIOBJ oldFont) {
    if (hdc && oldFont) SelectObject(hdc, oldFont);
}

BOOL DrawWideTextOut(HDC hdc, int x, int y, const std::wstring &text) {
    if (!RealTextOutW) return FALSE;
    HGDIOBJ oldFont = ContainsCjk(text) ? SelectChineseFont(hdc) : nullptr;
    InterlockedIncrement(&gWideDrawDepth);
    BOOL ok = RealTextOutW(hdc, x, y, text.c_str(), static_cast<int>(text.size()));
    InterlockedDecrement(&gWideDrawDepth);
    RestoreFont(hdc, oldFont);
    return ok;
}

BOOL DrawWideExtTextOut(HDC hdc, int x, int y, UINT options, const RECT *rect, const std::wstring &text, const INT *dx) {
    if (!RealExtTextOutW) return FALSE;
    HGDIOBJ oldFont = ContainsCjk(text) ? SelectChineseFont(hdc) : nullptr;
    InterlockedIncrement(&gWideDrawDepth);
    BOOL ok = RealExtTextOutW(hdc, x, y, options, rect, text.c_str(), static_cast<UINT>(text.size()), dx);
    InterlockedDecrement(&gWideDrawDepth);
    RestoreFont(hdc, oldFont);
    return ok;
}

int DrawWideDrawText(HDC hdc, const std::wstring &text, LPRECT rect, UINT format) {
    if (!RealDrawTextW) return 0;
    HGDIOBJ oldFont = ContainsCjk(text) ? SelectChineseFont(hdc) : nullptr;
    InterlockedIncrement(&gWideDrawDepth);
    int ok = RealDrawTextW(hdc, text.c_str(), static_cast<int>(text.size()), rect, format);
    InterlockedDecrement(&gWideDrawDepth);
    RestoreFont(hdc, oldFont);
    return ok;
}

const std::wstring *ResolveTranslation(const std::wstring &src) {
    if (!NeedsTranslate(src)) return nullptr;
    const std::wstring *translated = FindTranslation(src);
    if (!translated) {
        InterlockedIncrement(&gMissedCount);
        PublishRuntimeOverlayText(src, L"gdi_render");
    }
    return translated;
}

bool ResolveAnsiText(const char *text, int count, std::wstring *resolved, bool *isTunnel) {
    if (isTunnel) *isTunnel = false;
    if (!text || !resolved) return false;
    int len = count;
    if (len < 0) len = static_cast<int>(strnlen(text, 4097));
    if (len <= 0 || len > 4096) return false;
    std::wstring tunneled;
    if (DecodeCp932Tunnel(text, len, &tunneled) && !tunneled.empty()) {
        *resolved = tunneled;
        if (isTunnel) *isTunnel = true;
        return true;
    }
    std::wstring src = ReadAnsiText(text, len);
    std::wstring placeholder;
    if (ResolvePlaceholderText(src, &placeholder)) {
        *resolved = placeholder;
        if (isTunnel) *isTunnel = true;
        return true;
    }
    if (DecodeWidePuaTunnel(src.c_str(), static_cast<int>(src.size()), &tunneled) && !tunneled.empty()) {
        *resolved = tunneled;
        if (isTunnel) *isTunnel = true;
        return true;
    }
    const std::wstring *translated = ResolveTranslation(src);
    if (!translated) return false;
    *resolved = *translated;
    return true;
}

BOOL WINAPI HookTextOutA(HDC hdc, int x, int y, LPCSTR text, int count) {
    std::wstring out;
    bool tunneled = false;
    if (ResolveAnsiText(text, count, &out, &tunneled)) {
        if (tunneled) InterlockedIncrement(&gTunnelCount);
        else InterlockedIncrement(&gReplacedCount);
        return DrawWideTextOut(hdc, x, y, out);
    }
    std::wstring src = ReadAnsiText(text, count);
    HGDIOBJ oldFont = ContainsCjk(src) ? SelectChineseFont(hdc) : nullptr;
    BOOL ok = RealTextOutA ? RealTextOutA(hdc, x, y, text, count) : FALSE;
    RestoreFont(hdc, oldFont);
    return ok;
}

BOOL WINAPI HookTextOutW(HDC hdc, int x, int y, LPCWSTR text, int count) {
    if (gWideDrawDepth > 0) return RealTextOutW ? RealTextOutW(hdc, x, y, text, count) : FALSE;
    std::wstring src = ReadWideText(text, count);
    std::wstring placeholder;
    if (ResolvePlaceholderText(src, &placeholder)) {
        InterlockedIncrement(&gTunnelCount);
        return DrawWideTextOut(hdc, x, y, placeholder);
    }
    const std::wstring *translated = ResolveTranslation(src);
    const std::wstring &out = translated ? *translated : src;
    if (translated) InterlockedIncrement(&gReplacedCount);
    HGDIOBJ oldFont = ContainsCjk(out) ? SelectChineseFont(hdc) : nullptr;
    InterlockedIncrement(&gWideDrawDepth);
    BOOL ok = RealTextOutW ? RealTextOutW(hdc, x, y, out.c_str(), static_cast<int>(out.size())) : FALSE;
    InterlockedDecrement(&gWideDrawDepth);
    RestoreFont(hdc, oldFont);
    return ok;
}

BOOL WINAPI HookExtTextOutA(HDC hdc, int x, int y, UINT options, const RECT *rect, LPCSTR text, UINT count, const INT *dx) {
    std::wstring out;
    bool tunneled = false;
    if (ResolveAnsiText(text, static_cast<int>(count), &out, &tunneled)) {
        if (tunneled) InterlockedIncrement(&gTunnelCount);
        else InterlockedIncrement(&gReplacedCount);
        return DrawWideExtTextOut(hdc, x, y, options, rect, out, dx);
    }
    std::wstring src = ReadAnsiText(text, static_cast<int>(count));
    HGDIOBJ oldFont = ContainsCjk(src) ? SelectChineseFont(hdc) : nullptr;
    BOOL ok = RealExtTextOutA ? RealExtTextOutA(hdc, x, y, options, rect, text, count, dx) : FALSE;
    RestoreFont(hdc, oldFont);
    return ok;
}

BOOL WINAPI HookExtTextOutW(HDC hdc, int x, int y, UINT options, const RECT *rect, LPCWSTR text, UINT count, const INT *dx) {
    if (gWideDrawDepth > 0) return RealExtTextOutW ? RealExtTextOutW(hdc, x, y, options, rect, text, count, dx) : FALSE;
    std::wstring src = ReadWideText(text, static_cast<int>(count));
    std::wstring placeholder;
    if (ResolvePlaceholderText(src, &placeholder)) {
        InterlockedIncrement(&gTunnelCount);
        return DrawWideExtTextOut(hdc, x, y, options, rect, placeholder, dx);
    }
    const std::wstring *translated = ResolveTranslation(src);
    const std::wstring &out = translated ? *translated : src;
    if (translated) InterlockedIncrement(&gReplacedCount);
    HGDIOBJ oldFont = ContainsCjk(out) ? SelectChineseFont(hdc) : nullptr;
    InterlockedIncrement(&gWideDrawDepth);
    BOOL ok = RealExtTextOutW ? RealExtTextOutW(hdc, x, y, options, rect, out.c_str(), static_cast<UINT>(out.size()), dx) : FALSE;
    InterlockedDecrement(&gWideDrawDepth);
    RestoreFont(hdc, oldFont);
    return ok;
}

int WINAPI HookDrawTextA(HDC hdc, LPCSTR text, int count, LPRECT rect, UINT format) {
    std::wstring out;
    bool tunneled = false;
    if (ResolveAnsiText(text, count, &out, &tunneled)) {
        if (tunneled) InterlockedIncrement(&gTunnelCount);
        else InterlockedIncrement(&gReplacedCount);
        return DrawWideDrawText(hdc, out, rect, format);
    }
    std::wstring src = ReadAnsiText(text, count);
    HGDIOBJ oldFont = ContainsCjk(src) ? SelectChineseFont(hdc) : nullptr;
    int ok = RealDrawTextA ? RealDrawTextA(hdc, text, count, rect, format) : 0;
    RestoreFont(hdc, oldFont);
    return ok;
}

int WINAPI HookDrawTextW(HDC hdc, LPCWSTR text, int count, LPRECT rect, UINT format) {
    if (gWideDrawDepth > 0) return RealDrawTextW ? RealDrawTextW(hdc, text, count, rect, format) : 0;
    std::wstring src = ReadWideText(text, count);
    std::wstring placeholder;
    if (ResolvePlaceholderText(src, &placeholder)) {
        InterlockedIncrement(&gTunnelCount);
        return DrawWideDrawText(hdc, placeholder, rect, format);
    }
    const std::wstring *translated = ResolveTranslation(src);
    const std::wstring &out = translated ? *translated : src;
    if (translated) InterlockedIncrement(&gReplacedCount);
    HGDIOBJ oldFont = ContainsCjk(out) ? SelectChineseFont(hdc) : nullptr;
    InterlockedIncrement(&gWideDrawDepth);
    int ok = RealDrawTextW ? RealDrawTextW(hdc, out.c_str(), static_cast<int>(out.size()), rect, format) : 0;
    InterlockedDecrement(&gWideDrawDepth);
    RestoreFont(hdc, oldFont);
    return ok;
}

HFONT WINAPI HookCreateFontW(int h, int w, int esc, int orient, int weight, DWORD italic, DWORD underline, DWORD strike,
                             DWORD charset, DWORD outPrecision, DWORD clipPrecision, DWORD quality, DWORD pitch, LPCWSTR face) {
    if (gFontCreateDepth <= 0) {
        h = ScaleFontHeight(h);
        charset = kChineseCharset;
        quality = kFontQuality;
        face = kChineseFontFaceW;
    }
    return RealCreateFontW ? RealCreateFontW(h, w, esc, orient, weight, italic, underline, strike, charset, outPrecision, clipPrecision, quality, pitch, face) : nullptr;
}

HFONT WINAPI HookCreateFontA(int h, int w, int esc, int orient, int weight, DWORD italic, DWORD underline, DWORD strike,
                             DWORD charset, DWORD outPrecision, DWORD clipPrecision, DWORD quality, DWORD pitch, LPCSTR face) {
    if (gFontCreateDepth <= 0) {
        h = ScaleFontHeight(h);
        charset = kChineseCharset;
        quality = kFontQuality;
        face = kChineseFontFaceA;
    }
    return RealCreateFontA ? RealCreateFontA(h, w, esc, orient, weight, italic, underline, strike, charset, outPrecision, clipPrecision, quality, pitch, face) : nullptr;
}

HFONT WINAPI HookCreateFontIndirectW(const LOGFONTW *lf) {
    if (!lf || gFontCreateDepth > 0) return RealCreateFontIndirectW ? RealCreateFontIndirectW(lf) : nullptr;
    LOGFONTW patched = *lf;
    PatchLogFontW(&patched, true);
    return RealCreateFontIndirectW ? RealCreateFontIndirectW(&patched) : nullptr;
}

HFONT WINAPI HookCreateFontIndirectA(const LOGFONTA *lf) {
    if (!lf || gFontCreateDepth > 0) return RealCreateFontIndirectA ? RealCreateFontIndirectA(lf) : nullptr;
    LOGFONTA patched = *lf;
    PatchLogFontA(&patched, true);
    return RealCreateFontIndirectA ? RealCreateFontIndirectA(&patched) : nullptr;
}

int WINAPI HookMultiByteToWideChar(UINT codePage, DWORD flags, LPCCH mb, int cb, LPWSTR wide, int cch) {
    if (mb && (codePage == 932 || codePage == CP_ACP)) {
        std::wstring tunneled;
        if (DecodeCp932Tunnel(mb, cb, &tunneled) && !tunneled.empty()) {
            const int required = static_cast<int>(tunneled.size()) + (cb < 0 ? 1 : 0);
            if (!wide || cch == 0) {
                return required;
            }
            int copy = std::min<int>(static_cast<int>(tunneled.size()), cch - (cb < 0 ? 1 : 0));
            if (copy < 0) copy = 0;
            if (copy > 0) {
                memcpy(wide, tunneled.data(), copy * sizeof(wchar_t));
            }
            if (cb < 0 && copy < cch) wide[copy] = 0;
            InterlockedIncrement(&gTunnelCount);
            return cb < 0 ? copy + 1 : copy;
        }
        int len = cb;
        if (len < 0) len = static_cast<int>(strnlen(mb, 4097));
        if (len > 0 && len <= 4096) {
            std::vector<char> bytes(mb, mb + len);
            std::wstring src = DecodeCp932Bytes(bytes);
            const std::wstring *translated = NeedsTranslate(src) ? FindTranslation(src) : nullptr;
            if (translated && !translated->empty()) {
                int written = 0;
                if (WriteWideReplacement(*translated, cb < 0, wide, cch, &written)) {
                    InterlockedIncrement(&gReplacedCount);
                    return written;
                }
                InterlockedIncrement(&gMissedCount);
            }
            std::wstring placeholder;
            if (ResolvePlaceholderText(src, &placeholder)) {
                const int required = static_cast<int>(placeholder.size()) + (cb < 0 ? 1 : 0);
                if (!wide || cch == 0) return required;
                int copy = std::min<int>(static_cast<int>(placeholder.size()), cch - (cb < 0 ? 1 : 0));
                if (copy < 0) copy = 0;
                if (copy > 0) memcpy(wide, placeholder.data(), copy * sizeof(wchar_t));
                if (cb < 0 && copy < cch) wide[copy] = 0;
                InterlockedIncrement(&gTunnelCount);
                return cb < 0 ? copy + 1 : copy;
            }
        }
    }
    return RealMultiByteToWideChar ? RealMultiByteToWideChar(codePage, flags, mb, cb, wide, cch) : 0;
}

int WINAPI HookWideCharToMultiByte(UINT codePage, DWORD flags, LPCWCH wide, int cch, LPSTR mb, int cb, LPCCH defaultChar, LPBOOL usedDefaultChar) {
    if (wide && (codePage == 932 || codePage == CP_ACP)) {
        std::string tunneled;
        if (EncodeWidePuaTunnelToCp932(wide, cch, &tunneled) && !tunneled.empty()) {
            if (usedDefaultChar) *usedDefaultChar = FALSE;
            if (!mb || cb == 0) {
                return static_cast<int>(tunneled.size());
            }
            int copy = std::min<int>(static_cast<int>(tunneled.size()), cb);
            if (copy > 0) memcpy(mb, tunneled.data(), copy);
            InterlockedIncrement(&gTunnelCount);
            return copy;
        }
    }
    return RealWideCharToMultiByte
               ? RealWideCharToMultiByte(codePage, flags, wide, cch, mb, cb, defaultChar, usedDefaultChar)
               : 0;
}

const std::wstring *ResolveGdipString(const WCHAR *text, int count) {
    if (!text) return nullptr;
    std::wstring src = ReadWideText(text, count);
    return ResolveTranslation(src);
}

int WINAPI HookGdipDrawString(void *graphics, const WCHAR *text, int count, const void *font,
                              const void *layoutRect, const void *format, const void *brush) {
    const std::wstring *translated = ResolveGdipString(text, count);
    if (translated && RealGdipDrawString) {
        InterlockedIncrement(&gReplacedCount);
        InterlockedIncrement(&gGdipRenderReplaceCount);
        return RealGdipDrawString(
            graphics,
            translated->c_str(),
            static_cast<int>(translated->size()),
            font,
            layoutRect,
            format,
            brush);
    }
    return RealGdipDrawString ? RealGdipDrawString(graphics, text, count, font, layoutRect, format, brush) : 1;
}

int WINAPI HookGdipMeasureString(void *graphics, const WCHAR *text, int count, const void *font,
                                 const void *layoutRect, const void *format, void *boundingBox,
                                 int *codepointsFitted, int *linesFilled) {
    const std::wstring *translated = ResolveGdipString(text, count);
    if (translated && RealGdipMeasureString) {
        InterlockedIncrement(&gGdipMeasureReplaceCount);
        return RealGdipMeasureString(
            graphics,
            translated->c_str(),
            static_cast<int>(translated->size()),
            font,
            layoutRect,
            format,
            boundingBox,
            codepointsFitted,
            linesFilled);
    }
    return RealGdipMeasureString
               ? RealGdipMeasureString(graphics, text, count, font, layoutRect, format, boundingBox, codepointsFitted, linesFilled)
               : 1;
}

int WINAPI HookGdipAddPathString(void *path, const WCHAR *text, int count, const void *family,
                                 int style, float emSize, const void *layoutRect, const void *format) {
    const std::wstring *translated = ResolveGdipString(text, count);
    if (translated && RealGdipAddPathString) {
        InterlockedIncrement(&gReplacedCount);
        InterlockedIncrement(&gGdipRenderReplaceCount);
        return RealGdipAddPathString(
            path,
            translated->c_str(),
            static_cast<int>(translated->size()),
            family,
            style,
            emSize,
            layoutRect,
            format);
    }
    return RealGdipAddPathString ? RealGdipAddPathString(path, text, count, family, style, emSize, layoutRect, format) : 1;
}

int WINAPI HookGdipAddPathStringI(void *path, const WCHAR *text, int count, const void *family,
                                  int style, float emSize, const void *layoutRect, const void *format) {
    const std::wstring *translated = ResolveGdipString(text, count);
    if (translated && RealGdipAddPathStringI) {
        InterlockedIncrement(&gReplacedCount);
        InterlockedIncrement(&gGdipRenderReplaceCount);
        return RealGdipAddPathStringI(
            path,
            translated->c_str(),
            static_cast<int>(translated->size()),
            family,
            style,
            emSize,
            layoutRect,
            format);
    }
    return RealGdipAddPathStringI ? RealGdipAddPathStringI(path, text, count, family, style, emSize, layoutRect, format) : 1;
}

int WINAPI HookGdipCreateFontFamilyFromName(const WCHAR *name, void *fontCollection, void **family) {
    InterlockedIncrement(&gGdipFontFamilyCount);
    if (RealGdipCreateFontFamilyFromName) {
        int status = RealGdipCreateFontFamilyFromName(kChineseFontFaceW, fontCollection, family);
        if (status == 0 && family && *family) return status;
        return RealGdipCreateFontFamilyFromName(name, fontCollection, family);
    }
    return 1;
}

bool ReadWholeFile(const std::wstring &path, std::string *out) {
    if (!out) return false;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;

    LARGE_INTEGER size = {};
    if (!GetFileSizeEx(h, &size) || size.QuadPart < 0 || size.QuadPart > 8 * 1024 * 1024) {
        CloseHandle(h);
        return false;
    }
    out->assign(static_cast<size_t>(size.QuadPart), '\0');
    DWORD read = 0;
    bool ok = out->empty() ||
              ReadFile(h, out->data(), static_cast<DWORD>(out->size()), &read, nullptr);
    CloseHandle(h);
    if (!ok || read != out->size()) return false;
    return true;
}

std::wstring NormalizeSlashes(std::wstring value) {
    std::replace(value.begin(), value.end(), L'\\', L'/');
    while (value.rfind(L"./", 0) == 0) value.erase(0, 2);
    while (!value.empty() && value.front() == L'/') value.erase(0, 1);
    return value;
}

std::wstring SanitizeRelativePath(std::wstring value) {
    value = NormalizeSlashes(value);
    std::vector<std::wstring> parts;
    size_t start = 0;
    while (start <= value.size()) {
        size_t end = value.find(L'/', start);
        if (end == std::wstring::npos) end = value.size();
        std::wstring part = value.substr(start, end - start);
        if (!part.empty() && part != L"." && part != L"..") {
            for (wchar_t &ch : part) {
                if (ch < 32 || wcschr(L"<>:\"|?*", ch)) ch = L'_';
            }
            parts.push_back(part);
        }
        if (end == value.size()) break;
        start = end + 1;
    }
    std::wstring out;
    for (const auto &part : parts) {
        if (!out.empty()) out += L"\\";
        out += part;
    }
    return out;
}

std::wstring NormalizePatchEntry(std::wstring value) {
    value = NormalizeSlashes(value);
    std::transform(value.begin(), value.end(), value.begin(), towlower);
    return value;
}

std::wstring PatchBasenameKey(const std::wstring &value) {
    std::wstring normalized = NormalizePatchEntry(value);
    size_t slash = normalized.find_last_of(L"/\\");
    if (slash != std::wstring::npos && slash + 1 < normalized.size()) {
        return normalized.substr(slash + 1);
    }
    return normalized;
}

std::wstring TargetInnerName(const std::wstring &value) {
    std::wstring normalized = NormalizeSlashes(value);
    size_t sep = normalized.find(L'>');
    if (sep != std::wstring::npos && sep + 1 < normalized.size()) {
        return normalized.substr(sep + 1);
    }
    return normalized;
}

std::wstring TargetArchiveName(const std::wstring &value) {
    std::wstring normalized = NormalizeSlashes(value);
    size_t sep = normalized.find(L'>');
    if (sep != std::wstring::npos && sep > 0) {
        return normalized.substr(0, sep);
    }
    return L"";
}

bool StartsWithNoCase(const std::wstring &value, const std::wstring &prefix) {
    if (value.size() < prefix.size()) return false;
    return _wcsnicmp(value.c_str(), prefix.c_str(), prefix.size()) == 0;
}

bool EndsWithNoCase(const std::wstring &value, const std::wstring &suffix) {
    if (value.size() < suffix.size()) return false;
    return _wcsnicmp(value.c_str() + value.size() - suffix.size(), suffix.c_str(), suffix.size()) == 0;
}

std::wstring StripStorageArchivePrefix(std::wstring value) {
    value = NormalizeSlashes(value);
    size_t sep = value.find(L'>');
    if (sep != std::wstring::npos && sep + 1 < value.size()) {
        value = value.substr(sep + 1);
    }
    size_t slash = value.find(L'/');
    if (slash != std::wstring::npos && slash > 0) {
        std::wstring archive = value.substr(0, slash);
        if (EndsWithNoCase(archive, L".xp3")) {
            value = value.substr(slash + 1);
        }
    }
    while (value.rfind(L"./", 0) == 0) value.erase(0, 2);
    while (!value.empty() && value.front() == L'/') value.erase(0, 1);
    return value;
}

bool TryCopyTjsString(const tTJSString &source, wchar_t *buffer, int capacity, int *length) {
    if (!buffer || capacity <= 0 || !length) return false;
    buffer[0] = L'\0';
    *length = 0;
    __try {
        tTJSVariantString_S *ptr = source.Ptr;
        if (!ptr) return true;
        if (ptr->Length < 0 || ptr->Length > 4096) return false;
        if (ptr->Length >= capacity) return false;
        const wchar_t *value = ptr->LongString ? ptr->LongString : ptr->ShortString;
        if (!value) return true;
        for (int i = 0; i < ptr->Length; ++i) {
            buffer[i] = value[i];
        }
        buffer[ptr->Length] = L'\0';
        *length = ptr->Length;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

bool TryReadTjsString(const tTJSString &source, std::wstring *out) {
    if (!out) return false;
    out->clear();
    wchar_t buffer[4097] = {};
    int length = 0;
    if (!TryCopyTjsString(source, buffer, static_cast<int>(sizeof(buffer) / sizeof(buffer[0])), &length)) return false;
    if (length > 0) out->assign(buffer, buffer + length);
    return true;
}

class TjsStringHolder {
public:
    explicit TjsStringHolder(const std::wstring &value) : value_(value) {
        if (TJSAllocVariantString) {
            string_.Ptr = TJSAllocVariantString(value_.c_str());
        }
        if (!string_.Ptr) {
            memset(&borrowed_, 0, sizeof(borrowed_));
            borrowed_.RefCount = 1;
            borrowed_.Length = static_cast<tjs_int>(value_.size());
            if (value_.size() < 22) {
                memcpy(borrowed_.ShortString, value_.c_str(), (value_.size() + 1) * sizeof(wchar_t));
                borrowed_.LongString = nullptr;
            } else {
                borrowed_.LongString = const_cast<wchar_t *>(value_.c_str());
            }
            string_.Ptr = &borrowed_;
            borrowedMode_ = true;
        }
    }

    ~TjsStringHolder() {
        if (!borrowedMode_ && string_.Ptr && TJSVariantStringRelease) {
            TJSVariantStringRelease(string_.Ptr);
        }
    }

    const tTJSString &get() const {
        return string_;
    }

private:
    std::wstring value_;
    tTJSString string_ = {};
    tTJSVariantString_S borrowed_ = {};
    bool borrowedMode_ = false;
};

bool QueryFunction(iTVPFunctionExporter *exporter, const char *name, void **out) {
    if (!exporter || !out) return false;
    const char *names[1] = {name};
    void *funcs[1] = {nullptr};
    if (!exporter->QueryFunctionsByNarrowString(names, funcs, 1) || !funcs[0]) return false;
    *out = funcs[0];
    return true;
}

bool IsRegisterStorageExportName(const char *name) {
    return name && strcmp(name, "void ::TVPRegisterStorageMedia(iTVPStorageMedia *)") == 0;
}

bool IsUnregisterStorageExportName(const char *name) {
    return name && strcmp(name, "void ::TVPUnregisterStorageMedia(iTVPStorageMedia *)") == 0;
}

bool IsCreateIStreamExportName(const char *name) {
    return name && strcmp(name, "IStream * ::TVPCreateIStream(const ttstr &,tjs_uint32)") == 0;
}

bool IsRegisterStorageExportName(const wchar_t *name) {
    return name && wcscmp(name, L"void ::TVPRegisterStorageMedia(iTVPStorageMedia *)") == 0;
}

bool IsUnregisterStorageExportName(const wchar_t *name) {
    return name && wcscmp(name, L"void ::TVPUnregisterStorageMedia(iTVPStorageMedia *)") == 0;
}

bool IsCreateIStreamExportName(const wchar_t *name) {
    return name && wcscmp(name, L"IStream * ::TVPCreateIStream(const ttstr &,tjs_uint32)") == 0;
}

class ProxyFunctionExporter : public iTVPFunctionExporter {
public:
    explicit ProxyFunctionExporter(iTVPFunctionExporter *real) : real_(real) {}

    bool __cdecl QueryFunctionsByString(const tjs_char **names, void **functions, int count) override {
        if (!real_ || !real_->QueryFunctionsByString(names, functions, count)) return false;
        PatchWide(names, functions, count);
        return true;
    }

    bool __cdecl QueryFunctionsByNarrowString(const char **names, void **functions, int count) override {
        if (!real_ || !real_->QueryFunctionsByNarrowString(names, functions, count)) return false;
        PatchNarrow(names, functions, count);
        return true;
    }

    bool __cdecl QueryFunctionsByHash(const tjs_uint32 *hash, void **function, int count) override {
        return real_ && real_->QueryFunctionsByHash(hash, function, count);
    }

private:
    void PatchNarrow(const char **names, void **functions, int count) {
        if (!names || !functions) return;
        for (int i = 0; i < count; ++i) {
            if (IsRegisterStorageExportName(names[i]) && functions[i]) {
                if (!OriginalTVPRegisterStorageMedia) {
                    OriginalTVPRegisterStorageMedia = reinterpret_cast<TVPRegisterStorageMedia_t>(functions[i]);
                    Log(L"[kirikiri native] proxied TVPRegisterStorageMedia");
                }
                functions[i] = reinterpret_cast<void *>(HookTVPRegisterStorageMedia);
            } else if (IsUnregisterStorageExportName(names[i]) && functions[i]) {
                if (!OriginalTVPUnregisterStorageMedia) {
                    OriginalTVPUnregisterStorageMedia = reinterpret_cast<TVPUnregisterStorageMedia_t>(functions[i]);
                    Log(L"[kirikiri native] proxied TVPUnregisterStorageMedia");
                }
                functions[i] = reinterpret_cast<void *>(HookTVPUnregisterStorageMedia);
            } else if (IsCreateIStreamExportName(names[i]) && functions[i]) {
                if (!OriginalTVPCreateIStream) {
                    OriginalTVPCreateIStream = reinterpret_cast<TVPCreateIStream_t>(functions[i]);
                    TVPCreateIStream = OriginalTVPCreateIStream;
                    Log(L"[kirikiri native] proxied TVPCreateIStream");
                }
                functions[i] = reinterpret_cast<void *>(HookTVPCreateIStream);
            }
        }
    }

    void PatchWide(const tjs_char **names, void **functions, int count) {
        if (!names || !functions) return;
        for (int i = 0; i < count; ++i) {
            if (IsRegisterStorageExportName(names[i]) && functions[i]) {
                if (!OriginalTVPRegisterStorageMedia) {
                    OriginalTVPRegisterStorageMedia = reinterpret_cast<TVPRegisterStorageMedia_t>(functions[i]);
                    Log(L"[kirikiri native] proxied TVPRegisterStorageMedia");
                }
                functions[i] = reinterpret_cast<void *>(HookTVPRegisterStorageMedia);
            } else if (IsUnregisterStorageExportName(names[i]) && functions[i]) {
                if (!OriginalTVPUnregisterStorageMedia) {
                    OriginalTVPUnregisterStorageMedia = reinterpret_cast<TVPUnregisterStorageMedia_t>(functions[i]);
                    Log(L"[kirikiri native] proxied TVPUnregisterStorageMedia");
                }
                functions[i] = reinterpret_cast<void *>(HookTVPUnregisterStorageMedia);
            } else if (IsCreateIStreamExportName(names[i]) && functions[i]) {
                if (!OriginalTVPCreateIStream) {
                    OriginalTVPCreateIStream = reinterpret_cast<TVPCreateIStream_t>(functions[i]);
                    TVPCreateIStream = OriginalTVPCreateIStream;
                    Log(L"[kirikiri native] proxied TVPCreateIStream");
                }
                functions[i] = reinterpret_cast<void *>(HookTVPCreateIStream);
            }
        }
    }

    iTVPFunctionExporter *real_ = nullptr;
};

bool IsAcceptedStorageName(const std::wstring &name, std::wstring *innerName) {
    if (name.empty()) return false;
    if (name.size() > 2 && name[1] == L':' && (name[2] == L'\\' || name[2] == L'/')) return false;

    // KiriKiri may expose an XP3 member as file://./data.xp3>entry in
    // addition to arc:// and archive:// names.
    if (StartsWithNoCase(name, L"file://")) {
        *innerName = StripStorageArchivePrefix(name.substr(7));
        return !innerName->empty();
    }

    const wchar_t *raw = name.c_str();
    (void)raw;
    for (const auto &protocol : gPatchProtocols) {
        if (StartsWithNoCase(name, protocol)) {
            *innerName = StripStorageArchivePrefix(name.substr(protocol.size()));
            return true;
        }
    }
    size_t archiveSep = name.find(L'>');
    if (archiveSep != std::wstring::npos && archiveSep + 1 < name.size()) {
        std::wstring archive = name.substr(0, archiveSep);
        std::wstring inner = name.substr(archiveSep + 1);
        if (!archive.empty() && EndsWithNoCase(archive, L".xp3") && !inner.empty()) {
            *innerName = StripStorageArchivePrefix(inner);
            return true;
        }
    }
    if (name.find(L"://") != std::wstring::npos) return false;
    if (gPatchNoProtocol) {
        *innerName = StripStorageArchivePrefix(name);
        return true;
    }
    return false;
}

bool ShouldSkipPatch(const std::wstring &inner) {
    std::wstring lowered = inner;
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), towlower);
    if (EndsWithNoCase(lowered, L".sig")) return true;
    if (EndsWithNoCase(lowered, L".exe") || EndsWithNoCase(lowered, L".dll")) return true;
    return false;
}

bool IsScriptPatchCandidate(const std::wstring &inner) {
    std::wstring lowered = inner;
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), towlower);
    return EndsWithNoCase(lowered, L".txt.scn") ||
           EndsWithNoCase(lowered, L".scn") ||
           EndsWithNoCase(lowered, L".ks") ||
           EndsWithNoCase(lowered, L".tjs");
}

bool DumpTargetListContains(const std::wstring &inner) {
    if (gDumpTargets.empty()) return false;
    std::wstring key = NormalizePatchEntry(inner);
    for (const auto &target : gDumpTargets) {
        if (NormalizePatchEntry(TargetInnerName(target)) == key) return true;
    }
    return false;
}

bool PreferScnTargetOverKs(const std::wstring &target) {
    if (!EndsWithNoCase(target, L".ks")) return false;
    return DumpTargetListContains(target + L".scn");
}

bool DumpTargetsContainOnlyExtensionlessEntries() {
    if (gActiveExtensionlessDump) return false;
    bool sawExtensionless = false;
    for (const auto &target : gDumpTargets) {
        std::wstring normalized = TargetInnerName(target);
        size_t slash = normalized.find_last_of(L"/\\");
        std::wstring name = slash == std::wstring::npos ? normalized : normalized.substr(slash + 1);
        if (name.find(L'.') == std::wstring::npos) {
            sawExtensionless = true;
            continue;
        }
        if (IsScriptPatchCandidate(name)) return false;
    }
    return sawExtensionless;
}

bool PatchManifestContains(const std::wstring &inner);

bool ShouldDumpScript(const std::wstring &inner) {
    std::wstring lowered = inner;
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), towlower);
    if (ShouldSkipPatch(inner)) return false;
    if (DumpTargetListContains(inner)) return true;
    if (EndsWithNoCase(lowered, L".txt.scn") || EndsWithNoCase(lowered, L".scn")) return true;
    if (IsScriptPatchCandidate(inner) && DumpTargetListContains(inner)) return true;
    return false;
}

bool ShouldAttemptPatch(const std::wstring &inner) {
    if (ShouldSkipPatch(inner)) return false;
    if (IsScriptPatchCandidate(inner)) return true;
    return PatchManifestContains(inner);
}

bool IsPatchStorageRequest(const std::wstring &name) {
    std::wstring normalized = NormalizeSlashes(name);
    std::wstring lowered = normalized;
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), towlower);
    if (lowered.find(L"_translation_meta/kirikiri_patch") != std::wstring::npos) return true;
    if (lowered.find(L"patch.xp3") != std::wstring::npos) return true;
    return false;
}

bool WriteWholeFile(const std::wstring &path, const std::vector<unsigned char> &data) {
    std::wstring dir = DirName(path);
    if (!EnsureDirectory(dir)) return false;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    bool ok = data.empty() ||
              WriteFile(h, data.data(), static_cast<DWORD>(data.size()), &written, nullptr);
    CloseHandle(h);
    return ok && written == data.size();
}

std::vector<std::wstring> LoadNameListFile(const std::wstring &path) {
    std::vector<std::wstring> entries;
    std::string data;
    if (!ReadWholeFile(path, &data)) return entries;
    if (data.size() >= 3 &&
        static_cast<unsigned char>(data[0]) == 0xEF &&
        static_cast<unsigned char>(data[1]) == 0xBB &&
        static_cast<unsigned char>(data[2]) == 0xBF) {
        data.erase(0, 3);
    }

    size_t start = 0;
    while (start <= data.size()) {
        size_t end = data.find_first_of("\r\n", start);
        if (end == std::string::npos) end = data.size();
        std::string raw = data.substr(start, end - start);
        while (!raw.empty() && (raw.back() == ' ' || raw.back() == '\t')) raw.pop_back();
        size_t first = 0;
        while (first < raw.size() && (raw[first] == ' ' || raw[first] == '\t')) ++first;
        if (first) raw.erase(0, first);
        if (!raw.empty() && raw[0] != '#') {
            std::wstring entry = NormalizeSlashes(Utf8ToWide(raw));
            if (!entry.empty()) entries.push_back(entry);
        }
        if (end == data.size()) break;
        start = end + 1;
        if (start < data.size() && data[end] == '\r' && data[start] == '\n') ++start;
    }
    std::vector<std::wstring> ordered;
    for (const auto &entry : entries) {
        bool exists = false;
        std::wstring key = NormalizePatchEntry(entry);
        for (const auto &seen : ordered) {
            if (NormalizePatchEntry(seen) == key) {
                exists = true;
                break;
            }
        }
        if (!exists) ordered.push_back(entry);
    }
    entries.swap(ordered);
    return entries;
}

struct DumpState {
    std::wstring inner;
    std::wstring outPath;
    std::vector<unsigned char> data;
    tjs_uint64 position = 0;
    tjs_uint64 highWater = 0;
    StreamSeek_t realSeek = nullptr;
    StreamRead_t realRead = nullptr;
    void **originalVtable = nullptr;
    void **hookVtable = nullptr;
    LONG hitIndex = 0;
};

std::unordered_map<tTJSBinaryStream *, DumpState *> gDumpStreams;
std::unordered_map<void **, void **> gDumpVtables;

void FlushDumpState(DumpState *state) {
    if (!state || state->outPath.empty() || state->data.empty()) return;
    if (state->highWater == 0 || state->highWater > state->data.size()) return;
    std::vector<unsigned char> out(state->data.begin(), state->data.begin() + static_cast<size_t>(state->highWater));
    if (WriteWholeFile(state->outPath, out) && state->hitIndex <= 80) {
        Log(L"[kirikiri native] dumped script stream: " + state->inner + L" -> " + state->outPath +
            L" (" + std::to_wstring(state->highWater) + L" bytes)");
    }
}

DumpState *FindDumpState(tTJSBinaryStream *stream) {
    std::lock_guard<std::mutex> lock(gDumpMutex);
    auto it = gDumpStreams.find(stream);
    if (it == gDumpStreams.end()) return nullptr;
    return it->second;
}

tjs_uint64 __cdecl DumpSeek(tTJSBinaryStream *stream, tjs_int64 offset, int whence) {
    DumpState *state = FindDumpState(stream);
    if (!state || !state->realSeek) return 0;
    tjs_uint64 result = state->realSeek(stream, offset, whence);
    state->position = result;
    return result;
}

tjs_uint __cdecl DumpRead(tTJSBinaryStream *stream, void *buffer, tjs_uint readSize) {
    DumpState *state = FindDumpState(stream);
    if (!state || !state->realRead) return 0;
    tjs_uint got = state->realRead(stream, buffer, readSize);
    if (got && buffer) {
        tjs_uint64 end = state->position + got;
        if (end <= 32ull * 1024ull * 1024ull) {
            if (end > state->data.size()) state->data.resize(static_cast<size_t>(end));
            memcpy(state->data.data() + state->position, buffer, got);
            state->position = end;
            if (end > state->highWater) state->highWater = end;
        }
        if (state->highWater && (state->highWater % (256 * 1024)) < got) {
            FlushDumpState(state);
        }
    }
    return got;
}

bool TryHookDumpStream(tTJSBinaryStream *stream, const std::wstring &inner) {
    if (!stream || !ShouldDumpScript(inner)) return false;
    LONG dumpIndex = InterlockedIncrement(&gDumpHits);
    if (dumpIndex > MAX_DUMP_FILES) return false;

    void **originalVtable = *reinterpret_cast<void ***>(stream);
    if (!originalVtable || !originalVtable[0] || !originalVtable[1]) return false;

    std::wstring rel = SanitizeRelativePath(inner);
    if (rel.empty()) return false;
    auto *state = new DumpState();
    state->inner = inner;
    state->outPath = JoinPath(JoinPath(JoinPath(gGameDir, L"_translation_meta"), L"kirikiri_dump"), rel);
    state->realSeek = reinterpret_cast<StreamSeek_t>(originalVtable[0]);
    state->realRead = reinterpret_cast<StreamRead_t>(originalVtable[1]);
    state->originalVtable = originalVtable;
    state->hitIndex = dumpIndex;

    {
        std::lock_guard<std::mutex> lock(gDumpMutex);
        void **hookVtable = nullptr;
        auto existing = gDumpVtables.find(originalVtable);
        if (existing != gDumpVtables.end()) {
            hookVtable = existing->second;
        } else {
            hookVtable = static_cast<void **>(VirtualAlloc(nullptr, sizeof(void *) * 8, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
            if (!hookVtable) {
                delete state;
                return false;
            }
            memcpy(hookVtable, originalVtable, sizeof(void *) * 8);
            hookVtable[0] = reinterpret_cast<void *>(&DumpSeek);
            hookVtable[1] = reinterpret_cast<void *>(&DumpRead);
            gDumpVtables[originalVtable] = hookVtable;
        }
        state->hookVtable = hookVtable;
        gDumpStreams[stream] = state;
        *reinterpret_cast<void ***>(stream) = hookVtable;
    }
    if (dumpIndex <= 80) {
        Log(L"[kirikiri native] dump hook stream: " + inner);
    }
    return true;
}

void FlushAllDumpStreams() {
    std::vector<DumpState *> states;
    {
        std::lock_guard<std::mutex> lock(gDumpMutex);
        for (const auto &entry : gDumpStreams) {
            if (entry.second) states.push_back(entry.second);
        }
    }
    for (DumpState *state : states) FlushDumpState(state);
}

bool LoadPatchManifest(const std::wstring &path) {
    std::string data;
    if (!ReadWholeFile(path, &data)) return false;
    if (data.size() >= 3 &&
        static_cast<unsigned char>(data[0]) == 0xEF &&
        static_cast<unsigned char>(data[1]) == 0xBB &&
        static_cast<unsigned char>(data[2]) == 0xBF) {
        data.erase(0, 3);
    }

    size_t start = 0;
    while (start <= data.size()) {
        size_t end = data.find_first_of("\r\n", start);
        if (end == std::string::npos) end = data.size();
        std::string raw = data.substr(start, end - start);
        while (!raw.empty() && (raw.back() == ' ' || raw.back() == '\t')) raw.pop_back();
        size_t first = 0;
        while (first < raw.size() && (raw[first] == ' ' || raw[first] == '\t')) ++first;
        if (first) raw.erase(0, first);
        if (!raw.empty() && raw[0] != '#') {
            std::wstring entry = NormalizePatchEntry(Utf8ToWide(raw));
            if (!entry.empty()) gPatchEntries.push_back(entry);
        }
        if (end == data.size()) break;
        start = end + 1;
        if (start < data.size() && data[end] == '\r' && data[start] == '\n') ++start;
    }
    std::sort(gPatchEntries.begin(), gPatchEntries.end());
    gPatchEntries.erase(std::unique(gPatchEntries.begin(), gPatchEntries.end()), gPatchEntries.end());
    gPatchBasenameEntries.clear();
    for (const auto &entry : gPatchEntries) {
        std::wstring basename = PatchBasenameKey(entry);
        if (basename.empty() || basename == entry) continue;
        auto existing = gPatchBasenameEntries.find(basename);
        if (existing == gPatchBasenameEntries.end()) {
            gPatchBasenameEntries[basename] = entry;
        } else if (existing->second != entry) {
            // Ambiguous bare script names are intentionally not remapped.
            existing->second.clear();
        }
    }
    for (auto it = gPatchBasenameEntries.begin(); it != gPatchBasenameEntries.end();) {
        if (it->second.empty()) {
            it = gPatchBasenameEntries.erase(it);
        } else {
            ++it;
        }
    }
    return !gPatchEntries.empty();
}

bool PatchManifestContains(const std::wstring &inner) {
    if (gPatchEntries.empty()) return false;
    std::wstring key = NormalizePatchEntry(inner);
    return std::binary_search(gPatchEntries.begin(), gPatchEntries.end(), key);
}

std::vector<std::wstring> StorageNamePatchKeys(const std::wstring &inner) {
    std::vector<std::wstring> keys;
    std::wstring stripped = StripStorageArchivePrefix(inner);
    std::wstring normalized = NormalizeSlashes(stripped);
    if (!normalized.empty()) keys.push_back(normalized);
    std::wstring raw = NormalizeSlashes(inner);
    if (!raw.empty() && NormalizePatchEntry(raw) != NormalizePatchEntry(normalized)) keys.push_back(raw);
    // Flat patch archives use the final component after either '/' or '>'.
    // Keep this key for fully-qualified file:// XP3 member requests too.
    size_t slash = normalized.find_last_of(L"/\\>");
    std::wstring basename = slash == std::wstring::npos ? normalized : normalized.substr(slash + 1);
    if (!basename.empty()) keys.push_back(basename);
    std::vector<std::wstring> ordered;
    for (const auto &key : keys) {
        std::wstring cmp = NormalizePatchEntry(key);
        bool exists = false;
        for (const auto &seen : ordered) {
            if (NormalizePatchEntry(seen) == cmp) {
                exists = true;
                break;
            }
        }
        if (!exists) ordered.push_back(key);
    }
    return ordered;
}

std::wstring JoinPatchDiagnosticValues(const std::vector<std::wstring> &values) {
    if (values.empty()) return L"<none>";
    std::wstring joined;
    for (const auto &value : values) {
        if (!joined.empty()) joined += L"|";
        joined += value;
    }
    return joined;
}

bool AnyPatchManifestMatch(const std::vector<std::wstring> &keys) {
    for (const auto &key : keys) {
        if (PatchManifestContains(key)) return true;
    }
    return false;
}

std::vector<std::wstring> ResolvePatchEntryCandidates(const std::wstring &inner) {
    std::vector<std::wstring> candidates;
    for (const auto &storageKey : StorageNamePatchKeys(inner)) {
        std::wstring key = NormalizePatchEntry(storageKey);
        if (std::binary_search(gPatchEntries.begin(), gPatchEntries.end(), key)) {
            candidates.push_back(key);
        }
        std::wstring basename = PatchBasenameKey(storageKey);
        if (basename == key && !basename.empty()) {
            auto mapped = gPatchBasenameEntries.find(basename);
            if (mapped != gPatchBasenameEntries.end() && !mapped->second.empty()) {
                candidates.push_back(mapped->second);
            }
        }
    }
    std::sort(candidates.begin(), candidates.end());
    candidates.erase(std::unique(candidates.begin(), candidates.end()), candidates.end());
    return candidates;
}

bool DirectoryPatchContains(const std::wstring &archive, const std::wstring &inner) {
    std::wstring local = inner;
    std::replace(local.begin(), local.end(), L'/', L'\\');
    return FileExists(JoinPath(JoinPath(gGameDir, archive), local));
}

std::wstring FilePathToStorageUrl(std::wstring path) {
    std::replace(path.begin(), path.end(), L'\\', L'/');
    if (path.size() >= 2 && path[1] == L':') {
        path.erase(1, 1);
    }
    return L"file://./" + path;
}

std::wstring LocalPatchPath(const std::wstring &archive, const std::wstring &inner) {
    std::wstring local = inner;
    std::replace(local.begin(), local.end(), L'/', L'\\');
    return JoinPath(JoinPath(gGameDir, archive), local);
}

tTJSBinaryStream *OpenLoosePatchMemoryStream(const std::wstring &inner, std::wstring *openedPath) {
    for (const auto &archive : gPatchArchives) {
        if (EndsWithNoCase(archive, L".xp3")) continue;
        for (const auto &entry : ResolvePatchEntryCandidates(inner)) {
            std::wstring path = LocalPatchPath(archive, entry);
            if (!FileExists(path)) continue;
            if (void *rawStream = kirikiri_patch_stream::CreateReadOnlyFileStream(path)) {
                if (openedPath) *openedPath = path;
                return reinterpret_cast<tTJSBinaryStream *>(rawStream);
            }
        }
    }
    return nullptr;
}

std::vector<std::wstring> BuildPatchUrls(const std::wstring &inner) {
    std::vector<std::wstring> urls;
    std::vector<std::wstring> entryCandidates = ResolvePatchEntryCandidates(inner);
    if (entryCandidates.empty()) {
        entryCandidates.push_back(NormalizeSlashes(inner));
    }
    for (const auto &archive : gPatchArchives) {
        if (EndsWithNoCase(archive, L".xp3")) {
            std::wstring xp3Url = FilePathToStorageUrl(JoinPath(gGameDir, archive));
            for (const auto &entry : entryCandidates) {
                if (!PatchManifestContains(entry)) continue;
                urls.push_back(xp3Url + L">" + entry);
                size_t slash = entry.find_last_of(L"/\\");
                if (slash != std::wstring::npos && slash + 1 < entry.size()) {
                    urls.push_back(xp3Url + L">" + entry.substr(slash + 1));
                }
            }
        } else {
            for (const auto &entry : entryCandidates) {
                if (DirectoryPatchContains(archive, entry)) {
                    urls.push_back(FilePathToStorageUrl(LocalPatchPath(archive, entry)));
                }
            }
        }
    }
    return urls;
}

void InstallPatchAutoPaths() {
    if (!TVPAddAutoPath || gPatchArchives.empty()) return;
    if (InterlockedCompareExchange(&gPatchAutoPathInstalled, 1, 0) != 0) return;
    for (const auto &archive : gPatchArchives) {
        std::wstring full = JoinPath(gGameDir, archive);
        std::wstring url = FilePathToStorageUrl(full);
        if (EndsWithNoCase(archive, L".xp3")) url += L">";
        else if (!url.empty() && url.back() != L'/') url += L"/";
        TjsStringHolder holder(url);
        TVPAddAutoPath(holder.get());
        Log(L"[kirikiri native] added patch auto path: " + url);
    }
    if (TVPClearStorageCaches) {
        TVPClearStorageCaches();
        Log(L"[kirikiri native] cleared storage caches after patch auto path");
    }
}

void *OpenPatchIStream(const std::wstring &requested, tjs_uint32 flags, LONG sample) {
    if (!OriginalTVPCreateIStream) return nullptr;
    std::wstring inner;
    if (!IsAcceptedStorageName(requested, &inner)) return nullptr;
    inner = NormalizeSlashes(inner);
    if (gPatchArchives.empty()) return nullptr;
    bool shouldAttempt = ShouldAttemptPatch(inner);
    if (!shouldAttempt) {
        for (const auto &key : StorageNamePatchKeys(inner)) {
            if (ShouldAttemptPatch(key)) {
                shouldAttempt = true;
                break;
            }
        }
    }
    if (!shouldAttempt) return nullptr;

    for (const auto &candidate : BuildPatchUrls(inner)) {
        if (sample <= 200) {
            Log(L"[kirikiri native] istream patch try: " + requested + L" -> " + candidate);
        }
        TjsStringHolder patched(candidate);
        if (TVPIsExistentStorageNoSearchNoNormalize && !TVPIsExistentStorageNoSearchNoNormalize(patched.get())) {
            if (sample <= 200) Log(L"[kirikiri native] istream patch missing: " + candidate);
            continue;
        }
        void *stream = CallOriginalTVPCreateIStream(patched.get(), flags);
        if (stream) {
            InterlockedIncrement(&gOpenHits);
            if (sample <= 200) Log(L"[kirikiri native] istream patch hit: " + candidate);
            return stream;
        }
        if (sample <= 200) Log(L"[kirikiri native] istream patch open failed: " + candidate);
    }
    if (sample <= 200) Log(L"[kirikiri native] istream patch miss: " + inner);
    return nullptr;
}

bool ApplyIStreamHookLocked() {
    if (!gIStreamTarget || gIStreamHooked || !gIStreamPatchSize) return true;
    std::vector<BYTE> patch(gIStreamPatchSize, 0x90);
    patch[0] = 0xE9;
    *reinterpret_cast<int32_t *>(patch.data() + 1) =
        reinterpret_cast<BYTE *>(HookTVPCreateIStream) - (reinterpret_cast<BYTE *>(gIStreamTarget) + 5);
    if (!WriteProcessCode(gIStreamTarget, patch.data(), patch.size())) return false;
    gIStreamHooked = true;
    return true;
}

bool RestoreIStreamHookLocked() {
    if (!gIStreamTarget || !gIStreamPatchSize) return false;
    if (!gIStreamHooked) return true;
    if (!WriteProcessCode(gIStreamTarget, gIStreamOriginal, gIStreamPatchSize)) return false;
    gIStreamHooked = false;
    return true;
}

void *CallOriginalTVPCreateIStream(const tTJSString &name, tjs_uint32 flags) {
    TVPCreateIStream_t original = OriginalTVPCreateIStream ? OriginalTVPCreateIStream : TVPCreateIStream;
    if (!original) return nullptr;
    std::lock_guard<std::mutex> lock(gHookMutex);
    bool wasHooked = gIStreamHooked;
    if (wasHooked && !RestoreIStreamHookLocked()) return nullptr;
    void *result = original(name, flags);
    if (wasHooked && !ApplyIStreamHookLocked()) {
        Log(L"[kirikiri native] TVPCreateIStream rehook failed");
    }
    return result;
}

bool InstallIStreamHookAt(void *target) {
    if (!target) return false;
    std::lock_guard<std::mutex> lock(gHookMutex);
    if (gIStreamHooked || gIStreamTarget == target) return true;
    constexpr size_t patchSize = 5;
    memcpy(gIStreamOriginal, target, patchSize);
    gIStreamTarget = target;
    gIStreamPatchSize = patchSize;
    if (!ApplyIStreamHookLocked()) {
        Log(L"[kirikiri native] TVPCreateIStream inline hook failed");
        return false;
    }
    Log(L"[kirikiri native] TVPCreateIStream hook installed");
    return true;
}

void *__stdcall HookTVPCreateIStream(const tTJSString &name, tjs_uint32 flags) {
    TVPCreateIStream_t original = OriginalTVPCreateIStream ? OriginalTVPCreateIStream : TVPCreateIStream;
    if (!original) return nullptr;
    LONG sample = InterlockedIncrement(&gIStreamSamples);
    if ((flags & 0x0f) == TJS_BS_READ) {
        LONG depth = InterlockedIncrement(&gStorageRedirectDepth);
        if (depth == 1) {
            std::wstring requested;
            if (TryReadTjsString(name, &requested)) {
                if (sample <= 200) Log(L"[kirikiri native] istream open: " + requested);
                if (!IsPatchStorageRequest(requested)) {
                    void *patched = OpenPatchIStream(requested, flags, sample);
                    InterlockedDecrement(&gStorageRedirectDepth);
                    if (patched) return patched;
                } else {
                    InterlockedDecrement(&gStorageRedirectDepth);
                }
            } else {
                InterlockedDecrement(&gStorageRedirectDepth);
            }
        } else {
            InterlockedDecrement(&gStorageRedirectDepth);
        }
    }
    return CallOriginalTVPCreateIStream(name, flags);
}

StorageMediaOpen_t FindStorageMediaOpen(iTVPStorageMedia *media) {
    std::lock_guard<std::mutex> lock(gStorageMutex);
    auto it = gStorageMediaOpenOriginal.find(media);
    if (it != gStorageMediaOpenOriginal.end()) return it->second;
    return nullptr;
}

bool HookStorageMediaVtable(iTVPStorageMedia *media) {
    if (!media) return false;
    void **vtable = *reinterpret_cast<void ***>(media);
    if (!vtable || !vtable[6]) return false;

    std::lock_guard<std::mutex> lock(gStorageMutex);
    auto original = reinterpret_cast<StorageMediaOpen_t>(vtable[6]);
    gStorageMediaOpenOriginal[media] = original;

    auto byVtable = gStorageVtableOpenOriginal.find(vtable);
    if (byVtable == gStorageVtableOpenOriginal.end()) {
        DWORD oldProtect = 0;
        if (!VirtualProtect(&vtable[6], sizeof(void *), PAGE_EXECUTE_READWRITE, &oldProtect)) return false;
        gStorageVtableOpenOriginal[vtable] = original;
        vtable[6] = reinterpret_cast<void *>(HookStorageMediaOpen);
        VirtualProtect(&vtable[6], sizeof(void *), oldProtect, &oldProtect);
        FlushInstructionCache(GetCurrentProcess(), &vtable[6], sizeof(void *));
    }
    return true;
}

void UnhookStorageMediaVtable(iTVPStorageMedia *media) {
    if (!media) return;
    void **vtable = *reinterpret_cast<void ***>(media);
    if (!vtable) return;

    std::lock_guard<std::mutex> lock(gStorageMutex);
    gStorageMediaOpenOriginal.erase(media);
    bool stillUsed = false;
    for (const auto &entry : gStorageMediaOpenOriginal) {
        void **other = entry.first ? *reinterpret_cast<void ***>(entry.first) : nullptr;
        if (other == vtable) {
            stillUsed = true;
            break;
        }
    }
    if (stillUsed) return;

    auto original = gStorageVtableOpenOriginal.find(vtable);
    if (original != gStorageVtableOpenOriginal.end()) {
        DWORD oldProtect = 0;
        if (VirtualProtect(&vtable[6], sizeof(void *), PAGE_EXECUTE_READWRITE, &oldProtect)) {
            vtable[6] = reinterpret_cast<void *>(original->second);
            VirtualProtect(&vtable[6], sizeof(void *), oldProtect, &oldProtect);
            FlushInstructionCache(GetCurrentProcess(), &vtable[6], sizeof(void *));
        }
        gStorageVtableOpenOriginal.erase(original);
    }
}

tTJSBinaryStream *__cdecl OpenPatchStorageStream(const std::wstring &requested, tjs_uint32 flags, LONG sample) {
    TVPCreateIStream_t createIStream = OriginalTVPCreateIStream ? OriginalTVPCreateIStream : TVPCreateIStream;
    if (!createIStream || !TVPCreateBinaryStreamAdapter) return nullptr;
    std::wstring inner;
    if (!IsAcceptedStorageName(requested, &inner)) return nullptr;
    inner = NormalizeSlashes(inner);
    if (gPatchArchives.empty()) return nullptr;
    bool shouldAttempt = ShouldAttemptPatch(inner);
    if (!shouldAttempt) {
        for (const auto &key : StorageNamePatchKeys(inner)) {
            if (ShouldAttemptPatch(key)) {
                shouldAttempt = true;
                break;
            }
        }
    }
    if (!shouldAttempt) return nullptr;

    for (const auto &candidate : BuildPatchUrls(inner)) {
        if (sample <= 160) {
            Log(L"[kirikiri native] storage patch try: " + requested + L" -> " + candidate);
        }
        TjsStringHolder patched(candidate);
        if (TVPIsExistentStorageNoSearchNoNormalize && !TVPIsExistentStorageNoSearchNoNormalize(patched.get())) {
            if (sample <= 160) {
                Log(L"[kirikiri native] storage patch missing: " + candidate);
            }
            continue;
        }
        void *istream = createIStream(patched.get(), flags);
        if (!istream) {
            if (sample <= 160) {
                Log(L"[kirikiri native] storage patch istream failed: " + candidate);
            }
            continue;
        }
        tTJSBinaryStream *stream = TVPCreateBinaryStreamAdapter(istream);
        if (stream) {
            InterlockedIncrement(&gOpenHits);
            if (sample <= 160) {
                Log(L"[kirikiri native] storage patch hit: " + candidate);
            }
            return stream;
        }
        if (sample <= 160) {
            Log(L"[kirikiri native] storage patch adapter failed: " + candidate);
        }
    }

    InterlockedIncrement(&gOpenMisses);
    if (sample <= 160) {
        Log(L"[kirikiri native] storage patch miss: " + inner);
    }
    return nullptr;
}

tTJSBinaryStream *__cdecl HookStorageMediaOpen(iTVPStorageMedia *media, const tTJSString &name, tjs_uint32 flags) {
    StorageMediaOpen_t original = FindStorageMediaOpen(media);
    if (!original) return nullptr;
    LONG sample = InterlockedIncrement(&gStorageOpenSamples);

    if ((flags & 0x0f) == TJS_BS_READ) {
        LONG depth = InterlockedIncrement(&gStorageRedirectDepth);
        if (depth == 1) {
            std::wstring requested;
            if (TryReadTjsString(name, &requested)) {
                if (sample <= 160) {
                    Log(L"[kirikiri native] storage open: " + requested);
                }
                tTJSBinaryStream *patched = IsPatchStorageRequest(requested)
                                                ? nullptr
                                                : OpenPatchStorageStream(requested, flags, sample);
                InterlockedDecrement(&gStorageRedirectDepth);
                if (patched) return patched;
            } else {
                InterlockedDecrement(&gStorageRedirectDepth);
            }
        } else {
            InterlockedDecrement(&gStorageRedirectDepth);
        }
    }

    tTJSBinaryStream *stream = original(media, name, flags);
    std::wstring inner;
    std::wstring requested;
    if (stream && TryReadTjsString(name, &requested) && IsAcceptedStorageName(requested, &inner) && ShouldDumpScript(inner)) {
        TryHookDumpStream(stream, NormalizeSlashes(inner));
    }
    return stream;
}

tTJSBinaryStream *__fastcall HookTVPCreateStream(const tTJSString &name, tjs_uint flags);

bool WriteProcessCode(void *target, const void *data, size_t size) {
    DWORD oldProtect = 0;
    if (!VirtualProtect(target, size, PAGE_EXECUTE_READWRITE, &oldProtect)) return false;
    memcpy(target, data, size);
    VirtualProtect(target, size, oldProtect, &oldProtect);
    FlushInstructionCache(GetCurrentProcess(), target, size);
    return true;
}

bool ApplyCreateStreamHookLocked() {
    if (!gCreateStreamTarget || gCreateStreamHooked || !gCreateStreamPatchSize) return true;
    if (gCreateStreamPatchSize < 5 || gCreateStreamPatchSize > sizeof(gCreateStreamOriginal)) return false;

    std::vector<BYTE> patch(gCreateStreamPatchSize, 0x90);
    patch[0] = 0xE9;
    *reinterpret_cast<int32_t *>(patch.data() + 1) =
        reinterpret_cast<BYTE *>(HookTVPCreateStream) - (reinterpret_cast<BYTE *>(gCreateStreamTarget) + 5);
    if (!WriteProcessCode(gCreateStreamTarget, patch.data(), patch.size())) return false;
    gCreateStreamHooked = true;
    return true;
}

tTJSBinaryStream *CallOriginalTVPCreateStream(const tTJSString &name, tjs_uint flags) {
    if (!gCreateStreamTrampoline) return nullptr;
    auto original = reinterpret_cast<TVPCreateStream_t>(gCreateStreamTrampoline);
    return original(name, flags);
}

bool DumpWholeStream(tTJSBinaryStream *stream, const std::wstring &inner) {
    if (!stream || !ShouldDumpScript(inner)) return false;
    void **vtable = *reinterpret_cast<void ***>(stream);
    if (!vtable || !vtable[0] || !vtable[1]) return false;
    auto seek = reinterpret_cast<StreamSeek_t>(vtable[0]);
    auto read = reinterpret_cast<StreamRead_t>(vtable[1]);
    seek(stream, 0, TJS_BS_SEEK_SET);

    std::vector<unsigned char> data;
    std::vector<unsigned char> buffer(64 * 1024);
    while (data.size() < MAX_DUMP_STREAM_BYTES) {
        tjs_uint want = static_cast<tjs_uint>(std::min<size_t>(buffer.size(), static_cast<size_t>(MAX_DUMP_STREAM_BYTES - data.size())));
        if (!want) break;
        tjs_uint got = read(stream, buffer.data(), want);
        if (!got) break;
        data.insert(data.end(), buffer.begin(), buffer.begin() + got);
        if (got < want) break;
    }
    if (data.empty() || data.size() >= MAX_DUMP_STREAM_BYTES) return false;

    std::wstring rel = SanitizeRelativePath(TargetInnerName(inner));
    if (rel.empty()) return false;
    std::wstring outPath = JoinPath(JoinPath(JoinPath(gGameDir, L"_translation_meta"), L"kirikiri_dump"), rel);
    if (WriteWholeFile(outPath, data)) {
        Log(L"[kirikiri native] target dumped: " + inner + L" (" + std::to_wstring(data.size()) + L" bytes)");
        return true;
    }
    return false;
}

bool DumpTargetByName(const std::wstring &target) {
    std::vector<std::wstring> names;
    std::wstring normalized = NormalizeSlashes(target);
    std::wstring archive = TargetArchiveName(normalized);
    std::wstring inner = TargetInnerName(normalized);
    if (!archive.empty() && !inner.empty()) {
        names.push_back(FilePathToStorageUrl(JoinPath(gGameDir, archive)) + L">" + inner);
        names.push_back(archive + L">" + inner);
    }
    names.push_back(inner);
    if (inner != normalized) names.push_back(normalized);
    size_t slash = inner.find_last_of(L"/\\");
    if (slash != std::wstring::npos && slash + 1 < inner.size()) {
        names.push_back(inner.substr(slash + 1));
    }

    for (const auto &name : names) {
        TjsStringHolder holder(name);
        tTJSBinaryStream *stream = CallOriginalTVPCreateStream(holder.get(), TJS_BS_READ);
        if (!stream) continue;
        bool ok = DumpWholeStream(stream, inner);
        // KiriKiri streams are owned by the engine and normally destroyed with
        // delete. Calling that across compiler ABIs is riskier than leaking the
        // short-lived bootstrap stream, so the target dumper intentionally keeps
        // the handle alive until process exit.
        return ok;
    }
    return false;
}

void DumpTargetsInline() {
    if (!gCreateStreamHooked || gDumpTargets.empty()) return;
    Log(L"[kirikiri native] target dump start: " + std::to_wstring(gDumpTargets.size()));
    int okCount = 0;
    int failCount = 0;
    int skippedCount = 0;
    for (const auto &target : gDumpTargets) {
        std::wstring inner = TargetInnerName(target);
        if (!ShouldDumpScript(inner)) continue;
        if (PreferScnTargetOverKs(inner)) {
            ++skippedCount;
            continue;
        }
        if (!IsScriptPatchCandidate(inner) && !gActiveExtensionlessDump) {
            // Extensionless protected resources are dumped when the game opens
            // them. Probing them by bare name can block PackinOne/Yuzu engines
            // during normal user launches, so only the pipeline extraction
            // probe enables active enumeration explicitly.
            ++skippedCount;
            continue;
        }
        std::wstring rel = SanitizeRelativePath(inner);
        if (!rel.empty() && FileExists(JoinPath(JoinPath(JoinPath(gGameDir, L"_translation_meta"), L"kirikiri_dump"), rel))) {
            ++okCount;
            continue;
        }
        Log(L"[kirikiri native] target dump probing: " + target);
        if (DumpTargetByName(target)) {
            ++okCount;
        } else {
            ++failCount;
            if (failCount <= 80) Log(L"[kirikiri native] target dump failed: " + target);
        }
        Sleep(1);
    }
    Log(L"[kirikiri native] target dump finished: ok=" + std::to_wstring(okCount) +
        L", failed=" + std::to_wstring(failCount) +
        L", skipped=" + std::to_wstring(skippedCount));
}

DWORD WINAPI DumpTargetsThread(void *) {
    Sleep(300);
    DumpTargetsInline();
    return 0;
}

void StartDumpTargetsThreadOnce() {
    if (gDumpTargets.empty()) return;
    if (gPassiveDumpOnly) return;
    if (InterlockedCompareExchange(&gDumpTargetThreadStarted, 1, 0) != 0) return;
    HANDLE thread = CreateThread(nullptr, 0, DumpTargetsThread, nullptr, 0, nullptr);
    if (thread) {
        CloseHandle(thread);
        Log(L"[kirikiri native] target dump thread queued");
    } else {
        Log(L"[kirikiri native] target dump thread failed, running inline");
        DumpTargetsInline();
    }
}

tTJSBinaryStream *__fastcall HookTVPCreateStream(const tTJSString &name, tjs_uint flags) {
    if ((flags & 0x0f) == TJS_BS_READ && gCreateStreamTarget) {
        LONG readIndex = InterlockedIncrement(&gCreateStreamReads);
        std::wstring requested;
        if (!TryReadTjsString(name, &requested)) {
            return CallOriginalTVPCreateStream(name, flags);
        }
        if (gPatchArchiveHooks && EndsWithNoCase(requested, L".sig")) {
            static constexpr char kSkipSignature[] = "skip!";
            if (void *rawSkipStream = kirikiri_patch_stream::CreateReadOnlyMemoryStream(
                    kSkipSignature, sizeof(kSkipSignature) - 1)) {
                auto *skipStream = reinterpret_cast<tTJSBinaryStream *>(rawSkipStream);
                LONG hit = InterlockedIncrement(&gSignatureBypassHits);
                if (hit <= 40) Log(L"[kirikiri native] signature bypass: " + requested);
                return skipStream;
            }
        }
        std::wstring inner;
        bool accepted = IsAcceptedStorageName(requested, &inner);
        if (readIndex <= 120) {
            std::vector<std::wstring> keys = accepted ? StorageNamePatchKeys(inner) : std::vector<std::wstring>();
            std::vector<std::wstring> candidates = accepted ? ResolvePatchEntryCandidates(inner) : std::vector<std::wstring>();
            Log(L"[kirikiri native] stream normalize #" + std::to_wstring(readIndex) +
                L" raw=" + requested +
                L" accepted=" + (accepted ? L"1" : L"0") +
                L" inner=" + (accepted ? inner : L"<none>") +
                L" keys=" + JoinPatchDiagnosticValues(keys) +
                L" candidates=" + JoinPatchDiagnosticValues(candidates) +
                L" manifest=" + (AnyPatchManifestMatch(keys) ? L"1" : L"0"));
        }
        if (accepted) {
            inner = NormalizeSlashes(inner);
            if (readIndex <= INITIAL_READ_BYPASS && !ShouldAttemptPatch(inner)) {
                return CallOriginalTVPCreateStream(name, flags);
            }
            if (!gDumpTargets.empty() && ShouldDumpScript(inner)) StartDumpTargetsThreadOnce();
            if (!gPatchArchives.empty() && ShouldAttemptPatch(inner)) {
                std::wstring memoryPath;
                if (tTJSBinaryStream *memoryStream = OpenLoosePatchMemoryStream(inner, &memoryPath)) {
                    InterlockedIncrement(&gOpenHits);
                    LONG sample = InterlockedIncrement(&gReadSamples);
                    if (sample <= 80) {
                        Log(L"[kirikiri native] memory patch hit: " + requested + L" -> " + memoryPath);
                    }
                    return memoryStream;
                }
                for (const auto &candidate : BuildPatchUrls(inner)) {
                    LONG sample = InterlockedIncrement(&gReadSamples);
                    if (sample <= 80) {
                        Log(L"[kirikiri native] patch try: " + requested + L" -> " + candidate);
                    }
                    TjsStringHolder patched(candidate);
                    tTJSBinaryStream *stream = CallOriginalTVPCreateStream(patched.get(), flags);
                    if (stream) {
                        InterlockedIncrement(&gOpenHits);
                        if (sample <= 80) {
                            Log(L"[kirikiri native] patch hit: " + candidate);
                        }
                        return stream;
                    }
                    LONG failSample = InterlockedIncrement(&gCandidateOpenFailSamples);
                    if (failSample <= 80) {
                        Log(L"[kirikiri native] patch open failed: " + candidate);
                    }
                }
                InterlockedIncrement(&gOpenMisses);
                LONG missSample = InterlockedIncrement(&gCandidateMissSamples);
                if (missSample <= 80) {
                    Log(L"[kirikiri native] patch miss: " + inner);
                }
            }
            tTJSBinaryStream *stream = CallOriginalTVPCreateStream(name, flags);
            if (stream && ShouldDumpScript(inner)) {
                if (!TryHookDumpStream(stream, inner)) {
                    LONG failures = InterlockedIncrement(&gDumpFailures);
                    if (failures <= 80) {
                        Log(L"[kirikiri native] dump failed: " + requested + L" inner=" + inner);
                    }
                }
            }
            return stream;
        }
    }
    return CallOriginalTVPCreateStream(name, flags);
}

bool InstallInlineJump(void *target, size_t patchSize) {
    if (!target) return false;
    if (patchSize < 5 || patchSize > sizeof(gCreateStreamOriginal)) return false;
    memcpy(gCreateStreamOriginal, target, patchSize);
    gCreateStreamTarget = target;
    gCreateStreamPatchSize = patchSize;
    BYTE *gate = static_cast<BYTE *>(VirtualAlloc(nullptr, patchSize + 16, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!gate) return false;
    memcpy(gate, gCreateStreamOriginal, patchSize);

    // This wrapper prologue contains a relative call. Relocate it so the copied
    // gate can call the same callee instead of jumping into nowhere.
    for (size_t i = 0; i + 5 <= patchSize; ++i) {
        if (gate[i] != 0xE8) continue;
        int32_t oldRel = *reinterpret_cast<int32_t *>(gCreateStreamOriginal + i + 1);
        BYTE *oldNext = static_cast<BYTE *>(target) + i + 5;
        BYTE *callee = oldNext + oldRel;
        BYTE *newNext = gate + i + 5;
        *reinterpret_cast<int32_t *>(gate + i + 1) = static_cast<int32_t>(callee - newNext);
    }

    gate[patchSize] = 0xE9;
    *reinterpret_cast<int32_t *>(gate + patchSize + 1) =
        static_cast<int32_t>((static_cast<BYTE *>(target) + patchSize) - (gate + patchSize + 5));
    FlushInstructionCache(GetCurrentProcess(), gate, patchSize + 5);
    gCreateStreamTrampoline = gate;
    return ApplyCreateStreamHookLocked();
}

bool SearchPattern(const BYTE *base, size_t size, const BYTE *pattern, const char *mask, void **out) {
    size_t len = strlen(mask);
    if (!base || size < len) return false;
    for (size_t i = 0; i + len <= size; ++i) {
        bool ok = true;
        for (size_t j = 0; j < len; ++j) {
            if (mask[j] == 'x' && base[i + j] != pattern[j]) {
                ok = false;
                break;
            }
        }
        if (ok) {
            *out = const_cast<BYTE *>(base + i);
            return true;
        }
    }
    return false;
}

bool SearchPatternRange(const BYTE *begin, const BYTE *end, const BYTE *pattern, const char *mask, void **out) {
    if (!begin || !end || begin >= end || !pattern || !mask || !out) return false;
    return SearchPattern(begin, static_cast<size_t>(end - begin), pattern, mask, out);
}

bool ModuleTextRange(HMODULE module, BYTE **base, size_t *size) {
    auto *mz = reinterpret_cast<IMAGE_DOS_HEADER *>(module);
    if (!mz || mz->e_magic != IMAGE_DOS_SIGNATURE) return false;
    auto *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(reinterpret_cast<BYTE *>(module) + mz->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return false;
    auto *section = IMAGE_FIRST_SECTION(nt);
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++section) {
        if (memcmp(section->Name, ".text", 5) == 0) {
            *base = reinterpret_cast<BYTE *>(module) + section->VirtualAddress;
            *size = section->Misc.VirtualSize;
            return true;
        }
    }
    *base = reinterpret_cast<BYTE *>(module);
    *size = nt->OptionalHeader.SizeOfImage;
    return true;
}

bool ModuleImageRange(HMODULE module, BYTE **base, size_t *size) {
    auto *mz = reinterpret_cast<IMAGE_DOS_HEADER *>(module);
    if (!mz || mz->e_magic != IMAGE_DOS_SIGNATURE) return false;
    auto *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(reinterpret_cast<BYTE *>(module) + mz->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return false;
    *base = reinterpret_cast<BYTE *>(module);
    *size = nt->OptionalHeader.SizeOfImage;
    return true;
}

void AppendByte(std::vector<BYTE> *code, BYTE value) {
    code->push_back(value);
}

void AppendBytes(std::vector<BYTE> *code, std::initializer_list<BYTE> values) {
    code->insert(code->end(), values.begin(), values.end());
}

void AppendU32(std::vector<BYTE> *code, uintptr_t value) {
    for (int i = 0; i < 4; ++i) {
        code->push_back(static_cast<BYTE>((value >> (i * 8)) & 0xFF));
    }
}

void WriteRel32(BYTE *base, size_t offset, void *target) {
    int32_t rel = static_cast<int32_t>(
        reinterpret_cast<BYTE *>(target) - (base + offset + 5));
    *reinterpret_cast<int32_t *>(base + offset + 1) = rel;
}

BYTE *AllocateExecutableStub(std::vector<BYTE> code) {
    BYTE *stub = static_cast<BYTE *>(VirtualAlloc(nullptr, code.size(), MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!stub) return nullptr;
    memcpy(stub, code.data(), code.size());
    return stub;
}

bool InstallRuntimeJump(void *target, size_t patchSize, void *stub, const wchar_t *label) {
    if (!target || !stub || patchSize < 5 || patchSize > 16) return false;
    std::vector<BYTE> patch(patchSize, 0x90);
    patch[0] = 0xE9;
    *reinterpret_cast<int32_t *>(patch.data() + 1) =
        static_cast<int32_t>(reinterpret_cast<BYTE *>(stub) - (reinterpret_cast<BYTE *>(target) + 5));
    if (!WriteProcessCode(target, patch.data(), patch.size())) {
        Log(std::wstring(L"[kirikiri native] internal hook patch failed: ") + label);
        return false;
    }
    InterlockedIncrement(&gInternalHookInstallCount);
    Log(std::wstring(L"[kirikiri native] internal hook installed: ") + label +
        L" @" + std::to_wstring(reinterpret_cast<uintptr_t>(target)));
    return true;
}

BYTE *BuildKagParserStub(void *target, bool exParser, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    // KAGParser:   cmp word ptr [eax+ecx*2], 0x5b.  EAX is the text buffer, ECX is the cursor.
    // KAGParserEx: cmp word ptr [ecx+eax*2], 0x5b.  ECX is the text buffer, EAX is the cursor.
    if (exParser) {
        AppendBytes(&code, {0x85, 0xC0});       // test eax,eax
    } else {
        AppendBytes(&code, {0x85, 0xC9});       // test ecx,ecx
    }
    size_t jneOffset = code.size();
    AppendBytes(&code, {0x75, 0x00});           // jne original-cmp
    if (exParser) {
        AppendBytes(&code, {0x50, 0x53, 0x52, 0x56, 0x57, 0x55}); // save eax,ebx,edx,esi,edi,ebp
    } else {
        AppendBytes(&code, {0x53, 0x51, 0x52, 0x56, 0x57, 0x55}); // save ebx,ecx,edx,esi,edi,ebp
    }
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 1200);
    AppendByte(&code, exParser ? 0x51 : 0x50);  // push ecx/eax text pointer
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    if (exParser) {
        AppendBytes(&code, {0x8B, 0xC8});       // mov ecx,eax
        AppendBytes(&code, {0x5D, 0x5F, 0x5E, 0x5A, 0x5B, 0x58});
    } else {
        AppendBytes(&code, {0x5D, 0x5F, 0x5E, 0x5A, 0x59, 0x5B});
    }
    size_t cmpOffset = code.size();
    if (exParser) {
        AppendBytes(&code, {0x66, 0x83, 0x3C, 0x41, 0x5B});
    } else {
        AppendBytes(&code, {0x66, 0x83, 0x3C, 0x48, 0x5B});
    }
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});
    code[jneOffset + 1] = static_cast<BYTE>(cmpOffset - (jneOffset + 2));

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveInternalText));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + 5);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildKiriKiriZ3Stub(void *target, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendBytes(&code, {0x50, 0x53, 0x51, 0x52, 0x56, 0x55}); // save eax,ebx,ecx,edx,esi,ebp
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 1200);
    AppendByte(&code, 0x57);                                  // push edi text pointer
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendBytes(&code, {0x8B, 0xF8});                          // mov edi,eax
    AppendBytes(&code, {0x5D, 0x5E, 0x5A, 0x59, 0x5B, 0x58});
    AppendBytes(&code, {0x66, 0x83, 0x3F, 0x00});              // cmp word ptr [edi],0
    size_t jneOffset = code.size();
    AppendBytes(&code, {0x0F, 0x85, 0, 0, 0, 0});              // jne original nonzero branch
    size_t jmpZeroOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});                    // jmp original zero branch

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveInternalText));
    int32_t jneRel = static_cast<int32_t>((reinterpret_cast<BYTE *>(target) + 12) - (stub + jneOffset + 6));
    *reinterpret_cast<int32_t *>(stub + jneOffset + 2) = jneRel;
    WriteRel32(stub, jmpZeroOffset, reinterpret_cast<BYTE *>(target) + 6);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildKagParserEntryStub(void *target) {
    std::vector<BYTE> code;
    // Function entry stack layout before the original prologue:
    // [esp+0] return address, [esp+4] lhs tTJSString, [esp+8] rhs const wchar_t*.
    AppendBytes(&code, {0x8B, 0x44, 0x24, 0x08});              // mov eax,[esp+8]
    AppendByte(&code, 0x9C);                                  // pushfd
    AppendByte(&code, 0x60);                                  // pushad
    AppendByte(&code, 0x68);
    AppendU32(&code, 1600);
    AppendByte(&code, 0x50);                                  // push eax
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x61);                                  // popad
    AppendByte(&code, 0x9D);                                  // popfd
    AppendBytes(&code, {0x55, 0x8B, 0xEC, 0x6A, 0xFF});        // original prologue bytes
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveKagParserArgument));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + 5);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *FindEnclosingX86Function(BYTE *address, BYTE *rangeBase, size_t rangeSize, size_t maxBack) {
    if (!address || !rangeBase || address < rangeBase) return nullptr;
    BYTE *rangeEnd = rangeBase + rangeSize;
    if (address >= rangeEnd) return nullptr;
    size_t available = static_cast<size_t>(address - rangeBase);
    size_t back = std::min(maxBack, available);
    for (size_t i = 0; i <= back; ++i) {
        BYTE *candidate = address - i;
        if (candidate + 5 > rangeEnd) continue;
        if (candidate[0] == 0x55 && candidate[1] == 0x8B && candidate[2] == 0xEC) {
            return candidate;
        }
    }
    return nullptr;
}

bool FindFirstPushAddress(BYTE *base, size_t size, uintptr_t value, void **out) {
    if (!base || size < 5 || !out) return false;
    for (size_t i = 0; i + 5 <= size; ++i) {
        if (base[i] != 0x68) continue;
        uintptr_t imm = *reinterpret_cast<uint32_t *>(base + i + 1);
        if (imm == value) {
            *out = base + i;
            return true;
        }
    }
    return false;
}

size_t GuessSafeEntryPatchSize(BYTE *entry) {
    if (!entry) return 0;
    __try {
        if (entry[0] == 0x55 && entry[1] == 0x8B && entry[2] == 0xEC) {
            if (entry[3] == 0x83 && entry[4] == 0xEC) return 6;       // sub esp, imm8
            if (entry[3] == 0x81 && entry[4] == 0xEC) return 9;       // sub esp, imm32
            return 5;                                                 // push ebp; mov ebp,esp; push/push...
        }
        if (entry[0] == 0x8B && entry[1] == 0xFF &&
            entry[2] == 0x55 && entry[3] == 0x8B && entry[4] == 0xEC) {
            return 5;                                                 // hotpatch mov edi,edi; push ebp; mov ebp,esp
        }
        if ((entry[0] == 0x53 || entry[0] == 0x56 || entry[0] == 0x57) &&
            (entry[1] == 0x53 || entry[1] == 0x56 || entry[1] == 0x57 || entry[1] == 0x8B)) {
            return 5;                                                 // common frameless prologue
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return 0;
    }
    return 0;
}

void AppendOriginalBytes(std::vector<BYTE> *code, void *target, size_t patchSize) {
    BYTE *p = static_cast<BYTE *>(target);
    for (size_t i = 0; i < patchSize; ++i) code->push_back(p[i]);
}

BYTE *BuildStackWideCaptureStub(void *target, size_t patchSize, BYTE stackOffset, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendBytes(&code, {0x8B, 0x44, 0x24, stackOffset});              // mov eax,[esp+stackOffset]
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 1600);
    AppendByte(&code, 0x50);                                          // push eax text pointer
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveInternalText));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildStackTjsStringCaptureStub(void *target, size_t patchSize, BYTE stackOffset, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendBytes(&code, {0x8B, 0x44, 0x24, stackOffset});              // mov eax,[esp+stackOffset]
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x50);                                          // push eax tTJSString*
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriCaptureInternalTjsString));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildRegisterWideCaptureStub(void *target, size_t patchSize, BYTE pushRegisterOpcode, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 1600);
    AppendByte(&code, pushRegisterOpcode);                            // push original register text pointer
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveInternalText));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildEcxIndirectWideCaptureStub(void *target, size_t patchSize, int displacement, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    if (displacement >= -128 && displacement <= 127) {
        AppendBytes(&code, {0x8B, 0x41, static_cast<BYTE>(displacement & 0xFF)}); // mov eax,[ecx+disp8]
    } else {
        AppendBytes(&code, {0x8B, 0x81});                              // mov eax,[ecx+disp32]
        AppendU32(&code, static_cast<uintptr_t>(displacement));
    }
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 1600);
    AppendByte(&code, 0x50);                                          // push eax text pointer
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveInternalText));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildRegisterNarrowCaptureStub(
    void *target,
    size_t patchSize,
    BYTE pushRegisterOpcode,
    BYTE returnAddressStackOffset,
    volatile LONG *hitCounter) {
    if (pushRegisterOpcode != 0x51) return nullptr;                   // EmbedKrkrZ text lives in ECX.
    (void)returnAddressStackOffset;
    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 4096);
    AppendByte(&code, pushRegisterOpcode);                            // push original register text pointer
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    // pushad saved ECX at [esp+24]. A miss returns the original pointer,
    // while a hit restores the stable translated pointer into ECX.
    AppendBytes(&code, {0x89, 0x44, 0x24, 0x18});                    // mov [esp+24],eax
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriCaptureInternalUtf8Text));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildUtf8ConverterEntryStub(void *target, size_t patchSize, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendByte(&code, 0x52);                                          // save edx
    AppendByte(&code, 0x51);                                          // save ecx
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x52);                                          // destination
    AppendByte(&code, 0x51);                                          // source
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x59);                                          // restore ecx
    AppendByte(&code, 0x5A);                                          // restore edx
    AppendBytes(&code, {0x3D, 0x00, 0x00, 0x00, 0x80});              // cmp eax,INT_MIN
    size_t jeOffset = code.size();
    AppendBytes(&code, {0x74, 0x00});                                 // je original path
    AppendByte(&code, 0xC3);                                          // translated conversion complete
    size_t originalOffset = code.size();
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});
    code[jeOffset + 1] = static_cast<BYTE>(originalOffset - (jeOffset + 2));

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriConvertEmbeddedUtf8));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildEmbedKrkr2WideSlotStub(void *target, size_t patchSize) {
    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendBytes(&code, {0x8B, 0x4C, 0x24, 0x18});                    // mov ecx,[esp+24] saved ecx
    AppendBytes(&code, {0x8B, 0x44, 0x24, 0x1C});                    // mov eax,[esp+28] saved eax
    AppendBytes(&code, {0x8D, 0x14, 0xC1});                          // lea edx,[ecx+eax*8] text slot
    AppendBytes(&code, {0x8B, 0x44, 0x24, 0x04});                    // mov eax,[esp+4] saved esi
    AppendByte(&code, 0x52);                                          // push edx slot
    AppendByte(&code, 0x50);                                          // push eax text
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendBytes(&code, {0x89, 0x44, 0x24, 0x04});                    // mov [esp+4],eax saved esi
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveEmbedKrkr2Text));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildUtf8CursorEntryStub(void *target, size_t patchSize, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendByte(&code, 0x52);                                          // save edx
    AppendByte(&code, 0x51);                                          // save ecx
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x51);                                          // const char **cursor
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x59);                                          // restore ecx
    AppendByte(&code, 0x5A);                                          // restore edx
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriReplaceEmbeddedUtf8Cursor));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildPsbPostConversionStub(void *target) {
    BYTE *original = static_cast<BYTE *>(target);
    const BYTE expected[] = {0x5F, 0x5B, 0x8B, 0xE5, 0x5D};
    if (!original || memcmp(original, expected, sizeof(expected)) != 0) return nullptr;

    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendByte(&code, 0x57);                                          // destination in EDI
    AppendByte(&code, 0x53);                                          // source in EBX
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, sizeof(expected));
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriPatchPsbWideResult));
    WriteRel32(stub, jmpOffset, original + sizeof(expected));
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildStackWideEmbedAfterNewStub(void *target, size_t patchSize, volatile LONG *hitCounter) {
    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                          // pushfd
    AppendByte(&code, 0x60);                                          // pushad
    AppendBytes(&code, {0x8B, 0x44, 0x24, 0x28});                    // mov eax,[esp+40] original arg1
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 4096);
    AppendByte(&code, 0x50);                                          // push original UTF-16 arg1
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendBytes(&code, {0x89, 0x44, 0x24, 0x28});                    // replace original arg1
    AppendByte(&code, 0x61);                                          // popad
    AppendByte(&code, 0x9D);                                          // popfd
    AppendOriginalBytes(&code, target, patchSize);
    size_t jmpOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveTextRenderText));
    WriteRel32(stub, jmpOffset, reinterpret_cast<BYTE *>(target) + patchSize);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

BYTE *BuildEaxWideEmbedAfterNewStub(void *target, volatile LONG *hitCounter) {
    BYTE *original = static_cast<BYTE *>(target);
    if (!original || original[0] != 0x8B || (original[1] & 0xC7) != 0xC0 ||
        original[2] != 0x85 || original[3] != 0xC0 || original[4] != 0x75) {
        return nullptr;
    }

    std::vector<BYTE> code;
    AppendByte(&code, 0x9C);                                         // pushfd
    AppendByte(&code, 0x60);                                         // pushad
    AppendBytes(&code, {0x8B, 0x44, 0x24, 0x1C});                   // mov eax,[esp+28] saved EAX
    AppendByte(&code, 0x68);
    AppendU32(&code, reinterpret_cast<uintptr_t>(hitCounter));
    AppendByte(&code, 0x68);
    AppendU32(&code, 4096);
    AppendByte(&code, 0x50);                                         // push original UTF-16 EAX
    size_t callOffset = code.size();
    AppendBytes(&code, {0xE8, 0, 0, 0, 0});
    AppendBytes(&code, {0x89, 0x44, 0x24, 0x1C});                   // replace saved EAX
    AppendByte(&code, 0x61);                                         // popad
    AppendByte(&code, 0x9D);                                         // popfd
    AppendBytes(&code, {original[0], original[1]});                  // mov destination,EAX
    AppendBytes(&code, {0x85, 0xC0});                               // test eax,eax
    size_t jneOffset = code.size();
    AppendBytes(&code, {0x0F, 0x85, 0, 0, 0, 0});                   // jne original nonzero branch
    size_t jmpZeroOffset = code.size();
    AppendBytes(&code, {0xE9, 0, 0, 0, 0});                         // jmp original zero branch

    BYTE *stub = AllocateExecutableStub(code);
    if (!stub) return nullptr;
    WriteRel32(stub, callOffset, reinterpret_cast<void *>(KiriKiriResolveTextRenderText));
    BYTE *nonzeroTarget = original + 6 + static_cast<int8_t>(original[5]);
    *reinterpret_cast<int32_t *>(stub + jneOffset + 2) =
        static_cast<int32_t>(nonzeroTarget - (stub + jneOffset + 6));
    WriteRel32(stub, jmpZeroOffset, original + 6);
    FlushInstructionCache(GetCurrentProcess(), stub, code.size());
    return stub;
}

bool FindKagParserInternalTargetNearLineBreak(HMODULE module, bool exParser, void **out) {
    if (!module || !out) return false;
    BYTE *imageBase = nullptr;
    size_t imageSize = 0;
    if (!ModuleImageRange(module, &imageBase, &imageSize)) return false;

    const wchar_t lineBreak[] = L"[r]";
    void *lineBreakAddress = nullptr;
    if (!SearchPattern(
            imageBase,
            imageSize,
            reinterpret_cast<const BYTE *>(lineBreak),
            std::string((wcslen(lineBreak) + 1) * sizeof(wchar_t), 'x').c_str(),
            &lineBreakAddress)) {
        return false;
    }

    void *pushAddress = nullptr;
    if (!FindFirstPushAddress(imageBase, imageSize, reinterpret_cast<uintptr_t>(lineBreakAddress), &pushAddress)) {
        return false;
    }

    const BYTE patternKag[] = {0x66, 0x83, 0x3C, 0x48, 0x5B};
    const BYTE patternKagEx[] = {0x66, 0x83, 0x3C, 0x41, 0x5B};
    BYTE *nearBegin = static_cast<BYTE *>(pushAddress);
    BYTE *nearEnd = std::min(imageBase + imageSize, nearBegin + 0x80);
    void *target = nullptr;
    if (SearchPatternRange(nearBegin, nearEnd, exParser ? patternKagEx : patternKag, "xxxxx", &target)) {
        *out = target;
        return true;
    }
    nearBegin = nearBegin > imageBase + 0x80 ? nearBegin - 0x80 : imageBase;
    nearEnd = std::min(imageBase + imageSize, static_cast<BYTE *>(pushAddress) + 0x80);
    if (SearchPatternRange(nearBegin, nearEnd, exParser ? patternKagEx : patternKag, "xxxxx", &target)) {
        *out = target;
        return true;
    }
    return false;
}

bool FindKagParserTjsStringEntry(HMODULE module, void **out) {
    if (!module || !out) return false;
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleImageRange(module, &base, &size)) return false;
    const char marker[] = "tTJSString tTJSString::operator +(const tjs_char *) const";
    void *markerAddress = nullptr;
    if (!SearchPattern(base, size, reinterpret_cast<const BYTE *>(marker), std::string(strlen(marker), 'x').c_str(), &markerAddress)) {
        return false;
    }
    uintptr_t markerPtr = reinterpret_cast<uintptr_t>(markerAddress);
    BYTE ptrPattern[4] = {
        static_cast<BYTE>(markerPtr & 0xFF),
        static_cast<BYTE>((markerPtr >> 8) & 0xFF),
        static_cast<BYTE>((markerPtr >> 16) & 0xFF),
        static_cast<BYTE>((markerPtr >> 24) & 0xFF),
    };
    void *reference = nullptr;
    if (!SearchPattern(base, size, ptrPattern, "xxxx", &reference)) return false;
    BYTE *entry = FindEnclosingX86Function(static_cast<BYTE *>(reference), base, size, 0x200);
    if (!entry) return false;
    *out = entry;
    return true;
}

bool InstallKagParserEntryHook(const wchar_t *moduleName, void **targetSlot) {
    HMODULE module = GetModuleHandleW(moduleName);
    if (!module || !targetSlot) return false;
    if (*targetSlot) return true;
    void *target = nullptr;
    if (!FindKagParserTjsStringEntry(module, &target)) return false;
    BYTE *entry = static_cast<BYTE *>(target);
    if (!(entry[0] == 0x55 && entry[1] == 0x8B && entry[2] == 0xEC && entry[3] == 0x6A && entry[4] == 0xFF)) {
        return false;
    }
    BYTE *stub = BuildKagParserEntryStub(target);
    if (!stub) return false;
    std::wstring label = std::wstring(moduleName) + L" tTJSString arg2";
    if (!InstallRuntimeJump(target, 5, stub, label.c_str())) return false;
    *targetSlot = target;
    return true;
}

bool FindKagParserTextBufferCandidate(HMODULE module, bool exParser, void **out) {
    if (!module || !out) return false;
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;
    const BYTE patternKag[] = {0x66, 0x83, 0x3C, 0x48, 0x5B};
    const BYTE patternKagEx[] = {0x66, 0x83, 0x3C, 0x41, 0x5B};
    void *target = nullptr;
    if (!FindKagParserInternalTargetNearLineBreak(module, exParser, &target) &&
        !SearchPattern(base, size, exParser ? patternKagEx : patternKag, "xxxxx", &target)) {
        if (!ModuleImageRange(module, &base, &size) ||
            !SearchPattern(base, size, exParser ? patternKagEx : patternKag, "xxxxx", &target)) {
            return false;
        }
    }
    *out = target;
    return true;
}

bool InstallKagParserInternalHook(const wchar_t *moduleName, bool exParser) {
    HMODULE module = GetModuleHandleW(moduleName);
    if (!module) return false;
    void **targetSlot = exParser ? &gKagParserExHookTarget : &gKagParserHookTarget;
    if (*targetSlot) return true;

    void *target = nullptr;
    if (!FindKagParserTextBufferCandidate(module, exParser, &target)) return false;
    BYTE *stub = BuildKagParserStub(target, exParser, &gInternalKagHits);
    if (!stub) return false;
    const wchar_t *label = exParser ? L"KAGParserEx text buffer" : L"KAGParser text buffer";
    if (!InstallRuntimeJump(target, 5, stub, label)) return false;
    *targetSlot = target;
    return true;
}

bool FindTextRenderFallbackCandidate(HMODULE module, void **out) {
    if (!module || !out || !GetProcAddress(module, "V2Link")) return false;
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;
    const BYTE pattern[] = {
        0x83, 0x00, 0x52,
        0x0F, 0x87, 0x00, 0x00, 0x00, 0x00,
        0x0F, 0xB6, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00
    };
    std::string mask(sizeof(pattern), '?');
    for (size_t index : {size_t(0), size_t(2), size_t(3), size_t(4),
                         size_t(9), size_t(10), size_t(16), size_t(17)}) {
        mask[index] = 'x';
    }
    void *match = nullptr;
    if (!SearchPattern(base, size, pattern, mask.c_str(), &match)) return false;

    std::vector<BYTE> secondPattern(pattern, pattern + sizeof(pattern));
    secondPattern[2] = 0x0F;
    BYTE *searchBegin = static_cast<BYTE *>(match) + sizeof(pattern);
    BYTE *searchEnd = std::min(base + size, searchBegin + 0x100);
    void *second = nullptr;
    if (!SearchPatternRange(searchBegin, searchEnd, secondPattern.data(), mask.c_str(), &second)) return false;

    BYTE *entry = FindEnclosingX86Function(static_cast<BYTE *>(match), base, size, 0x400);
    if (!entry || GuessSafeEntryPatchSize(entry) < 5) return false;
    *out = entry;
    return true;
}

bool FindTextRenderGetStringEaxCandidate(HMODULE module, void **out) {
    if (!module || !out || !GetProcAddress(module, "V2Link")) return false;
    BYTE *imageBase = nullptr;
    size_t imageSize = 0;
    BYTE *textBase = nullptr;
    size_t textSize = 0;
    if (!ModuleImageRange(module, &imageBase, &imageSize) ||
        !ModuleTextRange(module, &textBase, &textSize)) {
        return false;
    }

    const char marker[] = "const tjs_char * tTJSVariant::GetString() const";
    const size_t markerSize = sizeof(marker);
    for (size_t markerOffset = 0; markerOffset + markerSize <= imageSize; ++markerOffset) {
        if (memcmp(imageBase + markerOffset, marker, markerSize) != 0) continue;
        uintptr_t markerAddress = reinterpret_cast<uintptr_t>(imageBase + markerOffset);
        for (size_t offset = 0; offset + 27 <= textSize; ++offset) {
            BYTE *resolver = textBase + offset;
            if (resolver[0] != 0x68 ||
                *reinterpret_cast<uint32_t *>(resolver + 1) != static_cast<uint32_t>(markerAddress)) {
                continue;
            }

            // Texture-render variant: push marker; call resolver; add esp,4;
            // store function pointer; push register; call that pointer.
            if (resolver[5] != 0xE8 ||
                resolver[10] != 0x83 || resolver[11] != 0xC4 || resolver[12] != 0x04 ||
                resolver[18] < 0x50 || resolver[18] > 0x57 ||
                resolver[19] != 0xFF || (resolver[20] & 0xF8) != 0xD0) {
                continue;
            }

            BYTE *target = resolver + 21;
            if (target[0] != 0x8B || (target[1] & 0xC7) != 0xC0 ||
                target[2] != 0x85 || target[3] != 0xC0 || target[4] != 0x75) {
                continue;
            }
            *out = target;
            return true;
        }
    }
    return false;
}

bool InstallTextRenderInternalHook() {
    if (gTextRenderHookTarget) return true;
    HMODULE module = GetModuleHandleW(L"textrender.dll");
    void *target = nullptr;
    if (FindTextRenderGetStringEaxCandidate(module, &target)) {
        BYTE *stub = BuildEaxWideEmbedAfterNewStub(target, &gTextRenderHits);
        if (!stub || !InstallRuntimeJump(target, 6, stub, L"TextRender EAX GetString")) return false;
        gTextRenderHookTarget = target;
        return true;
    }
    if (!FindTextRenderFallbackCandidate(module, &target)) return false;
    size_t patchSize = GuessSafeEntryPatchSize(static_cast<BYTE *>(target));
    BYTE *stub = BuildStackWideEmbedAfterNewStub(target, patchSize, &gTextRenderHits);
    if (!stub) return false;
    if (!InstallRuntimeJump(target, patchSize, stub, L"TextRender UTF-16 arg1")) return false;
    gTextRenderHookTarget = target;
    return true;
}

bool InstallPsbPostConversionHook() {
    if (gPsbPostHookTarget) return true;
    HMODULE module = GetModuleHandleW(L"psbfile.dll");
    void *target = kirikiri_psb_runtime::FindUtf8ToWidePostCall(module);
    if (!target) return false;
    BYTE *stub = BuildPsbPostConversionStub(target);
    if (!stub || !InstallRuntimeJump(target, 5, stub, L"PSB UTF-8 to UTF-16 post conversion")) {
        return false;
    }
    gPsbPostHookTarget = target;
    return true;
}

bool InstallKiriKiriZ3InternalHook() {
    if (gKiriKiriZ3HookTarget) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;
    const BYTE pattern[] = {
        0x66, 0x83, 0x3F, 0x00,
        0x75, 0x06,
        0x33, 0xDB,
        0x89, 0x1E,
        0xEB, 0x1B
    };
    void *target = nullptr;
    if (!SearchPattern(base, size, pattern, "xxxxxxxxxxxx", &target)) return false;
    BYTE *stub = BuildKiriKiriZ3Stub(target, &gInternalKirikiriZ3Hits);
    if (!stub) return false;
    if (!InstallRuntimeJump(target, 6, stub, L"KiriKiriZ3 text pointer")) return false;
    gKiriKiriZ3HookTarget = target;
    return true;
}

bool InstallKiriKiriZXInternalHook() {
    if (gKiriKiriZXHookTarget) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;

    const BYTE pattern[] = {
        0x8B, 0x4D, 0x10,
        0x66, 0x90,
        0x8B, 0x47, 0x00,
        0x48,
        0x83, 0xF8, 0x04,
        0x0F, 0x87, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0x24, 0x85, 0x00, 0x00, 0x00, 0x00
    };
    std::string mask(sizeof(pattern), 'x');
    mask[7] = '?';
    for (size_t i = 14; i <= 17; ++i) mask[i] = '?';
    for (size_t i = 21; i <= 24; ++i) mask[i] = '?';

    void *match = nullptr;
    if (!SearchPattern(base, size, pattern, mask.c_str(), &match)) return false;
    BYTE *entry = FindEnclosingX86Function(static_cast<BYTE *>(match), base, size, 0x500);
    size_t patchSize = GuessSafeEntryPatchSize(entry);
    if (!entry || patchSize < 5) return false;
    BYTE *stub = BuildStackTjsStringCaptureStub(entry, patchSize, 8, &gInternalKirikiriZXHits);
    if (!stub) return false;
    if (!InstallRuntimeJump(entry, patchSize, stub, L"KiriKiriZX stack text")) return false;
    gKiriKiriZXHookTarget = entry;
    return true;
}

bool InstallKrkrZ2InternalHook() {
    if (gKrkrZ2HookTarget) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;

    const BYTE pattern[] = {
        0x3B, 0x00,
        0x73, 0x18,
        0x0F, 0x1F, 0x80, 0x00, 0x00, 0x00, 0x00,
        0x8B, 0x43, 0x38,
        0x56,
        0x8B, 0x00,
        0xFF, 0xD0,
        0x03, 0xF0,
        0x83, 0xC4, 0x04,
        0x3B, 0xF7,
        0x72, 0x00,
        0x8B, 0x43, 0x4C,
        0x8B, 0xCE,
        0x48,
        0x83, 0xF8, 0x04,
        0x0F, 0x87, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0x24, 0x85, 0x00, 0x00, 0x00, 0x00
    };
    std::string mask(sizeof(pattern), 'x');
    mask[1] = '?';
    mask[27] = '?';
    for (size_t i = 39; i <= 42; ++i) mask[i] = '?';
    for (size_t i = 46; i <= 49; ++i) mask[i] = '?';

    void *match = nullptr;
    if (!SearchPattern(base, size, pattern, mask.c_str(), &match)) return false;
    BYTE *entry = FindEnclosingX86Function(static_cast<BYTE *>(match), base, size, 0x500);
    size_t patchSize = GuessSafeEntryPatchSize(entry);
    if (!entry || patchSize < 5) return false;
    BYTE *stub = BuildRegisterWideCaptureStub(entry, patchSize, 0x52, &gInternalKrkrZ2Hits); // edx
    if (!stub) return false;
    if (!InstallRuntimeJump(entry, patchSize, stub, L"krkrz2 edx text")) return false;
    gKrkrZ2HookTarget = entry;
    return true;
}

bool IsMovRegFromEcx(BYTE opcode, BYTE modrm) {
    if (opcode != 0x8B) return false;
    switch (modrm) {
    case 0xC1: // eax, ecx
    case 0xD9: // ebx, ecx
    case 0xE9: // ebp, ecx
    case 0xD1: // edx, ecx
    case 0xF9: // edi, ecx
    case 0xF1: // esi, ecx
        return true;
    default:
        return false;
    }
}

bool IsUtf8ByteLoadFromLikelyTextRegister(BYTE opcode, BYTE modrm) {
    if (opcode != 0x8A) return false;
    switch (modrm) {
    case 0x06: // al, [esi]
    case 0x1E: // bl, [esi]
    case 0x0E: // cl, [esi]
    case 0x16: // dl, [esi]
        return true;
    default:
        return false;
    }
}

bool HasUtf8ClassifierSequence(BYTE *begin, BYTE *end) {
    if (!begin || !end || begin >= end) return false;
    const BYTE thresholds[] = {0x80, 0xC2, 0xE0, 0xF0, 0xF8, 0xFC, 0xFE};
    for (BYTE threshold : thresholds) {
        bool found = false;
        for (BYTE *p = begin; p + 3 <= end; ++p) {
            if (p[0] == 0x3C && p[1] == threshold) {
                found = true;
                break;
            }
            if (p[0] == 0x80 && p[2] == threshold) {
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    return true;
}

size_t GuessEmbedKrkrZPatchSize(BYTE *target, BYTE *load) {
    if (!target || !load || load < target) return 0;
    size_t size = static_cast<size_t>(load - target) + 2;             // include mov r8,[reg]
    BYTE *next = load + 2;
    if (next[0] == 0x3C) {                                            // cmp al, imm8
        size = static_cast<size_t>(next - target) + 2;
    } else if (next[0] == 0x80) {                                     // cmp byte ptr [...], imm8
        size = static_cast<size_t>(next - target) + 3;
    }
    return size >= 5 && size <= 16 ? size : 0;
}

BYTE GuessEmbedKrkrZReturnAddressOffset(BYTE *target, BYTE *imageBase) {
    if (!target || !imageBase || target < imageBase + 8) return 0;

    // Whole-string UTF-8 converter:
    // push ebp; mov ebp,esp; sub esp,8; push ebx; push esi; <hook>
    const BYTE framedPrefix[] = {0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x08, 0x53, 0x56};
    if (memcmp(target - sizeof(framedPrefix), framedPrefix, sizeof(framedPrefix)) == 0) {
        return 0x38;  // pushfd + pushad (36) + original frame depth (20)
    }

    // Single-codepoint converter: push ebx; push esi; push edi; <hook>
    const BYTE framelessPrefix[] = {0x53, 0x56, 0x57};
    if (memcmp(target - sizeof(framelessPrefix), framelessPrefix, sizeof(framelessPrefix)) == 0) {
        return 0x30;  // pushfd + pushad (36) + original frame depth (12)
    }
    return 0;
}

bool InstallEmbedKrkrZUtf8Hooks() {
    if (!gEmbedKrkrZHookTargets.empty()) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;
    BYTE *end = base + size;
    bool installed = false;
    for (BYTE *p = base; p + 16 < end && gEmbedKrkrZHookTargets.size() < 8; ++p) {
        if (!IsMovRegFromEcx(p[0], p[1])) continue;
        BYTE *load = nullptr;
        for (BYTE *q = p + 2; q + 2 < std::min(end, p + 0x10); ++q) {
            if (IsUtf8ByteLoadFromLikelyTextRegister(q[0], q[1])) {
                load = q;
                break;
            }
        }
        if (!load) continue;
        if (!HasUtf8ClassifierSequence(load, std::min(end, load + 0x1000))) continue;
        if (p[1] == 0xF1 && !gEmbedKrkrZConverterTarget) {             // mov esi,ecx; byte load from [esi]
            BYTE *entry = FindEnclosingX86Function(p, base, size, 0x40);
            size_t entryPatchSize = GuessSafeEntryPatchSize(entry);
            if (entry && entryPatchSize >= 5) {
                BYTE *entryStub = BuildUtf8ConverterEntryStub(
                    entry,
                    entryPatchSize,
                    &gInternalEmbedKrkrZHits);
                if (entryStub &&
                    InstallRuntimeJump(entry, entryPatchSize, entryStub, L"EmbedKrkrZ UTF-8 converter entry")) {
                    gEmbedKrkrZConverterTarget = entry;
                    gEmbedKrkrZHookTargets.push_back(entry);
                    installed = true;
                    continue;
                }
            }
        }
        BYTE cursorContract = GuessEmbedKrkrZReturnAddressOffset(p, base);
        if (cursorContract == 0x30 && !gEmbedKrkrZCursorTarget) {       // char ** cursor decoder
            BYTE *entry = p - 3;                                      // push ebx; push esi; push edi
            size_t entryPatchSize = GuessSafeEntryPatchSize(entry);
            if (entry && entryPatchSize >= 5) {
                BYTE *entryStub = BuildUtf8CursorEntryStub(
                    entry,
                    entryPatchSize,
                    &gInternalEmbedKrkrZHits);
                if (entryStub &&
                    InstallRuntimeJump(entry, entryPatchSize, entryStub, L"EmbedKrkrZ UTF-8 cursor entry")) {
                    gEmbedKrkrZCursorTarget = entry;
                    gEmbedKrkrZHookTargets.push_back(entry);
                    installed = true;
                    continue;
                }
            }
        }
        size_t patchSize = GuessEmbedKrkrZPatchSize(p, load);
        if (patchSize < 5) continue;
        BYTE *stub = BuildRegisterNarrowCaptureStub(
            p,
            patchSize,
            0x51,
            GuessEmbedKrkrZReturnAddressOffset(p, base),
            &gInternalEmbedKrkrZHits); // ecx
        if (!stub) continue;
        if (InstallRuntimeJump(p, patchSize, stub, L"EmbedKrkrZ utf8 text")) {
            gEmbedKrkrZHookTargets.push_back(p);
            installed = true;
        }
    }
    return installed;
}

bool InstallKiriKiriZ2IndirectHook() {
    if (gKiriKiriZ2HookTarget) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;
    const BYTE pattern[] = {
        0x38, 0x4B, 0x21,
        0x0F, 0x95, 0xC1,
        0x33, 0xC0,
        0x38, 0x43, 0x20,
        0x0F, 0x95, 0xC0,
        0x33, 0xC8,
        0x33, 0x4B, 0x10,
        0x0F, 0xB7, 0x43, 0x14
    };
    void *match = nullptr;
    if (!SearchPattern(base, size, pattern, "xxxxxxxxxxxxxxxxxxxxxxx", &match)) return false;
    BYTE *entry = FindEnclosingX86Function(static_cast<BYTE *>(match), base, size, 0x100);
    size_t patchSize = GuessSafeEntryPatchSize(entry);
    if (!entry || patchSize < 5) return false;
    BYTE *stub = BuildEcxIndirectWideCaptureStub(entry, patchSize, 0x14, &gInternalKiriKiriZ2Hits);
    if (!stub) return false;
    if (!InstallRuntimeJump(entry, patchSize, stub, L"KiriKiriZ2 ecx+14 text")) return false;
    gKiriKiriZ2HookTarget = entry;
    return true;
}

bool InstallEmbedKrkr2WideHook() {
    if (gEmbedKrkr2HookTarget) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;
    const BYTE pattern[] = {
        0x66, 0x8B, 0x06,
        0x66, 0x83, 0xF8, 0x3B,
        0x0F, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x66, 0x83, 0xF8, 0x2A,
        0x0F
    };
    std::string mask(sizeof(pattern), 'x');
    for (size_t i = 8; i <= 12; ++i) mask[i] = '?';
    void *target = nullptr;
    if (!SearchPattern(base, size, pattern, mask.c_str(), &target)) return false;
    BYTE *stub = BuildEmbedKrkr2WideSlotStub(target, 7);
    if (!stub) return false;
    if (!InstallRuntimeJump(target, 7, stub, L"EmbedKrkr2 esi text slot")) return false;
    gEmbedKrkr2HookTarget = target;
    return true;
}

bool InstallKrkr2WcsHook() {
    if (gKrkr2WcsHookTarget) return true;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) return false;

    const BYTE wcscpyPattern[] = {
        0x55, 0x8B, 0xEC,
        0x53,
        0x56,
        0x8B, 0x75, 0x0C,
        0x56,
        0xE8, 0x00, 0xFF, 0xFF, 0xFF,
        0x59,
        0x8B, 0xD8,
        0x33, 0x00,
        0x8B, 0x45, 0x08
    };
    std::string wcscpyMask(sizeof(wcscpyPattern), 'x');
    wcscpyMask[10] = '?';
    wcscpyMask[18] = '?';

    const BYTE wcslenPattern[] = {
        0x55, 0x8B, 0xEC,
        0x33, 0x00,
        0x8B, 0x45, 0x08,
        0xEB, 0x04,
        0x00,
        0x83, 0xC0, 0x02,
        0x66, 0x83, 0x38, 0x00,
        0x75, 0xF6,
        0x8B, 0x00,
        0x5D,
        0xC3
    };
    std::string wcslenMask(sizeof(wcslenPattern), 'x');
    wcslenMask[4] = '?';
    wcslenMask[10] = '?';
    wcslenMask[21] = '?';

    void *target = nullptr;
    BYTE stackOffset = 8;
    if (!SearchPattern(base, size, wcscpyPattern, wcscpyMask.c_str(), &target)) {
        stackOffset = 4;
        if (!SearchPattern(base, size, wcslenPattern, wcslenMask.c_str(), &target)) return false;
    }
    BYTE *stub = BuildStackWideCaptureStub(target, 5, stackOffset, &gInternalKrkr2WcsHits);
    if (!stub) return false;
    if (!InstallRuntimeJump(target, 5, stub, stackOffset == 8 ? L"Krkr2wcs wcscpy text" : L"Krkr2wcs wcslen text")) return false;
    gKrkr2WcsHookTarget = target;
    return true;
}

size_t CountEmbedKrkrZCandidates(BYTE *base, size_t size, uintptr_t *firstAddress) {
    if (firstAddress) *firstAddress = 0;
    if (!base || size < 16) return 0;
    BYTE *end = base + size;
    size_t count = 0;
    for (BYTE *p = base; p + 16 < end; ++p) {
        if (!IsMovRegFromEcx(p[0], p[1])) continue;
        BYTE *load = nullptr;
        for (BYTE *q = p + 2; q + 2 < std::min(end, p + 0x10); ++q) {
            if (IsUtf8ByteLoadFromLikelyTextRegister(q[0], q[1])) {
                load = q;
                break;
            }
        }
        if (!load) continue;
        if (!HasUtf8ClassifierSequence(load, std::min(end, load + 0x1000))) continue;
        if (GuessEmbedKrkrZPatchSize(p, load) < 5) continue;
        if (count == 0 && firstAddress) *firstAddress = reinterpret_cast<uintptr_t>(p);
        ++count;
    }
    return count;
}

size_t CountIatCallsToFunction(void *functionAddress, uintptr_t *firstAddress) {
    if (firstAddress) *firstAddress = 0;
    if (!functionAddress) return 0;
    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *imageBase = nullptr;
    size_t imageSize = 0;
    BYTE *textBase = nullptr;
    size_t textSize = 0;
    if (!ModuleImageRange(module, &imageBase, &imageSize) || !ModuleTextRange(module, &textBase, &textSize)) {
        return 0;
    }

    std::vector<uint32_t> slots;
    uint32_t wanted = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(functionAddress));
    for (size_t i = 0; i + sizeof(uint32_t) <= imageSize; ++i) {
        uint32_t value = 0;
        memcpy(&value, imageBase + i, sizeof(value));
        if (value == wanted) slots.push_back(static_cast<uint32_t>(reinterpret_cast<uintptr_t>(imageBase + i)));
    }
    if (slots.empty()) return 0;

    size_t count = 0;
    BYTE *end = textBase + textSize;
    for (BYTE *p = textBase; p + 6 <= end; ++p) {
        if (p[0] != 0xFF || p[1] != 0x15) continue; // call dword ptr [iat]
        uint32_t slot = 0;
        memcpy(&slot, p + 2, sizeof(slot));
        if (std::find(slots.begin(), slots.end(), slot) == slots.end()) continue;
        if (count == 0 && firstAddress) *firstAddress = reinterpret_cast<uintptr_t>(p);
        ++count;
    }
    return count;
}

void ReportCaptureHookCandidate(bool found, const std::wstring &name, uintptr_t address = 0, size_t count = 0, const wchar_t *risk = nullptr) {
    std::wstring line = std::wstring(L"[kirikiri native] capture candidate ") + name +
                        L": " + (found ? L"yes" : L"no");
    if (count) line += L" count=" + std::to_wstring(count);
    if (address) line += L" @" + std::to_wstring(address);
    if (risk && *risk) line += L" risk=" + std::wstring(risk);
    Log(line);
}

void ReportKagParserModuleCandidates(const wchar_t *moduleName, bool exParser) {
    HMODULE module = GetModuleHandleW(moduleName);
    std::wstring prefix(moduleName);
    ReportCaptureHookCandidate(module != nullptr, prefix + L" module",
                            reinterpret_cast<uintptr_t>(module), 0, L"safe-scan");
    if (!module) return;

    void *target = nullptr;
    ReportCaptureHookCandidate(FindKagParserTjsStringEntry(module, &target),
                            prefix + L" tTJSString arg2",
                            reinterpret_cast<uintptr_t>(target), 0, L"default-safe");
    target = nullptr;
    ReportCaptureHookCandidate(FindKagParserTextBufferCandidate(module, exParser, &target),
                            prefix + L" text buffer",
                            reinterpret_cast<uintptr_t>(target), 0, L"default-safe");
}

void ReportLegacyGdiCallerCandidates() {
    HMODULE gdi32 = GetModuleHandleW(L"gdi32.dll");
    if (!gdi32) gdi32 = LoadLibraryW(L"gdi32.dll");
    if (!gdi32) {
        ReportCaptureHookCandidate(false, L"KiriKiri1/GetGlyphOutlineW caller", 0, 0, L"observe-only");
        ReportCaptureHookCandidate(false, L"KiriKiri2/GetTextExtentPoint32W caller", 0, 0, L"observe-only");
        return;
    }

    uintptr_t first = 0;
    size_t count = CountIatCallsToFunction(reinterpret_cast<void *>(GetProcAddress(gdi32, "GetGlyphOutlineW")), &first);
    ReportCaptureHookCandidate(count > 0, L"KiriKiri1/GetGlyphOutlineW caller", first, count, L"observe-only");

    first = 0;
    count = CountIatCallsToFunction(reinterpret_cast<void *>(GetProcAddress(gdi32, "GetTextExtentPoint32W")), &first);
    ReportCaptureHookCandidate(count > 0, L"KiriKiri2/GetTextExtentPoint32W caller", first, count, L"observe-only");
}

void ReportTextRenderCandidate() {
    HMODULE module = GetModuleHandleW(L"textrender.dll");
    void *target = nullptr;
    bool found = FindTextRenderGetStringEaxCandidate(module, &target);
    ReportCaptureHookCandidate(found, L"TextRender EAX GetString/textrender.dll",
                            reinterpret_cast<uintptr_t>(target), 0, L"default-embed");
    if (!found) {
        target = nullptr;
        found = FindTextRenderFallbackCandidate(module, &target);
        ReportCaptureHookCandidate(found, L"TextRender stack arg1/textrender.dll",
                                reinterpret_cast<uintptr_t>(target), 0, L"fallback-embed");
    }
}

void ReportExperimentalTextHookCandidates() {
    static bool reported = false;
    if (reported) return;
    reported = true;

    Log(L"[kirikiri native] capture candidate matrix begin");
    ReportKagParserModuleCandidates(L"KAGParser.dll", false);
    ReportKagParserModuleCandidates(L"ExtKAGParser.dll", true);
    ReportKagParserModuleCandidates(L"KAGParserEx.dll", true);
    ReportTextRenderCandidate();
    ReportLegacyGdiCallerCandidates();

    HMODULE module = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(module, &base, &size)) {
        Log(L"[kirikiri native] capture candidate scan skipped: executable .text not found");
        Log(L"[kirikiri native] capture candidate matrix end");
        return;
    }

    void *target = nullptr;
    const BYTE krkrz3Pattern[] = {
        0x66, 0x83, 0x3F, 0x00,
        0x75, 0x06,
        0x33, 0xDB,
        0x89, 0x1E,
        0xEB, 0x1B
    };
    ReportCaptureHookCandidate(SearchPattern(base, size, krkrz3Pattern, "xxxxxxxxxxxx", &target),
                            L"KiriKiriZ3", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");

    const BYTE kirikiriZXPattern[] = {
        0x8B, 0x4D, 0x10,
        0x66, 0x90,
        0x8B, 0x47, 0x00,
        0x48,
        0x83, 0xF8, 0x04,
        0x0F, 0x87, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0x24, 0x85, 0x00, 0x00, 0x00, 0x00
    };
    std::string kirikiriZXMask(sizeof(kirikiriZXPattern), 'x');
    kirikiriZXMask[7] = '?';
    for (size_t i = 14; i <= 17; ++i) kirikiriZXMask[i] = '?';
    for (size_t i = 21; i <= 24; ++i) kirikiriZXMask[i] = '?';
    target = nullptr;
    ReportCaptureHookCandidate(SearchPattern(base, size, kirikiriZXPattern, kirikiriZXMask.c_str(), &target),
                            L"KiriKiriZX", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");

    const BYTE krkrz2Pattern[] = {
        0x3B, 0x00,
        0x73, 0x18,
        0x0F, 0x1F, 0x80, 0x00, 0x00, 0x00, 0x00,
        0x8B, 0x43, 0x38,
        0x56,
        0x8B, 0x00,
        0xFF, 0xD0,
        0x03, 0xF0,
        0x83, 0xC4, 0x04,
        0x3B, 0xF7,
        0x72, 0x00,
        0x8B, 0x43, 0x4C,
        0x8B, 0xCE,
        0x48,
        0x83, 0xF8, 0x04,
        0x0F, 0x87, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0x24, 0x85, 0x00, 0x00, 0x00, 0x00
    };
    std::string krkrz2Mask(sizeof(krkrz2Pattern), 'x');
    krkrz2Mask[1] = '?';
    krkrz2Mask[27] = '?';
    for (size_t i = 39; i <= 42; ++i) krkrz2Mask[i] = '?';
    for (size_t i = 46; i <= 49; ++i) krkrz2Mask[i] = '?';
    target = nullptr;
    ReportCaptureHookCandidate(SearchPattern(base, size, krkrz2Pattern, krkrz2Mask.c_str(), &target),
                            L"krkrz2", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");

    const BYTE kirikiriZ2Pattern[] = {
        0x38, 0x4B, 0x21,
        0x0F, 0x95, 0xC1,
        0x33, 0xC0,
        0x38, 0x43, 0x20,
        0x0F, 0x95, 0xC0,
        0x33, 0xC8,
        0x33, 0x4B, 0x10,
        0x0F, 0xB7, 0x43, 0x14
    };
    target = nullptr;
    ReportCaptureHookCandidate(SearchPattern(base, size, kirikiriZ2Pattern, "xxxxxxxxxxxxxxxxxxxxxxx", &target),
                            L"KiriKiriZ2", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");

    uintptr_t firstEmbed = 0;
    size_t embedCount = CountEmbedKrkrZCandidates(base, size, &firstEmbed);
    ReportCaptureHookCandidate(embedCount > 0, L"EmbedKrkrZ", firstEmbed, embedCount, L"experimental-inline");

    const BYTE embedKrkr2Pattern[] = {
        0x66, 0x8B, 0x06,
        0x66, 0x83, 0xF8, 0x3B,
        0x0F, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x66, 0x83, 0xF8, 0x2A,
        0x0F
    };
    std::string embedKrkr2Mask(sizeof(embedKrkr2Pattern), 'x');
    for (size_t i = 8; i <= 12; ++i) embedKrkr2Mask[i] = '?';
    target = nullptr;
    ReportCaptureHookCandidate(SearchPattern(base, size, embedKrkr2Pattern, embedKrkr2Mask.c_str(), &target),
                            L"EmbedKrkr2", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");

    const BYTE wcscpyPattern[] = {
        0x55, 0x8B, 0xEC,
        0x53,
        0x56,
        0x8B, 0x75, 0x0C,
        0x56,
        0xE8, 0x00, 0xFF, 0xFF, 0xFF,
        0x59,
        0x8B, 0xD8,
        0x33, 0x00,
        0x8B, 0x45, 0x08
    };
    std::string wcscpyMask(sizeof(wcscpyPattern), 'x');
    wcscpyMask[10] = '?';
    wcscpyMask[18] = '?';
    target = nullptr;
    ReportCaptureHookCandidate(SearchPattern(base, size, wcscpyPattern, wcscpyMask.c_str(), &target),
                            L"Krkr2wcs", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");

    const BYTE kirikiri4Pattern[] = {
        0x66, 0xC7, 0x45, 0x00, 0xA4, 0x00,
        0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00,
        0xE8, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00,
        0x00, 0x00, 0x00,
        0x00, 0x00, 0x00,
        0x8B, 0x53, 0x00,
        0xE8, 0x00, 0x00, 0x00, 0x00
    };
    std::string kirikiri4Mask(sizeof(kirikiri4Pattern), '?');
    for (size_t index : {0u, 1u, 2u, 4u, 5u, 18u, 32u, 33u, 35u}) {
        kirikiri4Mask[index] = 'x';
    }
    target = nullptr;
    ReportCaptureHookCandidate(SearchPattern(base, size, kirikiri4Pattern, kirikiri4Mask.c_str(), &target),
                            L"KiriKiri4", reinterpret_cast<uintptr_t>(target), 0, L"experimental-inline");
    Log(L"[kirikiri native] capture candidate matrix end");
}

bool ExperimentalTextHooksEnabled() {
    // Legacy fallback: a launcher deployed before the rename only sets the old
    // name, and an enable flag that silently reads as "off" is worse than one
    // that honours both spellings.
    return EnvFlagEnabled(L"KIRIKIRI_EXPERIMENTAL_TEXT_HOOKS") ||
           EnvFlagEnabled(L"KIRIKIRI_EXPERIMENTAL_LUNA_HOOKS");
}

std::wstring CaptureHookMode() {
    // The current launcher writes both names. Reading the legacy name as a
    // fallback keeps a previously deployed launcher (which only knew that name)
    // working: without it this build would silently drop to the default profile
    // instead of reporting a mismatch.
    static const wchar_t *kEnvNames[] = {L"KIRIKIRI_CAPTURE_HOOKS", L"KIRIKIRI_LUNA_CAPTURE_HOOKS"};
    for (const wchar_t *envName : kEnvNames) {
        wchar_t value[128] = {};
        DWORD n = GetEnvironmentVariableW(envName, value, 128);
        if (n == 0 || n >= 128) continue;
        std::wstring mode(value, value + n);
        std::transform(mode.begin(), mode.end(), mode.begin(), towlower);
        return mode;
    }
    return L"";
}

bool CaptureHookSelected(const std::wstring &mode, const wchar_t *name) {
    if (mode.empty()) return false;
    if (mode == L"1" || mode == L"true" || mode == L"yes" || mode == L"all") return true;
    return mode.find(name) != std::wstring::npos;
}

// Top-level exception guard using VEH + setjmp/longjmp to prevent crashes from propagating.
// Adopts a defensive exception handling strategy for the hijack trampoline (__try around the original call),
// but uses VEH instead of SEH due to llvm-mingw i686 limitation: SEH code generation fails
// when __try blocks contain function calls (only inline memory access is supported).
static thread_local jmp_buf gHookInstallJmpBuf;
static thread_local bool gHookInstallGuardActive = false;
static thread_local DWORD gHookInstallExceptionCode = 0;

LONG WINAPI HookInstallExceptionHandler(EXCEPTION_POINTERS *exceptionInfo) {
    if (gHookInstallGuardActive && exceptionInfo && exceptionInfo->ExceptionRecord) {
        DWORD code = exceptionInfo->ExceptionRecord->ExceptionCode;
        gHookInstallExceptionCode = code;
        if (code == STATUS_GUARD_PAGE_VIOLATION && exceptionInfo->ExceptionRecord->NumberParameters >= 2) {
            void *addr = reinterpret_cast<void *>(exceptionInfo->ExceptionRecord->ExceptionInformation[1]);
            MEMORY_BASIC_INFORMATION mbi = {};
            if (VirtualQuery(addr, &mbi, sizeof(mbi)) && mbi.Protect != 0) {
                DWORD oldProtect = 0;
                VirtualProtect(mbi.BaseAddress, mbi.RegionSize, mbi.Protect | PAGE_GUARD, &oldProtect);
            }
        }
        longjmp(gHookInstallJmpBuf, 1);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

void InstallInternalTextHooks() {
    if (!gInternalTextHooks) return;
    bool ok = false;
    std::wstring captureMode = CaptureHookMode();
    if (CaptureHookSelected(captureMode, L"psb")) {
        ok = InstallPsbPostConversionHook() || ok;
    }
    if (CaptureHookSelected(captureMode, L"textrender")) {
        ok = InstallTextRenderInternalHook() || ok;
    }
    if (CaptureHookSelected(captureMode, L"kag")) {
        ok = InstallKagParserEntryHook(L"KAGParser.dll", &gKagParserEntryHookTarget) || ok;
        ok = InstallKagParserEntryHook(L"ExtKAGParser.dll", &gExtKagParserEntryHookTarget) || ok;
        ok = InstallKagParserInternalHook(L"KAGParser.dll", false) || ok;
        ok = InstallKagParserInternalHook(L"ExtKAGParser.dll", true) || ok;
        ok = InstallKagParserInternalHook(L"KAGParserEx.dll", true) || ok;
    }
    if (!captureMode.empty()) {
        static bool captureLogged = false;
        if (!captureLogged) {
            captureLogged = true;
            Log(L"[kirikiri native] capture-only hooks enabled: " + captureMode);
        }
        // 短路优先级策略：Z 系列 capture hook 按优先级依次尝试，装成功一个就跳过剩余的
        // 短路逻辑：本引擎的 KiriKiriZ 系列 hook 互斥，命中一个即停止后续安装
        bool zHookInstalled = false;
        if (CaptureHookSelected(captureMode, L"zx")) {
            if (InstallKiriKiriZXInternalHook()) {
                zHookInstalled = true;
                ok = true;
            }
        }
        if (!zHookInstalled && CaptureHookSelected(captureMode, L"embed")) {
            if (InstallEmbedKrkrZUtf8Hooks()) {
                zHookInstalled = true;
                ok = true;
            }
        }
        if (!zHookInstalled && (CaptureHookSelected(captureMode, L"z2") ||
                                     CaptureHookSelected(captureMode, L"kr2"))) {
            if (InstallKiriKiriZ2IndirectHook()) {
                zHookInstalled = true;
                ok = true;
            }
        }
        if (!zHookInstalled && CaptureHookSelected(captureMode, L"kr2")) {
            if (InstallEmbedKrkr2WideHook()) {
                zHookInstalled = true;
                ok = true;
            }
        }
    } else {
        static bool captureDisabledLogged = false;
        if (!captureDisabledLogged) {
            captureDisabledLogged = true;
            Log(L"[kirikiri native] capture-only hooks disabled by KIRIKIRI_CAPTURE_HOOKS");
        }
    }
    if (ExperimentalTextHooksEnabled()) {
        ok = InstallKiriKiriZ3InternalHook() || ok;
        ok = InstallKrkrZ2InternalHook() || ok;
        ok = InstallKrkr2WcsHook() || ok;
    } else {
        static bool logged = false;
        if (!logged) {
            logged = true;
            Log(L"[kirikiri native] experimental inline text hooks disabled by default");
            ReportExperimentalTextHookCandidates();
        }
    }
    if (!ok && gInternalHookInstallCount == 0) {
        Log(L"[kirikiri native] internal text hook signatures not found yet");
    }
}

bool InstallCreateStreamHookAt(void *target, size_t patchSize, const wchar_t *sourceLabel) {
    std::lock_guard<std::mutex> lock(gHookMutex);
    if (gCreateStreamHooked) return true;
    if (!target) return false;
    if (!InstallInlineJump(target, patchSize)) {
        Log(std::wstring(L"[kirikiri native] TVPCreateStream inline hook failed: ") + sourceLabel);
        return false;
    }
    const wchar_t *label = sourceLabel ? sourceLabel : L"unknown";
    if (std::wstring(label) == L"export") {
        Log(L"[kirikiri native] TVPCreateStream hook installed: export");
    } else {
        Log(std::wstring(L"[kirikiri native] TVPCreateStream hook installed: ") + label);
    }
    StartDumpTargetsThreadOnce();
    return true;
}

bool InstallCreateStreamHook() {
    std::lock_guard<std::mutex> lock(gHookMutex);
    if (gCreateStreamHooked) return true;
    HMODULE exe = GetModuleHandleW(nullptr);
    BYTE *base = nullptr;
    size_t size = 0;
    if (!ModuleTextRange(exe, &base, &size)) {
        Log(L"[kirikiri native] failed to inspect executable image");
        return false;
    }

    // KiriKiriZ/tvpwin32 x86 TVPCreateStream prologue observed in common builds.
    // The long signature avoids matching nearby helper functions with the same short SEH prologue.
    const BYTE pattern[] = {
        0x55, 0x8B, 0xEC, 0x6A, 0xFF, 0x68, 0, 0, 0, 0, 0x64, 0xA1,
        0, 0, 0, 0, 0x50, 0x83, 0xEC, 0x5C, 0x53, 0x56, 0x57, 0xA1,
        0, 0, 0, 0, 0x33, 0xC5, 0x50, 0x8D, 0x45, 0xF4, 0x64, 0xA3,
        0, 0, 0, 0, 0x89, 0x65, 0xF0, 0x89, 0x4D, 0xEC, 0xC7, 0x45,
        0, 0, 0, 0, 0, 0xE8, 0, 0, 0, 0, 0x8B, 0x4D, 0xF4, 0x64,
        0x89, 0x0D, 0, 0, 0, 0, 0x59, 0x5F, 0x5E, 0x5B, 0x8B, 0xE5,
        0x5D, 0xC3
    };
    const char *mask =
        "xxxxxx????xx????xxxxxxxx????xxxxxxxx????xxxxxxxx?????x????xxxxxx????xxxxxxxx";
    void *target = nullptr;
    if (!SearchPattern(base, size, pattern, mask, &target)) {
        Log(L"[kirikiri native] TVPCreateStream signature not found");
        return false;
    }
    if (!InstallInlineJump(target, strlen(mask))) {
        Log(L"[kirikiri native] TVPCreateStream inline hook failed: signature");
        return false;
    }
    Log(L"[kirikiri native] TVPCreateStream hook installed: signature");
    StartDumpTargetsThreadOnce();
    return true;
}

bool InstallCreateStreamHookFromExports(iTVPFunctionExporter *exporter) {
    if (!exporter || gCreateStreamHooked) return false;
    const char *names[] = {
        "tTJSBinaryStream * ::TVPCreateStream(const ttstr &, tjs_uint32)",
        "tTJSBinaryStream * ::TVPCreateStream(const ttstr &, tjs_uint)",
        "tTJSBinaryStream * ::TVPCreateStream(const tTJSString &, tjs_uint32)",
        "tTJSBinaryStream * ::TVPCreateStream(const tTJSString &, tjs_uint)",
        "class tTJSBinaryStream * ::TVPCreateStream(const class ttstr &, unsigned int)",
        "TVPCreateStream",
    };
    for (const char *name : names) {
        void *fn = nullptr;
        if (!QueryFunction(exporter, name, &fn) || !fn) continue;
        Log(L"[kirikiri native] imported TVPCreateStream via export: " + Utf8ToWide(name));
        if (InstallCreateStreamHookAt(fn, 5, L"export")) return true;
    }
    Log(L"[kirikiri native] TVPCreateStream export not found");
    return false;
}

bool InstallStorageMediaHooksFromExports(iTVPFunctionExporter *exporter) {
    if (!exporter) return false;
    bool ok = false;
    void *fn = nullptr;
    if (QueryFunction(exporter, "void ::TVPRegisterStorageMedia(iTVPStorageMedia *)", &fn) && fn) {
        OriginalTVPRegisterStorageMedia = reinterpret_cast<TVPRegisterStorageMedia_t>(fn);
        Log(L"[kirikiri native] imported TVPRegisterStorageMedia");
        ok = true;
    } else {
        Log(L"[kirikiri native] TVPRegisterStorageMedia export not found");
    }
    fn = nullptr;
    if (QueryFunction(exporter, "void ::TVPUnregisterStorageMedia(iTVPStorageMedia *)", &fn) && fn) {
        OriginalTVPUnregisterStorageMedia = reinterpret_cast<TVPUnregisterStorageMedia_t>(fn);
        Log(L"[kirikiri native] imported TVPUnregisterStorageMedia");
    } else {
        Log(L"[kirikiri native] TVPUnregisterStorageMedia export not found");
    }
    return ok;
}

void __stdcall HookTVPRegisterStorageMedia(iTVPStorageMedia *media) {
    if (media) {
        tTJSString mediaName = {};
        std::wstring name;
        media->GetName(mediaName);
        TryReadTjsString(mediaName, &name);
        if (HookStorageMediaVtable(media)) {
            LONG count = InterlockedIncrement(&gStorageMediaHookCount);
            Log(L"[kirikiri native] hooked storage media " +
                (name.empty() ? L"<unknown>" : name) +
                L" (#" + std::to_wstring(count) + L")");
        } else {
            Log(L"[kirikiri native] failed to hook storage media " + (name.empty() ? L"<unknown>" : name));
        }
    }
    if (OriginalTVPRegisterStorageMedia) OriginalTVPRegisterStorageMedia(media);
}

void __stdcall HookTVPUnregisterStorageMedia(iTVPStorageMedia *media) {
    UnhookStorageMediaVtable(media);
    if (OriginalTVPUnregisterStorageMedia) OriginalTVPUnregisterStorageMedia(media);
}

HRESULT __stdcall HookV2Link(iTVPFunctionExporter *exporter) {
    iTVPFunctionExporter *linkExporter = exporter;
    if (exporter && gLowLevelStreamHooks) {
        linkExporter = new ProxyFunctionExporter(exporter);
    }
    HRESULT result = RealV2Link ? RealV2Link(linkExporter) : 0;
    void *fn = nullptr;
    if (QueryFunction(exporter, "bool ::TVPIsExistentStorageNoSearchNoNormalize(const ttstr &)", &fn)) {
        TVPIsExistentStorageNoSearchNoNormalize = reinterpret_cast<TVPIsExistentStorage_t>(fn);
        Log(L"[kirikiri native] imported TVPIsExistentStorageNoSearchNoNormalize");
    }
    fn = nullptr;
    if (QueryFunction(exporter, "ttstr ::TVPGetAppPath()", &fn)) {
        TVPGetAppPath = reinterpret_cast<TVPGetAppPath_t>(fn);
        Log(L"[kirikiri native] imported TVPGetAppPath");
    }
    fn = nullptr;
    if (QueryFunction(exporter, "void ::TVPAddAutoPath(const ttstr &)", &fn)) {
        TVPAddAutoPath = reinterpret_cast<TVPAddAutoPath_t>(fn);
        Log(L"[kirikiri native] imported TVPAddAutoPath");
    }
    fn = nullptr;
    if (QueryFunction(exporter, "void ::TVPClearStorageCaches()", &fn)) {
        TVPClearStorageCaches = reinterpret_cast<TVPClearStorageCaches_t>(fn);
        Log(L"[kirikiri native] imported TVPClearStorageCaches");
    }
    if (gLowLevelStreamHooks) {
        fn = nullptr;
        if (QueryFunction(exporter, "IStream * ::TVPCreateIStream(const ttstr &,tjs_uint32)", &fn)) {
            if (!OriginalTVPCreateIStream) OriginalTVPCreateIStream = reinterpret_cast<TVPCreateIStream_t>(fn);
            TVPCreateIStream = reinterpret_cast<TVPCreateIStream_t>(fn);
            Log(L"[kirikiri native] imported TVPCreateIStream");
            InstallIStreamHookAt(fn);
        }
        fn = nullptr;
        if (QueryFunction(exporter, "tTJSBinaryStream * ::TVPCreateBinaryStreamAdapter(IStream *)", &fn)) {
            TVPCreateBinaryStreamAdapter = reinterpret_cast<TVPCreateBinaryStreamAdapter_t>(fn);
            Log(L"[kirikiri native] imported TVPCreateBinaryStreamAdapter");
        }
        fn = nullptr;
        if (QueryFunction(exporter, "tTJSVariantString * ::TJSAllocVariantString(const tjs_char *)", &fn)) {
            TJSAllocVariantString = reinterpret_cast<TJSAllocVariantString_t>(fn);
            Log(L"[kirikiri native] imported TJSAllocVariantString");
        }
        fn = nullptr;
        if (QueryFunction(exporter, "void tTJSVariantString::Release()", &fn)) {
            TJSVariantStringRelease = reinterpret_cast<TJSVariantStringRelease_t>(fn);
            Log(L"[kirikiri native] imported tTJSVariantString::Release");
        }
        if (!InstallCreateStreamHookFromExports(exporter)) {
            InstallCreateStreamHook();
        }
        InstallStorageMediaHooksFromExports(exporter);
    } else {
        Log(L"[kirikiri native] TVPCreateIStream query skipped in mtool profile");
        Log(L"[kirikiri native] storage/stream inline hooks skipped in mtool profile");
    }
    if (!gPatchArchives.empty()) {
        InstallPatchAutoPaths();
    } else {
        Log(L"[kirikiri native] patch auto path skipped: no patch archives");
    }
    return result;
}

bool PatchOneImport(HMODULE module, const char *dllName, const char *funcName, void *replacement, void **original) {
    auto *base = reinterpret_cast<unsigned char *>(module);
    auto *dos = reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return false;
    auto *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return false;
    auto &dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress) return false;
    auto *imports = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>(base + dir.VirtualAddress);
    bool patched = false;
    for (; imports->Name; ++imports) {
        const char *importDll = reinterpret_cast<const char *>(base + imports->Name);
        if (_stricmp(importDll, dllName) != 0) continue;
        auto *origThunk = imports->OriginalFirstThunk
                              ? reinterpret_cast<IMAGE_THUNK_DATA *>(base + imports->OriginalFirstThunk)
                              : nullptr;
        auto *firstThunk = reinterpret_cast<IMAGE_THUNK_DATA *>(base + imports->FirstThunk);
        if (!origThunk) continue;
        for (; origThunk->u1.AddressOfData; ++origThunk, ++firstThunk) {
            if (IMAGE_SNAP_BY_ORDINAL(origThunk->u1.Ordinal)) continue;
            auto *byName = reinterpret_cast<IMAGE_IMPORT_BY_NAME *>(base + origThunk->u1.AddressOfData);
            if (strcmp(reinterpret_cast<const char *>(byName->Name), funcName) != 0) continue;
            DWORD oldProtect = 0;
            if (VirtualProtect(&firstThunk->u1.Function, sizeof(void *), PAGE_READWRITE, &oldProtect)) {
                if (original && !*original) *original = reinterpret_cast<void *>(firstThunk->u1.Function);
                firstThunk->u1.Function = reinterpret_cast<ULONG_PTR>(replacement);
                VirtualProtect(&firstThunk->u1.Function, sizeof(void *), oldProtect, &oldProtect);
                FlushInstructionCache(GetCurrentProcess(), &firstThunk->u1.Function, sizeof(void *));
                patched = true;
            }
        }
    }
    return patched;
}

void PatchImportsForDlls(const char *funcName, void *replacement, std::initializer_list<const char *> dlls) {
    HMODULE module = GetModuleHandleW(nullptr);
    for (const char *dll : dlls) {
        if (PatchOneImport(module, dll, funcName, replacement, nullptr)) {
            Log(L"[kirikiri native] hooked " + Utf8ToWide(dll) + L"!" + Utf8ToWide(funcName));
        }
    }
}

bool ShouldPatchModule(HMODULE module) {
    if (!module) return false;
    wchar_t path[MAX_PATH * 4] = {};
    DWORD n = GetModuleFileNameW(module, path, static_cast<DWORD>(sizeof(path) / sizeof(path[0])));
    if (!n) return true;
    std::wstring lowered(path, n);
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), towlower);
    if (lowered.find(L"\\windows\\system32\\") != std::wstring::npos) return false;
    if (lowered.find(L"\\windows\\syswow64\\") != std::wstring::npos) return false;
    if (lowered.find(L"\\_translation_meta\\kirikiri_native_hook.dll") != std::wstring::npos) return false;
    return true;
}

void PatchImportsInLoadedModules(const char *funcName, void *replacement, std::initializer_list<const char *> dlls) {
    std::lock_guard<std::mutex> lock(gModulePatchMutex);
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) return;
    MODULEENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    if (!Module32FirstW(snapshot, &entry)) {
        CloseHandle(snapshot);
        return;
    }
    int patchedModules = 0;
    do {
        HMODULE module = entry.hModule;
        if (!ShouldPatchModule(module)) continue;
        bool patched = false;
        for (const char *dll : dlls) {
            if (PatchOneImport(module, dll, funcName, replacement, nullptr)) patched = true;
        }
        if (patched) ++patchedModules;
    } while (Module32NextW(snapshot, &entry));
    CloseHandle(snapshot);
    if (patchedModules > 0) {
        InterlockedAdd(&gModulePatchCount, patchedModules);
        Log(L"[kirikiri native] hooked loaded modules " + Utf8ToWide(funcName) + L": " + std::to_wstring(patchedModules));
    }
}

void LoadRealFunctions() {
    HMODULE gdi = LoadLibraryW(L"gdi32.dll");
    HMODULE user = LoadLibraryW(L"user32.dll");
    HMODULE kernel = LoadLibraryW(L"kernel32.dll");
    HMODULE gdiplus = LoadLibraryW(L"gdiplus.dll");
    RealTextOutA = reinterpret_cast<TextOutA_t>(GetProcAddress(gdi, "TextOutA"));
    RealTextOutW = reinterpret_cast<TextOutW_t>(GetProcAddress(gdi, "TextOutW"));
    RealExtTextOutA = reinterpret_cast<ExtTextOutA_t>(GetProcAddress(gdi, "ExtTextOutA"));
    RealExtTextOutW = reinterpret_cast<ExtTextOutW_t>(GetProcAddress(gdi, "ExtTextOutW"));
    RealCreateFontA = reinterpret_cast<CreateFontA_t>(GetProcAddress(gdi, "CreateFontA"));
    RealCreateFontW = reinterpret_cast<CreateFontW_t>(GetProcAddress(gdi, "CreateFontW"));
    RealCreateFontIndirectA = reinterpret_cast<CreateFontIndirectA_t>(GetProcAddress(gdi, "CreateFontIndirectA"));
    RealCreateFontIndirectW = reinterpret_cast<CreateFontIndirectW_t>(GetProcAddress(gdi, "CreateFontIndirectW"));
    RealDrawTextA = reinterpret_cast<DrawTextA_t>(GetProcAddress(user, "DrawTextA"));
    RealDrawTextW = reinterpret_cast<DrawTextW_t>(GetProcAddress(user, "DrawTextW"));
    RealMultiByteToWideChar = reinterpret_cast<MultiByteToWideChar_t>(GetProcAddress(kernel, "MultiByteToWideChar"));
    RealWideCharToMultiByte = reinterpret_cast<WideCharToMultiByte_t>(GetProcAddress(kernel, "WideCharToMultiByte"));
    if (gdiplus) {
        RealGdipDrawString = reinterpret_cast<GdipDrawString_t>(GetProcAddress(gdiplus, "GdipDrawString"));
        RealGdipMeasureString = reinterpret_cast<GdipMeasureString_t>(GetProcAddress(gdiplus, "GdipMeasureString"));
        RealGdipAddPathString = reinterpret_cast<GdipAddPathString_t>(GetProcAddress(gdiplus, "GdipAddPathString"));
        RealGdipAddPathStringI = reinterpret_cast<GdipAddPathString_t>(GetProcAddress(gdiplus, "GdipAddPathStringI"));
        RealGdipCreateFontFamilyFromName = reinterpret_cast<GdipCreateFontFamilyFromName_t>(GetProcAddress(gdiplus, "GdipCreateFontFamilyFromName"));
    }
}

void PatchFontImports() {
    PatchImportsForDlls("TextOutA", reinterpret_cast<void *>(HookTextOutA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("TextOutW", reinterpret_cast<void *>(HookTextOutW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("ExtTextOutA", reinterpret_cast<void *>(HookExtTextOutA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("ExtTextOutW", reinterpret_cast<void *>(HookExtTextOutW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("CreateFontA", reinterpret_cast<void *>(HookCreateFontA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("CreateFontW", reinterpret_cast<void *>(HookCreateFontW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("CreateFontIndirectA", reinterpret_cast<void *>(HookCreateFontIndirectA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("CreateFontIndirectW", reinterpret_cast<void *>(HookCreateFontIndirectW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsForDlls("DrawTextA", reinterpret_cast<void *>(HookDrawTextA), {"user32.dll"});
    PatchImportsForDlls("DrawTextW", reinterpret_cast<void *>(HookDrawTextW), {"user32.dll"});
    PatchImportsForDlls("MultiByteToWideChar", reinterpret_cast<void *>(HookMultiByteToWideChar), {"kernel32.dll", "KernelBase.dll"});
    PatchImportsForDlls("WideCharToMultiByte", reinterpret_cast<void *>(HookWideCharToMultiByte), {"kernel32.dll", "KernelBase.dll"});
    PatchImportsForDlls("GdipDrawString", reinterpret_cast<void *>(HookGdipDrawString), {"gdiplus.dll"});
    PatchImportsForDlls("GdipMeasureString", reinterpret_cast<void *>(HookGdipMeasureString), {"gdiplus.dll"});
    PatchImportsForDlls("GdipAddPathString", reinterpret_cast<void *>(HookGdipAddPathString), {"gdiplus.dll"});
    PatchImportsForDlls("GdipAddPathStringI", reinterpret_cast<void *>(HookGdipAddPathStringI), {"gdiplus.dll"});
    PatchImportsForDlls("GdipCreateFontFamilyFromName", reinterpret_cast<void *>(HookGdipCreateFontFamilyFromName), {"gdiplus.dll"});

    PatchImportsInLoadedModules("TextOutA", reinterpret_cast<void *>(HookTextOutA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("TextOutW", reinterpret_cast<void *>(HookTextOutW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("ExtTextOutA", reinterpret_cast<void *>(HookExtTextOutA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("ExtTextOutW", reinterpret_cast<void *>(HookExtTextOutW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("CreateFontA", reinterpret_cast<void *>(HookCreateFontA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("CreateFontW", reinterpret_cast<void *>(HookCreateFontW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("CreateFontIndirectA", reinterpret_cast<void *>(HookCreateFontIndirectA), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("CreateFontIndirectW", reinterpret_cast<void *>(HookCreateFontIndirectW), {"gdi32.dll", "gdi32full.dll"});
    PatchImportsInLoadedModules("DrawTextA", reinterpret_cast<void *>(HookDrawTextA), {"user32.dll"});
    PatchImportsInLoadedModules("DrawTextW", reinterpret_cast<void *>(HookDrawTextW), {"user32.dll"});
    PatchImportsInLoadedModules("MultiByteToWideChar", reinterpret_cast<void *>(HookMultiByteToWideChar), {"kernel32.dll", "KernelBase.dll"});
    PatchImportsInLoadedModules("WideCharToMultiByte", reinterpret_cast<void *>(HookWideCharToMultiByte), {"kernel32.dll", "KernelBase.dll"});
    PatchImportsInLoadedModules("GdipDrawString", reinterpret_cast<void *>(HookGdipDrawString), {"gdiplus.dll"});
    PatchImportsInLoadedModules("GdipMeasureString", reinterpret_cast<void *>(HookGdipMeasureString), {"gdiplus.dll"});
    PatchImportsInLoadedModules("GdipAddPathString", reinterpret_cast<void *>(HookGdipAddPathString), {"gdiplus.dll"});
    PatchImportsInLoadedModules("GdipAddPathStringI", reinterpret_cast<void *>(HookGdipAddPathStringI), {"gdiplus.dll"});
    PatchImportsInLoadedModules("GdipCreateFontFamilyFromName", reinterpret_cast<void *>(HookGdipCreateFontFamilyFromName), {"gdiplus.dll"});
}

FARPROC WINAPI HookGetProcAddress(HMODULE module, LPCSTR procName) {
    FARPROC result = RealGetProcAddress ? RealGetProcAddress(module, procName) : nullptr;
    if (!procName || !HIWORD(procName) || !result) return result;
    if (gV2LinkHooks && strcmp(procName, "V2Link") == 0) {
        RealV2Link = reinterpret_cast<V2Link_t>(result);
        Log(L"[kirikiri native] intercepted V2Link");
        return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookV2Link));
    }
    if (!gLegacySystemHooks) return result;
    if (strcmp(procName, "TextOutA") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookTextOutA));
    if (strcmp(procName, "TextOutW") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookTextOutW));
    if (strcmp(procName, "ExtTextOutA") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookExtTextOutA));
    if (strcmp(procName, "ExtTextOutW") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookExtTextOutW));
    if (strcmp(procName, "DrawTextA") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookDrawTextA));
    if (strcmp(procName, "DrawTextW") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookDrawTextW));
    if (strcmp(procName, "CreateFontA") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookCreateFontA));
    if (strcmp(procName, "CreateFontW") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookCreateFontW));
    if (strcmp(procName, "CreateFontIndirectA") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookCreateFontIndirectA));
    if (strcmp(procName, "CreateFontIndirectW") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookCreateFontIndirectW));
    if (strcmp(procName, "MultiByteToWideChar") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookMultiByteToWideChar));
    if (strcmp(procName, "WideCharToMultiByte") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookWideCharToMultiByte));
    if (strcmp(procName, "GdipDrawString") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookGdipDrawString));
    if (strcmp(procName, "GdipMeasureString") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookGdipMeasureString));
    if (strcmp(procName, "GdipAddPathString") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookGdipAddPathString));
    if (strcmp(procName, "GdipAddPathStringI") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookGdipAddPathStringI));
    if (strcmp(procName, "GdipCreateFontFamilyFromName") == 0) return reinterpret_cast<FARPROC>(reinterpret_cast<void *>(HookGdipCreateFontFamilyFromName));
    return result;
}

void LoadFontConfig() {
    wchar_t value[64] = {};
    DWORD n = GetEnvironmentVariableW(L"KIRIKIRI_FONT_HEIGHT_SCALE", value, static_cast<DWORD>(64));
    if (n > 0 && n < 64) {
        double parsed = wcstod(value, nullptr);
        if (parsed > 0.2 && parsed < 2.0) gFontHeightScale = parsed;
    }
    Log(L"[kirikiri native] font face=" + std::wstring(kChineseFontFaceW) +
        L", scale=" + std::to_wstring(gFontHeightScale));
}

void LoadHookProfile() {
    wchar_t value[64] = {};
    DWORD n = GetEnvironmentVariableW(L"KIRIKIRI_NATIVE_HOOK_PROFILE", value, static_cast<DWORD>(64));
    std::wstring profile = (n > 0 && n < 64) ? std::wstring(value, n) : L"mtool";
    std::transform(profile.begin(), profile.end(), profile.begin(), towlower);
    gLegacySystemHooks = (
        profile == L"legacy" ||
        profile == L"full" ||
        profile == L"gdi" ||
        profile == L"display"
    );
    gLowLevelStreamHooks = (
        profile == L"legacy" ||
        profile == L"full" ||
        profile == L"stream" ||
        profile == L"dump"
    );
    gDirectCreateStreamHook = (
        profile == L"patchstream" ||
        profile == L"mtool_stream"
    );
    gV2LinkHooks = (
        gLowLevelStreamHooks ||
        profile == L"autopath" ||
        profile == L"bridge"
    );
    gGetProcAddressHooks = (
        gV2LinkHooks ||
        profile == L"display" ||
        profile == L"gdi"
    );
    gPatchArchiveHooks = (
        gLowLevelStreamHooks ||
        gV2LinkHooks ||
        gDirectCreateStreamHook
    );
    gInternalTextHooks = (
        profile == L"mtool" ||
        profile == L"display" ||
        profile == L"full" ||
        profile == L"krkr" ||
        profile == L"internal"
    );
    gVmTextReplacement = EnvFlagEnabled(L"KIRIKIRI_ENABLE_VM_TEXT_REPLACE");
    gEmbedTextReplacement = EnvFlagEnabled(L"KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE");
    gEmbedTextWaitMs = EnvDword(L"KIRIKIRI_EMBED_WAIT_MS", 0, 60000);
    Log(L"[kirikiri native] hook profile=" + profile +
        (gLegacySystemHooks ? L" legacy_system_hooks=1" : L" legacy_system_hooks=0") +
        (gInternalTextHooks ? L" internal_text_hooks=1" : L" internal_text_hooks=0") +
        (gLowLevelStreamHooks ? L" low_level_stream_hooks=1" : L" low_level_stream_hooks=0") +
        (gV2LinkHooks ? L" v2link_hooks=1" : L" v2link_hooks=0") +
        (gGetProcAddressHooks ? L" getproc_hooks=1" : L" getproc_hooks=0") +
        (gDirectCreateStreamHook ? L" direct_create_stream=1" : L" direct_create_stream=0") +
        (gPatchArchiveHooks ? L" patch_archives=1" : L" patch_archives=0") +
        (gVmTextReplacement ? L" vm_text_replace=1" : L" vm_text_replace=0") +
        (gEmbedTextReplacement ? L" embed_text_replace=1" : L" embed_text_replace=0") +
        L" embed_wait_ms=" + std::to_wstring(gEmbedTextWaitMs));
}

void LoadPatchConfig() {
    gPatchArchives.clear();
    gPatchEntries.clear();
    gDumpTargets.clear();
    std::wstring meta = JoinPath(gGameDir, L"_translation_meta");
    gActiveExtensionlessDump = DumpCaptureEnabled() && FileExists(JoinPath(meta, L"kirikiri_active_extensionless_dump.txt"));
    std::wstring manifest = JoinPath(meta, L"kirikiri_patch_manifest.txt");
    if (gPatchArchiveHooks) {
        bool manifestLoaded = LoadPatchManifest(manifest);
        bool hasLoosePatch = DirectoryExists(JoinPath(meta, L"kirikiri_patch")) && manifestLoaded;
        if (hasLoosePatch) {
            gPatchArchives.push_back(L"_translation_meta/kirikiri_patch");
            Log(L"[kirikiri native] loose patch directory selected for filter-safe memory streams");
        } else if (FileExists(JoinPath(meta, L"kirikiri_patch.xp3")) && manifestLoaded) {
            gPatchArchives.push_back(L"_translation_meta/kirikiri_patch.xp3");
        }
        if (!hasLoosePatch && FileExists(JoinPath(gGameDir, L"patch.xp3")) && manifestLoaded) {
            gPatchArchives.push_back(L"patch.xp3");
        } else if (FileExists(JoinPath(gGameDir, L"patch.xp3"))) {
            Log(hasLoosePatch
                ? L"[kirikiri native] patch.xp3 kept as fallback; loose memory streams take priority"
                : L"[kirikiri native] patch.xp3 ignored because kirikiri_patch_manifest.txt is missing or empty");
        }
    } else {
        Log(L"[kirikiri native] patch archives skipped in display-only profile");
    }
    Log(L"[kirikiri native] patch archives: " + std::to_wstring(gPatchArchives.size()) +
        L", manifest entries: " + std::to_wstring(gPatchEntries.size()));
    if (DumpCaptureEnabled()) {
        EnsureDirectory(JoinPath(JoinPath(gGameDir, L"_translation_meta"), L"kirikiri_dump"));
        gDumpTargets = LoadNameListFile(JoinPath(meta, L"kirikiri_dump_targets.txt"));
        gPassiveDumpOnly = DumpTargetsContainOnlyExtensionlessEntries();
        if (!gDumpTargets.empty()) {
            Log(L"[kirikiri native] dump targets: " + std::to_wstring(gDumpTargets.size()) +
                (gPassiveDumpOnly ? L" passive" : L"") +
                (gActiveExtensionlessDump ? L" active-extensionless" : L""));
        }
    } else {
        gPassiveDumpOnly = false;
        Log(L"[kirikiri native] runtime dump disabled in display-only profile");
    }
}

void SafeInstallInternalTextHooks() {
    static PVOID vehHandle = nullptr;
    if (!vehHandle) {
        vehHandle = AddVectoredExceptionHandler(1, HookInstallExceptionHandler);
    }
    gHookInstallExceptionCode = 0;
    gHookInstallGuardActive = true;
    if (setjmp(gHookInstallJmpBuf) == 0) {
        InstallInternalTextHooks();
    } else {
        Log(L"[kirikiri native] exception during hook install: code=" +
            std::to_wstring(gHookInstallExceptionCode));
    }
    gHookInstallGuardActive = false;
}

void SignalLauncherReady() {
    std::wstring name = L"Local\\EngAixt_KiriKiriHookReady_" +
                        std::to_wstring(GetCurrentProcessId());
    HANDLE event = OpenEventW(EVENT_MODIFY_STATE, FALSE, name.c_str());
    if (!event) {
        Log(L"[kirikiri native] launcher ready event not found; continuing standalone");
        return;
    }
    if (SetEvent(event)) {
        Log(L"[kirikiri native] launcher ready event signaled");
    } else {
        Log(L"[kirikiri native] launcher ready event signal failed");
    }
    CloseHandle(event);
}

DWORD WINAPI InitThread(void *) {
    gGameDir = GetProcessDir();
    std::wstring metaDir = JoinPath(gGameDir, L"_translation_meta");
    EnsureDirectory(metaDir);
    gLogPath = JoinPath(metaDir, L"kirikiri_native_hook.log");
    gRuntimeCapturePath = JoinPath(metaDir, L"kirikiri_runtime_capture.jsonl");
    DeleteFileW(gLogPath.c_str());
    Log(L"[kirikiri native] init");
    Log(std::wstring(L"[kirikiri native] build=") + kHookBuildTag);
    Log(L"[kirikiri native] engine behavior reference: KiriKiri engine32 KAGParser/TextRender contracts");
    Log(L"[kirikiri native] engine layout reference=KRKRZ fd5c4ba tjs2/tjsVariantString.h");
    InitOverlaySharedMemory();
    if (gOverlaySharedMemoryView) {
        Log(L"[kirikiri native] overlay shared memory ready");
    }
    LoadHookProfile();
    LoadFontConfig();
    LoadRealFunctions();
    if (gLegacySystemHooks) {
        PatchFontImports();
    } else {
        Log(L"[kirikiri native] legacy GDI/encoding import hooks disabled");
    }
    LoadSjisTunnelTable();
    LoadTranslations();
    kirikiri_embed::Initialize(gGameDir, gEmbedTextReplacement, gEmbedTextWaitMs, &Log);
    LoadPlaceholderMap();
    LoadPatchConfig();
    SafeInstallInternalTextHooks();
    if (gDirectCreateStreamHook) {
        if (!InstallCreateStreamHook()) {
            Log(L"[kirikiri native] direct TVPCreateStream hook unavailable");
        }
    }
    if (gGetProcAddressHooks) {
        HMODULE exe = GetModuleHandleW(nullptr);
        void *original = nullptr;
        if (PatchOneImport(exe, "KERNEL32.dll", "GetProcAddress", reinterpret_cast<void *>(HookGetProcAddress), &original) ||
            PatchOneImport(exe, "kernel32.dll", "GetProcAddress", reinterpret_cast<void *>(HookGetProcAddress), &original)) {
            RealGetProcAddress = original ? reinterpret_cast<GetProcAddress_t>(original) : &GetProcAddress;
            Log(L"[kirikiri native] hooked kernel32!GetProcAddress");
        } else {
            RealGetProcAddress = GetProcAddress;
            Log(L"[kirikiri native] failed to hook GetProcAddress");
        }
    } else {
        RealGetProcAddress = GetProcAddress;
        Log(L"[kirikiri native] GetProcAddress hook skipped in mtool profile");
    }
    // The launcher resumes the suspended game as soon as this event is set.
    // Signal only after all startup-critical hooks have been installed.
    SignalLauncherReady();
    Log(L"[kirikiri native] ready, translations=" + std::to_wstring(gTranslations.size()) +
        L", placeholders=" + std::to_wstring(gPlaceholders.size()) +
        L", tunnel_chars=" + std::to_wstring(gSjisTunnelTable.size()) +
        L", font selects=" + std::to_wstring(gFontSelectCount) +
        (DumpCaptureEnabled()
            ? L", dump capture=enabled"
            : L", dump capture=disabled"));
    return 0;
}

DWORD WINAPI DumpFlushThread(void *) {
    while (true) {
        Sleep(3000);
        SafeInstallInternalTextHooks();
        if (gLegacySystemHooks) {
            PatchImportsInLoadedModules("MultiByteToWideChar", reinterpret_cast<void *>(HookMultiByteToWideChar), {"kernel32.dll", "KernelBase.dll"});
            PatchImportsInLoadedModules("WideCharToMultiByte", reinterpret_cast<void *>(HookWideCharToMultiByte), {"kernel32.dll", "KernelBase.dll"});
            PatchImportsInLoadedModules("TextOutA", reinterpret_cast<void *>(HookTextOutA), {"gdi32.dll", "gdi32full.dll"});
            PatchImportsInLoadedModules("ExtTextOutA", reinterpret_cast<void *>(HookExtTextOutA), {"gdi32.dll", "gdi32full.dll"});
            PatchImportsInLoadedModules("GdipDrawString", reinterpret_cast<void *>(HookGdipDrawString), {"gdiplus.dll"});
            PatchImportsInLoadedModules("GdipMeasureString", reinterpret_cast<void *>(HookGdipMeasureString), {"gdiplus.dll"});
            PatchImportsInLoadedModules("GdipAddPathString", reinterpret_cast<void *>(HookGdipAddPathString), {"gdiplus.dll"});
            PatchImportsInLoadedModules("GdipAddPathStringI", reinterpret_cast<void *>(HookGdipAddPathStringI), {"gdiplus.dll"});
            PatchImportsInLoadedModules("GdipCreateFontFamilyFromName", reinterpret_cast<void *>(HookGdipCreateFontFamilyFromName), {"gdiplus.dll"});
        }
        if (DumpCaptureEnabled()) {
            FlushAllDumpStreams();
        }
        kirikiri_embed::Stats embedStats = kirikiri_embed::GetStats();
        Log(L"[kirikiri native] render replaced=" + std::to_wstring(gReplacedCount) +
            L" missed=" + std::to_wstring(gMissedCount) +
            L" tunnel=" + std::to_wstring(gTunnelCount) +
            L" gdip_render=" + std::to_wstring(gGdipRenderReplaceCount) +
            L" gdip_measure=" + std::to_wstring(gGdipMeasureReplaceCount) +
            L" gdip_font=" + std::to_wstring(gGdipFontFamilyCount) +
            L" internal_hooks=" + std::to_wstring(gInternalHookInstallCount) +
            L" internal_replaced=" + std::to_wstring(gInternalReplaceCount) +
            L" internal_missed=" + std::to_wstring(gInternalMissCount) +
            L" kag_entry_hits=" + std::to_wstring(gInternalKagEntryHits) +
            L" kag_miss_samples=" + std::to_wstring(gInternalKagMissSamples) +
            L" kag_hits=" + std::to_wstring(gInternalKagHits) +
            L" textrender_hits=" + std::to_wstring(gTextRenderHits) +
            L" textrender_replaced=" + std::to_wstring(gTextRenderReplaceCount) +
            L" textrender_missed=" + std::to_wstring(gTextRenderMissCount) +
            L" psb_post_hits=" + std::to_wstring(gPsbPostHits) +
            L" psb_post_readonly=1" +
            L" krkrz3_hits=" + std::to_wstring(gInternalKirikiriZ3Hits) +
            L" krkrzx_hits=" + std::to_wstring(gInternalKirikiriZXHits) +
            L" krkrz2_hits=" + std::to_wstring(gInternalKrkrZ2Hits) +
            L" embed_krkrz_hits=" + std::to_wstring(gInternalEmbedKrkrZHits) +
            L" embed_converter_bypass=" + std::to_wstring(gEmbedKrkrZConverterBypassCount) +
            L" embed_cursor_replaced=" + std::to_wstring(gEmbedKrkrZCursorReplaceCount) +
            L" embed_replaced=" + std::to_wstring(embedStats.replacements) +
            L" embed_after_new=" + std::to_wstring(gEmbedKrkrZAfterNewCount) +
            L" embed_missed=" + std::to_wstring(embedStats.misses) +
            L" embed_wide_replaced=" + std::to_wstring(embedStats.wideReplacements) +
            L" embed_wide_missed=" + std::to_wstring(embedStats.wideMisses) +
            L" embed_map_entries=" + std::to_wstring(embedStats.mapEntries) +
            L" embed_map_reloads=" + std::to_wstring(embedStats.reloads) +
            L" embed_wait_hits=" + std::to_wstring(embedStats.waitHits) +
            L" embed_wait_timeouts=" + std::to_wstring(embedStats.waitTimeouts) +
            L" kirikiri_z2_hits=" + std::to_wstring(gInternalKiriKiriZ2Hits) +
            L" embed_krkr2_hits=" + std::to_wstring(gInternalEmbedKrkr2Hits) +
            L" embed_wide_slot_applied=" + std::to_wstring(gInternalEmbedKrkr2SlotApplied) +
            L" embed_wide_slot_write_fail=" + std::to_wstring(gInternalEmbedKrkr2SlotWriteFailures) +
            L" krkr2wcs_hits=" + std::to_wstring(gInternalKrkr2WcsHits) +
            L" overlay_miss=" + std::to_wstring(gRuntimeOverlayMissSamples) +
            L" text_captures=" + std::to_wstring(gRuntimeTextCaptureCount) +
            L" speaker_captures=" + std::to_wstring(gRuntimeSpeakerCaptureCount) +
            L" capture_duplicates=" + std::to_wstring(gRuntimeCaptureDuplicateSkips) +
            L" overlay_writes=" + std::to_wstring(gOverlayWriteCount) +
            L" font_selects=" + std::to_wstring(gFontSelectCount) +
            L" module_patches=" + std::to_wstring(gModulePatchCount) +
            L" sig_bypass=" + std::to_wstring(gSignatureBypassHits) +
            L" open_hits=" + std::to_wstring(gOpenHits) +
            L" open_misses=" + std::to_wstring(gOpenMisses) +
            L" istream=" + std::to_wstring(gIStreamSamples) +
            L" storage=" + std::to_wstring(gStorageOpenSamples) +
            L" create_stream=" + std::to_wstring(gCreateStreamReads));
    }
    return 0;
}

}  // namespace

extern "C" __declspec(dllexport) DWORD KiriKiriNativeHookVersion() {
    return 1;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        HANDLE thread = CreateThread(nullptr, 0, InitThread, nullptr, 0, nullptr);
        if (thread) CloseHandle(thread);
        HANDLE flushThread = CreateThread(nullptr, 0, DumpFlushThread, nullptr, 0, nullptr);
        if (flushThread) CloseHandle(flushThread);
    }
    return TRUE;
}
