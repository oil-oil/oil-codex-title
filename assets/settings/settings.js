"use strict";
const byId = (id) => document.getElementById(id);
const form = byId("settings-form");
const radios = Array.from(document.querySelectorAll('input[name="provider"]'));
let snapshot = null;
let drafts = {};
let mode = "official";
let busy = false;
let keyBusy = false;
let timeDraft = true;
const knownKeys = new Set();
const keyDialog = byId("key-dialog");
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get("token") || sessionStorage.getItem("oil-settings-token") || "";
if (fragment.has("token")) {
  sessionStorage.setItem("oil-settings-token", token);
  history.replaceState(null, "", location.pathname);
}

async function api(method, body, path = "/api/config") {
  const response = await fetch(path, {method, cache: "no-store",
    headers: {"X-Oil-Settings-Token": token, ...(body ? {"Content-Type": "application/json"} : {})},
    body: body ? JSON.stringify(body) : undefined});
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || "设置操作失败");
  return result;
}
function message(text, type = "") {
  byId("save-status").textContent = text;
  byId("save-status").className = type;
}
function takeDraft() {
  timeDraft = byId("title-time").checked;
  drafts[mode] = {model: byId("model").value, service_tier: byId("fast-tier").checked ? "fast" : "standard",
    ...(mode === "relay" ? {base_url: byId("base-url").value, api_key_env: byId("api-key-env").value} : {})};
}
function updateKeyStatus() {
  const name = byId("api-key-env").value;
  const matches = snapshot.relay && byId("api-key-env").value === snapshot.relay.api_key_env;
  const present = knownKeys.has(name) || (matches && snapshot.api_key_present);
  const text = knownKeys.has(name) ? "密钥已保存到用户环境变量。请完全重启 Codex。" :
    present ? "设置服务已识别该密钥环境变量。" : "可点击右侧按钮设置密钥，保存后完全重启 Codex。";
  byId("key-status").textContent = text;
  byId("key-status").className = present ? "key-status" : "key-status missing";
}
function changed() {
  takeDraft();
  const original = mode === "official" ? snapshot.official : snapshot.relay;
  // 以字段值比较，避免 JSON 属性顺序造成伪变更。
  const equal = original && Object.keys(drafts[mode]).every((key) => drafts[mode][key] === original[key]);
  const dirty = mode !== snapshot.provider || !equal || timeDraft !== snapshot.show_last_user_time;
  byId("save").disabled = busy || !dirty;
  if (dirty && !busy) message("有未保存的更改");
  else if (!busy) message("配置已读取");
  updateKeyStatus();
}
function showMode() {
  const relay = mode === "relay";
  radios.forEach((radio) => {radio.checked = radio.value === mode;});
  byId("configuration-title").textContent = relay ? "中转站配置" : "官方模型";
  byId("relay-fields").hidden = !relay;
  byId("base-url").required = relay;
  byId("api-key-env").required = relay;
  byId("base-url").disabled = !relay;
  byId("api-key-env").disabled = !relay;
  byId("set-key").disabled = !relay || !snapshot.key_management_supported;
  byId("key-help").textContent = snapshot.key_management_supported ?
    "填写保存 API Key 的变量名，或点击右侧按钮设置密钥。" : "此系统请通过环境变量设置密钥；对话框保存目前支持 Windows。";
  byId("model").value = drafts[mode].model;
  byId("fast-tier").checked = drafts[mode].service_tier === "fast";
  byId("title-time").checked = timeDraft;
  byId("base-url").value = drafts.relay.base_url;
  byId("api-key-env").value = drafts.relay.api_key_env;
  byId("model-help").textContent = relay ? "填写中转站提供的准确模型 ID。" : "默认使用 gpt-5.6-luna；模型需对当前官方账号可用。";
  changed();
}
function adopt(data) {
  snapshot = data;
  drafts = {official: {...data.official}, relay: {...(data.relay || {
    base_url: "", api_key_env: "OIL_TITLE_RELAY_KEY", model: "", service_tier: "standard"})}};
  mode = data.provider;
  timeDraft = data.show_last_user_time ?? true;
  byId("saved-mode").textContent = "当前：" + (mode === "official" ? "官方账号" : "中转站");
  byId("config-path").textContent = data.config_path;
  form.hidden = false;
  byId("load-error").hidden = true;
  showMode();
}
function setBusy(value) {
  busy = value;
  form.setAttribute("aria-busy", String(value));
  Array.from(form.elements).forEach((element) => {element.disabled = value;});
  if (!value && snapshot) showMode();
}
async function reload() {
  setBusy(true);
  let failure = null;
  try {adopt(await api("GET"));}
  catch (error) {
    if (snapshot) failure = error.message;
    else {byId("load-error").hidden = false; byId("load-error").textContent = error.message;}
  } finally {setBusy(false); if (failure) message(failure, "error");}
}
radios.forEach((radio) => radio.addEventListener("change", () => {
  takeDraft(); mode = radio.value; showMode();
}));
byId("model").addEventListener("input", () => {
  const original = mode === "official" ? snapshot.official : snapshot.relay;
  if (!original || byId("model").value !== original.model) byId("fast-tier").checked = false;
  changed();
});
["base-url", "api-key-env", "fast-tier", "title-time"].forEach((id) => byId(id).addEventListener("input", changed));
byId("reload").addEventListener("click", reload);
function closeKeyDialog() {
  if (!keyBusy) keyDialog.close();
}
byId("set-key").addEventListener("click", () => {
  const name = byId("api-key-env").value;
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(name)) {
    message("请先填写有效的密钥环境变量名", "error");
    byId("api-key-env").focus();
    return;
  }
  byId("key-target").value = name;
  byId("key-value").value = "";
  byId("key-message").textContent = "";
  keyDialog.showModal();
  byId("key-value").focus();
});
["key-close", "key-cancel"].forEach((id) => byId(id).addEventListener("click", closeKeyDialog));
keyDialog.addEventListener("cancel", (event) => {if (keyBusy) event.preventDefault();});
keyDialog.addEventListener("close", () => {
  byId("key-value").value = "";
  byId("key-target").value = "";
  byId("key-message").textContent = "";
  byId("set-key").focus();
});
byId("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (keyBusy) return;
  keyBusy = true;
  const name = byId("key-target").value;
  byId("key-message").textContent = "正在保存…";
  Array.from(byId("key-form").elements).forEach((element) => {element.disabled = true;});
  try {
    await api("PUT", {api_key_env: name, api_key: byId("key-value").value}, "/api/key");
    knownKeys.add(name);
    keyDialog.close();
    updateKeyStatus();
    message("密钥已保存，请完全重启 Codex；模式和模型仍需单独保存", "success");
  } catch (error) {
    byId("key-message").textContent = error.message;
  } finally {
    byId("key-value").value = "";
    keyBusy = false;
    Array.from(byId("key-form").elements).forEach((element) => {element.disabled = false;});
    if (keyDialog.open) byId("key-value").focus();
  }
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  takeDraft();
  const payload = {revision: snapshot.revision, provider: mode, ...drafts[mode], show_last_user_time: timeDraft};
  setBusy(true);
  message("正在保存…");
  let result;
  try {adopt(await api("PUT", payload)); result = ["已保存，下次命名生效", "success"];}
  catch (error) {result = [error.message, "error"];}
  finally {setBusy(false); message(...result);}
});
reload();
