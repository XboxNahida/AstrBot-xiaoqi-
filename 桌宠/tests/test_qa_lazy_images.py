
from cloud_plugin.astrbot_plugin_desktop_link.context_policy import ContextPlanner
from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for
from qa_session import QASession
from tests.test_qa_session import controller


def test_snapshot_defers_image_loading_and_only_reads_selected_rounds(tmp_path, monkeypatch):
    original, _ = controller()
    state = QASession(original, tmp_path)
    identity = state.current
    state.append(identity, 'user', '旧' * 10000, [b'never-read'])
    state.append(identity, 'assistant', '旧答')
    state.append(identity, 'user', '当前')
    def forbidden(_id):
        raise AssertionError('不应在主线程读取历史图片')
    monkeypatch.setattr(state.store, 'image', forbidden)
    job = state.prepare(defer_images=True)
    # 先按纯文字下界判断旧轮超限，旧图完全不读取/解码。
    policy = limits_for({'model': 'unknown'})
    result = ContextPlanner().prepare(job['messages'], job['system'], policy, image_reader=forbidden)
    assert result['stats']['dropped_rounds'] == 1
    assert state.store.messages(identity)[0]['images']
    state.store.close()
