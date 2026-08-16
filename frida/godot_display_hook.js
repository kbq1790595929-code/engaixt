'use strict';

var translations = (typeof GODOT_TRANSLATION_MAP !== 'undefined') ? GODOT_TRANSLATION_MAP : {};
var earlyTranslations = (typeof GODOT_EARLY_TRANSLATION_MAP !== 'undefined') ? GODOT_EARLY_TRANSLATION_MAP : {};
var srcLang = (typeof GODOT_SRC_LANG !== 'undefined') ? GODOT_SRC_LANG : 'auto';
var enableProgressiveTranslation = (typeof GODOT_ENABLE_PROGRESSIVE_TRANSLATION !== 'undefined') ? !!GODOT_ENABLE_PROGRESSIVE_TRANSLATION : false;
var overlayMode = (typeof GODOT_OVERLAY_MODE !== 'undefined') ? !!GODOT_OVERLAY_MODE : false;
var cjkFontPath = (typeof GODOT_CJK_FONT_PATH !== 'undefined') ? GODOT_CJK_FONT_PATH : '';
var seenMisses = {};
var replacedCount = 0;
var missedCount = 0;
var dwriteCount = 0;
var gdiCount = 0;
var monoCount = 0;
var godotTextServerCount = 0;
var godotUtf8StringCount = 0;
var overlayTextCount = 0;
var godotFontDataReplaceCount = 0;
var installedHooks = [];
var godotStringKeepAlive = [];
var GODOT_STRING_REFCOUNT_SENTINEL = 0x3fffffff;
var translationPrefixEntries = null;
var lastOverlayKey = '';
var cjkFontBlob = null;
var cjkFontPtr = null;
var cjkFontSize = 0;

function findExport(moduleName, exportName) {
    try {
        var mod = Process.getModuleByName(moduleName);
        return mod.getExportByName(exportName);
    } catch (e) {}
    try { return Module.getExportByName(moduleName, exportName); } catch (e) {}
    try { return Module.findExportByName(moduleName, exportName); } catch (e) {}
    return null;
}

function cleanText(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/\0+$/g, '')
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n')
        .replace(/\\n/g, '\n')
        .replace(/\[\/?[A-Za-z_][^\]]*\]/g, '')
        .replace(/^\s*-\s+/, '')
        .replace(/[ \t\u3000]+/g, ' ')
        .replace(/[ \t\u3000]*\n[ \t\u3000]*/g, '\n')
        .trim();
}

function hasLanguageText(text) {
    if (!text || text.length < 1 || text.length > 2000) return false;
    if (/[\u3040-\u30ff\uff66-\uff9f\u3400-\u9fff]/.test(text)) return true;
    if (srcLang === 'en' && /[A-Za-z]{3,}/.test(text)) return true;
    if (srcLang === 'auto' && /[A-Za-z\u3040-\u30ff\uff66-\uff9f\u3400-\u9fff]/.test(text)) return true;
    return false;
}

function lookupExactInMap(map, text) {
    if (!text) return null;
    var candidates = [
        String(text),
        String(text).trim(),
        cleanText(text)
    ];
    for (var i = 0; i < candidates.length; i++) {
        var key = candidates[i];
        if (key && map[key] && map[key] !== key) {
            return map[key];
        }
    }
    return null;
}

function lookupExactTranslation(text) {
    return lookupExactInMap(translations, text);
}

function lookupEarlyTranslation(text) {
    return lookupExactInMap(earlyTranslations, text);
}

function lookupTranslation(text) {
    var exact = lookupExactTranslation(text);
    if (exact) return exact;
    if (!enableProgressiveTranslation) return null;
    return lookupProgressiveTranslation(text);
}

function lookupOverlayTranslation(text) {
    return lookupExactTranslation(text) || lookupProgressiveTranslation(text);
}

function getTranslationPrefixEntries() {
    if (translationPrefixEntries !== null) return translationPrefixEntries;
    translationPrefixEntries = [];
    var seen = {};
    Object.keys(translations).forEach(function(key) {
        var source = cleanText(key);
        var target = cleanText(translations[key]) || String(translations[key] || '').trim();
        if (!source || !target || source === target || source.length < 8) return;
        if (seen[source]) return;
        seen[source] = true;
        translationPrefixEntries.push({source: source, target: target});
    });
    translationPrefixEntries.sort(function(a, b) {
        return a.source.length - b.source.length;
    });
    return translationPrefixEntries;
}

function prefixByCodePoint(text, count) {
    var out = '';
    var i = 0;
    for (var ch of String(text)) {
        if (i >= count) break;
        out += ch;
        i++;
    }
    return out;
}

function utf8ByteLength(text) {
    var len = 0;
    for (var ch of String(text)) {
        var cp = ch.codePointAt(0);
        if (cp <= 0x7f) len += 1;
        else if (cp <= 0x7ff) len += 2;
        else if (cp <= 0xffff) len += 3;
        else len += 4;
    }
    return len;
}

function lookupProgressiveTranslation(text) {
    var key = cleanText(text);
    if (!key || key.length < 5) return null;
    var matches = [];
    var entries = getTranslationPrefixEntries();
    for (var i = 0; i < entries.length; i++) {
        var source = entries[i].source;
        if (source.length <= key.length) continue;
        if (source.indexOf(key) !== 0) continue;
        matches.push(entries[i]);
        if (matches.length > 2) break;
    }
    if (matches.length !== 1) return null;
    var match = matches[0];
    var ratio = Math.max(0.05, Math.min(1, key.length / match.source.length));
    var targetChars = Array.from(match.target);
    var count = Math.max(1, Math.ceil(targetChars.length * ratio));
    return prefixByCodePoint(match.target, count);
}

function recordMiss(text, source) {
    var key = cleanText(text) || String(text || '').trim();
    if (!key || seenMisses[key] || !hasLanguageText(key)) return;
    if (srcLang === 'en' && key.length < 4) return;
    seenMisses[key] = true;
    missedCount++;
    send({type: 'new_text', text: key, source: source});
}

function emitOverlayText(original, translated, source) {
    var key = cleanText(original) + '\n' + cleanText(translated);
    if (!translated || key === lastOverlayKey) return;
    lastOverlayKey = key;
    overlayTextCount++;
    send({
        type: 'display_text',
        text: cleanText(original),
        translated: String(translated || '').trim(),
        source: source
    });
}

function readWide(ptrValue, count) {
    try {
        if (!ptrValue || ptrValue.isNull()) return null;
        if (count > 0 && count < 10000) return ptrValue.readUtf16String(count);
        return ptrValue.readUtf16String();
    } catch (e) {
        return null;
    }
}

function replaceWideArg(args, strIdx, lenIdx, replacement) {
    args[strIdx] = Memory.allocUtf16String(replacement);
    if (lenIdx >= 0) args[lenIdx] = ptr(replacement.length);
}

function handleWideArg(args, strIdx, lenIdx, source, counter) {
    try {
        var count = lenIdx >= 0 ? args[lenIdx].toInt32() : 0;
        var text = readWide(args[strIdx], count);
        if (!text || !hasLanguageText(text)) return false;
        var translated = lookupTranslation(text);
        if (translated) {
            replaceWideArg(args, strIdx, lenIdx, translated);
            replacedCount++;
            if (counter === 'dwrite') dwriteCount++;
            else if (counter === 'gdi') gdiCount++;
            else if (counter === 'mono') monoCount++;
            return true;
        }
        recordMiss(text, source);
    } catch (e) {}
    return false;
}

function installGdiHooks() {
    ['gdi32.dll', 'gdi32full.dll'].forEach(function(moduleName) {
        var ext = findExport(moduleName, 'ExtTextOutW');
        if (ext) {
            Interceptor.attach(ext, {
                onEnter: function(args) {
                    handleWideArg(args, 5, 6, moduleName + '!ExtTextOutW', 'gdi');
                }
            });
            installedHooks.push(moduleName + '!ExtTextOutW');
        }
        var textOut = findExport(moduleName, 'TextOutW');
        if (textOut) {
            Interceptor.attach(textOut, {
                onEnter: function(args) {
                    handleWideArg(args, 3, 4, moduleName + '!TextOutW', 'gdi');
                }
            });
            installedHooks.push(moduleName + '!TextOutW');
        }
    });
    var drawText = findExport('user32.dll', 'DrawTextW');
    if (drawText) {
        Interceptor.attach(drawText, {
            onEnter: function(args) {
                handleWideArg(args, 1, 2, 'user32!DrawTextW', 'gdi');
            }
        });
        installedHooks.push('user32!DrawTextW');
    }
}

var hookedFactoryMethods = {};

function hookFactoryMethod(factory, index, label) {
    try {
        if (!factory || factory.isNull()) return;
        var vtbl = factory.readPointer();
        var addr = vtbl.add(index * Process.pointerSize).readPointer();
        var key = addr.toString() + ':' + label;
        if (hookedFactoryMethods[key]) return;
        hookedFactoryMethods[key] = true;
        Interceptor.attach(addr, {
            onEnter: function(args) {
                handleWideArg(args, 1, 2, label, 'dwrite');
            }
        });
        installedHooks.push(label);
        send({type: 'log', msg: 'hooked ' + label + ' @ ' + addr});
    } catch (e) {
        send({type: 'log', msg: 'failed to hook ' + label + ': ' + String(e)});
    }
}

function hookDWriteFactory(factory) {
    // IDWriteFactory vtable: 18=CreateTextLayout, 19=CreateGdiCompatibleTextLayout.
    hookFactoryMethod(factory, 18, 'IDWriteFactory::CreateTextLayout');
    hookFactoryMethod(factory, 19, 'IDWriteFactory::CreateGdiCompatibleTextLayout');
}

function installDWriteHook() {
    var createFactory = findExport('dwrite.dll', 'DWriteCreateFactory');
    if (!createFactory) return false;
    Interceptor.attach(createFactory, {
        onEnter: function(args) {
            this.outFactory = args[2];
        },
        onLeave: function(retval) {
            try {
                if (retval.toInt32() !== 0 || !this.outFactory || this.outFactory.isNull()) return;
                hookDWriteFactory(this.outFactory.readPointer());
            } catch (e) {}
        }
    });
    installedHooks.push('dwrite!DWriteCreateFactory');
    send({type: 'log', msg: 'hooked dwrite!DWriteCreateFactory'});
    return true;
}

function installDWriteLoadHooks() {
    var installed = installDWriteHook();
    function maybeInstall(path) {
        if (!path || String(path).toLowerCase().indexOf('dwrite') < 0) return;
        setTimeout(function() { installDWriteHook(); }, 50);
    }
    var loadW = findExport('kernel32.dll', 'LoadLibraryW') || findExport('kernelbase.dll', 'LoadLibraryW');
    if (loadW) {
        Interceptor.attach(loadW, {
            onEnter: function(args) {
                try { maybeInstall(args[0].readUtf16String()); } catch (e) {}
            }
        });
        installedHooks.push('LoadLibraryW monitor');
    }
    var loadA = findExport('kernel32.dll', 'LoadLibraryA') || findExport('kernelbase.dll', 'LoadLibraryA');
    if (loadA) {
        Interceptor.attach(loadA, {
            onEnter: function(args) {
                try { maybeInstall(args[0].readCString()); } catch (e) {}
            }
        });
        installedHooks.push('LoadLibraryA monitor');
    }
    return installed;
}

function installMonoHooks() {
    var monoMod = Process.enumerateModules().find(function(m) {
        return m.name.toLowerCase().indexOf('mono') >= 0;
    });
    if (!monoMod) return;
    var monoStringNewUtf16 = findExport(monoMod.name, 'mono_string_new_utf16');
    if (monoStringNewUtf16) {
        Interceptor.attach(monoStringNewUtf16, {
            onEnter: function(args) {
                handleWideArg(args, 1, 2, monoMod.name + '!mono_string_new_utf16', 'mono');
            }
        });
        installedHooks.push(monoMod.name + '!mono_string_new_utf16');
    }
}

function readFixedAscii(address, maxLen) {
    var chars = [];
    for (var i = 0; i < maxLen; i++) {
        var c = address.add(i).readU8();
        if (c === 0) break;
        chars.push(String.fromCharCode(c));
    }
    return chars.join('');
}

function getMainGodotModule() {
    var modules = Process.enumerateModules();
    for (var i = 0; i < modules.length; i++) {
        var name = modules[i].name.toLowerCase();
        if (name.indexOf('godot') >= 0) return modules[i];
    }
    return modules.length ? modules[0] : null;
}

function parsePeSections(moduleInfo) {
    try {
        var base = moduleInfo.base;
        if (base.readU16() !== 0x5a4d) return null;
        var peOff = base.add(0x3c).readU32();
        var nt = base.add(peOff);
        if (nt.readU32() !== 0x4550) return null;
        var fileHeader = nt.add(4);
        var sectionCount = fileHeader.add(2).readU16();
        var optionalSize = fileHeader.add(16).readU16();
        var optional = fileHeader.add(20);
        var sectionTable = optional.add(optionalSize);
        var sections = [];
        for (var i = 0; i < sectionCount; i++) {
            var s = sectionTable.add(i * 40);
            var name = readFixedAscii(s, 8);
            var virtualSize = s.add(8).readU32();
            var rva = s.add(12).readU32();
            var rawSize = s.add(16).readU32();
            var size = Math.max(virtualSize, rawSize);
            if (rva > moduleInfo.size) continue;
            size = Math.min(size, moduleInfo.size - rva);
            sections.push({name: name, rva: rva, base: base.add(rva), size: size});
        }
        return sections;
    } catch (e) {
        send({type: 'log', msg: 'Godot PE parse failed: ' + String(e)});
        return null;
    }
}

function getSection(sections, name) {
    if (!sections) return null;
    for (var i = 0; i < sections.length; i++) {
        if (sections[i].name === name) return sections[i];
    }
    return null;
}

function asciiPattern(text) {
    var parts = [];
    for (var i = 0; i < text.length; i++) {
        parts.push(text.charCodeAt(i).toString(16).padStart(2, '0'));
    }
    return parts.join(' ');
}

function scanAscii(section, text, limit) {
    var out = [];
    if (!section || !text) return out;
    try {
        var matches = Memory.scanSync(section.base, section.size, asciiPattern(text));
        for (var i = 0; i < matches.length && (!limit || out.length < limit); i++) {
            out.push(matches[i].address);
        }
    } catch (e) {}
    return out;
}

function isReadablePointer(p) {
    if (!p || p.isNull()) return false;
    try {
        var range = Process.findRangeByAddress(p);
        return !!range && range.protection.indexOf('r') >= 0;
    } catch (e) {
        return false;
    }
}

function looksLikeRawFontData(p, size) {
    if (!isReadablePointer(p) || size < 1024 || size > 80 * 1024 * 1024) return false;
    try {
        var b0 = p.readU8();
        var b1 = p.add(1).readU8();
        var b2 = p.add(2).readU8();
        var b3 = p.add(3).readU8();
        if (b0 === 0x00 && b1 === 0x01 && b2 === 0x00 && b3 === 0x00) return true;
        if (b0 === 0x74 && b1 === 0x74 && b2 === 0x63 && b3 === 0x66) return true; // ttcf
        if (b0 === 0x4f && b1 === 0x54 && b2 === 0x54 && b3 === 0x4f) return true; // OTTO
        if (b0 === 0x52 && b1 === 0x53 && b2 === 0x43 && b3 === 0x43) return true; // RSCC, defensive
    } catch (e) {}
    return false;
}

function loadCjkFontBlob() {
    if (cjkFontPtr) return true;
    if (!cjkFontPath) return false;
    try {
        var file = new File(cjkFontPath, 'rb');
        cjkFontBlob = file.readBytes();
        file.close();
        cjkFontSize = cjkFontBlob.byteLength;
        if (cjkFontSize < 1024) return false;
        cjkFontPtr = Memory.alloc(cjkFontSize);
        cjkFontPtr.writeByteArray(cjkFontBlob);
        godotStringKeepAlive.push({font: cjkFontPtr, bytes: cjkFontBlob});
        send({type: 'log', msg: 'loaded CJK font for Godot font hook: ' + cjkFontPath + ' size=' + cjkFontSize});
        return true;
    } catch (e) {
        send({type: 'log', msg: 'failed to load CJK font: ' + cjkFontPath + ': ' + String(e)});
        return false;
    }
}

function ptrEquals(a, b) {
    return a && b && a.compare(b) === 0;
}

function readS32FromBytes(bytes, idx) {
    var v = (bytes[idx] | (bytes[idx + 1] << 8) | (bytes[idx + 2] << 16) | (bytes[idx + 3] << 24)) >>> 0;
    return v > 0x7fffffff ? v - 0x100000000 : v;
}

function findRipRelativeXrefs(moduleInfo, textSection, target) {
    var hits = [];
    if (!textSection || !target) return hits;
    var chunkSize = 4 * 1024 * 1024;
    for (var baseOff = 0; baseOff < textSection.size; baseOff += chunkSize) {
        var size = Math.min(chunkSize + 8, textSection.size - baseOff);
        var bytes;
        try {
            bytes = new Uint8Array(textSection.base.add(baseOff).readByteArray(size));
        } catch (e) {
            continue;
        }
        [0x48, 0x4c].forEach(function(rex) {
            var i = -1;
            while ((i = bytes.indexOf(rex, i + 1)) >= 0) {
                if (i + 7 > bytes.length) break;
                var op = bytes[i + 1];
                if (op !== 0x8d && op !== 0x8b) continue;
                var modrm = bytes[i + 2];
                if ((modrm & 0xc7) !== 0x05) continue;
                var disp = readS32FromBytes(bytes, i + 3);
                var instr = textSection.base.add(baseOff + i);
                if (ptrEquals(instr.add(7 + disp), target)) {
                    hits.push(textSection.rva + baseOff + i);
                }
            }
        });
        [0x8d, 0x8b].forEach(function(opcode) {
            var i = -1;
            while ((i = bytes.indexOf(opcode, i + 1)) >= 0) {
                if (i + 6 > bytes.length) break;
                if (i > 0 && (bytes[i - 1] === 0x48 || bytes[i - 1] === 0x4c)) continue;
                var modrm = bytes[i + 1];
                if ((modrm & 0xc7) !== 0x05) continue;
                var disp = readS32FromBytes(bytes, i + 2);
                var instr = textSection.base.add(baseOff + i);
                if (ptrEquals(instr.add(6 + disp), target)) {
                    hits.push(textSection.rva + baseOff + i);
                }
            }
        });
        if (hits.length) {
            break;
        }
    }
    var seen = {};
    return hits.filter(function(rva) {
        var key = String(rva);
        if (seen[key]) return false;
        seen[key] = true;
        return true;
    });
}

function findRuntimeFunction(pdataSection, rva) {
    if (!pdataSection) return null;
    try {
        var count = Math.floor(pdataSection.size / 12);
        var lo = 0;
        var hi = count - 1;
        while (lo <= hi) {
            var mid = (lo + hi) >> 1;
            var entry = pdataSection.base.add(mid * 12);
            var begin = entry.readU32();
            var end = entry.add(4).readU32();
            if (rva < begin) {
                hi = mid - 1;
            } else if (rva >= end) {
                lo = mid + 1;
            } else {
                return {begin: begin, end: end};
            }
        }
    } catch (e) {}
    return null;
}

function readGodotString32(strObj) {
    try {
        if (!strObj || strObj.isNull()) return null;
        var dataPtr = strObj.readPointer();
        if (!dataPtr || dataPtr.isNull()) return '';
        var sizeValue = dataPtr.sub(8).readU64();
        var size = Number(sizeValue);
        if (!isFinite(size) || size <= 0 || size > 4000) return null;
        var len = size - 1;
        var parts = [];
        for (var i = 0; i < len; i++) {
            var cp = dataPtr.add(i * 4).readU32();
            if (cp === 0) break;
            if (cp > 0x10ffff) return null;
            parts.push(String.fromCodePoint(cp));
        }
        return parts.join('');
    } catch (e) {
        return null;
    }
}

function makeGodotString32(text) {
    var cps = [];
    for (var ch of String(text)) {
        cps.push(ch.codePointAt(0));
    }
    cps.push(0);
    var block = Memory.alloc(16 + cps.length * 4);
    // Godot's CowData refcount lives at this offset. Its own String copy/
    // destroy path decrements it, and once it hits 0 Godot calls its own
    // allocator to free this block -- which was never allocated by Godot,
    // so the text goes blank (or worse) a moment later even though Frida
    // is still keeping the JS-side reference alive. Seed it far above what
    // any single session of copies/destructions can exhaust so Godot never
    // thinks it owns the last reference.
    block.writeU64(GODOT_STRING_REFCOUNT_SENTINEL);
    block.add(8).writeU64(cps.length);
    var dataPtr = block.add(16);
    for (var i = 0; i < cps.length; i++) {
        dataPtr.add(i * 4).writeU32(cps[i]);
    }
    var obj = Memory.alloc(Process.pointerSize);
    obj.writePointer(dataPtr);
    // Godot keeps rendering from this String32 buffer for as long as the
    // shaped text stays on screen (it does not re-call shaped_text_add_string
    // every frame). Frida frees a Memory.alloc() block once nothing in JS
    // references it, so trimming this array while the text is still visible
    // frees memory Godot is still reading from -- the currently displayed
    // line goes blank a moment after it finishes typing out. Never evict.
    godotStringKeepAlive.push({block: block, object: obj});
    return obj;
}

function installGodotUtf8StringHook(moduleInfo, sections) {
    var textSection = getSection(sections, '.text');
    var rdataSection = getSection(sections, '.rdata');
    var pdataSection = getSection(sections, '.pdata');
    if (!textSection || !rdataSection || !pdataSection) return false;

    var anchors = scanAscii(rdataSection, 'Invalid UTF-8 leading byte (%x)', 4);
    if (!anchors.length) return false;
    var candidates = {};
    for (var i = 0; i < anchors.length; i++) {
        var xrefs = findRipRelativeXrefs(moduleInfo, textSection, anchors[i]);
        for (var j = 0; j < xrefs.length; j++) {
            var fn = findRuntimeFunction(pdataSection, xrefs[j]);
            if (!fn) continue;
            var size = fn.end - fn.begin;
            if (size < 256 || size > 12000) continue;
            candidates[String(fn.begin)] = fn;
        }
    }

    var installed = 0;
    Object.keys(candidates).forEach(function(key) {
        var fn = candidates[key];
        var address = moduleInfo.base.add(fn.begin);
        try {
            Interceptor.attach(address, {
                onEnter: function(args) {
                    try {
                        if (!args[1] || args[1].isNull()) return;
                        var len = args[2].toInt32();
                        var text = (len >= 0 && len < 20000) ? args[1].readUtf8String(len) : args[1].readUtf8String();
                        if (!text || !hasLanguageText(text)) return;
                        if (overlayMode) return;
                        var translated = lookupEarlyTranslation(text);
                        if (!translated) return;
                        var mem = Memory.allocUtf8String(translated);
                        // Never evict: same use-after-free hazard as makeGodotString32.
                        godotStringKeepAlive.push({utf8: mem});
                        args[1] = mem;
                        args[2] = ptr(utf8ByteLength(translated));
                        replacedCount++;
                        godotUtf8StringCount++;
                    } catch (e) {}
                }
            });
            installedHooks.push('GodotString::parse_utf8@' + address);
            installed++;
            send({type: 'log', msg: 'hooked GodotString::parse_utf8 @ ' + address + ' size=' + (fn.end - fn.begin)});
        } catch (e) {
            send({type: 'log', msg: 'failed to hook GodotString::parse_utf8 @ ' + address + ': ' + String(e)});
        }
    });
    return installed > 0;
}

function installGodotFontDataHook(moduleInfo, sections) {
    if (!cjkFontPath || !loadCjkFontBlob()) return false;
    var textSection = getSection(sections, '.text');
    var rdataSection = getSection(sections, '.rdata');
    var pdataSection = getSection(sections, '.pdata');
    if (!textSection || !rdataSection || !pdataSection) return false;

    var anchors = [];
    ['_font_set_data_ptr', 'FreeType: Error loading font'].forEach(function(label) {
        scanAscii(rdataSection, label, 8).forEach(function(address) {
            anchors.push(address);
        });
    });
    var candidates = {};
    for (var i = 0; i < anchors.length; i++) {
        var xrefs = findRipRelativeXrefs(moduleInfo, textSection, anchors[i]);
        for (var j = 0; j < xrefs.length; j++) {
            var fn = findRuntimeFunction(pdataSection, xrefs[j]);
            if (!fn) continue;
            var size = fn.end - fn.begin;
            if (size < 128 || size > 30000) continue;
            candidates[String(fn.begin)] = fn;
        }
    }

    var installed = 0;
    Object.keys(candidates).forEach(function(key) {
        var fn = candidates[key];
        var address = moduleInfo.base.add(fn.begin);
        try {
            Interceptor.attach(address, {
                onEnter: function(args) {
                    try {
                        var originalPtr = args[2];
                        var originalSize = Number(args[3].toUInt32 ? args[3].toUInt32() : args[3].toInt32());
                        if (!looksLikeRawFontData(originalPtr, originalSize)) return;
                        args[2] = cjkFontPtr;
                        args[3] = ptr(cjkFontSize);
                        godotFontDataReplaceCount++;
                    } catch (e) {}
                }
            });
            installedHooks.push('GodotTextServer::_font_set_data_ptr@' + address);
            installed++;
            send({type: 'log', msg: 'hooked GodotTextServer::_font_set_data_ptr candidate @ ' + address + ' size=' + (fn.end - fn.begin)});
        } catch (e) {
            send({type: 'log', msg: 'failed to hook Godot font data @ ' + address + ': ' + String(e)});
        }
    });
    if (!installed) {
        send({type: 'log', msg: 'Godot font hook: no _font_set_data_ptr candidate found'});
    }
    return installed > 0;
}

function installGodotTextServerHook() {
    var moduleInfo = getMainGodotModule();
    if (!moduleInfo) return false;
    var sections = parsePeSections(moduleInfo);
    var textSection = getSection(sections, '.text');
    var rdataSection = getSection(sections, '.rdata');
    var pdataSection = getSection(sections, '.pdata');
    if (!textSection || !rdataSection || !pdataSection) {
        send({type: 'log', msg: 'Godot internal hook skipped: PE sections unavailable'});
        return false;
    }

    var versionMatches = scanAscii(rdataSection, 'Godot Engine v', 2);
    if (!versionMatches.length) {
        send({type: 'log', msg: 'Godot internal hook skipped: Godot version string not found in ' + moduleInfo.name});
        return false;
    }
    try {
        send({type: 'log', msg: 'Godot module=' + moduleInfo.name + ' base=' + moduleInfo.base + ' version=' + versionMatches[0].readCString()});
    } catch (e) {
        send({type: 'log', msg: 'Godot module=' + moduleInfo.name + ' base=' + moduleInfo.base});
    }

    installGodotUtf8StringHook(moduleInfo, sections);
    installGodotFontDataHook(moduleInfo, sections);

    var anchors = scanAscii(rdataSection, 'Condition "p_size <= 0" is true. Returning: false', 8);
    if (!anchors.length) {
        anchors = scanAscii(rdataSection, '_shaped_text_add_string', 8);
    }
    var candidates = {};
    for (var i = 0; i < anchors.length; i++) {
        var xrefs = findRipRelativeXrefs(moduleInfo, textSection, anchors[i]);
        for (var j = 0; j < xrefs.length; j++) {
            var fn = findRuntimeFunction(pdataSection, xrefs[j]);
            if (!fn) continue;
            var size = fn.end - fn.begin;
            // TextServerAdvanced::shaped_text_add_string is a medium-sized method.
            // Reject tiny bind wrappers and huge unrelated dispatchers.
            if (size < 128 || size > 20000) continue;
            candidates[String(fn.begin)] = fn;
        }
    }

    var installed = 0;
    Object.keys(candidates).forEach(function(key) {
        var fn = candidates[key];
        var address = moduleInfo.base.add(fn.begin);
        try {
            Interceptor.attach(address, {
                onEnter: function(args) {
                    var text = readGodotString32(args[2]);
                    if (!text || !hasLanguageText(text)) return;
                    var translated = overlayMode ? lookupOverlayTranslation(text) : lookupTranslation(text);
                    if (translated) {
                        if (overlayMode) {
                            emitOverlayText(text, translated, 'GodotTextServer::shaped_text_add_string');
                            return;
                        }
                        args[2] = makeGodotString32(translated);
                        replacedCount++;
                        godotTextServerCount++;
                    } else {
                        recordMiss(text, 'GodotTextServer::shaped_text_add_string');
                    }
                }
            });
            installedHooks.push('GodotTextServer::shaped_text_add_string@' + address);
            installed++;
            send({type: 'log', msg: 'hooked GodotTextServer::shaped_text_add_string @ ' + address + ' size=' + (fn.end - fn.begin)});
        } catch (e) {
            send({type: 'log', msg: 'failed to hook Godot text server @ ' + address + ': ' + String(e)});
        }
    });
    if (!installed) {
        send({type: 'log', msg: 'Godot internal hook: no shaped_text_add_string candidate found'});
    }
    return installed > 0;
}

installGdiHooks();
installDWriteLoadHooks();
installMonoHooks();
installGodotTextServerHook();

setInterval(function() {
    send({
        type: 'stats',
        replaced: replacedCount,
        missed: missedCount,
        dwrite: dwriteCount,
        gdi: gdiCount,
        mono: monoCount,
        godot_text_server: godotTextServerCount,
        godot_utf8_string: godotUtf8StringCount,
        overlay_text: overlayTextCount,
        godot_font_data: godotFontDataReplaceCount
    });
}, 5000);

send({type: 'ready', count: Object.keys(translations).length, hooks: installedHooks});
