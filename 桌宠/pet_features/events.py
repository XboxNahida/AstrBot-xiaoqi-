"""本地事件和动作意图；不生成模型请求或隐含桌面移动。"""
import random
import time
from collections import OrderedDict, deque

FOODS = {"fish": ("小鱼干", ["小鱼干！我的最爱！", "咔嚓咔嚓……谢谢投喂！"]),
         "cake": ("蛋糕", ["蛋糕！甜到冒泡泡～", "蛋糕好吃，谢谢你！"]),
         "candy": ("棒棒糖", ["棒棒糖！转圈圈～", "嘎嘣脆，好吃！"]),
         "dango": ("团子", ["三色团子！软乎乎～", "团子糯叽叽，爱了爱了！"]),
         "diamond": ("钻石", ["钻石？！这能吃吗……真香！", "收到钻石啦，闪闪发光！"])}

INTENTS = {"eat": ("吃", "进食", "零食"), "happy": ("开心", "欢呼", "庆祝"),
           "weather": ("天气", "雨", "晴"), "sing": ("唱歌", "音乐", "耳机", "跳舞"),
           "working": ("思考",), "tool_pwsh": ("代码", "键盘"), "tool_edit": ("代码", "写"),
           "tool_read": ("阅读", "看书"), "tool_web": ("搜索", "望远镜"),
           "approval": ("疑问", "疑惑", "举手"), "error": ("惊吓", "吓", "慌"),
           "done": ("庆祝", "开心"), "start": ("打招呼", "招手"), "idle": ("待机",),
           "reminder": ("闹钟", "提醒", "举手"), "sleep": ("睡", "困"), "return": ("招手", "欢迎")}


def action_key(intent, actions):
    for keyword in INTENTS.get(intent, ()):
        for key, row in actions.items():
            if keyword in row.get("title", key):
                return key
    fallback = {"working": "thinking", "tool_pwsh": "coding", "tool_edit": "coding",
                "error": "shocked", "idle": "idle"}.get(intent, "happy")
    return fallback if fallback in actions else next(iter(actions), None)


def feed_event(food):
    if food not in FOODS:
        raise ValueError("请选择有效的食物")
    return {"id": "feed:" + str(time.monotonic_ns()), "source": "feeding", "text": random.choice(FOODS[food][1]),
                "action": "eat", "priority": 50, "ttl": 10, "manual": True, "created": time.time()}


class EventGate:
    def __init__(self):
        self.seen = OrderedDict()
        self.pending = deque(maxlen=50)

    def accept(self, event, now, *, dnd=False):
        event = dict(event)
        if not event.get("manual") and now - event.get("created", now) > event.get("ttl", 60):
            return None
        key = str(event.get("id", ""))
        if not event.get("manual") and key in self.seen:
            return None
        self.seen[key] = now
        while len(self.seen) > 1000:
            self.seen.popitem(last=False)
        if dnd and not event.get("manual"):
            if event.get("text"):
                self.pending.append(event)
            return None
        return event

    def flush(self):
        if not self.pending:
            return None
        rows = list(self.pending)
        self.pending.clear()
        summary = "；".join(row.get("text", "")[:80] for row in rows[-3:])
        return {"id": "dnd:" + str(time.monotonic_ns()), "source": "reminders", "manual": False,
                    "action": "reminder", "priority": 60, "ttl": 60,
                    "text": f"免打扰期间共收到{len(rows)}条提醒：{summary}"}
