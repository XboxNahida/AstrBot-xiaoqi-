#!/bin/bash
set -e

# Change directory to the script's directory
cd "$(dirname "$0")"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

echo "==================================================="
echo "  Starting Ha-Gemi Dynamic Pet on macOS..."
echo "==================================================="
echo "  快捷键 (macOS):"
echo "    Cmd+F1  (或 Ctrl+F1)  : 开启/停止语音对线"
echo "    Cmd+F9  (或 Ctrl+F9)  : 立即截屏锐评"
echo "    Cmd+F10 (或 Ctrl+F10) : 开启/静音语音朗读"
echo "    Cmd+F11 (或 Ctrl+F11) : 切换立绘造型"
echo "    鼠标滚轮 / 中键点击桌宠 : 快速切换立绘"
echo "==================================================="
echo "  [提示] 首次在 macOS 运行需在「系统设置 -> 隐私与安全性」中授予："
echo "    1. 辅助功能 (Accessibility) - 全局快捷键监听"
echo "    2. 屏幕录制 (Screen Recording) - 截屏视奸锐评"
echo "    3. 麦克风 (Microphone) - 语音对线录音"
echo "==================================================="

# Find python interpreter
PYTHON_CMD="python3"
if [ -d ".venv" ] && [ -f ".venv/bin/python" ]; then
    PYTHON_CMD=".venv/bin/python"
elif [ -d "venv" ] && [ -f "venv/bin/python" ]; then
    PYTHON_CMD="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "[ERROR] 未找到 Python 3，请先安装 Python 3.10+ 环境！"
    exit 1
fi

$PYTHON_CMD main_dynamic.py
