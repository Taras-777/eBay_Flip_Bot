"""Службові повідомлення: не в чат і без кнопки «🔔 Повідомлення» в меню — лише в лог."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import db
import notifications
import panel


def _labels(chat_id=1):
    return [b.text for r in panel.build_main_menu(chat_id).inline_keyboard for b in r]


def test_service_notices_only_logged(caplog):
    wid = db.add_watch(1, "ROG Strix G18", "ROG Strix G18", "", "", 15)
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    stat = {"cond_group": "new", "spec_group": "RTX 5060", "sale_price": 1587, "median_price": 1600,
            "sample_size": 8}
    with caplog.at_level("INFO"):
        asyncio.run(notifications._notify_median_ready(app, db.get_watch(wid, 1), [stat]))
        asyncio.run(notifications._notify_median_problem(app, db.get_watch(wid, 1), "замало оголошень"))
    assert not app.bot.send_message.called                                  # у чат нічого
    assert not any(x.startswith("🔔") for x in _labels())                  # і кнопки в меню немає
    assert "Ринок для «ROG Strix G18»" in caplog.text


def test_old_notices_table_dropped():
    with db.get_conn() as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'notices'").fetchone()
