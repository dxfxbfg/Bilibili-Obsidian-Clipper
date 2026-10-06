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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    # 默认贪心解码：实测比 5 束搜索快约 30%，且未必更差——
    # 对内容里的自造词/专有名词，束搜索反而更容易"顺"成常见词。
    "beam_size": 1,
    "best_of": 1,
}

JOBS = {}
JOB_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()

TEMP_PREFIX = "boc-whisper-"
ACTIVE_WORKDIRS = set()   # 本进程创建、尚未清理的临时目录
STALE_TEMP_AGE = 600      # 启动清扫：超过这个秒数没被动过的残留目录才算废弃
JOB_RESULT_TTL = 1800     # 已完成任务的结果保留时长，超时释放内存
MAX_FINISHED_JOBS = 20    # 最多保留多少个已完成任务的结果


# ---------------------------------------------------------------- 清理

def sweep_stale_workdirs():
    """清掉上次崩溃/被杀留下的临时目录。只动自己的前缀，且跳过还热着的目录。"""
    root = tempfile.gettempdir()
    now = time.time()
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(root, name)
        if path in ACTIVE_WORKDIRS or not os.path.isdir(path):
            continue
        try:
            if now - os.path.getmtime(path) < STALE_TEMP_AGE:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    if removed:
        log(f"启动清扫：删除 {removed} 个残留临时目录")
    return removed


def drop_workdir(path):
    """删除单个任务的工作目录，并从活跃集合里摘掉。"""
    if not path:
        return
    ACTIVE_WORKDIRS.discard(path)
    if CONFIG["keep_temp"]:
        log(f"保留临时目录：{path}")
        return
    shutil.rmtree(path, ignore_errors=True)


def stop_job_proc(job, reason=""):
    """终止任务正在跑的 whisper 子进程，避免它变成孤儿继续占 CPU。"""
    proc = job.get("_proc") if isinstance(job, dict) else None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log(f"{job.get('id', '')[:8]} 已终止子进程{('：' + reason) if reason else ''}")
        except Exception as exc:  # noqa: BLE001
            log(f"终止子进程失败：{exc}")


RESULT_CACHE = {}         # 相同视频+相同参数直接复用，省一次完整转写
CACHE_TTL = 1800
CACHE_MAX = 5


def build_cache_key(options):
    model = find_model(options.get("model", ""))
    translate = bool(options.get("translate", CONFIG["translate_default"]))
    output = str(options.get("output") or ("bilingual" if translate else "original"))
    return "|".join([
        str(options.get("bvid") or ""),
        str(options.get("cid") or ""),
        str(options.get("language") or "en"),
        os.path.basename(model),
        "1" if translate else "0",
        output,
        str(options.get("beam_size") or CONFIG["beam_size"]),
    ])


def cache_lookup(options):
    key = build_cache_key(options)
    item = RESULT_CACHE.get(key)
    if not item:
        return None
    if time.time() - item["at"] > CACHE_TTL:
        RESULT_CACHE.pop(key, None)
        return None
    return item["cues"]


def cache_store(options, cues):
    if not cues:
        return
    RESULT_CACHE[build_cache_key(options)] = {"at": time.time(), "cues": cues}
    if len(RESULT_CACHE) > CACHE_MAX:
        for key, _ in sorted(RESULT_CACHE.items(), key=lambda kv: kv[1]["at"])[
            : len(RESULT_CACHE) - CACHE_MAX
        ]:
            RESULT_CACHE.pop(key, None)


def cache_evict_expired():
    now = time.time()
    for key, item in list(RESULT_CACHE.items()):
        if now - item["at"] > CACHE_TTL:
            RESULT_CACHE.pop(key, None)


def evict_finished_jobs():
    """释放已完成任务的结果，避免服务常驻后内存无限增长。"""
    now = time.time()
    finished = [
        (jid, job) for jid, job in JOBS.items()
        if job.get("status") in ("done", "error", "cancelled")
    ]
    for jid, job in finished:
        stamp = job.get("finished_at") or job.get("created_at") or now
        if now - stamp > JOB_RESULT_TTL:
            JOBS.pop(jid, None)

    still = [(jid, job) for jid, job in JOBS.items()
             if job.get("status") in ("done", "error", "cancelled")]
    if len(still) <= MAX_FINISHED_JOBS:
        return
    still.sort(key=lambda kv: kv[1].get("finished_at") or kv[1].get("created_at") or 0)
    for jid, _ in still[:len(still) - MAX_FINISHED_JOBS]:
        JOBS.pop(jid, None)


def cleanup_all():
    """退出前收尾：停子进程 + 删所有本进程的临时目录。"""
    for job in list(JOBS.values()):
        if job.get("status") == "running":
            job["_cancelled"] = True
        stop_job_proc(job, "服务退出")
    for path in list(ACTIVE_WORKDIRS):
        ACTIVE_WORKDIRS.discard(path)
        if not CONFIG["keep_temp"]:
            shutil.rmtree(path, ignore_errors=True)


def start_janitor():
    """后台清理线程：过期结果和残留目录不依赖请求触发。"""
    def loop():
        while True:
            time.sleep(60)
            try:
                evict_finished_jobs()
                cache_evict_expired()
                if not ACTIVE_WORKDIRS:
                    sweep_stale_workdirs()
            except Exception as exc:  # noqa: BLE001
                log(f"清理线程异常：{exc}")

    thread = threading.Thread(target=loop, daemon=True, name="boc-janitor")
    thread.start()
    return thread


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

def transcribe(wav, model, language, threads, on_stage, duration=0, on_proc=None,
               beam_size=None, best_of=None):
    out_prefix = os.path.join(os.path.dirname(wav), "asr")
    beam = int(beam_size if beam_size is not None else CONFIG["beam_size"])
    best = int(best_of if best_of is not None else CONFIG["best_of"])
    cmd = [CONFIG["whisper_bin"], "-m", model, "-f", wav, "-oj", "-of", out_prefix,
           "-t", str(threads), "-bs", str(beam), "-bo", str(best)]
    if language and language != "auto":
        cmd += ["-l", language]

    on_stage("transcribe", 30, "正在转写音频")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if on_proc:
        on_proc(proc)

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

TRANSLATE_MIN_LINES = 16
TRANSLATE_WORKERS = 4


def translate_lines(texts, target, on_stage):
    """整批字幕一次性并行翻译。翻译耗时主要卡在上游生成 token，
    分批并发比逐批串行快得多，长视频尤其明显。"""
    template = TRANSLATE_TEMPLATE.format(target=target)
    total = len(texts)
    if not total:
        return []

    # 均分而不是定长切：88 行按 40 切会得到 40/40/8，前两批拖住整体，
    # 均分成 22×4 才吃满并发。
    workers = max(1, min(TRANSLATE_WORKERS, -(-total // TRANSLATE_MIN_LINES)))
    per_chunk = -(-total // workers)
    chunks = []
    for start in range(0, total, per_chunk):
        chunk = texts[start:start + per_chunk]
        body = "\n".join(f"{start + i + 1}. {t}" for i, t in enumerate(chunk))
        chunks.append((start, body))

    results = {}
    lock = threading.Lock()
    progress = {"done": 0}

    def run(item):
        start, body = item
        content = translate_one(template + body)
        with lock:
            for line in content.split("\n"):
                m = re.match(r"\s*(\d+)[.、)]\s*(.+)", line.strip())
                if m:
                    results[int(m.group(1))] = m.group(2).strip()
            progress["done"] = min(total, progress["done"] + len(body.split("\n")))
            on_stage("translate", 85 + int(progress["done"] / total * 12),
                     f"正在翻译 {progress['done']}/{total}")

    with ThreadPoolExecutor(max_workers=min(TRANSLATE_WORKERS, len(chunks))) as pool:
        list(pool.map(run, chunks))

    return [results.get(i + 1, "") for i in range(total)]


def translate_one(prompt, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            data = http_post_json(CONFIG["translate_url"], {"text": prompt}, timeout=180)
            if "content" in data:
                return str(data["content"])
            last = RuntimeError(str(data.get("message") or "翻译服务返回异常"))
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < attempts - 1:
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"翻译失败：{last}")


# ---------------------------------------------------------------- 任务

def run_job(job_id, options):
    job = JOBS[job_id]
    workdir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    ACTIVE_WORKDIRS.add(workdir)

    def on_stage(stage, percent, text):
        job["stage"] = stage
        job["percent"] = percent
        job["message"] = text
        log(f"{job_id[:8]} {percent:3d}% {text}")

    def on_proc(proc):
        job["_proc"] = proc

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
            cues = transcribe(wav, model, language, threads, on_stage, duration, on_proc=on_proc,
                              beam_size=options.get("beam_size"), best_of=options.get("best_of"))
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
            cache_store(options, cues)
            log(f"{job_id[:8]} 完成，共 {len(cues)} 条")
    except Exception as exc:  # noqa: BLE001
        if job.get("_cancelled"):
            job["status"] = "cancelled"
            job["message"] = "任务已取消"
            log(f"{job_id[:8]} 已取消")
        else:
            job["status"] = "error"
            job["error"] = str(exc)
            job["message"] = str(exc)
            log(f"{job_id[:8]} 失败：{exc}")
        job["finished_at"] = time.time()
    finally:
        job.pop("_proc", None)
        job.pop("_cancelled", None)
        drop_workdir(workdir)


def create_job(options):
    if not options.get("bvid"):
        raise ValueError("缺少 bvid")
    evict_finished_jobs()

    # cid 缺失时先补上，缓存键才稳定（补不上就让任务自己去报错）
    if not options.get("cid"):
        try:
            options["cid"] = resolve_cid(options["bvid"], "")
        except Exception:  # noqa: BLE001
            pass

    job_id = uuid.uuid4().hex
    cached = cache_lookup(options)
    if cached is not None:
        JOBS[job_id] = {
            "id": job_id,
            "status": "done",
            "stage": "cached",
            "percent": 100,
            "message": f"命中缓存，共 {len(cached)} 条",
            "cues": cached,
            "cached": True,
            "created_at": time.time(),
            "finished_at": time.time(),
        }
        log(f"{job_id[:8]} 命中缓存")
        return job_id

    JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "stage": "queued",
        "percent": 0,
        "message": "任务已创建",
        "created_at": time.time(),
    }
    thread = threading.Thread(target=run_job, args=(job_id, options),
                              daemon=True, name=f"boc-job-{job_id[:8]}")
    thread.start()
    return job_id


def cancel_job(job_id):
    """取消任务：正在跑的停子进程，已完成的直接释放结果。"""
    job = JOBS.get(job_id)
    if not job:
        return False
    if job.get("status") == "running":
        job["_cancelled"] = True
        stop_job_proc(job, "任务取消")
        job["status"] = "cancelled"
        job["message"] = "任务已取消"
        job["finished_at"] = time.time()
        return True
    JOBS.pop(job_id, None)
    return True


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
                "running_jobs": sum(1 for j in JOBS.values() if j.get("status") == "running"),
                "temp_dirs": len(ACTIVE_WORKDIRS),
                "temp_dir": tempfile.gettempdir(),
                "keep_temp": CONFIG["keep_temp"],
                "cached_results": len(RESULT_CACHE),
            })
            return
        if path.startswith("/jobs/"):
            job_id = path.split("/", 2)[2]
            job = JOBS.get(job_id)
            if not job:
                self._send(404, {"ok": False, "error": "任务不存在"})
                return
            # 内部字段（子进程句柄等）不外传
            payload = {k: v for k, v in job.items() if not k.startswith("_")}
            payload["ok"] = job["status"] not in ("error", "cancelled")
            self._send(200, payload)
            evict_finished_jobs()
            return
        if path in ("/", "/index.html"):
            self._send(200, {
                "ok": True,
                "service": "BOC 字幕助手",
                "version": VERSION,
                "endpoints": ["GET /health", "POST /jobs", "GET /jobs/<id>",
                              "DELETE /jobs/<id>", "POST /cleanup", "POST /transcribe"],
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

        if path == "/cleanup":
            evict_finished_jobs()
            cache_evict_expired()
            removed = 0 if ACTIVE_WORKDIRS else sweep_stale_workdirs()
            self._send(200, {"ok": True, "swept_temp_dirs": removed,
                             "jobs_left": len(JOBS),
                             "cached_results": len(RESULT_CACHE),
                             "running_jobs": sum(1 for j in JOBS.values()
                                                 if j.get("status") == "running")})
            return

        if path == "/transcribe":
            job_id = create_job(options)
            job = JOBS[job_id]
            while job["status"] == "running":
                time.sleep(0.5)
            if job["status"] != "done":
                self._send(500, {"ok": False, "error": job.get("error") or job.get("message")})
                return
            self._send(200, {"ok": True, "job_id": job_id, "cues": job["cues"]})
            return

        self._send(404, {"ok": False, "error": "未知路径"})

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/jobs/"):
            job_id = path.split("/", 2)[2]
            if cancel_job(job_id):
                self._send(200, {"ok": True})
            else:
                self._send(404, {"ok": False, "error": "任务不存在"})
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
    parser.add_argument("--beam-size", type=int, default=CONFIG["beam_size"],
                        help="whisper 束搜索宽度，1 为贪心解码（默认，最快）")
    parser.add_argument("--best-of", type=int, default=CONFIG["best_of"],
                        help="候选采样数，1 表示不做额外采样（默认）")
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
        "beam_size": args.beam_size,
        "best_of": args.best_of,
    })

    model = find_model("")
    log(f"BOC 字幕助手 v{VERSION}")
    log(f"  whisper : {CONFIG['whisper_bin']} ({'ok' if which_ok(CONFIG['whisper_bin']) else '未找到'})")
    log(f"  ffmpeg  : {CONFIG['ffmpeg_bin']} ({'ok' if which_ok(CONFIG['ffmpeg_bin']) else '未找到'})")
    log(f"  模型    : {model or '未找到，请用 --model 指定'}")
    log(f"  翻译    : {'默认开启' if CONFIG['translate_default'] else '默认关闭'}")
    log(f"  解码    : beam={CONFIG['beam_size']} best-of={CONFIG['best_of']}"
        f"{'（贪心，最快）' if CONFIG['beam_size'] == 1 else ''}")
    log(f"  临时目录: {tempfile.gettempdir()}"
        f"{'（--keep-temp 已开启，不会自动删）' if CONFIG['keep_temp'] else ''}")

    if not which_ok(CONFIG["whisper_bin"]):
        log("提示：brew install whisper-cpp")
    if not model:
        log("提示：curl -L -o ~/.cache/whisper.cpp/ggml-base.en.bin "
            "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin")

    # 上次被杀/崩溃留下的目录，启动先扫一遍
    sweep_stale_workdirs()
    start_janitor()

    def on_signal(signum, _frame):
        log(f"收到信号 {signum}，正在清理...")
        cleanup_all()
        # 交给主循环退出
        threading.Thread(target=server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    log(f"监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_all()
        server.server_close()
        log("已退出，临时文件已清理")
    return 0


if __name__ == "__main__":
    sys.exit(main())
