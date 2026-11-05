// Shared background controller. Downloads happen in the helper, never in the worker.
(function (root) {
  "use strict";
  async function health(base) {
    const response = await fetch(`${base}/health`, { cache: "no-store", signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error(`字幕助手返回 HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || (!payload.service && !payload.version)) throw new Error("该地址不是字幕助手");
    return payload;
  }
  function isManagedAddress(base) {
    try {
      const url = new URL(base);
      return url.protocol === "http:" && ["127.0.0.1", "localhost"].includes(url.hostname) &&
        url.port === "8788" && url.pathname === "/" && !url.username && !url.password && !url.search && !url.hash;
    } catch { return false; }
  }
  function wake() {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendNativeMessage("com.boc.desk", { cmd: "start", service: "whisper" }, (response) => {
        const error = chrome.runtime.lastError;
        if (error) {
          reject(new Error("尚未安装本地启动器。请在设置页复制安装命令，完成一次安装后重试。"));
        } else if (!response?.ok || !response.running) {
          reject(new Error(response?.error || "字幕助手启动失败，请更新本地启动器"));
        } else resolve(response);
      });
    });
  }
  async function ensure(base) {
    base = String(base || "").trim().replace(/\/+$/, "");
    try { return await health(base); } catch (error) {
      if (!isManagedAddress(base)) throw new Error(`无法连接指定的字幕助手：${error.message}`);
    }
    await wake();
    return health(base);
  }
  async function setup(base, { start = false, model = "" } = {}) {
    const current = await ensure(base);
    if (!current.auto_setup) throw new Error("当前字幕助手版本不支持自动配置，请更新 helper 目录并重启助手");
    const response = await fetch(`${base.replace(/\/+$/, "")}/setup`, {
      method: start ? "POST" : "GET", cache: "no-store", signal: AbortSignal.timeout(5000),
      ...(start ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }) } : {})
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `配置失败：HTTP ${response.status}`);
    return payload;
  }
  root.BocWhisperHelper = Object.freeze({ ensure, setup, isManagedAddress });
})(globalThis);
