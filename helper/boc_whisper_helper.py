#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BOC 字幕助手服务

给 Bilibili Obsidian Clipper 扩展用：当 B 站视频没有可抓取的字幕轨时，
由本服务去 playurl 取音频、用 whisper.cpp 转写、可选翻译成中文。

只依赖 Python 3 标准库 + 外部命令 ffmpeg / whisper-cli。

启动：
    python3 boc_whisper_helper.py
    python3 boc_whisper_helper.py --port 8788 --model ~/.cache/whisper.cpp/ggml-base.en.bin

接口：
    GET  /health          服务与依赖状态
    POST /jobs            新建转写任务（异步），返回 {job_id}
    GET  /jobs/<id>       查询任务状态与结果
    POST /transcribe      同步转写（阻塞到完成）
    GET  /                浏览器里看状态

请求体字段（都是可选，除 bvid/cid）：
    bvid, cid, aid        视频标识
    language              "en"（默认）| "auto" | 其他语言码；非 en 需要多语言模型
    model                 指定模型路径，覆盖服务默认
    translate             true/false，是否翻译
    translate_target      目标语言，默认 "zh"
    output                "bilingual"（默认）| "original" | "translated"
    threads               线程数，默认 CPU 核数
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

VERSION = "1.0.0"

BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

# 部分免费服务会拒绝非浏览器 UA（如 Python-urllib），统一带上
WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"

TRANSLATE_URL = "https://api1.pdf2zh-next.com/chatproxy"
TRANSLATE_TEMPLATE = (
    "You are a professional,authentic machine translation engine.\n\n"
    ";; Treat next line as plain text input and translate it into {target}, output translation ONLY. "
    "If translation is unnecessary (e.g. proper nouns, codes, {{{{1}}}}, etc. ), return the original text. "
    "NO explanations. NO notes. Input:\n\n"
)

MODEL_CANDIDATES = [
    "~/.cache/whisper.cpp/ggml-base.en.bin",
    "~/.cache/whisper.cpp/ggml-small.en.bin",
    "~/.cache/whisper.cpp/ggml-large-v3-turbo.bin",
    "~/Library/Application Support/whisper.cpp/ggml-base.en.bin",
    "/opt/homebrew/share/whisper-cpp/models/ggml-base.en.bin",
    "/usr/local/share/whisper-cpp/models/ggml-base.en.bin",
]

CONFIG = {
    "whisper_bin": "whisper-cli",
    "ffmpeg_bin": "ffmpeg",
    "model": "",
    "translate_default": False,
    "translate_url": TRANSLATE_URL,
    "threads": 0,
    "keep_temp": False,
    "fix_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "text_fixes.json"),
}

JOBS = {}
JOB_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


# ---------------------------------------------------------------- 工具

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def expand(path):
    return os.path.expanduser(str(path or ""))


def find_model(explicit=""):
    if explicit:
        p = expand(explicit)
        return p if os.path.isfile(p) else ""
    if CONFIG["model"]:
        p = expand(CONFIG["model"])
        if os.path.isfile(p):
            return p
    for cand in MODEL_CANDIDATES:
        p = expand(cand)
        if os.path.isfile(p):
            return p
    return ""


def which_ok(binary):
    if os.path.isabs(binary):
        return os.path.isfile(binary)
    return bool(shutil.which(binary))


def http_get_json(url, timeout=30):
    import urllib.request

    req = urllib.request.Request(url, headers=BILI_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=180, extra_headers=None):
    import urllib.request

    headers = {"Content-Type": "application/json", "User-Agent": WEB_UA, **(extra_headers or {})}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url, dest):
    import urllib.request

    req = urllib.request.Request(url, headers=BILI_HEADERS)
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh, length=1024 * 256)
    return os.path.getsize(dest)


# ---------------------------------------------------------------- 取音频

def resolve_cid(bvid, cid):
    if cid:
        return str(cid)
    data = http_get_json(f"{VIEW_API}?bvid={bvid}")
    if data.get("code") != 0:
        raise RuntimeError(f"取视频信息失败：{data.get('message')}")
    return str(data["data"]["cid"])


def fetch_audio_url(bvid, cid, aid=""):
    query = f"?bvid={bvid}&cid={cid}&fnval=16&fnver=0&fourk=1"
    if aid:
        query += f"&aid={aid}"
    data = http_get_json(PLAYURL_API + query)
    if data.get("code") != 0:
        raise RuntimeError(f"取音频地址失败：{data.get('message')}")
    dash = (data.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        raise RuntimeError("该视频没有返回音频轨（可能是付费或受限内容）")
    best = sorted(audios, key=lambda item: item.get("bandwidth", 0), reverse=True)[0]
    url = best.get("baseUrl") or best.get("base_url")
    if not url:
        raise RuntimeError("音频地址为空")
    return url, dash.get("duration", 0)


def prepare_wav(workdir, bvid, cid, aid, on_stage):
    on_stage("fetch-audio", 5, "正在获取音频地址")
    url, duration = fetch_audio_url(bvid, cid, aid)
    raw = os.path.join(workdir, "audio.m4s")
    on_stage("download-audio", 12, "正在下载音频")
    size = download_file(url, raw)
    log(f"音频已下载 {size/1048576:.1f}MB，视频时长 {duration:.1f}s")

    wav = os.path.join(workdir, "audio.wav")
    on_stage("decode-audio", 22, "正在转码为 16kHz 单声道")
    cmd = [CONFIG["ffmpeg_bin"], "-v", "error", "-i", raw, "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-y", wav]
    subprocess.run(cmd, check=True, capture_output=True)
    if not duration:
        duration = probe_duration(wav)
    return wav, float(duration or 0)


def probe_duration(path):
    try:
        out = subprocess.run(
            [CONFIG["ffmpeg_bin"].replace("ffmpeg", "ffprobe"), "-v", "error",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------- 转写

def transcribe(wav, model, language, threads, on_stage, duration=0):
    out_prefix = os.path.join(os.path.dirname(wav), "asr")
    cmd = [CONFIG["whisper_bin"], "-m", model, "-f", wav, "-oj", "-of", out_prefix,
           "-t", str(threads)]
    if language and language != "auto":
        cmd += ["-l", language]

    on_stage("transcribe", 30, "正在转写音频")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # 这个版本的 whisper-cli 没有进度回调，用分段行的时间戳推算进度
    total = 0.0
    seg_time = re.compile(r"^\[(\d+):(\d+):(\d+)\.(\d+)\s*-->")
    for line in proc.stderr:
        line = line.strip()
        m = seg_time.match(line)
        if m:
            h, mi, s, _ = (int(x) for x in m.groups())
            done = h * 3600 + mi * 60 + s
            if duration > 0:
                pct = min(99, int(done / duration * 100))
                on_stage("transcribe", 30 + int(pct * 0.55), f"正在转写音频 {pct}%")
            continue
        m = re.search(r"total time\s*=\s*([\d.]+)\s*ms", line)
        if m:
            total = float(m.group(1)) / 1000.0
            continue
        if line and "load_backend" not in line and "metal" not in line.lower():
            log(f"whisper: {line}")
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"whisper 转写失败，退出码 {proc.returncode}")

    json_path = out_prefix + ".json"
    if not os.path.isfile(json_path):
        raise RuntimeError("whisper 没有产出 JSON 结果")
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)

    cues = []
    for item in data.get("transcription") or []:
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            continue
        offsets = item.get("offsets") or {}
        frm = offsets.get("from")
        to = offsets.get("to")
        if frm is None or to is None:
            ts = item.get("timestamps") or {}
            frm = srt_time_to_ms(ts.get("from"))
            to = srt_time_to_ms(ts.get("to"))
        cues.append({"from": round(float(frm) / 1000.0, 3), "to": round(float(to) / 1000.0, 3),
                     "text": text})
    if not cues:
        raise RuntimeError("转写结果为空")
    log(f"转写完成，{len(cues)} 段，用时 {total:.1f}s")
    return cues


def srt_time_to_ms(value):
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", str(value or ""))
    if not m:
        return 0
    h, mi, s, ms = (int(x) for x in m.groups())
    return ((h * 60 + mi) * 60 + s) * 1000 + ms


# ---------------------------------------------------------------- 合并成句

SENTENCE_END = re.compile(r"[.?!][\"'”’)]?$")


def merge_sentences(cues):
    merged = []
    for cue in cues:
        if merged and not SENTENCE_END.search(merged[-1]["text"]):
            merged[-1]["text"] = (merged[-1]["text"] + " " + cue["text"]).strip()
            merged[-1]["to"] = cue["to"]
        else:
            merged.append(dict(cue))
    return merged


def apply_text_fixes(cues):
    """按 text_fixes.json 纠正 whisper 的听写偏差（术语、自造词等）。"""
    path = CONFIG["fix_file"]
    if not path or not os.path.isfile(path):
        return cues
    try:
        with open(path, encoding="utf-8") as fh:
            rules = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log(f"纠正表读取失败，已跳过：{exc}")
        return cues
    pairs = []
    for key, value in (rules.get("replacements") or {}).items():
        if key and value is not None:
            pairs.append((re.compile(key), value))
    for cue in cues:
        for pattern, value in pairs:
            cue["text"] = pattern.sub(value, cue["text"])
    return cues


# ---------------------------------------------------------------- 翻译

def translate_lines(texts, target, on_stage):
    template = TRANSLATE_TEMPLATE.format(target=target)
    results = {}
    chunk_size = 40
    total = len(texts)
    for start in range(0, total, chunk_size):
        chunk = texts[start:start + chunk_size]
        body = "\n".join(f"{start + i + 1}. {t}" for i, t in enumerate(chunk))
        payload = {"text": template + body}
        for attempt in range(3):
            try:
                data = http_post_json(CONFIG["translate_url"], payload, timeout=180)
                if "content" in data:
                    break
                raise RuntimeError(str(data.get("message") or "翻译服务返回异常"))
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError(f"翻译失败：{exc}") from exc
                time.sleep(2 + attempt * 3)
        for line in str(data["content"]).split("\n"):
            m = re.match(r"\s*(\d+)[.、)]\s*(.+)", line.strip())
            if m:
                results[int(m.group(1))] = m.group(2).strip()
        done = min(start + chunk_size, total)
        on_stage("translate", 85 + int(done / total * 12), f"正在翻译 {done}/{total}")
    return [results.get(i + 1, "") for i in range(total)]


# ---------------------------------------------------------------- 任务

def run_job(job_id, options):
    job = JOBS[job_id]
    workdir = tempfile.mkdtemp(prefix="boc-whisper-")

    def on_stage(stage, percent, text):
        job["stage"] = stage
        job["percent"] = percent
        job["message"] = text
        log(f"{job_id[:8]} {percent:3d}% {text}")

    try:
        with RUN_LOCK:
            bvid = options["bvid"]
            cid = resolve_cid(bvid, options.get("cid", ""))
            model = find_model(options.get("model", ""))
            if not model:
                raise RuntimeError(
                    "找不到 whisper 模型。用 --model 指定，或放到 ~/.cache/whisper.cpp/ggml-base.en.bin。"
                    "下载：curl -L -o ~/.cache/whisper.cpp/ggml-base.en.bin "
                    "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
                )
            language = str(options.get("language") or "en")
            if language != "en" and model.endswith(".en.bin"):
                raise RuntimeError(
                    f"模型 {os.path.basename(model)} 只支持英文，识别 {language} 请换多语言模型"
                    "（如 ggml-base.bin / ggml-small.bin）"
                )
            threads = int(options.get("threads") or CONFIG["threads"] or (os.cpu_count() or 4))

            job["cid"] = cid
            job["model"] = os.path.basename(model)
            wav, duration = prepare_wav(workdir, bvid, cid, options.get("aid", ""), on_stage)
            cues = transcribe(wav, model, language, threads, on_stage, duration)
            cues = apply_text_fixes(merge_sentences(cues))

            want_translate = bool(options.get("translate", CONFIG["translate_default"]))
            output = str(options.get("output") or ("bilingual" if want_translate else "original"))
            target = str(options.get("translate_target") or "zh")

            if want_translate or output == "translated":
                on_stage("translate", 85, "正在翻译字幕")
                zh = translate_lines([c["text"] for c in cues], target, on_stage)
            else:
                zh = [""] * len(cues)

            for cue, translated in zip(cues, zh):
                if output == "translated":
                    cue["content"] = translated or cue["text"]
                elif output == "bilingual" and translated:
                    cue["content"] = f"{translated}\n{cue['text']}"
                else:
                    cue["content"] = cue["text"]

            job["cues"] = cues
            job["status"] = "done"
            job["percent"] = 100
            job["message"] = f"完成，共 {len(cues)} 条"
            job["finished_at"] = time.time()
            log(f"{job_id[:8]} 完成，共 {len(cues)} 条")
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)
        job["message"] = str(exc)
        log(f"{job_id[:8]} 失败：{exc}")
    finally:
        if not CONFIG["keep_temp"]:
            shutil.rmtree(workdir, ignore_errors=True)
        job.pop("_options", None)


def create_job(options):
    if not options.get("bvid"):
        raise ValueError("缺少 bvid")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "stage": "queued",
        "percent": 0,
        "message": "任务已创建",
        "created_at": time.time(),
    }
    thread = threading.Thread(target=run_job, args=(job_id, options), daemon=True)
    thread.start()
    return job_id


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"BOCWhisperHelper/{VERSION}"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, {})

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            model = find_model("")
            self._send(200, {
                "ok": True,
                "version": VERSION,
                "whisper": which_ok(CONFIG["whisper_bin"]),
                "whisper_bin": CONFIG["whisper_bin"],
                "ffmpeg": which_ok(CONFIG["ffmpeg_bin"]),
                "model": model,
                "model_found": bool(model),
                "translate_default": CONFIG["translate_default"],
                "jobs": len(JOBS),
            })
            return
        if path.startswith("/jobs/"):
            job_id = path.split("/", 2)[2]
            job = JOBS.get(job_id)
            if not job:
                self._send(404, {"ok": False, "error": "任务不存在"})
                return
            payload = {k: v for k, v in job.items() if k != "options"}
            payload["ok"] = job["status"] != "error"
            self._send(200, payload)
            return
        if path in ("/", "/index.html"):
            self._send(200, {
                "ok": True,
                "service": "BOC 字幕助手",
                "version": VERSION,
                "endpoints": ["GET /health", "POST /jobs", "GET /jobs/<id>", "POST /transcribe"],
            })
            return
        self._send(404, {"ok": False, "error": "未知路径"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            options = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"ok": False, "error": f"请求体不是合法 JSON：{exc}"})
            return

        if path == "/jobs":
            try:
                job_id = create_job(options)
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"ok": False, "error": str(exc)})
                return
            self._send(200, {"ok": True, "job_id": job_id})
            return

        if path == "/transcribe":
            job_id = create_job(options)
            job = JOBS[job_id]
            while job["status"] == "running":
                time.sleep(0.5)
            if job["status"] == "error":
                self._send(500, {"ok": False, "error": job.get("error")})
                return
            self._send(200, {"ok": True, "job_id": job_id, "cues": job["cues"]})
            return

        self._send(404, {"ok": False, "error": "未知路径"})

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/jobs/"):
            job_id = path.split("/", 2)[2]
            JOBS.pop(job_id, None)
            self._send(200, {"ok": True})
            return
        self._send(404, {"ok": False, "error": "未知路径"})


def main():
    parser = argparse.ArgumentParser(description="BOC 字幕助手服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--model", default="", help="whisper 模型路径，默认自动探测")
    parser.add_argument("--whisper-bin", default="whisper-cli")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--threads", type=int, default=0, help="whisper 线程数，0 为自动")
    parser.add_argument("--translate-url", default=TRANSLATE_URL)
    parser.add_argument("--translate", action="store_true", help="默认开启翻译")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时文件（调试用）")
    parser.add_argument("--fix-file", default=CONFIG["fix_file"],
                        help="术语纠正表 JSON，默认取脚本同目录的 text_fixes.json")
    args = parser.parse_args()

    CONFIG.update({
        "whisper_bin": args.whisper_bin,
        "ffmpeg_bin": args.ffmpeg_bin,
        "model": args.model,
        "threads": args.threads,
        "translate_default": args.translate,
        "translate_url": args.translate_url,
        "keep_temp": args.keep_temp,
        "fix_file": args.fix_file,
    })

    model = find_model("")
    log(f"BOC 字幕助手 v{VERSION}")
    log(f"  whisper : {CONFIG['whisper_bin']} ({'ok' if which_ok(CONFIG['whisper_bin']) else '未找到'})")
    log(f"  ffmpeg  : {CONFIG['ffmpeg_bin']} ({'ok' if which_ok(CONFIG['ffmpeg_bin']) else '未找到'})")
    log(f"  模型    : {model or '未找到，请用 --model 指定'}")
    log(f"  翻译    : {'默认开启' if CONFIG['translate_default'] else '默认关闭'}")

    if not which_ok(CONFIG["whisper_bin"]):
        log("提示：brew install whisper-cpp")
    if not model:
        log("提示：curl -L -o ~/.cache/whisper.cpp/ggml-base.en.bin "
            "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("退出")
        server.server_close()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
