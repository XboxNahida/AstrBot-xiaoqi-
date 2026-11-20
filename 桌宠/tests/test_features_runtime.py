"""主线程事件调度：文字不受语音概率影响，用户任务优先。"""
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from pet_features.config import FeaturesStore
from pet_features.runtime import FeatureRuntime
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from ui.features_settings import FeaturesSettingsPanel

APP = QApplication.instance() or QApplication([])


class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.texts, self.played = [], []
        self.actions = {"happy": {"title": "开心"}}

    def show_critique(self, text):
        self.texts.append(text)

    def play_action(self, key, **kwargs):
        self.played.append(key)


def test_context_menu_adds_balance_without_removing_original_items(tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / 'features.json'))
    pet = Pet()
    def original_menu():
        menu = QMenu(pet)
        menu.setStyleSheet('QMenu { background: #21182f; }')
        for name in ('立即锐评', '设置中心', '退出'):
            menu.addAction(name)
        return menu
    pet._build_context_menu = original_menu
    controller = SimpleNamespace(pet=pet, settings_win=SimpleNamespace(open_features=lambda: None),
                                 speaker=SimpleNamespace(config={}), smart=None, _is_analyzing=False)
    # 只测试装配，避免扩展初始apply需要完整原控制器。
    controller_no_settings = SimpleNamespace(**{k: v for k, v in vars(controller).items() if k != 'settings_win'})
    runtime = FeatureRuntime(controller_no_settings, panel, tmp_path)
    runtime.controller.settings_win = controller.settings_win
    calls = []
    runtime.request = calls.append
    try:
        menu = pet._build_context_menu()
        actions = menu.actions()
        assert [a.text() for a in actions[:3]] == ['立即锐评', '设置中心', '退出']
        assert menu.styleSheet() == original_menu().styleSheet()
        balance = next((a for a in actions if a.text() == '查询余额'), None)
        assert balance is not None
        balance.trigger()
        assert calls == ['balance:refresh']
        extension = next(a.menu() for a in actions if a.text() == '扩展功能')
        next(a for a in extension.actions() if a.text() == '用量统计').trigger()
        assert calls == ['balance:refresh', 'usage:report']
        before = panel.store.load()
        mirror = next((a for a in actions if a.text() == '↔ 水平镜像'), None)
        assert mirror is not None and mirror.isCheckable() and not mirror.isChecked()
        mirror.trigger()
        assert pet.manual_mirrored is True
        saved = panel.store.load()
        assert saved['resources']['mirrored'] is True
        saved['resources'].pop('mirrored')
        before['resources'].pop('mirrored')
        assert saved == before
        reopened = pet._build_context_menu()
        mirror = next(a for a in reopened.actions() if a.text() == '↔ 水平镜像')
        assert mirror.isChecked()
        mirror.trigger()
        assert not panel.store.load()['resources']['mirrored']
        reopened.deleteLater()
    finally:
        runtime.close()
        pet.close()
        panel.deleteLater()


@pytest.mark.parametrize('action', ['balance:refresh', 'usage:report', 'balance:ledger', 'balance:adjust'])
def test_query_results_and_failures_use_bubble_without_dialog(tmp_path, monkeypatch, action):
    from PyQt6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda parent, title, text: shown.append((title, text)))
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / 'features.json'))
    pet = Pet()
    runtime = FeatureRuntime(SimpleNamespace(pet=pet), panel, tmp_path)
    try:
        assert not panel.isVisible()
        runtime._complete((action, runtime.generation, {'text': '令牌无限额度'}, ''))
        assert pet.texts == ['令牌无限额度'] and shown == []
        text = '累计输入1234，输出60。\n' * 30
        runtime._complete((action, runtime.generation, {'text': text}, ''))
        assert pet.texts[-1] == text and shown == []
        assert panel.feedback.text() == text
        assert runtime.user_text_until > time.monotonic()
        runtime._complete((action, runtime.generation, {}, '查询失败：配置无效'))
        assert pet.texts[-1] == '查询失败：配置无效' and shown == []
        count = len(pet.texts)
        runtime._complete((action, runtime.generation - 1, {'text': '过期结果'}, ''))
        assert len(pet.texts) == count
    finally:
        runtime.close()
        pet.close()
        panel.deleteLater()


@pytest.mark.parametrize('dynamic', [False, True])
def test_actual_original_menus_keep_items_and_balance_qa_actions(original_config, tmp_path, dynamic):
    from qa_menu import install_menu
    from ui.dynamic_pet_widget import DynamicDesktopPetWidget
    from ui.pet_widget import DesktopPetWidget

    pet = DynamicDesktopPetWidget() if dynamic else DesktopPetWidget()
    original = [a.text() for a in pet._build_context_menu().actions()]
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / 'features.json'))
    controller = SimpleNamespace(pet=pet, screenshot_qa=SimpleNamespace(open=lambda: None))
    runtime = FeatureRuntime(controller, panel, tmp_path)
    controller.settings_win = SimpleNamespace(open_features=lambda: None)
    install_menu(pet, controller)
    calls = []
    runtime.request = calls.append
    try:
        menu = pet._build_context_menu()
        labels = [a.text() for a in menu.actions()]
        assert all(label in labels for label in original)
        assert any('截图答疑' in label for label in labels)
        next(a for a in menu.actions() if a.text() == '查询余额').trigger()
        assert calls == ['balance:refresh']
        runtime._complete(('balance:refresh', runtime.generation, {'text': '当前令牌无限额度，账户余额未返回。'}, ''))
        APP.processEvents()
        assert pet.bubble.isVisible()
        assert pet.bubble._full_text == '当前令牌无限额度，账户余额未返回。'
        pet.bubble.close_btn.click()
        assert not pet.bubble.isVisible()
    finally:
        runtime.close()
        pet.close()
        pet.bubble.hide()
        pet.interaction_dialog.hide()
        panel.deleteLater()


def test_runtime_feed_preserves_text_without_voice_and_busy_action(tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    pet = Pet()
    speaker = SimpleNamespace(config={}, speak_async=lambda _t: (_ for _ in ()).throw(AssertionError("不应播报")))
    controller = SimpleNamespace(pet=pet, speaker=speaker, smart=SimpleNamespace(blocked=lambda: False), _is_analyzing=True)
    runtime = FeatureRuntime(controller, panel, tmp_path)
    try:
        runtime.request("feed:fish")
        deadline = time.monotonic() + 3
        while not pet.texts and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.01)
        assert pet.texts
        assert pet.played == []
    finally:
        runtime.close()
        pet.close()
        panel.deleteLater()


def test_recent_user_reply_and_priority_not_overwritten(tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    pet = Pet()
    speaker = SimpleNamespace(config={})
    controller = SimpleNamespace(pet=pet, speaker=speaker, smart=SimpleNamespace(blocked=lambda: False), _is_analyzing=False)
    runtime = FeatureRuntime(controller, panel, tmp_path)
    try:
        runtime._protect_reply("用户回复")
        event = {"id": "song", "source": "media", "text": "歌词", "action": "happy", "priority": 10}
        runtime._complete(("poll", runtime.generation, {"events": [event]}, ""))
        assert pet.texts == []
        runtime.user_text_until = 0
        runtime.gate.pending.clear()
        urgent = {"id": "urgent", "source": "agent", "text": "", "action": "error", "priority": 80}
        low = {"id": "low", "source": "media", "text": "", "action": "happy", "priority": 10}
        runtime._complete(("poll", runtime.generation, {"events": [urgent, low]}, ""))
        assert len(pet.played) == 1
    finally:
        runtime.close()
        pet.hide()
        panel.deleteLater()
