# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all

BASE_DIR = os.path.abspath(SPECPATH)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
try:
    from version import VERSION
except Exception:
    VERSION = "1.5.2"

datas = [
    (os.path.join(BASE_DIR, 'assets'), 'assets'),
    (os.path.join(BASE_DIR, 'config.example.json'), '.'),
]
sounddevice_datas, sounddevice_binaries, sounddevice_hiddenimports = collect_all('sounddevice')
datas += sounddevice_datas

hiddenimports = [
    'pynput',
    'sounddevice',
    *sounddevice_hiddenimports,
    'edge_tts',
    'mss',
    'PIL',
    'PIL.Image',
    'requests',
    'PyQt6',
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'ctypes',
    'aiohttp',
    'asyncio',
    'config_manager',
    'log_manager',
    'app_paths',
    'version',
    'brain.context_memory',
    'brain.gemini_client',
    'brain.rate_limiter',
    'perception.window_tracker',
    'perception.screen_capture',
    'perception.hotkey_listener',
    'tts.edge_speaker',
    'ui.pet_widget',
    'ui.tray_icon',
    'ui.settings_window',
    'ui.interaction_dialog',
    'ui.bubble_widget',
    'ui.update_dialog',
    'update_checker',
    'cryptography',
    'cryptography.hazmat.primitives',
    'cryptography.hazmat.primitives.asymmetric',
    'cryptography.hazmat.primitives.serialization',
]

if sys.platform == 'win32':
    hiddenimports += [
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'winreg',
        'ctypes.wintypes',
    ]
elif sys.platform == 'darwin':
    hiddenimports += [
        'pynput.keyboard._darwin',
        'pynput.mouse._darwin',
    ]

try:
    import google.auth
    hiddenimports += ['google.auth', 'google.oauth2', 'google.oauth2.service_account']
except ImportError:
    pass

a = Analysis(
    [os.path.join(BASE_DIR, 'main.py')],
    pathex=[BASE_DIR],
    binaries=sounddevice_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy'],
    noarchive=False,
)

pyz = PYZ(a.pure)

icon_path = None
if sys.platform == 'win32':
    icon_path = os.path.join(BASE_DIR, 'assets', 'app_icon.ico')
elif sys.platform == 'darwin':
    icns = os.path.join(BASE_DIR, 'assets', 'app_icon.icns')
    if os.path.exists(icns):
        icon_path = icns

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Ha-Gemi',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Ha-Gemi',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Ha-Gemi.app',
        icon=icon_path,
        bundle_identifier='com.lyw.hagemi',
        info_plist={
            'CFBundleName': 'Ha-Gemi',
            'CFBundleDisplayName': 'Ha-Gemi',
            'CFBundleIdentifier': 'com.lyw.hagemi',
            'CFBundleVersion': VERSION,
            'CFBundleShortVersionString': VERSION,
            'NSPrincipalClass': 'NSApplication',
            'NSAppleScriptEnabled': False,
            'NSHighResolutionCapable': True,
            'NSSupportsAutomaticGraphicsSwitching': True,
            'NSMicrophoneUsageDescription': '哈Gemi桌宠需要麦克风权限以支持语音互动与倾听。',
            'NSScreenCaptureUsageDescription': '哈Gemi桌宠需要屏幕录制权限以识别当前屏幕内容并进行傲娇互动吐槽。',
            'LSUIElement': False,
        },
    )
