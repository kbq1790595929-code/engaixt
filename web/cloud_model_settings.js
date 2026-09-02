const CLOUD_MODEL_FIELDS = [
    "openai_model",
    "deepseek_model",
    "qwen_model",
    "zhipu_model",
    "moonshot_model",
    "doubao_model",
    "anthropic_model",
];

const CLOUD_SETTING_FIELDS = [
    ...CLOUD_MODEL_FIELDS,
    "openai_api_key",
    "deepseek_api_key",
    "qwen_api_key",
    "zhipu_api_key",
    "moonshot_api_key",
    "doubao_api_key",
    "anthropic_api_key",
];

const CLOUD_MODEL_BY_TRANSLATOR = {
    openai: "openai_model",
    deepseek: "deepseek_model",
    qwen: "qwen_model",
    zhipu: "zhipu_model",
    moonshot: "moonshot_model",
    doubao: "doubao_model",
    anthropic: "anthropic_model",
};

const CLOUD_DEFAULT_MODEL_LABELS = {
    openai: "ChatGPT",
    deepseek: "DeepSeek",
    qwen: "通义千问",
    zhipu: "智谱 GLM",
    moonshot: "Kimi",
    doubao: "豆包",
    anthropic: "Claude",
};
const CLOUD_DEFAULT_MODEL_VALUE = "__provider_default__";

let _cloudSettingsSaveTimer = null;
let _cloudModelRefreshInFlight = null;

function _cloudSettingValue(element) {
    return element && element.type === "checkbox" ? element.checked : element?.value ?? "";
}

function _cloudModelFieldForActiveTranslator() {
    const translator = document.querySelector('#settings-overlay [name="active_translator"]');
    return CLOUD_MODEL_BY_TRANSLATOR[String(translator?.value || "").toLowerCase()] || "";
}

function _cloudProviderForModelField(field) {
    return Object.keys(CLOUD_MODEL_BY_TRANSLATOR).find(
        provider => CLOUD_MODEL_BY_TRANSLATOR[provider] === field
    ) || "";
}

function cloudDisplayModelValue(element, value) {
    if (!element || element.tagName?.toLowerCase() !== "select") return false;
    const provider = _cloudProviderForModelField(element.getAttribute("name"));
    if (!provider) return false;
    const actual = String(value ?? "");
    element.dataset.savedModel = actual;
    if (!_activeCloudApiKey(provider)) {
        element.value = CLOUD_DEFAULT_MODEL_VALUE;
        return true;
    }
    return false;
}

function _displayCloudModel(provider, model) {
    if (!_activeCloudApiKey(provider)) return CLOUD_DEFAULT_MODEL_LABELS[provider] || provider;
    return model || CLOUD_DEFAULT_MODEL_LABELS[provider] || provider;
}

function renderActiveCloudModel() {
    const target = document.getElementById("active-cloud-model");
    if (!target) return;
    const field = _cloudModelFieldForActiveTranslator();
    const model = field ? document.querySelector(`#settings-overlay [name="${field}"]`) : null;
    const provider = _activeCloudProvider();
    const modelValue = model?.dataset.savedModel || model?.value || "";
    target.textContent = modelValue
        ? `${t("当前模型")}：${_displayCloudModel(provider, modelValue)}`
        : t("当前未选择云端模型");
    renderCloudModelPrice();
}

function _cloudProviderLabel(provider) {
    return {
        openai: "OpenAI",
        deepseek: "DeepSeek",
        qwen: "通义千问",
        zhipu: "智谱 GLM",
        moonshot: "Kimi",
        doubao: "豆包",
        anthropic: "Claude",
    }[provider] || provider;
}

function _activeCloudProvider() {
    const translator = document.querySelector('#settings-overlay [name="active_translator"]');
    const value = String(translator?.value || "").toLowerCase();
    return CLOUD_MODEL_BY_TRANSLATOR[value] ? value : "";
}

function _activeCloudModelElement(provider) {
    const field = CLOUD_MODEL_BY_TRANSLATOR[provider || _activeCloudProvider()];
    return field ? document.querySelector(`#settings-overlay [name="${field}"]`) : null;
}

function _activeCloudApiKey(provider) {
    const field = provider ? `${provider}_api_key` : "";
    return field ? String(document.querySelector(`#settings-overlay [name="${field}"]`)?.value || "").trim() : "";
}

function _formatCloudPrice(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "-";
    return number.toFixed(number < 0.01 && number > 0 ? 4 : 2).replace(/0+$/, "").replace(/\.$/, "");
}

function renderCloudModelPrice(pricing) {
    const target = document.getElementById("cloud-model-price");
    if (!target) return;
    const provider = _activeCloudProvider();
    const modelElement = _activeCloudModelElement(provider);
    const model = modelElement?.dataset.savedModel || modelElement?.value || "";
    const data = pricing || window._cloudModelPricing;
    if (!provider || !model || !data) {
        target.textContent = "";
        return;
    }
    const p = data.pricing || data;
    const source = data.pricing_source || window._cloudModelPricingSource || "local_estimate";
    const sourceLabel = source === "provider_api" || source === "online_catalog"
        ? "在线价格"
        : "本地估算";
    const output = _formatCloudPrice(p.period === "peak" ? (p.peak_output_cny_per_m ?? p.output_cny_per_m) : p.output_cny_per_m);
    if (provider === "deepseek" && p.input_cache_hit_cny_per_m !== undefined) {
        const hit = _formatCloudPrice(p.period === "peak" ? p.peak_input_cache_hit_cny_per_m : p.input_cache_hit_cny_per_m);
        const miss = _formatCloudPrice(p.period === "peak" ? p.peak_input_cache_miss_cny_per_m : p.input_cache_miss_cny_per_m);
        target.textContent = `${_cloudProviderLabel(provider)} / ${_displayCloudModel(provider, model)}：输入命中 ¥${hit}，未命中 ¥${miss}，输出 ¥${output} / 百万 tokens（${sourceLabel}）`;
    } else {
        const input = _formatCloudPrice(p.input_cny_per_m);
        target.textContent = `${_cloudProviderLabel(provider)} / ${_displayCloudModel(provider, model)}：输入 ¥${input}，输出 ¥${output} / 百万 tokens（${sourceLabel}）`;
    }
}

async function refreshCloudPrice() {
    const provider = _activeCloudProvider();
    const model = _activeCloudModelElement(provider)?.dataset.savedModel
        || _activeCloudModelElement(provider)?.value || "";
    if (!provider || !model || !window.pywebview || !pywebview.api?.get_cloud_pricing) return;
    try {
        const data = await pywebview.api.get_cloud_pricing(provider, model);
        window._cloudModelPricing = data?.pricing || null;
        window._cloudModelPricingSource = data?.pricing_source || "local_estimate";
        renderCloudModelPrice();
    } catch (_) {}
}

function renderCloudModelOptions(provider, models) {
    const select = _activeCloudModelElement(provider);
    if (!select || !Array.isArray(models) || !models.length) return;
    const current = select.dataset.savedModel || select.value;
    select.replaceChildren();
    for (const row of models) {
        const option = document.createElement("option");
        option.value = String(row.id || "");
        option.textContent = String(row.label || row.id || "");
        option.dataset.dynamicModel = "1";
        select.appendChild(option);
    }
    if (current && !_selectHasValue(select, current)) {
        const option = document.createElement("option");
        option.value = current;
        option.textContent = current + "（当前配置）";
        option.dataset.currentConfig = "1";
        select.appendChild(option);
    }
    select.value = current || select.options[0]?.value || "";
    select.dataset.savedModel = select.value;
    const selected = models.find(row => row.id === select.value);
    window._cloudModelPricing = selected?.pricing || null;
    window._cloudModelPricingSource = selected?.pricing_source || "local_estimate";
    renderCloudModelPrice();
}

async function refreshCloudModels(provider) {
    const selectedProvider = String(provider || _activeCloudProvider()).toLowerCase();
    if (!selectedProvider || !window.pywebview || !pywebview.api) return null;
    const status = document.getElementById("cloud-model-status");
    if (!_activeCloudApiKey(selectedProvider)) {
        if (status) status.textContent = `${_cloudProviderLabel(selectedProvider)}：请先填写对应 API Key，再刷新模型列表`;
        return null;
    }
    if (_cloudModelRefreshInFlight) return _cloudModelRefreshInFlight;
    if (status) status.textContent = `${_cloudProviderLabel(selectedProvider)}：正在读取模型列表…`;
    _cloudModelRefreshInFlight = pywebview.api.refresh_cloud_model_catalog(selectedProvider)
        .then(data => {
            if (data?.models?.length) renderCloudModelOptions(selectedProvider, data.models);
            if (selectedProvider === _activeCloudProvider()) {
                const selected = data?.models?.find(row => row.id === _activeCloudModelElement(selectedProvider)?.value);
                window._cloudModelPricing = selected?.pricing || data?.pricing || null;
                window._cloudModelPricingSource = selected?.pricing_source || data?.pricing_source || "local_estimate";
                renderCloudModelPrice();
            }
            if (status) {
                if (data?.models?.length) {
                    const time = data.fetched_at ? new Date(data.fetched_at).toLocaleTimeString() : "";
                    status.textContent = `${_cloudProviderLabel(selectedProvider)}：已更新 ${data.models.length} 个模型${time ? `（${time}）` : ""}`;
                } else {
                    status.textContent = data?.error || "未获取到模型列表，保留现有选项";
                }
            }
            return data;
        })
        .catch(error => {
            if (status) status.textContent = `模型列表刷新失败：${error?.message || error}`;
            return null;
        })
        .finally(() => { _cloudModelRefreshInFlight = null; });
    return _cloudModelRefreshInFlight;
}

async function saveCloudSetting(element) {
    if (!element || !window.pywebview || !pywebview.api) return;
    const name = element.getAttribute("name");
    if (!CLOUD_SETTING_FIELDS.includes(name)) return;
    try {
        await pywebview.api.save_config({ [name]: _cloudSettingValue(element) });
        if (CLOUD_MODEL_FIELDS.includes(name)) {
            renderActiveCloudModel();
            refreshCloudPrice();
            refreshMainCostEstimate();
        } else if (name.endsWith("_api_key") && _cloudSettingValue(element).trim()) {
            refreshCloudModels(name.slice(0, -7));
        }
    } catch (error) {
        console.error("Failed to save cloud setting:", error);
    }
}

function scheduleCloudSettingSave(element) {
    if (_cloudSettingsSaveTimer) clearTimeout(_cloudSettingsSaveTimer);
    _cloudSettingsSaveTimer = setTimeout(() => {
        _cloudSettingsSaveTimer = null;
        saveCloudSetting(element);
    }, 250);
}

function bindCloudSettingsRealtime() {
    document.querySelectorAll("#tab-api [name]").forEach(element => {
        if (!CLOUD_SETTING_FIELDS.includes(element.getAttribute("name"))) return;
        const eventName = element.type === "text" || element.tagName.toLowerCase() === "textarea"
            ? "input"
            : "change";
        element.addEventListener(eventName, () => scheduleCloudSettingSave(element));
        element.addEventListener("change", () => {
            if (CLOUD_MODEL_FIELDS.includes(element.getAttribute("name"))) {
                element.dataset.savedModel = element.value;
                renderActiveCloudModel();
                refreshCloudPrice();
            }
        });
    });

    const translator = document.querySelector('#settings-overlay [name="active_translator"]');
    if (translator) translator.addEventListener("change", () => {
        renderActiveCloudModel();
        refreshCloudModels();
        refreshMainCostEstimate();
    });
    const refreshButton = document.getElementById("refresh-cloud-models");
    if (refreshButton) refreshButton.addEventListener("click", () => refreshCloudModels());
    renderActiveCloudModel();
    refreshCloudPrice();
}

function refreshCloudModelState() {
    renderActiveCloudModel();
    refreshCloudModels();
    refreshMainCostEstimate();
}

document.addEventListener("DOMContentLoaded", bindCloudSettingsRealtime);

// The estimate is intentionally driven by completed extraction metadata.
// It does not scan the selected game or estimate before text is available.
function _selectedEstimateTarget() {
    const providerEl = document.querySelector('#settings-overlay [name="active_translator"]')
        || document.querySelector('[name="active_translator"]');
    const provider = String(providerEl?.value || "").trim().toLowerCase();
    const modelEl = provider
        ? document.querySelector(`#settings-overlay [name="${provider}_model"]`)
            || document.querySelector(`[name="${provider}_model"]`)
        : null;
    return {
        provider,
        model: String(modelEl?.dataset.savedModel || modelEl?.value || "").trim(),
    };
}

function renderMainCostEstimate(data) {
    const target = document.getElementById("stat-cost-text");
    const stat = document.getElementById("stat-cost-estimate");
    if (!target || !stat) return;
    if (!data?.available) {
        stat.hidden = true;
        return;
    }
    const count = Number(data.text_count || 0).toLocaleString();
    const inputTokens = Number(data.estimated_input_tokens || 0).toLocaleString();
    const model = String(data.model || "").trim();
    const isLocal = Boolean(data.is_local) || String(data.provider || "").toLowerCase() === "hy_mt2";
    if (isLocal) {
        target.textContent = `${t("离线模型")} ¥0.00 · ${count} ${t("条")} · ${t("不产生 API 费用")}`;
        stat.title = data.note || t("本地模型在本机运行，不产生云端 API 费用");
    } else {
        const cost = _formatCloudPrice(data.estimated_cost_cny);
        const zeroPrice = Number(data.estimated_cost_cny || 0) === 0
            && Number(data.pricing?.input_cny_per_m || 0) === 0
            && Number(data.pricing?.output_cny_per_m || 0) === 0;
        const priceNote = zeroPrice ? ` · ${t("免费模型")}` : "";
        target.textContent = `${t("预估")} ¥${cost}${priceNote} · ${model} · ${count} ${t("条")} · ${t("输入")} ${inputTokens} tokens`;
        stat.title = data.note || `${data.provider || ""} / ${model}`;
    }
    stat.hidden = false;
    if (typeof showStats === "function") showStats();
}

let _mainEstimateRequestSeq = 0;

function _hideMainCostEstimate() {
    const target = document.getElementById("stat-cost-text");
    const stat = document.getElementById("stat-cost-estimate");
    if (target) target.textContent = "";
    if (stat) {
        stat.hidden = true;
        stat.removeAttribute("title");
    }
}

function _invalidateMainCostEstimate() {
    _mainEstimateRequestSeq += 1;
}

function _renderLocalCostEstimateImmediately(textCount) {
    const extraction = window._engaixtExtractionStats;
    const selectedPath = document.getElementById("path-input")?.value || "";
    const count = textCount ?? extraction?.text_count;
    if (!selectedPath || count === undefined || count === null) {
        _hideMainCostEstimate();
        return;
    }
    const selected = _selectedEstimateTarget();
    renderMainCostEstimate({
        available: true,
        provider: "hy_mt2",
        model: selected.model,
        text_count: Number(count || 0),
        estimated_input_tokens: 0,
        estimated_output_tokens: 0,
        estimated_cost_cny: 0,
        is_local: true,
        note: t("本地模型在本机运行，不产生云端 API 费用"),
    });
}

function handleTranslatorCostEstimateChanged(value) {
    _invalidateMainCostEstimate();
    const provider = String(value || _selectedEstimateTarget().provider).trim().toLowerCase();
    if (provider === "hy_mt2") {
        _renderLocalCostEstimateImmediately();
        return;
    }
    // A cloud estimate is asynchronous. Do not leave the previous provider's
    // amount visible while the newly selected provider is being estimated.
    _hideMainCostEstimate();
    refreshMainCostEstimate();
}

async function refreshMainCostEstimate(path, textCount, sourceChars) {
    const requestSeq = ++_mainEstimateRequestSeq;
    const selectedPath = path || document.getElementById("path-input")?.value || "";
    const extraction = window._engaixtExtractionStats;
    const count = textCount ?? extraction?.text_count;
    const chars = sourceChars ?? extraction?.source_chars;
    if (!selectedPath || count === undefined || count === null) return;
    const selected = _selectedEstimateTarget();
    if (selected.provider === "hy_mt2") {
        _renderLocalCostEstimateImmediately(count);
        return;
    }
    if (!window.pywebview || !pywebview.api?.estimate_translation_cost) return;
    const targetKey = `${selected.provider}|${selected.model}`;
    try {
        const result = await pywebview.api.estimate_translation_cost(
            selectedPath,
            selected.provider,
            selected.model,
            Number(count || 0),
            Number(chars || 0),
        );
        const current = _selectedEstimateTarget();
        const currentKey = `${current.provider}|${current.model}`;
        if (requestSeq === _mainEstimateRequestSeq
            && targetKey === currentKey
            && selectedPath === (document.getElementById("path-input")?.value || "")) {
            renderMainCostEstimate(result);
        }
    } catch (_) {
        // A failed request must not leave a stale provider amount visible.
        if (requestSeq === _mainEstimateRequestSeq) _hideMainCostEstimate();
    }
}
