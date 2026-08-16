#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

namespace kirikiri_psb_runtime {

void *FindUtf8ToWidePostCall(HMODULE module);

}  // namespace kirikiri_psb_runtime
