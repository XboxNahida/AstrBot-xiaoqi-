"""追加页保存、规则编辑与不撑宽原窗口。"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from pet_features.config import FeaturesStore
from ui.features_settings import FeaturesSettingsPanel

APP = QApplication.instance() or QApplication([])


def test_balance_auto_default_follow_profile_and_wheel_guard(tmp_path):
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / 'features.json'),
                                  lambda: [{'id': 'p', 'name': '我的站点'}])
    panel.select_section('balance')
    provider = panel.controls[('balance', 'provider')]
    profile = panel.controls[('balance', 'profile_id')]
    assert provider.currentData() == 'auto'
    assert '自动识别' in provider.currentText()
    assert profile.currentData() == ''
    assert '当前' in profile.currentText()
    for control in (provider, profile):
        event = QWheelEvent(QPointF(2, 2), QPointF(2, 2), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        old = control.currentData()
        APP.sendEvent(control, event)
        assert control.currentData() == old
    assert panel.save_settings()
    assert panel.store.load()['balance']['provider'] == 'auto'


def test_interaction_title_and_independent_switches(tmp_path):
    from PyQt6.QtWidgets import QCheckBox, QLabel

    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    panel.select_section("interaction")
    assert panel.findChild(QLabel, "section_interaction").text() == "交互与物理"
    for key in ("enabled", "fling_enabled", "bounce_enabled", "rope_enabled",
                "gravity_enabled", "sling_enabled", "click_enabled", "trail_enabled", "auto_sleep"):
        control = panel.controls[("interaction", key)]
        assert isinstance(control, QCheckBox)
        control.setChecked(not control.isChecked())
    assert panel.save_settings()
    assert panel.store.load()["interaction"] == panel.capture()["interaction"]


def test_interaction_numeric_bounds_and_lossless_save(tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    expected = panel.config["interaction"].copy()
    panel.select_section("interaction")
    assert panel.capture()["interaction"] == expected
    for key, low, high in (("gravity", 0, 3000), ("restitution", 0, 1),
                           ("friction", 0.8, 1), ("sling_power", 1, 30),
                           ("sleep_minutes", 1, 120)):
        control = panel.controls[("interaction", key)]
        assert (control.minimum(), control.maximum()) == (low, high)
        for boundary in (low, high):
            control.setValue(boundary)
            assert panel.save_settings()
            assert panel.store.load()["interaction"][key] == boundary
        control.setValue(expected[key])
    assert panel.save_settings()
    reopened = FeaturesSettingsPanel(panel.store)
    reopened.select_section("interaction")
    assert reopened.capture()["interaction"] == expected


def test_interaction_spinboxes_ignore_wheel_including_editors(tmp_path):
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QAbstractSpinBox, QLineEdit

    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    panel.select_section("interaction")
    before = panel.capture()
    for (section, _key), control in panel.controls.items():
        if section == "interaction" and isinstance(control, QAbstractSpinBox):
            for target in [control] + control.findChildren(QLineEdit):
                event = QWheelEvent(QPointF(2, 2), QPointF(2, 2), QPoint(), QPoint(0, 120),
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.NoScrollPhase, False)
                APP.sendEvent(target, event)
                assert not event.isAccepted()
    assert panel.capture() == before


def test_manual_city_has_stable_id_and_geographic_bounds():
    from ui.features_rules import RuleDialog

    dialog = RuleDialog("weather")
    city = dialog.value()
    assert isinstance(city.get("id"), str) and city["id"]
    assert dialog.value()["id"] == city["id"]
    assert dialog.fields["latitude"].minimum() == -90
    assert dialog.fields["latitude"].maximum() == 90
    assert dialog.fields["longitude"].minimum() == -180
    assert dialog.fields["longitude"].maximum() == 180
    existing = RuleDialog("weather", {"id": 1796236, "name": "上海", "latitude": 31.23, "longitude": 121.47})
    assert existing.value()["id"] == 1796236


def test_city_unknown_coordinates_stay_unknown_until_entered():
    from ui.features_rules import RuleDialog

    dialog = RuleDialog("weather", {"id": "101020100", "name": "上海", "latitude": None, "longitude": None})
    assert dialog.value()["latitude"] is None
    assert dialog.value()["longitude"] is None
    assert "未知" in dialog.fields["latitude"].lineEdit().placeholderText()
    dialog.fields["latitude"].setValue(0)
    dialog.fields["longitude"].setValue(121.47)
    assert dialog.value()["latitude"] == 0
    assert dialog.value()["longitude"] == 121.47
    assert dialog.value()["id"] == "101020100"


def test_rules_use_forms_and_preserve_unknown_fields(tmp_path):
    from ui.features_rules import RuleDialog, RuleListEditor
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    panel.select_section("reminders")
    editor = panel.controls[("reminders", "rules")]
    assert isinstance(editor, RuleListEditor)
    rule = {"id": "water", "enabled": True, "when": "weekly", "days": [0, 4], "time": "10:15", "text": "喝水", "extra": 7}
    editor.set_value([rule])
    dialog = RuleDialog("reminders", rule, panel)
    assert dialog.value() == {**rule, "every": 60}
    assert panel.capture()["reminders"]["rules"] == [rule]
    editor.value()[0]["text"] = "不应污染"
    assert editor.value()[0]["text"] == "喝水"


def test_resource_and_profile_choices_preserve_saved_identifiers(tmp_path):
    from PyQt6.QtWidgets import QComboBox
    store = FeaturesStore(tmp_path / "features.json")
    config = store.load()
    config["resources"].update(selected="custom", items=[{"id": "custom", "name": "自建形象", "path": "x.png"}])
    config["balance"]["profile_id"] = "removed"
    store.save(config)
    panel = FeaturesSettingsPanel(store, lambda: [{"id": "p1", "name": "常用模型"}])
    panel.select_section("resources")
    selected = panel.controls[("resources", "selected")]
    assert isinstance(selected, QComboBox)
    assert selected.currentData() == "custom"
    assert "自建形象" in selected.currentText()
    panel.select_section("balance")
    assert panel.controls[("balance", "profile_id")].currentData() == "removed"
    assert panel.capture()["resources"]["items"] == config["resources"]["items"]


def test_rule_forms_all_types_and_wheel_guards(tmp_path):
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QLineEdit

    from ui.features_rules import RuleDialog
    for section in ("apps", "reminders", "hotkeys", "phrases", "balance", "weather"):
        dialog = RuleDialog(section)
        assert dialog.value()["enabled"] is True
        for widget in dialog.fields.values():
            if hasattr(widget, "value") or hasattr(widget, "currentData") or hasattr(widget, "time"):
                before = dialog.value()
                for target in [widget] + widget.findChildren(QLineEdit):
                    event = QWheelEvent(QPointF(2, 2), QPointF(2, 2), QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                    APP.sendEvent(target, event)
                assert dialog.value() == before
        dialog.deleteLater()


def test_lists_add_edit_delete_without_json(monkeypatch):
    from PyQt6.QtWidgets import QDialog

    from ui.features_rules import RuleDialog, RuleListEditor
    editor = RuleListEditor("phrases", [])
    monkeypatch.setattr(RuleDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(RuleDialog, "value", lambda self: {"enabled": True, "category": "idle", "text": "休息一下"})
    editor.add_rule()
    assert editor.value()[0]["text"] == "休息一下"
    editor.list.setCurrentRow(0)
    monkeypatch.setattr(RuleDialog, "value", lambda self: {"enabled": False, "category": "return", "text": "欢迎回来"})
    editor.edit_rule()
    assert editor.value()[0]["enabled"] is False
    editor.remove_rule()
    assert editor.value() == []


def test_all_sections_have_titles_and_fit_small_window(tmp_path):
    from PyQt6.QtWidgets import QLabel

    from pet_features.ui_schema import CATEGORIES
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    panel.resize(640, 420)
    panel.show()
    for index, (_, sections) in enumerate(CATEGORIES):
        panel.navigation.setCurrentRow(index)
        APP.processEvents()
        assert panel.current_scroll.horizontalScrollBar().maximum() == 0
        titles = [label.objectName() for label in panel.current_scroll.findChildren(QLabel)]
        assert all("section_" + section in titles for section in sections)
    panel.close()


def test_panel_has_all_categories_saves_and_no_horizontal_overflow(tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    panel.resize(740, 450)
    panel.show()
    try:
        assert panel.navigation.count() == 11
        panel.select_section("weather")
        APP.processEvents()
        assert panel.current_scroll.horizontalScrollBar().maximum() == 0
        panel.controls[("weather", "enabled")].setChecked(True)
        panel.save_settings()
        assert panel.store.load()["weather"]["enabled"] is True
        panel.select_section("feeding")
        assert panel.controls[("feeding", "enabled")].isChecked()
    finally:
        panel.hide()
        panel.deleteLater()
        APP.processEvents()
