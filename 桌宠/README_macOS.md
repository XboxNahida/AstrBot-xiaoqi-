# 🍎 哈Gemi (Desktop Gemini Girl) macOS 运行与使用指南

欢迎在 macOS 系统上使用哈Gemi桌宠！本版本已全面深度适配 macOS 系统特性（支持 Apple Silicon M系列芯片与 Intel 架构）。

---

## ⚡ 极速启动 (快捷运行)

1. **环境准备**：
   确保您的 Mac 已安装 Python 3.10 或更高版本（若未安装，推荐在终端通过 `brew install python@3.11` 安装）。

2. **安装依赖**：
   打开终端（Terminal），进入本目录并执行：
   ```bash
   pip3 install -r requirements.txt
   ```

3. **一键启动**：
   - **全新动态立绘版 (推荐！120帧高精度动作)**：
     ```bash
     chmod +x start_dynamic_pet.sh
     ./start_dynamic_pet.sh
     ```
   - **经典静态立绘版 (支持4表情换装)**：
     ```bash
     chmod +x start_pet.sh
     ./start_pet.sh
     ```

---

## 🔒 macOS 系统权限配置 (重要)

首次在 macOS 运行，请在 **「系统设置」 ➔ 「隐私与安全性」** 中为终端（Terminal / iTerm2）或生成的 App 授予以下权限：

1. **辅助功能 (Accessibility)**：用于监听全局快捷键对线。
2. **屏幕录制 (Screen Recording)**：用于截屏多模态视奸吐槽与锐评。
3. **麦克风 (Microphone)**：用于实时语音输入对线互动。

---

## 🎹 macOS 专属全局快捷键

- **`Cmd + F1`**（或 `Ctrl + F1`）：开启 / 停止语音录音对线
- **`Cmd + F9`**（或 `Ctrl + F9`）：即时截屏并触发屏幕视觉锐评
- **`Cmd + F10`**（或 `Ctrl + F10`）：切换静音 / 恢复语音朗读
- **`Cmd + F11`**（或 `Ctrl + F11`）：循环切换立绘差分造型
- **鼠标滚轮 / 中键点击桌宠**：快速切换表情差分

---

## 🛠️ 本地一键打包为 Mac 应用程序 (.app)

如果您希望生成原生的 Mac 双击独立运行程序（`.app`），只需在终端执行：
```bash
chmod +x build_mac.sh
./build_mac.sh
```
打包完成后将在 `dist/` 目录下生成：
- `Ha-Gemi-Dynamic.app` 与 `Ha-Gemi_Dynamic_macOS.zip`
- `Ha-Gemi.app` 与 `Ha-Gemi_Classic_macOS.zip`

> 提示：若在其他 Mac 上解压运行提示“应用程序已损坏”，在终端执行 `xattr -cr /Applications/Ha-Gemi*.app` 即可解除系统安全隔离。
