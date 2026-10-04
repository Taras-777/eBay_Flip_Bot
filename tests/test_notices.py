"""«🔔 Повідомлення»: службові сповіщення — у меню, а не окремими повідомленнями в чаті."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import db
import notifications
import panel
from test_sales import _screen


def _labels(chat_id=1):
    return [b.text for r in panel.build_main_menu(chat_id).inline_keyboard for b in r]


def test_market_ready_goes_to_notices_not_chat():
    wid = db.add_watch(1, "ROG Strix G18", "ROG Strix G18", "", "", 15)
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    app.user_data = {}
    assert not any(x.startswith("🔔") for x in _labels())                 # немає повідомлень — немає кнопки
    stat = {"cond_group": "new", "spec_group": "RTX 5060", "sale_price": 1587, "median_price": 1600,
            "sample_size": 8}
    asyncio.run(notifications._notify_median_ready(app, db.get_watch(wid, 1), [stat]))
    asyncio.run(notifications._notify_median_problem(app, db.get_watch(wid, 1), "замало оголошень"))
    assert not app.bot.send_message.called                                  # у чат нічого не надіслано
    assert "🔔 Повідомлення · 🆕 2" in _labels()


def test_notices_screen_marks_seen_and_clears(monkeypatch):
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15)
    db.add_notice(1, "market_ready", "📊 Ринок для «MSI Katana» проаналізовано", wid)
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("ntc:0")
    asyncio.run(handlers.notices_callback(upd, ctx))
    text, buttons = shown[-1]
    assert "🆕" in text and "Ринок для «MSI Katana»" in text and "📊 MSI Katana" in buttons
    assert db.count_notices(1) == (1, 0)                                     # переглянуто
    assert "🔔 Повідомлення" in _labels()
    monkeypatch.setattr(panel, "show_panel", AsyncMock())                     # після очищення — головне меню
    upd, ctx = press("ntc:clear")
    asyncio.run(handlers.notices_callback(upd, ctx))
    assert db.count_notices(1) == (0, 0) and not any(x.startswith("🔔") for x in _labels())
