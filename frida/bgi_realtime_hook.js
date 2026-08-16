'use strict';

var translations = (typeof BGI_TRANSLATION_MAP !== 'undefined') ? BGI_TRANSLATION_MAP : {};
var enableMultiByteReplace = (typeof BGI_ENABLE_MULTIBYTE_REPLACE !== 'undefined') ? BGI_ENABLE_MULTIBYTE_REPLACE : false;
var enableAnsiRenderReplace = (typeof BGI_ANSI_RENDER_REPLACE !== 'undefined') ? BGI_ANSI_RENDER_REPLACE : false;
var enableFontCreateHook = (typeof BGI_FONT_CREATE_HOOK !== 'undefined') ? BGI_FONT_CREATE_HOOK : false;
var sjisTunnelTable = (typeof BGI_SJIS_TUNNEL_TABLE !== 'undefined') ? BGI_SJIS_TUNNEL_TABLE : '';
var seen = {};
var replacedCount = 0;
var missedCount = 0;
var tunnelCount = 0;

function isSjisLead(byteValue) {
    return (byteValue >= 0x81 && byteValue < 0xA0) || (byteValue >= 0xE0 && byteValue < 0xFD);
}

function tunnelIndex(high, low) {
    if (high < 0xF0 || high > 0xFC) return -1;
    if (low < 0x40 || low > 0xFC || low === 0x7F) return -1;
    var lowIdx = low < 0x7F ? low - 0x40 : low - 0x41;
    return (high - 0xF0) * 188 + lowIdx;
}

function loadTunnelMappings(table) {
    var out = [];
    for (var i = 0; i + 3 < table.length; i += 4) {
        out.push(String.fromCharCode(parseInt(table.substring(i, i + 4), 16)));
    }
    return out;
}

var tunnelMappings = loadTunnelMappings(sjisTunnelTable);
var cp932ToWideFn = null;
var internalMbDepth = 0;
var internalWideDrawDepth = 0;
var internalFontCreateDepth = 0;

function decodeCp932Bytes(bytes) {
    if (!bytes.length) return '';
    var buf = Memory.alloc(bytes.length + 1);
    for (var i = 0; i < bytes.length; i++) buf.add(i).writeU8(bytes[i]);
    buf.add(bytes.length).writeU8(0);

    try {
        if (!cp932ToWideFn) {
            var mb = findExport('kernel32.dll', 'MultiByteToWideChar');
            if (mb) cp932ToWideFn = new NativeFunction(mb, 'int', ['uint', 'uint', 'pointer', 'int', 'pointer', 'int']);
        }
        if (cp932ToWideFn) {
            internalMbDepth++;
            var chars = cp932ToWideFn(932, 0, buf, bytes.length, ptr(0), 0);
            if (chars > 0) {
                var out = Memory.alloc((chars + 1) * 2);
                cp932ToWideFn(932, 0, buf, bytes.length, out, chars);
                out.add(chars * 2).writeU16(0);
                internalMbDepth--;
                return out.readUtf16String(chars);
            }
            internalMbDepth--;
        }
    } catch (e) {
        internalMbDepth = Math.max(0, internalMbDepth - 1);
    }
    return buf.readAnsiString(bytes.length);
}

function hasTunnelBytes(ptrValue, byteCount) {
    try {
        if (!ptrValue || ptrValue.isNull()) return false;
        var max = byteCount > 0 ? byteCount : 4096;
        for (var i = 0; i < max; i++) {
            var high = ptrValue.add(i).readU8();
            if (high === 0 && byteCount <= 0) return false;
            if (!isSjisLead(high)) continue;
            if (i + 1 >= max) return false;
            var low = ptrValue.add(i + 1).readU8();
            if (low === 0 && byteCount <= 0) return false;
            var idx = tunnelIndex(high, low);
            if (idx >= 0 && idx < tunnelMappings.length) return true;
            i++;
        }
    } catch (e) {}
    return false;
}

function decodeCp932Tunnel(ptrValue, byteCount) {
    if (!hasTunnelBytes(ptrValue, byteCount)) return null;
    var chunks = [];
    var raw = [];
    var max = byteCount > 0 ? byteCount : 4096;

    function flushRaw() {
        if (!raw.length) return;
        chunks.push(decodeCp932Bytes(raw));
        raw = [];
    }

    try {
        for (var i = 0; i < max; i++) {
            var high = ptrValue.add(i).readU8();
            if (high === 0 && byteCount <= 0) break;
            if (isSjisLead(high) && i + 1 < max) {
                var low = ptrValue.add(i + 1).readU8();
                if (low === 0 && byteCount <= 0) break;
                var idx = tunnelIndex(high, low);
                if (idx >= 0 && idx < tunnelMappings.length) {
                    flushRaw();
                    chunks.push(tunnelMappings[idx]);
                    i++;
                    continue;
                }
                raw.push(high);
                raw.push(low);
                i++;
            } else {
                raw.push(high);
            }
        }
        flushRaw();
        return chunks.join('');
    } catch (e) {
        return null;
    }
}

function findExport(moduleName, exportName) {
    try {
        var mod = Process.getModuleByName(moduleName);
        return mod.getExportByName(exportName);
    } catch (e) {}
    try {
        return Module.getExportByName(moduleName, exportName);
    } catch (e) {}
    return null;
}

var CHINESE_FONT_FACE = 'Microsoft YaHei UI';
var FONT_HEIGHT_SCALE = (typeof BGI_FONT_HEIGHT_SCALE !== 'undefined') ? BGI_FONT_HEIGHT_SCALE : 0.90;
var GB2312_CHARSET = 134;
var CLEARTYPE_QUALITY = 5;
var OBJ_FONT = 6;
var LOGFONTW_SIZE = 92;
var selectObjectPtr = findExport('gdi32.dll', 'SelectObject') || findExport('gdi32full.dll', 'SelectObject');
var getCurrentObjectPtr = findExport('gdi32.dll', 'GetCurrentObject') || findExport('gdi32full.dll', 'GetCurrentObject');
var getObjectWPtr = findExport('gdi32.dll', 'GetObjectW') || findExport('gdi32full.dll', 'GetObjectW');
var createFontIndirectWPtr = findExport('gdi32.dll', 'CreateFontIndirectW') || findExport('gdi32full.dll', 'CreateFontIndirectW');
var selectObjectFn = selectObjectPtr ? new NativeFunction(selectObjectPtr, 'pointer', ['pointer', 'pointer']) : null;
var getCurrentObjectFn = getCurrentObjectPtr ? new NativeFunction(getCurrentObjectPtr, 'pointer', ['pointer', 'uint']) : null;
var getObjectWFn = getObjectWPtr ? new NativeFunction(getObjectWPtr, 'int', ['pointer', 'int', 'pointer']) : null;
var createFontIndirectWFn = createFontIndirectWPtr ? new NativeFunction(createFontIndirectWPtr, 'pointer', ['pointer']) : null;
var chineseFontCache = {};
var hookFontHandles = {};

function containsCjk(text) {
    return !!text && /[\u3400-\u4dbf\u4e00-\u9fff]/.test(text);
}

function clearBytes(ptrValue, count) {
    for (var i = 0; i < count; i++) ptrValue.add(i).writeU8(0);
}

function pointerFromSigned32(value) {
    if (value >= 0) return ptr(value);
    return ptr('0x' + ((value >>> 0).toString(16)));
}

function scaledFontHeight(value) {
    if (!value) return value;
    var absValue = Math.abs(value);
    if (absValue < 10) return value;
    var scaled = Math.max(1, Math.round(absValue * FONT_HEIGHT_SCALE));
    return value < 0 ? -scaled : scaled;
}

function patchLogFontCommon(lf) {
    lf.add(23).writeU8(GB2312_CHARSET);
    lf.add(26).writeU8(CLEARTYPE_QUALITY);
}

function patchLogFontW(lf, scaleHeight) {
    if (scaleHeight) lf.writeS32(scaledFontHeight(lf.readS32()));
    patchLogFontCommon(lf);
    clearBytes(lf.add(28), 64);
    lf.add(28).writeUtf16String(CHINESE_FONT_FACE);
}

function patchLogFontA(lf, scaleHeight) {
    if (scaleHeight) lf.writeS32(scaledFontHeight(lf.readS32()));
    patchLogFontCommon(lf);
    clearBytes(lf.add(28), 32);
    lf.add(28).writeAnsiString(CHINESE_FONT_FACE);
}

function makeDefaultLogFontW() {
    var lf = Memory.alloc(LOGFONTW_SIZE);
    clearBytes(lf, LOGFONTW_SIZE);
    lf.writeS32(-18);
    lf.add(16).writeS32(400);
    patchLogFontW(lf, true);
    return lf;
}

function logFontKey(lf) {
    return [
        lf.readS32(),
        lf.add(4).readS32(),
        lf.add(8).readS32(),
        lf.add(12).readS32(),
        lf.add(16).readS32(),
        lf.add(20).readU8(),
        lf.add(21).readU8(),
        lf.add(22).readU8(),
        lf.add(27).readU8()
    ].join(':');
}

function fontForHdc(hdc) {
    if (!createFontIndirectWFn) return null;
    var lf = Memory.alloc(LOGFONTW_SIZE);
    clearBytes(lf, LOGFONTW_SIZE);
    var copied = 0;
    var current = ptr(0);
    try {
        if (getCurrentObjectFn && getObjectWFn) {
            current = getCurrentObjectFn(hdc, OBJ_FONT);
            if (current && !current.isNull()) copied = getObjectWFn(current, LOGFONTW_SIZE, lf);
        }
    } catch (e) {
        copied = 0;
    }
    if (copied <= 0) lf = makeDefaultLogFontW();
    var currentIsHookFont = current && !current.isNull() && hookFontHandles[current.toString()];
    patchLogFontW(lf, !currentIsHookFont);
    var key = logFontKey(lf);
    var font = chineseFontCache[key];
    if (!font || font.isNull()) {
        internalFontCreateDepth++;
        try {
            font = createFontIndirectWFn(lf);
        } finally {
            internalFontCreateDepth = Math.max(0, internalFontCreateDepth - 1);
        }
        if (font && !font.isNull()) hookFontHandles[font.toString()] = true;
        chineseFontCache[key] = font;
    }
    return font;
}

function selectChineseFont(hdc) {
    if (!selectObjectFn || !hdc || hdc.isNull()) return null;
    try {
        var font = fontForHdc(hdc);
        if (!font || font.isNull()) return null;
        return selectObjectFn(hdc, font);
    } catch (e) {
        return null;
    }
}

function restoreFont(hdc, oldFont) {
    try {
        if (selectObjectFn && oldFont && !oldFont.isNull()) selectObjectFn(hdc, oldFont);
    } catch (e) {}
}

function drawWithChineseFont(hdc, callback) {
    var oldFont = selectChineseFont(hdc);
    try {
        internalWideDrawDepth++;
        callback();
    } finally {
        internalWideDrawDepth = Math.max(0, internalWideDrawDepth - 1);
        restoreFont(hdc, oldFont);
    }
}

function needsTranslate(text) {
    if (!text || text.length < 1 || text.length > 1000) return false;
    return /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]/.test(text);
}

function replaceUtf16Arg(args, strIdx, lenIdx, translated) {
    args[strIdx] = Memory.allocUtf16String(translated);
    if (lenIdx >= 0) args[lenIdx] = ptr(translated.length);
}

function suppressAnsiTextArg(args, strIdx, lenIdx) {
    args[strIdx] = Memory.allocAnsiString('');
    if (lenIdx >= 0) args[lenIdx] = ptr(0);
}

function readCp932(ptrValue, byteCount) {
    try {
        if (ptrValue.isNull()) return null;
        if (byteCount <= 0) return ptrValue.readAnsiString();
        return ptrValue.readAnsiString(byteCount);
    } catch (e) {
        return null;
    }
}

function handleText(text, args, strIdx, lenIdx, source) {
    if (!needsTranslate(text)) return null;
    var translated = translations[text];
    if (translated) {
        replaceUtf16Arg(args, strIdx, lenIdx, translated);
        replacedCount++;
        return translated;
    }
    if (!seen[text]) {
        seen[text] = true;
        missedCount++;
        send({type: 'new_text', text: text, source: source});
    }
    return null;
}

function drawTranslatedAnsi(ptrValue, byteCount, drawWide, source) {
    var text = decodeCp932Tunnel(ptrValue, byteCount);
    var isTunnel = !!text;
    if (!text) {
        var max = byteCount > 0 ? byteCount : -1;
        text = readCp932(ptrValue, max);
    }
    if (!needsTranslate(text)) return false;

    var translated = isTunnel ? text : translations[text];
    if (!translated) {
        if (!seen[text]) {
            seen[text] = true;
            missedCount++;
            send({type: 'new_text', text: text, source: source});
        }
        return false;
    }

    drawWide(translated);
    if (isTunnel) {
        tunnelCount++;
    } else {
        replacedCount++;
    }
    return true;
}

function hookTextOut(moduleName) {
    var extTextOutWCall = findExport(moduleName, 'ExtTextOutW');
    var textOutWCall = findExport(moduleName, 'TextOutW');
    var extTextOutWFn = extTextOutWCall ? new NativeFunction(extTextOutWCall, 'int', ['pointer', 'int', 'int', 'uint', 'pointer', 'pointer', 'uint', 'pointer']) : null;
    var textOutWFn = textOutWCall ? new NativeFunction(textOutWCall, 'int', ['pointer', 'int', 'int', 'pointer', 'int']) : null;

    var extTextOutA = enableAnsiRenderReplace ? findExport(moduleName, 'ExtTextOutA') : null;
    if (extTextOutA) {
        Interceptor.attach(extTextOutA, {
            onEnter: function(args) {
                try {
                    var count = args[6].toInt32();
                    if (!extTextOutWFn) return;
                    var drawn = drawTranslatedAnsi(args[5], count, function(text) {
                        drawWithChineseFont(args[0], function() {
                            var wide = Memory.allocUtf16String(text);
                            extTextOutWFn(args[0], args[1].toInt32(), args[2].toInt32(), args[3].toUInt32(), args[4], wide, text.length, args[7]);
                        });
                    }, moduleName + '!ExtTextOutA');
                    if (drawn) suppressAnsiTextArg(args, 5, 6);
                } catch (e) {}
            }
        });
        send({type: 'log', msg: 'hooked ' + moduleName + '!ExtTextOutA render replace'});
    }

    var textOutA = enableAnsiRenderReplace ? findExport(moduleName, 'TextOutA') : null;
    if (textOutA) {
        Interceptor.attach(textOutA, {
            onEnter: function(args) {
                try {
                    var count = args[4].toInt32();
                    if (!textOutWFn) return;
                    var drawn = drawTranslatedAnsi(args[3], count, function(text) {
                        drawWithChineseFont(args[0], function() {
                            var wide = Memory.allocUtf16String(text);
                            textOutWFn(args[0], args[1].toInt32(), args[2].toInt32(), wide, text.length);
                        });
                    }, moduleName + '!TextOutA');
                    if (drawn) suppressAnsiTextArg(args, 3, 4);
                } catch (e) {}
            }
        });
        send({type: 'log', msg: 'hooked ' + moduleName + '!TextOutA render replace'});
    }

    var extTextOutW = findExport(moduleName, 'ExtTextOutW');
    if (extTextOutW) {
        Interceptor.attach(extTextOutW, {
            onEnter: function(args) {
                try {
                    if (internalWideDrawDepth > 0) return;
                    this.hdc = args[0];
                    var count = args[6].toInt32();
                    var text = args[5].readUtf16String(count > 0 ? count : 0);
                    var translated = handleText(text, args, 5, 6, moduleName + '!ExtTextOutW');
                    if (containsCjk(translated || text)) this.oldFont = selectChineseFont(args[0]);
                } catch (e) {}
            },
            onLeave: function(_retval) {
                restoreFont(this.hdc, this.oldFont);
            }
        });
        send({type: 'log', msg: 'hooked ' + moduleName + '!ExtTextOutW'});
    }

    var textOutW = findExport(moduleName, 'TextOutW');
    if (textOutW) {
        Interceptor.attach(textOutW, {
            onEnter: function(args) {
                try {
                    if (internalWideDrawDepth > 0) return;
                    this.hdc = args[0];
                    var count = args[4].toInt32();
                    var text = args[3].readUtf16String(count > 0 ? count : 0);
                    var translated = handleText(text, args, 3, 4, moduleName + '!TextOutW');
                    if (containsCjk(translated || text)) this.oldFont = selectChineseFont(args[0]);
                } catch (e) {}
            },
            onLeave: function(_retval) {
                restoreFont(this.hdc, this.oldFont);
            }
        });
        send({type: 'log', msg: 'hooked ' + moduleName + '!TextOutW'});
    }
}

hookTextOut('gdi32.dll');
hookTextOut('gdi32full.dll');

var drawTextW = findExport('user32.dll', 'DrawTextW');
if (drawTextW) {
    Interceptor.attach(drawTextW, {
        onEnter: function(args) {
            try {
                if (internalWideDrawDepth > 0) return;
                this.hdc = args[0];
                var count = args[2].toInt32();
                var text = args[1].readUtf16String(count > 0 ? count : 0);
                var translated = handleText(text, args, 1, 2, 'user32!DrawTextW');
                if (containsCjk(translated || text)) this.oldFont = selectChineseFont(args[0]);
            } catch (e) {}
        },
        onLeave: function(_retval) {
            restoreFont(this.hdc, this.oldFont);
        }
    });
    send({type: 'log', msg: 'hooked user32!DrawTextW'});
}

var drawTextA = enableAnsiRenderReplace ? findExport('user32.dll', 'DrawTextA') : null;
var drawTextWCall = findExport('user32.dll', 'DrawTextW');
var drawTextWFn = drawTextWCall ? new NativeFunction(drawTextWCall, 'int', ['pointer', 'pointer', 'int', 'pointer', 'uint']) : null;
if (drawTextA) {
    Interceptor.attach(drawTextA, {
        onEnter: function(args) {
            try {
                var count = args[2].toInt32();
                if (!drawTextWFn) return;
                var drawn = drawTranslatedAnsi(args[1], count, function(text) {
                    drawWithChineseFont(args[0], function() {
                        var wide = Memory.allocUtf16String(text);
                        drawTextWFn(args[0], wide, text.length, args[3], args[4].toUInt32());
                    });
                }, 'user32!DrawTextA');
                if (drawn) suppressAnsiTextArg(args, 1, 2);
            } catch (e) {}
        }
    });
    send({type: 'log', msg: 'hooked user32!DrawTextA render replace'});
}

// Prefer a modern Chinese-capable font when the engine creates GDI fonts.
var hookedFontExports = {};

function attachFontHook(addr, callbacks, label) {
    if (!addr) return;
    var key = addr.toString();
    if (hookedFontExports[key]) return;
    hookedFontExports[key] = true;
    Interceptor.attach(addr, callbacks);
    send({type: 'log', msg: 'hooked ' + label});
}

function hookFontCreation(moduleName) {
    attachFontHook(findExport(moduleName, 'CreateFontW'), {
        onEnter: function(args) {
            try {
                if (internalFontCreateDepth > 0) return;
                args[0] = pointerFromSigned32(scaledFontHeight(args[0].toInt32()));
                args[8] = ptr(GB2312_CHARSET);
                args[11] = ptr(CLEARTYPE_QUALITY);
                args[13] = Memory.allocUtf16String(CHINESE_FONT_FACE);
            } catch (e) {}
        }
    }, moduleName + '!CreateFontW');

    attachFontHook(findExport(moduleName, 'CreateFontA'), {
        onEnter: function(args) {
            try {
                if (internalFontCreateDepth > 0) return;
                args[0] = pointerFromSigned32(scaledFontHeight(args[0].toInt32()));
                args[8] = ptr(GB2312_CHARSET);
                args[11] = ptr(CLEARTYPE_QUALITY);
                args[13] = Memory.allocAnsiString(CHINESE_FONT_FACE);
            } catch (e) {}
        }
    }, moduleName + '!CreateFontA');

    attachFontHook(findExport(moduleName, 'CreateFontIndirectW'), {
        onEnter: function(args) {
            try {
                if (internalFontCreateDepth > 0) return;
                patchLogFontW(args[0], true);
            } catch (e) {}
        }
    }, moduleName + '!CreateFontIndirectW');

    attachFontHook(findExport(moduleName, 'CreateFontIndirectA'), {
        onEnter: function(args) {
            try {
                if (internalFontCreateDepth > 0) return;
                patchLogFontA(args[0], true);
            } catch (e) {}
        }
    }, moduleName + '!CreateFontIndirectA');
}

if (enableFontCreateHook) {
    hookFontCreation('gdi32.dll');
    hookFontCreation('gdi32full.dll');
}

// BGI often converts script SJIS to UTF-16 while loading/running bytecode.
// Writing back here is intentionally opt-in because it can corrupt script/code
// size calculations before the text reaches the renderer.
var multiByteToWideChar = findExport('kernel32.dll', 'MultiByteToWideChar');
if (multiByteToWideChar) {
    Interceptor.attach(multiByteToWideChar, {
        onEnter: function(args) {
            if (internalMbDepth > 0) {
                this.skip = true;
                return;
            }
            this.srcPtr = args[2];
            this.srcBytes = args[3].toInt32();
            this.src = readCp932(args[2], args[3].toInt32());
            this.dst = args[4];
            this.dstChars = args[5].toInt32();
        },
        onLeave: function(retval) {
            try {
                if (this.skip) return;
                var tunneled = decodeCp932Tunnel(this.srcPtr, this.srcBytes);
                if (tunneled && !this.dst.isNull() && this.dstChars > 0) {
                    var maxTunnelChars = Math.max(0, this.dstChars - 1);
                    var tunnelOut = tunneled.length > maxTunnelChars ? tunneled.substring(0, maxTunnelChars) : tunneled;
                    this.dst.writeUtf16String(tunnelOut);
                    retval.replace(tunnelOut.length);
                    tunnelCount++;
                    return;
                }
                if (!this.src || !needsTranslate(this.src)) return;
                var translated = translations[this.src];
                if (!translated) {
                    if (!seen[this.src]) {
                        seen[this.src] = true;
                        missedCount++;
                        send({type: 'new_text', text: this.src, source: 'kernel32!MultiByteToWideChar'});
                    }
                    return;
                }
                if (!enableMultiByteReplace) return;
                if (this.dst.isNull() || this.dstChars <= 0) return;
                var maxChars = Math.max(0, this.dstChars - 1);
                var out = translated.length > maxChars ? translated.substring(0, maxChars) : translated;
                this.dst.writeUtf16String(out);
                retval.replace(out.length);
                replacedCount++;
            } catch (e) {}
        }
    });
    send({type: 'log', msg: 'hooked kernel32!MultiByteToWideChar monitor replace=' + enableMultiByteReplace});
}

recv('translations', function onTranslations(message) {
    if (message && message.map) {
        var keys = Object.keys(message.map);
        for (var i = 0; i < keys.length; i++) translations[keys[i]] = message.map[keys[i]];
        send({type: 'log', msg: 'loaded ' + keys.length + ' translations'});
    }
    recv('translations', onTranslations);
});

setInterval(function() {
    send({type: 'stats', replaced: replacedCount, missed: missedCount, tunnel: tunnelCount});
}, 5000);

send({type: 'ready', count: Object.keys(translations).length, tunnel: tunnelMappings.length});
