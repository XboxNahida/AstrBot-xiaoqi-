# 哈Gemi (Desktop Gemini Girl)

> **元气毒舌的 AI 桌面陪伴桌宠 · 实时视觉多模态屏幕感知 · 少女音锐评吐槽**  
> 支持 **Windows** 与 **macOS (Apple Silicon & Intel)** 双平台原生运行。  
> 支持 **Google Gemini 官方原生接口 (AI Studio)**、**Google Cloud Vertex AI (GCP 算力/赠金通道，支持 Gemini 4 Argon)**、**Google Antigravity 订阅通道 (免Key/烧会员额度)** 与 **OpenRouter / OpenAI 兼容自定义接口**。

---

## 核心特性

- **双版本并行独立架构（经典静态版 + 全新动态独立版）**：
  - **经典静态立绘版**：极简低功耗，支持 4 表情立绘工坊与本地一键去白底换装；
  - **全新动态独立版**：
    - **120 帧高精度动作序列**：待机呼吸、疯狂敲键盘写代码、被鼠标拖拽悬空挣扎晃动、点击开心跃动等连贯动作；
    - **60 FPS 物理引擎解耦**：将动画播放与窗口物理拖拽、摇摆倾斜重力补偿及飘字粒子完全解耦，带来丝滑顺手的物理微动；
    - **紧凑漫画对话气泡**：自适应猫耳方位定位，鼠标直接点击气泡即可打开互动输入框，随时沿上次锐评继续吐槽聊天；
    - **专属动态立绘帧率滑块**：控制台支持 8 ~ 24 FPS 自由变速调节，快慢节奏由你掌控。
- **全局屏幕多模态感知**：
  - 跨平台支持 Windows 与 macOS：自动识别当前前台活跃窗口（应用名与窗口标题，支持 macOS 原生 System Events 智能提取），针对不同场景智能唤醒多模态视觉推理：
    - **游戏战况锐评**：识别逆风劣势、逆天死法与下饭操作，毒舌不留情！
    - **视频与直播接梗**：深度参与 UP 主台词与弹幕互动，弹幕接梗妙语连珠！
    - **码字与工作摸鱼抓包**：报错抓现行、代码发呆吐槽、摸鱼立刻警告！
- **多模型与多服务商全渠道支持**：
  - **Google Antigravity 订阅通道 (免Key/烧会员额度)**：
    - 针对有 Antigravity 会员订阅的开发者量身打造，**无需申请或配置任何 API Key**；
    - 自动复用本机已登录的 Google Antigravity 会员凭证（支持 `google-antigravity` Python SDK 与 `agy` 原生 CLI 双通道桥接）；
    - 支持 `gemini-3.8-flash-high`、`claude-sonnet-4-6`、`claude-opus-4-6-thinking` 等顶级订阅模型；
    - 彻底远离 AI Studio 429 免费池排队限流，也无需担心 Vertex AI 产生意外账单扣费，真正实现“桌宠整天挂机随心烧会员算力”。
  - **Google Cloud Vertex AI (GCP 算力/赠金通道)**：
    - 针对 AI Studio 高峰期频繁忙碌 (429 / Resource Exhausted) 的高可用解决方案；
    - 完整支持 **`gemini-4-argon`**（Google 最新旗舰推理与 Agent 思考大模型）、`gemini-2.5-flash`、`gemini-2.5-pro` 等；
    - 默认采用 `global` 全球调度区域（自动跨地区负载均衡，超高可用与抗并发）；
    - 支持三种免折腾认证方式：**GCP 服务账号 JSON 密钥文件**、**本地 gcloud CLI / ADC 自动免密读取**、或 **Vertex API Key / Bearer Token**；
    - 自动过滤思考模型的思维链路（Thought/Reasoning），保证桌宠气泡播报纯净灵动。
  - **Google Gemini 原生 (AI Studio)**：内置 gemini-2.5-flash、gemini-2.0-flash、gemini-1.5-pro 等标准 API 接口，全面兼容新版 AQ 开头授权密钥与传统 AIzaSy 密钥；
  - **OpenRouter / OpenAI 兼容接口**：支持自定义 API URL（如 OpenRouter、OneAPI、第三方中转站），自由填入 `anthropic/claude-3.5-sonnet`、`openai/gpt-4o`、`deepseek/deepseek-chat` 等视觉大模型。
- **跨平台原生语音与音效引擎**：
  - **Windows**：底层基于 Windows MCI 多媒体引擎，毫秒级即时播报与插话打断；
  - **macOS**：原生基于系统级 `afplay` 音频引擎，零外置依赖，极速响应并支持随时切断打断。
- **表情工坊与立绘差分**：
  - 内置 4 套情绪立绘差分（自信傲娇、呆萌疑惑、抱头震惊、无语擦汗）；
  - 根据 AI 评论的情绪标签（`[shocked]`、`[curious]`、`[relieved]`、`[confident]`）全自动无缝切换立绘姿态；
  - **支持本地上传任意立绘**：内置智能去白底抠图算法，任意白底 JPG/PNG 自动秒转透明背景立绘。
- **自适应漫画气泡框**：
  - 纯白可爱圆角漫画气泡框配系统自适应黑体字体（macOS 完美匹配苹方 PingFang SC / 华文黑体，Windows 完美匹配微软雅黑），自带萌系小尾巴；
  - 动态预计算尺寸，根据长短句自动缩放，逐字打字机动效，支持右上角一键关闭与自由拖拽。
- **高品质神经语音与自定义语音模型 (TTS)**：
  - 内置丰富 Edge-TTS 拟真音色（新增超自然台湾软萌甜妹「晓臻」、经典傲娇「晓伊」、知性「晓晓」、二次元萌系「七海 Nanami」、东北/陕西方言妹等，彻底告别生硬机械音）；
  - 支持语速（Rate）与语调音高（Pitch）自由滑块微调；
  - **支持自定义语音模型 / 本地克隆 API**：支持接入 OpenAI 兼容的 `/v1/audio/speech` 语音合成接口（如本地 **GPT-SoVITS**、**CosyVoice**、**Fish Audio** 等声音克隆项目），随心为桌宠赋予你的专属角色音色！
  - 支持随时快捷键一键静音。
- **现代化独立 GUI 控制台与系统托盘**：
  - 完整的管理中心（核心感知设置、API 请求超时时间自定义与一键恢复默认、立绘工坊、口癖与人设调教、实时运行流水日志）；
  - 腾讯云一句话识别 API 密钥控制台一键跳转与快速复制，方便开箱配置语音输入；
  - 跨平台开机自启（Windows 注册表 `Run` 项 / macOS `LaunchAgent` 用户守护项）；
  - 托盘小图标双击唤出、桌宠右键菜单。
- **应用内检查更新与版本公告中心 (In-App Updates & Releases)**：
  - **开机自动感知**：应用启动时在后台静默异步查询 GitHub Releases，发现新版本时哈Gemi会主动通过动作、气泡与语音撒娇提醒主人更新；
  - **可视化更新公告阅读器**：控制台底部与托盘菜单一键唤出“🔄 检查更新与公告”，在应用内直接阅读富文本排版的最新版本特性、开源致谢与更新日志；
  - **一键极速直连下载**：内置多线程流式下载器，进度条直观显示，一键自动保存至系统【下载】文件夹并弹出定位。
- **安全与隐私保护**：
  - 密钥纯本地存储在 `config.json`（macOS 打包后保存在 `~/Library/Application Support/HaGemi/`），绝不上报任何云端服务器；
  - 拥有专属“桌宠自免盾”，永远不会检索或锐评自己与控制台窗口。

---

## 常用快捷键

| 快捷键 (Windows) | 快捷键 (macOS) | 鼠标交互 | 功能说明 |
| :--- | :--- | :--- | :--- |
| **`Ctrl + F9`** | **`Cmd + F9`** 或 **`Ctrl + F9`** | **双击桌宠** | 立即召唤哈Gemi对当前屏幕/前台窗口进行视觉锐评 |
| **`Ctrl + F10`** | **`Cmd + F10`** 或 **`Ctrl + F10`** | - | 切换语音播报（开启 / 静音） |
| **`Ctrl + F1`** | **`Cmd + F1`** 或 **`Ctrl + F1`** | - | 开始录音；再次按下后停止并通过语音识别，文字发送给桌宠 |
| **`Ctrl + F11`** | **`Cmd + F11`** 或 **`Ctrl + F11`** | **滚轮滑动立绘 / 鼠标中键** | 瞬间循环切换下一个立绘姿态/表情差分 |
| - | - | **右键桌宠 / 双击托盘图标** | 打开【控制台与设置中心】 |
| - | - | **点击气泡右上角 `×`** | 快速关闭当前说话气泡 |

---

## 快速上手

### 方式一：源码运行（Windows / macOS）

1. **克隆仓库并安装依赖**：
   ```bash
   git clone https://github.com/LYW199911/DesktopGeminiGirl.git
   cd DesktopGeminiGirl
   pip install -r requirements.txt
   ```

2. **配置与启动**：
   - **Windows**：
     ```bash
     start_pet.bat
     # 或者 python main.py
     ```
   - **macOS**：
     ```bash
     chmod +x start_pet.sh
     ./start_pet.sh
     # 或者 python3 main.py
     ```

3. **macOS 首次运行系统权限授权**：
   macOS 的安全隐私机制要求应用在使用系统能力时获得授权。请在 macOS **「系统设置」 -> 「隐私与安全性」** 中为当前运行的终端（如 Terminal、iTerm2、VSCode）或打出的 `Ha-Gemi.app` 开启以下权限：
   - **辅助功能 (Accessibility)**：用于全局快捷键（`Cmd+F1`、`Cmd+F9` 等）监听；
   - **屏幕录制 (Screen Recording)**：用于桌宠截取前台窗口进行视觉理解；
   - **麦克风 (Microphone)**：用于语音对线录音输入。

4. **配置 API 渠道**：
   - 启动后右键桌宠或点击托盘图标，在【核心与感知】中选择你的接口类型：
     - **Google Antigravity 订阅通道 (推荐)**：
       1. 零配置，无需填任何 Key；
       2. 模型可选 `gemini-3.8-flash-high`、`claude-sonnet-4-6` 等；
       3. 点击【测试连接】即可直接使用本机 Antigravity 会员配额！
     - **Google Cloud Vertex AI (GCP 算力/赠金)**：
       1. 区域推荐 `global`，模型推荐 `gemini-4-argon`（体验最新思考推理大模型）或 `gemini-2.5-flash`；
       2. 填入 GCP 项目 ID；
       3. 认证方式任选其一：点击【浏览】选择 GCP 服务账号 JSON 密钥文件，或若本地已登录 gcloud 则直接留空凭据即可自动识别！
     - **Google Gemini 官方原生接口 (AI Studio)**：填入 AI Studio API Key（兼容 `AQ` 与 `AIzaSy` 前缀）。
     - **OpenRouter / OpenAI 兼容接口**：填入 URL 与 Key。
   - 点击【测试连接】并保存即可！

---

### 方式二：打包为独立分发程序

- **Windows 打包 (.exe)**：
  ```bash
  build_exe.bat
  ```
  打包生成于 `dist\Ha-Gemi\`，直接双击 `Ha-Gemi.exe` 即可运行。

- **macOS 打包 (.app & .zip)**：
  ```bash
  chmod +x build_mac.sh
  ./build_mac.sh
  ```
  打包生成于 `dist/Ha-Gemi.app` 与 `dist/Ha-Gemi-macOS.zip`。  
  > **macOS 提示“已损坏，无法打开”解决办法**：  
  > 因未经 Apple 开发者签名公证，在另一台 Mac 解压打开时若提示损坏，在终端执行以下命令清除隔离属性即可正常运行：
  > ```bash
  > xattr -cr /Applications/Ha-Gemi.app
  > ```

---

## 目录结构

```text
DesktopGeminiGirl/
├── assets/                     # 桌宠立绘、图标与表情差分
│   ├── app_icon.ico            # Windows 托盘与窗口图标
│   ├── app_icon.icns           # macOS 高清视网膜应用图标
│   ├── avatar_standing.png     # 傲娇自信立绘
│   ├── avatar_bust.png         # 疑惑Q版立绘
│   ├── avatar_shocked.png      # 震惊表情
│   └── avatar_relieved.png     # 害羞擦汗表情
├── brain/                      # AI 推理大脑
│   ├── gemini_client.py        # Gemini 原生 / Vertex AI / Antigravity 订阅 / OpenRouter 调度引擎
│   ├── prompts.py              # 元气毒舌人设与多场景提示词构建
│   ├── context_memory.py       # 上下文记忆管理
│   └── rate_limiter.py         # 智能感知冷却与场景限流器
├── perception/                 # 屏幕与桌面环境感知
│   ├── window_tracker.py       # 跨平台 (Win32/macOS System Events) 前台活跃窗口与场景分类
│   ├── screen_capture.py       # 高性能局部/全屏屏幕截图与多模态图像压缩
│   ├── tencent_asr.py          # 腾讯云一句话语音识别
│   └── hotkey_listener.py      # 全局键盘快捷键监听 (支持 Win/Mac 跨平台修饰键)
├── tts/                        # 语音播报模块
│   └── edge_speaker.py         # 微软 Edge-TTS 异步语音生成与播放 (Win32 MCI / macOS afplay)
├── ui/                         # 界面与交互设计
│   ├── bubble_widget.py        # 自适应防塌陷漫画白气泡框 (PingFang SC / 微软雅黑 字体兼容)
│   ├── pet_widget.py           # 悬浮桌宠挂件（支持拖拽、右键菜单、悬浮晃动）
│   ├── settings_window.py      # 全功能 GUI 设置中心 (含 Vertex AI、Antigravity 会员与各渠道配置)
│   ├── tray_icon.py            # 系统托盘菜单
│   ├── interaction_dialog.py   # 吐槽交互对话框
│   └── img_utils.py            # 智能白色背景抠图转换工具
├── config_manager.py           # 配置管理与跨平台开机自启 (Win 注册表 / macOS LaunchAgent)
├── log_manager.py              # Qt 线程安全日志系统
├── app_paths.py                # 跨平台与 PyInstaller 冻结路径解析 (macOS Application Support 适配)
├── main.py                     # 程序入口、跨平台单实例锁与生命周期管理
├── config.example.json         # 配置文件示例（不含敏感 API Key）
├── requirements.txt            # Python 运行环境依赖
├── HaGemi.spec                 # 跨平台 PyInstaller 打包描述文件
├── build_exe.bat               # Windows 一键编译脚本
├── start_pet.bat               # Windows 一键启动脚本
├── build_mac.sh                # macOS 一键打包脚本 (.app & .zip)
└── start_pet.sh                # macOS 一键启动脚本
```

---

## 隐私与安全性

- **无隐私上传**：所有的配置、立绘和音频缓存均保存在本地计算机上；
- **透明开源**：代码 100% 开源，API 请求直连官方或你指定的 API 代理，不经过任何中间遥测收集服务器。

---

## 鸣谢与版权致谢 (Credits & Acknowledgments)

- **开源生态与动作资源致谢**：
  - **[hunmer/aboutme-pets](https://github.com/hunmer/aboutme-pets)**：提供了 100 款 120 帧连贯 WebP 动作序列图集与高质量配套音频，为动态版动作呈现提供了核心资产支持；
  - **[KCFindstr/hagemi-pet-agy](https://github.com/KCFindstr/hagemi-pet-agy)**：探索了基于 Google Antigravity 原生 Sidecar 桌面萌宠与转录会话感知联动的优秀方案；
  - **[lokicorvus/crosspet](https://github.com/lokicorvus/crosspet)**：为多姿态状态机、60 FPS 悬空挣扎物理反馈与边缘交互提供了极具价值的实现参考；
  - **[Monokumakuki/Antigravity-Star-Suite](https://github.com/Monokumakuki/Antigravity-Star-Suite)**：为桌面部件与感知监控设计提供了宝贵的工程参考。
- **桌宠立绘与表情差分出处**：感谢 B 站 UP 主 **[@Xx勿忘xX](https://space.bilibili.com/25914748)** 制作并分享的 Gemini 表情包与立绘差分（动态专栏原帖：[⭐Gemini⭐分享几个Gemini的表情包和立绘无耳差分](https://www.bilibili.com/opus/1264669110489317411)）。
- **原立绘人设原创**：原版 Gemini 娘人设与主立绘出处为画师大佬 **@zipzip**。
- **开源生态**：感谢 PyQt6、Edge-TTS 与 Google Gemini 提供的强大生态支持。
