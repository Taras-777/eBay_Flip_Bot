"""Одночасна обробка кнопок і 🐢 журнал повільних дій."""
import asyncio
from unittest.mock import MagicMock

from ui import action_log
from core import db
from core.concurrency import DialogSafeUpdateProcessor


def test_buttons_run_in_parallel_but_dialog_steps_in_order():
    async def run():
        proc = DialogSafeUpdateProcessor(4)
        conv = MagicMock()
        conv.check_update = lambda u: 1 if u == "dialog" else None
        proc.conversations = [conv]
        order = []

        async def job(name, delay):
            order.append(name + "+")
            await asyncio.sleep(delay)
            order.append(name + "-")

        await asyncio.gather(proc.do_process_update("btn", job("a", 0.2)),
                             proc.do_process_update("btn", job("b", 0.05)),
                             proc.do_process_update("dialog", job("c", 0.1)),
                             proc.do_process_update("dialog", job("d", 0.01)))
        return order
    order = asyncio.run(run())
    assert order.index("b-") < order.index("a-")              # швидка кнопка не чекає повільну
    assert order.index("c-") < order.index("d+")              # кроки діалогу — по черзі


def _update(data):
    from telegram import Update
    upd = MagicMock(spec=Update)
    upd.callback_query.data = data
    upd.effective_chat.id = 1
    return upd


def test_action_names_and_journal():
    assert action_log.action_name(_update("watch_details:15")) == "📌 Екран товару"
    assert action_log.action_name(_update("menu:stats")) == "📊 Статистика"
    assert action_log.action_name(_update("foo:12:v1|3344|0")) == "foo:N:vN"
    db.record_action(1, "📌 Екран товару", 3.2)
    db.record_action(1, "📌 Екран товару", 1.8)
    db.record_action(1, "🔥 Вигідні пропозиції", 0, "BadRequest: Message is too long")
    text = action_log.journal_text()
    assert "📌 Екран товару — 2 раз, у середньому 2.5 с, найдовше 3.2 с" in text
    assert "⚠️ BadRequest: Message is too long" in text
    db.clear_action_log()
    assert "Поки порожньо" in action_log.journal_text()


def test_slow_update_is_recorded(monkeypatch):
    from telegram.ext import Application
    monkeypatch.setattr(action_log, "SLOW_ACTION_SECONDS", 0.01)

    async def slow(self, update):
        await asyncio.sleep(0.03)
    monkeypatch.setattr(Application, "process_update", slow)
    app = Application.builder().application_class(action_log.TimedApplication).token("1:A").build()
    asyncio.run(app.process_update(_update("configs:4")))
    assert db.get_action_log()[0]["action"] == "🧩 Конфігурації"


def test_progress_shown_only_for_slow_actions(monkeypatch):
    import time
    from unittest.mock import AsyncMock
    from ui import panel
    monkeypatch.setattr(panel, "PROGRESS_AFTER_SECONDS", 0.05)
    upd, ctx = MagicMock(), MagicMock()
    upd.effective_chat.id = 1
    ctx.bot.edit_message_text = AsyncMock()
    assert asyncio.run(panel.run_with_progress(upd, ctx, "⏳ …", lambda x: x * 2, 21)) == 42
    assert not ctx.bot.edit_message_text.called                 # швидко — без «⏳»
    assert asyncio.run(panel.run_with_progress(upd, ctx, "⏳ …", lambda: time.sleep(0.2) or 7)) == 7
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "⏳ …"


def test_developer_report_file(monkeypatch):
    from unittest.mock import AsyncMock
    from ui import access
    assert action_log.action_name(_update("backup:send")) == "💾 Резервна копія"
    db.record_action(1, "💡 Оновити аналіз", 80.1)
    db.record_action(1, "🔥 Вигідні пропозиції", 0, "BadRequest: Message is too long")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO api_usage (day, api, count) VALUES ('2099-01-01', 'browse', 4300)")
    text, count = action_log.report_text()
    assert count == 2 and "Версія:" in text
    assert "💡 Оновити аналіз: 1 раз, середнє 80.1 с" in text
    assert "=== Помилки (1) ===" in text and "BadRequest: Message is too long" in text

    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append([b.text for r in reply_markup.inline_keyboard for b in r])
    monkeypatch.setattr(action_log, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.data = "slowlog:file"
    upd.callback_query.answer = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot.send_document = AsyncMock()
    asyncio.run(action_log.slowlog_callback(upd, ctx))
    doc = ctx.bot.send_document.await_args.kwargs["document"]
    assert doc.filename.startswith("zvit_bota_") and "📤 Звіт для розробника (файл)" in shown[-1]
