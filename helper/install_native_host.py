"""One-time, per-user Native Messaging registration for Chrome/Edge on Windows."""
import argparse
import json
import os
import re
import sys
from pathlib import Path


def install(extension_id, browser="chrome"):
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("扩展 ID 必须是扩展管理页显示的 32 位 a-p 字符")
    if os.name != "nt":
        raise RuntimeError("自动注册目前支持 Windows Chrome/Edge")
    import winreg
    helper = Path(__file__).resolve().parent
    destination = Path(os.environ["LOCALAPPDATA"]) / "BOC" / "native"
    destination.mkdir(parents=True, exist_ok=True)
    launcher = destination / "boc-native.bat"
    launcher.write_bytes(('@echo off\r\nchcp 65001 >nul\r\n"' + sys.executable + '" "' + str(helper / "boc_native_host.py") + '"\r\n').encode("utf-8"))
    manifest = destination / f"com.boc.desk-{browser}.json"
    manifest.write_text(json.dumps({
        "name": "com.boc.desk", "description": "BOC local helpers",
        "path": str(launcher), "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"]
    }, indent=2), encoding="utf-8")
    vendor = {"chrome": r"Google\Chrome", "edge": r"Microsoft\Edge"}[browser]
    key_path = rf"Software\{vendor}\NativeMessagingHosts\com.boc.desk"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="安装 BOC 本地启动器，无需管理员权限")
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    args = parser.parse_args()
    print(install(args.extension_id, args.browser))
    print("启动器已注册。回到扩展点击自动配置，或直接生成字幕。移动项目后需重新注册。")
