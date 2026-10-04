"""Одночасна обробка кнопок і 🐢 журнал повільних дій."""
import asyncio
from unittest.mock import MagicMock

import action_log
import db
from concurrency import DialogSafeUpdateProcessor


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
    assert db.get_action_log()[0]["action"] == "🧩 Усі конфігурації"


def test_progress_shown_only_for_slow_actions(monkeypatch):
    import time
    from unittest.mock import AsyncMock
    import panel
    monkeypatch.setattr(panel, "PROGRESS_AFTER_SECONDS", 0.05)
    upd, ctx = MagicMock(), MagicMock()
    upd.effective_chat.id = 1
    ctx.bot.edit_message_text = AsyncMock()
    assert asyncio.run(panel.run_with_progress(upd, ctx, "⏳ …", lambda x: x * 2, 21)) == 42
    assert not ctx.bot.edit_message_text.called                 # швидко — без «⏳»
    assert asyncio.run(panel.run_with_progress(upd, ctx, "⏳ …", lambda: time.sleep(0.2) or 7)) == 7
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "⏳ …"
