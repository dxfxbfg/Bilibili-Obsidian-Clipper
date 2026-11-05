@echo off
chcp 65001 >nul

if defined BOC_PYTHON (
    "%BOC_PYTHON%" "%~dp0boc_native_host.py"
) else (
    python "%~dp0boc_native_host.py"
)
