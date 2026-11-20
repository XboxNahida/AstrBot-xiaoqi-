import base64
import io

import pytest
from PIL import Image

from cloud_plugin.astrbot_plugin_desktop_link.context_policy import ContextPlanner
from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for


def policy(**values):
    return dict(limits_for({'model': 'unknown'}), **values)


def test_sliding_window_drops_complete_old_rounds_leaves_history_unchanged():
    history = [{'role': 'user', 'content': '旧问题' * 1000}, {'role': 'assistant', 'content': '旧答案' * 1000},
               {'role': 'user', 'content': '最近问题'}, {'role': 'assistant', 'content': '最近答案'},
               {'role': 'user', 'content': '当前完整问题'}]
    planner = ContextPlanner()
    prepared = planner.prepare(history, '固定人设', policy())
    assert prepared['messages'] == history[2:]
    assert len(history) == 5 and prepared['stats']['dropped_rounds'] == 1
    assert prepared['stats']['input_tokens_estimate'] + prepared['stats']['output_tokens'] + prepared['stats']['safety_tokens'] <= 8192
    again = planner.prepare(history, '固定人设', policy())
    assert prepared['prefix_hash'] == again['prefix_hash']


def test_current_question_never_silently_cut_and_byte_budget_is_actual_json():
    planner = ContextPlanner()
    with pytest.raises(ValueError, match='当前问题'):
        planner.prepare([{'role': 'user', 'content': '字' * 20000}], '人设', policy())
    history = [{'role': 'user', 'content': '😀' * 10000}, {'role': 'assistant', 'content': '答案'},
               {'role': 'user', 'content': '当前'}]
    prepared = planner.prepare(history, '人设', policy(context_tokens=1000000, max_bytes=100000))
    assert prepared['messages'] == history[-1:] and prepared['stats']['wire_bytes'] <= 100000


def test_normalized_images_keep_latest_and_are_stable_across_followups():
    image = Image.new('RGB', (3000, 2000), 'white')
    data = io.BytesIO()
    image.save(data, 'BMP')
    url = 'data:image/bmp;base64,' + base64.b64encode(data.getvalue()).decode()
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': '题目'},
               {'type': 'image_url', 'image_url': {'url': url}}]}]
    planner = ContextPlanner()
    first = planner.prepare(history, '人设', policy())
    second = planner.prepare(history + [{'role': 'assistant', 'content': '答'},
                              {'role': 'user', 'content': '继续'}], '人设', policy())
    assert first['messages'][0] == second['messages'][0]
    assert second['stats']['image_cache_hits'] >= 1
    assert first['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
