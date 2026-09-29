"""Керування користувачами: видалення і повторний запит на доступ."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import access
import db
import panel


def owner_update(data):
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    return upd


def setup(monkeypatch):
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r] if reply_markup else []))
        context.user_data["panel_state"] = {}

    monkeypatch.setattr(access, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: uid == 1)
    monkeypatch.setattr(panel, "is_owner", lambda uid: uid == 1)
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot.send_message = AsyncMock()
    return shown, ctx


def test_delete_user_and_request_again(monkeypatch):
    shown, ctx = setup(monkeypatch)
    db.upsert_user_request(42, 42, "anna", "Anna")
    db.set_user_status(42, "approved")
    wid = db.add_watch(42, "PS5", "PS5", "", "", 15)
    assert "👥 Користувачі" in [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]

    asyncio.run(access.cmd_users(owner_update("menu:users"), ctx))
    assert "🗑 Видалити @anna" in shown[-1][1]

    asyncio.run(access.delete_user_callback(owner_update("udel:42"), ctx))
    assert "Видалити користувача @anna?" in shown[-1][0] and "✅ Так, видалити" in shown[-1][1]

    asyncio.run(access.delete_user_callback(owner_update("udelok:42"), ctx))
    assert db.get_user_row(42) is None
    assert db.list_watches(chat_id=42) == []                     # товари більше не скануються
    ctx.bot.send_message.assert_called_once()                   # користувача повідомлено
    assert "@anna видалено" in shown[-1][0]
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert "👥 Користувачі" not in labels                        # кнопка зникла

    # Видалений користувач може знову попросити доступ — і його знову можна схвалити
    db.upsert_user_request(42, 42, "anna", "Anna")
    assert db.get_user_row(42)["status"] == "pending"
    db.set_user_status(42, "approved")
    assert db.get_user_row(42)["status"] == "approved"