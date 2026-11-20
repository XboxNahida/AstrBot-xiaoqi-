"""独立于 Qt 的甩抛速度估计与有界运动积分。时间单位为秒。"""
import math
from collections import deque
from dataclasses import dataclass

MAX_SPEED = 2000.0
STOP_SPEED = 12.0


def _finite(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _limit(vx, vy):
    speed = math.hypot(vx, vy)
    if speed > MAX_SPEED:
        return vx / speed * MAX_SPEED, vy / speed * MAX_SPEED
    return vx, vy


class VelocityTracker:
    """仅采纳最近 120 毫秒；停留或慢放不产生甩抛。"""

    def __init__(self):
        self._samples = deque(maxlen=128)

    def clear(self):
        self._samples.clear()

    def sample(self, x, y, now):
        values = tuple(_finite(v, None) for v in (x, y, now))
        if None in values:
            return
        x, y, now = values
        if self._samples and now < self._samples[-1][2]:
            self.clear()
        if self._samples and now == self._samples[-1][2]:
            self._samples[-1] = (x, y, now)
        else:
            self._samples.append((x, y, now))
        # 保留窗口之前的一个基准点，避免事件合并/解码阻塞后速度归零。
        while len(self._samples) > 2 and now - self._samples[1][2] >= .12:
            self._samples.popleft()

    def velocity(self, now):
        now = _finite(now, None)
        if now is None or len(self._samples) < 2:
            return 0.0, 0.0
        x0, y0, t0 = self._samples[0]
        x1, y1, t1 = self._samples[-1]
        if t0 < t1 - .12:
            xn, yn, tn = self._samples[1]
            ratio = min(1., (t1 - .12 - t0) / (tn - t0))
            x0, y0, t0 = x0 * (1 - ratio) + xn * ratio, y0 * (1 - ratio) + yn * ratio, t1 - .12
        if now < t1 or now - t1 >= .15 or t1 - t0 < .001:
            return 0.0, 0.0
        vx, vy = (x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0)
        if not math.isfinite(vx) or not math.isfinite(vy):
            return 0.0, 0.0
        if math.hypot(vx, vy) < 180:
            return 0.0, 0.0
        return _limit(vx, vy)


@dataclass
class MotionState:
    """边界为窗口左上角可达矩形；反弹关闭时允许自由越界。"""

    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    active: bool = False

    def launch(self, x, y, vx, vy):
        values = tuple(_finite(v, None) for v in (x, y, vx, vy))
        if None in values:
            self.stop()
            return
        self.x, self.y, vx, vy = values
        self.vx, self.vy = _limit(vx, vy)
        self.active = math.hypot(self.vx, self.vy) >= STOP_SPEED

    def stop(self):
        self.active = False
        self.vx = self.vy = 0.0

    def step(self, dt, bounds, cfg):
        result = {'bounced': False, 'impact': 0.0, 'resting': not self.active}
        dt = _finite(dt)
        if not self.active or dt <= 0:
            return result
        if any(_finite(v, None) is None for v in (self.x, self.y, self.vx, self.vy)):
            self.x, self.y = _finite(self.x), _finite(self.y)
            self.stop()
            result['resting'] = True
            return result
        self.vx, self.vy = _limit(self.vx, self.vy)
        dt = min(dt, .25)  # 暂停/卡顿不补算长时间，最多 60 个积分小步。
        gravity = max(0.0, min(5000.0, _finite(cfg.get('gravity', 1000), 1000)))
        if not cfg.get('gravity_enabled', False):
            gravity = 0.0
        friction = max(.01, min(1.0, _finite(cfg.get('friction', .985), .985)))
        restitution = max(0.0, min(1.0, _finite(cfg.get('restitution', .65), .65)))
        edges = None
        if cfg.get('bounce_enabled', True):
            try:
                edges = tuple(_finite(v, None) for v in bounds)
                if len(edges) != 4 or None in edges or edges[0] > edges[2] or edges[1] > edges[3]:
                    edges = None
            except TypeError:
                edges = None
        count = max(1, math.ceil(dt * 240))
        h = dt / count
        decay = -60 * math.log(friction)
        for _ in range(count):
            old_x, old_y = self.x, self.y
            if edges:
                for value, speed, low, high in ((old_x, "vx", edges[0], edges[2]),
                                                (old_y, "vy", edges[1], edges[3])):
                    velocity = getattr(self, speed)
                    # 主动甩出已越过边缘时只反向速度，绝不传送窗口坐标。
                    if (value < low and velocity < 0) or (value > high and velocity > 0):
                        result['bounced'] = True
                        result['impact'] = max(result['impact'], abs(velocity))
                        setattr(self, speed, -velocity * restitution)
            grounded = bool(edges and old_y == edges[3] and abs(self.vy) < STOP_SPEED)
            acceleration = 0.0 if grounded else gravity
            if decay:
                factor = math.exp(-decay * h)
                travel = -math.expm1(-decay * h) / decay
                self.x += self.vx * travel
                self.y += self.vy * travel + acceleration / decay * (h - travel)
                self.vx *= factor
                self.vy = self.vy * factor + acceleration * travel
            else:
                self.x += self.vx * h
                self.y += self.vy * h + .5 * acceleration * h * h
                self.vy += acceleration * h
            self.vx, self.vy = _limit(self.vx, self.vy)
            if edges:
                left, top, right, bottom = edges
                for pos, speed, old, low, high in (
                    ('x', 'vx', old_x, left, right), ('y', 'vy', old_y, top, bottom)
                ):
                    value = getattr(self, pos)
                    # 只处理从区间内穿出，不将屏外起点传送到屏内。
                    if low <= old <= high and (value < low or value > high):
                        impact = abs(getattr(self, speed))
                        result['bounced'] = True
                        result['impact'] = max(result['impact'], impact)
                        setattr(self, pos, low if value < low else high)
                        reflected = -getattr(self, speed) * restitution
                        if pos == 'y' and value > high and abs(reflected) < 45:
                            reflected = 0.0
                        setattr(self, speed, reflected)
            on_ground = bool(edges and self.y == edges[3] and self.vy == 0)
            if math.hypot(self.vx, self.vy) < STOP_SPEED and (not gravity or on_ground):
                self.stop()
                break
        result['resting'] = not self.active
        return result
