'use strict';

/*
 * D3D11 presentation-time GDI overlay.
 *
 * This draws translated text from inside the game process, after the swapchain
 * presents a frame. It intentionally uses a normal Windows CJK font instead of
 * GameMaker's built-in bitmap font atlas.
 */

var overlayItems = (typeof GM_OVERLAY_ITEMS !== 'undefined') ? GM_OVERLAY_ITEMS : [
    { key: 'PLAY', text: '开始游戏', x: 0.735, y: 0.875, size: 32, width: 230, height: 60 },
    { key: 'SETTINGS', text: '设置', x: 0.748, y: 0.955, size: 28, width: 240, height: 58 },
    { key: 'CREDITS', text: '制作人员', x: 0.748, y: 1.035, size: 25, width: 240, height: 52 },
    { key: 'PATCH NOTES', text: '更新日志', x: 0.748, y: 1.115, size: 25, width: 250, height: 52 },
    { key: 'QUIT', text: '退出', x: 0.748, y: 1.195, size: 25, width: 210, height: 52 }
];
var windowTitle = (typeof GM_WINDOW_TITLE !== 'undefined') ? GM_WINDOW_TITLE : 'Voidigo';

// ═══════════════════════════════════════════════════════════════
// Runtime text capture layer — hooks GameMaker internal draw
// functions to capture English text + screen coordinates per frame.
// ═══════════════════════════════════════════════════════════════

var moduleBase = Process.enumerateModules()[0].base;
var translationMap = (typeof GM_TRANSLATION_MAP !== 'undefined') ? GM_TRANSLATION_MAP : {};
var internalDrawHooks = (typeof GM_INTERNAL_DRAW_HOOKS !== 'undefined') ? GM_INTERNAL_DRAW_HOOKS : [
    { name: 'internal_draw_text', rva: 0x3a07d90, textIndex: 2 },
    { name: 'internal_draw_text_color', rva: 0x3a07fe0, textIndex: 2 },
    { name: 'internal_draw_text_transformed', rva: 0x3a08220, textIndex: 2 },
    { name: 'internal_draw_text_transformed_color', rva: 0x3a085b0, textIndex: 2 }
];
var capturedTexts = [];       // [{text, x, y, w, h}] — cleared each frame
var capturedFrameCount = 0;
var lastCaptureFlush = 0;

function isPtrReadable(p) {
    if (!p || p.isNull()) return false;
    try {
        var r = Process.findRangeByAddress(p);
        return !!r && r.protection.indexOf('r') !== -1;
    } catch (e) { return false; }
}

function cleanStr(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/\0+$/g, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
}

function looksLikeText(s) {
    s = cleanStr(s);
    if (s.length < 2 || s.length > 200) return false;
    if (!/[A-Za-z㐀-鿿぀-ヿ]/.test(s)) return false;
    if (/[\\/](steamapps|windows|program files|appdata)[\\/]/i.test(s)) return false;
    if (/^(gml_|obj_|spr_|snd_|scr_|rm_|bg_|shd_|font_|global\.|argument)/i.test(s)) return false;
    if (/^[0-9a-f]{16,}$/i.test(s)) return false;
    return true;
}

function readUtf8(p, maxLen) {
    if (!isPtrReadable(p)) return null;
    try { var s = cleanStr(p.readUtf8String(maxLen || 300)); return looksLikeText(s) ? s : null; } catch (e) { return null; }
}

function readAnsi(p, maxLen) {
    if (!isPtrReadable(p)) return null;
    try { var s = cleanStr(p.readCString(maxLen || 300)); return looksLikeText(s) ? s : null; } catch (e) { return null; }
}

function readFloatArg(v) {
    try {
        var bits = v.toUInt32();
        var buf = new ArrayBuffer(4);
        var view = new DataView(buf);
        view.setUint32(0, bits, true);
        return view.getFloat32(0, true);
    } catch (e) { return null; }
}

function installInternalDrawHook(spec) {
    var addr = moduleBase.add(spec.rva);
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                var text = readUtf8(args[spec.textIndex], 500) || readAnsi(args[spec.textIndex], 500);
                if (!text) return;
                var x = readFloatArg(args[0]);
                var y = readFloatArg(args[1]);
                if (x === null || y === null) return;
                // rough width/height estimation — will refine from draw args
                var w = null, h = null;
                try { w = readFloatArg(args[2]); } catch (e) {}
                try { h = readFloatArg(args[3]); } catch (e) {}
                capturedTexts.push({ text: text, x: x, y: y, w: w, h: h });
            }
        });
        report({ type: 'gm-hooked', name: spec.name, address: addr.toString(), rva: '0x' + spec.rva.toString(16) });
    } catch (e) {
        report({ type: 'gm-hook-error', name: spec.name, msg: String(e) });
    }
}

function installCaptureHooks() {
    // Internal draw hooks (direct text + coordinates, works for standard GM text)
    internalDrawHooks.forEach(installInternalDrawHook);
    // Draw wrapper hooks (context + coordinates via RValue argv)
    drawWrapperHooks.forEach(installDrawWrapperHook);
    // String convert hook (catches all RValue→C string conversions)
    installStringConvertHook();
    report({ type: 'capture-ready', hooks: internalDrawHooks.length + hookedDrawWrappers + 1, map_size: Object.keys(translationMap).length });
}

// ── Per-frame: flush captured texts, look up translations, return draw list ──
function resolveCapturedOverlayItems(drawWidth, drawHeight) {
    var items = [];
    var seen = {};
    for (var i = 0; i < capturedTexts.length; i++) {
        var cap = capturedTexts[i];
        if (seen[cap.text]) continue;
        seen[cap.text] = true;
        var translated = translationMap[cap.text];
        if (!translated || translated === cap.text) continue;
        // Convert GameMaker coordinates (assumed 1280x720) to overlay pixels
        var scaleX = drawWidth / 1280.0;
        var scaleY = drawHeight / 720.0;
        var sx = Math.round(cap.x * scaleX);
        var sy = Math.round(cap.y * scaleY);
        var boxW = Math.round((cap.w || 180) * scaleX);
        var boxH = Math.round((cap.h || 48) * scaleY);
        if (sx < 0 || sy < 0 || sx > drawWidth * 2 || sy > drawHeight * 2) continue;
        items.push({
            text: translated,
            x: sx,
            y: sy,
            w: boxW,
            h: boxH,
            size: Math.max(14, Math.round(Math.min(boxH * 0.6, 28) * scaleX))
        });
    }
    capturedTexts.length = 0;
    capturedFrameCount++;
    return items;
}

// ═══════════════════════════════════════════════════════════════
// Draw wrapper + string_convert capture layer
// For games that bypass internal_draw_text (e.g. sprite-font titles).
// Hooks GM's draw_text wrappers for context + coordinates, and
// string_convert for the actual text string after RValue→C conversion.
// ═══════════════════════════════════════════════════════════════

var drawWrapperHooks = (typeof GM_DRAW_WRAPPER_HOOKS !== 'undefined') ? GM_DRAW_WRAPPER_HOOKS : [
    { name: 'draw_text', rva: 0x3908490, stringIndex: 2 },
    { name: 'draw_text_ext', rva: 0x3908610, stringIndex: 2 },
    { name: 'draw_text_color', rva: 0x3908530, stringIndex: 2 },
    { name: 'draw_text_transformed', rva: 0x39089b0, stringIndex: 2 },
    { name: 'draw_text_ext_transformed', rva: 0x39087b0, stringIndex: 2 },
    { name: 'draw_text_transformed_color', rva: 0x3908a80, stringIndex: 2 }
];
var stringConvertRva = (typeof GM_STRING_CONVERT_RVA !== 'undefined') ? GM_STRING_CONVERT_RVA : 0x38ea320;
var drawContextByThread = {};
var hookedDrawWrappers = 0;

function readRValueAsDouble(rvPtr) {
    try { return rvPtr.readDouble(); } catch (e) { return null; }
}

function installDrawWrapperHook(spec) {
    var addr = moduleBase.add(spec.rva);
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                var t = Process.getCurrentThreadId().toString();
                var argc = 0;
                try { argc = args[3].toInt32(); } catch (e) {}
                var argv = args[4];
                var x = null, y = null;
                if (!argv.isNull() && argc > 0) {
                    x = readRValueAsDouble(argv);              // argv[0]
                    if (argc > 1) y = readRValueAsDouble(argv.add(16)); // argv[1]
                }
                drawContextByThread[t] = {
                    name: spec.name,
                    stringIndex: spec.stringIndex,
                    argc: argc,
                    argv: argv,
                    x: x,
                    y: y
                };
            },
            onLeave: function () {
                // Keep context alive briefly for string_convert which fires
                // synchronously inside the draw call. Clear after a short delay.
                var t = Process.getCurrentThreadId().toString();
                // Don't clear immediately — string_convert runs inside and needs it
            }
        });
        hookedDrawWrappers++;
        report({ type: 'gm-draw-hooked', name: spec.name, address: addr.toString(), rva: '0x' + spec.rva.toString(16) });
    } catch (e) {
        report({ type: 'gm-draw-error', name: spec.name, msg: String(e) });
    }
}

function installStringConvertHook() {
    var addr = moduleBase.add(stringConvertRva);
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                var t = Process.getCurrentThreadId().toString();
                this.ctx = drawContextByThread[t] || null;
                if (!this.ctx) return;
                this.out0 = args[0];
                this.out1 = args[1];
                this.out2 = args[2];
            },
            onLeave: function () {
                var ctx = this.ctx;
                if (!ctx) return;
                // Read text from each output pointer
                var ptrs = [this.out0, this.out1, this.out2];
                for (var i = 0; i < ptrs.length; i++) {
                    var text = readUtf8(ptrs[i], 400) || readAnsi(ptrs[i], 400);
                    if (text) {
                        capturedTexts.push({
                            text: text,
                            x: ctx.x || 0,
                            y: ctx.y || 0,
                            w: null,
                            h: null
                        });
                        break; // one good text per conversion is enough
                    }
                }
            }
        });
        report({ type: 'gm-sc-hooked', address: addr.toString(), rva: '0x' + stringConvertRva.toString(16) });
    } catch (e) {
        report({ type: 'gm-sc-error', msg: String(e) });
    }
}

// Extend installCaptureHooks to also install draw wrappers + string_convert

var maxReports = 80;
var reports = 0;
var presentInstalled = {};
var factoryInstalled = {};
var hwndFromSwapchain = ptr(0);
var presentFrames = 0;
var drawFrames = 0;
var framesBySwapchain = {};
var fontCache = {};
var d2dFactory = ptr(0);
var dwriteFactory = ptr(0);
var d2dTargets = {};
var textFormatCache = {};
var d2dAvailable = true;
var resizeInstalled = {};
var sizeReports = {};

function report(payload) {
    if (reports >= maxReports && payload.type !== 'stats') {
        return;
    }
    reports++;
    send(payload);
}

function api(moduleName, name, ret, args) {
    var p = findExport(moduleName, name);
    if (!p) {
        throw new Error('missing export ' + moduleName + '!' + name);
    }
    var abi = Process.arch === 'ia32' ? 'stdcall' : 'default';
    return new NativeFunction(p, ret, args, abi);
}

function findExport(moduleName, exportName) {
    try {
        if (typeof Process.getModuleByName === 'function') {
            var moduleObj = Process.getModuleByName(moduleName);
            if (moduleObj) {
                if (typeof moduleObj.findExportByName === 'function') {
                    var moduleAddr = moduleObj.findExportByName(exportName);
                    if (moduleAddr) {
                        return moduleAddr;
                    }
                }
                if (typeof moduleObj.getExportByName === 'function') {
                    try {
                        return moduleObj.getExportByName(exportName);
                    } catch (e) {}
                }
            }
        }
    } catch (e) {}

    try {
        if (typeof Module !== 'undefined' && typeof Module.findExportByName === 'function') {
            var legacyAddr = Module.findExportByName(moduleName, exportName);
            if (legacyAddr) {
                return legacyAddr;
            }
        }
    } catch (e) {}

    try {
        if (typeof Module !== 'undefined' && typeof Module.findGlobalExportByName === 'function') {
            return Module.findGlobalExportByName(exportName);
        }
    } catch (e) {}

    return null;
}

var FindWindowW = api('user32.dll', 'FindWindowW', 'pointer', ['pointer', 'pointer']);
var GetClientRect = api('user32.dll', 'GetClientRect', 'bool', ['pointer', 'pointer']);
var GetDC = api('user32.dll', 'GetDC', 'pointer', ['pointer']);
var ReleaseDC = api('user32.dll', 'ReleaseDC', 'int', ['pointer', 'pointer']);
var IsWindow = api('user32.dll', 'IsWindow', 'bool', ['pointer']);
var GetDeviceCaps = api('gdi32.dll', 'GetDeviceCaps', 'int', ['pointer', 'int']);
var SetBkMode = api('gdi32.dll', 'SetBkMode', 'int', ['pointer', 'int']);
var SetTextColor = api('gdi32.dll', 'SetTextColor', 'uint32', ['pointer', 'uint32']);
var DrawTextW = api('user32.dll', 'DrawTextW', 'int', ['pointer', 'pointer', 'int', 'pointer', 'uint32']);
var CreateFontW = api('gdi32.dll', 'CreateFontW', 'pointer', [
    'int', 'int', 'int', 'int', 'int', 'uint32', 'uint32', 'uint32',
    'uint32', 'uint32', 'uint32', 'uint32', 'uint32', 'pointer'
]);
var SelectObject = api('gdi32.dll', 'SelectObject', 'pointer', ['pointer', 'pointer']);
var DeleteObject = api('gdi32.dll', 'DeleteObject', 'bool', ['pointer']);
var CreateSolidBrush = api('gdi32.dll', 'CreateSolidBrush', 'pointer', ['uint32']);
var FillRect = api('user32.dll', 'FillRect', 'int', ['pointer', 'pointer', 'pointer']);
var LoadLibraryW = api('kernel32.dll', 'LoadLibraryW', 'pointer', ['pointer']);

var titlePtr = Memory.allocUtf16String(windowTitle);
var facePtr = Memory.allocUtf16String('Microsoft YaHei UI');
var DT_CENTER = 0x00000001;
var DT_VCENTER = 0x00000004;
var DT_SINGLELINE = 0x00000020;
var DT_NOCLIP = 0x00000100;
var TRANSPARENT = 1;
var LOGPIXELSX = 88;
var LOGPIXELSY = 90;
var CLEARTYPE_QUALITY = 5;
var DEFAULT_CHARSET = 1;
var FW_BOLD = 700;
var D2D1_FACTORY_TYPE_SINGLE_THREADED = 0;
var DWRITE_FACTORY_TYPE_SHARED = 0;
var D2D1_RENDER_TARGET_TYPE_DEFAULT = 0;
var DXGI_FORMAT_UNKNOWN = 0;
var D2D1_ALPHA_MODE_IGNORE = 3;
var D2D1_ALPHA_MODE_PREMULTIPLIED = 1;
var D2D1_DRAW_TEXT_OPTIONS_NONE = 0;
var DWRITE_MEASURING_MODE_NATURAL = 0;
var D2D1_TEXT_ANTIALIAS_MODE_GRAYSCALE = 2;
var DWRITE_FONT_WEIGHT_BOLD = 700;
var DWRITE_FONT_STYLE_NORMAL = 0;
var DWRITE_FONT_STRETCH_NORMAL = 5;
var DWRITE_TEXT_ALIGNMENT_CENTER = 1;
var DWRITE_PARAGRAPH_ALIGNMENT_CENTER = 1;
var DWRITE_WORD_WRAPPING_NO_WRAP = 1;

var IID_IDXGISurface = makeGuid('cafcb56c-6ac3-4889-bf47-9e23bbd260ec');
var IID_ID2D1Factory = makeGuid('06152247-6f50-465a-9245-118bfd3b6007');
var IID_IDWriteFactory = makeGuid('b859ee5a-d838-4b5b-a2e8-1adc7d93db48');

function rgb(r, g, b) {
    return (r & 0xff) | ((g & 0xff) << 8) | ((b & 0xff) << 16);
}

function rectAlloc(left, top, right, bottom) {
    var r = Memory.alloc(16);
    r.add(0).writeS32(left);
    r.add(4).writeS32(top);
    r.add(8).writeS32(right);
    r.add(12).writeS32(bottom);
    return r;
}

function rectFAlloc(left, top, right, bottom) {
    var r = Memory.alloc(16);
    r.add(0).writeFloat(left);
    r.add(4).writeFloat(top);
    r.add(8).writeFloat(right);
    r.add(12).writeFloat(bottom);
    return r;
}

function colorFAlloc(r, g, b, a) {
    var c = Memory.alloc(16);
    c.add(0).writeFloat(r);
    c.add(4).writeFloat(g);
    c.add(8).writeFloat(b);
    c.add(12).writeFloat(a);
    return c;
}

function makeGuid(text) {
    var m = /^([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{2})([0-9a-f]{2})-([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(text);
    if (!m) {
        throw new Error('bad guid: ' + text);
    }
    var p = Memory.alloc(16);
    p.add(0).writeU32(parseInt(m[1], 16));
    p.add(4).writeU16(parseInt(m[2], 16));
    p.add(6).writeU16(parseInt(m[3], 16));
    for (var i = 0; i < 8; i++) {
        p.add(8 + i).writeU8(parseInt(m[4 + i], 16));
    }
    return p;
}

function hresultFailed(hr) {
    return (hr | 0) < 0;
}

function vmethod(comObject, index, ret, args) {
    var method = comObject.readPointer().add(Process.pointerSize * index).readPointer();
    var abi = Process.arch === 'ia32' ? 'stdcall' : 'default';
    return new NativeFunction(method, ret, args, abi);
}

function comRelease(comObject) {
    if (!comObject || comObject.isNull()) {
        return;
    }
    try {
        vmethod(comObject, 2, 'uint32', ['pointer'])(comObject);
    } catch (e) {}
}

function releaseD2DTargetForSwapchain(swapchain) {
    var key = swapchain ? swapchain.toString() : '';
    var entry = d2dTargets[key];
    if (!entry) {
        return;
    }
    comRelease(entry.textBrush);
    comRelease(entry.shadowBrush);
    comRelease(entry.boxBrush);
    comRelease(entry.rt);
    delete d2dTargets[key];
    report({ type: 'd2d-target-released', swapchain: key });
}

function releaseAllD2DTargets() {
    Object.keys(d2dTargets).forEach(function (key) {
        var entry = d2dTargets[key];
        comRelease(entry.textBrush);
        comRelease(entry.shadowBrush);
        comRelease(entry.boxBrush);
        comRelease(entry.rt);
        delete d2dTargets[key];
    });
    report({ type: 'd2d-targets-cleared' });
}

function getRectWidth(rect) {
    return rect.add(8).readS32() - rect.add(0).readS32();
}

function getRectHeight(rect) {
    return rect.add(12).readS32() - rect.add(4).readS32();
}

function isValidHwnd(hwnd) {
    try {
        return !!hwnd && !hwnd.isNull() && IsWindow(hwnd);
    } catch (e) {
        return false;
    }
}

function getTargetHwnd() {
    if (isValidHwnd(hwndFromSwapchain)) {
        return hwndFromSwapchain;
    }
    try {
        var hwnd = FindWindowW(ptr(0), titlePtr);
        if (isValidHwnd(hwnd)) {
            return hwnd;
        }
    } catch (e) {}
    return ptr(0);
}

function getWindowDpiScale(hwnd) {
    var scale = { x: 1.0, y: 1.0, dpiX: 96, dpiY: 96 };
    if (!isValidHwnd(hwnd)) {
        return scale;
    }
    var hdc = GetDC(hwnd);
    if (!hdc || hdc.isNull()) {
        return scale;
    }
    try {
        var dpiX = GetDeviceCaps(hdc, LOGPIXELSX);
        var dpiY = GetDeviceCaps(hdc, LOGPIXELSY);
        if (dpiX >= 72 && dpiX <= 768) {
            scale.dpiX = dpiX;
            scale.x = dpiX / 96.0;
        }
        if (dpiY >= 72 && dpiY <= 768) {
            scale.dpiY = dpiY;
            scale.y = dpiY / 96.0;
        }
    } catch (e) {
        report({ type: 'dpi-error', msg: String(e) });
    } finally {
        ReleaseDC(hwnd, hdc);
    }
    return scale;
}

function getFont(size) {
    var key = String(size);
    if (fontCache[key]) {
        return fontCache[key];
    }
    var font = CreateFontW(
        -size, 0, 0, 0, FW_BOLD, 0, 0, 0,
        DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, facePtr
    );
    if (!font.isNull()) {
        fontCache[key] = font;
    }
    return font;
}

function fillBox(hdc, left, top, right, bottom) {
    var brush = CreateSolidBrush(rgb(18, 0, 19));
    if (brush.isNull()) {
        return;
    }
    var r = rectAlloc(left, top, right, bottom);
    FillRect(hdc, r, brush);
    DeleteObject(brush);
}

function drawCentered(hdc, text, left, top, right, bottom, size) {
    var font = getFont(size);
    var oldFont = ptr(0);
    if (!font.isNull()) {
        oldFont = SelectObject(hdc, font);
    }
    SetBkMode(hdc, TRANSPARENT);
    var textPtr = Memory.allocUtf16String(text);
    var flags = DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOCLIP;

    SetTextColor(hdc, rgb(34, 0, 24));
    DrawTextW(hdc, textPtr, -1, rectAlloc(left + 2, top + 2, right + 2, bottom + 2), flags);
    SetTextColor(hdc, rgb(255, 244, 255));
    DrawTextW(hdc, textPtr, -1, rectAlloc(left, top, right, bottom), flags);

    if (!oldFont.isNull()) {
        SelectObject(hdc, oldFont);
    }
}

function drawOverlay() {
    var hwnd = getTargetHwnd();
    if (hwnd.isNull()) {
        return;
    }
    var client = Memory.alloc(16);
    if (!GetClientRect(hwnd, client)) {
        return;
    }
    var width = getRectWidth(client);
    var height = getRectHeight(client);
    if (width < 100 || height < 100) {
        return;
    }
    var hdc = GetDC(hwnd);
    if (hdc.isNull()) {
        return;
    }
    try {
        // ── 1) 运行时捕获的动态文本（优先） ──
        var captured = resolveCapturedOverlayItems(width, height);
        captured.forEach(function (item) {
            var left = item.x - Math.floor(item.w / 2);
            var top = item.y - Math.floor(item.h / 2);
            var right = left + item.w;
            var bottom = top + item.h;
            if (right <= 0 || bottom <= 0 || left >= width || top >= height) return;
            fillBox(hdc, left, top, right, bottom);
            drawCentered(hdc, item.text, left, top, right, bottom, item.size || 24);
        });

        // ── 2) 硬编码静态项（无捕获文本时兜底） ──
        if (captured.length === 0) {
            var scale = Math.min(width / 1280.0, height / 720.0);
            overlayItems.forEach(function (item) {
                var x = Math.round(width * item.x);
                var y = Math.round(height * item.y);
                var boxW = Math.round((item.width || 180) * scale);
                var boxH = Math.round((item.height || 52) * scale);
                var size = Math.max(16, Math.round((item.size || 28) * scale));
                var left = x - Math.floor(boxW / 2);
                var top = y - Math.floor(boxH / 2);
                var right = x + Math.floor(boxW / 2);
                var bottom = y + Math.floor(boxH / 2);
                fillBox(hdc, left, top, right, bottom);
                drawCentered(hdc, item.text, left, top, right, bottom, size);
            });
        }
        drawFrames++;
    } catch (e) {
        report({ type: 'draw-error', msg: String(e) });
    } finally {
        ReleaseDC(hwnd, hdc);
    }
}

function loadSystemLibrary(name) {
    try {
        return LoadLibraryW(Memory.allocUtf16String(name));
    } catch (e) {
        return ptr(0);
    }
}

function initD2D() {
    if (!d2dAvailable) {
        return false;
    }
    if (!d2dFactory.isNull() && !dwriteFactory.isNull()) {
        return true;
    }
    loadSystemLibrary('d2d1.dll');
    loadSystemLibrary('dwrite.dll');
    var d2dCreate = findExport('d2d1.dll', 'D2D1CreateFactory');
    var dwriteCreate = findExport('dwrite.dll', 'DWriteCreateFactory');
    if (!d2dCreate || !dwriteCreate) {
        report({ type: 'd2d-unavailable', reason: 'missing exports' });
        d2dAvailable = false;
        return false;
    }
    var abi = Process.arch === 'ia32' ? 'stdcall' : 'default';
    var D2D1CreateFactory = new NativeFunction(d2dCreate, 'int', ['uint32', 'pointer', 'pointer', 'pointer'], abi);
    var DWriteCreateFactory = new NativeFunction(dwriteCreate, 'int', ['uint32', 'pointer', 'pointer'], abi);

    var ppD2D = Memory.alloc(Process.pointerSize);
    ppD2D.writePointer(ptr(0));
    var hr = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, IID_ID2D1Factory, ptr(0), ppD2D);
    if (hresultFailed(hr)) {
        report({ type: 'd2d-error', step: 'D2D1CreateFactory', hr: hr });
        d2dAvailable = false;
        return false;
    }
    d2dFactory = ppD2D.readPointer();

    var ppDW = Memory.alloc(Process.pointerSize);
    ppDW.writePointer(ptr(0));
    hr = DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, IID_IDWriteFactory, ppDW);
    if (hresultFailed(hr)) {
        report({ type: 'd2d-error', step: 'DWriteCreateFactory', hr: hr });
        d2dAvailable = false;
        return false;
    }
    dwriteFactory = ppDW.readPointer();
    report({ type: 'd2d-ready', d2d_factory: d2dFactory.toString(), dwrite_factory: dwriteFactory.toString() });
    return true;
}

function renderTargetProperties() {
    var props = Memory.alloc(28);
    props.add(0).writeU32(D2D1_RENDER_TARGET_TYPE_DEFAULT);
    props.add(4).writeU32(DXGI_FORMAT_UNKNOWN);
    props.add(8).writeU32(D2D1_ALPHA_MODE_IGNORE);
    props.add(12).writeFloat(96.0);
    props.add(16).writeFloat(96.0);
    props.add(20).writeU32(0);
    props.add(24).writeU32(0);
    return props;
}

function createSolidBrush(rt, r, g, b, a) {
    var ppBrush = Memory.alloc(Process.pointerSize);
    ppBrush.writePointer(ptr(0));
    var createBrush = vmethod(rt, 8, 'int', ['pointer', 'pointer', 'pointer', 'pointer']);
    var hr = createBrush(rt, colorFAlloc(r, g, b, a), ptr(0), ppBrush);
    if (hresultFailed(hr)) {
        report({ type: 'd2d-error', step: 'CreateSolidColorBrush', hr: hr });
        return ptr(0);
    }
    return ppBrush.readPointer();
}

function createD2DRenderTarget(swapchain) {
    var key = swapchain.toString();
    if (d2dTargets[key]) {
        return d2dTargets[key];
    }
    if (!initD2D()) {
        return null;
    }

    var ppSurface = Memory.alloc(Process.pointerSize);
    ppSurface.writePointer(ptr(0));
    var getBuffer = vmethod(swapchain, 9, 'int', ['pointer', 'uint32', 'pointer', 'pointer']);
    var hr = getBuffer(swapchain, 0, IID_IDXGISurface, ppSurface);
    if (hresultFailed(hr)) {
        report({ type: 'd2d-error', step: 'IDXGISwapChain.GetBuffer', hr: hr });
        return null;
    }
    var surface = ppSurface.readPointer();
    var ppRt = Memory.alloc(Process.pointerSize);
    ppRt.writePointer(ptr(0));
    var createRt = vmethod(d2dFactory, 15, 'int', ['pointer', 'pointer', 'pointer', 'pointer']);
    hr = createRt(d2dFactory, surface, renderTargetProperties(), ppRt);
    comRelease(surface);
    if (hresultFailed(hr)) {
        report({ type: 'd2d-error', step: 'CreateDxgiSurfaceRenderTarget', hr: hr });
        return null;
    }
    var rt = ppRt.readPointer();
    try {
        vmethod(rt, 34, 'void', ['pointer', 'uint32'])(rt, D2D1_TEXT_ANTIALIAS_MODE_GRAYSCALE);
    } catch (e) {}
    var entry = {
        rt: rt,
        textBrush: createSolidBrush(rt, 1.0, 0.96, 1.0, 1.0),
        shadowBrush: createSolidBrush(rt, 0.09, 0.0, 0.07, 1.0),
        boxBrush: createSolidBrush(rt, 0.02, 0.0, 0.025, 1.0)
    };
    d2dTargets[key] = entry;
    report({ type: 'd2d-target-created', swapchain: key, target: rt.toString() });
    return entry;
}

function createTextFormat(size) {
    var key = String(size);
    if (textFormatCache[key]) {
        return textFormatCache[key];
    }
    if (!initD2D()) {
        return ptr(0);
    }
    var ppFormat = Memory.alloc(Process.pointerSize);
    ppFormat.writePointer(ptr(0));
    var createTextFormat = vmethod(dwriteFactory, 15, 'int', [
        'pointer', 'pointer', 'pointer', 'uint32', 'uint32', 'uint32', 'float', 'pointer', 'pointer'
    ]);
    var hr = createTextFormat(
        dwriteFactory,
        facePtr,
        ptr(0),
        DWRITE_FONT_WEIGHT_BOLD,
        DWRITE_FONT_STYLE_NORMAL,
        DWRITE_FONT_STRETCH_NORMAL,
        size,
        Memory.allocUtf16String('zh-cn'),
        ppFormat
    );
    if (hresultFailed(hr)) {
        report({ type: 'd2d-error', step: 'CreateTextFormat', hr: hr, size: size });
        return ptr(0);
    }
    var format = ppFormat.readPointer();
    try {
        vmethod(format, 3, 'int', ['pointer', 'uint32'])(format, DWRITE_TEXT_ALIGNMENT_CENTER);
        vmethod(format, 4, 'int', ['pointer', 'uint32'])(format, DWRITE_PARAGRAPH_ALIGNMENT_CENTER);
        vmethod(format, 5, 'int', ['pointer', 'uint32'])(format, DWRITE_WORD_WRAPPING_NO_WRAP);
    } catch (e) {}
    textFormatCache[key] = format;
    return format;
}

function drawD2DText(rt, text, format, brush, left, top, right, bottom) {
    if (!format || format.isNull() || !brush || brush.isNull()) {
        return;
    }
    var drawText = vmethod(rt, 27, 'void', ['pointer', 'pointer', 'uint32', 'pointer', 'pointer', 'pointer', 'uint32', 'uint32']);
    var textPtr = Memory.allocUtf16String(text);
    drawText(
        rt,
        textPtr,
        text.length,
        format,
        rectFAlloc(left, top, right, bottom),
        brush,
        D2D1_DRAW_TEXT_OPTIONS_NONE,
        DWRITE_MEASURING_MODE_NATURAL
    );
}

function getSwapchainSize(swapchain) {
    try {
        var desc = Memory.alloc(96);
        var getDesc = vmethod(swapchain, 12, 'int', ['pointer', 'pointer']);
        var hr = getDesc(swapchain, desc);
        if (!hresultFailed(hr)) {
            var width = desc.add(0).readU32();
            var height = desc.add(4).readU32();
            if (width >= 100 && height >= 100 && width <= 16384 && height <= 16384) {
                return { width: width, height: height, source: 'swapchain' };
            }
        }
    } catch (e) {}
    return null;
}

function drawD2DOverlay(swapchain) {
    var swapKey = swapchain.toString();
    framesBySwapchain[swapKey] = (framesBySwapchain[swapKey] || 0) + 1;
    if (framesBySwapchain[swapKey] < 90) {
        return false;
    }
    var entry = createD2DRenderTarget(swapchain);
    if (!entry || !entry.rt || entry.rt.isNull()) {
        return false;
    }
    var size = getSwapchainSize(swapchain);
    var width = size ? size.width : 1280;
    var height = size ? size.height : 720;
    var hwnd = getTargetHwnd();
    var client = Memory.alloc(16);
    var clientWidth = 0;
    var clientHeight = 0;
    if (!hwnd.isNull() && GetClientRect(hwnd, client)) {
        clientWidth = getRectWidth(client);
        clientHeight = getRectHeight(client);
    }
    var dpi = getWindowDpiScale(hwnd);
    var rawDrawWidth = clientWidth >= 100 ? clientWidth : width;
    var rawDrawHeight = clientHeight >= 100 ? clientHeight : height;
    var drawWidth = Math.round(rawDrawWidth / Math.max(0.25, dpi.x));
    var drawHeight = Math.round(rawDrawHeight / Math.max(0.25, dpi.y));
    if (Math.abs(drawWidth - width) <= 2 && Math.abs(drawHeight - height) <= 2) {
        drawWidth = width;
        drawHeight = height;
    }
    var reportKey = swapKey + ':' + width + 'x' + height + ':' + clientWidth + 'x' + clientHeight + ':' + dpi.dpiX + 'x' + dpi.dpiY + ':' + drawWidth + 'x' + drawHeight;
    if (!sizeReports[reportKey]) {
        sizeReports[reportKey] = true;
        report({
            type: 'overlay-size',
            swapchain: swapKey,
            width: width,
            height: height,
            client_width: clientWidth,
            client_height: clientHeight,
            dpi_x: dpi.dpiX,
            dpi_y: dpi.dpiY,
            draw_width: drawWidth,
            draw_height: drawHeight,
            source: size ? size.source : 'fallback'
        });
    }
    if (drawWidth < 100 || drawHeight < 100) {
        return false;
    }

    var rt = entry.rt;
    var beginDraw = vmethod(rt, 48, 'void', ['pointer']);
    var endDraw = vmethod(rt, 49, 'int', ['pointer', 'pointer', 'pointer']);
    var fillRectangle = vmethod(rt, 17, 'void', ['pointer', 'pointer', 'pointer']);
    beginDraw(rt);
    try {
        // ── 1) 运行时捕获的动态文本（优先） ──
        var captured = resolveCapturedOverlayItems(drawWidth, drawHeight);
        captured.forEach(function (item) {
            var left = item.x - Math.floor(item.w / 2);
            var top = item.y - Math.floor(item.h / 2);
            var right = left + item.w;
            var bottom = top + item.h;
            if (right <= 0 || bottom <= 0 || left >= drawWidth || top >= drawHeight) return;
            fillRectangle(rt, rectFAlloc(left, top, right, bottom), entry.boxBrush);
            var size = Math.max(14, item.size || 24);
            var format = createTextFormat(size);
            drawD2DText(rt, item.text, format, entry.shadowBrush, left + 2, top + 2, right + 2, bottom + 2);
            drawD2DText(rt, item.text, format, entry.textBrush, left, top, right, bottom);
        });

        // ── 2) 硬编码静态项（无捕获文本时兜底） ──
        if (captured.length === 0) {
            var scale = Math.min(drawWidth / 1280.0, drawHeight / 720.0);
            overlayItems.forEach(function (item) {
                var x = Math.round(drawWidth * item.x);
                var y = Math.round(drawHeight * item.y);
                var boxW = Math.round((item.width || 180) * scale);
                var boxH = Math.round((item.height || 52) * scale);
                var size = Math.max(16, Math.round((item.size || 28) * scale));
                var left = x - Math.floor(boxW / 2);
                var top = y - Math.floor(boxH / 2);
                var right = left + boxW;
                var bottom = top + boxH;
                if (right <= 0 || bottom <= 0 || left >= drawWidth || top >= drawHeight) return;
                fillRectangle(rt, rectFAlloc(left, top, right, bottom), entry.boxBrush);
                var format = createTextFormat(size);
                drawD2DText(rt, item.text, format, entry.shadowBrush, left + 2, top + 2, right + 2, bottom + 2);
                drawD2DText(rt, item.text, format, entry.textBrush, left, top, right, bottom);
            });
        }
    } catch (e) {
        report({ type: 'd2d-draw-error', msg: String(e) });
    }
    var tag1 = Memory.alloc(Process.pointerSize);
    var tag2 = Memory.alloc(Process.pointerSize);
    var hr = endDraw(rt, tag1, tag2);
    if (hresultFailed(hr)) {
        delete d2dTargets[swapchain.toString()];
        report({ type: 'd2d-error', step: 'EndDraw', hr: hr });
        return false;
    }
    drawFrames++;
    return true;
}

function readOutputWindowFromDesc(desc) {
    if (!desc || desc.isNull()) {
        return ptr(0);
    }
    try {
        var offset = Process.pointerSize === 4 ? 44 : 48;
        return desc.add(offset).readPointer();
    } catch (e) {
        return ptr(0);
    }
}

function installPresentHook(swapchain, source) {
    if (!swapchain || swapchain.isNull()) {
        return;
    }
    try {
        var vtbl = swapchain.readPointer();
        var present = vtbl.add(Process.pointerSize * 8).readPointer();
        var key = present.toString();
        if (presentInstalled[key]) {
            return;
        }
        presentInstalled[key] = true;
        Interceptor.attach(present, {
            onEnter: function (args) {
                this.swapchain = args[0];
                var ok = drawD2DOverlay(this.swapchain);
                this.drawn = ok;
            },
            onLeave: function () {
                presentFrames++;
                if (!this.drawn) {
                    drawOverlay();
                }
            }
        });
        report({ type: 'present-hooked', source: source, swapchain: swapchain.toString(), present: present.toString() });
        installResizeHooks(swapchain, source);
    } catch (e) {
        report({ type: 'present-error', source: source, msg: String(e), swapchain: swapchain ? swapchain.toString() : '' });
    }
}

function installResizeHooks(swapchain, source) {
    if (!swapchain || swapchain.isNull()) {
        return;
    }
    try {
        var vtbl = swapchain.readPointer();
        [
            { name: 'ResizeBuffers', index: 13 },
            { name: 'ResizeTarget', index: 14 }
        ].forEach(function (spec) {
            var method = vtbl.add(Process.pointerSize * spec.index).readPointer();
            var key = spec.name + ':' + method.toString();
            if (resizeInstalled[key]) {
                return;
            }
            resizeInstalled[key] = true;
            Interceptor.attach(method, {
                onEnter: function (args) {
                    releaseD2DTargetForSwapchain(args[0]);
                }
            });
            report({ type: 'resize-hooked', source: source, method: spec.name, address: method.toString() });
        });
    } catch (e) {
        report({ type: 'resize-error', source: source, msg: String(e), swapchain: swapchain.toString() });
    }
}

function hookD3D11CreateDeviceAndSwapChain() {
    var fn = findExport('d3d11.dll', 'D3D11CreateDeviceAndSwapChain');
    if (!fn) {
        return false;
    }
    Interceptor.attach(fn, {
        onEnter: function (args) {
            this.desc = args[7];
            this.ppSwapchain = args[8];
        },
        onLeave: function (retval) {
            if (retval.toInt32() < 0 || !this.ppSwapchain || this.ppSwapchain.isNull()) {
                return;
            }
            var hwnd = readOutputWindowFromDesc(this.desc);
            if (isValidHwnd(hwnd)) {
                hwndFromSwapchain = hwnd;
            }
            var swapchain = this.ppSwapchain.readPointer();
            report({ type: 'swapchain-created', source: 'D3D11CreateDeviceAndSwapChain', hwnd: hwnd.toString(), swapchain: swapchain.toString() });
            installPresentHook(swapchain, 'D3D11CreateDeviceAndSwapChain');
        }
    });
    report({ type: 'd3d11-hooked', address: fn.toString() });
    return true;
}

function installFactoryMethod(factory, source, spec) {
    if (!factory || factory.isNull()) {
        return;
    }
    try {
        var vtbl = factory.readPointer();
        var method = vtbl.add(Process.pointerSize * spec.index).readPointer();
        var key = source + ':' + spec.name + ':' + method.toString();
        if (factoryInstalled[key]) {
            return;
        }
        factoryInstalled[key] = true;
        Interceptor.attach(method, {
            onEnter: function (args) {
                this.desc = spec.descIndex >= 0 ? args[spec.descIndex] : ptr(0);
                this.hwnd = spec.hwndIndex >= 0 ? args[spec.hwndIndex] : ptr(0);
                this.ppSwapchain = args[spec.ppIndex];
            },
            onLeave: function (retval) {
                if (retval.toInt32() < 0 || !this.ppSwapchain || this.ppSwapchain.isNull()) {
                    return;
                }
                var hwnd = isValidHwnd(this.hwnd) ? this.hwnd : readOutputWindowFromDesc(this.desc);
                if (isValidHwnd(hwnd)) {
                    hwndFromSwapchain = hwnd;
                }
                var swapchain = this.ppSwapchain.readPointer();
                report({ type: 'swapchain-created', source: source + '.' + spec.name, hwnd: hwnd.toString(), swapchain: swapchain.toString() });
                installPresentHook(swapchain, source + '.' + spec.name);
            }
        });
        report({ type: 'factory-method-hooked', source: source, method: spec.name, factory: factory.toString(), address: method.toString() });
    } catch (e) {
        report({ type: 'factory-error', source: source + '.' + spec.name, msg: String(e), factory: factory ? factory.toString() : '' });
    }
}

function installFactoryCreateSwapChain(factory, source) {
    [
        { name: 'CreateSwapChain', index: 10, descIndex: 2, hwndIndex: -1, ppIndex: 3 },
        { name: 'CreateSwapChainForHwnd', index: 15, descIndex: 3, hwndIndex: 2, ppIndex: 6 },
        { name: 'CreateSwapChainForCoreWindow', index: 16, descIndex: 3, hwndIndex: -1, ppIndex: 5 },
        { name: 'CreateSwapChainForComposition', index: 24, descIndex: 2, hwndIndex: -1, ppIndex: 4 }
    ].forEach(function (spec) {
        installFactoryMethod(factory, source, spec);
    });
}

function hookFactoryExport(name, ppFactoryIndex) {
    var fn = findExport('dxgi.dll', name);
    if (!fn) {
        return false;
    }
    Interceptor.attach(fn, {
        onEnter: function (args) {
            this.ppFactory = args[ppFactoryIndex];
        },
        onLeave: function (retval) {
            if (retval.toInt32() < 0 || !this.ppFactory || this.ppFactory.isNull()) {
                return;
            }
            installFactoryCreateSwapChain(this.ppFactory.readPointer(), name);
        }
    });
    report({ type: 'dxgi-hooked', name: name, address: fn.toString() });
    return true;
}

function installHooksWhenReady() {
    var ok = false;
    try {
        ok = hookD3D11CreateDeviceAndSwapChain() || ok;
    } catch (e) {
        report({ type: 'hook-error', target: 'D3D11CreateDeviceAndSwapChain', msg: String(e) });
    }
    try {
        ok = hookFactoryExport('CreateDXGIFactory', 1) || ok;
        ok = hookFactoryExport('CreateDXGIFactory1', 1) || ok;
        ok = hookFactoryExport('CreateDXGIFactory2', 2) || ok;
    } catch (e) {
        report({ type: 'hook-error', target: 'CreateDXGIFactory', msg: String(e) });
    }
    return ok;
}

var attempts = 0;
var captureInstalled = false;
var timer = setInterval(function () {
    attempts++;
    if (installHooksWhenReady() || attempts > 80) {
        clearInterval(timer);
        if (!captureInstalled) {
            try { installCaptureHooks(); } catch (e) { report({ type: 'capture-error', msg: String(e) }); }
            captureInstalled = true;
        }
        report({ type: 'ready', attempts: attempts, title: windowTitle, item_count: overlayItems.length, map_size: Object.keys(translationMap).length });
    }
}, 100);

setInterval(function () {
    report({
        type: 'stats',
        present_frames: presentFrames,
        draw_frames: drawFrames,
        capture_frames: capturedFrameCount,
        capture_texts: capturedTexts.length,
        present_hooks: Object.keys(presentInstalled).length,
        factory_hooks: Object.keys(factoryInstalled).length,
        hwnd: hwndFromSwapchain.toString()
    });
}, 5000);
