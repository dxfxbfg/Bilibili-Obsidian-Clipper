#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BOC 桌面助手：把 B 站页面和 Obsidian 摆成左右分屏。

扩展里点「保存并并排」时调用：
    POST /split  {"url": "obsidian://open?vault=...&file=...", "layout": "right"}

它做三件事：记住当前活动窗口（浏览器）→ 用 obsidian:// 打开笔记 →
等 Obsidian 窗口出现 → 两个窗口各占半屏。

只用标准库 + ctypes，不需要 pywin32。

启动：
    python helper/boc_desk_helper.py            # 默认 127.0.0.1:8789
    python helper/boc_desk_helper.py --port 8789
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

user32 = ctypes.windll.user32

SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SPI_GETWORKAREA = 0x0030

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

# 空闲计时：每次请求刷新，超时自己退出，不常驻
LAST_HIT = time.time()
IDLE_TIMEOUT = 90.0


def screen_size():
    return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


def work_area():
    """任务栏之外的可视区域。按全屏尺寸摆窗口会被任务栏压住底部。"""
    rect = wt.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    width, height = screen_size()
    return 0, 0, width, height


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def is_visible_window(hwnd):
    return bool(user32.IsWindowVisible(hwnd)) and bool(user32.GetWindowTextLengthW(hwnd))


def find_obsidian_window(timeout=6.0, exclude=0, vault=""):
    """按标题找 Obsidian 窗口，标题形如「笔记 - 库名 - Obsidian 1.14.4」。
    给了 vault 就优先挑那个库的窗口，避免多库同时开着时摆错。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []
        matched = []

        def callback(hwnd, _):
            if hwnd == exclude or not is_visible_window(hwnd):
                return True
            title = window_title(hwnd)
            if "Obsidian" in title and " - " in title:
                found.append(hwnd)
                if vault and (" - %s - " % vault) in title:
                    matched.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if matched:
            return matched[0]
        if found and not vault:
            return found[0]
        time.sleep(0.35)
    return 0


def find_browser_window(timeout=2.0):
    """前台是 Obsidian 时（上一次并排把它激活了），改找浏览器主窗口"""
    deadline = time.time() + timeout
    keywords = ("Chrome", "Edge", "Firefox", "Brave", "Opera")
    while time.time() < deadline:
        found = []

        def callback(hwnd, _):
            if not is_visible_window(hwnd):
                return True
            title = window_title(hwnd)
            if any(word in title for word in keywords):
                found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if found:
            return found[0]
        time.sleep(0.2)
    return 0


def place(hwnd, x, y, w, h):
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
    return True


def open_note_and_split(note_url, layout="right", keep_browser=True, vault=""):
    ox, oy, aw, ah = work_area()
    half = aw // 2
    browser = 0
    if keep_browser:
        foreground = user32.GetForegroundWindow()
        # 前台已经停在 Obsidian 上时不能拿它当浏览器，否则会把自己排除掉
        if foreground and "Obsidian" not in window_title(foreground):
            browser = foreground
        else:
            browser = find_browser_window()

    # 交给系统处理 obsidian:// —— 用 startfile 比 shellexecute 省事
    try:
        os.startfile(note_url)
    except OSError as exc:
        return {"ok": False, "error": "打开 Obsidian 失败：%s" % exc}

    obsidian = find_obsidian_window(timeout=6.0, exclude=browser, vault=vault)
    if not obsidian:
        return {"ok": False, "error": "没找到 Obsidian 窗口，笔记已打开但没并排"}

    def arrange():
        # 有的窗口有最小宽度（Chrome 就压不到 744），设完要读回真实边界再分另一半，
        # 否则两半会叠在一起来
        if layout == "left":
            place(obsidian, ox, oy, half, ah)
            rect = wt.RECT()
            edge = ox + half
            user32.GetWindowRect(obsidian, ctypes.byref(rect))
            edge = rect.right
            place(browser, edge, oy, ox + aw - edge, ah)
        else:
            place(browser, ox, oy, half, ah)
            rect = wt.RECT()
            edge = ox + half
            if browser:
                user32.GetWindowRect(browser, ctypes.byref(rect))
                edge = rect.right
            place(obsidian, edge, oy, ox + aw - edge, ah)

    arrange()
    user32.SetForegroundWindow(obsidian)
    # Obsidian 激活后常把窗口还原成最大化，隔一下再摆一次才稳
    time.sleep(0.35)
    arrange()
    return {
        "ok": True,
        "layout": layout,
        "screen": [aw, ah],
        "browser": int(browser),
        "obsidian": int(obsidian),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "BOCDeskHelper/1.0"

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        global LAST_HIT
        LAST_HIT = time.time()
        if self.path.split("?")[0] in ("/health", "/"):
            self._json({"ok": True, "service": "boc-desk-helper", "build": 5,
                 "idle_for": round(time.time() - LAST_HIT, 1), "idle_timeout": IDLE_TIMEOUT,
                 "screen": screen_size(), "work": work_area()})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        global LAST_HIT
        LAST_HIT = time.time()
        if self.path.split("?")[0] != "/split":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf8") or "{}")
        except Exception as exc:
            self._json({"ok": False, "error": "请求体不是合法 JSON：%s" % exc}, 400)
            return

        url = str(payload.get("url") or "").strip()
        if not url.startswith("obsidian://"):
            self._json({"ok": False, "error": "只接受 obsidian:// 开头的地址"}, 400)
            return
        layout = "left" if payload.get("layout") == "left" else "right"
        vault = str(payload.get("vault") or "").strip()
        result = open_note_and_split(url, layout=layout, vault=vault)
        self._json(result, 200 if result.get("ok") else 500)

    def log_message(self, fmt, *args):
        # pythonw 下 sys.stderr 是 None，不判空会让每个请求都崩
        if sys.stderr:
            sys.stderr.write("[desk] " + (fmt % args) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--idle-timeout", type=float, default=90.0,
                        help="秒；这么久没请求就自己退出，0 表示常驻")
    args = parser.parse_args()

    global IDLE_TIMEOUT
    IDLE_TIMEOUT = args.idle_timeout

    def watchdog():
        while True:
            time.sleep(5)
            if IDLE_TIMEOUT > 0 and time.time() - LAST_HIT > IDLE_TIMEOUT:
                if sys.stderr:
                    sys.stderr.write("[desk] 空闲 %.0fs，退出\n" % (time.time() - LAST_HIT))
                os._exit(0)

    if IDLE_TIMEOUT > 0:
        threading.Thread(target=watchdog, daemon=True).start()

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        # 端口已被占：说明已经有一个在跑，自己直接退，别留僵尸进程
        if sys.stderr:
            sys.stderr.write("[desk] %d 已被占用，退出\n" % args.port)
        os._exit(0)

    out = sys.stderr or open(os.devnull, "w")
    out.write("BOC 桌面助手已启动：http://%s:%d，屏幕 %sx%s\n"
              % (args.host, args.port, *screen_size()))
    out.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
