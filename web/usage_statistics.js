"use strict";

let _usageRange = "latest";
let _usageData = null;
let _usageRequestSeq = 0;
let _usageRefreshTimer = null;
let _usageLoading = false;

function _usageLocale() {
    return typeof uiLocale === "function" ? uiLocale() : "zh-CN";
}

function _usageText(value) {
    return typeof t === "function" ? t(String(value || "")) : String(value || "");
}

function _usageEscape(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function(ch) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch];
    });
}

function _usageCount(value) {
    const number = Number(value || 0);
    return (Number.isFinite(number) ? Math.round(number) : 0).toLocaleString(_usageLocale());
}

function _usageCompactCount(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "0";
    if (Math.abs(number) >= 1000000000) return (number / 1000000000).toFixed(2) + "B";
    if (Math.abs(number) >= 1000000) return (number / 1000000).toFixed(2) + "M";
    if (Math.abs(number) >= 1000) return (number / 1000).toFixed(1) + "K";
    return _usageCount(number);
}

function _usageCost(value) {
    const number = Number(value || 0);
    return "¥" + (Number.isFinite(number) ? number.toFixed(2) : "0.00");
}

function _usageAnimateNumber(elementId, value, formatter) {
    const element = document.getElementById(elementId);
    if (!element) return;
    const target = Number(value || 0);
    if (!Number.isFinite(target) || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        element.textContent = formatter(target || 0);
        return;
    }
    const sequence = String(Number(element.dataset.animationSequence || 0) + 1);
    element.dataset.animationSequence = sequence;
    const startAt = performance.now();
    const duration = 460;
    function frame(now) {
        if (element.dataset.animationSequence !== sequence) return;
        const progress = Math.min(1, (now - startAt) / duration);
        const eased = 1 - Math.pow(1 - progress, 3);
        element.textContent = formatter(target * eased);
        if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
}

function openStatistics() {
    const main = document.getElementById("main-page");
    const games = document.getElementById("games-page");
    const page = document.getElementById("statistics-page");
    if (main) main.classList.remove("active");
    if (games) games.classList.remove("active");
    if (page) page.classList.add("active");
    if (typeof _setNavActive === "function") _setNavActive("statistics");
    _usageSyncRangeButtons();
    loadUsageStatistics();
    if (!_usageRefreshTimer) {
        _usageRefreshTimer = setInterval(function() {
            if (_isStatisticsPageActive()) loadUsageStatistics(true);
        }, 60000);
    }
}

function closeStatistics() {
    _usageRequestSeq += 1;
    _usageLoading = false;
    const page = document.getElementById("statistics-page");
    if (page) page.classList.remove("active");
    if (_usageRefreshTimer) {
        clearInterval(_usageRefreshTimer);
        _usageRefreshTimer = null;
    }
}

function _isStatisticsPageActive() {
    const page = document.getElementById("statistics-page");
    return Boolean(page && page.classList.contains("active"));
}

function _usageSyncRangeButtons() {
    document.querySelectorAll(".range-btn").forEach(function(button) {
        const active = button.dataset.range === _usageRange;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
    });
}

function selectUsageRange(rangeKey) {
    _usageRange = ["latest", "7d", "30d", "90d", "all"].includes(rangeKey) ? rangeKey : "latest";
    _usageSyncRangeButtons();
    loadUsageStatistics(true);
}

async function loadUsageStatistics(force) {
    if (!_isStatisticsPageActive()) return;
    if (!window.pywebview || !pywebview.api || !pywebview.api.get_usage_statistics) return;
    if (_usageLoading) return;
    _usageLoading = true;
    const sequence = ++_usageRequestSeq;
    const loading = document.getElementById("statistics-loading");
    if (loading) loading.hidden = false;
    try {
        const data = await pywebview.api.get_usage_statistics(_usageRange);
        if (sequence !== _usageRequestSeq) return;
        _usageData = data || null;
        renderUsageStatistics(_usageData);
    } catch (error) {
        if (sequence !== _usageRequestSeq) return;
        const message = error && error.message ? error.message : String(error || "");
        if (typeof pushLog === "function") pushLog("error", _usageText("统计读取失败") + ": " + message);
    } finally {
        if (sequence === _usageRequestSeq && loading) loading.hidden = true;
        _usageLoading = false;
    }
}

function renderUsageStatistics(data) {
    const payload = data || {};
    const summary = payload.summary || {};
    const hasData = Number(summary.run_count || 0) > 0;
    const content = document.getElementById("statistics-content");
    const empty = document.getElementById("statistics-empty");
    if (content) content.hidden = !hasData;
    if (empty) empty.hidden = hasData;

    const updated = document.getElementById("statistics-updated");
    if (updated) {
        updated.textContent = payload.to
            ? _usageText("更新于") + " " + String(payload.to).replace("T", " ")
            : _usageText("等待读取");
    }
    if (!hasData) return;

    _usageAnimateNumber("usage-success-runs", summary.run_count, _usageCount);
    _usageAnimateNumber("usage-total-tokens", summary.total_tokens, _usageCompactCount);
    _usageAnimateNumber("usage-display-cost", summary.display_cost_cny, _usageCost);
    _usageAnimateNumber("usage-cache-rate", Number(summary.cache_hit_rate || 0) * 100, function(value) {
        return value.toFixed(1) + "%";
    });

    _usageSetText("usage-run-detail", _usageCount(summary.translated_texts) + " / " + _usageCount(summary.total_texts) + " 条文本");
    _usageSetText("usage-token-detail", "输入 " + _usageCompactCount(summary.input_tokens) + " · 输出 " + _usageCompactCount(summary.output_tokens));
    _usageSetText("usage-cost-detail", _usageCostKindLabel(summary.display_cost_kind));
    _usageSetText("usage-cache-detail", "命中 " + _usageCount(summary.cache_hit_count) + " · 未命中 " + _usageCount(summary.cache_miss_count));
    _usageAnimateNumber("usage-api-requests", summary.api_request_count, _usageCount);
    _usageAnimateNumber("usage-prompt-cache", summary.prompt_cache_hit_tokens, _usageCompactCount);
    _usageAnimateNumber("usage-saved-cost", summary.estimated_saved_cost_cny, _usageCost);

    renderUsageTrend(payload.trend || []);
    renderUsageProviders(payload.providers || []);
    renderUsageEngines(payload.engines || []);
    renderUsageRuns(payload.recent_runs || []);
}

function _usageSetText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
}

function _usageCostKindLabel(kind) {
    if (kind === "local") return "本地模型 · 不计 API 费用";
    if (kind === "actual") return "服务商实际用量";
    if (kind === "estimated") return "按模型价格估算";
    if (kind === "mixed") return "实际、估算与本地记录合计";
    return "暂无费用";
}

function _usageTitleSourceLabel(source) {
    const labels = {
        steam_manifest: "Steam 作品信息",
        rpgmaker_json: "游戏标题数据",
        rpgmaker_ini: "游戏配置标题",
        godot: "Godot 项目标题",
        renpy: "Ren'Py 项目标题",
        package_json: "应用标题数据",
        exe_version_info: "EXE 产品信息",
        directory_fallback: "目录名兜底",
        pipeline: "管线标题",
    };
    return labels[String(source || "")] || "作品标题";
}

function renderUsageTrend(items) {
    const target = document.getElementById("usage-trend");
    if (!target) return;
    if (!items.length) {
        target.innerHTML = '<div class="usage-no-data">选定范围内没有成功记录</div>';
        return;
    }
    const maxTokens = Math.max.apply(null, items.map(item => Number(item.tokens || 0)).concat([1]));
    const maxCost = Math.max.apply(null, items.map(item => Number(item.cost_cny || 0)).concat([0.000001]));
    target.innerHTML = items.map(function(item, index) {
        const tokenHeight = Math.max(5, Number(item.tokens || 0) / maxTokens * 100);
        const rawCost = Number(item.cost_cny || 0);
        const costHeight = rawCost > 0 ? Math.max(4, rawCost / maxCost * 100) : 0;
        const title = String(item.date || "") + " · " + _usageCount(item.tokens) + " Token · " + _usageCost(rawCost);
        return '<div class="usage-trend-column" title="' + _usageEscape(title) + '">' +
            '<div class="usage-bar-pair"><i class="usage-bar-token" style="--bar-height:' + tokenHeight + '%;--bar-index:' + index + '"></i>' +
            '<i class="usage-bar-cost" style="--bar-height:' + costHeight + '%;--bar-index:' + index + '"></i></div>' +
            '<span>' + _usageEscape(String(item.date || "").slice(5)) + '</span></div>';
    }).join("");
}

function renderUsageProviders(items) {
    const target = document.getElementById("usage-providers");
    if (!target) return;
    if (!items.length) {
        target.innerHTML = '<div class="usage-no-data">暂无模型记录</div>';
        return;
    }
    const maxTokens = Math.max.apply(null, items.map(item => Number(item.tokens || 0)).concat([1]));
    target.innerHTML = items.slice(0, 6).map(function(item, index) {
        const label = String(item.provider || "未知") + " · " + String(item.model || "未知");
        const width = Math.max(3, Number(item.tokens || 0) / maxTokens * 100);
        return '<div class="usage-rank-row"><div class="usage-rank-line"><span title="' + _usageEscape(label) + '">' +
            _usageEscape(label) + '</span><strong>' + _usageCompactCount(item.tokens) + '</strong></div>' +
            '<div class="usage-rank-track"><i style="--bar-width:' + width + '%;--bar-index:' + index + '"></i></div>' +
            '<small>' + _usageCount(item.runs) + ' 个成功作品 · ' + _usageCost(item.cost_cny) + '</small></div>';
    }).join("");
}

function renderUsageEngines(items) {
    const target = document.getElementById("usage-engines");
    if (!target) return;
    if (!items.length) {
        target.innerHTML = '<div class="usage-no-data">暂无引擎记录</div>';
        return;
    }
    const maxRuns = Math.max.apply(null, items.map(item => Number(item.runs || 0)).concat([1]));
    target.innerHTML = items.slice(0, 6).map(function(item, index) {
        const width = Math.max(4, Number(item.runs || 0) / maxRuns * 100);
        const engine = _usageText(item.engine || "未知");
        return '<div class="usage-rank-row engine"><div class="usage-rank-line"><span>' + _usageEscape(engine) +
            '</span><strong>' + _usageCount(item.runs) + '</strong></div>' +
            '<div class="usage-rank-track"><i style="--bar-width:' + width + '%;--bar-index:' + index + '"></i></div>' +
            '<small>' + _usageCount(item.translated) + ' / ' + _usageCount(item.texts) + ' 条文本</small></div>';
    }).join("");
}

function renderUsageRuns(items) {
    const target = document.getElementById("usage-runs");
    if (!target) return;
    if (!items.length) {
        target.innerHTML = '<tr><td colspan="6" class="usage-no-data">暂无成功作品</td></tr>';
        return;
    }
    target.innerHTML = items.map(function(item) {
        const title = String(item.game_title || "未命名作品");
        const provider = String(item.provider || "未知") + " · " + String(item.model || "未知");
        const completed = String(item.completed_at || "").replace("T", " ");
        const costLabel = item.cost_kind === "local" ? "本地" : item.cost_kind === "actual" ? "实际" : "估算";
        return '<tr><td class="run-game" title="' + _usageEscape(title) + '"><strong>' + _usageEscape(title) +
            '</strong><small>' + _usageEscape(_usageTitleSourceLabel(item.title_source)) + '</small></td>' +
            '<td><span class="engine-badge">' + _usageEscape(_usageText(item.engine || "未知")) + '</span></td>' +
            '<td class="run-model" title="' + _usageEscape(provider) + '">' + _usageEscape(provider) + '</td>' +
            '<td class="run-number">' + _usageCount(item.tokens) + '<small>' + (item.token_kind === "actual" ? "实际" : "估算") + '</small></td>' +
            '<td class="run-number">' + _usageCost(item.display_cost_cny) + '<small>' + costLabel + '</small></td>' +
            '<td class="run-time">' + _usageEscape(completed) + '</td></tr>';
    }).join("");
}
