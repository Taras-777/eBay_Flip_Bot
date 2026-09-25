"""
Спільні налаштування тестів: шлях до модулів бота, окрема тимчасова база
для кожного тесту й підробний HTTP-шар eBay (тести ніколи не ходять у мережу).
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# У CI config.py створюється з config.example.py; якщо його немає зовсім — заглушка
if "config" not in sys.modules:
    try:
        import config  # noqa: F401
    except ImportError:
        sys.modules["config"] = types.SimpleNamespace(
            EBAY_CLIENT_ID="test", EBAY_CLIENT_SECRET="test",
            TELEGRAM_BOT_TOKEN="123:TEST", OWNER_TELEGRAM_ID=1,
        )

import pytest  # noqa: E402

import db  # noqa: E402
import ebay_api  # noqa: E402
import settings  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "test.sqlite3"))
    db.init_db()
    ebay_api._rate_limit_cache.update(data=None, fetched_at=0)
    yield


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture
def fake_ebay(monkeypatch):
    """
    Підробний eBay: listings — список оголошень у форматі itemSummaries,
    aspects — {item_id: {назва: значення}}. Повертає об'єкт зі списком викликів.
    """
    state = types.SimpleNamespace(listings=[], aspects={}, calls=[])

    def fake_request(method, url, **kwargs):
        state.calls.append(url)
        if url.startswith(ebay_api.ITEM_URL):
            from urllib.parse import unquote
            item_id = unquote(url[len(ebay_api.ITEM_URL):])
            aspects = state.aspects.get(item_id, {})
            return FakeResponse({"localizedAspects": [{"name": k, "value": v} for k, v in aspects.items()]})
        if url == ebay_api.SEARCH_URL:
            params = kwargs.get("params") or {}
            offset = int(params.get("offset", 0))
            return FakeResponse({"itemSummaries": state.listings if offset == 0 else []})
        return FakeResponse({})

    monkeypatch.setattr(ebay_api, "_get_access_token", lambda: "TOKEN")
    monkeypatch.setattr(ebay_api, "_request_with_retries", fake_request)
    return state


def listing(item_id, title, price, condition_id="3000", created_ago_s=60, **extra):
    """Оголошення у форматі відповіді Browse API."""
    from datetime import datetime, timedelta, timezone
    created = (datetime.now(timezone.utc) - timedelta(seconds=created_ago_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = {
        "itemId": item_id, "title": title, "price": {"value": str(price), "currency": "EUR"},
        "buyingOptions": ["FIXED_PRICE"], "conditionId": condition_id, "condition": "Gebraucht",
        "itemWebUrl": f"https://www.ebay.de/itm/{item_id}", "itemCreationDate": created,
        "seller": {"feedbackScore": 500, "feedbackPercentage": "99.8"},
    }
    data.update(extra)
    return data