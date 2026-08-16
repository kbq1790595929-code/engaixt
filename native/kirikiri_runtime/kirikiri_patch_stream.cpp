#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "kirikiri_patch_stream.h"

#include <algorithm>
#include <cstdint>
#include <cstring>

namespace kirikiri_patch_stream {
namespace {

using tjs_uint = unsigned int;
using tjs_int = int;
using tjs_uint64 = unsigned long long;
using tjs_int64 = long long;

// KiriKiri's first five stream methods use cdecl with this on the stack. Its
// deleting destructor uses the MSVC x86 thiscall shape (ECX + one stack flag).
struct MemoryPatchStream {
    void **vtable;
    unsigned char *data;
    tjs_uint64 size;
    tjs_uint64 position;
};

tjs_uint64 __cdecl Seek(MemoryPatchStream *stream, tjs_int64 offset, tjs_int whence) {
    if (!stream) return 0;
    tjs_int64 base = 0;
    if (whence == 1) base = static_cast<tjs_int64>(stream->position);
    else if (whence == 2) base = static_cast<tjs_int64>(stream->size);
    tjs_int64 next = base + offset;
    if (next < 0) next = 0;
    if (static_cast<tjs_uint64>(next) > stream->size) next = static_cast<tjs_int64>(stream->size);
    stream->position = static_cast<tjs_uint64>(next);
    return stream->position;
}

tjs_uint __cdecl Read(MemoryPatchStream *stream, void *buffer, tjs_uint readSize) {
    if (!stream || !buffer || !readSize || stream->position >= stream->size) return 0;
    tjs_uint64 remain = stream->size - stream->position;
    tjs_uint actual = static_cast<tjs_uint>(std::min<tjs_uint64>(remain, readSize));
    memcpy(buffer, stream->data + static_cast<size_t>(stream->position), actual);
    stream->position += actual;
    return actual;
}

tjs_uint __cdecl Write(MemoryPatchStream *, const void *, tjs_uint) {
    return 0;
}

void __cdecl SetEndOfStorage(MemoryPatchStream *) {}

tjs_uint64 __cdecl GetSize(MemoryPatchStream *stream) {
    return stream ? stream->size : 0;
}

void *__fastcall DeletingDestructor(MemoryPatchStream *stream, void *, unsigned int flags) {
    if (!stream) return nullptr;
    if (stream->data) HeapFree(GetProcessHeap(), 0, stream->data);
    stream->data = nullptr;
    if (flags & 1u) {
        HeapFree(GetProcessHeap(), 0, stream);
        return nullptr;
    }
    return stream;
}

void *kVtable[] = {
    reinterpret_cast<void *>(Seek),
    reinterpret_cast<void *>(Read),
    reinterpret_cast<void *>(Write),
    reinterpret_cast<void *>(SetEndOfStorage),
    reinterpret_cast<void *>(GetSize),
    reinterpret_cast<void *>(DeletingDestructor),
};

}  // namespace

void *CreateReadOnlyMemoryStream(const void *source, size_t size) {
    if (size && !source) return nullptr;
    auto *stream = static_cast<MemoryPatchStream *>(
        HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(MemoryPatchStream)));
    if (!stream) return nullptr;
    stream->vtable = kVtable;
    stream->size = static_cast<tjs_uint64>(size);
    if (size) {
        stream->data = static_cast<unsigned char *>(HeapAlloc(GetProcessHeap(), 0, size));
        if (!stream->data) {
            HeapFree(GetProcessHeap(), 0, stream);
            return nullptr;
        }
        memcpy(stream->data, source, size);
    }
    return stream;
}

void *CreateReadOnlyFileStream(const std::wstring &path) {
    HANDLE file = CreateFileW(
        path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return nullptr;
    LARGE_INTEGER fileSize = {};
    constexpr uint64_t kMaximumPatchFileSize = 128ull * 1024ull * 1024ull;
    if (!GetFileSizeEx(file, &fileSize) || fileSize.QuadPart < 0 ||
        static_cast<uint64_t>(fileSize.QuadPart) > kMaximumPatchFileSize) {
        CloseHandle(file);
        return nullptr;
    }

    size_t size = static_cast<size_t>(fileSize.QuadPart);
    unsigned char *bytes = nullptr;
    if (size) {
        bytes = static_cast<unsigned char *>(HeapAlloc(GetProcessHeap(), 0, size));
        if (!bytes) {
            CloseHandle(file);
            return nullptr;
        }
        DWORD bytesRead = 0;
        bool readOk = ReadFile(file, bytes, static_cast<DWORD>(size), &bytesRead, nullptr) &&
                      bytesRead == size;
        if (!readOk) {
            CloseHandle(file);
            HeapFree(GetProcessHeap(), 0, bytes);
            return nullptr;
        }
    }
    CloseHandle(file);
    void *stream = CreateReadOnlyMemoryStream(bytes, size);
    if (bytes) HeapFree(GetProcessHeap(), 0, bytes);
    return stream;
}

}  // namespace kirikiri_patch_stream
