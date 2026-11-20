import sys
import threading
from typing import Callable, Optional
from pynput import keyboard

class GlobalHotkeyManager:
    def __init__(
        self,
        on_trigger_critique: Callable[[], None],
        on_toggle_mute: Optional[Callable[[], None]] = None,
        on_switch_outfit: Optional[Callable[[], None]] = None,
        on_toggle_voice_input: Optional[Callable[[], None]] = None
    ):
        self.on_trigger_critique = on_trigger_critique
        self.on_toggle_mute = on_toggle_mute
        self.on_switch_outfit = on_switch_outfit
        self.on_toggle_voice_input = on_toggle_voice_input
        self._listener: Optional[keyboard.GlobalHotKeys] = None

    def start(self):
        hotkeys = {
            '<ctrl>+<f9>': self._handle_critique
        }
        if sys.platform == 'darwin':
            hotkeys['<cmd>+<f9>'] = self._handle_critique

        if self.on_toggle_mute:
            hotkeys['<ctrl>+<f10>'] = self._handle_mute
            if sys.platform == 'darwin':
                hotkeys['<cmd>+<f10>'] = self._handle_mute

        if self.on_switch_outfit:
            hotkeys['<ctrl>+<f11>'] = self._handle_switch_outfit
            if sys.platform == 'darwin':
                hotkeys['<cmd>+<f11>'] = self._handle_switch_outfit

        if self.on_toggle_voice_input:
            hotkeys['<ctrl>+<f1>'] = self._handle_voice_input
            if sys.platform == 'darwin':
                hotkeys['<cmd>+<f1>'] = self._handle_voice_input

        try:
            self._listener = keyboard.GlobalHotKeys(hotkeys)
            self._listener.daemon = True
            self._listener.start()
            if sys.platform == 'darwin':
                print("[Hotkey] Registered <ctrl>/<cmd>+<f1> (Voice Input), +<f9> (Critique), +<f10> (Mute), +<f11> (Switch Outfit)")
            else:
                print("[Hotkey] Registered <ctrl>+<f1> (Voice Input), <ctrl>+<f9> (Critique), <ctrl>+<f10> (Mute), <ctrl>+<f11> (Switch Outfit)")
        except Exception as e:
            print(f"[Hotkey] Warning: Could not start global hotkey listener: {e}")
            if sys.platform == 'darwin':
                print("[Hotkey] Note: On macOS, global hotkeys require Accessibility permissions in System Settings -> Privacy & Security -> Accessibility.")

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass

    def _handle_critique(self):
        print("[Hotkey] Critique hotkey pressed -> Instant Critique triggered!")
        if self.on_trigger_critique:
            self.on_trigger_critique()

    def _handle_mute(self):
        print("[Hotkey] Mute hotkey pressed -> Toggle Mute triggered!")
        if self.on_toggle_mute:
            self.on_toggle_mute()

    def _handle_switch_outfit(self):
        print("[Hotkey] Switch outfit hotkey pressed -> Switch Outfit triggered!")
        if self.on_switch_outfit:
            self.on_switch_outfit()

    def _handle_voice_input(self):
        print("[Hotkey] Voice input hotkey pressed -> Toggle Voice Input")
        if self.on_toggle_voice_input:
            self.on_toggle_voice_input()
