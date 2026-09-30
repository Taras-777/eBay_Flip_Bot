"""Обслуговування: індекси бази, резервні копії, збереження історії при зміні фільтрів."""
from conftest import patch_ui
import asyncio
import gzip
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import backup
import db
import market
import settings


def test_indexes_exist():
    with db.get_conn() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    for idx in ("idx_obs_watch_status", "idx_obs_sold_check", "idx_obs_item", "idx_deals_watch", "idx_watches_chat"):
        assert idx in names


def test_queries_use_indexes():
    with db.get_conn() as conn:
        plan = " ".join(r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM listing_obs WHERE sold_check = 'pending' AND gone_at >= 0"))
    assert "idx_obs_sold_check" in plan


def test_backup_rotation_and_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    paths = []
    for i in range(backup.BACKUP_KEEP + 2):
        monkeypatch.setattr(backup, "datetime", type("D", (), {"now": staticmethod(
            lambda tz=None, i=i: __import__("datetime").datetime(2026, 9, 1, 10, 0, i))}))
        paths.append(backup.make_backup())
    assert len(backup.list_backups()) == backup.BACKUP_KEEP          # старі прибрано
    restored = tmp_path / "restored.sqlite3"
    with gzip.open(backup.list_backups()[0], "rb") as f_in, open(restored, "wb") as f_out:
        f_out.write(f_in.read())
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT id FROM watches").fetchone()[0] == wid  # копія справжня
    conn.close()


def test_daily_backup_once_a_day(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    assert backup.daily_backup() is not None
    assert backup.daily_backup() is None                               # сьогодні вже є
    assert "Резервні копії: остання" in backup.backups_summary()


def test_backup_button_sends_file(monkeypatch, tmp_path):
    import account
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(account, "is_owner", lambda uid: True)
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append(text)

    monkeypatch.setattr(account, "show_panel", fake_show)
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.answer = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot.send_document = AsyncMock()
    asyncio.run(account.backup_send_callback(upd, ctx))
    ctx.bot.send_document.assert_called_once()
    assert "надіслано" in shown[-1]


def _obs(watch_id, item_id, price, spec="256GB", status="active"):
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price, first_seen,
                        last_seen, status, gone_at, sold_check) VALUES (?, ?, 'used', ?, ?, ?, ?, ?, ?, ?)""",
                     (watch_id, item_id, spec, price, now, now, status,
                      now if status == "gone" else None, "sold" if status == "gone" else None))


def test_new_min_price_keeps_history():
    wid = db.add_watch(1, "iPhone", "iPhone 15 Pro", "", "", 15)
    _obs(wid, "cheap_sold", 90, status="gone")      # чохол, що затесався
    _obs(wid, "real_sold", 600, status="gone")
    _obs(wid, "active", 580)
    db.upsert_market_stats(wid, "used", "*", 590, 20, sale_price=345, sale_source="за 2 проданими")
    db.update_watch_min_price(wid, 1, 300)
    removed = market.apply_filters_to_history(db.get_watch(wid, 1))
    assert removed == 1
    assert [r["item_id"] for r in db.get_sold_listings(wid)] == ["real_sold"]   # справжній продаж лишився
    assert db.get_market_stats(wid)[0]["updated_at"] == 0                       # ринок перерахується


def test_list_screen_has_add_button(monkeypatch):
    import access
    import handlers
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append([b.text for r in reply_markup.inline_keyboard for b in r])

    patch_ui(monkeypatch, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.answer = AsyncMock()
    asyncio.run(handlers.cmd_list(upd, MagicMock()))
    assert "➕ Додати товар" in shown[-1]
    db.add_watch(1, "PS5", "PS5", "", "", 15)
    asyncio.run(handlers.cmd_list(upd, MagicMock()))
    assert "➕ Додати товар" in shown[-1]
