#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native Messaging 启动器：浏览器扩展按需拉起桌面助手。

Chrome 在扩展调用 chrome.runtime.sendNativeMessage 时启动本进程，
通过 stdin 送一条 4 字节长度 + JSON 的消息。这里读掉它、把
boc_desk_helper.py 以无窗口方式拉起来、回一条结果就退出。

助手自己有空闲超时（默认 90 秒没请求自杀），所以不需要常驻。
"""

import json
import os
import socket
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "boc_desk_helper.py")
PYTHONW = os.path.join(
    os.path.expanduser("~"),
    ".workbuddy", "binaries", "python", "envs", "default", "Scripts", "pythonw.exe",
)
PORT = 8789

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    if not length:
        return None
    body = sys.stdin.buffer.read(length)
    try:
        return json.loads(body.decode("utf8"))
    except Exception:
        return {}


def write_message(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf8")
    sys.stdout.buffer.write(struct.pack("<I", len(body)))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def port_open(port=PORT, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def launch():
    python = PYTHONW if os.path.exists(PYTHONW) else sys.executable
    subprocess.Popen(
        [python, HELPER, "--port", str(PORT)],
        cwd=HERE,
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main():
    msg = read_message()
    action = str((msg or {}).get("cmd") or "start").strip()

    if action == "status":
        write_message({"ok": True, "running": port_open(), "port": PORT})
        return

    if port_open():
        write_message({"ok": True, "running": True, "started": False, "port": PORT})
        return

    launch()
    write_message({"ok": True, "running": False, "started": True, "port": PORT})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 启动失败也要回一条，否则扩展一直等
        try:
            write_message({"ok": False, "error": str(exc)})
        except Exception:
            pass
        sys.exit(1)
