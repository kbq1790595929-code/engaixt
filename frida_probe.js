/**
 * Frida 探针 — Godot 4.x AES-256 密钥捕获 (适配 Frida 17.x API)
 *
 * 多级梯队扫描策略：
 *   梯队 1: Hook Windows BCrypt API（bcrypt.dll）
 *   梯队 2: Hook mbedTLS 导出函数（全局搜索）
 *   梯队 3: Hook OpenSSL 导出函数（全局搜索）
 *   梯队 4: 扫描 EXE 模块内存中的高熵 32 字节序列
 *
 * Frida 17.x API 变化:
 *   - Module.findExportByName(module, name) → Process.getModuleByName(module).getExportByName(name)
 *   - Module.findExportByName(null, name)    → Module.findGlobalExportByName(name)
 *   - Process.enumerateModules()             → Process.enumerateModules() (返回 Module 对象)
 *
 * 通信：捕获到有效密钥后立即通过 send({type: 'KEY', value: hexString}) 回传 Python。
 */

'use strict';

var foundKeys = new Set();

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function hex(bytes, maxLen) {
    var len = Math.min(bytes.byteLength, maxLen || 64);
    var result = '';
    for (var i = 0; i < len; i++) {
        var b = bytes[i];
        if (b === undefined) break;
        var h = b.toString(16);
        result += h.length === 1 ? '0' + h : h;
    }
    return result;
}

function isValidKey(keyBytes) {
    if (!keyBytes || keyBytes.byteLength !== 32) return false;

    var keyHex = hex(keyBytes, 32);
    // 跳过已知测试向量
    var testVectors = [
        '000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f',
        'a0a1a2a3a4a5a6a7a8A9AAABACADAEAFB0B1B2B3B4B5B6B7B8B9BABBBCBDBEBF',
        '808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f',
    ];
    for (var i = 0; i < testVectors.length; i++) {
        if (keyHex.toLowerCase() === testVectors[i].toLowerCase()) return false;
    }

    // 熵检查：至少 10 个不同字节
    var unique = {};
    for (var j = 0; j < 32; j++) {
        unique[keyBytes[j]] = true;
    }
    var count = 0;
    for (var _ in unique) { count++; }
    return count >= 10;
}

function reportKey(keyBytes, source) {
    if (!isValidKey(keyBytes)) return;

    var keyHex = hex(keyBytes, 32);
    if (foundKeys.has(keyHex)) return;

    foundKeys.add(keyHex);
    // 零延迟 IPC：立即回传 Python
    send({type: 'KEY', value: keyHex, source: source});
    console.log('[KEY] AES-256 captured from: ' + source + ' -> ' + keyHex);
}

function tryReadKey(ptr, source) {
    try {
        var keyBytes = ptr.readByteArray(32);
        if (keyBytes) reportKey(keyBytes, source);
    } catch (e) {
        // ignore access violations
    }
}

// 安全获取模块导出（Frida 17.x API）
function tryGetExport(moduleName, exportName) {
    try {
        var mod = Process.getModuleByName(moduleName);
        return mod.getExportByName(exportName);
    } catch (e) {
        return null;
    }
}

// 安全获取全局导出（Frida 17.x API）
function tryGetGlobalExport(exportName) {
    try {
        return Module.findGlobalExportByName(exportName);
    } catch (e) {
        return null;
    }
}

// ---------------------------------------------------------------------------
// 梯队 1: Windows BCrypt API hooks
// ---------------------------------------------------------------------------

function hookBCrypt() {
    var BCryptGenerateSymmetricKey = tryGetExport('bcrypt.dll', 'BCryptGenerateSymmetricKey');
    var BCryptImportKey = tryGetExport('bcrypt.dll', 'BCryptImportKey');

    if (BCryptGenerateSymmetricKey) {
        Interceptor.attach(BCryptGenerateSymmetricKey, {
            onEnter: function (args) {
                var secretLen = args[5].toInt32();
                if (secretLen === 32) {
                    tryReadKey(args[4], 'BCryptGenerateSymmetricKey');
                }
            }
        });
        send({type: 'log', message: '梯队 1: BCryptGenerateSymmetricKey hooked'});
    }

    if (BCryptImportKey) {
        Interceptor.attach(BCryptImportKey, {
            onEnter: function (args) {
                var inputLen = args[7].toInt32();
                if (inputLen === 32) {
                    tryReadKey(args[6], 'BCryptImportKey');
                }
            }
        });
        send({type: 'log', message: '梯队 1: BCryptImportKey hooked'});
    }

    if (!BCryptGenerateSymmetricKey && !BCryptImportKey) {
        send({type: 'log', message: '梯队 1: BCrypt 不可用（无 bcrypt.dll 或函数未找到）'});
    }
}

// ---------------------------------------------------------------------------
// 梯队 2: mbedTLS 静态链接函数 hooks
// ---------------------------------------------------------------------------

function hookMbedTLS() {
    var funcs = [
        'mbedtls_aes_setkey_enc',
        'mbedtls_aes_setkey_dec',
        'mbedtls_aes_init',
    ];

    var found = false;
    funcs.forEach(function (name) {
        var addr = tryGetGlobalExport(name);
        if (addr) {
            found = true;
            send({type: 'log', message: '梯队 2: 发现导出 ' + name + ' @ ' + addr});

            if (name === 'mbedtls_aes_setkey_enc' || name === 'mbedtls_aes_setkey_dec') {
                Interceptor.attach(addr, {
                    onEnter: function (args) {
                        var keybits = args[2].toInt32();
                        if (keybits === 256) {
                            tryReadKey(args[1], name);
                        }
                    }
                });
            }
        }
    });

    if (!found) {
        send({type: 'log', message: '梯队 2: 无 mbedTLS 全局导出函数'});
    }
}

// ---------------------------------------------------------------------------
// 梯队 3: OpenSSL AES hooks
// ---------------------------------------------------------------------------

function hookOpenSSL() {
    var funcs = [
        'AES_set_encrypt_key',
        'AES_set_decrypt_key',
        'EVP_CipherInit_ex',
        'EVP_EncryptInit_ex',
    ];

    var found = false;
    funcs.forEach(function (name) {
        var addr = tryGetGlobalExport(name);
        if (addr) {
            found = true;
            send({type: 'log', message: '梯队 3: 发现导出 ' + name + ' @ ' + addr});

            if (name === 'AES_set_encrypt_key') {
                Interceptor.attach(addr, {
                    onEnter: function (args) {
                        var bits = args[1].toInt32();
                        if (bits === 256) {
                            tryReadKey(args[0], 'AES_set_encrypt_key');
                        }
                    }
                });
            }
        }
    });

    if (!found) {
        send({type: 'log', message: '梯队 3: 无 OpenSSL 全局导出函数'});
    }
}

// ---------------------------------------------------------------------------
// 梯队 4: EXE 模块内存高熵扫描
// ---------------------------------------------------------------------------

function scanModuleForKeys() {
    var modules = Process.enumerateModules();
    var exeModule = null;

    for (var i = 0; i < modules.length; i++) {
        var m = modules[i];
        if (m.name && m.name.toLowerCase().endsWith('.exe')) {
            exeModule = m;
            break;
        }
    }

    if (!exeModule) {
        send({type: 'log', message: '梯队 4: 未找到主 EXE 模块'});
        return;
    }

    send({type: 'log', message: '梯队 4: 扫描 ' + exeModule.name +
        ' (base=' + exeModule.base + ', size=0x' + exeModule.size.toString(16) + ')'});

    var candidates = [];
    var base = exeModule.base;
    var size = exeModule.size;

    var chunkSize = 65536;
    var offset = 0;

    while (offset < size) {
        try {
            var scanSize = Math.min(chunkSize, size - offset);
            var addr = base.add(offset);
            var data = addr.readByteArray(scanSize);
            if (!data) break;

            var bytes = new Uint8Array(data);
            for (var j = 0; j <= bytes.length - 32; j++) {
                var candidate = bytes.slice(j, j + 32);
                if (isValidKey(candidate)) {
                    var keyHex = hex(candidate, 32);
                    var keyAddr = addr.add(j);
                    if (!foundKeys.has(keyHex)) {
                        candidates.push({key: keyHex, address: keyAddr});
                    }
                }
            }

            offset += chunkSize - 31;
        } catch (e) {
            offset += chunkSize;
        }
    }

    if (candidates.length > 0) {
        send({type: 'log', message: '梯队 4: 发现 ' + candidates.length + ' 个高熵候选密钥'});
        for (var k = 0; k < Math.min(candidates.length, 5); k++) {
            send({type: 'CANDIDATE', value: candidates[k].key,
                  address: candidates[k].address.toString()});
            console.log('[CANDIDATE] ' + candidates[k].key + ' @ ' + candidates[k].address);
        }
    } else {
        send({type: 'log', message: '梯队 4: 未发现高熵候选密钥'});
    }
}

// ---------------------------------------------------------------------------
// 主入口
// ---------------------------------------------------------------------------

send({type: 'log', message: '=== Frida 探针启动 (Frida 17.x) ==='});
send({type: 'log', message: '进程: ' + Process.id});

// 梯队 1: Windows BCrypt（最可能命中）
hookBCrypt();

// 梯队 2: mbedTLS 静态链接
hookMbedTLS();

// 梯队 3: OpenSSL
hookOpenSSL();

// 梯队 4: 1 秒后开始内存高熵扫描（等模块完全加载）
setTimeout(function () {
    send({type: 'log', message: '梯队 4: 开始内存高熵扫描...'});
    scanModuleForKeys();
}, 1000);

send({type: 'log', message: '探针就绪，等待 AES-256 密钥...'});
send({type: 'READY'});
