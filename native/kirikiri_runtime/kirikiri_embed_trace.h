#pragma once

#include <cstdint>
#include <string>

namespace kirikiri_embed_trace {

bool ShouldLog(
    uint32_t sourceHash,
    uintptr_t hookRva,
    uintptr_t callerRva,
    uintptr_t parentCallerRva,
    uintptr_t grandparentCallerRva,
    bool hasDestination);

std::wstring FormatEvent(
    uint32_t sourceHash,
    uintptr_t hookRva,
    uintptr_t callerRva,
    uintptr_t parentCallerRva,
    uintptr_t grandparentCallerRva,
    uintptr_t destination,
    const std::wstring &sourcePreview);

}  // namespace kirikiri_embed_trace
