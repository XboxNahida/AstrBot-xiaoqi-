#!/bin/bash
set -e

# Change directory to the script's directory
cd "$(dirname "$0")"

echo "==================================================="
echo "  Building Ha-Gemi Desktop Pet for macOS (.app)..."
echo "==================================================="

PYTHON_CMD="python3"
if [ -d ".venv" ] && [ -f ".venv/bin/python" ]; then
    PYTHON_CMD=".venv/bin/python"
elif [ -d "venv" ] && [ -f "venv/bin/python" ]; then
    PYTHON_CMD="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

if ! $PYTHON_CMD -m PyInstaller --version >/dev/null 2>&1; then
    echo "[INFO] 未检测到 pyinstaller，正在安装..."
    $PYTHON_CMD -m pip install pyinstaller
fi

# 1. Build Dynamic Edition
echo "[1/2] Building Ha-Gemi Dynamic Edition (.app)..."
$PYTHON_CMD -m PyInstaller HaGemiDynamic.spec --noconfirm --distpath dist --workpath build

if [ -d "dist/Ha-Gemi-Dynamic.app" ]; then
    mkdir -p "dist/Ha-Gemi-Dynamic.app/Contents/Resources/assets"
    cp -R assets/* "dist/Ha-Gemi-Dynamic.app/Contents/Resources/assets/" 2>/dev/null || true
    if [ -f "config.example.json" ]; then
        cp "config.example.json" "dist/Ha-Gemi-Dynamic.app/Contents/Resources/" 2>/dev/null || true
    fi
    echo "Packaging Ha-Gemi_Dynamic_macOS.zip..."
    (cd dist && rm -f Ha-Gemi_Dynamic_macOS.zip && zip -r -q -y Ha-Gemi_Dynamic_macOS.zip Ha-Gemi-Dynamic.app)
fi

# 2. Build Classic Edition
echo "[2/2] Building Ha-Gemi Classic Edition (.app)..."
$PYTHON_CMD -m PyInstaller HaGemi.spec --noconfirm --distpath dist --workpath build

if [ -d "dist/Ha-Gemi.app" ]; then
    mkdir -p "dist/Ha-Gemi.app/Contents/Resources/assets"
    cp -R assets/* "dist/Ha-Gemi.app/Contents/Resources/assets/" 2>/dev/null || true
    if [ -f "config.example.json" ]; then
        cp "config.example.json" "dist/Ha-Gemi.app/Contents/Resources/" 2>/dev/null || true
    fi
    echo "Packaging Ha-Gemi_Classic_macOS.zip..."
    (cd dist && rm -f Ha-Gemi_Classic_macOS.zip && zip -r -q -y Ha-Gemi_Classic_macOS.zip Ha-Gemi.app)
fi

echo "==================================================="
echo "[SUCCESS] macOS 打包完成！"
echo "  - 动态版: dist/Ha-Gemi-Dynamic.app (Ha-Gemi_Dynamic_macOS.zip)"
echo "  - 经典版: dist/Ha-Gemi.app (Ha-Gemi_Classic_macOS.zip)"
echo "  - 若在其他 Mac 上打开提示已损坏，请执行："
echo "      xattr -cr /Applications/Ha-Gemi*.app"
echo "==================================================="
