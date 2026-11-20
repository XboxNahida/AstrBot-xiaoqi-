import os
import sys
import shutil

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
    BASE_DIR = APP_DIR

    # macOS App Bundle Resource Detection
    # If running inside Contents/MacOS/, Resources may be at Contents/Resources/
    bundle_res_assets = os.path.join(APP_DIR, "..", "Resources", "assets")
    macos_app_dir_assets = os.path.join(APP_DIR, "assets")
    internal_assets = os.path.join(APP_DIR, "_internal", "assets")

    if IS_MAC and os.path.exists(bundle_res_assets):
        ASSETS_DIR = os.path.abspath(bundle_res_assets)
    elif os.path.exists(macos_app_dir_assets):
        ASSETS_DIR = macos_app_dir_assets
    elif os.path.exists(internal_assets):
        ASSETS_DIR = internal_assets
    else:
        ASSETS_DIR = macos_app_dir_assets
        os.makedirs(ASSETS_DIR, exist_ok=True)

    # On macOS, write configuration to ~/Library/Application Support/HaGemi
    # because /Applications is read-only for apps without root privileges.
    if IS_MAC:
        USER_DATA_DIR = os.path.expanduser("~/Library/Application Support/HaGemi")
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        CONFIG_FILE = os.path.join(USER_DATA_DIR, "config.json")
        MEMORY_FILE = os.path.join(USER_DATA_DIR, "memory.json")
    else:
        CONFIG_FILE = os.path.join(APP_DIR, "config.json")
        MEMORY_FILE = os.path.join(APP_DIR, "memory.json")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    ASSETS_DIR = os.path.join(BASE_DIR, "assets")
    CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
    MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")

ICON_PATH = os.path.join(ASSETS_DIR, "app_icon.ico")
if IS_MAC:
    mac_icns = os.path.join(ASSETS_DIR, "app_icon.icns")
    if os.path.exists(mac_icns):
        ICON_PATH = mac_icns
    elif not os.path.exists(ICON_PATH):
        ICON_PATH = os.path.join(ASSETS_DIR, "avatar_default.png")
elif not os.path.exists(ICON_PATH):
    ICON_PATH = os.path.join(ASSETS_DIR, "avatar_default.png")

def get_asset_path(filename: str) -> str:
    return os.path.join(ASSETS_DIR, filename)
