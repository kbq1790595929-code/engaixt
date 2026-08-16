#pragma once

#include <cstddef>
#include <string>

namespace kirikiri_patch_stream {

// Returns an MSVC x86 tTJSBinaryStream-compatible object owned by the engine.
void *CreateReadOnlyFileStream(const std::wstring &path);
void *CreateReadOnlyMemoryStream(const void *source, size_t size);

}  // namespace kirikiri_patch_stream
