import os
import sys
import json
from typing import Dict, Any, Tuple
from app_paths import BASE_DIR, CONFIG_FILE

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import winreg
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
else:
    winreg = None
    RUN_KEY = ""

APP_NAME = "GeminiDesktopPet_HaGemi"
MAC_LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/com.lyw.hagemi.plist")
CYBER_WHALE_CONFIG = os.path.expanduser("~/.gemini/cyber-whale/ai_config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_provider": "gemini",  # "gemini", "vertex", "antigravity", or "openai_compatible"
    "api_key": "",
    "custom_api_url": "https://openrouter.ai/api/v1",
    "vertex_project_id": "",
    "vertex_location": "global",
    "vertex_credentials_file": "",
    "model": "gemini-3.8-flash",
    "antigravity_cli_path": "",
    "tencent_secret_id": "",
    "tencent_secret_key": "",
    "api_timeout": 35,
    "temperature": 0.88,
    "cooldown_seconds": 45,
    "enable_tts": True,
    "tts_engine": "edge_tts",
    "tts_voice": "zh-CN-XiaoyiNeural",
    "tts_rate": "+10%",
    "tts_pitch": "+10Hz",
    "tts_api_url": "",
    "tts_api_key": "",
    "tts_model": "",
    "gemini_tts_voice": "Aoede",
    "gemini_tts_model": "gemini-3.8-flash-tts",
    "dynamic_fps": 15,
    "auto_mode": True,
    "target_height": 250,
    "comment_on_games": True,
    "comment_on_videos": True,
    "comment_on_work": True,
    "enable_persistent_memory": True,
    "pet_name": "哈Gemi",
    "catchphrases": ["喵~", "喵？！", "哼~", "盯——"],
    "persona_style": "元气毒舌",
    "custom_system_prompt": "",
    "outfits": {
        "confident": "avatar_standing.png",
        "curious": "avatar_bust.png",
        "shocked": "avatar_shocked.png",
        "relieved": "avatar_relieved.png"
    }
}

class ConfigManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.config = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> Dict[str, Any]:
        # 1. Fallback / inherit API key from cyber-whale if empty
        inherited_key = ""
        inherited_model = "gemini-3.8-flash"
        if os.path.exists(CYBER_WHALE_CONFIG):
            try:
                with open(CYBER_WHALE_CONFIG, "r", encoding="utf-8") as f:
                    cw = json.load(f)
                    inherited_key = cw.get("api_key", "").strip()
                    inherited_model = cw.get("model", "gemini-3.8-flash").strip()
            except Exception:
                pass

        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.config.update(loaded)
            except Exception as e:
                print(f"[ConfigManager] Error reading config: {e}")

        # If still no api_key, use inherited
        if not self.config.get("api_key") and inherited_key:
            self.config["api_key"] = inherited_key
        if not self.config.get("model") and inherited_model:
            self.config["model"] = inherited_model

        return self.config

    def save(self, new_config: Dict[str, Any] = None):
        if new_config:
            self.config.update(new_config)
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            print("[ConfigManager] Settings saved to config.json")
        except Exception as e:
            print(f"[ConfigManager] Failed to save config: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value

    # Startup Management (Windows Registry / macOS LaunchAgent)
    def is_autostart_enabled(self) -> bool:
        if IS_WIN and winreg:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, APP_NAME)
                    return bool(val)
            except (FileNotFoundError, OSError):
                return False
        elif IS_MAC:
            return os.path.exists(MAC_LAUNCH_AGENT)
        return False

    def set_autostart(self, enable: bool) -> bool:
        if IS_WIN and winreg:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    if enable:
                        if getattr(self, 'autostart_command', ''):
                            exe_path = self.autostart_command
                        elif getattr(sys, 'frozen', False):
                            exe_path = f'"{sys.executable}"'
                        else:
                            exe_path = f'"{os.path.abspath(os.path.join(BASE_DIR, "start_pet.bat"))}"'
                        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
                        print(f"[ConfigManager] Autostart enabled -> {exe_path}")
                    else:
                        try:
                            winreg.DeleteValue(key, APP_NAME)
                            print("[ConfigManager] Autostart disabled")
                        except FileNotFoundError:
                            pass
                return True
            except Exception as e:
                print(f"[ConfigManager] Failed to set Windows autostart: {e}")
                return False
        elif IS_MAC:
            try:
                if enable:
                    os.makedirs(os.path.dirname(MAC_LAUNCH_AGENT), exist_ok=True)
                    target_cmd = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(os.path.join(BASE_DIR, "start_pet.sh"))
                    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lyw.hagemi</string>
    <key>ProgramArguments</key>
    <array>
        <string>{target_cmd}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>"""
                    with open(MAC_LAUNCH_AGENT, "w", encoding="utf-8") as f:
                        f.write(plist_content)
                    print(f"[ConfigManager] macOS LaunchAgent enabled -> {MAC_LAUNCH_AGENT}")
                else:
                    if os.path.exists(MAC_LAUNCH_AGENT):
                        os.remove(MAC_LAUNCH_AGENT)
                        print("[ConfigManager] macOS LaunchAgent disabled")
                return True
            except Exception as e:
                print(f"[ConfigManager] Failed to set macOS autostart: {e}")
                return False
        return False
