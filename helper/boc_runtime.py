"""Provision a private, checksummed whisper.cpp runtime without pip/admin rights."""
import ctypes
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

try:
    from . import boc_gpu
except ImportError:
    import boc_gpu

DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "BOC" / "whisper"
PROVISION_LOCK = threading.Lock()
MODELS = {
    "tiny": ("ggml-tiny-q5_1.bin", 32152673, "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7"),
    "base": ("ggml-base-q5_1.bin", 59707625, "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898"),
    "small": ("ggml-small-q5_1.bin", 190085487, "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"),
}
WHISPER_URL = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip"
WHISPER_SHA = "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip"
FFMPEG_SHA = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
# Upstream currently ships no Windows Vulkan executable. This pinned community
# build compiles unmodified upstream v1.9.5 in its public GitHub Actions workflow.
VULKAN_URL = "https://github.com/fahrim/whisper-vulkan-win/releases/download/engine-vulkan-b5454-static/whisper-vulkan-win-x64-v1.9.5.zip"
VULKAN_SHA = "105b03c51d38583b333c7f116c5c12e8ce72c443f3a614204425f617e9ecbdde"


def hardware_info():
    total = available = 0
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                (name, ctypes.c_uint64) for name in (
                    "total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            total, available = status.total, status.available
    else:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            available = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            if platform.system() == "Darwin":
                try:
                    total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=3))
                    available = total // 2  # conservative estimate, not measured free RAM
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
    gpus = boc_gpu.devices()
    return {"system": platform.system(), "arch": platform.machine().lower(),
            "cpu_threads": os.cpu_count() or 1, "memory_gb": round(total / 2**30, 1),
            "available_gb": round(available / 2**30, 1), "gpus": gpus,
            "backend": "vulkan" if gpus else "cpu"}


def choose_model(hardware):
    cores = hardware["cpu_threads"]
    total, available = hardware["memory_gb"], hardware["available_gb"]
    gpus = hardware.get("gpus", [])
    if gpus:
        # Do not equate integrated GPU shared memory with dedicated VRAM.
        capacity = max(min(gpu["memory_gb"], available / 2) if gpu.get("integrated") else gpu["memory_gb"] for gpu in gpus)
        if capacity >= 2 and available >= 3:
            return "small"
        if capacity >= 1 and available >= 2:
            return "base"
        return "tiny"
    if cores >= 8 and total >= 15 and available >= 6:
        return "small"
    if cores >= 4 and total >= 7 and available >= 2:
        return "base"
    return "tiny"


def plan(hardware=None):
    hardware = hardware or hardware_info()
    model = choose_model(hardware)
    name, size, _ = MODELS[model]
    return {"hardware": hardware, "model_name": name, "model_bytes": size,
            "threads": min(8, max(1, hardware["cpu_threads"] - 1)), "cache_dir": str(DATA_DIR)}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, destination, expected_sha, progress):
    """Never expose an incomplete or unverified file as an installed asset."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256(destination) == expected_sha:
        return destination
    part = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "BOC-Helper/1.1"})
        with urllib.request.urlopen(request, timeout=60) as source, part.open("wb") as out:
            total = int(source.headers.get("Content-Length") or 0)
            if total and shutil.disk_usage(destination.parent).free < total + 256 * 2**20:
                raise RuntimeError("磁盘剩余空间不足，请清理后重试")
            received = 0
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(256 * 1024), b""):
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                progress(received, total)
        if digest.hexdigest() != expected_sha:
            raise RuntimeError("下载文件 SHA-256 校验失败，请重试")
        part.replace(destination)
    finally:
        part.unlink(missing_ok=True)
    return destination


def extract_zip(archive, destination):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = entry.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or ":" in name or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("压缩包包含不安全的路径")
        source.extractall(destination)


def find_binary(root, name):
    return next((str(p) for p in Path(root).rglob(name) if p.is_file()), "") if Path(root).exists() else ""


def resolve_binary(configured, name, folder):
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    # Respect an explicit path instead of silently replacing it.
    if configured != name:
        raise RuntimeError(f"指定的程序不存在：{configured}")
    return find_binary(DATA_DIR / folder, name + (".exe" if os.name == "nt" else ""))


def provision_binary(configured, name, folder, url, checksum, progress, hardware):
    existing = resolve_binary(configured, name, folder)
    if existing:
        return existing
    if hardware["system"] != "Windows" or hardware["arch"] not in ("amd64", "x86_64"):
        raise RuntimeError("此平台需先安装 whisper-cli 和 ffmpeg（macOS：brew install whisper-cpp ffmpeg），模型仍可自动下载")
    archive = DATA_DIR / "downloads" / (folder + ".zip")
    download(url, archive, checksum, progress)
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        extract_zip(archive, tmp)
        if not find_binary(tmp, name + ".exe"):
            raise RuntimeError(f"下载包中缺少 {name}.exe")
        destination = DATA_DIR / folder
        if destination.exists():
            # Only a known managed runtime directory is replaced; never a user path.
            shutil.rmtree(destination)
        shutil.copytree(tmp, destination)
    return find_binary(destination, name + ".exe")


def ensure_runtime(config, model_override="", report=lambda stage, pct, text: None):
    with PROVISION_LOCK:
        selected = plan()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        hardware = selected["hardware"]
        def progress_for(label):
            def update(received, total):
                amount = f"{received / 2**20:.1f} MB"
                if total:
                    amount += f" / {total / 2**20:.1f} MB"
                report("setup", 0, f"正在下载 {label}：{amount}")
            return update
        report("setup", 0, "正在检测电脑并准备转写环境")
        whisper = provision_binary(config["whisper_bin"], "whisper-cli", "whisper-b5454", WHISPER_URL, WHISPER_SHA, progress_for("whisper.cpp"), hardware)
        ffmpeg = provision_binary(config["ffmpeg_bin"], "ffmpeg", "ffmpeg-8.1.2", FFMPEG_URL, FFMPEG_SHA, progress_for("FFmpeg"), hardware)
        server = ""
        fallback_reason = ""
        if hardware.get("gpus"):
            try:
                server = provision_binary("whisper-server", "whisper-server", "vulkan-b5454",
                                          VULKAN_URL, VULKAN_SHA, progress_for("GPU 运行时"), hardware)
            except Exception as exc:
                fallback_reason = f"GPU 运行时不可用，使用 CPU：{exc}"
                report("setup", 0, fallback_reason)
        explicit = model_override or config.get("model", "")
        if explicit:
            model = Path(explicit).expanduser()
            if not model.is_file():
                raise RuntimeError(f"指定的模型不存在：{model}")
        else:
            key = choose_model(hardware)
            name, _, checksum = MODELS[key]
            model = DATA_DIR / "models" / name
            download("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/" + name, model, checksum, progress_for(name))
        # Validate that binaries and their DLL dependencies can actually start.
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        for binary, argument in ((whisper, "--help"), (ffmpeg, "-version")):
            result = subprocess.run([binary, argument], capture_output=True, timeout=20, **flags)
            if result.returncode:
                raise RuntimeError(f"程序启动失败：{Path(binary).name}（{result.returncode}），请检查系统运行库")
        config.update(whisper_bin=whisper, ffmpeg_bin=ffmpeg)
        if server:
            try:
                result = subprocess.run([server, "--help"], capture_output=True, timeout=20, **flags)
                if result.returncode:
                    raise RuntimeError(f"GPU 运行时启动失败，退出码 {result.returncode}")
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                fallback_reason, server = str(exc), ""
        backend = "vulkan" if server else "cpu"
        # Keep automatic selection automatic; do not overwrite a user's model path.
        report("ready", 100, f"环境就绪：{model.name}，优先使用 {backend.upper()}")
        return {**selected, "model_name": model.name, "model": str(model), "whisper_bin": whisper,
                "ffmpeg_bin": ffmpeg, "gpu_server": server, "backend": backend, "fallback_reason": fallback_reason}
