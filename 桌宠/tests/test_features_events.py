"""动作意图复用、去重及主动文字和免打扰独立。"""
from pet_features.events import EventGate, action_key, feed_event


def test_food_reuses_available_actions_and_no_unknown_key():
    actions = {"happy": {"title": "开心"}, "gemini:吃蛋糕": {"title": "吃蛋糕"}}
    assert action_key("eat", actions) == "gemini:吃蛋糕"
    assert action_key("missing", actions) == "happy"
    assert feed_event("fish")["manual"] is True
    assert feed_event("fish")["text"]


def test_dedup_dnd_bounded_and_manual_never_hidden():
    gate = EventGate()
    event = {"id": "a", "source": "system", "text": "温度高", "ttl": 60, "created": 100, "manual": False}
    assert gate.accept(event, 101, dnd=True) is None
    assert gate.accept(dict(event, manual=True), 102, dnd=True)["text"] == "温度高"
    assert gate.accept(event, 103) is None
    assert "1" in gate.flush()["text"]
    assert gate.accept(dict(event, id="old"), 200) is None
    for i in range(500):
        gate.accept(dict(event, id=str(i), created=200), 200, dnd=True)
    assert len(gate.pending) <= 50
