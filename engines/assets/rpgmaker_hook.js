// RPG Maker MV/MZ Text Hook — HTTP 通信，不依赖 require('fs')
// 扫描模式: 收集文本 → POST 到翻译服务器
// 替换模式: GET 翻译表 → 立即 hook 显示层，查表替换
(function() {
    var MODE = "scan";
    var PROXY = "http://127.0.0.1:5120";

    // ---- 诊断（写文件，优先 save/ 目录） ----
    function _diagWrite(msg) {
        try {
            var fs = require('fs');
            fs.appendFileSync('save/hook_diag.txt', msg + '\n');
        } catch(e) {}
    }
    _diagWrite('===== HOOK START mode=' + MODE + ' =====');

    // ---- Control code protection ----
    var CTRL_RE = /\\[A-Za-z]{1,3}\s*\[([^\]]*)\]|\\[\.\|!>\^<>{}]|\\FS\[\d+\]|\\fr\b|\\fb\b|\\fi\b|\\g\b/gi;
    var CTRL_TYPE_MAP = {
        'C': 'COLOR', 'N': 'NAME', 'V': 'VAR', 'I': 'ICON',
        'SE': 'SE', 'SP': 'SPRITE', 'SA': 'SA', 'FS': 'FONT',
        '.': 'DOT', '|': 'WAIT', '!': 'SHAKE', '>': 'INSTANT', '^': 'PAUSE',
        'fr': 'FRESET', 'fb': 'FBOLD', 'fi': 'FITAL', 'g': 'CURRENCY'
    };
    var _ctrlTokens = [], _ctrlTypes = [];

    function protectControls(text) {
        _ctrlTokens = []; _ctrlTypes = [];
        return text.replace(CTRL_RE, function(match) {
            var idx = _ctrlTokens.length;
            _ctrlTokens.push(match);
            var m = match.match(/^\\([A-Za-z]+)/);
            var code = m ? m[1].toUpperCase() : '';
            var inner = match.match(/\[([^\]]*)\]/);
            var param = inner ? inner[1].replace(/[^a-zA-Z0-9]/g, '_') : '';
            var baseType = CTRL_TYPE_MAP[code] || code;
            var token = param ? ('__' + baseType + '_' + param + '__') : ('__' + baseType + '__');
            _ctrlTypes.push({type: baseType, param: param, token: token, original: match});
            return token;
        });
    }

    function restoreControls(text) {
        for (var i = 0; i < _ctrlTokens.length; i++)
            text = text.replace(_ctrlTypes[i].token, _ctrlTokens[i]);
        return text;
    }

    function isOnlyPunctuation(text) {
        var s = String(text || '').replace(/\s+/g, '');
        if (!s) return false;
        return /^[\u3000。、，．.!！?？…・：:；;「」『』（）()［］\[\]【】《》〈〉～〜ー—\-♡♪☆·]+$/.test(s);
    }

    function looksPollutedTranslation(source, translated) {
        var src = String(source || '').trim();
        var dst = String(translated || '').trim();
        if (!src || !dst) return true;
        if (isOnlyPunctuation(src)) return true;
        if (/您(?:没有|未)(?:提供|给出)|没有提供(?:具体|完整|需要翻译|日语|原文)|请提供|可以翻译为|具体取决于|自然流畅的中文|日语游戏文本|\*\*/.test(dst)) return true;
        if (src.length <= 4 && dst.length >= Math.max(15, src.length * 6)) return true;
        return false;
    }

    // ---- HTTP helpers ----
    function httpGetSync(url) {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', url, false);
        try { xhr.send(); return xhr.responseText; } catch(e) { return null; }
    }

    function httpPostSync(url, data) {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', url, false);
        xhr.setRequestHeader('Content-Type', 'application/json');
        try { xhr.send(JSON.stringify(data)); return xhr.responseText; } catch(e) { return null; }
    }

    function httpPost(url, data, callback) {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', url, true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4 && callback) callback(xhr.status, xhr.responseText);
        };
        xhr.send(JSON.stringify(data));
    }

    // ---- Scan mode ----
    function makeCtx(cat, id, name, role) {
        return [cat, id, role].filter(Boolean).join('.');
    }

    function scanList(list, context) {
        if (!list) return [];
        var texts = [];
        for (var j = 0; j < list.length; j++) {
            var cmd = list[j];
            if (!cmd || !cmd.parameters) continue;
            if (cmd.code === 101) texts.push({text: cmd.parameters[4], ctx: context + '.line'});
            if (cmd.code === 401) texts.push({text: cmd.parameters[0], ctx: context + '.line'});
            if (cmd.code === 102 && cmd.parameters[0])
                for (var c = 0; c < cmd.parameters[0].length; c++)
                    texts.push({text: cmd.parameters[0][c], ctx: context + '.choice'});
            if (cmd.code === 402) texts.push({text: cmd.parameters[0], ctx: context + '.choice'});
            if (cmd.code === 105) texts.push({text: cmd.parameters[0], ctx: context + '.scroll'});
            if (cmd.code === 405) texts.push({text: cmd.parameters[0], ctx: context + '.scroll'});
        }
        return texts;
    }

    function doScan() {
        var items = [];
        function add(text, context) {
            if (!text || typeof text !== 'string') return;
            var s = text.trim();
            if (s.length < 1 || s.length > 500) return;
            if (/^\\[A-Za-z]+\s*\[[^\]]*\]$/i.test(s)) return;
            items.push({text: s, context: context || 'unknown', count: 1});
        }

        if ($dataCommonEvents) {
            for (var i = 0; i < $dataCommonEvents.length; i++) {
                var ev = $dataCommonEvents[i];
                if (!ev || !ev.list) continue;
                scanList(ev.list, makeCtx('CmEv', ev.id, ev.name, 'dialogue')).forEach(function(t) { add(t.text, t.ctx); });
            }
        }
        if ($dataMap && $dataMap.events) {
            for (var i = 0; i < $dataMap.events.length; i++) {
                var mev = $dataMap.events[i];
                if (!mev || !mev.pages) continue;
                for (var p = 0; p < mev.pages.length; p++)
                    scanList(mev.pages[p].list, makeCtx('Map', ($dataMap.mapId||0) + '.Ev' + (mev.id || i), mev.name, 'dialogue')).forEach(function(t) { add(t.text, t.ctx); });
            }
        }
        if ($dataMapInfos) {
            for (var m = 0; m < $dataMapInfos.length; m++)
                if ($dataMapInfos[m]) add($dataMapInfos[m].name, makeCtx('MapInfo', m, $dataMapInfos[m].name, 'menu'));
        }
        var dbFields = ['name', 'description', 'message', 'displayName', 'title', 'profile'];
        var dbDefs = {
            '$dataActors': ['Actor','status'], '$dataItems': ['Item','menu'],
            '$dataWeapons': ['Weapon','menu'], '$dataArmors': ['Armor','menu'],
            '$dataSkills': ['Skill','battle'], '$dataStates': ['State','battle'],
            '$dataEnemies': ['Enemy','battle'], '$dataTroops': ['Troop','battle'],
            '$dataClasses': ['Class','status']
        };
        for (var an in dbDefs) {
            var arr = null;
            try { arr = eval(an); } catch(e) {}
            if (!arr) continue;
            var di = dbDefs[an];
            for (var a = 0; a < arr.length; a++) {
                var e = arr[a]; if (!e) continue;
                var ec = makeCtx(di[0], e.id || a, e.name, di[1]);
                for (var f = 0; f < dbFields.length; f++) add(e[dbFields[f]], ec + '.' + dbFields[f]);
            }
        }
        if ($dataSystem) {
            add($dataSystem.gameTitle, makeCtx('Sys','','','menu'));
            add($dataSystem.currencyUnit, makeCtx('Sys','','','menu'));
            var t = $dataSystem.terms;
            if (t) {
                if (t.basic) for (var b = 0; b < t.basic.length; b++) add(t.basic[b], makeCtx('Sys','basic',b,'menu'));
                if (t.commands) for (var c = 0; c < t.commands.length; c++) add(t.commands[c], makeCtx('Sys','cmd',c,'menu'));
                if (t.params) for (var p = 0; p < t.params.length; p++) add(t.params[p], makeCtx('Sys','param',p,'status'));
                if (t.messages) for (var k in t.messages) add(t.messages[k], makeCtx('Sys','msg',k,'menu'));
            }
            if ($dataSystem.elements) for (var e = 0; e < $dataSystem.elements.length; e++) add($dataSystem.elements[e], makeCtx('Sys','elem',e,'battle'));
            if ($dataSystem.skillTypes) for (var s = 0; s < $dataSystem.skillTypes.length; s++) add($dataSystem.skillTypes[s], makeCtx('Sys','stype',s,'battle'));
            if ($dataSystem.weaponTypes) for (var w = 0; w < $dataSystem.weaponTypes.length; w++) add($dataSystem.weaponTypes[w], makeCtx('Sys','wtype',w,'status'));
            if ($dataSystem.armorTypes) for (var a = 0; a < $dataSystem.armorTypes.length; a++) add($dataSystem.armorTypes[a], makeCtx('Sys','atype',a,'status'));
        }

        // Deduplicate
        var seen = {};
        var unique = [];
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var safe = protectControls(item.text);
            if (!seen[safe]) {
                seen[safe] = true;
                unique.push({safe: safe, text: item.text, context: item.context, count: 1, types: _ctrlTypes.slice()});
            } else {
                for (var u = 0; u < unique.length; u++)
                    if (unique[u].safe === safe) { unique[u].count++; break; }
            }
        }

        httpPost(PROXY + '/_hook_scan', {items: unique, game: document.title || 'unknown'}, function(status, resp) {
            console.log('hook scan sent: ' + unique.length + ' items, status=' + status);
        });

        window.__scanDone = true;
        window.__scanCount = unique.length;
    }

    var _scanFired = false;
    var _scanTimer = 0;
    var _scanMaxWait = 30000; // 最多等 30 秒

    function _tryFireScan() {
        if (_scanFired) return true;
        // 关键数据就绪：CommonEvents 和 MapInfos 是最大头
        if (typeof $dataCommonEvents !== 'undefined' && typeof $dataMapInfos !== 'undefined') {
            _scanFired = true;
            if (_scanTimer) clearInterval(_scanTimer);
            _diagWrite('SCAN_READY: data loaded, firing doScan');
            doScan();
            return true;
        }
        return false;
    }

    function launchScan() {
        _diagWrite('launchScan: hooking DataManager.isDatabaseLoaded...');

        // 方案 A：Hook DataManager.isDatabaseLoaded（精确时机）
        if (typeof DataManager !== 'undefined' && typeof DataManager.isDatabaseLoaded === 'function') {
            var _origIsDbLoaded = DataManager.isDatabaseLoaded;
            DataManager.isDatabaseLoaded = function() {
                var result = _origIsDbLoaded.apply(this, arguments);
                if (result) _tryFireScan();
                return result;
            };
            _diagWrite('hooked DataManager.isDatabaseLoaded');
            // 如果数据库已经加载完了（回退），立即试一次
            setTimeout(function() { _tryFireScan(); }, 1000);
        } else {
            _diagWrite('DataManager.isDatabaseLoaded NOT FOUND, fallback to poll');
            // 方案 B：轮询全局变量
            _scanTimer = setInterval(function() {
                if (_tryFireScan()) clearInterval(_scanTimer);
            }, 500);
        }

        // 硬超时兜底
        setTimeout(function() {
            if (!_scanFired) {
                _diagWrite('SCAN_TIMEOUT: ' + _scanMaxWait + 'ms, firing anyway');
                if (_scanTimer) clearInterval(_scanTimer);
                _scanFired = true;
                doScan();
            }
        }, _scanMaxWait);
    }

    // ---- Replace mode ----

    var TRANSLATION_MAP = {};

    function tryLoadFile(relativePath) {
        try {
            var fs = require('fs');
            if (fs && fs.readFileSync) {
                var data = fs.readFileSync(relativePath, 'utf-8');
                if (data) return data;
            }
        } catch(e) {}
        return null;
    }

    function loadReplaceMap() {
        // 通道 1: HTTP 服务器
        var data = httpGetSync(PROXY + '/_hook_map');
        if (data) {
            try {
                TRANSLATION_MAP = JSON.parse(data);
                var n = Object.keys(TRANSLATION_MAP).length;
                _diagWrite('HTTP_OK map=' + n);
                if (n > 0) return true;
            } catch(e) { _diagWrite('HTTP_JSON_ERR: ' + e.message); }
        } else {
            _diagWrite('HTTP_FAIL (server not running?)');
        }

        // 通道 2: 本地 JSON 文件
        var filePaths = ['save/hook_translation_map.json', '../save/hook_translation_map.json'];
        for (var i = 0; i < filePaths.length; i++) {
            var fileData = tryLoadFile(filePaths[i]);
            if (fileData) {
                try {
                    TRANSLATION_MAP = JSON.parse(fileData);
                    var fn = Object.keys(TRANSLATION_MAP).length;
                    _diagWrite('FILE_OK path=' + filePaths[i] + ' map=' + fn);
                    if (fn > 0) return true;
                } catch(e) { _diagWrite('FILE_JSON_ERR: ' + e.message); }
            }
        }
        _diagWrite('ALL_FAIL map=0');
        return false;
    }

    var _replaceCount = 0;
    var _replaceHit = 0;
    function _replace(text) {
        if (text && typeof text === 'string' && text.trim()) {
            var orig = text.trim();
            if (isOnlyPunctuation(orig)) return text;
            var safe = protectControls(orig);
            var trans = TRANSLATION_MAP[safe];
            _replaceCount++;
            if (trans && trans !== safe && trans !== orig && !looksPollutedTranslation(safe, trans)) {
                _replaceHit++;
                if (_replaceHit <= 5) {
                    _diagWrite('HIT#' + _replaceHit + ': [' + orig.substring(0,40) + '] -> [' + trans.substring(0,40) + ']');
                }
                text = text.replace(orig, restoreControls(trans));
            } else if (trans && looksPollutedTranslation(safe, trans)) {
                _diagWrite('DROP_POLLUTED: [' + orig.substring(0,40) + '] -> [' + String(trans).substring(0,40) + ']');
            } else if (_replaceCount <= 5) {
                _diagWrite('MISS#' + _replaceCount + ': [' + orig.substring(0,40) + '] safe=[' + safe.substring(0,40) + ']');
            }
        }
        return text;
    }

    function _replaceTextTree(value) {
        if (Array.isArray(value)) {
            return value.map(function(item) { return _replaceTextTree(item); });
        }
        return typeof value === 'string' ? _replace(value) : value;
    }

    // Old RPG Maker/NW.js builds can keep decomposed Japanese filenames in
    // event data while the Windows filesystem stores the same name as NFC.
    function _resourcePathExists(decodedPath) {
        try {
            var fs = require('fs');
            var nodePath = require('path');
            var root = process.cwd();
            var relative = decodedPath.replace(/[\\/]+/g, nodePath.sep);
            var roots = [root, nodePath.join(root, 'www')];
            var variants = [relative];
            if (/\.png$/i.test(relative)) {
                variants.push(relative.replace(/\.png$/i, '.rpgmvp'));
            }
            for (var r = 0; r < roots.length; r++) {
                for (var v = 0; v < variants.length; v++) {
                    if (fs.existsSync(nodePath.join(roots[r], variants[v]))) return true;
                }
            }
        } catch(e) {}
        return false;
    }

    function _normalizeResourcePath(value) {
        var encoded = String(value || '');
        if (!encoded || !String.prototype.normalize) return encoded;
        var decoded;
        try { decoded = decodeURIComponent(encoded); } catch(e) { return encoded; }
        var normalized;
        try { normalized = decoded.normalize('NFC'); } catch(e) { return encoded; }
        if (normalized === decoded || !_resourcePathExists(normalized)) return encoded;
        _diagWrite('RESOURCE_NFC: [' + decoded.substring(0, 120) + ']');
        return encodeURI(normalized);
    }

    function _installResourcePathHook() {
        if (typeof ImageManager === 'undefined' || !ImageManager.loadNormalBitmap) return;
        if (ImageManager.__engaixtResourcePathHooked) return;
        var originalLoadNormalBitmap = ImageManager.loadNormalBitmap;
        ImageManager.loadNormalBitmap = function(path, hue) {
            return originalLoadNormalBitmap.call(this, _normalizeResourcePath(path), hue);
        };
        ImageManager.__engaixtResourcePathHooked = true;
        _diagWrite('hooked ImageManager.loadNormalBitmap NFC compatibility');
    }

    function hookReplace() {
        _diagWrite('hookReplace start, map=' + Object.keys(TRANSLATION_MAP).length);

        // Hook 1: Game_Message.add — 对话文本
        if (typeof Game_Message !== 'undefined' && Game_Message.prototype && Game_Message.prototype.add) {
            var _origAdd = Game_Message.prototype.add;
            Game_Message.prototype.add = function(text) {
                return _origAdd.call(this, _replace(text));
            };
            _diagWrite('hooked Game_Message.add');
        } else {
            _diagWrite('Game_Message.add NOT FOUND');
        }

        // Choices and plugin-provided choice help can bypass Game_Message.add.
        if (typeof Game_Message !== 'undefined' && Game_Message.prototype) {
            if (Game_Message.prototype.setChoices) {
                var _origSetChoices = Game_Message.prototype.setChoices;
                Game_Message.prototype.setChoices = function(choices, defaultType, cancelType) {
                    var translated = _replaceTextTree(choices);
                    return _origSetChoices.call(this, translated, defaultType, cancelType);
                };
                _diagWrite('hooked Game_Message.setChoices');
            }
            if (Game_Message.prototype.setChoiceHelpTexts) {
                var _origSetChoiceHelpTexts = Game_Message.prototype.setChoiceHelpTexts;
                Game_Message.prototype.setChoiceHelpTexts = function(texts) {
                    var translated = _replaceTextTree(texts);
                    return _origSetChoiceHelpTexts.call(this, translated);
                };
                _diagWrite('hooked Game_Message.setChoiceHelpTexts');
            }
            if (Game_Message.prototype.setTexts) {
                var _origSetTexts = Game_Message.prototype.setTexts;
                Game_Message.prototype.setTexts = function(texts) {
                    return _origSetTexts.call(this, _replaceTextTree(texts));
                };
                _diagWrite('hooked Game_Message.setTexts');
            }
        }

        // Hook 2: Window_Base.drawText — 所有窗口文本
        if (typeof Window_Base !== 'undefined' && Window_Base.prototype && Window_Base.prototype.drawText) {
            var _origDrawText = Window_Base.prototype.drawText;
            Window_Base.prototype.drawText = function(text, x, y, mw, align) {
                return _origDrawText.call(this, _replace(text), x, y, mw, align);
            };
            _diagWrite('hooked Window_Base.drawText');
        } else {
            _diagWrite('Window_Base.drawText NOT FOUND');
        }

        // drawTextEx renders one character at a time. Hooking Bitmap.drawText alone
        // cannot match a complete sentence, so replace before text-state creation.
        if (typeof Window_Base !== 'undefined' && Window_Base.prototype) {
            if (Window_Base.prototype.drawTextEx) {
                var _origDrawTextEx = Window_Base.prototype.drawTextEx;
                Window_Base.prototype.drawTextEx = function(text, x, y, width) {
                    return _origDrawTextEx.call(this, _replace(text), x, y, width);
                };
                _diagWrite('hooked Window_Base.drawTextEx');
            }
            if (Window_Base.prototype.createTextState) {
                var _origCreateTextState = Window_Base.prototype.createTextState;
                Window_Base.prototype.createTextState = function(text, x, y, width) {
                    return _origCreateTextState.call(this, _replace(text), x, y, width);
                };
                _diagWrite('hooked Window_Base.createTextState');
            }
        }

        if (typeof Window_Help !== 'undefined' && Window_Help.prototype && Window_Help.prototype.setText) {
            var _origHelpSetText = Window_Help.prototype.setText;
            Window_Help.prototype.setText = function(text) {
                return _origHelpSetText.call(this, _replace(text));
            };
            _diagWrite('hooked Window_Help.setText');
        }

        if (typeof Window_Command !== 'undefined' && Window_Command.prototype && Window_Command.prototype.addCommand) {
            var _origAddCommand = Window_Command.prototype.addCommand;
            Window_Command.prototype.addCommand = function(name, symbol, enabled, ext) {
                return _origAddCommand.call(this, _replace(name), symbol, enabled, ext);
            };
            _diagWrite('hooked Window_Command.addCommand');
        }

        // Hook 3: Bitmap.drawText — 底层文本渲染
        if (typeof Bitmap !== 'undefined' && Bitmap.prototype && Bitmap.prototype.drawText) {
            var _origBmpDraw = Bitmap.prototype.drawText;
            Bitmap.prototype.drawText = function(text, x, y, mw, lh, al) {
                return _origBmpDraw.call(this, _replace(text), x, y, mw, lh, al);
            };
            _diagWrite('hooked Bitmap.drawText');
        }

        // Hook 4: Window_Message / Window_ChoiceList etc. — more specific hooks
        if (typeof Window_Message !== 'undefined' && Window_Message.prototype) {
            if (Window_Message.prototype.newPage) {
                var _origNewPage = Window_Message.prototype.newPage;
                var self = this;
                Window_Message.prototype.newPage = function(text) {
                    return _origNewPage.call(this, _replace(text));
                };
                _diagWrite('hooked Window_Message.newPage');
            }
        }

        _diagWrite('hookReplace done, ready. replaceCount=' + _replaceCount + ' replaceHit=' + _replaceHit);
    }

    function launchReplace() {
        // Install before the first Scene_Boot resource request.
        _installResourcePathHook();
        var ok = loadReplaceMap();

        // 立即 hook，不等 Scene_Boot（Bitmap/Game_Message 在脚本加载时已定义）
        if (ok) {
            _diagWrite('launchReplace: map loaded, hooking now...');
            // 小延迟确保所有类都已完全初始化
            setTimeout(hookReplace, 500);
        } else {
            _diagWrite('launchReplace: FAILED to load map, no hooks installed');
        }

        // 也写一份标题诊断
        try {
            document.title = '[GT] map=' + Object.keys(TRANSLATION_MAP).length + ' count=' + _replaceCount;
        } catch(e) {}
    }

    // ---- INIT ----
    _diagWrite('INIT mode=' + MODE);
    if (MODE === "scan") launchScan();
    else if (MODE === "replace") launchReplace();
})();
