#pragma once

#include <windows.h>

#include <string>

namespace kirikiri_embed {

using LogCallback = void (*)(const std::wstring &message);

struct Stats {
    long enabled = 0;
    long mapEntries = 0;
    long replacements = 0;
    long misses = 0;
    long wideReplacements = 0;
    long wideMisses = 0;
    long reloads = 0;
    long reloadFailures = 0;
    long waitHits = 0;
    long waitTimeouts = 0;
};

void Initialize(const std::wstring &gameDir, bool enabled, DWORD waitTimeoutMs, LogCallback logCallback);
bool PassesEmbedKrkrZFilter(const std::string &text);
std::string NormalizeEmbedKrkrZText(const std::string &text);
bool PassesEmbedKrkr2WideFilter(const std::wstring &text);
const char *ResolveUtf8(
    const char *original,
    const std::string &source,
    const std::string &visibleSource = std::string());
const wchar_t *ResolveWide(
    const wchar_t *original,
    const std::wstring &source,
    const std::wstring &visibleSource = std::wstring());
Stats GetStats();

}  // namespace kirikiri_embed
