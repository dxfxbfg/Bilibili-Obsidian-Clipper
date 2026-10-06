const el = {
  progress: document.getElementById("progress"),
  progressFill: document.getElementById("progressFill"),
  message: document.getElementById("message"),
  videoTitle: document.getElementById("videoTitle"),
  videoMeta: document.getElementById("videoMeta"),
  previewCount: document.getElementById("previewCount"),
  subtitleSelect: document.getElementById("subtitleSelect"),
  preview: document.getElementById("preview"),
  copyBtn: document.getElementById("copyBtn"),
  downloadBtn: document.getElementById("downloadBtn"),
  sendBtn: document.getElementById("sendBtn"),
  whisperBtn: document.getElementById("whisperBtn"),
  readingViewBtn: document.getElementById("readingViewBtn"),
  aiBtn: document.getElementById("aiBtn"),
  settingsBtn: document.getElementById("settingsBtn")
};

// 没有字幕轨时，"语言"里选的是想要什么语言的字幕。
// 选中文就意味着要翻译——所以不再单独放一个"自动翻译"开关。
const OUTPUT_MODES = [
  { value: "bilingual", label: "中英对照" },
  { value: "translated", label: "仅中文" },
  { value: "original", label: "仅原文" }
];
const OUTPUT_MODE_VALUES = new Set(OUTPUT_MODES.map((item) => item.value));

let latestPayload = null;
let whisperPollTimer = 0;
let whisperPollFailures = 0;
let progressLeaveTimer = 0;
const EXPECTED_CONTENT_SCRIPT_VERSION = chrome.runtime.getManifest().version || "";
const DEFAULT_SETTINGS = {
  downloadFormat: "srt",
  popupShowCopy: true,
  popupShowDownload: true,
  popupShowReading: true,
  popupShowAi: true,
  popupShowSettings: true
};

function formatLocalDate(value = Date.now()) {
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

init().catch((error) => {
  setStatus(`初始化失败：${error.message}`, true);
});

async function init() {
  bindEvents();
  applyPanelVisibility(await getSettingsFromRuntime());

  // 内容脚本如果已经抓过当前视频，先把结果直接画出来，不再白等一次
  // 完整的网络往返（视频信息 + 字幕列表 + 字幕内容）。
  const tab = await getActiveTab();
  const cached = await sendToContent({ type: "popup-get-state" });
  if (!cached?.ok) {
    // 连不上内容脚本（不在 B 站视频页，或页面需要刷新）。
    // sendToContent 已经把原因写到状态栏了，不必再用一次必然失败的抓取覆盖它。
    render(cached?.payload || latestPayload, { preserveStatus: true });
    return;
  }

  // 转写是在页面里跑的，弹窗重新打开时要接着看进度，而不是从头抓一遍
  if (cached.payload?.generating) {
    render(cached.payload);
    pollWhisperProgress();
    return;
  }

  if (isSameVideo(cached.payload?.url, tab?.url) && hasUsableSubtitle(cached.payload)) {
    render(cached.payload);
    return;
  }

  await refreshFromTab();
}

// 哪些按钮显示由设置决定；三个操作全关掉时整行收起
function applyPanelVisibility(settings) {
  const pairs = [
    [el.copyBtn, settings.popupShowCopy],
    [el.downloadBtn, settings.popupShowDownload],
    [el.readingViewBtn, settings.popupShowReading],
    [el.aiBtn, settings.popupShowAi],
    [el.settingsBtn, settings.popupShowSettings]
  ];
  pairs.forEach(([node, visible]) => {
    if (node) {
      node.hidden = visible === false;
    }
  });

  const actions = document.querySelector(".actions");
  if (actions) {
    actions.hidden = !actions.querySelector("button:not([hidden])");
  }
}

function hasUsableSubtitle(payload) {
  return Boolean(
    (payload?.subtitleOptions || []).length || String(payload?.subtitlePreview || "").trim()
  );
}

function isSameVideo(a, b) {
  const left = extractBvid(a);
  return Boolean(left) && left === extractBvid(b);
}

function extractBvid(url) {
  const match = String(url || "").match(/BV[0-9A-Za-z]{10}/);
  return match ? match[0] : "";
}

function bindEvents() {
  el.copyBtn.addEventListener("click", async () => {
    const payload = await ensurePayload();
    if (!payload?.markdown) {
      setMessage("没有可复制内容，请先刷新。");
      return;
    }
    try {
      await navigator.clipboard.writeText(payload.markdown);
      setMessage("已复制完整 Markdown。");
    } catch (error) {
      setMessage(`复制失败：${error?.message || "无法访问剪贴板"}`);
    }
  });

  el.downloadBtn.addEventListener("click", async () => {
    const payload = await ensurePayload();
    const settings = await getSettingsFromRuntime();
    const format = normalizeDownloadFormat(settings?.downloadFormat || payload?.downloadFormat);
    const content =
      format === "txt" ? payload?.txt || payload?.subtitlePreview || "" : payload?.srt || "";
    if (!content) {
      setMessage("没有可下载字幕。");
      return;
    }
    const safeTitle = sanitizeFileName(payload.title || "bilibili-subtitle");
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${safeTitle}.${format}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setMessage(`已下载 ${format.toUpperCase()}。`);
  });

  el.sendBtn.addEventListener("click", async () => {
    setStatus("正在发送到 Obsidian...");
    const resp = await sendToContent({ type: "popup-send-obsidian" });
    if (!resp?.ok) {
      setStatus(`发送失败：${resp?.error || "未知错误"}`, true);
      setMessage(`发送失败：${resp?.error || "未知错误"}`);
    }
    render(resp?.payload || latestPayload);
  });

  el.whisperBtn?.addEventListener("click", () => {
    const output = String(latestPayload?.whisperOutput || el.subtitleSelect?.value || "bilingual");
    startWhisperGeneration(output);
  });

  el.readingViewBtn?.addEventListener("click", async () => {
    const tab = await getActiveTab();
    if (!isSupportedSubtitlePage(tab?.url || "")) {
      setMessage("请先打开一个 B 站视频页。");
      return;
    }

    const prepResp = await sendToContent({ type: "popup-get-state" });
    if (!prepResp?.ok) {
      setStatus(prepResp?.error || "请刷新浏览器网页重试，或当前网页不支持", true);
      setMessage(prepResp?.error || "请刷新浏览器网页重试，或当前网页不支持");
      return;
    }

    setStatus("正在打开阅读视图...");
    const resp = await sendToRuntime({
      type: "open-reading-view-tab",
      url: tab.url,
      tabId: tab.id
    });
    if (!resp?.ok) {
      setStatus(`打开失败：${resp?.error || "未知错误"}`, true);
      setMessage(`打开失败：${resp?.error || "未知错误"}`);
      return;
    }
    setMessage("已在当前页面打开阅读视图。");
    setStatus("阅读视图已打开。");
    window.setTimeout(() => window.close(), 80);
  });

  el.subtitleSelect.addEventListener("change", async (event) => {
    const value = String(event.target.value || "");
    if (!value) {
      return;
    }

    // 无字幕轨（或当前就是生成的字幕）时，这里选的是输出语言：
    // 选定即开始生成，选中文就自动翻译
    if (latestPayload?.whisperTrack || latestPayload?.canGenerate) {
      if (!OUTPUT_MODE_VALUES.has(value)) {
        return;
      }
      // 只记住选择，不要替用户开始生成——生成必须由按钮显式触发
      await sendToRuntime({ type: "save-settings", settings: { whisperOutputMode: value } });
      latestPayload.whisperOutput = value;
      applyGenerationState(latestPayload);
      return;
    }

    setStatus("正在切换字幕...");
    const option = event.target.options[event.target.selectedIndex];
    const url = String(option?.value || "");
    if (!url) {
      return;
    }
    const resp = await sendToContent({
      type: "popup-select-subtitle",
      url,
      lang: String(option.dataset.lang || "unknown"),
      subtitleId: String(option.dataset.id || "")
    });
    if (!resp?.ok) {
      setStatus(`切换失败：${resp?.error || "未知错误"}`, true);
      setMessage(`切换失败：${resp?.error || "未知错误"}`);
    }
    render(resp?.payload || latestPayload);
  });

  el.settingsBtn.addEventListener("click", async () => {
    await sendToRuntime({ type: "open-options" });
  });

  el.aiBtn?.addEventListener("click", async () => {
    try {
      if (globalThis.browser?.sidebarAction?.open) {
        globalThis.browser.sidebarAction.open();
        window.setTimeout(() => window.close(), 80);
        return;
      }

      const tab = await getActiveTab();
      if (!tab?.id) {
        setStatus("找不到当前标签页。", true);
        setMessage("找不到当前标签页。");
        return;
      }

      if (chrome.sidePanel?.open) {
        await chrome.sidePanel.open({ tabId: tab.id });
      } else {
        throw new Error("当前浏览器不支持扩展侧边栏");
      }
      window.setTimeout(() => window.close(), 80);
    } catch (error) {
      setStatus(`打开侧边栏失败：${error?.message || error}`, true);
      setMessage(`打开侧边栏失败：${error?.message || error}`);
    }
  });
}

async function refreshFromTab() {
  setStatus("正在抓取...");
  const resp = await sendToContent({ type: "popup-refresh" });
  if (!resp?.ok) {
    const errorText = (resp?.error || "请在 B 站视频页使用。").replace(
      "请刷新浏览器网页重试，或当前网页不支持",
      "请刷新网页重试，或当前网页不支持"
    );
    setStatus(`抓取失败：${errorText}`, true);
    render(resp?.payload || latestPayload, { preserveStatus: true });
    return;
  }
  render(resp?.payload || latestPayload);
}

/**
 * 转写在页面里跑，popup 只负责看进度。
 * 必须轮询而不是 await：popup 一失焦就被关掉，任务不能跟着死。
 */
async function startWhisperGeneration(output) {
  if (el.whisperBtn) {
    el.whisperBtn.disabled = true;
  }
  if (el.subtitleSelect) {
    el.subtitleSelect.disabled = true;
  }
  const label = outputLabel(output);
  setStatus(`正在生成${label ? `「${label}」` : ""}字幕...`);

  const resp = await sendToContent({ type: "popup-generate-subtitle", output });
  if (!resp?.ok) {
    setStatus(`生成失败：${resp?.error || "未知错误"}`, true);
    render(resp?.payload || latestPayload, { preserveStatus: true });
    return;
  }
  render(resp?.payload || latestPayload);
  pollWhisperProgress();
}

function outputLabel(value) {
  const found = OUTPUT_MODES.find((item) => item.value === value);
  return found ? found.label : "";
}

function pollWhisperProgress() {
  window.clearTimeout(whisperPollTimer);
  whisperPollFailures = 0;

  async function tick() {
    const resp = await sendToContent({ type: "popup-get-state" });
    const payload = resp?.payload || null;
    if (!payload) {
      return;
    }
    if (resp?.ok === false && whisperPollFailures < 5) {
      // 页面正好在切换之类的情况，再给几次机会
      whisperPollFailures += 1;
    } else {
      whisperPollFailures = 0;
      if (payload.generating) {
        // 生成期间只动进度和状态，别重建整个面板
        applyGenerationState(payload);
        const statusText = String(payload.status || "").trim();
        if (statusText) {
          setStatus(statusText);
        }
      } else {
        render(payload);
        return;
      }
    }
    whisperPollTimer = window.setTimeout(tick, 1200);
  }

  tick();
}

window.addEventListener("unload", () => window.clearTimeout(whisperPollTimer));

async function ensurePayload() {
  if (latestPayload) {
    return latestPayload;
  }
  const resp = await sendToContent({ type: "popup-get-state" });
  if (resp?.ok && resp.payload) {
    latestPayload = resp.payload;
  }
  return latestPayload;
}

function render(payload, { preserveStatus = false } = {}) {
  if (!payload) {
    return;
  }
  latestPayload = payload;

  if (!preserveStatus && /失败|错误|不可用|不支持/.test(String(payload.status || ""))) {
    setFeedback(payload.status, true);
  } else {
    setMessage(payload.message || "");
  }

  // 页面里可能还挂着旧版注入脚本（扩展更新后没有刷新页面），
  // 那种情况下新入口是不存在的，直接说清楚而不是让人对着没反应的按钮猜。
  if (
    payload.contentVersion &&
    EXPECTED_CONTENT_SCRIPT_VERSION &&
    payload.contentVersion !== EXPECTED_CONTENT_SCRIPT_VERSION
  ) {
    setMessage(`页面里是旧版脚本（${payload.contentVersion}），请刷新页面（⌘R / Ctrl+R）后再试。`);
  }

  setText(el.videoTitle, payload.title || "尚未识别视频");
  el.videoTitle.title = payload.title || "";
  setText(el.videoMeta, buildMetaLine(payload));

  const options = payload.subtitleOptions || [];
  // 没有字幕轨时，"语言"里放的是想要的输出语言；已经生成出来后
  // 也继续放这几个选项，换一个就等于重新生成
  if (payload.canGenerate || payload.whisperTrack) {
    const current = String(payload.whisperOutput || "bilingual");
    setHtml(
      el.subtitleSelect,
      OUTPUT_MODES.map(
        (item) =>
          `<option value="${item.value}"${item.value === current ? " selected" : ""}>${item.label}</option>`
      ).join("")
    );
    el.subtitleSelect.disabled = Boolean(payload.generating);
  } else if (options.length === 0) {
    setHtml(el.subtitleSelect, '<option value="">暂无字幕</option>');
    el.subtitleSelect.disabled = true;
  } else {
    setHtml(el.subtitleSelect, options
      .map((item) => {
        const selected = item.selected ? "selected" : "";
        const aiTag = item.isAi ? " [AI]" : "";
        return `<option value="${escapeHtml(item.url)}" data-id="${escapeHtml(
          item.id || ""
        )}" data-lang="${escapeHtml(item.lang || "")}" ${selected}>${escapeHtml(
          `${item.lang || "unknown"}${aiTag}`
        )}</option>`;
      })
      .join(""));
    el.subtitleSelect.disabled = false;
  }

  // 从"空预览"变成"有内容"时淡入，不要整块啪地出现
  const hadPreview = Boolean(el.preview.value);
  setValue(el.preview, payload.subtitlePreview || "");
  if (!hadPreview && el.preview.value) {
    playEnter(document.querySelector(".preview-card"));
  }
  setText(el.previewCount, countPreviewLines(payload.subtitlePreview));
  applyGenerationState(payload);
}

function buildMetaLine(payload) {
  const parts = [payload.author, payload.bvid, payload.uploadDate].filter(Boolean);
  if (parts.length) {
    return parts.join(" · ");
  }
  return String(payload.url || "");
}

function countPreviewLines(text) {
  const lines = String(text || "").split("\n").filter((line) => line.trim()).length;
  return lines ? `${lines} 行` : "";
}

/**
 * 只更新与"生成字幕"相关的节点。
 * 轮询期间不能整个 render：重建 select 的 innerHTML 会让面板闪、也会白费布局。
 */
function applyGenerationState(payload) {
  const generating = Boolean(payload?.generating);
  const progress = Math.max(0, Math.min(100, Number(payload?.progress) || 0));

  if (el.progress) {
    if (generating) {
      window.clearTimeout(progressLeaveTimer);
      progressLeaveTimer = 0;
      el.progress.classList.remove("is-leaving");
      setVisible(el.progress, true);
      if (el.progressFill) {
        el.progressFill.style.width = `${progress}%`;
      }
    } else if (!el.progress.hidden && !progressLeaveTimer) {
      // 先让它走满再淡出，不要停在 60% 就消失
      if (el.progressFill) {
        el.progressFill.style.width = "100%";
      }
      el.progress.classList.add("is-leaving");
      progressLeaveTimer = window.setTimeout(() => {
        el.progress.hidden = true;
        el.progress.classList.remove("is-leaving");
        progressLeaveTimer = 0;
      }, 240);
    }
  }

  if (el.whisperBtn) {
    // 生成完成后 fetchState 从 empty 变成 ready，canGenerate 会变 false；
    // 这里必须把"已经有生成的字幕"也算进去，否则按钮会在完成那一刻消失。
    const canGenerate = Boolean(payload?.canGenerate);
    const hasTrack = Boolean(payload?.whisperTrack);
    setVisible(el.whisperBtn, canGenerate || hasTrack || generating);
    el.whisperBtn.disabled = generating;
    el.whisperBtn.textContent = generating
      ? "生成中…"
      : hasTrack
        ? "重新生成字幕"
        : "生成字幕";
  }
}

/**
 * 只在内容真的变了才写 DOM。
 * 生成完成那一刻会把整份载荷重新过一遍，无脑赋值等于整块重绘，
 * 视觉上就是闪一下；大部分字段其实没变。
 */
function setText(node, text) {
  if (!node) {
    return;
  }
  const value = String(text ?? "");
  if (node.textContent !== value) {
    node.textContent = value;
  }
}

/**
 * 显隐切换。直接改 hidden 是瞬间跳变，"生硬"就来自这里；
 * 从隐藏变可见时补一段进场动画。
 */
function setVisible(node, visible) {
  if (!node) {
    return;
  }
  const wasHidden = node.hidden;
  node.hidden = !visible;
  if (visible && wasHidden) {
    playEnter(node);
  }
}

function playEnter(node) {
  node.classList.remove("boc-enter");
  void node.offsetWidth;
  node.classList.add("boc-enter");
}

function setValue(node, value) {
  if (!node) {
    return;
  }
  const next = String(value ?? "");
  if (node.value !== next) {
    node.value = next;
  }
}

function setHtml(node, html) {
  if (!node) {
    return;
  }
  const next = String(html ?? "");
  if (node.__bocHtml !== next) {
    node.innerHTML = next;
    node.__bocHtml = next;
  }
}

/**
 * 过程性提示一律不显示——进度看进度条，状态看内容本身。
 * 只有真正的异常才说出来，否则界面保持安静。
 */
function setStatus(text, isError = false) {
  if (!isError) {
    return;
  }
  setFeedback(text, true);
}

function setFeedback(text, isError = false) {
  const value = String(text || "");
  el.message.textContent = value;
  el.message.classList.toggle("is-error", Boolean(isError));
}

function setMessage(text) {
  setFeedback(text, false);
}

function sanitizeFileName(value) {
  return String(value || "subtitle")
    .replace(/[\\/:*?"<>|]/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120);
}

function normalizeDownloadFormat(value) {
  return value === "txt" ? "txt" : "srt";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs?.[0] || null;
}

async function getActiveTabId() {
  const tab = await getActiveTab();
  return tab?.id || null;
}

async function sendToContent(message) {
  const tab = await getActiveTab();
  const tabId = tab?.id || null;
  if (!tabId) {
    throw new Error("找不到当前标签页");
  }

  try {
    return await sendMessageToTab(tabId, message);
  } catch (error) {
    if (shouldRetryAfterInjection(error) && isSupportedSubtitlePage(tab?.url || "")) {
      try {
        await ensureContentScriptReady(tabId);
        await sleep(80);
        return await sendMessageToTab(tabId, message);
      } catch (retryError) {
        error = retryError;
      }
    }

    const normalizedError = normalizeContentErrorMessage(error);
    setStatus("请在 B 站视频页使用插件。");
    setMessage(normalizedError);
    return { ok: false, error: normalizedError, payload: latestPayload };
  }
}

function normalizeContentErrorMessage(error) {
  const message = String(error?.message || "").trim();
  if (message.includes("Could not establish connection. Receiving end does not exist.")) {
    return "请刷新浏览器网页重试，或当前网页不支持";
  }
  return message || "未知错误";
}

function shouldRetryAfterInjection(error) {
  const message = String(error?.message || "");
  return message.includes("Could not establish connection. Receiving end does not exist.");
}

function isSupportedSubtitlePage(url) {
  try {
    const parsed = new URL(String(url || ""));
    if (parsed.hostname !== "www.bilibili.com") {
      return false;
    }
    return parsed.pathname === "/list/watchlater" ||
      parsed.pathname === "/list/watchlater/" ||
      parsed.pathname.startsWith("/video/");
  } catch {
    return false;
  }
}

async function ensureContentScriptReady(tabId) {
  if (!chrome.scripting) {
    throw new Error("请刷新浏览器网页重试，或当前网页不支持");
  }

  const loadedVersion = await probeContentScriptVersion(tabId);
  if (loadedVersion === EXPECTED_CONTENT_SCRIPT_VERSION) {
    return;
  }

  await chrome.scripting.insertCSS({
    target: { tabId },
    files: ["content.css"]
  });

  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"]
    });
  } catch (error) {
    const message = String(error?.message || "");
    if (!message.includes("Identifier 'DEFAULT_SETTINGS' has already been declared")) {
      throw error;
    }
  }

  const reinjectedVersion = await probeContentScriptVersion(tabId);
  if (reinjectedVersion !== EXPECTED_CONTENT_SCRIPT_VERSION) {
    throw new Error("扩展刚更新，请刷新当前页面后重试。");
  }
}

async function probeContentScriptVersion(tabId) {
  try {
    const probe = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => globalThis.__BOC_CONTENT_SCRIPT_LOADED__ || ""
    });
    return String(probe?.[0]?.result || "");
  } catch {
    return "";
  }
}

async function sendMessageToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(resp);
    });
  });
}

async function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function sendToRuntime(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(resp);
    });
  });
}

async function getSettingsFromRuntime() {
  try {
    const resp = await sendToRuntime({ type: "get-settings" });
    if (!resp?.ok) {
      return { ...DEFAULT_SETTINGS };
    }
    return { ...DEFAULT_SETTINGS, ...(resp.settings || {}) };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}
