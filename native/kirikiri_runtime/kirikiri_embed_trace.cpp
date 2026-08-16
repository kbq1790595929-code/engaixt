#include "kirikiri_embed_trace.h"

#include <mutex>
#include <set>
#include <tuple>

namespace kirikiri_embed_trace {
namespace {

std::mutex gTraceMutex;
std::set<std::tuple<uint32_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t, bool>> gSeenEvents;

}  // namespace

bool ShouldLog(
    uint32_t sourceHash,
    uintptr_t hookRva,
    uintptr_t callerRva,
    uintptr_t parentCallerRva,
    uintptr_t grandparentCallerRva,
    bool hasDestination) {
    std::lock_guard<std::mutex> lock(gTraceMutex);
    return gSeenEvents.emplace(
        sourceHash,
        hookRva,
        callerRva,
        parentCallerRva,
        grandparentCallerRva,
        hasDestination).second;
}

std::wstring FormatEvent(
    uint32_t sourceHash,
    uintptr_t hookRva,
    uintptr_t callerRva,
    uintptr_t parentCallerRva,
    uintptr_t grandparentCallerRva,
    uintptr_t destination,
    const std::wstring &sourcePreview) {
    return
        L"[kirikiri native] embed caller trace source_hash=" + std::to_wstring(sourceHash) +
        L" hook_rva=" + std::to_wstring(hookRva) +
        L" caller_rva=" + std::to_wstring(callerRva) +
        L" parent_caller_rva=" + std::to_wstring(parentCallerRva) +
        L" grandparent_caller_rva=" + std::to_wstring(grandparentCallerRva) +
        L" destination_mode=" + (destination ? L"write" : L"measure") +
        L" destination_address=" + std::to_wstring(destination) +
        L" source=" + sourcePreview;
}

}  // namespace kirikiri_embed_trace
