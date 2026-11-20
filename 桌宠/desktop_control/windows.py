"""Windows物理坐标观察和可取消输入适配器。"""
import base64
import ctypes
import io
import json
import logging
import math
import os
import threading
import time
from ctypes import wintypes

LIMIT = 10 * 1024 * 1024

KEYS = {'ctrl': 17, 'alt': 18, 'shift': 16, 'win': 91, 'enter': 13, 'tab': 9, 'esc': 27,
        'escape': 27, 'space': 32, 'backspace': 8, 'delete': 46,
        'left': 37, 'up': 38, 'right': 39, 'down': 40, 'home': 36, 'end': 35}


def validate_action(action):
    name = action.get('name')
    allowed = {'move': {'x', 'y'}, 'click': {'x', 'y', 'button', 'count'},
               'drag': {'x', 'y', 'to_x', 'to_y', 'button', 'duration'},
               'scroll': {'x', 'y', 'delta'}, 'type': {'text'}, 'hotkey': {'keys'}}
    if name not in allowed:
        raise ValueError('unknown_action')
    if set(action) - allowed[name] - {'name', 'request_id', 'observation_id'}:
        raise ValueError('unknown_argument')
    for key in ({'x', 'y'} if name in {'move', 'click', 'drag', 'scroll'} else set()) | ({'to_x', 'to_y'} if name == 'drag' else set()):
        value = action.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('invalid_coordinates')
    if name in {'click', 'drag'} and action.get('button', 'left') not in {'left', 'right', 'middle'}:
        raise ValueError('invalid_button')
    if name == 'click' and (type(action.get('count', 1)) is not int or not 1 <= action.get('count', 1) <= 3):
        raise ValueError('invalid_count')
    if name == 'drag':
        value = action.get('duration', .5)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 5:
            raise ValueError('invalid_duration')
    if name == 'scroll' and (type(action.get('delta')) is not int or not -12000 <= action['delta'] <= 12000):
        raise ValueError('invalid_delta')
    if name == 'type' and (not isinstance(action.get('text'), str) or len(action['text']) > 10000):
        raise ValueError('invalid_text')
    if name == 'hotkey':
        values = action.get('keys')
        if not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise ValueError('invalid_keys')
        for key in values:
            if not isinstance(key, str):
                raise ValueError('invalid_key')  # noqa: TRY004 -- 统一参数错误契约
            value = key.lower()
            if value not in KEYS and not (len(value) == 1 and value.isascii() and value.isalnum()) and not (value.startswith('f') and value[1:].isdigit() and 1 <= int(value[1:]) <= 24):
                raise ValueError('invalid_key')


def encode_capture(image, left, top, monitor_id, original=False):
    width, height = image.size
    image = image.convert('RGB')
    if not original:
        image.thumbnail((1440, 1440))
    stream = io.BytesIO()
    image.save(stream, format='PNG' if original else 'JPEG', **({} if original else {'quality': 85}))
    result = {'image_b64': base64.b64encode(stream.getvalue()).decode('ascii'),
                  'mime': 'image/png' if original else 'image/jpeg', 'width': image.width,
                  'height': image.height, 'left': left, 'top': top, 'monitor_id': monitor_id,
                  'physical_width': width, 'physical_height': height,
                  'source_width': width, 'source_height': height}
    if len(json.dumps(result).encode('utf-8')) > LIMIT - 4096:
        raise ValueError('image_too_large')
    return result


class WindowsDesktop:
    def __init__(self):
        self._user = ctypes.WinDLL('user32', use_last_error=True)
        self._user.GetForegroundWindow.restype = wintypes.HWND
        self._user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._held_keys = set()
        self._held_buttons = set()
        self._input_lock = threading.RLock()
        self._active_cancel = None

    def _dpi(self):
        # 每个工作线程使用物理像素，保留原Qt进程的DPI策略。
        function = getattr(self._user, 'SetThreadDpiAwarenessContext', None)
        if function:
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_void_p
            return function(ctypes.c_void_p(-4))
        return None

    def monitors(self):
        import mss
        previous = self._dpi()
        try:
            with mss.mss() as capture:
                monitors = [dict(id=i, **dict(m)) for i, m in enumerate(capture.monitors) if i]
            class MonitorInfo(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                            ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD),
                            ('szDevice', wintypes.WCHAR * 32)]
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
            def visit(handle, hdc, rect, data):
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                if self._user.GetMonitorInfoW(handle, ctypes.byref(info)):
                    for monitor in monitors:
                        if (monitor['left'], monitor['top']) == (info.rcMonitor.left, info.rcMonitor.top):
                            monitor['name'] = info.szDevice
                            try:
                                dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
                                function = ctypes.WinDLL('shcore').GetDpiForMonitor
                                function.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
                                if function(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                                    monitor.update(dpi=dpi_x.value, scale=dpi_x.value / 96)
                            except (OSError, AttributeError):
                                pass
                return True
            self._user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            self._user.EnumDisplayMonitors(None, None, callback_type(visit), 0)
            return monitors
        finally:
            if previous:
                self._user.SetThreadDpiAwarenessContext(previous)

    def capture(self, monitor_id=None, region=None, original=False):
        import mss
        from PIL import Image
        previous = self._dpi()
        try:
            with mss.mss() as capture:
                if monitor_id is not None and (isinstance(monitor_id, bool) or not isinstance(monitor_id, int) or not 0 < monitor_id < len(capture.monitors)):
                    raise ValueError('invalid_monitor')
                bounds = {key: capture.monitors[monitor_id or 0][key] for key in ('left', 'top', 'width', 'height')}
                target = dict(region) if region is not None else bounds
                if set(target) != {'left', 'top', 'width', 'height'} or any(isinstance(v, bool) or not isinstance(v, int) for v in target.values()):
                    raise ValueError('invalid_region')
                if target['width'] <= 0 or target['height'] <= 0 or target['width'] * target['height'] > 100_000_000:
                    raise ValueError('invalid_region')
                if any(target[k] < bounds[k] for k in ('left', 'top')) or any(target[k] + target[s] > bounds[k] + bounds[s] for k, s in (('left', 'width'), ('top', 'height'))):
                    raise ValueError('region_outside_desktop')
                shot = capture.grab(target)
                image = Image.frombytes('RGB', shot.size, shot.rgb)
                return encode_capture(image, target['left'], target['top'], monitor_id, original)
        finally:
            if previous:
                self._user.SetThreadDpiAwarenessContext(previous)

    def foreground(self):
        handle = self._user.GetForegroundWindow()
        pid = wintypes.DWORD()
        self._user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return {'hwnd': int(handle or 0), 'pid': pid.value}

    def _taskbar_handles(self):
        handles = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        def visit(hwnd, _data):
            name = ctypes.create_unicode_buffer(128)
            self._user.GetClassNameW(hwnd, name, len(name))
            if name.value in ('Shell_TrayWnd', 'Shell_SecondaryTrayWnd'):
                handles.append(hwnd)
            return True
        self._user.EnumWindows(callback_type(visit), 0)
        return handles

    def controls(self):
        try:
            import uiautomation as auto
        except ImportError:
            return {'available': False, 'reason': 'uiautomation_not_installed', 'items': []}
        previous = self._dpi()
        try:
            with auto.UIAutomationInitializerInThread():
                items = []
                visited = 0
                def walk(control, depth, taskbar=False):
                    nonlocal visited
                    if depth > (7 if taskbar else 4) or len(items) >= (60 if taskbar else 200) or visited >= 600:
                        return
                    visited += 1
                    rect = control.BoundingRectangle
                    kind = control.ControlTypeName
                    if ((not taskbar or kind in ('ButtonControl', 'MenuItemControl', 'TabItemControl') or 'TaskListButton' in control.ClassName)
                            and rect.right > rect.left and rect.bottom > rect.top):
                        items.append({'name': control.Name[:200], 'control_type': kind,
                                      'left': rect.left, 'top': rect.top, 'right': rect.right, 'bottom': rect.bottom,
                                      'scope': 'taskbar' if taskbar else 'foreground'})
                    for child in control.GetChildren():
                        walk(child, depth + 1, taskbar)
                for handle in self._taskbar_handles():
                    try:
                        walk(auto.ControlFromHandle(handle), 0, True)
                    except Exception as exc:  # noqa: BLE001 -- 单个任务栏提供程序不可用时仍保留其他控件。
                        logging.getLogger(__name__).debug('任务栏控件读取失败：%s', type(exc).__name__)
                visited = 0
                foreground = self.foreground()
                if foreground.get('pid') != os.getpid():
                    walk(auto.ControlFromHandle(foreground['hwnd']), 0)
                items.extend(self.desktop_icons())
                return {'available': True, 'items': items}
        except Exception as exc:  # noqa: BLE001 -- 可选COM提供程序失败明确降级，不伪造成功
            return {'available': False, 'reason': 'uiautomation_failed', 'error_type': type(exc).__name__, 'items': []}
        finally:
            if previous:
                self._user.SetThreadDpiAwarenessContext(previous)

    def _desktop_listviews(self):
        """只定位Windows Shell的桌面图标列表，不遍历其他应用。"""
        user, views = self._user, []
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowExW.restype = wintypes.HWND
        user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _data):
            name = ctypes.create_unicode_buffer(128)
            user.GetClassNameW(hwnd, name, len(name))
            if name.value in ('Progman', 'WorkerW'):
                view = user.FindWindowExW(hwnd, None, 'SHELLDLL_DefView', None)
                listing = user.FindWindowExW(view, None, 'SysListView32', None) if view else None
                if listing:
                    views.append((int(hwnd), int(listing)))
            return True
        user.EnumWindows(callback_type(visit), 0)
        return views

    def _desktop_icon_visible(self, listing, point):
        user = self._user
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        handle = user.WindowFromPoint(wintypes.POINT(round(point[0]), round(point[1])))
        return bool(handle and (handle == listing or user.IsChild(listing, handle)))

    def desktop_icons(self, point=None):
        """桌面图标身份与物理边界；命中被其他窗口遮挡时不返回。"""
        previous = self._dpi()
        try:
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread():
                items = []
                active = self.foreground()
                foreground = active.get('hwnd')
                for shell, listing in self._desktop_listviews():
                    if (foreground not in (shell, listing) and active.get('pid') != os.getpid()
                            and not self._user.IsChild(shell, foreground)):
                        continue
                    for control in auto.ControlFromHandle(listing).GetChildren()[:500]:
                        if control.ControlTypeName != 'ListItemControl' or getattr(control, 'IsOffscreen', False):
                            continue
                        rect = control.BoundingRectangle
                        if rect.right <= rect.left or rect.bottom <= rect.top:
                            continue
                        target = point if point is not None else ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2)
                        if not (rect.left <= target[0] < rect.right and rect.top <= target[1] < rect.bottom):
                            continue
                        if not self._desktop_icon_visible(listing, target):
                            continue
                        identity = control.GetRuntimeId()
                        if not identity:
                            continue
                        items.append({'scope': 'desktop', 'control_type': 'ListItemControl',
                                      'name': control.Name[:200], 'hwnd': listing, 'runtime_id': list(identity),
                                      'left': rect.left, 'top': rect.top, 'right': rect.right, 'bottom': rect.bottom})
                return items
        except Exception as exc:  # noqa: BLE001 -- 可选Shell/UIA失败则保留原像素复核。
            logging.getLogger(__name__).debug('桌面图标身份读取失败：%s', type(exc).__name__)
            return []
        finally:
            if previous:
                self._user.SetThreadDpiAwarenessContext(previous)

    def input_stamp(self):
        class LastInput(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
        value = LastInput()
        value.cbSize = ctypes.sizeof(value)
        if not self._user.GetLastInputInfo(ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.dwTime

    def _key(self, key, down, cancel=None):
        with self._input_lock:
            if down and cancel is not None and cancel.is_set():
                raise ValueError('cancelled')
            self._user.keybd_event(key, 0, 0 if down else 2, 0)
            (self._held_keys.add if down else self._held_keys.discard)(key)

    def _button(self, button, down, cancel=None):
        flags = {'left': (2, 4), 'right': (8, 16), 'middle': (32, 64)}
        if button not in flags:
            raise ValueError('invalid_button')
        with self._input_lock:
            if down and cancel is not None and cancel.is_set():
                raise ValueError('cancelled')
            self._user.mouse_event(flags[button][0 if down else 1], 0, 0, 0, 0)
            (self._held_buttons.add if down else self._held_buttons.discard)(button)

    def release_inputs(self):
        with self._input_lock:
            if self._active_cancel is not None:
                self._active_cancel.set()
            self._release_held()

    def _release_held(self):
        with self._input_lock:
            for key in tuple(self._held_keys):
                self._key(key, False)
            for button in tuple(self._held_buttons):
                self._button(button, False)

    def perform(self, action, cancel):
        validate_action(action)
        name = action.get('name')
        if name not in {'move', 'click', 'drag', 'scroll', 'type', 'hotkey'}:
            raise ValueError('unknown_action')
        def check():
            if cancel.is_set():
                raise ValueError('cancelled')
        def move(x, y):
            check()
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
                raise ValueError('invalid_coordinates')
            if not any(m['left'] <= x < m['left'] + m['width'] and m['top'] <= y < m['top'] + m['height'] for m in monitors):
                raise ValueError('coordinates_outside_desktop')
            with self._input_lock:
                check()
                if not self._user.SetCursorPos(round(x), round(y)):
                    raise ctypes.WinError(ctypes.get_last_error())
        previous = self._dpi()
        try:
            with self._input_lock:
                check()
                if self._active_cancel is not None:
                    raise ValueError('busy')
                self._active_cancel = cancel
            monitors = self.monitors()
            check()
            if name in {'move', 'click', 'drag', 'scroll'}:
                move(action['x'], action['y'])
            if name == 'click':
                count = action.get('count', 1)
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 3:
                    raise ValueError('invalid_count')
                for _ in range(count):
                    check()
                    self._button(action.get('button', 'left'), True, cancel)
                    self._button(action.get('button', 'left'), False)
            elif name == 'drag':
                duration = action.get('duration', .5)
                if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 5:
                    raise ValueError('invalid_duration')
                self._button(action.get('button', 'left'), True, cancel)
                started = time.monotonic()
                while True:
                    fraction = min(1, (time.monotonic() - started) / duration) if duration else 1
                    move(action['x'] + (action['to_x'] - action['x']) * fraction,
                         action['y'] + (action['to_y'] - action['y']) * fraction)
                    if fraction == 1:
                        break
                    if cancel.wait(.01):
                        check()
            elif name == 'scroll':
                delta = action.get('delta')
                if isinstance(delta, bool) or not isinstance(delta, int) or not -12000 <= delta <= 12000:
                    raise ValueError('invalid_delta')
                with self._input_lock:
                    check()
                    self._user.mouse_event(0x800, 0, 0, ctypes.c_uint32(delta).value, 0)
            elif name == 'hotkey':
                mapping = {'ctrl': 17, 'alt': 18, 'shift': 16, 'win': 91, 'enter': 13, 'tab': 9, 'esc': 27,
                           'escape': 27, 'space': 32, 'backspace': 8, 'delete': 46,
                           'left': 37, 'up': 38, 'right': 39, 'down': 40, 'home': 36, 'end': 35}
                keys = action.get('keys')
                if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
                    raise ValueError('invalid_keys')
                codes = []
                for key in keys:
                    value = str(key).lower()
                    if value in mapping:
                        codes.append(mapping[value])
                    elif len(value) == 1 and value.isascii() and value.isalnum():
                        codes.append(ord(value.upper()))
                    elif value.startswith('f') and value[1:].isdigit() and 1 <= int(value[1:]) <= 24:
                        codes.append(111 + int(value[1:]))
                    else:
                        raise ValueError('invalid_key')
                for code in codes:
                    check()
                    self._key(code, True, cancel)
            elif name == 'type':
                text = action.get('text')
                if not isinstance(text, str) or len(text) > 10000:
                    raise ValueError('invalid_text')
                # Unicode SendInput避免剪贴板覆盖，支持中文及代理对。
                class KeyInput(ctypes.Structure):
                    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
                class MouseInput(ctypes.Structure):
                    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
                class InputUnion(ctypes.Union):
                    _fields_ = [('ki', KeyInput), ('mi', MouseInput)]
                class Input(ctypes.Structure):
                    _fields_ = [('type', wintypes.DWORD), ('value', InputUnion)]
                encoded = text.encode('utf-16-le')
                for index in range(0, len(encoded), 2):
                    check()
                    code = int.from_bytes(encoded[index:index + 2], 'little')
                    events = (Input * 2)(Input(1, InputUnion(ki=KeyInput(0, code, 4, 0, 0))),
                                         Input(1, InputUnion(ki=KeyInput(0, code, 6, 0, 0))))
                    with self._input_lock:
                        check()
                        if self._user.SendInput(2, events, ctypes.sizeof(Input)) != 2:
                            # 可能只发出Unicode按下，补发对应释放，不重播文本。
                            self._user.SendInput(1, ctypes.byref(events[1]), ctypes.sizeof(Input))
                            raise ctypes.WinError(ctypes.get_last_error())
        finally:
            with self._input_lock:
                if self._active_cancel is cancel:
                    self._release_held()
                    self._active_cancel = None
            if previous:
                self._user.SetThreadDpiAwarenessContext(previous)
