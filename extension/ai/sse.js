// Shared by classic workers and the ESM client; no browser dependencies.
(function (root) {
  "use strict";
  async function* parseOpenAISSE(response) {
    if (!response?.body) throw new Error("AI 响应没有可读取的数据流");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let dataLines = [];
    let exhausted = false;
    function parseEvent() {
      const data = dataLines.join("\n").trim();
      dataLines = [];
      if (!data) return {};
      if (data === "[DONE]") return { done: true };
      let payload;
      try { payload = JSON.parse(data); } catch { return {}; }
      if (payload.error) throw new Error(payload.error.message || String(payload.error));
      const content = payload?.choices?.[0]?.delta?.content;
      return { token: typeof content === "string" ? content : "" };
    }
    function consumeLine(line) {
      if (line === "") return parseEvent();
      if (line.startsWith("data:")) {
        const value = line.slice(5);
        dataLines.push(value.startsWith(" ") ? value.slice(1) : value);
      }
      return {};
    }
    try {
      while (true) {
        const { value, done } = await reader.read();
        exhausted = done;
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let start = 0;
        for (let i = 0; i < buffer.length; i++) {
          const char = buffer[i];
          if (char !== "\r" && char !== "\n") continue;
          // The LF belonging to this CR may arrive in the next chunk.
          if (char === "\r" && i === buffer.length - 1 && !done) break;
          const event = consumeLine(buffer.slice(start, i));
          if (char === "\r" && buffer[i + 1] === "\n") i++;
          start = i + 1;
          if (event.done) return;
          if (event.token) yield event.token;
        }
        buffer = buffer.slice(start);
        if (done) {
          // Compatible providers may omit the final event terminator.
          if (buffer) consumeLine(buffer);
          const event = parseEvent();
          if (event.token) yield event.token;
          return;
        }
      }
    } finally {
      try {
        if (!exhausted) await reader.cancel();
      } catch {
        // An abort/disconnect may already have closed the stream.
      } finally {
        reader.releaseLock();
      }
    }
  }
  root.BocAIStream = Object.freeze({ parseOpenAISSE });
})(globalThis);
