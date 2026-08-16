/*
 * EngAixt WOLF native bridge.
 *
 * This adapter links the pinned UberWolf source tree and exposes only the
 * archive operations required by the pipeline. It intentionally does not
 * capture keys or alter a running game process; callers supply a temporary
 * key file produced by a separate, audited acquisition path.
 */

#include <windows.h>

#include <cctype>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "UberWolfLib/Types.h"
#include "UberWolfLib/WolfDec.h"

namespace fs = std::filesystem;

namespace {

struct Options {
    std::wstring command;
    fs::path input;
    fs::path key_file;
    uint16_t crypt_version = 0;
};

constexpr int EXIT_USAGE = 2;
constexpr int EXIT_INPUT = 3;
constexpr int EXIT_KEY = 4;
constexpr int EXIT_OPERATION = 5;

void print_usage() {
    std::wcerr
        << L"Usage:\n"
        << L"  engaixt_wolf_native inspect <archive.wolf>\n"
        << L"  engaixt_wolf_native unpack --input <archive.wolf> --key-file <hex.txt> --crypt-version <n>\n"
        << L"  engaixt_wolf_native pack --input <Data-directory> --key-file <hex.txt> --crypt-version <n>\n";
}

bool parse_u16(const std::wstring& value, uint16_t* output) {
    if (value.empty()) {
        return false;
    }
    wchar_t* end = nullptr;
    const unsigned long parsed = wcstoul(value.c_str(), &end, 0);
    if (end == value.c_str() || *end != L'\0' || parsed > 0xffff) {
        return false;
    }
    *output = static_cast<uint16_t>(parsed);
    return true;
}

bool parse_options(int argc, wchar_t* argv[], Options* options) {
    if (argc < 3) {
        return false;
    }
    options->command = argv[1];
    if (options->command == L"inspect") {
        options->input = argv[2];
        return argc == 3;
    }
    if (options->command != L"unpack" && options->command != L"pack") {
        return false;
    }

    for (int index = 2; index < argc; index += 2) {
        if (index + 1 >= argc) {
            return false;
        }
        const std::wstring flag = argv[index];
        const std::wstring value = argv[index + 1];
        if (flag == L"--input") {
            options->input = value;
        } else if (flag == L"--key-file") {
            options->key_file = value;
        } else if (flag == L"--crypt-version") {
            if (!parse_u16(value, &options->crypt_version)) {
                return false;
            }
        } else {
            return false;
        }
    }
    return !options->input.empty() && !options->key_file.empty() && options->crypt_version != 0;
}

bool read_key(const fs::path& path, Key* key) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
        return false;
    }
    std::string input((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    std::string digits;
    digits.reserve(input.size());
    for (const unsigned char ch : input) {
        if (std::isxdigit(ch)) {
            digits.push_back(static_cast<char>(ch));
        } else if (!std::isspace(ch) && ch != ':' && ch != '-' && ch != ',') {
            return false;
        }
    }
    if (digits.empty() || digits.size() % 2 != 0) {
        return false;
    }

    key->clear();
    key->reserve(digits.size() / 2 + 1);
    for (size_t index = 0; index < digits.size(); index += 2) {
        const std::string byte = digits.substr(index, 2);
        key->push_back(static_cast<uint8_t>(std::stoul(byte, nullptr, 16)));
    }
    // DXArchive consumes the key as a C string. Upstream's JSON config path
    // adds this terminator as well, so keep direct native calls equivalent.
    if (key->empty() || key->back() != 0) {
        key->push_back(0);
    }
    return true;
}

bool read_crypt_version(const fs::path& archive, uint16_t* crypt_version) {
    std::ifstream stream(archive, std::ios::binary);
    unsigned char header[48]{};
    stream.read(reinterpret_cast<char*>(header), sizeof(header));
    if (stream.gcount() != static_cast<std::streamsize>(sizeof(header)) || header[0] != 'D' || header[1] != 'X') {
        return false;
    }
    *crypt_version = static_cast<uint16_t>(header[46] | (static_cast<uint16_t>(header[47]) << 8));
    return true;
}

int inspect(const Options& options) {
    uint16_t crypt_version = 0;
    if (!read_crypt_version(options.input, &crypt_version)) {
        std::wcerr << L"Invalid DXArchive: " << options.input.wstring() << L'\n';
        return EXIT_INPUT;
    }
    std::wcout
        << L"{\"format\":\"DXArchive\",\"crypt_version\":" << crypt_version
        << L",\"protection\":\"" << (crypt_version >= 1000 ? L"pro" : L"standard") << L"\"}" << std::endl;
    return 0;
}

int operate(const Options& options) {
    if (options.command == L"unpack" && !fs::is_regular_file(options.input)) {
        std::wcerr << L"Archive is missing: " << options.input.wstring() << L'\n';
        return EXIT_INPUT;
    }
    if (options.command == L"pack" && !fs::is_directory(options.input)) {
        std::wcerr << L"Data directory is missing: " << options.input.wstring() << L'\n';
        return EXIT_INPUT;
    }

    Key key;
    if (!read_key(options.key_file, &key)) {
        std::wcerr << L"Invalid key file: " << options.key_file.wstring() << L'\n';
        return EXIT_KEY;
    }

    // Direct mode keeps UberWolf's archive routine in this controlled process.
    // The normal CLI mode respawns its own executable, which is not valid for
    // an EngAixt adapter with a different command-line contract.
    WolfDec wolf(L"", 0, true);
    wolf.AddAndSetKey("EngAixt custom WOLF key", options.crypt_version, false, key);
    const bool success = options.command == L"unpack"
        ? wolf.UnpackArchive(options.input.wstring(), true)
        : wolf.PackArchive(options.input.wstring(), true);
    if (!success) {
        std::wcerr << L"WOLF " << options.command << L" failed" << std::endl;
        return EXIT_OPERATION;
    }
    return 0;
}

}  // namespace

int wmain(int argc, wchar_t* argv[]) {
    Options options;
    if (!parse_options(argc, argv, &options)) {
        print_usage();
        return EXIT_USAGE;
    }
    if (options.command == L"inspect") {
        return inspect(options);
    }
    return operate(options);
}
