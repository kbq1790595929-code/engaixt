function on_meta(key, val) {
    if (key === "stage_failure") {
        const failure = (val && typeof val === "object") ? val : {};
        const message = String(failure.user_message || t("当前阶段执行失败"));
        pushLog("error", message);
        const detail = String(failure.technical_detail || "").trim();
        if (detail) pushLog("warn", t("技术详情: ") + detail);
        const fallback = document.getElementById("krkr-fallback-btn");
        const canRealtime = failure.fallback === "realtime" || failure.fallback_available === true;
        if (fallback) {
            fallback.hidden = !canRealtime;
            fallback.disabled = !canRealtime;
        }
        on_status("失败");
    } else if (key === "text_count") {
        document.getElementById("stat-text-count").textContent = val;
        document.getElementById("stat-files").hidden = true;
        showStats();
    } else if (key === "file_count") {
        document.getElementById("stat-file-count").textContent = val;
        document.getElementById("stat-files").hidden = false;
    } else if (key === "extraction_stats") {
        const stats = (val && typeof val === "object") ? val : {};
        window._engaixtExtractionStats = {
            text_count: Number(stats.text_count || 0),
            source_chars: Number(stats.source_chars || 0),
            file_count: Number(stats.file_count || 0),
            path: String(stats.path || "")
        };
        const count = document.getElementById("stat-text-count");
        const files = document.getElementById("stat-file-count");
        const fileStat = document.getElementById("stat-files");
        if (count) count.textContent = window._engaixtExtractionStats.text_count.toLocaleString();
        if (files) files.textContent = window._engaixtExtractionStats.file_count.toLocaleString();
        if (fileStat) fileStat.hidden = false;
        const selectedPath = document.getElementById("path-input")?.value || "";
        if (!stats.path || selectedPath === stats.path) {
            if (typeof refreshMainCostEstimate === "function") {
                refreshMainCostEstimate(selectedPath, stats.text_count, stats.source_chars);
            }
        }
        showStats();
    } else if (key === "local_speed") {
        const speed = (val && typeof val === "object") ? Number(val.current_tps) : 0;
        const speedEl = document.getElementById("local-speed");
        if (speedEl && Number.isFinite(speed) && speed > 0) {
            speedEl.textContent = `${speed.toFixed(1)} tokens/s`;
            speedEl.hidden = false;
        }
    } else if (key === "preflight_result") {
        const result = (val && typeof val === "object") ? val : {};
        const selectedPath = document.getElementById("path-input")?.value || "";
        if (!result.path || selectedPath === result.path) {
            if (result.status === "success" || result.status === "skipped" || result.status === "failed") {
                enableButtons(true);
            }
        }
    } else if (key === "translate_progress") {
        const current = Number(val && val.current);
        const total = Number(val && val.total);
        const prev = _taskProgressGuards.translate || { current: 0, total: 0 };
        if (Number.isFinite(current) && Number.isFinite(total)) {
            if (current < prev.current && total <= prev.total) return;
            _taskProgressGuards.translate = { current, total };
        }
        document.getElementById("stat-trans-cur").textContent = val.current;
        document.getElementById("stat-trans-total").textContent = val.total;
        showStats();
    }
}

let _taskProgressGuards = {};
window._engaixtExtractionStats = null;

function hideLocalSpeed() {
    const speedEl = document.getElementById("local-speed");
    if (speedEl) {
        speedEl.hidden = true;
        speedEl.textContent = "";
    }
}

function _resetTaskProgressGuards() {
    _taskProgressGuards = {};
}

function _progressGuardKey(step) {
    const text = String(step || "");
    if (text.includes("润色")) return "polish";
    if (text.includes("翻译")) return "translate";
    return "";
}

function _shouldDropRegressiveProgress(step) {
    const key = _progressGuardKey(step);
    if (!key) return false;
    const match = String(step || "").match(/\((\d+)\/(\d+)\)/);
    if (!match) return false;
    const current = Number(match[1]);
    const total = Number(match[2]);
    if (!Number.isFinite(current) || !Number.isFinite(total)) return false;
    const prev = _taskProgressGuards[key] || { current: 0, total: 0 };
    if (current < prev.current && total <= prev.total) return true;
    _taskProgressGuards[key] = { current, total };
    return false;
}

function showStats() {
    const row = document.getElementById("stats-row");
    if (row && row.hidden) row.hidden = false;
}

function hideStats() {
    const row = document.getElementById("stats-row");
    if (row) row.hidden = true;
}

function _formatProgressBytes(bytes) {
    const value = Number(bytes || 0);
    if (!Number.isFinite(value) || value <= 0) return "0 MB";
    if (value >= 1024 * 1024 * 1024) return (value / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    return (value / (1024 * 1024)).toFixed(1) + " MB";
}

function _progressDetailText(detail) {
    const data = detail || {};
    const downloaded = Number(data.downloaded || 0);
    const size = Number(data.size || 0);
    if (size > 0 && downloaded >= 0) {
        return `${_formatProgressBytes(downloaded)} / ${_formatProgressBytes(size)}`;
    }
    return "";
}

function on_progress(step, pct, detail) {
    if (_shouldDropRegressiveProgress(step)) return;
    document.getElementById("progress-fill").style.width = pct + "%";
    const detailText = _progressDetailText(detail);
    document.getElementById("progress-text").textContent = detailText
        ? `${Math.round(pct)}% · ${detailText}`
        : Math.round(pct) + "%";
    const stepEl = document.getElementById("status-step");
    const pctEl = document.getElementById("status-pct");
    if (stepEl) {
        stepEl.dataset.rawStep = step || "";
        stepEl.textContent = t(step || "");
    }
    if (pctEl) pctEl.textContent = detailText || (Math.round(pct) + "%");
    // 100% 到达时更新状态 + 清理
    if (pct >= 100 && !String(step || "").includes("preflight")) {
        hideLocalSpeed();
        const dot = document.getElementById("status-dot");
        dot.className = "dot idle";
        const statusText = document.getElementById("status-text");
        statusText.dataset.rawStatus = "完成";
        statusText.textContent = t("完成");
        document.querySelector(".progress-bar").classList.remove("running");
        enableButtons(true);
        setTimeout(hideStats, 2000);
        if (_isStatisticsPageActive()) loadUsageStatistics(true);
    }
}

function on_log(level, msg) {
    const cls = level >= 50 ? "error" : level >= 40 ? "warn" : "info";
    appendLog(cls, msg);
}

function on_status(status) {
    const dot = document.getElementById("status-dot");
    const running = ["运行中", "安装中", "更新中", "清理中", "卸载中"].includes(status);
    dot.className = "dot";
    if (running) {
        dot.classList.add("running");
        const statusText = document.getElementById("status-text");
        statusText.dataset.rawStatus = status;
        statusText.textContent = t(status);
    } else if (status === "空闲" || status === "完成") {
        dot.classList.add("idle");
        const statusText = document.getElementById("status-text");
        statusText.dataset.rawStatus = status;
        statusText.textContent = t(status);
        const stepEl = document.getElementById("status-step");
        const pctEl = document.getElementById("status-pct");
        if (stepEl) {
            stepEl.dataset.rawStep = status === "完成" ? "完成" : "就绪";
            stepEl.textContent = status === "完成" ? t("完成") : t("就绪");
        }
        if (pctEl) pctEl.textContent = "";
    } else {
        dot.classList.add("error");
        const statusText = document.getElementById("status-text");
        statusText.dataset.rawStatus = status;
        statusText.textContent = t(status);
    }
    // 进度条流光只在运行中显示
    const bar = document.querySelector(".progress-bar");
    if (bar) bar.classList.toggle("running", running);
}

// ── 日志 ──

function appendLog(cls, msg) {
    const area = document.getElementById("log-area");
    const line = document.createElement("div");
    line.className = "line " + cls;
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = "[" + new Date().toLocaleTimeString(uiLocale(), { hour12: false }) + "]";
    line.appendChild(ts);
    line.appendChild(document.createTextNode(msg));
    area.appendChild(line);
    while (area.childElementCount > 1000) area.firstElementChild.remove();
    area.scrollTop = area.scrollHeight;
}

function pushLog(level, msg) {
    const cls = level === "error" ? "error" : level === "warn" ? "warn" : "info";
    appendLog(cls, msg);
}

function clearLog() {
    document.getElementById("log-area").innerHTML = "";
}

function resetRun() {
    clearLog();
    _resetTaskProgressGuards();
    window._engaixtExtractionStats = null;
    hideLocalSpeed();
    document.getElementById("progress-fill").style.width = "0%";
    document.getElementById("progress-text").textContent = "0%";
    const stepEl = document.getElementById("status-step");
    if (stepEl) {
        stepEl.dataset.rawStep = "就绪";
        stepEl.textContent = t("就绪");
    }
    hideStats();
    const fallback = document.getElementById("krkr-fallback-btn");
    if (fallback) {
        fallback.hidden = true;
        fallback.disabled = true;
    }
}

// ── Drop Zone ──

const dropZone = document.getElementById("drop-zone");
const pathInput = document.getElementById("path-input");
let _setPathSeq = 0;
let _engineDetectSeq = 0;

async function setPath(path) {
    const seq = ++_setPathSeq;
    // 快捷方式 / Steam 入口会解析到真实路径；手动选择的 .exe 会保留为启动入口。
    if (shouldResolvePath(path)) {
        try {
            const resolved = await pywebview.api.resolve_path(path);
            if (seq !== _setPathSeq) return;
            if (resolved) path = resolved;
        } catch (_) {}
    }
    if (seq !== _setPathSeq) return;

    // ── 顶出：已有文件时，新文件替换旧文件 ──
    const wasDone = dropZone.classList.contains("done");
    if (wasDone) {
        // 播放"顶出"动画：旧路径滑出，新路径滑入
        const oldText = dropZone.querySelector(".path-text");
        oldText.classList.add("push-out");
        // 重置翻译状态
        resetUI();
        setTimeout(() => oldText.classList.remove("push-out"), 300);
    }

    pathInput.value = path;
    pathInput.title = path;
    const pathText = dropZone.querySelector(".path-text");
    pathText.textContent = path;
    pathText.title = path;
    dropZone.classList.add("done");
    detectEngine(path);
}

function shouldResolvePath(path) {
    if (!path) return false;
    const p = String(path).toLowerCase();
    return p.startsWith("steam://")
        || p.endsWith(".lnk")
        || p.endsWith(".url")
        || p.endsWith(".exe")
        || p.endsWith(".acf")
        || p.endsWith("steam_appid.txt");
}

// 重置 UI 到初始状态（保留日志）
function resetUI() {
    _resetTaskProgressGuards();
    window._engaixtExtractionStats = null;
    hideLocalSpeed();
    document.getElementById("progress-fill").style.width = "0%";
    document.getElementById("progress-text").textContent = "0%";
    const statusStep = document.getElementById("status-step");
    statusStep.dataset.rawStep = "就绪";
    statusStep.textContent = t("就绪");
    document.getElementById("status-pct").hidden = true;
    document.getElementById("stats-row").hidden = true;
    document.getElementById("stat-text-count").textContent = "0";
    document.getElementById("stat-trans-cur").textContent = "0";
    document.getElementById("stat-trans-total").textContent = "0";
    document.getElementById("stat-file-count").textContent = "0";
    const costStat = document.getElementById("stat-cost-estimate");
    if (costStat) costStat.hidden = true;
    document.getElementById("engine-banner").hidden = true;
    _engCache = {};
    enableButtons(true);
}

let _engCache = {};

async function getEngineInfo(path) {
    if (_engCache[path]) return _engCache[path];
    try {
        const info = await pywebview.api.get_engine_info(path);
        _engCache[path] = info;
        if (pathInput.value === path) _showEngine(info);
        return info;
    } catch (e) {
        return null;
    }
}

async function detectEngine(path) {
    const seq = ++_engineDetectSeq;
    const banner = document.getElementById("engine-banner");
    if (_engCache[path]) {
        if (seq !== _engineDetectSeq || pathInput.value !== path) return;
        _showEngine(_engCache[path]);
        schedulePreflightExtraction(path, _engCache[path]);
        return;
    }
    banner.hidden = true;
    try {
        const info = await pywebview.api.get_engine_info(path);
        _engCache[path] = info;
        if (seq !== _engineDetectSeq || pathInput.value !== path) return;
        _showEngine(info);
        schedulePreflightExtraction(path, info);
    } catch (e) {
        if (seq !== _engineDetectSeq || pathInput.value !== path) return;
        banner.hidden = true;
    }
}

let _preflightPath = "";

function _shouldPreflightExtract(info) {
    if (!info || !info.name || _isRealtimeOnlyEngine(info)) return false;
    if (info.name === "unknown" || info.name === "error") return false;
    const capabilities = info?.["capabilities"] || {};
    return capabilities.extract !== false && capabilities.supports_extract !== false;
}

async function schedulePreflightExtraction(path, info) {
    if (!_shouldPreflightExtract(info)) return;
    if (!window.pywebview || !pywebview.api?.preflight_extract) return;
    if (path !== pathInput.value) return;
    _preflightPath = path;
    hideLocalSpeed();
    enableButtons(false);
    pushLog("info", t("正在预检：解包并提取文本，暂不开始翻译"));
    try {
        await pywebview.api.preflight_extract(path, String(info.name || ""));
    } catch (error) {
        if (_preflightPath === path) {
            enableButtons(true);
            pushLog("error", t("预检提取失败: ") + ((error && error.message) || error));
        }
    }
}

function _isRealtimeOnlyEngine(info) {
    const name = String(info?.name || "").toLowerCase();
    return ["unity", "xunity_realtime", "unity_arch000_lua"].includes(name);
}

function _realtimeOnlyMessage(info) {
    const label = info?.label || info?.name || t("当前引擎");
    return `${label} ${t("只能使用“实时翻译”。请点击右侧的“实时翻译”按钮启动，不要走“开始翻译”静态流程。")}`;
}

function _showEngine(info) {
    const banner = document.getElementById("engine-banner");
    if (!info || info.name === "error" || info.name === "unknown") {
        banner.hidden = true;
        return;
    }
    document.getElementById("eng-name").textContent = info.label;
    const badge = document.getElementById("eng-badge");
    const level = info.support_level || "unknown";
    badge.textContent = _supportLevelLabel(level);
    badge.className = "eng-badge " + level;
    const caps = document.getElementById("eng-caps");
    if (caps) caps.innerHTML = "";
    const limits = document.getElementById("eng-limits");
    if (limits) limits.innerHTML = "";
    banner.hidden = false;
}

function _supportLevelLabel(level) {
    const labels = {
        stable: t("稳定版"),
        beta: t("Beta 版"),
        experimental: t("实验性"),
        partial: t("部分支持"),
        planned: t("计划中"),
        unknown: t("未知")
    };
    return labels[level] || level || t("未知");
}

dropZone.addEventListener("dragover", e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    dropZone.classList.add("dragover");
});
dropZone.addEventListener("dragleave", () => { dropZone.classList.remove("dragover"); });
dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("dragover");

    // 1) pywebview 注入的 File 对象（优先）
    const files = e.dataTransfer.files;
    if (files.length > 0) {
        const p = files[0].pywebviewFullPath || files[0].path;
        if (p) { setPath(p); return; }
    }

    // 2) 降级：从 dataTransfer 文本中提取路径/Steam URI（兼容 WebView2 桌面拖拽）
    const raw = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text");
    if (raw) {
        // 去掉 file:// 前缀和换行符，decode 特殊字符
        let p = raw.replace(/^file:\/\/\/?/i, "").split(/\r?\n/)[0].trim();
        try { p = decodeURIComponent(p); } catch (_) {}
        // Windows 路径可能以 /C:/ 开头，去掉多余斜杠
        p = p.replace(/^\/([A-Za-z]:\/)/, "$1");
        if (p) { setPath(p); return; }
    }
});
dropZone.addEventListener("click", browseFile);

// ── 文件选择 ──

async function browseFolder() {
    const path = await pywebview.api.select_directory();
    if (path) setPath(path);
}

async function browseFile() {
    const path = await pywebview.api.select_file();
    if (path) setPath(path);
}

// ── 工具函数 ──

function getPath() {
    const p = pathInput.value.trim();
    if (!p) { pushLog("warn", t("请先选择游戏路径")); return ""; }
    return p;
}

function getCoverage() {
    return parseInt(document.getElementById("coverage").value) || 100;
}

function updateCoverage(el) {
    document.getElementById("coverage-val").textContent = el.value + "%";
    const pct = (el.value - el.min) / (el.max - el.min) * 100;
    el.style.setProperty("--p", pct + "%");
}

function enableButtons(en) {
    document.querySelectorAll(".actions-btn").forEach(b => b.disabled = !en);
}

async function openSupportLink(key) {
    try {
        if (window.pywebview && pywebview.api && pywebview.api.open_support_link) {
            await pywebview.api.open_support_link(key);
        } else {
            const fallback = {
                official: "https://engaixt.com/",
                feedback: "https://github.com/kbq1790595929-code/engaixt/issues/new",
            }[key];
            if (fallback) window.open(fallback, "_blank", "noopener");
        }
    } catch (e) {
        pushLog("warn", `${t("打开链接失败")}: ${e && e.message ? e.message : e}`);
    }
}

async function _runAction(action, label) {
    enableButtons(false);
    try {
        return await action();
    } catch (e) {
        const msg = (e && e.message) ? e.message : String(e || t("未知错误"));
        pushLog("error", (label ? label + t("失败: ") : t("操作失败: ")) + msg);
        on_status("失败");
        return null;
    } finally {
        enableButtons(true);
    }
}

// ── 页面导航 ──

let _activePage = "main";

function navTo(page) {
    if (page === "games") {
        closeStatistics();
        openTranslatedGames();
        _activePage = "games";
        return;
    }
    if (page === "statistics") {
        closeTranslatedGames(false);
        openStatistics();
        _activePage = "statistics";
        return;
    }
    closeStatistics();
    closeTranslatedGames(false);
    document.getElementById("main-page").classList.add("active");
    _setNavActive("main");
    _activePage = "main";
}

function _setNavActive(page) {
    const navT = document.getElementById("nav-translate");
    const navG = document.getElementById("nav-games");
    const navS = document.getElementById("nav-statistics");
    if (navT) navT.classList.toggle("active", page === "main");
    if (navG) navG.classList.toggle("active", page === "games");
    if (navS) navS.classList.toggle("active", page === "statistics");
}

// ── 开始翻译 ──

async function startTranslation() {
    const path = getPath();
    if (!path) return;
    const info = await getEngineInfo(path);
    if (_isRealtimeOnlyEngine(info)) {
        const msg = _realtimeOnlyMessage(info);
        pushLog("warn", msg);
        alert(msg);
        return;
    }
    const injector = document.getElementById("injector").value;
    const coverage = getCoverage();
    resetRun();
    // auto_launch 由后端从配置读取
    await _runAction(() => pywebview.api.run_translation(path, injector, coverage), t("开始翻译"));
}

// ── 仅提取 ──

async function extractOnly() {
    const path = getPath();
    if (!path) return;
    resetRun();
    await _runAction(() => pywebview.api.extract_only(path), t("仅提取"));
}

// ── 从 JSON 回填 ──

async function patchFromJson() {
    const path = getPath();
    if (!path) return;
    const jsonPath = await pywebview.api.select_json_file();
    if (!jsonPath) { pushLog("warn", t("未选择 JSON 文件")); return; }
    resetRun();
    await _runAction(() => pywebview.api.patch_from_json(path, jsonPath), t("从 JSON 回填"));
}

// ── 文本润色 ──

async function polishTranslation() {
    const path = getPath();
    if (!path) return;
    if (!confirm(t("将使用已有翻译检查点进行低成本文本润色，默认预算约 1 元。继续吗？"))) return;
    resetRun();
    await _runAction(() => pywebview.api.polish_translation(path, 1.0), t("文本润色"));
}

// ── 清除缓存 ──

async function clearCache() {
    await _runAction(() => pywebview.api.clear_cache(), t("清除缓存"));
}

// ── 实时翻译 ──

async function startRealtime() {
    const path = getPath();
    if (!path) return;
    const src = document.getElementById("src-lang")?.value || "ja";
    const tgt = document.getElementById("tgt-lang")?.value || "zh-CN";
    await _runAction(() => pywebview.api.run_realtime(path, src, tgt), t("实时翻译"));
}

// ── 设置面板 ──

let currentTab = "api";

function openHelp() {
    document.getElementById("help-overlay").classList.add("open");
    requestAnimationFrame(() => {
        const first = document.querySelector("#help-overlay .help-nav button");
        if (first) jumpHelpEngine("help-engine-bgi", first, true);
    });
}
function closeHelp() {
    document.getElementById("help-overlay").classList.remove("open");
}

function jumpHelpEngine(id, button, instant) {
    const scroll = document.getElementById("help-scroll");
    const target = document.getElementById(id);
    if (!scroll || !target) return;
    document.querySelectorAll("#help-overlay .help-nav button").forEach(b => b.classList.remove("active"));
    if (button) button.classList.add("active");
    document.querySelectorAll("#help-overlay .engine-help-card").forEach(card => card.classList.remove("selected"));
    target.classList.add("selected");
    const scrollRect = scroll.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const top = scroll.scrollTop + targetRect.top - scrollRect.top - 8;
    scroll.scrollTo({ top: Math.max(0, top), behavior: instant ? "auto" : "smooth" });
}

function openSettings() {
    document.getElementById("settings-overlay").classList.add("open");
    loadSettings();
    refreshAppUpdateInfo();
}
function closeSettings() {
    document.getElementById("settings-overlay").classList.remove("open");
}
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    document.querySelector(`[data-tab="${tab}"]`).classList.add("active");
    document.getElementById(`tab-${tab}`).classList.add("active");
}

function showConcurrencyWarning() {
    alert("云端并发数会直接按这里的设置执行，仅作用于云端 API，不影响 Hy-MT2 本地推理。数值太高时，部分模型或代理线路可能卡死、超时、429 限流，甚至让整批翻译长时间停住。\n\n建议先用 30~100 测试；如果日志稳定、没有超时和限流，再逐步提高。Kimi、部分国内模型和网络代理环境尤其要谨慎。");
}

function setSecretInputVisible(input, visible) {
    if (!input) return;
    input.type = visible ? "text" : "password";
    const wrap = input.closest(".secret-input");
    const btn = wrap ? wrap.querySelector(".secret-toggle") : null;
    if (wrap) wrap.classList.toggle("revealed", visible);
    if (btn) {
        const label = visible ? "隐藏 API Key" : "显示 API Key";
        btn.setAttribute("aria-label", label);
        btn.title = label;
        const use = btn.querySelector("use");
        if (use) use.setAttribute("href", visible ? "#i-eye-off" : "#i-eye");
    }
}

function resetSecretInputs() {
    document.querySelectorAll("#settings-overlay .secret-input input").forEach(input => {
        setSecretInputVisible(input, false);
    });
}

function toggleSecretInput(name) {
    const input = document.querySelector(`#settings-overlay input[name="${name}"]`);
    if (!input) return;
    const visible = input.type === "password";
    setSecretInputVisible(input, visible);
    input.focus();
}

let _editableContextTarget = null;
let _editableContextSelection = null;

function _isEditableContextTarget(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    if (tag === "textarea") return !target.disabled && !target.readOnly;
    if (tag !== "input") return false;
    const type = String(target.type || "text").toLowerCase();
    if (["button", "checkbox", "color", "file", "image", "radio", "range", "reset", "submit"].includes(type)) return false;
    return !target.disabled && !target.readOnly;
}

function _ensureEditableContextMenu() {
    let menu = document.getElementById("editable-context-menu");
    if (menu) return menu;
    menu = document.createElement("div");
    menu.id = "editable-context-menu";
    menu.className = "input-context-menu";
    menu.hidden = true;
    menu.innerHTML = `
        <button type="button" data-action="paste">粘贴</button>
        <button type="button" data-action="copy">复制</button>
        <button type="button" data-action="cut">剪切</button>
        <button type="button" data-action="selectAll">全选</button>
    `;
    document.body.appendChild(menu);
    menu.addEventListener("click", _handleEditableContextAction);
    return menu;
}

function _hideEditableContextMenu() {
    const menu = document.getElementById("editable-context-menu");
    if (menu) menu.hidden = true;
}

async function _readClipboardText() {
    try {
        if (window.pywebview && pywebview.api && pywebview.api.get_clipboard_text) {
            return await pywebview.api.get_clipboard_text();
        }
    } catch (_) {}
    try {
        if (navigator.clipboard && navigator.clipboard.readText) {
            return await navigator.clipboard.readText();
        }
    } catch (_) {}
    return "";
}

async function _writeClipboardText(text) {
    try {
        if (window.pywebview && pywebview.api && pywebview.api.set_clipboard_text) {
            return await pywebview.api.set_clipboard_text(text);
        }
    } catch (_) {}
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (_) {}
    return false;
}

function _editableSelection(input) {
    if (_editableContextSelection && _editableContextSelection.input === input) {
        return _editableContextSelection;
    }
    const start = input.selectionStart ?? 0;
    const end = input.selectionEnd ?? start;
    return { input, start, end };
}

function _selectedEditableText(input) {
    const { start, end } = _editableSelection(input);
    return String(input.value || "").slice(start, end);
}

function _replaceEditableSelection(input, text) {
    const selection = _editableSelection(input);
    const start = selection.start ?? String(input.value || "").length;
    const end = selection.end ?? start;
    const value = String(input.value || "");
    input.value = value.slice(0, start) + text + value.slice(end);
    const caret = start + text.length;
    try {
        input.setSelectionRange(caret, caret);
    } catch (_) {}
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
}

async function _handleEditableContextAction(event) {
    const button = event.target.closest("button[data-action]");
    if (!button || !_editableContextTarget) return;
    const input = _editableContextTarget;
    const action = button.dataset.action;
    event.preventDefault();
    input.focus();
    const selection = _editableSelection(input);
    try {
        input.setSelectionRange(selection.start, selection.end);
    } catch (_) {}

    if (action === "paste") {
        const text = await _readClipboardText();
        if (text) _replaceEditableSelection(input, text);
    } else if (action === "copy") {
        const selected = _selectedEditableText(input) || String(input.value || "");
        await _writeClipboardText(selected);
    } else if (action === "cut") {
        const selected = _selectedEditableText(input);
        if (selected) {
            await _writeClipboardText(selected);
            _replaceEditableSelection(input, "");
        }
    } else if (action === "selectAll") {
        input.select();
    }
    _hideEditableContextMenu();
    _editableContextSelection = null;
}

function installEditableContextMenu() {
    _ensureEditableContextMenu();
    document.addEventListener("contextmenu", function(event) {
        if (!_isEditableContextTarget(event.target)) {
            _hideEditableContextMenu();
            return;
        }
        event.preventDefault();
        _editableContextTarget = event.target;
        _editableContextSelection = {
            input: event.target,
            start: event.target.selectionStart ?? 0,
            end: event.target.selectionEnd ?? event.target.selectionStart ?? 0,
        };
        const menu = _ensureEditableContextMenu();
        menu.hidden = false;
        const margin = 8;
        const rect = menu.getBoundingClientRect();
        const left = Math.min(event.clientX, window.innerWidth - rect.width - margin);
        const top = Math.min(event.clientY, window.innerHeight - rect.height - margin);
        menu.style.left = Math.max(margin, left) + "px";
        menu.style.top = Math.max(margin, top) + "px";
    });
    document.addEventListener("click", function(event) {
        const menu = document.getElementById("editable-context-menu");
        if (menu && !menu.hidden && !menu.contains(event.target)) _hideEditableContextMenu();
    });
    document.addEventListener("keydown", function(event) {
        if (event.key === "Escape") _hideEditableContextMenu();
    });
    window.addEventListener("resize", _hideEditableContextMenu);
    window.addEventListener("scroll", _hideEditableContextMenu, true);
}

async function loadSettings() {
    const c = await pywebview.api.get_config();
    if (c.ui_language) setUiLanguage(c.ui_language, true);
    if (c.ui_theme) {
        applyTheme(c.ui_theme, { initial: true });
    } else {
        syncThemeUi();
    }
    for (const [key, val] of Object.entries(c)) {
        document.querySelectorAll(`[name="${key}"]`).forEach(el => {
            setFormControlValue(el, val);
            if (el.type === "range") updateRangeValue(el);
        });
    }
    if (c.translation_coverage !== undefined) {
        const coverageEl = document.getElementById("coverage");
        if (coverageEl) {
            coverageEl.value = Math.max(10, Math.min(100, parseInt(c.translation_coverage) || 100));
            updateCoverage(coverageEl);
        }
    }
    // 配置里的颜色可能是另一套主题的出厂默认值（用户从没改过），
    // 那种情况下不要拿它去覆盖当前主题 —— 否则切到浅色主题会顶着一片黑底。
    if (c.ui_accent && !isOtherThemeDefault(THEME_ACCENT_FACTORY, c.ui_accent)) {
        applyAccent(c.ui_accent);
    }
    if (c.bg_color && !isOtherThemeDefault(THEME_BG_FACTORY, c.bg_color)) {
        document.getElementById("bg-picker").value = c.bg_color;
        setBgColor(c.bg_color);
    }
    if (c.bg_image) {
        _bgImagePath = c.bg_image;
        applyBgImage(c.bg_image);
        document.getElementById("bg-image-path").value = c.bg_image;
        document.getElementById("clear-bg-btn").hidden = false;
    }
    resetSecretInputs();
    onTranslatorChanged(c.active_translator || "deepseek");
    if (typeof refreshCloudModelState === "function") refreshCloudModelState();
}

function setFormControlValue(el, val) {
    if (!el) return;
    if (typeof cloudDisplayModelValue === "function" && cloudDisplayModelValue(el, val)) return;
    if (el.type === "checkbox") {
        el.checked = !!val;
        return;
    }
    const text = val == null ? "" : String(val);
    if (el.tagName && el.tagName.toLowerCase() === "select" && text && !_selectHasValue(el, text)) {
        const opt = document.createElement("option");
        opt.value = text;
        opt.textContent = text + "（当前配置）";
        opt.dataset.currentConfig = "1";
        el.appendChild(opt);
    }
    el.value = text;
}

function _selectHasValue(select, value) {
    return Array.from(select.options || []).some(opt => opt.value === value);
}

function syncSettingValue(name, value) {
    document.querySelectorAll(`#settings-overlay [name="${name}"]`).forEach(el => {
        if (el.value !== value) setFormControlValue(el, value);
        if (el.type === "range") updateRangeValue(el);
    });
}

function updateRangeValue(el) {
    if (!el) return;
    const target = document.querySelector(`#settings-overlay .range-value[data-for="${el.name}"]`);
    if (target) target.textContent = el.value;
}

let _overlayAutoSaveTimer = null;

function collectSettings(selector) {
    const data = {};
    document.querySelectorAll(`${selector} [name]`).forEach(el => {
        data[el.getAttribute("name")] = el.type === "checkbox" ? el.checked : el.value;
    });
    return data;
}

function scheduleOverlaySettingsSave() {
    if (_overlayAutoSaveTimer) clearTimeout(_overlayAutoSaveTimer);
    _overlayAutoSaveTimer = setTimeout(saveOverlaySettingsRealtime, 250);
}

async function saveOverlaySettingsRealtime() {
    _overlayAutoSaveTimer = null;
    if (!window.pywebview || !pywebview.api) return;
    try {
        await pywebview.api.save_config(collectSettings("#tab-overlay-ui"));
    } catch (e) {
        console.error("Failed to save overlay settings:", e);
    }
}

function bindOverlaySettingsRealtime() {
    document.querySelectorAll("#tab-overlay-ui [name]").forEach(el => {
        const handler = () => {
            if (el.type === "range") updateRangeValue(el);
            scheduleOverlaySettingsSave();
        };
        el.addEventListener("input", handler);
        el.addEventListener("change", handler);
    });
}

function onUiLanguageChanged(value) {
    setUiLanguage(value, true);
}

var _bgImagePath = "";

async function pickBgImage() {
    var path = await pywebview.api.select_file();
    if (!path) return;
    _bgImagePath = path;
    await applyBgImage(path);
    document.getElementById("bg-image-path").value = path;
    document.getElementById("clear-bg-btn").hidden = false;
}

function clearBgImage() {
    _bgImagePath = "";
    applyBgImage("");
    document.getElementById("bg-image-path").value = "";
    document.getElementById("clear-bg-btn").hidden = true;
}

async function applyBgImage(path) {
    if (path) {
        try {
            var dataUrl = await pywebview.api.read_bg_image(path);
            if (dataUrl) {
                document.body.style.backgroundImage = "url(" + dataUrl + ")";
                document.body.style.backgroundSize = "cover";
                document.body.style.backgroundPosition = "center";
                document.body.style.backgroundAttachment = "fixed";
                document.querySelector(".aurora").style.display = "none";
                document.querySelector(".grain").style.display = "none";
                document.body.style.backgroundColor = "transparent";
            }
        } catch (e) { console.error("Failed to load bg image:", e); }
    } else {
        document.body.style.backgroundImage = "";
        document.body.style.backgroundSize = "";
        document.body.style.backgroundPosition = "";
        document.body.style.backgroundAttachment = "";
        document.querySelector(".aurora").style.display = "";
        document.querySelector(".grain").style.display = "";
        document.body.style.backgroundColor = "";
    }
}

// ── 主题 ──
// 两套主题各自记住用户改过的背景色 / 主色调。某个值等于该主题的默认值时，
// 就撤掉 inline 覆盖，把控制权交回 CSS 的 [data-theme] 令牌块 ——
// 那里定义的立体层次（--ridge / --raise / --surface-*）比 JS 推算的更精确。
var THEME_BG = { dark: "#08080d", light: "#edf1f5" };
var THEME_ACCENT = { dark: "#8b7cff", light: "#1769b0" };
// 出厂默认值，运行期不会被改写；用来识别"旧配置里存的是另一套主题的默认色"
var THEME_BG_FACTORY = { dark: "#08080d", light: "#edf1f5" };
var THEME_ACCENT_FACTORY = { dark: "#8b7cff", light: "#1769b0" };

function isOtherThemeDefault(map, hex) {
    var cur = currentTheme();
    var v = (hex || "").toLowerCase();
    return Object.keys(map).some(function (t) { return t !== cur && map[t] === v; });
}

var THEME_TOKEN_VARS = ["--bg-0", "--bg-1", "--bg-card", "--hover-bg",
    "--border", "--border-hi", "--text-hi", "--text", "--text-dim"];
var ACCENT_VARS = ["--accent", "--accent-r", "--accent-g", "--accent-b", "--accent-l",
    "--accent-2", "--accent-2-r", "--accent-2-g", "--accent-2-b",
    "--accent-lt-r", "--accent-lt-g", "--accent-lt-b",
    "--aurora-1", "--aurora-2", "--aurora-3", "--gradient"];

function currentTheme() {
    return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function resetThemeTokens() {
    var s = document.documentElement.style;
    THEME_TOKEN_VARS.forEach(function (k) { s.removeProperty(k); });
}

function syncThemeUi() {
    var t = currentTheme();
    var hidden = document.getElementById("theme-input");
    if (hidden) hidden.value = t;
    ["dark", "light"].forEach(function (n) {
        var el = document.getElementById("theme-opt-" + n);
        if (el) el.classList.toggle("on", n === t);
    });
}

function applyTheme(name, opts) {
    opts = opts || {};
    var next = name === "light" ? "light" : "dark";
    var picker = document.getElementById("bg-picker");
    var accentPicker = document.getElementById("accent-picker");
    if (!opts.initial) {
        // 记住当前主题下的自定义配色，切回来时还原
        var prev = currentTheme();
        if (picker && picker.value) THEME_BG[prev] = picker.value.toLowerCase();
        if (accentPicker && accentPicker.value) THEME_ACCENT[prev] = accentPicker.value.toLowerCase();
    }
    document.documentElement.dataset.theme = next;
    if (picker) picker.value = THEME_BG[next];
    if (accentPicker) accentPicker.value = THEME_ACCENT[next];
    setBgColor(THEME_BG[next]);
    applyAccent(THEME_ACCENT[next]);
    syncThemeUi();
}

function setBgColor(hex) {
    var root = document.documentElement;
    // 只有等于"出厂默认值"时才交还 CSS。必须比对 THEME_BG_FACTORY：
    // applyTheme 会把用户自定义色回写到 THEME_BG[prev]，拿运行时表去比对，
    // 用户的自定义色就会被误判成主题默认，导致色板显示黑、背景却用默认值。
    if ((hex || "").toLowerCase() === THEME_BG_FACTORY[currentTheme()]) {
        resetThemeTokens();
        return;
    }
    root.style.setProperty("--bg-0", hex);
    var r = parseInt(hex.slice(1,3), 16);
    var g = parseInt(hex.slice(3,5), 16);
    var b = parseInt(hex.slice(5,7), 16);
    // bg-1 比 bg-0 稍亮（暗色模式）或稍暗（亮色模式）
    var lum = (r * 299 + g * 587 + b * 114) / 1000;
    var dark = lum < 128;

    if (dark) {
        var r1 = Math.min(255, r + 12);
        var g1 = Math.min(255, g + 12);
        var b1 = Math.min(255, b + 20);
        root.style.setProperty("--bg-card", "rgba(255,255,255,.04)");
        root.style.setProperty("--hover-bg", "rgba(255,255,255,.07)");
        root.style.setProperty("--border", "rgba(255,255,255,.08)");
        root.style.setProperty("--border-hi", "rgba(255,255,255,.16)");
        root.style.setProperty("--text-hi", "#f1f2f9");
        root.style.setProperty("--text", "#b9bed3");
        root.style.setProperty("--text-dim", "#6f7593");
    } else {
        var r1 = Math.max(0, r - 12);
        var g1 = Math.max(0, g - 12);
        var b1 = Math.max(0, b - 20);
        root.style.setProperty("--bg-card", "rgba(0,0,0,.04)");
        root.style.setProperty("--hover-bg", "rgba(0,0,0,.06)");
        root.style.setProperty("--border", "rgba(0,0,0,.08)");
        root.style.setProperty("--border-hi", "rgba(0,0,0,.14)");
        root.style.setProperty("--text-hi", "#1a1a2e");
        root.style.setProperty("--text", "#4a4f66");
        root.style.setProperty("--text-dim", "#7a7f96");
    }
    var hex1 = "#" + [r1,g1,b1].map(function(v){return v.toString(16).padStart(2,"0")}).join("");
    root.style.setProperty("--bg-1", hex1);
}

function applyPreset(hex) {
    document.getElementById("accent-picker").value = hex;
    applyAccent(hex);
}

function applyAccent(hex) {
    if (!hex || hex.length < 7) hex = "#8b7cff";
    // 同理：只跟出厂默认值比对（浅色主题的 aurora 光斑是单独调过的）
    if (hex.toLowerCase() === THEME_ACCENT_FACTORY[currentTheme()]) {
        var st = document.documentElement.style;
        ACCENT_VARS.forEach(function (k) { st.removeProperty(k); });
        return;
    }
    var r = parseInt(hex.slice(1,3), 16);
    var g = parseInt(hex.slice(3,5), 16);
    var b = parseInt(hex.slice(5,7), 16);
    // accent-2: 色轮偏移 + 加亮
    var r2 = Math.min(255, r + 80);
    var g2 = Math.min(255, g + 40);
    var b2 = Math.min(255, b + 160);
    // 亮色变体
    var rL = Math.min(255, r + 40);
    var gL = Math.min(255, g + 20);
    var bL = Math.min(255, b + 100);
    var hexL = "#" + [rL,gL,bL].map(function(v){return v.toString(16).padStart(2,"0")}).join("");
    // 暗色变体 (aurora)
    var rD = Math.max(0, r - 20);
    var gD = Math.max(0, g - 10);
    var bD = Math.max(0, b - 60);
    var rD2 = Math.max(0, r - 60);
    var gD2 = Math.max(0, g + 20);
    var bD2 = Math.max(0, b - 200);
    var rD3 = Math.max(0, r + 60);
    var gD3 = Math.max(0, g - 10);
    var bD3 = Math.max(0, b + 30);

    var root = document.documentElement;
    root.style.setProperty("--accent", hex);
    root.style.setProperty("--accent-r", r); root.style.setProperty("--accent-g", g); root.style.setProperty("--accent-b", b);
    root.style.setProperty("--accent-l", hexL);
    root.style.setProperty("--accent-2", "rgb(" + r2 + "," + g2 + "," + b2 + ")");
    root.style.setProperty("--accent-2-r", r2); root.style.setProperty("--accent-2-g", g2); root.style.setProperty("--accent-2-b", b2);
    root.style.setProperty("--aurora-1", "rgb(" + rD + "," + gD + "," + bD + ")");
    root.style.setProperty("--aurora-2", "rgb(" + Math.max(0,b2-80) + "," + g2 + "," + r2 + ")");
    root.style.setProperty("--aurora-3", "rgb(" + rD3 + "," + gD3 + "," + bD3 + ")");
    // lighter accent (for glow effects)
    var rLt = Math.min(255, r + 15); var gLt = Math.min(255, g + 18); var bLt = Math.min(255, b + 0);
    root.style.setProperty("--accent-lt-r", rLt); root.style.setProperty("--accent-lt-g", gLt); root.style.setProperty("--accent-lt-b", bLt);
    root.style.setProperty("--gradient", "linear-gradient(135deg, " + hex + " 0%, " + hexL + " 45%, rgb(" + r2 + "," + g2 + "," + b2 + ") 100%)");
}

async function saveSettings() {
    const data = {};
    document.querySelectorAll("#settings-overlay [name]").forEach(el => {
        data[el.getAttribute("name")] = el.type === "checkbox" ? el.checked : el.value;
    });
    // 主界面自动启动开关与配置同步
    const cb = document.getElementById("auto-launch");
    if (cb && data.auto_launch !== undefined) cb.checked = data.auto_launch;
    data.bg_image = _bgImagePath;
    await pywebview.api.save_config(data);
    if (data.translation_coverage !== undefined) {
        const coverageEl = document.getElementById("coverage");
        if (coverageEl) {
            coverageEl.value = Math.max(10, Math.min(100, parseInt(data.translation_coverage) || 100));
            updateCoverage(coverageEl);
        }
    }
    if (data.ui_accent) applyAccent(data.ui_accent);
    if (data.ui_language) setUiLanguage(data.ui_language, true);
    closeSettings();
    pushLog("info", t("配置已保存"));
}

// ── 卸载汉化 ──

async function uninstallTranslation() {
    const path = getPath();
    if (!path) return;
    if (!confirm(t("确定要卸载当前游戏的汉化吗？\n\n将恢复被修改的原文件并删除补丁文件。\n翻译缓存会保留。"))) return;
    resetRun();
    await _runAction(() => pywebview.api.uninstall_translation(path), t("卸载汉化"));
}

// ── 诊断报告 ──

async function openDiagnostics() {
    await pywebview.api.open_diagnostics();
}

// ── 安装工具 ──

async function installTools() {
    resetRun();
    await _runAction(() => pywebview.api.install_tools(), t("安装/更新工具"));
}

async function updateTools() {
    resetRun();
    await _runAction(() => pywebview.api.update_tools(), t("检查工具更新"));
}

function renderAppUpdateInfo(info) {
    const el = document.getElementById("app-update-info");
    const nav = document.getElementById("nav-app-update");
    const badge = document.getElementById("nav-app-update-badge");
    const versionBadge = document.getElementById("app-version-badge");
    const data = info || {};
    const version = data.version || "unknown";
    const latest = data.latest_version || "";

    if (versionBadge) versionBadge.textContent = version === "unknown" ? "v?" : `v${version}`;

    if (nav) {
        nav.classList.toggle("update-available", !!(data.update_available && latest && !data.error));
        nav.title = data.update_available && latest
            ? `${t("当前版本")}：v${version} · ${t("最新版本")}：v${latest}`
            : `${t("当前版本")}：v${version}`;
    }
    if (badge) badge.hidden = !(data.update_available && latest && !data.error);

    if (!el) return;
    if (data.manual_update_required) {
        el.textContent = `${t("当前版本")}：v${version} · ${t("暂无可用更新包")}`;
        if (nav) {
            nav.classList.remove("update-available");
            nav.title = `${t("当前版本")}：v${version} · ${t("暂无可用更新包")}`;
        }
        if (badge) badge.hidden = true;
        return;
    }
    if (data.error) {
        el.textContent = `${t("当前版本")}：${data.version || "unknown"} · ${t("软件更新信息读取失败")}`;
        if (nav) {
            nav.classList.remove("update-available");
            nav.title = `${t("当前版本")}：${data.version || "unknown"} · ${t("软件更新信息读取失败")}`;
        }
        if (badge) badge.hidden = true;
        return;
    }
    if (data.update_available && latest) {
        el.textContent = `${t("当前版本")}：v${version} · ${t("最新版本")}：v${latest}`;
    } else {
        el.textContent = `${t("当前版本")}：v${version}`;
    }
}

async function refreshAppUpdateInfo() {
    if (!window.pywebview || !pywebview.api || !pywebview.api.get_app_update_info) return;
    try {
        const info = await pywebview.api.get_app_update_info();
        renderAppUpdateInfo(info);
        if (
            info && info.update_available && info.can_apply_update &&
            info.manifest && Object.keys(info.manifest).length > 0 &&
            pywebview.api.start_background_app_update_predownload
        ) {
            pywebview.api.start_background_app_update_predownload(JSON.stringify(info.manifest)).catch(() => {});
        }
    } catch (e) {
        renderAppUpdateInfo({ version: "unknown", error: String(e || "") });
    }
}

async function updateApp() {
    if (!window.pywebview || !pywebview.api || !pywebview.api.update_app) {
        pushLog("warn", t("当前环境不支持软件更新"));
        return;
    }
    resetRun();
    await _runAction(() => pywebview.api.update_app(), t("软件更新"));
}

// ── CJK 字体选择 ──

async function browseCjkFont() {
    const path = await pywebview.api.select_cjk_font();
    if (path) {
        document.querySelector("[name='cjk_font_path']").value = path;
    }
}

// ── 封面图 & 毛玻璃背景 ──

const _coverCache = {};
let _backdropFlip = false;
let _backdropUri = "";
let _backdropClearTimer = 0;
let _coverLoadToken = 0;
let _coverObserver = null;
let _coverQueue = [];
let _coverQueuedKeys = new Set();
let _coverWorkersActive = 0;
const COVER_CACHE_PREFIX = "gt_cover_v1:";
const COVER_CACHE_TTL_MS = 14 * 24 * 60 * 60 * 1000;
const COVER_CONCURRENCY = 2;
const COVER_PREFETCH_MARGIN = 620;

function _clearBackdrop() {
    if (_backdropClearTimer) {
        clearTimeout(_backdropClearTimer);
        _backdropClearTimer = 0;
    }
    _backdropUri = "";
    const wrap = document.getElementById("games-backdrop");
    if (!wrap) return;
    wrap.classList.remove("on");
    wrap.querySelectorAll(".backdrop-layer").forEach(function(l) { l.classList.remove("show"); });
}

function _setBackdrop(uri) {
    if (_backdropClearTimer) {
        clearTimeout(_backdropClearTimer);
        _backdropClearTimer = 0;
    }
    if (_bgImagePath) return;
    if (uri === _backdropUri) return;
    if (!uri) { _clearBackdrop(); return; }
    const a = document.getElementById("backdrop-a");
    const b = document.getElementById("backdrop-b");
    const wrap = document.getElementById("games-backdrop");
    if (!a || !b || !wrap) return;
    _backdropUri = uri;
    var incoming = _backdropFlip ? b : a;
    var outgoing = _backdropFlip ? a : b;
    _backdropFlip = !_backdropFlip;
    incoming.style.backgroundImage = "url('" + uri + "')";
    incoming.classList.add("show");
    outgoing.classList.remove("show");
    wrap.classList.add("on");
}

function _coverCacheKey(dir) {
    var h = 2166136261;
    var text = String(dir || "");
    for (var i = 0; i < text.length; i++) {
        h ^= text.charCodeAt(i);
        h = Math.imul(h, 16777619);
    }
    return COVER_CACHE_PREFIX + (h >>> 0).toString(36) + ":" + text.length;
}

function _readStoredCover(dir) {
    try {
        var raw = localStorage.getItem(_coverCacheKey(dir));
        if (!raw) return undefined;
        var data = JSON.parse(raw);
        if (!data || Date.now() - Number(data.at || 0) > COVER_CACHE_TTL_MS) {
            localStorage.removeItem(_coverCacheKey(dir));
            return undefined;
        }
        return data.none ? null : { uri: data.uri || "", kind: data.kind || "cover" };
    } catch (_) {
        return undefined;
    }
}

function _writeStoredCover(dir, result) {
    try {
        var payload = result && result.uri
            ? { at: Date.now(), uri: result.uri, kind: result.kind || "cover" }
            : { at: Date.now(), none: true };
        localStorage.setItem(_coverCacheKey(dir), JSON.stringify(payload));
    } catch (_) {
        // localStorage may be full; memory cache still keeps the current session fast.
    }
}

function _applyCoverToCard(games, i, result) {
    if (!result || games !== _carouselGames) return;
    var uri = result.uri || "";
    if (!uri) return;
    if (!games[i]) return;
    games[i].cover = uri;
    var inner = document.querySelector(".carousel-card[data-idx='" + i + "'] .carousel-card-inner");
    if (inner && !inner.classList.contains("has-cover")) {
        inner.classList.add("has-cover");
        inner.style.backgroundImage = "url('" + uri + "')";
        var ov = document.createElement("div");
        ov.className = "card-cover-overlay";
        inner.prepend(ov);
    }
}

function _cachedCoverForDir(dir) {
    if (!dir) return "";
    var result = _coverCache[dir];
    if (result === undefined) {
        result = _readStoredCover(dir);
        if (result !== undefined) _coverCache[dir] = result || null;
    }
    return result && result.uri ? result.uri : "";
}

async function _loadCoverItem(games, item, token) {
    var dir = item.dir;
    try {
        var result = _coverCache[dir];
        if (result === undefined) {
            result = _readStoredCover(dir);
        }
        if (result === undefined) {
            result = await pywebview.api.find_cover(dir);
            result = result || null;
            _writeStoredCover(dir, result);
        }
        _coverCache[dir] = result || null;
        if (token !== _coverLoadToken) return;
        _applyCoverToCard(games, item.index, result);
    } catch (e) {
        _coverCache[dir] = null;
    }
}

function _cancelCoverLoads() {
    _coverLoadToken++;
    _coverQueue = [];
    _coverQueuedKeys.clear();
    if (_coverObserver) {
        _coverObserver.disconnect();
        _coverObserver = null;
    }
}

function _coverQueueKey(token, index, dir) {
    return token + "|" + index + "|" + dir;
}

function _isCardNearViewport(card) {
    var rect = card.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight || 720;
    return rect.bottom >= -COVER_PREFETCH_MARGIN && rect.top <= vh + COVER_PREFETCH_MARGIN;
}

function _enqueueCover(games, index, token) {
    if (token !== _coverLoadToken || games !== _carouselGames) return;
    var g = games[index];
    var dir = g && (g.game_dir || "");
    if (!dir) return;

    var cached = _coverCache[dir];
    if (cached === undefined) {
        cached = _readStoredCover(dir);
        if (cached !== undefined) _coverCache[dir] = cached || null;
    }
    if (cached !== undefined) {
        _applyCoverToCard(games, index, cached);
        return;
    }

    var key = _coverQueueKey(token, index, dir);
    if (_coverQueuedKeys.has(key)) return;
    _coverQueuedKeys.add(key);
    _coverQueue.push({ index: index, dir: dir, key: key });
    _startCoverWorkers(games, token);
}

function _startCoverWorkers(games, token) {
    while (
        _coverWorkersActive < COVER_CONCURRENCY
        && _coverQueue.length
        && token === _coverLoadToken
        && games === _carouselGames
    ) {
        _coverWorker(games, token);
    }
}

async function _coverWorker(games, token) {
    _coverWorkersActive++;
    try {
        while (_coverQueue.length && token === _coverLoadToken && games === _carouselGames) {
            var item = _coverQueue.shift();
            if (item && item.key) _coverQueuedKeys.delete(item.key);
            await _loadCoverItem(games, item, token);
            await new Promise(function(resolve) { setTimeout(resolve, 16); });
        }
    } finally {
        _coverWorkersActive = Math.max(0, _coverWorkersActive - 1);
        if (_coverQueue.length && token === _coverLoadToken && games === _carouselGames) {
            _startCoverWorkers(games, token);
        }
    }
}

function _loadCovers(games) {
    _cancelCoverLoads();
    var token = _coverLoadToken;
    var cards = Array.from(document.querySelectorAll(".carousel-card"));
    if (!cards.length) return;

    if ("IntersectionObserver" in window) {
        _coverObserver = new IntersectionObserver(function(entries) {
            entries.forEach(function(entry) {
                if (!entry.isIntersecting) return;
                var card = entry.target;
                _coverObserver.unobserve(card);
                _enqueueCover(games, parseInt(card.dataset.idx || "-1", 10), token);
            });
        }, { root: null, rootMargin: COVER_PREFETCH_MARGIN + "px 0px", threshold: 0.01 });
    }

    cards.forEach(function(card, i) {
        var idx = parseInt(card.dataset.idx || String(i), 10);
        if (_isCardNearViewport(card) || !_coverObserver) {
            _enqueueCover(games, idx, token);
        } else {
            _coverObserver.observe(card);
        }
    });
}

// ── 已翻译的游戏列表（平铺网格） ──

let _allCarouselGames = [];
let _carouselGames = [];
let _gameLibraryLoadedAt = 0;
let _gameLibraryLoadSeq = 0;
let _lastRenderedGameKeys = "";
const GAME_LIBRARY_STALE_MS = 15000;

function _centerMessageHtml(text, colorVar) {
    return "<p style='color: " + (colorVar || "var(--text-dim)") + "; text-align: center; padding: 80px 0;'>" + _escapeHtml(text) + "</p>";
}

function _loadingHtml() {
    return _centerMessageHtml(t("加载中..."));
}

function _errorHtml(title, detail) {
    return "<p style='color: var(--error); text-align: center; padding: 80px 0;'>" +
        _escapeHtml(title) + "<br><span style='font-size:12px;opacity:0.7'>" +
        _escapeHtml(detail || "") + "</span></p>";
}

async function openTranslatedGames() {
    closeStatistics();
    document.getElementById("main-page").classList.remove("active");
    var page = document.getElementById("games-page");
    page.classList.add("active");
    _setNavActive("games");
    const track = document.getElementById("carousel-track");
    const seq = ++_gameLibraryLoadSeq;
    const hasCachedGames = _allCarouselGames && _allCarouselGames.length > 0;
    if (hasCachedGames) {
        _syncGameFilterOptions(_allCarouselGames);
        applyGameLibraryFilters(false);
        if (Date.now() - _gameLibraryLoadedAt < GAME_LIBRARY_STALE_MS) return;
    } else {
        track.dataset.messageKey = "loading";
        track.innerHTML = _loadingHtml();
    }
    try {
        const games = await pywebview.api.list_translated_games();
        if (seq !== _gameLibraryLoadSeq || !_isGamesPageActive()) return;
        if (!games || games.length === 0) {
            _allCarouselGames = [];
            _carouselGames = [];
            _gameLibraryLoadedAt = Date.now();
            _lastRenderedGameKeys = "";
            _syncGameFilterOptions([]);
            if (track) {
                track.dataset.messageKey = "empty-library";
                track.innerHTML = _centerMessageHtml(t("暂无已翻译的游戏。"));
            }
            var countEl = document.getElementById("games-count");
            if (countEl) countEl.textContent = "0 / 0";
            return;
        }
        _allCarouselGames = games;
        _gameLibraryLoadedAt = Date.now();
        _syncGameFilterOptions(games);
        applyGameLibraryFilters(!hasCachedGames);
    } catch (e) {
        if (seq !== _gameLibraryLoadSeq || !_isGamesPageActive()) return;
        const errMsg = (e && e.message) ? e.message : String(e || t("未知错误"));
        const track = document.getElementById("carousel-track");
        track.dataset.messageKey = "error";
        track.innerHTML = _errorHtml(t("加载失败"), errMsg);
        pushLog("error", t("加载已翻译游戏列表失败: ") + errMsg);
    }
}

function closeTranslatedGames(returnToMain = true) {
    _cancelCoverLoads();
    _gameLibraryLoadSeq++;
    if (document.fullscreenElement) document.exitFullscreen();
    var page = document.getElementById("games-page");
    page.classList.remove("active");
    _clearGridCardInteractions();
    _clearBackdrop();
    if (returnToMain) {
        document.getElementById("main-page").classList.add("active");
        _setNavActive("main");
        _activePage = "main";
    }
}

function _escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function(ch) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch];
    });
}

function _gameKey(g) {
    return (g && (g.game_id || g.game_dir)) || "";
}

function _syncGameFilterOptions(games) {
    var select = document.getElementById("game-engine-filter");
    if (!select) return;
    var current = select.value || "";
    var engines = Array.from(new Set(games.map(function(g) { return _engineDisplayName(g.engine || t("未知")); }))).sort(function(a, b) {
        return a.localeCompare(b, uiLocale());
    });
    select.innerHTML = '<option value="">' + _escapeHtml(t("全部引擎")) + '</option>' + engines.map(function(engine) {
        return '<option value="' + _escapeHtml(engine) + '">' + _escapeHtml(engine) + '</option>';
    }).join("");
    select.value = engines.includes(current) ? current : "";
}

function _getFilteredGames() {
    var queryEl = document.getElementById("game-search");
    var engineEl = document.getElementById("game-engine-filter");
    var sortEl = document.getElementById("game-sort");
    var query = queryEl ? queryEl.value.trim().toLowerCase() : "";
    var engine = engineEl ? engineEl.value : "";
    var sort = sortEl ? sortEl.value : "updated_desc";

    var filtered = _allCarouselGames.filter(function(g) {
        var dir = g.game_dir || g.game_id || "";
        var name = _gameName(dir);
        var eng = _engineDisplayName(g.engine || t("未知"));
        var haystack = (name + " " + dir + " " + eng).toLowerCase();
        return (!query || haystack.includes(query)) && (!engine || eng === engine);
    });

    filtered.sort(function(a, b) {
        if (sort === "name_asc") return _gameName(a.game_dir || a.game_id).localeCompare(_gameName(b.game_dir || b.game_id), uiLocale());
        if (sort === "engine_asc") return _engineDisplayName(a.engine || t("未知")).localeCompare(_engineDisplayName(b.engine || t("未知")), uiLocale());
        if (sort === "created_desc") return String(b.created_at || "").localeCompare(String(a.created_at || ""));
        return String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""));
    });
    return filtered;
}

function _renderFilteredGames(filtered, animate) {
    _carouselGames = filtered;
    var countEl = document.getElementById("games-count");
    if (countEl) countEl.textContent = filtered.length + " / " + _allCarouselGames.length;

    if (filtered.length === 0) {
        const track = document.getElementById("carousel-track");
        track.dataset.messageKey = "empty";
        track.innerHTML = _centerMessageHtml(t("没有匹配的游戏。"));
        _lastRenderedGameKeys = "";
        return;
    }

    document.getElementById("carousel-track").dataset.messageKey = "";
    var keys = getUiLanguage() + "|" + _cardScale() + "|" + filtered.map(function(g) { return _gameKey(g); }).join("|");
    if (keys !== _lastRenderedGameKeys) {
        _lastRenderedGameKeys = keys;
        _buildCarousel(filtered);
        _layoutGrid();
        if (animate) _animateLibraryCardsIn();
        _loadCovers(filtered);
    } else {
        _layoutGrid();
        _loadCovers(filtered);
    }
}

function applyGameLibraryFilters(animate) {
    var track = document.getElementById("carousel-track");
    var filtered = _getFilteredGames();
    var shouldAnimate = animate !== undefined ? !!animate : !!(track && track.querySelector(".carousel-card"));
    _renderFilteredGames(filtered, shouldAnimate);
}

function _animateLibraryCardsIn() {
    requestAnimationFrame(function() {
        var cards = Array.from(document.querySelectorAll(".carousel-card"));
        if (!cards.length) return;
        var limit = 42;
        cards.slice(0, limit).forEach(function(card, i) {
            var delay = Math.min(i * 18, 220);
            card.style.setProperty("--library-enter-delay", delay + "ms");
            card.classList.add("library-enter");
            card.addEventListener("animationend", function cleanup() {
                card.classList.remove("library-enter");
                card.style.removeProperty("--library-enter-delay");
                card.removeEventListener("animationend", cleanup);
            });
        });
    });
}

function _buildCarousel(games) {
    const track = document.getElementById("carousel-track");
    const total = games.length;
    let html = "";
    for (let i = 0; i < total; i++) {
        const g = games[i];
        const dir = g.game_dir || g.game_id;
        const engine = _engineDisplayName(g.engine || t("未知"));
        const time = g.updated_at || g.created_at || "";
        const cachedCover = _cachedCoverForDir(g.game_dir || "") || g.cover || "";
        html += "<div class='carousel-card' data-idx='" + i + "' onclick='cardOpenGameDir(event," + i + ")' onmouseenter='cardHoverIn(event," + i + ")' onmouseleave='cardHoverOut()'>";
        html += "<div class='carousel-card-inner" + (cachedCover ? " has-cover" : "") + "' style='background: " + _engineColor(engine) + ";" + (cachedCover ? " background-image: url(&quot;" + _escapeHtml(cachedCover) + "&quot;);" : "") + "'>";
        if (cachedCover) html += "<div class='card-cover-overlay'></div>";
        html += "<div class='card-actions'>";
        html += "<button class='card-action' onclick='selectGameFromCard(event," + i + ")' title='" + _escapeHtml(t("选择")) + "'><svg class='ic'><use href='#i-check'/></svg></button>";
        html += "<button class='card-action' onclick='revealGameFromCard(event," + i + ")' title='" + _escapeHtml(t("打开目录")) + "'><svg class='ic'><use href='#i-folder'/></svg></button>";
        html += "<button class='card-action danger' onclick='removeGameFromCard(event," + i + ")' title='" + _escapeHtml(t("移除记录")) + "'><svg class='ic'><use href='#i-trash'/></svg></button>";
        html += "</div>";
        html += "<div class='game-title'>" + _escapeHtml(_gameName(dir)) + "</div>";
        html += "<div class='game-engine'>" + _escapeHtml(engine) + "</div>";
        if (time) html += "<div class='game-date'>" + _escapeHtml(time) + "</div>";
        html += "</div></div>";
    }
    track.innerHTML = html;
}

function _engineColor(engine) {
    const e = engine.toLowerCase();
    if (e.includes("renpy")) return "linear-gradient(135deg, #667eea, #764ba2)";
    if (e.includes("rpgm") || e.includes("maker")) return "linear-gradient(135deg, #f093fb, #f5576c)";
    if (e.includes("krkr") || e.includes("kirikiri")) return "linear-gradient(135deg, #4facfe, #00f2fe)";
    if (e.includes("artemis") || e.includes("marmalade") || e.includes("softhouse")) return "linear-gradient(135deg, #fa709a, #fee140)";
    if (e.includes("bgi") || e.includes("buriko")) return "linear-gradient(135deg, #a18cd1, #fbc2eb)";
    if (e.includes("ethornell") || e.includes("cmvs")) return "linear-gradient(135deg, #ffecd2, #fcb69f)";
    if (e.includes("advhd")) return "linear-gradient(135deg, #ff9a9e, #fecfef)";
    if (e.includes("wuff") || e.includes("wolf")) return "linear-gradient(135deg, #84fab0, #8fd3f4)";
    if (e.includes("yuris")) return "linear-gradient(135deg, #ffd3e8, #fbc2eb)";
    if (e.includes("siglus")) return "linear-gradient(135deg, #d4fc79, #96e6a1)";
    if (e.includes("livemaker")) return "linear-gradient(135deg, #fddb92, #d1fdff)";
    if (e.includes("qlie") || e.includes("adv")) return "linear-gradient(135deg, #cfd9df, #e2ebf0)";
    return "linear-gradient(135deg, #434343, #000000)";
}

function _engineDisplayName(engine) {
    const raw = String(engine || t("未知"));
    const e = raw.toLowerCase();
    if (e === "bgi" || e.includes("bgi") || e.includes("buriko") || e.includes("ethornell")) {
        return t("BGI / Ethornell（Beta）");
    }
    if (e === "rpgmaker") return "RPG Maker";
    if (e === "renpy") return "Ren'Py";
    if (e === "godot_pck") return "Godot PCK";
    if (e === "godot_frida") return t("Godot 加密 PCK");
    if (e === "xunity_realtime") return "Unity / XUnity";
    return raw;
}

function refreshLocalizedDynamicUi() {
    const statusText = document.getElementById("status-text");
    if (statusText) {
        const rawStatus = statusText.dataset.rawStatus || statusText.textContent || "空闲";
        statusText.dataset.rawStatus = rawStatus;
        statusText.textContent = t(rawStatus);
    }
    const statusStep = document.getElementById("status-step");
    if (statusStep) {
        const rawStep = statusStep.dataset.rawStep || statusStep.textContent || "就绪";
        statusStep.dataset.rawStep = rawStep;
        statusStep.textContent = t(rawStep);
    }
    if (_engCache && pathInput && pathInput.value && _engCache[pathInput.value]) {
        _showEngine(_engCache[pathInput.value]);
    }
    if (_allCarouselGames && _allCarouselGames.length) {
        _syncGameFilterOptions(_allCarouselGames);
        if (_isGamesPageActive()) applyGameLibraryFilters();
    } else {
        const track = document.getElementById("carousel-track");
        if (track && track.dataset.messageKey) {
            if (track.dataset.messageKey === "loading") track.innerHTML = _loadingHtml();
            if (track.dataset.messageKey === "empty") track.innerHTML = _centerMessageHtml(t("没有匹配的游戏。"));
        }
    }
    if (_usageData && _isStatisticsPageActive()) renderUsageStatistics(_usageData);
}
window.refreshLocalizedDynamicUi = refreshLocalizedDynamicUi;

function _gameName(dir) {
    const name = dir.replace(/\\\\/g, "/");
    const parts = name.split("/");
    return parts[parts.length - 1] || parts[parts.length - 2] || name;
}


function cardHoverIn(event, index) {
    if (!_isGamesPageActive()) return;
    var g = _carouselGames[index];
    if (g && g.cover) _setBackdrop(g.cover);
}

function cardHoverOut() {
    if (_backdropClearTimer) clearTimeout(_backdropClearTimer);
    _backdropClearTimer = setTimeout(_clearBackdrop, 220);
}

function cardOpenGameDir(event, index) {
    if (event) event.stopPropagation();
    if (!_isGamesPageActive()) return;
    var g = _carouselGames[index];
    if (!g) return;
    pywebview.api.open_game_dir(g.game_dir || g.game_id);
    pushLog("info", t("打开游戏: ") + (g.game_dir || g.game_id));
    closeTranslatedGames();
}

function selectGameFromCard(event, index) {
    if (event) event.stopPropagation();
    if (!_isGamesPageActive()) return;
    var g = _carouselGames[index];
    if (!g) return;
    setPath(g.game_dir || g.game_id);
    pushLog("info", t("已选择游戏: ") + (g.game_dir || g.game_id));
    closeTranslatedGames();
}

async function revealGameFromCard(event, index) {
    if (event) event.stopPropagation();
    if (!_isGamesPageActive()) return;
    var g = _carouselGames[index];
    if (!g) return;
    try {
        await pywebview.api.reveal_game_dir(g.game_dir || g.game_id);
        pushLog("info", t("已打开目录: ") + (g.game_dir || g.game_id));
    } catch (e) {
        pushLog("error", t("打开目录失败: ") + ((e && e.message) ? e.message : e));
    }
}

async function removeGameFromCard(event, index) {
    if (event) event.stopPropagation();
    if (!_isGamesPageActive()) return;
    var g = _carouselGames[index];
    if (!g) return;
    var name = _gameName(g.game_dir || g.game_id);
    if (!confirm(t("从游戏库移除「") + name + t("」？\n\n不会卸载汉化，也不会删除游戏文件。"))) return;
    try {
        var ok = await pywebview.api.remove_game_record(g.game_id);
        if (!ok) {
            pushLog("warn", t("没有找到可移除的游戏库记录"));
            return;
        }
        var key = _gameKey(g);
        _allCarouselGames = _allCarouselGames.filter(function(item) { return _gameKey(item) !== key; });
        _syncGameFilterOptions(_allCarouselGames);
        applyGameLibraryFilters();
    } catch (e) {
        pushLog("error", t("移除记录失败: ") + ((e && e.message) ? e.message : e));
    }
}



function _setGridCardTransform(card, extraTransform) {
    var base = card.dataset.gridTransform || "";
    card.style.transform = base + (extraTransform ? " " + extraTransform : "");
}

function _resetGridCardTilt(card) {
    if (card._gridTiltFrame) {
        cancelAnimationFrame(card._gridTiltFrame);
        card._gridTiltFrame = 0;
    }
    delete card._gridTiltX;
    delete card._gridTiltY;
    card.classList.remove("is-tilting");
    _setGridCardTransform(card, "rotateY(0deg) translateZ(0px) scale(1)");
}

function _queueGridCardTilt(card, rotateX, rotateY) {
    card._gridTiltX = rotateX;
    card._gridTiltY = rotateY;
    if (card._gridTiltFrame) return;
    card._gridTiltFrame = requestAnimationFrame(function() {
        card._gridTiltFrame = 0;
        if (!card.isConnected) return;
        card.classList.add("is-tilting");
        _setGridCardTransform(
            card,
            "rotateX(" + card._gridTiltX.toFixed(2) + "deg) rotateY(" + card._gridTiltY.toFixed(2) + "deg) translateZ(14px) scale(1.025)"
        );
    });
}

function _handleGridCardPointerMove(event) {
    var card = event.currentTarget;
    var rect = card.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    var px = (event.clientX - rect.left) / rect.width;
    var py = (event.clientY - rect.top) / rect.height;
    var nx = Math.max(-1, Math.min(1, px * 2 - 1));
    var ny = Math.max(-1, Math.min(1, py * 2 - 1));
    _queueGridCardTilt(card, -ny * 7, nx * 7);
}

function _handleGridCardPointerLeave(event) {
    _resetGridCardTilt(event.currentTarget);
}

function _clearGridCardInteractions() {
    document.querySelectorAll(".carousel-card").forEach(function(card) {
        if (card._gridTiltFrame) {
            cancelAnimationFrame(card._gridTiltFrame);
            card._gridTiltFrame = 0;
        }
        delete card._gridTiltX;
        delete card._gridTiltY;
        card.classList.remove("is-tilting");
        card.onpointermove = null;
        card.onpointerleave = null;
        delete card.dataset.gridTransform;
    });
}

function _cardScale() {
    var slider = document.getElementById("card-scale");
    return (slider ? parseFloat(slider.value) : 100) / 100;
}

function onCardScaleChange() {
    if (_isGamesPageActive() && _carouselGames.length) _layoutGrid();
}

function _carouselStageBaseHeight() {
    var vh = window.innerHeight || 720;
    if (document.fullscreenElement) return Math.min(760, Math.max(380, vh * 0.50));
    return Math.min(800, Math.max(400, vh * 0.54));
}

function _layoutGrid() {
    var cards = document.querySelectorAll(".carousel-card");
    var stage = document.getElementById("carousel-stage");
    var track = document.getElementById("carousel-track");
    if (!stage || !track) return;
    var stageW = Math.max(1, stage.clientWidth || stage.offsetWidth || 900);
    var gap = stageW < 640 ? 9 : 11;
    var rowGap = stageW < 640 ? 12 : 16;
    var topPad = 8;
    var bottomPad = 26;
    var scale = _cardScale();
    var minCardW = (stageW < 520 ? 114 : 134) * scale;
    var maxCardW = (stageW >= 1280 ? 160 : 146) * scale;
    var cols = Math.max(1, Math.floor((stageW + gap) / (minCardW + gap)));
    var cardW = Math.floor((stageW - gap * (cols - 1)) / cols);
    while (cols > 1 && cardW < minCardW) {
        cols -= 1;
        cardW = Math.floor((stageW - gap * (cols - 1)) / cols);
    }
    cardW = Math.max(Math.min(minCardW, stageW), Math.min(maxCardW, cardW));
    var cardH = Math.round(cardW * 1.4);
    var rows = Math.ceil(cards.length / cols);
    var contentW = cols * cardW + Math.max(0, cols - 1) * gap;
    var startX = Math.max(0, (stageW - contentW) / 2);
    var baseStageH = _carouselStageBaseHeight();
    var contentH = topPad + rows * cardH + Math.max(0, rows - 1) * rowGap + bottomPad;
    var stageH = Math.max(baseStageH, contentH);
    var planeZ = 0;
    stage.style.height = stageH + "px";
    track.style.height = stageH + "px";
    cards.forEach(function(card, i) {
        var col = i % cols;
        var row = Math.floor(i / cols);
        var x = startX + col * (cardW + gap);
        var y = topPad + row * (cardH + rowGap);
        var baseTransform = "translate3d(" + x + "px, " + y + "px, " + planeZ + "px)";
        card.style.transition = "";
        card.style.left = "0";
        card.style.top = "0";
        card.dataset.gridTransform = baseTransform;
        card.dataset.carouselGhost = "0";
        card.dataset.carouselSide = "0";
        card.classList.remove("is-tilting");
        card.style.transform = baseTransform + " rotateY(0deg) scale(1)";
        card.style.opacity = "1";
        card.style.visibility = "visible";
        card.removeAttribute("aria-hidden");
        card.style.zIndex = "1";
        card.style.pointerEvents = "auto";
        card.style.width = cardW + "px";
        card.style.height = cardH + "px";
        card.style.margin = "";
        card.style.marginLeft = "0";
        card.style.marginTop = "0";
        card.style.marginRight = "";
        card.style.marginBottom = "";
        card.onpointermove = _handleGridCardPointerMove;
        card.onpointerleave = _handleGridCardPointerLeave;
    });
}

function toggleFullscreen() {
    if (!_isGamesPageActive()) return;
    if (document.fullscreenElement) {
        document.exitFullscreen();
    } else {
        document.documentElement.requestFullscreen();
    }
}

function _isGamesPageActive() {
    var page = document.getElementById("games-page");
    return !!(page && page.classList.contains("active"));
}

function _isTypingTarget(target) {
    if (!target) return false;
    var tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
}

function _handleGameLibraryKeydown(event) {
    if (!_isGamesPageActive() || _isTypingTarget(event.target)) return;
    if (event.key === "Escape") {
        event.preventDefault();
        closeTranslatedGames();
    }
}

document.addEventListener("keydown", _handleGameLibraryKeydown);
document.addEventListener("keydown", function(event) {
    if (event.key !== "Escape") return;
    const help = document.getElementById("help-overlay");
    if (help && help.classList.contains("open")) closeHelp();
});
document.addEventListener("click", function(event) {
    const help = document.getElementById("help-overlay");
    if (help && event.target === help) closeHelp();
});

document.addEventListener("fullscreenchange", function () {
    if (!_carouselGames.length) return;
    if (!document.fullscreenElement) {
        setTimeout(function () {
            if (_isGamesPageActive() && _carouselGames.length) _layoutGrid();
        }, 550);
    }
});

window.addEventListener("resize", function () {
    if (!_isGamesPageActive() || !_carouselGames.length) return;
    _layoutGrid();
});

// ── 启动 ──

async function loadAutoLaunch() {
    try {
        const c = await pywebview.api.get_config();
        const cb = document.getElementById("auto-launch");
        if (cb && c.auto_launch !== undefined) cb.checked = !!c.auto_launch;
    } catch (e) { /* API 未就绪时静默忽略 */ }
}

document.addEventListener("DOMContentLoaded", () => {
    applyLanguage(getUiLanguage());
    pushLog("info", t("工具已就绪"));
    updateCoverage(document.getElementById("coverage"));
    installEditableContextMenu();
    bindOverlaySettingsRealtime();
    hideStats();
    const alCb = document.getElementById("auto-launch");
    if (alCb) {
        alCb.addEventListener("change", async () => {
            await pywebview.api.save_config({ auto_launch: alCb.checked });
        });
    }
});

// pywebview API 就绪后再读配置
window.addEventListener("pywebviewready", function () {
    loadAutoLaunch();
    refreshAppUpdateInfo();
    setInterval(refreshAppUpdateInfo, 4 * 60 * 60 * 1000);
    if (pywebview.api.start_background_tool_update_check) {
        pywebview.api.start_background_tool_update_check();
    }
    pywebview.api.get_config().then(function (c) {
        if (c && c.ui_language) setUiLanguage(c.ui_language, true);
        applyTheme((c && c.ui_theme) || currentTheme(), { initial: true });
        if (c && c.ui_accent && !isOtherThemeDefault(THEME_ACCENT_FACTORY, c.ui_accent)) applyAccent(c.ui_accent);
        if (c && c.bg_color && !isOtherThemeDefault(THEME_BG_FACTORY, c.bg_color)) setBgColor(c.bg_color);
        if (c && c.bg_image) { _bgImagePath = c.bg_image; applyBgImage(c.bg_image); }
    });
    if (_isStatisticsPageActive()) loadUsageStatistics(true);
});

// 首屏先把主题选中态摆正，不必等 pywebview 就绪
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncThemeUi);
} else {
    syncThemeUi();
}
