#pragma once

#include <cwctype>
#include <string>

namespace kirikiri_text {

inline bool IsSpace(wchar_t ch) {
    return ch == 0x3000 || iswspace(ch);
}

inline bool IsPunctuationOnly(const std::wstring &text) {
    bool visible = false;
    const std::wstring symbols =
        L"\u3001\u3002\uff0c\uff0e\uff01\uff1f\u2026\u30fb\uff1a\uff1b"
        L"\u300c\u300d\u300e\u300f\uff08\uff09\uff3b\uff3d\u3010\u3011"
        L"\u300a\u300b\u3008\u3009\uff5e\u301c\u30fc\u2014\u2015"
        L"\u2661\u266a\u2606\u00b7";
    for (wchar_t ch : text) {
        if (IsSpace(ch)) continue;
        visible = true;
        if (!iswpunct(ch) && symbols.find(ch) == std::wstring::npos) return false;
    }
    return visible;
}

inline bool HasUtf8DialogueEvidence(
    const std::wstring &raw, const std::wstring &visible) {
    // Encoding converters also see object names. Bare identifiers need
    // confirmation from a text/render hook, regardless of their language.
    if (!raw.empty() && (IsSpace(raw.front()) ||
                        raw.find_first_of(L"\r\n") != std::wstring::npos)) {
        return true;
    }
    if (visible.find_first_of(
        L"\u3002\u3001\uff01\uff1f\u2026\u300c\u300d\u300e\u300f"
        L"\u2014\u2015!?") != std::wstring::npos) {
        return true;
    }
    std::wstring compact;
    for (wchar_t ch : visible) {
        if (IsSpace(ch)) continue;
        if (ch < 0x3040 || ch > 0x309F) return false;
        compact.push_back(ch);
    }
    return !compact.empty() && compact.size() <= 6;
}

}  // namespace kirikiri_text
