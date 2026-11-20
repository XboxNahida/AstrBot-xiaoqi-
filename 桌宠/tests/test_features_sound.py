"""本地音效复用总开关/设备/免打扰与单通道，禁止另开播放器。"""
from tests.test_smart_audio import rig as rig  # noqa: PLC0414


def test_local_sound_obeys_same_output_and_no_tts(rig, tmp_path):
    speaker, original, cfg, backend, generated = rig
    path = tmp_path / "sound.wav"
    path.write_bytes(b"local-audio")
    cfg.update(device_filter_enabled=True, allowed_devices=["bb"])
    speaker.apply_config(cfg)
    assert not speaker.play_local_file(path, 30)
    cfg["allowed_devices"] = ["aa"]
    speaker.apply_config(cfg)
    assert speaker.play_local_file(path, 30)
    assert backend.played == [(b"local-audio", "aa", 30)]
    assert not speaker.play_local_file(path, 30)
    assert not generated
    speaker.stop()
    speaker.set_blocker(lambda: True)
    assert not speaker.play_local_file(path, 30)
    speaker.set_blocker(lambda: False)
    original.enabled = False
    assert not speaker.play_local_file(path, 30)
