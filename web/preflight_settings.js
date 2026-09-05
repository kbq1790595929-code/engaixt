// Selection-time extraction is opt-in. Keep this policy separate from the
// general GUI script so preflight-specific events cannot affect run completion.
(() => {
    const runtimeConfig = window._engaixtRuntimeConfig = {
        selection_preflight_enabled: false,
        ...(window._engaixtRuntimeConfig || {}),
    };

    function applyPreflightRuntimeConfig(config) {
        runtimeConfig.selection_preflight_enabled = Boolean(config?.selection_preflight_enabled);
    }

    function normalizeGamePath(value) {
        return String(value || "")
            .replaceAll("/", "\\")
            .replace(/[\\/]+$/, "")
            .toLocaleLowerCase();
    }

    function matchesSelectedGame(statsPath) {
        const selectedPath = document.getElementById("path-input")?.value || "";
        if (!selectedPath) return false;
        return !statsPath || normalizeGamePath(statsPath) === normalizeGamePath(selectedPath);
    }

    function renderExtractionStats(stats) {
        window._engaixtExtractionStats = {
            text_count: Number(stats.text_count || 0),
            source_chars: Number(stats.source_chars || 0),
            file_count: Number(stats.file_count || 0),
            path: String(stats.path || ""),
        };
        const count = document.getElementById("stat-text-count");
        const files = document.getElementById("stat-file-count");
        const fileStat = document.getElementById("stat-files");
        if (count) count.textContent = window._engaixtExtractionStats.text_count.toLocaleString();
        if (files) files.textContent = window._engaixtExtractionStats.file_count.toLocaleString();
        if (fileStat) fileStat.hidden = false;
        if (typeof refreshMainCostEstimate === "function") {
            refreshMainCostEstimate(
                document.getElementById("path-input")?.value || "",
                window._engaixtExtractionStats.text_count,
                window._engaixtExtractionStats.source_chars,
            );
        }
        showStats();
    }

    const baseOnMeta = on_meta;
    on_meta = function(key, value) {
        if (key === "extraction_stats") {
            const stats = (value && typeof value === "object") ? value : {};
            if (!matchesSelectedGame(stats.path)) return;
            renderExtractionStats(stats);
            return;
        }

        baseOnMeta(key, value);
        if (key !== "preflight_result") return;
        const result = (value && typeof value === "object") ? value : {};
        if (result.status !== "success" || !matchesSelectedGame(result.path)) return;
        const stats = window._engaixtExtractionStats;
        if (stats && matchesSelectedGame(stats.path) && typeof refreshMainCostEstimate === "function") {
            refreshMainCostEstimate(stats.path || result.path, stats.text_count, stats.source_chars);
        }
    };

    const baseOnProgress = on_progress;
    on_progress = function(step, pct, detail) {
        const isPreflight = Boolean(detail?.preflight)
            || String(step || "").startsWith("preflight_");
        if (!isPreflight) return baseOnProgress(step, pct, detail);
        if (typeof _shouldDropRegressiveProgress === "function" && _shouldDropRegressiveProgress(step)) return;

        const percent = Number(pct) || 0;
        document.getElementById("progress-fill").style.width = percent + "%";
        document.getElementById("progress-text").textContent = Math.round(percent) + "%";
        const stepEl = document.getElementById("status-step");
        const pctEl = document.getElementById("status-pct");
        if (stepEl) {
            stepEl.dataset.rawStep = step || "";
            stepEl.textContent = t(step || "");
        }
        if (pctEl) pctEl.textContent = Math.round(percent) + "%";
    };

    const baseSchedulePreflightExtraction = schedulePreflightExtraction;
    schedulePreflightExtraction = async function(path, info) {
        if (!runtimeConfig.selection_preflight_enabled) return;
        return baseSchedulePreflightExtraction(path, info);
    };

    const baseSaveSettings = saveSettings;
    saveSettings = async function() {
        await baseSaveSettings();
        const control = document.querySelector('#settings-overlay [name="selection_preflight_enabled"]');
        applyPreflightRuntimeConfig({ selection_preflight_enabled: control?.checked });
    };

    window.addEventListener("pywebviewready", () => {
        if (!window.pywebview?.api?.get_config) return;
        pywebview.api.get_config().then(applyPreflightRuntimeConfig).catch(() => {});
    });
})();
