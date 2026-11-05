"""Vulkan device discovery and per-job whisper-server GPU inference."""
import ctypes as c
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid


def devices():
    """Query the installed Vulkan driver, not adapter names or nvidia-smi."""
    if os.name != "nt":
        return []
    instance = c.c_void_p()
    vk = None
    try:
        vk = c.WinDLL("vulkan-1.dll")
        class Application(c.Structure):
            _fields_ = [("type", c.c_uint32), ("next", c.c_void_p), ("name", c.c_char_p),
                        ("version", c.c_uint32), ("engine", c.c_char_p), ("engine_version", c.c_uint32), ("api", c.c_uint32)]
        class Instance(c.Structure):
            _fields_ = [("type", c.c_uint32), ("next", c.c_void_p), ("flags", c.c_uint32),
                        ("app", c.POINTER(Application)), ("layers", c.c_uint32), ("layer_names", c.c_void_p),
                        ("extensions", c.c_uint32), ("extension_names", c.c_void_p)]
        class MemoryType(c.Structure):
            _fields_ = [("flags", c.c_uint32), ("heap", c.c_uint32)]
        class Heap(c.Structure):
            _fields_ = [("size", c.c_uint64), ("flags", c.c_uint32)]
        class Memory(c.Structure):
            _fields_ = [("types_count", c.c_uint32), ("types", MemoryType * 32),
                        ("heaps_count", c.c_uint32), ("heaps", Heap * 16)]
        vk.vkCreateInstance.argtypes = [c.POINTER(Instance), c.c_void_p, c.POINTER(c.c_void_p)]
        vk.vkEnumeratePhysicalDevices.argtypes = [c.c_void_p, c.POINTER(c.c_uint32), c.c_void_p]
        vk.vkGetPhysicalDeviceProperties.argtypes = [c.c_void_p, c.c_void_p]
        vk.vkGetPhysicalDeviceMemoryProperties.argtypes = [c.c_void_p, c.POINTER(Memory)]
        vk.vkDestroyInstance.argtypes = [c.c_void_p, c.c_void_p]
        app = Application(type=0, name=b"BOC", api=(1 << 22) | (1 << 12))
        info = Instance(type=1, app=c.pointer(app))
        if vk.vkCreateInstance(c.byref(info), None, c.byref(instance)) != 0:
            return []
        count = c.c_uint32()
        if vk.vkEnumeratePhysicalDevices(instance, c.byref(count), None) != 0 or count.value > 64:
            return []
        handles = (c.c_void_p * count.value)()
        if vk.vkEnumeratePhysicalDevices(instance, c.byref(count), handles) != 0:
            return []
        result = []
        for index, handle in enumerate(handles):
            properties = c.create_string_buffer(4096)
            vk.vkGetPhysicalDeviceProperties(handle, properties)
            device_type = c.c_uint32.from_buffer(properties, 16).value
            if device_type not in (1, 2):  # integrated or discrete; exclude software/virtual adapters
                continue
            name = properties.raw[20:276].split(b"\0", 1)[0].decode("utf-8", errors="replace")
            memory = Memory()
            vk.vkGetPhysicalDeviceMemoryProperties(handle, c.byref(memory))
            local = max((heap.size for heap in memory.heaps[:min(memory.heaps_count, 16)] if heap.flags & 1), default=0)
            result.append({"name": name, "memory_gb": round(local / 2**30, 1),
                           "integrated": device_type == 1, "index": index, "backend": "vulkan"})
        return result
    except (OSError, AttributeError, ValueError):
        return []
    finally:
        if vk and instance.value:
            vk.vkDestroyInstance(instance, None)


def _json(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def transcribe(server, wav, model, language, threads, report, on_proc=None):
    # A per-job server keeps GPU memory reclaimable when the job ends or is cancelled.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [server, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-t", str(threads), "-l", language or "auto"]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    report("load-gpu", 28, "正在加载模型到 GPU")
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors, **flags)
        if on_proc:
            on_proc(process)
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    errors.seek(0)
                    raise RuntimeError("GPU 模型加载失败：" + errors.read()[-1500:].decode("utf-8", errors="replace"))
                try:
                    _json(base + "/health")
                    break
                except (OSError, ValueError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("GPU 模型加载超过 120 秒")
                    time.sleep(0.2)
            # A backend can silently choose CPU; only claim GPU when logs confirm it.
            errors.seek(0)
            logs = errors.read().decode("utf-8", errors="replace")
            if "using Vulkan" not in logs and "using GPU" not in logs:
                raise RuntimeError("引擎未确认使用 GPU，回退到 CPU")
            report("transcribe", 30, "正在使用 GPU 转写音频")
            boundary = "BOC" + uuid.uuid4().hex
            fields = {"response_format": "verbose_json", "language": language or "auto", "temperature": "0.0"}
            body = bytearray()
            for key, value in fields.items():
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
            with open(wav, "rb") as stream:
                body.extend(stream.read())
            body.extend(f"\r\n--{boundary}--\r\n".encode())
            request = urllib.request.Request(base + "/inference", data=bytes(body),
                                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            result = _json(request, timeout=3600)
            cues = [{"from": float(s["start"]), "to": float(s["end"]), "text": str(s["text"]).strip()}
                    for s in result.get("segments", []) if str(s.get("text", "")).strip()]
            if not cues:
                raise RuntimeError("GPU 转写没有返回带时间戳的字幕")
            return cues
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
