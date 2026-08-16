/*
 * EngAixt WOLF RPG structured-text bridge.
 *
 * Built against Sinflower/UberWolf's MIT-licensed WolfRPG parser. The bridge
 * deliberately exposes only JSON export, JSON patching, and validation; archive
 * encryption/decryption remains the responsibility of the official UberWolfCli.
 */

#include <windows.h>
#include <shellapi.h>

#include <filesystem>
#include <iostream>
#include <string>

#include <WolfRPG/WolfRPG.hpp>

namespace fs = std::filesystem;

namespace {

struct WideArgs {
    int count = 0;
    wchar_t** values = nullptr;

    WideArgs() : values(CommandLineToArgvW(GetCommandLineW(), &count)) {}
    ~WideArgs() {
        if (values != nullptr) {
            LocalFree(values);
        }
    }
};

void ensure_directory(const fs::path& path) {
    if (!fs::exists(path)) {
        fs::create_directories(path);
    }
}

int export_json(const fs::path& data_path, const fs::path& output_path) {
    WolfRPG game(data_path);
    if (!game.Valid()) {
        std::wcerr << L"UberWolf parser could not load data directory: " << data_path << std::endl;
        return 3;
    }

    const fs::path game_dir = output_path / L"game";
    const fs::path common_dir = output_path / L"common_events";
    const fs::path maps_dir = output_path / L"maps";
    const fs::path databases_dir = output_path / L"databases";
    ensure_directory(game_dir);
    ensure_directory(common_dir);
    ensure_directory(maps_dir);
    ensure_directory(databases_dir);

    game.GetGameDat().ToJson(game_dir);
    game.GetCommonEvents().ToJson(common_dir);
    for (const Map& map : game.GetMaps()) {
        map.ToJson(maps_dir);
    }
    for (const Database& database : game.GetDatabases()) {
        database.ToJson(databases_dir);
    }

    std::cout << "{\"status\":\"ok\",\"operation\":\"export\",";
    std::cout << "\"maps\":" << game.GetMaps().size() << ",";
    std::cout << "\"common_events\":" << game.GetCommonEvents().GetEvents().size() << ",";
    std::cout << "\"databases\":" << game.GetDatabases().size() << "}" << std::endl;
    return 0;
}

int apply_json(const fs::path& data_path, const fs::path& patch_path, const fs::path& output_path) {
    WolfRPG game(data_path);
    if (!game.Valid()) {
        std::wcerr << L"UberWolf parser could not load data directory: " << data_path << std::endl;
        return 3;
    }

    game.GetGameDat().Patch(patch_path / L"game");
    game.GetCommonEvents().Patch(patch_path / L"common_events");
    for (Map& map : game.GetMaps()) {
        map.Patch(patch_path / L"maps");
    }
    for (Database& database : game.GetDatabases()) {
        database.Patch(patch_path / L"databases");
    }

    ensure_directory(output_path);
    game.Save2File(output_path);
    std::cout << "{\"status\":\"ok\",\"operation\":\"apply\"}" << std::endl;
    return 0;
}

int verify_data(const fs::path& data_path) {
    WolfRPG game(data_path);
    if (!game.Valid()) {
        std::wcerr << L"UberWolf parser could not validate data directory: " << data_path << std::endl;
        return 3;
    }
    std::cout << "{\"status\":\"ok\",\"operation\":\"verify\"}" << std::endl;
    return 0;
}

void print_usage() {
    std::cerr << "Usage:\n"
              << "  wolf_text_bridge export <DataDir> <JsonDir>\n"
              << "  wolf_text_bridge apply <DataDir> <JsonDir> <OutputDataDir>\n"
              << "  wolf_text_bridge verify <DataDir>\n";
}

}  // namespace

int main() {
    WideArgs args;
    if (args.values == nullptr || args.count < 3) {
        print_usage();
        return 2;
    }

    try {
        const std::wstring operation = args.values[1];
        if (operation == L"export" && args.count == 4) {
            return export_json(args.values[2], args.values[3]);
        }
        if (operation == L"apply" && args.count == 5) {
            return apply_json(args.values[2], args.values[3], args.values[4]);
        }
        if (operation == L"verify" && args.count == 3) {
            return verify_data(args.values[2]);
        }
        print_usage();
        return 2;
    } catch (const std::exception& exc) {
        std::cerr << "wolf_text_bridge failed: " << exc.what() << std::endl;
        return 4;
    }
}
