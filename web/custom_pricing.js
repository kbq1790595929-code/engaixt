// 用户自定义模型价格：仅用于费用预估，不参与任何计费。
let _cpTable = {};
let _cpModels = {};

function showCustomPricingHelp() {
    window.alert(
        "按服务商官网价格填写（元/百万 tokens）。\n" +
        "保存后，费用预估优先使用这里的价格；未填写的模型仍使用内置估算。"
    );
}

function _cpProviders() {
    const source = document.querySelector('#settings-overlay [name="active_translator"]');
    const result = [];
    if (source) {
        Array.from(source.options).forEach(option => {
            if (option.value && option.value !== "__provider_default__") {
                result.push({ value: option.value, label: option.textContent.trim() });
            }
        });
    }
    return result;
}

function _cpCurrentProvider() {
    const select = document.getElementById("custom-pricing-provider");
    return (select && select.value) || "";
}

function openCustomPricing() {
    const overlay = document.getElementById("custom-pricing-overlay");
    if (!overlay) return;
    const providerSelect = document.getElementById("custom-pricing-provider");
    if (providerSelect && !providerSelect.options.length) {
        _cpProviders().forEach(item => {
            const option = document.createElement("option");
            option.value = item.value;
            option.textContent = item.label;
            providerSelect.appendChild(option);
        });
        const active = document.querySelector('#settings-overlay [name="active_translator"]');
        if (active && active.value) providerSelect.value = active.value;
    }
    overlay.classList.add("open");
    loadCustomPricing();
}

function closeCustomPricing() {
    const overlay = document.getElementById("custom-pricing-overlay");
    if (overlay) overlay.classList.remove("open");
}

async function loadCustomPricing() {
    try {
        if (window.pywebview && pywebview.api && pywebview.api.get_custom_pricing) {
            const data = await pywebview.api.get_custom_pricing();
            _cpTable = (data && data.custom_pricing) || {};
        }
    } catch (_) {
        _cpTable = {};
    }
    await onCustomPricingProviderChanged();
}

async function onCustomPricingProviderChanged() {
    const provider = _cpCurrentProvider();
    if (!provider) return;
    await ensureCustomPricingModels(provider);
    renderCustomPricingRows();
}

async function ensureCustomPricingModels(provider) {
    if (_cpModels[provider]) return;
    let models = [];
    try {
        if (window.pywebview && pywebview.api && pywebview.api.get_cloud_model_catalog) {
            const data = await pywebview.api.get_cloud_model_catalog(provider);
            if (data && Array.isArray(data.models)) {
                models = data.models.map(row => row.id).filter(Boolean);
            }
        }
    } catch (_) {
        models = [];
    }
    Object.keys((_cpTable && _cpTable[provider]) || {}).forEach(name => {
        if (name && !models.includes(name)) models.push(name);
    });
    _cpModels[provider] = models;
}

function renderCustomPricingRows() {
    const tbody = document.getElementById("custom-pricing-rows");
    if (!tbody) return;
    tbody.replaceChildren();
    const provider = _cpCurrentProvider();
    const saved = (_cpTable && _cpTable[provider]) || {};
    const models = _cpModels[provider] || [];
    const names = new Set([...models, ...Object.keys(saved)]);
    if (!names.size) names.add("");
    names.forEach(name => addCustomPricingRow(name, saved[name], models));
}

function addCustomPricingRow(name, entry, models) {
    const tbody = document.getElementById("custom-pricing-rows");
    if (!tbody) return;
    const provider = _cpCurrentProvider();
    const available = models || _cpModels[provider] || [];
    const row = document.createElement("tr");

    const modelCell = document.createElement("td");
    const modelSelect = document.createElement("select");
    modelSelect.className = "cp-model";
    const options = new Set([...available, name].filter(Boolean));
    if (!options.size) {
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "先在上一个页面刷新模型列表";
        modelSelect.appendChild(placeholder);
    }
    options.forEach(value => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        modelSelect.appendChild(option);
    });
    if (name) modelSelect.value = name;
    modelCell.appendChild(modelSelect);

    const hitCell = document.createElement("td");
    hitCell.appendChild(_cpNumberInput("cp-hit", entry && entry.input_cache_hit_cny_per_m));

    const missCell = document.createElement("td");
    missCell.appendChild(_cpNumberInput("cp-miss", entry && entry.input_cache_miss_cny_per_m));

    const outputCell = document.createElement("td");
    outputCell.appendChild(_cpNumberInput("cp-output", entry && entry.output_cny_per_m));

    const removeCell = document.createElement("td");
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "icon-btn";
    removeButton.title = "删除";
    removeButton.innerHTML = '<svg class="ic"><use href="#i-trash"/></svg>';
    removeButton.onclick = () => row.remove();
    removeCell.appendChild(removeButton);

    row.append(modelCell, hitCell, missCell, outputCell, removeCell);
    row.querySelector("select").dataset.savedModel = name || "";
    tbody.appendChild(row);
}

function _cpNumberInput(className, value) {
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.step = "0.01";
    input.className = className;
    input.placeholder = "0";
    if (value !== undefined && value !== null && value !== "") input.value = value;
    return input;
}

async function saveCustomPricing() {
    const status = document.getElementById("custom-pricing-status");
    const provider = _cpCurrentProvider();
    if (!provider) return;
    if (!window.pywebview || !pywebview.api || !pywebview.api.save_custom_pricing) {
        if (status) status.textContent = "当前版本不支持保存价格，请重启 EngAixt";
        window.alert("当前 EngAixt 版本不支持保存价格，请重启 EngAixt 后重试。");
        return;
    }
    const table = {};
    let skipped = 0;
    document.querySelectorAll("#custom-pricing-rows tr").forEach(row => {
        const model = (row.querySelector(".cp-model")?.value || "").trim();
        const rawHit = (row.querySelector(".cp-hit")?.value || "").trim();
        const rawMiss = (row.querySelector(".cp-miss")?.value || "").trim();
        const rawOutput = (row.querySelector(".cp-output")?.value || "").trim();
        if (!model && !rawHit && !rawMiss && !rawOutput) return;
        if (!model || rawOutput === "") {
            skipped += 1;
            return;
        }
        const output = Number(rawOutput);
        const hit = rawHit === "" ? null : Number(rawHit);
        const miss = rawMiss === "" ? null : Number(rawMiss);
        if (!Number.isFinite(output) || output < 0) {
            skipped += 1;
            return;
        }
        if ((hit !== null && (!Number.isFinite(hit) || hit < 0)) ||
            (miss !== null && (!Number.isFinite(miss) || miss < 0))) {
            skipped += 1;
            return;
        }
        const inputRate = miss !== null ? miss : (hit !== null ? hit : 0);
        const entry = { input_cny_per_m: inputRate, output_cny_per_m: output };
        if (hit !== null) entry.input_cache_hit_cny_per_m = hit;
        if (miss !== null) entry.input_cache_miss_cny_per_m = miss;
        table[model] = entry;
    });
    try {
        const data = await pywebview.api.save_custom_pricing({ [provider]: table });
        if (data && data.ok) {
            _cpTable = data.custom_pricing || {};
            delete _cpModels[provider];
            if (typeof refreshCloudPrice === "function") refreshCloudPrice();
            closeCustomPricing();
        } else if (status) {
            status.textContent = (data && data.error) || "保存失败";
        }
    } catch (error) {
        if (status) status.textContent = "保存失败：" + (error && error.message ? error.message : error);
    }
}
