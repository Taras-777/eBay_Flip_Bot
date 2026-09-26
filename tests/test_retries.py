"""
Повторні запити до eBay (_request_with_retries): тимчасові мережеві
помилки й коди 429/5xx повторюються з паузою, решта — повертається одразу.
Мережа й паузи підмінені — тести виконуються миттєво й без інтернету.
"""
import requests

import ebay_api


class FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeSession:
    """Повертає заздалегідь задані відповіді (або кидає винятки) по черзі."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def setup(monkeypatch, outcomes):
    session = FakeSession(outcomes)
    sleeps = []
    monkeypatch.setattr(ebay_api, "_http_session", lambda: session)
    monkeypatch.setattr(ebay_api.time, "sleep", sleeps.append)
    return session, sleeps


def test_retries_connection_error_then_returns_response(monkeypatch):
    session, sleeps = setup(monkeypatch, [requests.exceptions.ConnectionError("down"), FakeResponse(200)])
    resp = ebay_api._request_with_retries("GET", "https://example.test")
    assert resp.status_code == 200
    assert session.calls == 2 and sleeps == [1.0]


def test_retries_rate_limit_and_honors_retry_after(monkeypatch):
    session, sleeps = setup(monkeypatch, [FakeResponse(429, {"Retry-After": "3"}), FakeResponse(200)])
    resp = ebay_api._request_with_retries("GET", "https://example.test")
    assert resp.status_code == 200
    assert sleeps == [3.0]


def test_retry_after_is_capped(monkeypatch):
    _, sleeps = setup(monkeypatch, [FakeResponse(503, {"Retry-After": "3600"}), FakeResponse(200)])
    ebay_api._request_with_retries("GET", "https://example.test")
    assert sleeps == [ebay_api.NETWORK_MAX_BACKOFF_SECONDS]


def test_returns_final_error_response_without_extra_retry(monkeypatch):
    outcomes = [FakeResponse(500)] * ebay_api.NETWORK_MAX_ATTEMPTS
    session, sleeps = setup(monkeypatch, outcomes)
    resp = ebay_api._request_with_retries("GET", "https://example.test")
    assert resp.status_code == 500
    assert session.calls == ebay_api.NETWORK_MAX_ATTEMPTS
    assert len(sleeps) == ebay_api.NETWORK_MAX_ATTEMPTS - 1  # після останньої спроби не чекаємо


def test_client_error_is_not_retried(monkeypatch):
    session, sleeps = setup(monkeypatch, [FakeResponse(400)])
    resp = ebay_api._request_with_retries("GET", "https://example.test")
    assert resp.status_code == 400 and session.calls == 1 and sleeps == []


def test_minimum_sample_size_is_eight_for_every_query():
    """Навмисна зміна: медіана з 3–4 оголошень давала шум, тепер мінімум 8."""
    import market
    assert market.minimum_sample_size_for_query("Dell Latitude 5230") == 8
    assert market.minimum_sample_size_for_query("PS5") == 8