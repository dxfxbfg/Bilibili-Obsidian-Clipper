const el = {
  status: document.getElementById("status"),
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
const EXPECTED_CONTENT_SCRIPT_VERSION = chrome.runtime.getManifest().version || "";
const DEFAULT_SETTINGS = {
  downloadFormat: "srt"
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
      await sendToRuntime({ type: "save-settings", settings: { whisperOutputMode: value } });
      await startWhisperGeneration(value);
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

  if (!preserveStatus) {
    const statusText = String(payload.status || "准备就绪");
    const isErrorStatus = /失败|错误|不可用|不支持/.test(statusText);
    setStatus(statusText, isErrorStatus);
  }
  setMessage(payload.message || "");

  // 页面里可能还挂着旧版注入脚本（扩展更新后没有刷新页面），
  // 那种情况下新入口是不存在的，直接说清楚而不是让人对着没反应的按钮猜。
  if (
    payload.contentVersion &&
    EXPECTED_CONTENT_SCRIPT_VERSION &&
    payload.contentVersion !== EXPECTED_CONTENT_SCRIPT_VERSION
  ) {
    setMessage(`页面里是旧版脚本（${payload.contentVersion}），请刷新页面（⌘R / Ctrl+R）后再试。`);
  }

  // 正常状态下内容都在眼前，再报一遍条数只是噪音；
  // 只有需要用户注意时才显示状态条（生成中 / 出错 / 没有字幕）。
  el.status.hidden = !shouldShowStatus(payload);

  setText(el.videoTitle, payload.title || "尚未识别视频");
  el.videoTitle.title = payload.title || "";
  setText(el.videoMeta, buildMetaLine(payload));

  const options = payload.subtitleOptions || [];
  // 没有字幕轨时，"语言"里放的是想要的输出语言；已经生成出来后
  // 也继续放这几个选项，换一个就等于重新生成
  if (payload.canGenerate || payload.whisperTrack) {
    const current = String(payload.whisperOutput || "bilingual");
    el.subtitleSelect.innerHTML = OUTPUT_MODES.map(
      (item) =>
        `<option value="${item.value}"${item.value === current ? " selected" : ""}>${item.label}</option>`
    ).join("");
    el.subtitleSelect.disabled = Boolean(payload.generating);
  } else if (options.length === 0) {
    el.subtitleSelect.innerHTML = '<option value="">暂无字幕</option>';
    el.subtitleSelect.disabled = true;
  } else {
    el.subtitleSelect.innerHTML = options
      .map((item) => {
        const selected = item.selected ? "selected" : "";
        const aiTag = item.isAi ? " [AI]" : "";
        return `<option value="${escapeHtml(item.url)}" data-id="${escapeHtml(
          item.id || ""
        )}" data-lang="${escapeHtml(item.lang || "")}" ${selected}>${escapeHtml(
          `${item.lang || "unknown"}${aiTag}`
        )}</option>`;
      })
      .join("");
    el.subtitleSelect.disabled = false;
  }

  el.preview.value = payload.subtitlePreview || "";
  setText(el.previewCount, countPreviewLines(payload.subtitlePreview));
  applyGenerationState(payload);
}

function shouldShowStatus(payload) {
  if (!payload) {
    return true;
  }
  if (payload.generating || payload.canGenerate) {
    return true;
  }
  const statusText = String(payload.status || "");
  if (/失败|错误|无字幕|不支持|请刷新/.test(statusText)) {
    return true;
  }
  // 还没抓到任何内容时也别沉默
  return !hasUsableSubtitle(payload);
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
    el.progress.hidden = !generating;
    if (el.progressFill) {
      el.progressFill.style.width = `${progress}%`;
    }
  }

  if (el.whisperBtn) {
    const canGenerate = Boolean(payload?.canGenerate);
    el.whisperBtn.hidden = !canGenerate && !generating;
    el.whisperBtn.disabled = generating;
    el.whisperBtn.textContent = generating ? "生成中…" : "生成字幕（whisper）";
  }
}

function setText(node, text) {
  node.textContent = String(text || "");
}

function setStatus(text, isError = false) {
  const value = String(text || "");
  el.status.textContent = value;
  el.status.classList.toggle("is-error", Boolean(isError));
  el.status.classList.toggle(
    "is-ok",
    !isError && /完成|就绪|已生成|已复制|已下载|已写入|已保存/.test(value)
  );
}

function setMessage(text) {
  el.message.textContent = String(text || "");
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
