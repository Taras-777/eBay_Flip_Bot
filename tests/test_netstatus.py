"""Після мережевого збою в лозі має з'явитися «з'єднання відновлено»."""
import asyncio
import logging
import types

import pytest
import requests
from telegram.error import NetworkError

import ebay_api
import netstatus
import scheduler
from test_retries import FakeResponse, setup


@pytest.fixture(autouse=True)
def clean_state():
    netstatus._down_since.clear()
    yield
    netstatus._down_since.clear()


def restored(caplog, name):
    return [r.getMessage() for r in caplog.records if f"З'єднання з {name} відновлено" in r.getMessage()]


def test_ebay_restored_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    setup(monkeypatch, [requests.exceptions.ConnectionError("down"), FakeResponse(200)])
    ebay_api._request_with_retries("GET", "https://example.test")
    assert len(restored(caplog, "eBay")) == 1
    assert not netstatus.is_down("eBay")


def test_no_message_without_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    setup(monkeypatch, [FakeResponse(200)])
    ebay_api._request_with_retries("GET", "https://example.test")
    assert restored(caplog, "eBay") == []


def test_ebay_down_after_all_attempts_then_restored_later(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    fails = [requests.exceptions.ConnectionError("down")] * ebay_api.NETWORK_MAX_ATTEMPTS
    setup(monkeypatch, fails)
    with pytest.raises(requests.exceptions.ConnectionError):
        ebay_api._request_with_retries("GET", "https://example.test")
    assert netstatus.is_down("eBay") and restored(caplog, "eBay") == []
    setup(monkeypatch, [FakeResponse(200)])                  # наступна перевірка — мережа є
    ebay_api._request_with_retries("GET", "https://example.test")
    assert len(restored(caplog, "eBay")) == 1


def test_duration_text():
    assert netstatus._duration(12) == "12 с"
    assert netstatus._duration(125) == "2 хв 5 с"
    assert netstatus._duration(3700) == "1 год 1 хв"


def test_telegram_restored_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(scheduler, "TELEGRAM_RECHECK_SECONDS", 0)
    answers = [NetworkError("still down"), types.SimpleNamespace(id=1)]

    class Bot:
        async def get_me(self):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    app = types.SimpleNamespace(bot=Bot(), bot_data={})
    context = types.SimpleNamespace(error=NetworkError("httpx.ReadError"), application=app)

    async def scenario():
        await scheduler.error_handler(None, context)
        await app.bot_data["telegram_watch_task"]

    asyncio.run(scenario())
    assert len(restored(caplog, "Telegram")) == 1 and answers == []
