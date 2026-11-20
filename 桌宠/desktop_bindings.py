"""原快捷键与扩展动作的装配，默认行为保持不变。"""
from hotkey_config import ConfiguredHotkeys, HotkeyStore
from ui.hotkey_settings import HotkeySettingsPanel


def install_bindings(runtime, root):
    controller, window, pet = runtime.controller, runtime.window, runtime.pet
    store = HotkeyStore(root / "hotkey_settings.json")
    from hotkey_config import DEFAULT_KEYS
    try:
        bindings = store.load()
    except (OSError, ValueError):
        bindings = dict(DEFAULT_KEYS)
    panel = HotkeySettingsPanel(bindings)
    window.hotkey_panel = panel
    window.hotkey_tab = window.tabs.insertTab(window.extension_tab, panel, "按键绑定")
    if hasattr(window, "desktop_tab") and window.desktop_tab >= window.hotkey_tab:
        window.desktop_tab += 1
    window.extension_tab += 1
    previous = controller.hotkeys
    manager = ConfiguredHotkeys({
        "critique": controller.trigger_manual_critique, "voice": controller.voice_input.request_toggle.emit,
        "mute": controller.toggle_tts, "outfit": pet.cycle_next_outfit,
        "qa": lambda: controller.screenshot_qa.open(),
        "desktop": lambda: getattr(getattr(controller, "desktop_control", None), "open", lambda: None)(),
        "desktop_voice": lambda: getattr(getattr(controller, "desktop_control", None), "voice", lambda: None)(),
        "desktop_stop": lambda: getattr(getattr(controller, "desktop_control", None), "stop", lambda: None)(),
    })
    # 录音入口原本经request_toggle信号进入主线程。
    manager.callbacks["voice"] = controller.voice_input.request_toggle.emit

    def apply(values, persist=True):
        extras = controller.features.cfg["hotkeys"]
        extra = [rule.get("keys", "") for rule in extras["rules"] if rule.get("enabled")]
        try:
            callbacks = {}
            for rule in extras['rules']:
                if extras['enabled'] and rule.get('enabled') and rule.get('keys'):
                    command = rule.get('action', 'phrases:preview')
                    callbacks[rule['keys']] = lambda action=command: controller.features.command_received.emit(action)
            manager.apply(values, extra if extras["enabled"] else (), extra_callbacks=callbacks, persist=store.save if persist else None)
            if controller.hotkeys is previous:
                previous.stop()
                controller.hotkeys = manager
            if controller.features.hotkey_listener:
                controller.features.hotkey_listener.stop()
                controller.features.hotkey_listener = None
            pet.shortcut_labels = dict(values)
            panel.feedback.setText("按键绑定已生效。")
            return True
        except (ValueError, OSError, RuntimeError) as exc:
            panel.feedback.setText("按键未更新：" + str(exc))
            controller.features.panel.feedback.setText('按键冲突或注册失败，已保留旧绑定：' + str(exc))
            return False

    panel.requested.connect(apply)
    apply(bindings, False)
    controller.features.rebind_hotkeys = lambda: apply(manager.bindings or bindings, False)
    return manager
