"""国内实况优先、Open-Meteo分时与备用；不定位、不调用模型。"""
import copy
import json
import math
import ssl
import time
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WMO_ZH = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "冻雾",
          51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
          61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
          71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "小阵雨", 81: "中阵雨",
          82: "强阵雨", 85: "小阵雪", 86: "大阵雪", 95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹"}


def _fetch_json(url, timeout):
    headers = {"User-Agent": "Mozilla/5.0"}
    if "weather.com.cn/" in url:
        headers["Referer"] = "https://www.weather.com.cn/"
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        body = response.read(2_000_000).decode("utf-8").strip()
    if "toy1.weather.com.cn/" in url:
        body = body.strip("()")
    elif "d1.weather.com.cn/" in url:
        if "dataSK" not in body:
            raise ValueError("国内源没有实况数据")
        body = body[body.index("{"):body.rindex("}") + 1]
    return json.loads(body)


def _weather_error(exc):
    """只向界面返回可操作的中文说明，不泄露底层网络异常。"""
    if isinstance(exc, HTTPError):
        return f"天气服务暂不可用（HTTP {exc.code}），请稍后重试。"
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
        prefix = "天气查询总超时" if str(reason) == "天气查询总超时" else "天气连接超时"
        return prefix + "，请稍后重试。"
    if isinstance(reason, ssl.SSLError):
        return "天气服务安全连接失败，请检查网络或系统时间后重试。"
    if isinstance(exc, OSError):
        return "暂时无法连接天气服务，请检查网络后重试。"
    if str(exc) in ("城市经纬度无效", "城市暂无经纬度，当前国内源失败或分时预报不可用；请稍后重新搜索补全坐标"):
        return str(exc)
    return "天气服务返回的数据暂不可用，请稍后重试。"


class WeatherClient:
    def __init__(self, *, fetch=None, clock=None, today=None, timeout=8, cache_seconds=1800):
        self.fetch = fetch or _fetch_json
        self.clock = clock or time.time
        self.today = today or date.today
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache = {}
        self._city_codes = {}

    def search(self, query):
        query = str(query).strip()
        if not query:
            return []
        deadline = time.monotonic() + self.timeout
        domestic = []
        try:
            rows = self.fetch("https://toy1.weather.com.cn/search?" + urlencode({"cityname": query}),
                              max(0.01, self.timeout * 0.5))
            for row in rows:
                parts = str(row.get("ref", "")).split("~")
                if len(parts) >= 3 and parts[0].isdigit() and parts[2]:
                    domestic.append({"id": "cn:" + parts[0], "cn_code": parts[0], "name": parts[2],
                                     "latitude": None, "longitude": None, "country": "中国",
                                     "admin1": parts[9] if len(parts) > 9 else "", "source": "中国天气网"})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            params = urlencode({"name": query, "count": 20, "language": "zh", "format": "json"})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return domestic
            raw = self.fetch("https://geocoding-api.open-meteo.com/v1/search?" + params, remaining)
            overseas = [{key: item.get(key) for key in
                         ("id", "name", "latitude", "longitude", "country", "admin1")}
                        for item in raw.get("results", [])]
            for city in domestic:
                matching = next((c for c in overseas if c["name"] == city["name"] and c["country"] in ("中国", "China")), None)
                if matching:
                    city.update(latitude=matching["latitude"], longitude=matching["longitude"])
                    overseas.remove(matching)
            return domestic + overseas
        except (OSError, ValueError, TypeError, AttributeError):
            if domestic:
                return domestic
            raise

    query = search

    def locate(self):
        """仅明确点击调用；城市定位不保存IP地址。"""
        raw = self.fetch("https://ipapi.co/json/", self.timeout)
        lat, lon = float(raw["latitude"]), float(raw["longitude"])
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("IP定位没有有效坐标，请手动搜索城市")
        return {"id": f"location:{lat}:{lon}", "name": str(raw.get("city", "定位城市")) + "（IP估计）",
                "latitude": lat, "longitude": lon, "source": "IP估计"}

    def _read(self, city, kind):
        lat, lon = city.get("latitude"), city.get("longitude")
        key = (str(city["id"]), lat, lon, kind, city.get("cn_code"))
        old = self._cache.get(key)
        now = self.clock()
        if old and 0 <= now - old["updated_at"] < self.cache_seconds:
            return copy.deepcopy(old)
        params = {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3}
        if kind == "current":
            params["current"] = "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
        else:
            params["hourly"] = "temperature_2m,weather_code,precipitation_probability,precipitation"
        try:
            deadline = time.monotonic() + self.timeout
            code = city.get("cn_code")
            if kind == "current" and (code or city.get("country") in ("中国", "China")):
                try:
                    domestic_budget = self.timeout * 0.5
                    if not code:
                        code = self._domestic_code(city, self.timeout * 0.25)
                        domestic_budget = min(domestic_budget, deadline - self.timeout * 0.5 - time.monotonic())
                    if not code or domestic_budget <= 0:
                        raise ValueError("国内源暂不可用")
                    data = self._domestic_current(code, domestic_budget)
                    return self._save(key, city, data, "中国天气网")
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            if lat is None or lon is None:
                raise ValueError("城市暂无经纬度，当前国内源失败或分时预报不可用；请稍后重新搜索补全坐标")
            lat, lon = float(lat), float(lon)
            if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("城市经纬度无效")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("天气查询总超时")
            raw = self.fetch("https://api.open-meteo.com/v1/forecast?" + urlencode(params), remaining)
            if raw.get("error"):
                raise ValueError(str(raw.get("reason", "天气接口失败")))
            if kind == "current":
                data = raw["current"]
                if "temperature_2m" not in data:
                    raise ValueError("当前气温缺失")
                data = dict(data, description=WMO_ZH.get(data.get("weather_code"), "未知天气"))
            else:
                hourly = raw["hourly"]
                tomorrow = (self.today() + timedelta(days=1)).isoformat()
                rows = []
                for i, stamp in enumerate(hourly["time"]):
                    row = {"time": stamp}
                    for field, label in (("temperature_2m", "temperature"), ("weather_code", "weather_code"),
                                         ("precipitation_probability", "probability"), ("precipitation", "precipitation")):
                        values = hourly.get(field, [])
                        row[label] = values[i] if i < len(values) else None
                    rows.append(row)
                data = {"hourly": rows, "tomorrow_rain": [r for r in rows if r["time"].startswith(tomorrow)
                        and ((r["precipitation"] or 0) > 0 or (r["probability"] or 0) >= 40)]}
            return self._save(key, city, data, "Open-Meteo")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = copy.deepcopy(old) if old else {"available": False, "updated_at": None,
                                                     "city": dict(city), "data": None}
            result.update(stale=bool(old), error=_weather_error(exc))
            return result

    def _domestic_code(self, city, timeout):
        """补全旧配置的国内编码；名称/省份严格匹配，不修改用户配置。"""
        name = str(city.get("name", "")).removesuffix("市")
        province = str(city.get("admin1") or "").removesuffix("省").removesuffix("市")
        key = (name, province)
        now = self.clock()
        cached = self._city_codes.get(key)
        if cached and 0 <= now - cached[0] < (86400 if cached[1] else 600):
            return cached[1]
        code = None
        try:
            rows = self.fetch("https://toy1.weather.com.cn/search?" + urlencode({"cityname": name}), timeout)
            matches = set()
            for row in rows:
                parts = str(row.get("ref", "")).split("~")
                if len(parts) < 10 or len(parts[0]) != 9 or not parts[0].isdigit():
                    continue
                region = parts[9].removesuffix("省").removesuffix("市")
                if parts[2].removesuffix("市") == name and (not province or region == province):
                    matches.add(parts[0])
            if len(matches) == 1:
                code = matches.pop()
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._city_codes[key] = (now, code)
        return code

    def _save(self, key, city, data, source):
        result = {"available": True, "stale": False, "updated_at": self.clock(), "source": source,
                  "city": dict(city), "data": data, "error": ""}
        self._cache[key] = result
        return copy.deepcopy(result)

    def _domestic_current(self, code, timeout):
        if not str(code).isdigit():
            raise ValueError("国内城市编号无效")
        raw = self.fetch(f"https://d1.weather.com.cn/sk_2d/{code}.html", timeout)
        if not isinstance(raw, dict):
            raise TypeError("国内源实况格式错误")
        value = raw.get("temp")
        if value in (None, "", "999"):
            raise ValueError("国内源气温缺失")
        temp = float(value)
        if not math.isfinite(temp):
            raise ValueError("国内源气温无效")
        data = {"temperature_2m": temp, "description": raw.get("weather") or "未知天气"}
        humidity = str(raw.get("SD", "")).rstrip("%")
        try:
            humidity = float(humidity)
            if 0 <= humidity <= 100:
                data["relative_humidity_2m"] = humidity
        except ValueError:
            pass
        for original, normalized in (("WD", "wind_direction"), ("WS", "wind_description"),
                                     ("time", "observed_time"), ("date", "observed_date")):
            if raw.get(original):
                data[normalized] = raw[original]
        return data

    def current(self, city):
        return self._read(city, "current")

    def forecast(self, city):
        return self._read(city, "forecast")

    def close(self):
        self._cache.clear()
        self._city_codes.clear()
