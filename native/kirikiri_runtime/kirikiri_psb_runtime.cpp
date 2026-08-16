#include "kirikiri_psb_runtime.h"

#include <algorithm>
#include <cstdint>
#include <cstring>

namespace kirikiri_psb_runtime {
namespace {

bool ImageRange(HMODULE module, BYTE **base, size_t *size) {
    if (!module || !base || !size) return false;
    auto *mz = reinterpret_cast<IMAGE_DOS_HEADER *>(module);
    if (mz->e_magic != IMAGE_DOS_SIGNATURE) return false;
    auto *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(
        reinterpret_cast<BYTE *>(module) + mz->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return false;
    *base = reinterpret_cast<BYTE *>(module);
    *size = nt->OptionalHeader.SizeOfImage;
    return true;
}

bool TextRange(HMODULE module, BYTE **base, size_t *size) {
    auto *mz = reinterpret_cast<IMAGE_DOS_HEADER *>(module);
    if (!mz || mz->e_magic != IMAGE_DOS_SIGNATURE) return false;
    auto *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(
        reinterpret_cast<BYTE *>(module) + mz->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return false;
    auto *section = IMAGE_FIRST_SECTION(nt);
    for (WORD index = 0; index < nt->FileHeader.NumberOfSections; ++index, ++section) {
        if (memcmp(section->Name, ".text", 5) != 0) continue;
        *base = reinterpret_cast<BYTE *>(module) + section->VirtualAddress;
        *size = section->Misc.VirtualSize;
        return true;
    }
    return false;
}

}  // namespace

void *FindUtf8ToWidePostCall(HMODULE module) {
    BYTE *image = nullptr;
    size_t imageSize = 0;
    BYTE *text = nullptr;
    size_t textSize = 0;
    if (!ImageRange(module, &image, &imageSize) ||
        !TextRange(module, &text, &textSize)) {
        return nullptr;
    }

    const char marker[] = "tjs_int ::TVPUtf8ToWideCharString(const char *,tjs_char *)";
    BYTE *markerAddress = nullptr;
    for (size_t offset = 0; offset + sizeof(marker) <= imageSize; ++offset) {
        if (memcmp(image + offset, marker, sizeof(marker)) == 0) {
            markerAddress = image + offset;
            break;
        }
    }
    if (!markerAddress) return nullptr;

    uint32_t markerValue = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(markerAddress));
    const BYTE tail[] = {0x57, 0x53, 0xFF, 0xD0, 0x5F, 0x5B, 0x8B, 0xE5, 0x5D, 0xC3};
    for (size_t offset = 0; offset + 4 <= textSize; ++offset) {
        if (*reinterpret_cast<uint32_t *>(text + offset) != markerValue) continue;
        BYTE *searchBegin = text + offset;
        BYTE *searchEnd = std::min(text + textSize, searchBegin + 0x100);
        for (BYTE *cursor = searchBegin; cursor + sizeof(tail) <= searchEnd; ++cursor) {
            if (memcmp(cursor, tail, sizeof(tail)) == 0) {
                return cursor + 4;
            }
        }
    }
    return nullptr;
}

}  // namespace kirikiri_psb_runtime
