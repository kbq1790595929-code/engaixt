function onTranslatorChanged(value) {
    const localSelected = value === "hy_mt2";
    const cloudSettings = document.getElementById("cloud-translation-settings");
    if (cloudSettings) {
        cloudSettings.hidden = localSelected;
        cloudSettings.querySelectorAll("input, select, button").forEach(control => {
            control.disabled = localSelected;
        });
    }
    if (localSelected) refreshHyMt2Status();
}

function _formatComponentBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "";
    return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

async function refreshHyMt2Status() {
    const statusEl = document.getElementById("hy-mt2-status");
    if (!statusEl || !window.pywebview || !pywebview.api) return;
    try {
        onHyMt2Status(await pywebview.api.get_hy_mt2_status());
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}

function onHyMt2Status(status) {
    const statusEl = document.getElementById("hy-mt2-status");
    const badge = document.getElementById("hy-mt2-status-badge");
    const size = document.getElementById("hy-mt2-size");
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    if (!statusEl || !badge) return;
    const hardware = status && status.hardware || {};
    const runner = status && (status.runner || status.expected_runner) || "";
    let message = t("未安装（首次下载约 1.2-1.8 GB）");
    if (status && status.ready) {
        message = `${t("已就绪")}：${hardware.name || t("未知设备")} / ${runner}`;
    } else if (status && status.model_ready && status.runtime_ready && !status.hardware_matches) {
        message = `${t("检测到设备变化，需要重新安装运行器")}：${status.expected_runner || ""}`;
    } else if (status && status.model_ready) {
        message = t("模型已下载，运行器缺失");
    }
    if (status && status.running && status.active_backend) {
        message += ` · ${t("正在运行")}：${status.active_backend}`;
    }
    statusEl.textContent = message;
    badge.textContent = status && status.ready ? t("已安装") : t("未安装");
    badge.classList.toggle("ready", !!(status && status.ready));
    if (size) size.textContent = _formatComponentBytes(status && status.installed_bytes);
    if (install) {
        install.disabled = false;
        install.innerHTML = `<svg class="ic"><use href="#i-download"/></svg>${status && status.ready ? t("检查/修复组件") : t("下载/修复离线组件")}`;
    }
    if (remove) {
        remove.hidden = !(status && (status.model_ready || status.runtime_ready));
        remove.disabled = false;
    }
}

function onHyMt2InstallProgress(event) {
    const statusEl = document.getElementById("hy-mt2-status");
    if (!statusEl) return;
    const current = Number(event && event.current_bytes || 0) / 1024 / 1024;
    const total = Number(event && event.total_bytes || 0) / 1024 / 1024;
    statusEl.textContent = `${event && event.message || t("下载离线组件")} · ${current.toFixed(0)} / ${total.toFixed(0)} MB`;
}

function onHyMt2InstallFailed(message) {
    const statusEl = document.getElementById("hy-mt2-status");
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    if (statusEl) statusEl.textContent = `${t("离线组件操作失败")}：${message || t("未知错误")}`;
    if (install) install.disabled = false;
    if (remove) remove.disabled = false;
}

async function installHyMt2() {
    const install = document.getElementById("hy-mt2-install-btn");
    const remove = document.getElementById("hy-mt2-remove-btn");
    if (install) install.disabled = true;
    if (remove) remove.disabled = true;
    try {
        await pywebview.api.install_hy_mt2_component();
    } catch (e) {
        onHyMt2InstallFailed(e && e.message ? e.message : String(e));
    }
}

async function removeHyMt2() {
    if (!confirm(t("确定删除 Hy-MT2 离线模型和运行器吗？"))) return;
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
