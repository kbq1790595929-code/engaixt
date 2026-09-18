'use strict';

var translations = (typeof KIRIKIRI_TRANSLATION_MAP !== 'undefined') ? KIRIKIRI_TRANSLATION_MAP : {};
var enableAnsiRenderReplace = (typeof KIRIKIRI_ANSI_RENDER_REPLACE !== 'undefined') ? KIRIKIRI_ANSI_RENDER_REPLACE : true;
var enableFontCreateHook = (typeof KIRIKIRI_FONT_CREATE_HOOK !== 'undefined') ? KIRIKIRI_FONT_CREATE_HOOK : true;
var fontHeightScale = (typeof KIRIKIRI_FONT_HEIGHT_SCALE !== 'undefined') ? KIRIKIRI_FONT_HEIGHT_SCALE : 1.0;

var seen = {};
var replacedCount = 0;
var missedCount = 0;
var conversionCount = 0;
var measureRunCount = 0;
var internalWideDrawDepth = 0;
var internalFontCreateDepth = 0;
var internalMbDepth = 0;
var cp932ToWideFn = null;
var measureBuffers = {};
var measureHooks = {};

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

function readBytes(ptrValue, byteCount) {
    var bytes = [];
    if (!ptrValue || ptrValue.isNull()) return bytes;
    var max = byteCount > 0 ? byteCount : 4096;
    try {
        for (var i = 0; i < max; i++) {
            var b = ptrValue.add(i).readU8();
            if (byteCount <= 0 && b === 0) break;
            bytes.push(b);
        }
    } catch (e) {}
    return bytes;
}

function decodeCp932Bytes(bytes) {
    if (!bytes || !bytes.length) return '';
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

    try {
        return buf.readAnsiString(bytes.length);
    } catch (e2) {
        return '';
    }
}

function readCp932(ptrValue, byteCount) {
    return decodeCp932Bytes(readBytes(ptrValue, byteCount));
}

function readUtf16(ptrValue, charCount) {
    if (!ptrValue || ptrValue.isNull()) return '';
    try {
        if (charCount > 0) return ptrValue.readUtf16String(charCount);
        return ptrValue.readUtf16String();
    } catch (e) {
        return '';
    }
}

function scrubText(text) {
    if (!text) return '';
    return String(text).replace(/\u0000/g, '');
}

function hasJapaneseOrCjk(text) {
    return /[\u3040-\u30ff\uff66-\uff9f\u3400-\u4dbf\u4e00-\u9fff]/.test(text || '');
}

function isNoise(text) {
    if (!text) return true;
    var trimmed = text.trim();
    if (trimmed.length < 2 || trimmed.length > 1200) return true;
    for (var i = 0; i < trimmed.length; i++) {
        var code = trimmed.charCodeAt(i);
        if (code > 0 && code < 32 && code !== 9 && code !== 10 && code !== 13) return true;
    }
    if (!hasJapaneseOrCjk(trimmed)) return true;
    if (/^[\s\d.,:;!?()[\]{}"'`~+\-*/\\|_=<>#$%&^@]+$/.test(trimmed)) return true;
    if (/^[A-Za-z0-9_./\\:-]+$/.test(trimmed)) return true;
    return false;
}

function lookupTranslation(text) {
    if (!text) return null;
    if (translations[text]) return translations[text];
    var trimmed = text.trim();
    if (trimmed && translations[trimmed]) {
        var leading = text.match(/^\s*/)[0];
        var trailing = text.match(/\s*$/)[0];
        return leading + translations[trimmed] + trailing;
    }
    return null;
}

function rememberMiss(text, source) {
    text = scrubText(text);
    if (isNoise(text)) return;
    if (!seen[text]) {
        seen[text] = true;
        missedCount++;
        send({type: 'new_text', text: text, source: source});
    }
}

function replaceUtf16Arg(args, strIdx, lenIdx, translated) {
    args[strIdx] = Memory.allocUtf16String(translated);
    if (lenIdx >= 0) args[lenIdx] = ptr(translated.length);
}

function suppressAnsiTextArg(args, strIdx, lenIdx) {
    args[strIdx] = Memory.allocAnsiString('');
    if (lenIdx >= 0) args[lenIdx] = ptr(0);
}

var CHINESE_FONT_FACE = 'Microsoft YaHei UI';
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
    var scaled = Math.max(1, Math.round(absValue * fontHeightScale));
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

function handleWideText(text, args, strIdx, lenIdx, source) {
    text = scrubText(text);
    if (isNoise(text)) return null;
    var translated = lookupTranslation(text);
    if (translated) {
        replaceUtf16Arg(args, strIdx, lenIdx, translated);
        replacedCount++;
        return translated;
    }
    rememberMiss(text, source);
    return null;
}

function isMeasureTextFragment(text) {
    if (!text) return false;
    var trimmed = scrubText(text).trim();
    if (!trimmed || trimmed.length > 32) return false;
    if (!hasJapaneseOrCjk(trimmed) && !isJapanesePunctuationFragment(trimmed) && !isMeasureNumericFragment(trimmed)) return false;
    if (/^[\s\d.,:;!?()[\]{}"'`~+\-*/\\|_=<>#$%&^@]+$/.test(trimmed)) return false;
    return true;
}

function isJapanesePunctuationFragment(text) {
    return /^[「」『』（）【】《》〈〉、。，．・…！？!?]+$/.test(text || '');
}

function isMeasureNumericFragment(text) {
    return /^[0-9０-９]+$/.test(text || '');
}

function isRedundantGdiMeasureSource(source) {
    if (!source || source.indexOf('gdi32.dll!') !== 0) return false;
    var twin = source.replace(/^gdi32\.dll!/, 'gdi32full.dll!');
    return !!measureHooks[twin];
}

function measureKey(source) {
    return source.replace(/^gdi32full\.dll!/, 'gdi!').replace(/^gdi32\.dll!/, 'gdi!');
}

function flushMeasureBuffer(key, reason) {
    var buf = measureBuffers[key];
    if (!buf || !buf.text) return;
    var text = cleanMeasureRunText(scrubText(buf.text));
    measureBuffers[key] = {text: '', last: Date.now(), source: buf.source};
    if (isNoise(text)) return;
    measureRunCount++;
    rememberMiss(text, (buf.source || key) + ':measure-run:' + (reason || 'flush'));
}

function cleanMeasureRunText(text) {
    if (!text) return '';
    var out = '';
    for (var i = 0; i < text.length;) {
        var ch = text.charAt(i);
        var j = i + 1;
        while (j < text.length && text.charAt(j) === ch) j++;
        var run = j - i;
        if (run >= 3 && /[\u3040-\u30ff\uff66-\uff9f\u3400-\u4dbf\u4e00-\u9fff]/.test(ch)) {
            out += ch;
        } else if (run >= 3 && /[0-9０-９]/.test(ch)) {
            out += ch + ch;
        } else {
            out += text.substring(i, j);
        }
        i = j;
    }
    return out
        .replace(/([「『])\s+/g, '$1')
        .replace(/\s+([」』、。，．！？!?])/g, '$1')
        .trim();
}

function appendMeasuredText(text, source) {
    text = scrubText(text);
    if (isRedundantGdiMeasureSource(source)) return;
    if (!isMeasureTextFragment(text)) return;
    var key = measureKey(source);
    var now = Date.now();
    var buf = measureBuffers[key] || {text: '', last: now, source: source};
    if (buf.text && now - buf.last > 450) {
        flushMeasureBuffer(key, 'idle');
        buf = {text: '', last: now, source: source};
    }

    var current = scrubText(buf.text || '');
    if (!current) {
        buf.text = text;
    } else if (text === current || current.indexOf(text) === 0) {
        // Prefix measurement often repeats shorter fragments after a longer one.
        buf.text = current;
    } else if (text.indexOf(current) === 0) {
        // KiriKiri/textrender measures growing prefixes: 事 -> 事情 -> 事情は...
        buf.text = text;
    } else {
        var overlap = longestOverlap(current, text);
        if (overlap >= 1) {
            buf.text = current + text.substring(overlap);
        } else if (shouldAppendMeasureFragment(current, text)) {
            // Some KiriKiriZ/textrender paths measure one glyph at a time.
            buf.text = current + text;
        } else {
            flushMeasureBuffer(key, 'switch');
            buf = {text: text, last: now, source: source};
        }
    }
    buf.last = now;
    buf.source = source;
    measureBuffers[key] = buf;
    if (buf.text.length >= 160 || /[。…」』）\]]$/.test(text)) {
        flushMeasureBuffer(key, 'boundary');
    }
}

function shouldAppendMeasureFragment(current, text) {
    if (!current || !text) return false;
    if (current.length >= 220) return false;
    if (text.indexOf(current) === 0 || current.indexOf(text) === 0) return false;
    if (/[\r\n]/.test(current) || /[\r\n]/.test(text)) return false;
    if (/[！？!?]$/.test(current) && /^[」』）\]]$/.test(text)) return true;
    if (/[！？!?]$/.test(current)) return false;
    if (/[。…」』）\]]$/.test(current)) return false;
    return text.length <= 2 || isJapanesePunctuationFragment(text);
}

function longestOverlap(left, right) {
    var max = Math.min(left.length, right.length, 24);
    for (var len = max; len >= 1; len--) {
        if (left.substring(left.length - len) === right.substring(0, len)) return len;
    }
    return 0;
}

function drawTranslatedAnsi(ptrValue, byteCount, drawWide, source) {
    var text = scrubText(readCp932(ptrValue, byteCount));
    if (isNoise(text)) return false;

    var translated = lookupTranslation(text);
    if (!translated) {
        rememberMiss(text, source);
        return false;
    }

    drawWide(translated);
    replacedCount++;
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
                    if (!extTextOutWFn) return;
                    var count = args[6].toInt32();
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
                    if (!textOutWFn) return;
                    var count = args[4].toInt32();
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
                    var text = readUtf16(args[5], count);
                    var translated = handleWideText(text, args, 5, 6, moduleName + '!ExtTextOutW');
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
                    var text = readUtf16(args[3], count);
                    var translated = handleWideText(text, args, 3, 4, moduleName + '!TextOutW');
                    if (containsCjk(translated || text)) this.oldFont = selectChineseFont(args[0]);
                } catch (e) {}
            },
            onLeave: function(_retval) {
                restoreFont(this.hdc, this.oldFont);
            }
        });
        send({type: 'log', msg: 'hooked ' + moduleName + '!TextOutW'});
    }

    hookTextMeasure(moduleName);
}

function hookTextMeasure(moduleName) {
    var keyA = moduleName + '!GetTextExtentPoint32A';
    if (!measureHooks[keyA]) {
        var extentA = findExport(moduleName, 'GetTextExtentPoint32A');
        if (extentA) {
            measureHooks[keyA] = true;
            Interceptor.attach(extentA, {
                onEnter: function(args) {
                    try {
                        appendMeasuredText(readCp932(args[1], args[2].toInt32()), moduleName + '!GetTextExtentPoint32A');
                    } catch (e) {}
                }
            });
            send({type: 'log', msg: 'hooked ' + moduleName + '!GetTextExtentPoint32A capture'});
        }
    }

    var keyW = moduleName + '!GetTextExtentPoint32W';
    if (!measureHooks[keyW]) {
        var extentW = findExport(moduleName, 'GetTextExtentPoint32W');
        if (extentW) {
            measureHooks[keyW] = true;
            Interceptor.attach(extentW, {
                onEnter: function(args) {
                    try {
                        appendMeasuredText(readUtf16(args[1], args[2].toInt32()), moduleName + '!GetTextExtentPoint32W');
                    } catch (e) {}
                }
            });
            send({type: 'log', msg: 'hooked ' + moduleName + '!GetTextExtentPoint32W capture'});
        }
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
                var text = readUtf16(args[1], count);
                var translated = handleWideText(text, args, 1, 2, 'user32!DrawTextW');
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
                if (!drawTextWFn) return;
                var count = args[2].toInt32();
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

var multiByteToWideChar = findExport('kernel32.dll', 'MultiByteToWideChar');
if (multiByteToWideChar) {
    Interceptor.attach(multiByteToWideChar, {
        onEnter: function(args) {
            if (internalMbDepth > 0) {
                this.skip = true;
                return;
            }
            this.codePage = args[0].toUInt32();
            this.srcPtr = args[2];
            this.srcBytes = args[3].toInt32();
        },
        onLeave: function(_retval) {
            try {
                if (this.skip) return;
                if (this.codePage !== 932 && this.codePage !== 0) return;
                var text = scrubText(readCp932(this.srcPtr, this.srcBytes));
                if (!isNoise(text)) {
                    conversionCount++;
                    rememberMiss(text, 'kernel32!MultiByteToWideChar');
                }
            } catch (e) {}
        }
    });
    send({type: 'log', msg: 'hooked kernel32!MultiByteToWideChar capture'});
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
    var keys = Object.keys(measureBuffers);
    for (var i = 0; i < keys.length; i++) {
        var buf = measureBuffers[keys[i]];
        if (buf && buf.text && Date.now() - buf.last > 650) flushMeasureBuffer(keys[i], 'timer');
    }
    send({
        type: 'stats',
        replaced: replacedCount,
        missed: missedCount,
        conversions: conversionCount,
        measure_runs: measureRunCount
    });
}, 5000);

send({type: 'ready', count: Object.keys(translations).length});
