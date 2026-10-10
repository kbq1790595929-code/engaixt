'use strict';

/*
 * RPG Maker XP / mkxp SDL_ttf runtime translation hook.
 *
 * The hook replaces text at the SDL_ttf boundary, just before the engine
 * rasterizes it into a texture. This keeps game data untouched and works for
 * mkxp builds that expose SDL_ttf symbols from the game executable.
 */

var TRANSLATION_MAP = (typeof RPGMAKER_MKXP_TRANSLATION_MAP !== 'undefined')
    ? RPGMAKER_MKXP_TRANSLATION_MAP
    : {};
var OPTIONS = (typeof RPGMAKER_MKXP_OPTIONS !== 'undefined')
    ? RPGMAKER_MKXP_OPTIONS
    : {};

var maxReadChars = OPTIONS.max_read_chars || 512;
var cjkFontPath = OPTIONS.cjk_font_path || '';
var cjkFontNames = OPTIONS.cjk_font_names || [];
var debugLog = !!OPTIONS.debug_log;
var seen = {};
var seenCount = 0;
var hitCount = 0;
var missCount = 0;
var eventLimit = OPTIONS.event_limit || 250;
var eventCount = 0;
var installedHooks = [];
var rubyInjected = false;
var rubyInjecting = false;
var rubyRuntimeReady = false;
var rubyInjectionAttempts = 0;
var rubyInjectionTimer = null;

function emit(payload) {
    if (eventCount >= eventLimit && payload.type !== 'ready' && payload.type !== 'stats') {
        return;
    }
    eventCount++;
    send(payload);
}

function normalizeText(text) {
    return String(text || '')
        .replace(/\u0000+$/g, '')
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n');
}

function hasDisplayLetters(text) {
    return /[A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/.test(text);
}

function findExport(name) {
    try {
        var direct = Module.findGlobalExportByName(name);
        if (direct) return direct;
    } catch (e) {}
    try {
        var direct2 = Module.getGlobalExportByName(name);
        if (direct2) return direct2;
    } catch (e) {}

    var modules = Process.enumerateModules();
    for (var i = 0; i < modules.length; i++) {
        var m = modules[i];
        try {
            if (m.findExportByName) {
                var addr = m.findExportByName(name);
                if (addr) return addr;
            }
        } catch (e) {}
        try {
            if (m.getExportByName) {
                var addr2 = m.getExportByName(name);
                if (addr2) return addr2;
            }
        } catch (e) {}
    }
    return null;
}

function lookupTranslation(text) {
    if (!text) return null;
    var direct = TRANSLATION_MAP[text];
    if (direct && direct !== text) return { translated: direct, mode: 'exact' };

    var trimmed = text.trim();
    if (trimmed && trimmed !== text) {
        var trimmedHit = TRANSLATION_MAP[trimmed];
        if (trimmedHit && trimmedHit !== trimmed) {
            return {
                translated: text.replace(trimmed, trimmedHit),
                mode: 'trim'
            };
        }
    }
    return null;
}

function readUtf8(ptrValue) {
    if (ptrValue.isNull()) return '';
    try {
        return normalizeText(ptrValue.readUtf8String(maxReadChars));
    } catch (e) {
        return '';
    }
}

function readUtf16(ptrValue) {
    if (ptrValue.isNull()) return '';
    try {
        return normalizeText(ptrValue.readUtf16String(maxReadChars));
    } catch (e) {
        return '';
    }
}

function maybeReportSeen(src, text, replacement) {
    var key = src + '\n' + text;
    if (seen[key]) return;
    seen[key] = true;
    seenCount++;
    if (!hasDisplayLetters(text)) return;
    if (replacement) {
        hitCount++;
        emit({
            type: 'hit',
            src: src,
            text: text,
            translated: replacement.translated,
            mode: replacement.mode
        });
    } else {
        missCount++;
        emit({ type: 'miss_text', src: src, text: text });
    }
}

function replaceUtf8Arg(args, src) {
    var text = readUtf8(args[1]);
    if (!text) return null;
    var replacement = lookupTranslation(text);
    maybeReportSeen(src, text, replacement);
    if (!replacement) return null;
    var memory = Memory.allocUtf8String(replacement.translated);
    args[1] = memory;
    return memory;
}

function replaceUtf16Arg(args, src) {
    var text = readUtf16(args[1]);
    if (!text) return null;
    var replacement = lookupTranslation(text);
    maybeReportSeen(src, text, replacement);
    if (!replacement) return null;
    var memory = Memory.allocUtf16String(replacement.translated);
    args[1] = memory;
    return memory;
}

function readCString(ptrValue) {
    if (ptrValue.isNull()) return '';
    try {
        return ptrValue.readCString(1024) || '';
    } catch (e) {
        return '';
    }
}

function hookFontOpen(name) {
    if (!cjkFontPath) return;
    var addr = findExport(name);
    if (!addr) return;
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                var path = readCString(args[0]);
                if (!path) return;
                if (/HigashiOme|Terminus|Gothic|\.ttf|\.otf/i.test(path)) {
                    this.originalPath = path;
                    this.fontPath = Memory.allocUtf8String(cjkFontPath);
                    args[0] = this.fontPath;
                    emit({ type: 'font_replace', src: name, from: path, to: cjkFontPath });
                }
            }
        });
        installedHooks.push(name);
    } catch (e) {
        emit({ type: 'hook_error', name: name, message: String(e) });
    }
}

function rubyLiteral(value) {
    return JSON.stringify(String(value || ''));
}

function rubyArrayLiteral(values) {
    return '[' + values.map(function (value) { return rubyLiteral(value); }).join(', ') + ']';
}

function buildRubyPatch() {
    var pairs = [];
    Object.keys(TRANSLATION_MAP).forEach(function (key) {
        var value = TRANSLATION_MAP[key];
        if (!key || !value || key === value) return;
        pairs.push([key, value]);
    });
    if (pairs.length === 0) return '';

    var lines = [];
    lines.push('# encoding: UTF-8');
    lines.push('begin');
    lines.push('  $gt_runtime_debug_log = ' + (debugLog ? 'true' : 'false'));
    lines.push('  $gt_runtime_font_names = ' + rubyArrayLiteral(cjkFontNames));
    lines.push('  $gt_runtime_translation_map ||= {}');
    pairs.forEach(function (pair) {
        lines.push('  $gt_runtime_translation_map[' + rubyLiteral(pair[0]) + '] = ' + rubyLiteral(pair[1]));
    });
    lines.push('  $gt_runtime_clean_text ||= proc do |value|');
    lines.push('    value.to_s.gsub("\\\\n", "\\n").split(/\\r?\\n/).map do |raw_line|');
    lines.push('      line = raw_line.to_s.strip');
    lines.push('      line = line.sub(/\\A\\$+\\s*/, "")');
    lines.push('      loop do');
    lines.push('        changed = line.sub!(/\\A@[A-Za-z0-9_]+\\s*/, "")');
    lines.push('        break unless changed');
    lines.push('        line = line.lstrip');
    lines.push('      end');
    lines.push('      line = line.gsub(/\\\\[A-Za-z]{1,3}\\s*\\[[^\\]]*\\]/, "")');
    lines.push('      line = line.gsub(/\\\\[\\.\\|!>\\^<>{}]/, "")');
    lines.push('      line = line.gsub(/\\\\FS\\[\\d+\\]/i, "")');
    lines.push('      line = line.gsub(/\\\\[A-Za-z]+/, "")');
    lines.push('      line.strip');
    lines.push('    end.reject {|line| line.empty? }.join("\\n")');
    lines.push('  end');
    lines.push('  $gt_runtime_translate_text ||= proc do |value|');
    lines.push('    text = value.to_s');
    lines.push('    mapped = $gt_runtime_translation_map[text]');
    lines.push('    unless mapped');
    lines.push('      stripped = text.strip');
    lines.push('      if stripped != text');
    lines.push('        hit = $gt_runtime_translation_map[stripped]');
    lines.push('        mapped = text.sub(stripped, hit) if hit');
    lines.push('      end');
    lines.push('    end');
    lines.push('    unless mapped');
    lines.push('      clean = $gt_runtime_clean_text.call(text)');
    lines.push('      hit = $gt_runtime_translation_map[clean] if clean && !clean.empty?');
    lines.push('      if hit');
    lines.push('        mapped = (clean != text && text.include?(clean)) ? text.sub(clean, hit) : hit');
    lines.push('      end');
    lines.push('    end');
    lines.push('    mapped');
    lines.push('  end');
    lines.push('  $gt_runtime_measure_text_rect ||= proc do |bitmap, value|');
    lines.push('    mapped = $gt_runtime_translate_text.call(value)');
    lines.push('    text = mapped ? String.new(mapped) : value');
    lines.push('    rect = bitmap.gt_original_text_size(text)');
    lines.push('    begin');
    lines.push('      raw = text.to_s');
    lines.push('      if raw.length > 1 && raw =~ /[\\u3040-\\u30ff\\u3400-\\u9fff\\uf900-\\ufaff\\uff00-\\uffef]/');
    lines.push('        width = 0');
    lines.push('        raw.scan(/./m) {|char| width += bitmap.gt_original_text_size(char).width }');
    lines.push('        rect.width = width if width > rect.width');
    lines.push('      end');
    lines.push('    rescue Exception');
    lines.push('    end');
    lines.push('    rect');
    lines.push('  end');
    lines.push('  $gt_runtime_reflow_message_text ||= proc do |window|');
    lines.push('    begin');
    lines.push('      text = window.instance_variable_get(:@text)');
    lines.push('      if text && !text.empty? && window.respond_to?(:contents) && window.contents');
    lines.push('        maxwidth = window.contents.width - 4 - ((defined?($game_temp) && $game_temp && $game_temp.message_face == nil) ? 0 : 96)');
    lines.push('        out = String.new');
    lines.push('        x = 0');
    lines.push('        y = 0');
    lines.push('        i = 0');
    lines.push('        while i < text.length');
    lines.push('          c = text[i]');
    lines.push('          if c == "\\n"');
    lines.push('            out << c');
    lines.push('            x = 0');
    lines.push('            y += 1');
    lines.push('            i += 1');
    lines.push('            break if y >= 4');
    lines.push('            next');
    lines.push('          elsif c == "\\000"');
    lines.push('            rest = text[(i + 1)..-1] || ""');
    lines.push('            token = rest[/\\A\\[[0-9]+\\]/] || ""');
    lines.push('            out << c << token');
    lines.push('            i += 1 + token.length');
    lines.push('            next');
    lines.push('          elsif c == "\\001" || c == "\\002" || c == "\\004"');
    lines.push('            out << c');
    lines.push('            i += 1');
    lines.push('            next');
    lines.push('          elsif c == "\\003"');
    lines.push('            rest = text[(i + 1)..-1] || ""');
    lines.push('            token = rest[/\\A[^\\s]+ */] || ""');
    lines.push('            out << c << token');
    lines.push('            i += 1 + token.length');
    lines.push('            next');
    lines.push('          end');
    lines.push('          if c == " " && x == 0');
    lines.push('            i += 1');
    lines.push('            next');
    lines.push('          end');
    lines.push('          width = window.contents.text_size(c).width');
    lines.push('          if x > 0 && x + width > maxwidth');
    lines.push('            out << "\\n"');
    lines.push('            x = 0');
    lines.push('            y += 1');
    lines.push('            break if y >= 4');
    lines.push('            if c == " "');
    lines.push('              i += 1');
    lines.push('              next');
    lines.push('            end');
    lines.push('          end');
    lines.push('          out << c');
    lines.push('          x += width');
    lines.push('          i += 1');
    lines.push('        end');
    lines.push('        out.rstrip');
    lines.push('      else');
    lines.push('        text');
    lines.push('      end');
    lines.push('    rescue Exception');
    lines.push('      nil');
    lines.push('    end');
    lines.push('  end');
    lines.push('  $gt_runtime_wrap_raw_text ||= proc do |bitmap, value, maxwidth, max_lines|');
    lines.push('    begin');
    lines.push('      src = value.to_s.gsub("\\\\n", "\\n")');
    lines.push('      out = String.new');
    lines.push('      x = 0');
    lines.push('      y = 0');
    lines.push('      i = 0');
    lines.push('      while i < src.length');
    lines.push('        token = nil');
    lines.push('        measure = nil');
    lines.push('        c = src[i]');
    lines.push('        if c == "\\n"');
    lines.push('          out << c');
    lines.push('          x = 0');
    lines.push('          y += 1');
    lines.push('          i += 1');
    lines.push('          break if max_lines && y >= max_lines');
    lines.push('          next');
    lines.push('        elsif c == "\\\\"');
    lines.push('          rest = src[i..-1] || ""');
    lines.push('          if rest =~ /\\A\\\\c\\[[0-9]+\\]/');
    lines.push('            token = $&');
    lines.push('            measure = ""');
    lines.push('          elsif rest =~ /\\A\\\\v\\[([0-9]+)\\]/');
    lines.push('            token = $&');
    lines.push('            measure = (defined?($game_variables) && $game_variables[$1.to_i] ? $game_variables[$1.to_i].to_s : token)');
    lines.push('          elsif rest =~ /\\A\\\\n\\[([0-9]+)\\]/');
    lines.push('            token = $&');
    lines.push('            actor = defined?($game_actors) ? $game_actors[$1.to_i] : nil');
    lines.push('            measure = actor ? actor.name.to_s : ""');
    lines.push('          elsif rest =~ /\\A\\\\p/');
    lines.push('            token = $&');
    lines.push('            measure = (defined?($game_oneshot) && $game_oneshot ? $game_oneshot.player_name.to_s : token)');
    lines.push('          elsif rest =~ /\\A\\\\[\\.\\|!>\\^<>{}@]/');
    lines.push('            token = $&');
    lines.push('            measure = ""');
    lines.push('          elsif rest =~ /\\A\\\\\\\\/');
    lines.push('            token = $&');
    lines.push('            measure = "\\\\"');
    lines.push('          end');
    lines.push('        end');
    lines.push('        token ||= c');
    lines.push('        measure = token if measure.nil?');
    lines.push('        if measure == " " && x == 0');
    lines.push('          i += token.length');
    lines.push('          next');
    lines.push('        end');
    lines.push('        width = measure.empty? ? 0 : bitmap.text_size(measure).width');
    lines.push('        if x > 0 && width > 0 && x + width > maxwidth');
    lines.push('          out << "\\n"');
    lines.push('          x = 0');
    lines.push('          y += 1');
    lines.push('          break if max_lines && y >= max_lines');
    lines.push('          if measure == " "');
    lines.push('            i += token.length');
    lines.push('            next');
    lines.push('          end');
    lines.push('        end');
    lines.push('        out << token');
    lines.push('        x += width');
    lines.push('        i += token.length');
    lines.push('      end');
    lines.push('      out.rstrip');
    lines.push('    rescue Exception');
    lines.push('      value.to_s');
    lines.push('    end');
    lines.push('  end');
    lines.push('  $gt_runtime_prepare_raw_text ||= proc do |value|');
    lines.push('    text = value.to_s.gsub("\\\\n", "\\n")');
    lines.push('    text = String.new($gt_runtime_translate_text.call(text) || text)');
    lines.push('    text.gsub!(/\\\\v\\[([0-9]+)\\]/) { defined?($game_variables) && $game_variables[$1.to_i] ? $game_variables[$1.to_i].to_s : "" }');
    lines.push('    text.gsub!(/\\\\n\\[([0-9]+)\\]/) { defined?($game_actors) && $game_actors[$1.to_i] ? $game_actors[$1.to_i].name.to_s : "" }');
    lines.push('    text.gsub!("\\\\p", (defined?($game_oneshot) && $game_oneshot ? $game_oneshot.player_name.to_s : ""))');
    lines.push('    text.gsub!("\\\\n", "\\n")');
    lines.push('    text.gsub!(/\\\\c\\[([0-9]+)\\]/, "\\000[\\\\1]")');
    lines.push('    text.gsub!("\\\\\\\\", "\\\\")');
    lines.push('    text');
    lines.push('  end');
    lines.push('  $gt_runtime_visible_width ||= proc do |bitmap, value|');
    lines.push('    width = 0');
    lines.push('    value.to_s.gsub(/\\000\\[[0-9]+\\]/, "").scan(/./m) {|char| width += bitmap.gt_original_text_size(char).width }');
    lines.push('    width');
    lines.push('  end');
    lines.push('  $gt_runtime_wrap_prepared_text ||= proc do |bitmap, value, maxwidth, max_lines|');
    lines.push('    out = String.new');
    lines.push('    line = String.new');
    lines.push('    line_width = 0');
    lines.push('    lines = 0');
    lines.push('    i = 0');
    lines.push('    src = value.to_s');
    lines.push('    while i < src.length');
    lines.push('      c = src[i]');
    lines.push('      if c == "\\n"');
    lines.push('        out << line.rstrip << "\\n"');
    lines.push('        line = String.new');
    lines.push('        line_width = 0');
    lines.push('        lines += 1');
    lines.push('        i += 1');
    lines.push('        break if max_lines && lines >= max_lines');
    lines.push('        next');
    lines.push('      elsif c == "\\000"');
    lines.push('        rest = src[(i + 1)..-1] || ""');
    lines.push('        token = rest[/\\A\\[[0-9]+\\]/] || ""');
    lines.push('        line << c << token');
    lines.push('        i += 1 + token.length');
    lines.push('        next');
    lines.push('      end');
    lines.push('      char_width = bitmap.gt_original_text_size(c).width');
    lines.push('      if line_width > 0 && char_width > 0 && line_width + char_width > maxwidth');
    lines.push('        out << line.rstrip << "\\n"');
    lines.push('        line = String.new');
    lines.push('        line_width = 0');
    lines.push('        lines += 1');
    lines.push('        break if max_lines && lines >= max_lines');
    lines.push('        if c == " "');
    lines.push('          i += 1');
    lines.push('          next');
    lines.push('        end');
    lines.push('      end');
    lines.push('      unless c == " " && line_width == 0');
    lines.push('        line << c');
    lines.push('        line_width += char_width');
    lines.push('      end');
    lines.push('      i += 1');
    lines.push('    end');
    lines.push('    out << line.rstrip unless line.empty?');
    lines.push('    out.rstrip');
    lines.push('  end');
    lines.push('  $gt_runtime_draw_centered_text ||= proc do |bitmap, value, maxwidth, max_lines, base_color|');
    lines.push('    prepared = $gt_runtime_prepare_raw_text.call(value)');
    lines.push('    wrapped = $gt_runtime_wrap_prepared_text.call(bitmap, prepared, maxwidth, max_lines)');
    lines.push('    rows = wrapped.split("\\n")');
    lines.push('    rows = [""] if rows.empty?');
    lines.push('    bitmap.clear');
    lines.push('    bitmap.font.color = base_color');
    lines.push('    y_top = ((bitmap.height - rows.length * 24) / 2).to_i');
    lines.push('    rows.each_with_index do |row, y|');
    lines.push('      row_width = [$gt_runtime_visible_width.call(bitmap, row), maxwidth].min');
    lines.push('      x = ((bitmap.width - row_width) / 2).to_i');
    lines.push('      i = 0');
    lines.push('      while i < row.length');
    lines.push('        c = row[i]');
    lines.push('        if c == "\\000"');
    lines.push('          rest = row[(i + 1)..-1] || ""');
    lines.push('          token = rest[/\\A\\[([0-9]+)\\]/] || ""');
    lines.push('          if token && token =~ /\\A\\[([0-9]+)\\]/');
    lines.push('            color = $1.to_i');
    lines.push('            bitmap.font.color = Window_Base.text_color(color) if defined?(Window_Base) && color >= 0 && color <= 7');
    lines.push('          end');
    lines.push('          i += 1 + token.to_s.length');
    lines.push('          next');
    lines.push('        end');
    lines.push('        bitmap.gt_original_draw_text(x, y_top + y * 24, 40, 24, c)');
    lines.push('        x += bitmap.gt_original_text_size(c).width');
    lines.push('        i += 1');
    lines.push('      end');
    lines.push('      bitmap.font.color = base_color');
    lines.push('    end');
    lines.push('    Graphics.frame_reset rescue nil');
    lines.push('    true');
    lines.push('  end');
    lines.push('  $gt_runtime_install_language_hook ||= proc do');
    lines.push('    if defined?(Language) && Language.respond_to?(:tr) && !Language.instance_variable_get(:@gt_runtime_hooked)');
    lines.push('      class << Language');
    lines.push('        alias_method :gt_original_tr, :tr unless method_defined?(:gt_original_tr)');
    lines.push('        def tr(string)');
    lines.push('          rv = gt_original_tr(string)');
    lines.push('          mapped = $gt_runtime_translate_text.call(rv) || $gt_runtime_translate_text.call(string)');
    lines.push('          if mapped');
    lines.push('            $gt_runtime_hit_count ||= 0');
    lines.push('            if $gt_runtime_debug_log && $gt_runtime_hit_count < 80');
    lines.push('              File.open("gt_runtime_hook.log", "a") {|f| f.puts(rv.to_s + " => " + mapped.to_s) } rescue nil');
    lines.push('            end');
    lines.push('            $gt_runtime_hit_count += 1');
    lines.push('          end');
    lines.push('          mapped ? String.new(mapped) : rv');
    lines.push('        end');
    lines.push('      end');
    lines.push('      Language.instance_variable_set(:@gt_runtime_hooked, true)');
    lines.push('      if defined?(Font)');
        lines.push('        names = ($gt_runtime_font_names || []).compact');
    lines.push('        chosen = nil');
    lines.push('        if Font.respond_to?(:exist?)');
    lines.push('          names.each {|name| begin chosen = name if Font.exist?(name); break if chosen; rescue Exception; end }');
    lines.push('        end');
    lines.push('        chosen ||= names[0]');
    lines.push('        begin');
    lines.push('          Font.default_name = names if names && !names.empty?');
    lines.push('        rescue Exception');
    lines.push('          Font.default_name = chosen rescue nil');
    lines.push('        end');
    lines.push('        Font.default_size = [Font.default_size, 20].max rescue nil');
    lines.push('      end');
    lines.push('      if Language.instance_variable_defined?(:@languageFontMap)');
    lines.push('        map = Language.instance_variable_get(:@languageFontMap) rescue nil');
    lines.push('        if map && defined?(Font)');
    lines.push('          chosen = Font.default_name.is_a?(Array) ? Font.default_name[0] : Font.default_name');
    lines.push('          ["ja", "zh_CN", "zh", "zh-CN"].each {|code| map[code] = chosen if chosen }');
    lines.push('        end');
    lines.push('      end');
    lines.push('      if Language.respond_to?(:reset_fonts) && Language.instance_variable_defined?(:@text_sprites)');
    lines.push('        Language.reset_fonts(Language.instance_variable_get(:@text_sprites)) rescue nil');
    lines.push('      end');
    lines.push('      if defined?(Bitmap) && !Bitmap.method_defined?(:gt_original_draw_text)');
    lines.push('        class Bitmap');
    lines.push('          alias_method :gt_original_draw_text, :draw_text');
    lines.push('          def draw_text(*args)');
    lines.push('            text_index = args[0].is_a?(Rect) ? 1 : 4');
    lines.push('            if args.length > text_index');
    lines.push('              mapped = $gt_runtime_translate_text.call(args[text_index])');
    lines.push('              args[text_index] = String.new(mapped) if mapped');
    lines.push('            end');
    lines.push('            gt_original_draw_text(*args)');
    lines.push('          end');
    lines.push('          if method_defined?(:text_size) && !method_defined?(:gt_original_text_size)');
    lines.push('            alias_method :gt_original_text_size, :text_size');
    lines.push('            def text_size(text)');
    lines.push('              $gt_runtime_measure_text_rect.call(self, text)');
    lines.push('            end');
    lines.push('          end');
    lines.push('        end');
    lines.push('      end');
    lines.push('      if defined?(Window_Message) && Window_Message.method_defined?(:refresh) && !Window_Message.method_defined?(:gt_original_refresh)');
    lines.push('        class Window_Message');
    lines.push('          alias_method :gt_original_refresh, :refresh');
    lines.push('          def refresh');
    lines.push('            if defined?($game_temp) && $game_temp');
    lines.push('              [:message_text, :message_ed_text, :message_doc_text, :message_desktop_text, :message_credits_text].each do |name|');
    lines.push('                setter = (name.to_s + "=").to_sym');
    lines.push('                next unless $game_temp.respond_to?(name) && $game_temp.respond_to?(setter)');
    lines.push('                current = $game_temp.send(name)');
    lines.push('                next unless current');
    lines.push('                mapped = $gt_runtime_translate_text.call(current)');
    lines.push('                $game_temp.send(setter, String.new(mapped)) if mapped');
    lines.push('              end');
    lines.push('            end');
    lines.push('            gt_original_refresh');
    lines.push('            reflowed = $gt_runtime_reflow_message_text.call(self)');
    lines.push('            @text = reflowed if reflowed');
    lines.push('          end');
    lines.push('        end');
    lines.push('      end');
    lines.push('      if defined?(Ed_Message) && Ed_Message.method_defined?(:refresh) && !Ed_Message.method_defined?(:gt_original_refresh)');
    lines.push('        class Ed_Message');
    lines.push('          alias_method :gt_original_refresh, :refresh');
    lines.push('          def refresh');
    lines.push('            original = defined?($game_temp) && $game_temp ? $game_temp.message_ed_text : nil');
    lines.push('            if original && instance_variable_defined?(:@contents)');
    lines.push('              contents = instance_variable_get(:@contents)');
    lines.push('              if contents');
    lines.push('                $gt_runtime_draw_centered_text.call(contents, original.to_s, [contents.width - 160, 360].min, 4, Color.new(255, 255, 255, 255))');
    lines.push('                return');
    lines.push('              end');
    lines.push('            end');
    lines.push('            gt_original_refresh');
    lines.push('          end');
    lines.push('        end');
    lines.push('      end');
    lines.push('      if defined?(Desktop_Message) && Desktop_Message.method_defined?(:refresh) && !Desktop_Message.method_defined?(:gt_original_refresh)');
    lines.push('        class Desktop_Message');
    lines.push('          alias_method :gt_original_refresh, :refresh');
    lines.push('          def refresh');
    lines.push('            original = defined?($game_temp) && $game_temp ? $game_temp.message_desktop_text : nil');
    lines.push('            begin');
    lines.push('              if original && instance_variable_defined?(:@contents)');
    lines.push('                contents = instance_variable_get(:@contents)');
    lines.push('                $game_temp.message_desktop_text = String.new($gt_runtime_wrap_raw_text.call(contents, original.to_s, contents.width - 16, 4)) if contents');
    lines.push('              end');
    lines.push('              gt_original_refresh');
    lines.push('            ensure');
    lines.push('              $game_temp.message_desktop_text = original if defined?($game_temp) && $game_temp && original');
    lines.push('            end');
    lines.push('          end');
    lines.push('        end');
    lines.push('      end');
    lines.push('      if defined?(Doc_Message) && Doc_Message.method_defined?(:refresh) && !Doc_Message.method_defined?(:gt_original_refresh)');
    lines.push('        class Doc_Message');
    lines.push('          alias_method :gt_original_refresh, :refresh');
    lines.push('          def refresh');
    lines.push('            original = defined?($game_temp) && $game_temp ? $game_temp.message_doc_text : nil');
    lines.push('            begin');
    lines.push('              if original && instance_variable_defined?(:@contents)');
    lines.push('                contents = instance_variable_get(:@contents)');
    lines.push('                $game_temp.message_doc_text = String.new($gt_runtime_wrap_raw_text.call(contents, original.to_s, contents.width - 16, nil)) if contents');
    lines.push('              end');
    lines.push('              gt_original_refresh');
    lines.push('            ensure');
    lines.push('              $game_temp.message_doc_text = original if defined?($game_temp) && $game_temp && original');
    lines.push('            end');
    lines.push('          end');
    lines.push('        end');
    lines.push('      end');
    lines.push('      File.open("gt_runtime_hook.log", "a") {|f| f.puts("GT_RUNTIME_HOOK_INSTALLED map=" + $gt_runtime_translation_map.size.to_s) } rescue nil if $gt_runtime_debug_log');
    lines.push('      true');
    lines.push('    else');
    lines.push('      false');
    lines.push('    end');
    lines.push('  end');
    lines.push('  unless $gt_runtime_hook_thread_started');
    lines.push('    $gt_runtime_hook_thread_started = true');
    lines.push('    Thread.new do');
    lines.push('      300.times do');
    lines.push('        break if $gt_runtime_install_language_hook.call');
    lines.push('        sleep 0.05');
    lines.push('      end');
    lines.push('    end');
    lines.push('  end');
    lines.push('  $gt_runtime_install_language_hook.call');
    lines.push('rescue Exception => e');
    lines.push('  File.open("gt_runtime_hook.log", "a") {|f| f.puts("GT_RUNTIME_HOOK_ERROR " + e.class.to_s + ": " + e.message.to_s) } rescue nil if $gt_runtime_debug_log');
    lines.push('end');
    return lines.join('\n');
}

function tryInjectRubyPatch(src) {
    if (rubyInjected || rubyInjecting) return;
    var evalAddr = findExport('rb_eval_string_protect') || findExport('rb_eval_string');
    if (!evalAddr) return;
    var patch = buildRubyPatch();
    if (!patch) return;
    try {
        rubyInjecting = true;
        if (String(evalAddr).indexOf('0x') !== 0) return;
        if (findExport('rb_eval_string_protect')) {
            var evalProtect = new NativeFunction(evalAddr, 'pointer', ['pointer', 'pointer']);
            var state = Memory.alloc(4);
            state.writeS32(0);
            evalProtect(Memory.allocUtf8String(patch), state);
            var stateValue = state.readS32();
            if (stateValue !== 0) {
                emit({ type: 'ruby_inject_error', src: src, state: stateValue });
                return;
            }
        } else {
            var evalString = new NativeFunction(evalAddr, 'pointer', ['pointer']);
            evalString(Memory.allocUtf8String(patch));
        }
        rubyInjected = true;
        installedHooks.push('rb_eval_string_protect:inject');
        emit({ type: 'ruby_injected', src: src, map_size: Object.keys(TRANSLATION_MAP).length });
    } catch (e) {
        emit({ type: 'ruby_inject_error', src: src, message: String(e) });
    } finally {
        rubyInjecting = false;
    }
}

function markRubyReady(src) {
    rubyRuntimeReady = true;
    tryInjectRubyPatch(src);
}

function hookRubyRequire() {
    ['rb_require', 'rb_load', 'ruby_init', 'ruby_setup', 'ruby_options', 'rb_eval_string_protect', 'rb_eval_string'].forEach(function (name) {
        var addr = findExport(name);
        if (!addr) return;
        try {
            Interceptor.attach(addr, {
                onLeave: function () {
                    if (/ruby_init|ruby_setup|ruby_options/.test(name)) {
                        markRubyReady(name);
                    } else {
                        tryInjectRubyPatch(name);
                    }
                }
            });
            installedHooks.push(name);
        } catch (e) {
            emit({ type: 'hook_error', name: name, message: String(e) });
        }
    });
}

function scheduleRubyInjectionPoll() {
    if (rubyInjectionTimer) return;
    rubyInjectionTimer = setInterval(function () {
        if (rubyInjected) {
            clearInterval(rubyInjectionTimer);
            rubyInjectionTimer = null;
            return;
        }
        rubyInjectionAttempts++;
        if (!rubyRuntimeReady && rubyInjectionAttempts < 6) {
            return;
        }
        if (rubyInjectionAttempts === 6) {
            rubyRuntimeReady = true;
        }
        tryInjectRubyPatch('poll:' + rubyInjectionAttempts);
        if (rubyInjectionAttempts >= 240) {
            clearInterval(rubyInjectionTimer);
            rubyInjectionTimer = null;
            emit({ type: 'ruby_inject_giveup', attempts: rubyInjectionAttempts });
        }
    }, 500);
}

function hookUtf8(name) {
    var addr = findExport(name);
    if (!addr) {
        emit({ type: 'missing_export', name: name });
        return;
    }
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                this.replacement = replaceUtf8Arg(args, name);
            }
        });
        installedHooks.push(name);
    } catch (e) {
        emit({ type: 'hook_error', name: name, message: String(e) });
    }
}

function hookUtf16(name) {
    var addr = findExport(name);
    if (!addr) {
        return;
    }
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                this.replacement = replaceUtf16Arg(args, name);
            }
        });
        installedHooks.push(name);
    } catch (e) {
        emit({ type: 'hook_error', name: name, message: String(e) });
    }
}

[
    'TTF_SizeUTF8',
    'TTF_RenderUTF8_Blended',
    'TTF_RenderUTF8_Blended_Wrapped',
    'TTF_RenderUTF8_Solid',
    'TTF_RenderUTF8_Shaded'
].forEach(hookUtf8);

[
    'TTF_SizeUNICODE',
    'TTF_RenderUNICODE_Blended',
    'TTF_RenderUNICODE_Blended_Wrapped',
    'TTF_RenderUNICODE_Solid',
    'TTF_RenderUNICODE_Shaded'
].forEach(hookUtf16);

[
    'TTF_OpenFont',
    'TTF_OpenFontIndex'
].forEach(hookFontOpen);

hookRubyRequire();
scheduleRubyInjectionPoll();

emit({
    type: 'ready',
    pid: Process.id,
    hooks: installedHooks,
    map_size: Object.keys(TRANSLATION_MAP).length,
    modules: Process.enumerateModules()
        .filter(function (m) { return /oneshot|ruby|sdl|ttf|mkxp/i.test(m.name); })
        .map(function (m) { return { name: m.name, path: m.path }; })
});

setInterval(function () {
    emit({
        type: 'stats',
        seen: seenCount,
        hits: hitCount,
        misses: missCount,
        hooks: installedHooks.length
    });
}, 5000);
