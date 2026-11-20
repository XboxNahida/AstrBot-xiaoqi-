"""天气接口使用合成数据，不访问真实网络。"""
from datetime import datetime

from pet_features.weather import WeatherClient


def test_ip_location_is_only_explicit_and_does_not_keep_ip():
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        return {"city": "测试城", "latitude": 30, "longitude": 120, "ip": "203.0.113.1"}
    client = WeatherClient(fetch=fetch)
    assert calls == []
    city = client.locate()
    assert len(calls) == 1
    assert city["latitude"] == 30 and "ip" not in city


def test_search_chinese_and_multiple_cities():
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        if "weather.com.cn" in url:
            return []
        return {"results": [{"id": 1, "name": "北京", "latitude": 39.9, "longitude": 116.4}]}
    client = WeatherClient(fetch=fetch)
    assert client.search("北京")[0]["name"] == "北京"
    assert "language=zh" in calls[-1]
    assert "%E5%8C%97" in calls[-1]


def test_cache_expiry_failure_keeps_timestamp_and_city_isolation():
    now = [100.0]
    calls = []
    def fetch(url, timeout):
        calls.append(timeout)
        if len(calls) > 2:
            raise TimeoutError("模拟超时")
        return {"current": {"temperature_2m": 12}, "hourly": {"time": []}}
    client = WeatherClient(fetch=fetch, clock=lambda: now[0])
    city = {"id": 1, "name": "北京", "latitude": 39.9, "longitude": 116.4}
    assert client.current(city)["data"]["temperature_2m"] == 12
    assert client.current(city)["updated_at"] == 100
    assert len(calls) == 1
    assert client.current(dict(city, id=2))["available"]
    now[0] = 1901
    result = client.current(city)
    assert result["stale"] and result["updated_at"] == 100
    assert "超时" in result["error"]


def test_forecast_tomorrow_rain_and_unavailable():
    payload = {"hourly": {"time": ["2026-11-17T08:00", "2026-11-18T08:00"],
                          "precipitation_probability": [80, 0], "precipitation": [1.2, 0]}}
    client = WeatherClient(fetch=lambda *_: payload, today=lambda: datetime(2026, 11, 16).date())
    city = {"id": "x", "name": "测试", "latitude": 0, "longitude": 0}
    assert client.forecast(city)["data"]["tomorrow_rain"][0]["probability"] == 80
    bad = WeatherClient(fetch=lambda *_: (_ for _ in ()).throw(OSError("离线")))
    assert not bad.current(city)["available"]


def test_domestic_search_now_source_and_missing_coordinates():
    calls = []
    def fetch(url, timeout):
        calls.append((url, timeout))
        if "toy1.weather" in url:
            return [{"ref": "101010100~beijing~北京~Beijing~~~~~~北京"}]
        if "geocoding" in url:
            raise TimeoutError("海外源不可达")
        assert "d1.weather.com.cn/sk_2d/101010100.html" in url
        return {"temp": "8.5", "weather": "晴", "SD": "23%", "WD": "北风", "WS": "2级", "time": "12:00"}
    client = WeatherClient(fetch=fetch)
    city = client.search("北京")[0]
    assert city["cn_code"] == "101010100" and city["latitude"] is None
    result = client.current(city)
    assert result["source"] == "中国天气网" and result["updated_at"]
    assert result["data"]["temperature_2m"] == 8.5
    assert result["data"]["description"] == "晴"
    assert result["data"]["relative_humidity_2m"] == 23
    assert "wind_speed_10m" not in result["data"]


def test_domestic_failure_fallback_remaining_budget_and_cached_source():
    budgets = []
    def fetch(url, timeout):
        budgets.append(timeout)
        if "d1.weather.com.cn" in url:
            raise TimeoutError("国内源超时")
        return {"current": {"temperature_2m": 3, "weather_code": 61}}
    client = WeatherClient(fetch=fetch, timeout=4)
    city = {"id": 1, "name": "北京", "latitude": 39.9, "longitude": 116.4, "cn_code": "101010100"}
    result = client.current(city)
    assert result["source"] == "Open-Meteo"
    assert result["data"]["description"] == "小雨"
    assert len(budgets) == 2 and all(0 < value <= 4 for value in budgets)
    assert client.current(city)["source"] == "Open-Meteo"


def test_fallback_budget_exhaustion_preserves_domestic_cache(monkeypatch):
    import pet_features.weather as module
    now = [100]
    fail = [False]
    def fetch(url, timeout):
        assert "d1.weather.com.cn" in url
        if fail[0]:
            raise TimeoutError("国内源超时")
        return {"temp": "4", "weather": "晴"}
    client = WeatherClient(fetch=fetch, clock=lambda: now[0])
    city = {"id": 1, "name": "北京", "latitude": 39.9, "longitude": 116.4, "cn_code": "101010100"}
    assert client.current(city)["source"] == "中国天气网"
    now[0] = 1901
    fail[0] = True
    ticks = iter([0, 20])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    result = client.current(city)
    assert result["stale"] and result["source"] == "中国天气网"
    assert result["updated_at"] == 100 and "总超时" in result["error"]


def test_legacy_chinese_city_resolves_domestic_source_without_changing_config():
    import copy
    calls = []
    now = [100.0]
    def fetch(url, timeout):
        calls.append((url, timeout))
        if "toy1.weather" in url:
            return [{"ref": "101270101~sichuan~成都~Chengdu~~~~~~四川"}]
        if "d1.weather" in url:
            assert "101270101.html" in url
            return {"temp": "15", "weather": "阴"}
        raise TimeoutError("_ssl.c:993: The handshake operation timed out")
    city = {"id": 1815286, "name": "成都", "latitude": 30.66, "longitude": 104.06,
            "country": "中国", "admin1": "四川省"}
    original = copy.deepcopy(city)
    client = WeatherClient(fetch=fetch, clock=lambda: now[0], timeout=8)
    first = client.current(city)
    assert first["available"] and first["source"] == "中国天气网"
    now[0] += 1801
    assert client.current(city)["source"] == "中国天气网"
    assert city == original
    assert len(calls) == 3 and all(0 < budget <= 4 for _, budget in calls)


def test_domestic_lookup_rejects_wrong_province_and_caches_failure():
    calls = []
    now = [100.0]
    def fetch(url, timeout):
        calls.append(url)
        if "toy1.weather" in url:
            return [{"ref": "101010100~beijing~成都~Chengdu~~~~~~北京"}]
        assert "api.open-meteo.com" in url
        raise TimeoutError("The handshake operation timed out")
    client = WeatherClient(fetch=fetch, clock=lambda: now[0])
    city = {"id": 1, "name": "成都", "latitude": 30.66, "longitude": 104.06,
            "country": "中国", "admin1": "四川"}
    assert not client.current(city)["available"]
    assert not client.current(city)["available"]
    assert len([url for url in calls if "toy1.weather" in url]) == 1


def test_city_resolution_reserves_budget_for_overseas_fallback(monkeypatch):
    import pet_features.weather as module
    ticks = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: ticks[0])
    budgets = []
    def fetch(url, timeout):
        budgets.append(timeout)
        ticks[0] += timeout
        if "toy1.weather" in url:
            return [{"ref": "101270101~sichuan~成都~Chengdu~~~~~~四川"}]
        if "d1.weather" in url:
            raise TimeoutError("国内源超时")
        return {"current": {"temperature_2m": 15}}
    city = {"id": 1, "name": "成都", "latitude": 30.66, "longitude": 104.06,
            "country": "中国", "admin1": "四川"}
    result = WeatherClient(fetch=fetch, timeout=8).current(city)
    assert result["available"] and result["source"] == "Open-Meteo"
    assert len(budgets) == 3 and sum(budgets) <= 8


def test_tls_timeout_is_chinese_for_current_and_forecast():
    from urllib.error import URLError
    city = {"id": 1, "name": "测试", "latitude": 30, "longitude": 104}
    def fetch(*_):
        raise URLError(TimeoutError("_ssl.c:993: The handshake operation timed out"))
    client = WeatherClient(fetch=fetch)
    for result in (client.current(city), client.forecast(city)):
        assert not result["available"] and not result["stale"]
        assert "超时" in result["error"]
        assert "_ssl" not in result["error"] and "urlopen" not in result["error"]
