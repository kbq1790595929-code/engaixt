#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <initializer_list>
#include <mutex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

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

std::unordered_map<std::wstring, std::wstring> gTranslations;
std::vector<wchar_t> gTunnelTable;
std::unordered_map<std::wstring, HFONT> gFontCache;
std::mutex gFontMutex;
std::wstring gGameDir;
std::wstring gLogPath;
double gFontHeightScale = 0.95;
volatile LONG gWideDrawDepth = 0;
volatile LONG gFontCreateDepth = 0;
volatile LONG gReplacedCount = 0;
volatile LONG gTunnelCount = 0;
volatile LONG gMissedCount = 0;

constexpr const wchar_t *kChineseFontFaceW = L"Microsoft YaHei UI";
constexpr const char *kChineseFontFaceA = "Microsoft YaHei UI";
constexpr BYTE kChineseCharset = GB2312_CHARSET;
constexpr BYTE kFontQuality = CLEARTYPE_QUALITY;

std::wstring DirName(const std::wstring &path) {
    size_t pos = path.find_last_of(L"\\/");
    if (pos == std::wstring::npos) return L".";
    return path.substr(0, pos);
}

std::wstring GetProcessDir() {
    wchar_t buf[MAX_PATH * 4] = {};
    DWORD n = GetModuleFileNameW(nullptr, buf, static_cast<DWORD>(MAX_PATH * 4));
    if (n == 0) return L".";
    return DirName(std::wstring(buf, n));
}

std::wstring JoinPath(const std::wstring &a, const std::wstring &b) {
    if (a.empty()) return b;
    if (a.back() == L'\\' || a.back() == L'/') return a + b;
    return a + L"\\" + b;
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

void Log(const std::wstring &message) {
    if (gLogPath.empty()) return;
    std::string line = WideToUtf8(message + L"\r\n");
    HANDLE h = CreateFileW(gLogPath.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(h, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
    CloseHandle(h);
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
        JoinPath(meta, L"bgi_native_map.tsv"),
        JoinPath(meta, L"bgi_runtime_map.tsv"),
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
        Log(L"[bgi native] translation TSV not found");
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
        if (line.empty() || line[0] == '#') continue;
        size_t tab = line.find('\t');
        if (tab == std::string::npos) continue;
        std::wstring src = Utf8ToWide(DecodeBase64(line.substr(0, tab)));
        std::wstring dst = Utf8ToWide(DecodeBase64(line.substr(tab + 1)));
        if (!src.empty() && !dst.empty() && src != dst) {
            gTranslations[src] = dst;
            ++count;
        }
        if (end == data.size()) break;
    }
    Log(L"[bgi native] loaded translations: " + std::to_wstring(count));
}

void LoadTunnelTable() {
    std::string bytes;
    if (!ReadBinary(JoinPath(gGameDir, L"sjis_ext.bin"), &bytes) || bytes.size() < 2) {
        Log(L"[bgi native] sjis_ext.bin not found");
        return;
    }
    for (size_t i = 0; i + 1 < bytes.size(); i += 2) {
        wchar_t ch = static_cast<wchar_t>(static_cast<unsigned char>(bytes[i]) |
                                          (static_cast<unsigned char>(bytes[i + 1]) << 8));
        gTunnelTable.push_back(ch);
    }
    Log(L"[bgi native] loaded sjis tunnel chars: " + std::to_wstring(gTunnelTable.size()));
}

bool IsSjisLead(unsigned char b) {
    return (b >= 0x81 && b < 0xA0) || (b >= 0xE0 && b < 0xFD);
}

int TunnelIndex(unsigned char high, unsigned char low) {
    if (high < 0xF0 || high > 0xFC) return -1;
    if (low < 0x40 || low > 0xFC || low == 0x7F) return -1;
    int lowIdx = low < 0x7F ? low - 0x40 : low - 0x41;
    return (high - 0xF0) * 188 + lowIdx;
}

std::wstring DecodeCp932Bytes(const std::vector<char> &bytes) {
    if (bytes.empty()) return {};
    MultiByteToWideChar_t fn = RealMultiByteToWideChar ? RealMultiByteToWideChar : MultiByteToWideChar;
    int chars = fn(932, 0, bytes.data(), static_cast<int>(bytes.size()), nullptr, 0);
    if (chars <= 0) return {};
    std::wstring out(chars, L'\0');
    fn(932, 0, bytes.data(), static_cast<int>(bytes.size()), out.data(), chars);
    return out;
}

bool DecodeCp932Tunnel(const char *ptr, int byteCount, std::wstring *out) {
    if (!ptr || gTunnelTable.empty()) return false;
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
            int idx = TunnelIndex(high, low);
            if (idx >= 0 && static_cast<size_t>(idx) < gTunnelTable.size()) {
                flushRaw();
                result.push_back(gTunnelTable[idx]);
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
    *out = result;
    return true;
}

std::wstring ReadAnsiText(const char *text, int count) {
    if (!text) return {};
    int len = count;
    if (len < 0) len = static_cast<int>(strlen(text));
    if (len <= 0 || len > 4096) return {};
    std::vector<char> bytes(text, text + len);
    return DecodeCp932Bytes(bytes);
}

std::wstring ReadWideText(const wchar_t *text, int count) {
    if (!text) return {};
    int len = count;
    if (len < 0) len = static_cast<int>(wcslen(text));
    if (len <= 0 || len > 4096) return {};
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

const std::wstring *FindTranslation(const std::wstring &text) {
    auto it = gTranslations.find(text);
    if (it == gTranslations.end()) return nullptr;
    return &it->second;
}

int ScaleFontHeight(int value) {
    if (value == 0) return value;
    int absValue = value < 0 ? -value : value;
    if (absValue < 10) return value;
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

bool ResolveAnsiText(const char *text, int count, std::wstring *resolved, bool *isTunnel) {
    if (isTunnel) *isTunnel = false;
    if (!text) return false;
    int len = count;
    if (len < 0) len = static_cast<int>(strlen(text));
    if (len <= 0 || len > 4096) return false;
    std::wstring tunneled;
    if (DecodeCp932Tunnel(text, len, &tunneled) && !tunneled.empty()) {
        *resolved = tunneled;
        if (isTunnel) *isTunnel = true;
        return true;
    }
    std::wstring src = ReadAnsiText(text, len);
    if (!NeedsTranslate(src)) return false;
    const std::wstring *translated = FindTranslation(src);
    if (!translated) {
        InterlockedIncrement(&gMissedCount);
        return false;
    }
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
    return RealTextOutA ? RealTextOutA(hdc, x, y, text, count) : FALSE;
}

BOOL WINAPI HookExtTextOutA(HDC hdc, int x, int y, UINT options, const RECT *rect, LPCSTR text, UINT count, const INT *dx) {
    std::wstring out;
    bool tunneled = false;
    if (ResolveAnsiText(text, static_cast<int>(count), &out, &tunneled)) {
        if (tunneled) InterlockedIncrement(&gTunnelCount);
        else InterlockedIncrement(&gReplacedCount);
        return DrawWideExtTextOut(hdc, x, y, options, rect, out, dx);
    }
    return RealExtTextOutA ? RealExtTextOutA(hdc, x, y, options, rect, text, count, dx) : FALSE;
}

int WINAPI HookDrawTextA(HDC hdc, LPCSTR text, int count, LPRECT rect, UINT format) {
    std::wstring out;
    bool tunneled = false;
    if (ResolveAnsiText(text, count, &out, &tunneled)) {
        if (tunneled) InterlockedIncrement(&gTunnelCount);
        else InterlockedIncrement(&gReplacedCount);
        return DrawWideDrawText(hdc, out, rect, format);
    }
    return RealDrawTextA ? RealDrawTextA(hdc, text, count, rect, format) : 0;
}

BOOL WINAPI HookTextOutW(HDC hdc, int x, int y, LPCWSTR text, int count) {
    if (gWideDrawDepth > 0) return RealTextOutW ? RealTextOutW(hdc, x, y, text, count) : FALSE;
    std::wstring src = ReadWideText(text, count);
    const std::wstring *translated = NeedsTranslate(src) ? FindTranslation(src) : nullptr;
    const std::wstring &out = translated ? *translated : src;
    if (translated) InterlockedIncrement(&gReplacedCount);
    HGDIOBJ oldFont = ContainsCjk(out) ? SelectChineseFont(hdc) : nullptr;
    BOOL ok = RealTextOutW ? RealTextOutW(hdc, x, y, out.c_str(), static_cast<int>(out.size())) : FALSE;
    RestoreFont(hdc, oldFont);
    return ok;
}

BOOL WINAPI HookExtTextOutW(HDC hdc, int x, int y, UINT options, const RECT *rect, LPCWSTR text, UINT count, const INT *dx) {
    if (gWideDrawDepth > 0) return RealExtTextOutW ? RealExtTextOutW(hdc, x, y, options, rect, text, count, dx) : FALSE;
    std::wstring src = ReadWideText(text, static_cast<int>(count));
    const std::wstring *translated = NeedsTranslate(src) ? FindTranslation(src) : nullptr;
    const std::wstring &out = translated ? *translated : src;
    if (translated) InterlockedIncrement(&gReplacedCount);
    HGDIOBJ oldFont = ContainsCjk(out) ? SelectChineseFont(hdc) : nullptr;
    BOOL ok = RealExtTextOutW ? RealExtTextOutW(hdc, x, y, options, rect, out.c_str(), static_cast<UINT>(out.size()), dx) : FALSE;
    RestoreFont(hdc, oldFont);
    return ok;
}

int WINAPI HookDrawTextW(HDC hdc, LPCWSTR text, int count, LPRECT rect, UINT format) {
    if (gWideDrawDepth > 0) return RealDrawTextW ? RealDrawTextW(hdc, text, count, rect, format) : 0;
    std::wstring src = ReadWideText(text, count);
    const std::wstring *translated = NeedsTranslate(src) ? FindTranslation(src) : nullptr;
    const std::wstring &out = translated ? *translated : src;
    if (translated) InterlockedIncrement(&gReplacedCount);
    HGDIOBJ oldFont = ContainsCjk(out) ? SelectChineseFont(hdc) : nullptr;
    int ok = RealDrawTextW ? RealDrawTextW(hdc, out.c_str(), static_cast<int>(out.size()), rect, format) : 0;
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
    int ret = RealMultiByteToWideChar ? RealMultiByteToWideChar(codePage, flags, mb, cb, wide, cch) : 0;
    if (!mb || !wide || cch <= 0) return ret;
    std::wstring tunneled;
    if (DecodeCp932Tunnel(mb, cb, &tunneled) && !tunneled.empty()) {
        int copy = std::min<int>(static_cast<int>(tunneled.size()), cch - 1);
        if (copy > 0) {
            memcpy(wide, tunneled.data(), copy * sizeof(wchar_t));
            wide[copy] = 0;
            InterlockedIncrement(&gTunnelCount);
            return copy;
        }
    }
    return ret;
}

bool PatchOneImport(HMODULE module, const char *dllName, const char *funcName, void *replacement) {
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
        if (PatchOneImport(module, dll, funcName, replacement)) {
            Log(L"[bgi native] hooked " + Utf8ToWide(dll) + L"!" + Utf8ToWide(funcName));
        }
    }
}

void LoadRealFunctions() {
    HMODULE gdi = LoadLibraryW(L"gdi32.dll");
    HMODULE user = LoadLibraryW(L"user32.dll");
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
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
}

void PatchImports() {
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
}

void LoadConfig() {
    wchar_t value[64] = {};
    DWORD n = GetEnvironmentVariableW(L"BGI_FONT_HEIGHT_SCALE", value, static_cast<DWORD>(64));
    if (n > 0 && n < 64) {
        double parsed = wcstod(value, nullptr);
        if (parsed > 0.2 && parsed < 2.0) gFontHeightScale = parsed;
    }
}

DWORD WINAPI InitThread(void *) {
    Sleep(50);
    gGameDir = GetProcessDir();
    gLogPath = JoinPath(JoinPath(gGameDir, L"_translation_meta"), L"bgi_native_hook.log");
    DeleteFileW(gLogPath.c_str());
    Log(L"[bgi native] init");
    LoadConfig();
    LoadRealFunctions();
    LoadTranslations();
    LoadTunnelTable();
    PatchImports();
    Log(L"[bgi native] ready");
    return 0;
}

}  // namespace

extern "C" __declspec(dllexport) DWORD BgiNativeHookVersion() {
    return 1;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        HANDLE thread = CreateThread(nullptr, 0, InitThread, nullptr, 0, nullptr);
        if (thread) CloseHandle(thread);
    }
    return TRUE;
}
