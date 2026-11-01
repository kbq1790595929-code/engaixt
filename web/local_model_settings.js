let hyMt2StatusRequest = null;
let hyMt2InstallActive = false;

function onTranslatorChanged(value) {
    const localSelected = value === "hy_mt2";
    if (typeof handleTranslatorCostEstimateChanged === "function") {
        handleTranslatorCostEstimateChanged(value);
    }
    // Keep cloud controls visible so users can configure them before switching
    // back to a cloud translator. Local inference simply ignores these values.
    if (localSelected) refreshHyMt2Status();
}

async function onHyMt2ModelChanged(value) {
    syncSettingValue("hy_mt2_model", value);
    if (!window.pywebview || !pywebview.api) return;
    try {
        await pywebview.api.save_config({ hy_mt2_model: value });
        await refreshHyMt2Status();
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}

function _formatComponentBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "";
    return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

async function refreshHyMt2Status() {
    const statusEl = document.getElementById("hy-mt2-status");
    if (!statusEl || !window.pywebview || !pywebview.api) return;
    if (hyMt2StatusRequest) return hyMt2StatusRequest;
    hyMt2StatusRequest = (async () => {
        try {
            onHyMt2Status(await pywebview.api.get_hy_mt2_status());
        } catch (e) {
            onHyMt2InstallFailed(e && e.message ? e.message : String(e));
        } finally {
            hyMt2StatusRequest = null;
        }
    })();
    return hyMt2StatusRequest;
}

function onHyMt2Status(status) {
    const statusEl = document.getElementById("hy-mt2-status");
    const badge = document.getElementById("hy-mt2-status-badge");
    const size = document.getElementById("hy-mt2-size");
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    const customPath = document.querySelector('#hy-mt2-component [name="hy_mt2_custom_model_path"]');
    const customClear = document.getElementById("hy-mt2-custom-clear-btn");
    const modelSelect = document.querySelector('#hy-mt2-component [name="hy_mt2_model"]');
    if (!statusEl || !badge) return;
    if (status && status.installing === false) hyMt2InstallActive = false;
    let message = status && status.message || t("未安装");
    if (status && status.deployment_verification) {
        const verified = status.deployment_verification;
        message += ` · ${t("后端验证通过")}：${verified.backend || ""}`;
    }
    if (status && status.running && status.active_backend) {
        message += ` · ${t("正在运行")}：${status.active_backend}`;
    }
    statusEl.textContent = message;
    if (customPath && status && status.custom_model) customPath.value = status.local_model_path || "";
    if (customClear) customClear.hidden = !(status && status.custom_model);
    if (modelSelect) modelSelect.disabled = !!(status && status.custom_model);
    badge.textContent = status && status.ready
        ? t("已安装")
        : status && status.local_model_found
            ? t("已发现本地模型")
            : t("未安装");
    badge.classList.toggle("ready", !!(status && status.ready));
    if (size) size.textContent = _formatComponentBytes(status && status.installed_bytes);
    if (install) {
        if (status && status.installing) hyMt2InstallActive = true;
        install.disabled = !!(status && status.custom_model && status.runtime_ready) && !hyMt2InstallActive;
        if (hyMt2InstallActive) {
            install.innerHTML = `<svg class="ic"><use href="#i-close"/></svg>${t("取消下载")}`;
            install.disabled = false;
            return;
        }
        const installLabel = status && status.custom_model
            ? (status.runtime_ready ? "已选择自定义 GGUF" : "安装本地运行器")
            : (status && status.ready ? t("检查/修复当前模型") : t("下载/修复当前模型"));
        install.innerHTML = `<svg class="ic"><use href="#i-download"/></svg>${installLabel}`;
    }
    if (remove) {
        remove.hidden = !!(status && status.custom_model) || !(status && status.model_ready);
        remove.disabled = false;
    }
}

async function selectCustomGgufModel() {
    if (!window.pywebview || !pywebview.api?.select_gguf_model) return;
    try {
        const path = await pywebview.api.select_gguf_model();
        if (!path) return;
        const status = await pywebview.api.set_hy_mt2_custom_model(path);
        onHyMt2Status(status);
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}

async function clearCustomGgufModel() {
    if (!window.pywebview || !pywebview.api?.clear_hy_mt2_custom_model) return;
    try {
        const status = await pywebview.api.clear_hy_mt2_custom_model();
        const field = document.querySelector('#hy-mt2-component [name="hy_mt2_custom_model_path"]');
        if (field) field.value = "";
        onHyMt2Status(status);
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}

function onHyMt2InstallProgress(event) {
    const statusEl = document.getElementById("hy-mt2-status");
    if (!statusEl) return;
    hyMt2InstallActive = true;
    const install = document.getElementById("hy-mt2-install-btn");
    if (install) {
        install.disabled = false;
        install.innerHTML = `<svg class="ic"><use href="#i-close"/></svg>${t("取消下载")}`;
    }
    const current = Number(event && event.current_bytes || 0) / 1024 / 1024;
    const total = Number(event && event.total_bytes || 0) / 1024 / 1024;
    statusEl.textContent = `${event && event.message || t("下载离线组件")} · ${current.toFixed(0)} / ${total.toFixed(0)} MB`;
}

function onHyMt2InstallFailed(message) {
    const statusEl = document.getElementById("hy-mt2-status");
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    hyMt2InstallActive = false;
    if (statusEl) statusEl.textContent = `${t("离线组件操作失败")}：${message || t("未知错误")}`;
    if (install) install.disabled = false;
    if (remove) remove.disabled = false;
}

function onHyMt2InstallCancelled() {
    hyMt2InstallActive = false;
    const statusEl = document.getElementById("hy-mt2-status");
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    if (statusEl) statusEl.textContent = t("下载已取消");
    if (install) {
        install.disabled = false;
        install.innerHTML = `<svg class="ic"><use href="#i-download"/></svg>${t("下载/修复当前模型")}`;
    }
    if (remove) remove.disabled = false;
}

async function installHyMt2() {
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    if (hyMt2InstallActive) {
        try {
            await pywebview.api.cancel_hy_mt2_install();
        } catch (e) {
            onHyMt2InstallFailed(e && e.message ? e.message : String(e));
        }
        return;
    }
    if (install) install.disabled = true;
    if (remove) remove.disabled = true;
    try {
        const result = await pywebview.api.install_hy_mt2_component();
        if (result && result.ok) {
            hyMt2InstallActive = true;
            if (install) {
                install.disabled = false;
                install.innerHTML = `<svg class="ic"><use href="#i-close"/></svg>${t("取消下载")}`;
            }
        } else if (result && result.already_running) {
            hyMt2InstallActive = true;
            if (install) {
                install.disabled = false;
                install.innerHTML = `<svg class="ic"><use href="#i-close"/></svg>${t("取消下载")}`;
            }
        }
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}

async function removeHyMt2() {
    if (!confirm("确定删除当前本地 GGUF 模型吗？已选择的自定义文件不会被删除。")) return;
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    if (install) install.disabled = true;
    if (remove) remove.disabled = true;
    try {
        await pywebview.api.remove_hy_mt2_component();
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}
