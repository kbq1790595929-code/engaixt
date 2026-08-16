'use strict';
// Realtime text hook — multi-language, multi-engine.
//
// Inject with:
//   GM_SRC_LANG     — source language filter  (ja/ko/ru/en/zh/auto)
//   GM_TRANSLATION_MAP — pre-loaded cache hits at JS layer
//
// Hooks: ExtTextOutW, DrawTextW, TextOutW (GDI)
//        mono_string_new (Unity Mono)
//        CreateFontIndirectW (font detection — CJK games using latin glyphs)

var srcLang = (typeof GM_SRC_LANG !== 'undefined') ? GM_SRC_LANG : 'auto';
var seen = {};  // text → true, avoid re-sending

// ── language filter ───────────────────────────────────────────────────────
var LANG_RANGES = {
    ja: [/[぀-ヿ一-鿿]/],                     // hiragana + katakana + kanji
    ko: [/[가-힯ᄀ-ᇿ㄰-㆏]/],        // hangul
    zh: [/[一-鿿㐀-䶿]/],                      // CJK
    ru: [/[Ѐ-ԯ]/],                                   // cyrillic
    en: [/[a-zA-Z]{3,}/],                                      // latin words (min 3 chars)
};

function needsTranslate(text) {
    if (!text || text.length < 2 || text.length > 500) return false;

    // Filter: only CJK / Hangul / Cyrillic chars (exclude pure ASCII UI strings)
    var hasLangChar = /[぀-ヿ一-鿿㐀-䶿가-힯ᄀ-ᇿ㄰-㆏Ѐ-ԯ]/.test(text);
    if (hasLangChar) return true;

    // English → target lang: treat long ASCII strings as translatable
    if (srcLang === 'en' && /[a-zA-Z]{4}/.test(text) && text.length > 5) return true;

    // auto mode: any text with letters is a candidate
    if (srcLang === 'auto') return /[A-Za-zÀ-ɏЀ-ԯ぀-ヿ一-鿿㐀-䶿가-힯]/.test(text);

    return false;
}

// ── API compat (Frida 16 → 17) ─────────────────────────────────────────────

function findAPI(moduleName, exportName) {
    try {
        var mod = Process.getModuleByName(moduleName);
        return mod.getExportByName(exportName);
    } catch(e) {}
    try {
        return findAPI(moduleName, exportName);
    } catch(e) {}
    try {
        return Module.getExportByName(moduleName, exportName);
    } catch(e) {}
    return null;
}

// ── helpers ───────────────────────────────────────────────────────────────

function readWide(ptr, count) {
    try {
        if (ptr.isNull()) return null;
        return count > 0 ? ptr.readUtf16String(count) : ptr.readUtf16String();
    } catch(e) { return null; }
}

function handleText(text, args, strIdx, lenIdx) {
    if (!needsTranslate(text)) return;

    if (!seen[text]) {
        seen[text] = true;
        send({type: 'new_text', text: text});
    }
}

// ── GDI hooks ─────────────────────────────────────────────────────────────

var GDI = ['gdi32.dll', 'gdi32full.dll'];
GDI.forEach(function(mod) {
    var addr = findAPI(mod, 'ExtTextOutW');
    if (!addr) return;
    Interceptor.attach(addr, { onEnter: function(args) {
        var c = args[5].toInt32();
        var text = readWide(args[4], c > 0 ? c : 0);
        if (text) handleText(text, args, 4, 5);
    }});

    addr = findAPI(mod, 'TextOutW');
    if (addr) Interceptor.attach(addr, { onEnter: function(args) {
        var c = args[3].toInt32();
        var text = readWide(args[2], c > 0 ? c : 0);
        if (text) handleText(text, args, 2, 3);
    }});
});

var user32 = findAPI('user32.dll', 'DrawTextW');
if (user32) Interceptor.attach(user32, { onEnter: function(args) {
    var text = readWide(args[1], args[2].toInt32());
    if (text) handleText(text, args, 1, 2);
}});

// ── Unity Mono hooks ──────────────────────────────────────────────────────

var monoMod = Process.enumerateModules().find(function(m) {
    return m.name.toLowerCase().indexOf('mono') >= 0;
});
if (monoMod) {
    var msn = findAPI(monoMod.name, 'mono_string_new');
    if (msn) Interceptor.attach(msn, {
        onEnter: function(args) { this.ptr = args[1]; },
        onLeave: function(retval) {
            if (!this.ptr || this.ptr.isNull() || retval.isNull()) return;
            try {
                var text = this.ptr.readUtf8String();
                if (!needsTranslate(text)) return;
                if (!seen[text]) {
                    seen[text] = true;
                    send({type: 'new_text', text: text});
                }
            } catch(e) {}
        }
    });
    send({type: 'log', msg: 'Hooked Mono: ' + monoMod.name});

    var tmpDll = Process.enumerateModules().find(function(m) {
        return m.name.toLowerCase().indexOf('tmpro') >= 0;
    });
    if (tmpDll) send({type: 'log', msg: 'TMP: ' + tmpDll.name});
}

var hookNames = ['ExtTextOutW', 'TextOutW', 'DrawTextW'];
if (monoMod) hookNames.push('mono_string_new');
send({type: 'ready', hooks: hookNames, src_lang: srcLang});
