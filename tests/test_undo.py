"""«↩️ Скасувати»: кожна дія, яку можна скасувати, повертає все як було."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import access
import db
import handlers
import panel
import screen_undo
import undo
from conftest import patch_ui
from test_learning import GOOD, _watch_with_listings
from test_sold_check import add_sale, sales_watch


def _ui(monkeypatch):
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r] if reply_markup else []))

    patch_ui(monkeypatch, "show_panel", fake_show)
    monkeypatch.setattr(screen_undo, "show_panel", fake_show)
    monkeypatch.setattr(access, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    ctx = MagicMock()
    ctx.user_data = {"panel_state": {}}   # справжній show_panel створює це сам
    ctx.bot_data = {}
    ctx.bot.send_message = AsyncMock()

    def press(data):
        upd = MagicMock()
        upd.effective_chat.id = upd.effective_user.id = 1
        upd.callback_query.data = data
        upd.callback_query.answer = AsyncMock()
        asyncio.run(_dispatch(data, upd, ctx))
        return upd

    return shown, ctx, press


async def _dispatch(data, upd, ctx):
    routes = {"delwatch_yes": handlers.delwatch_yes_callback, "undo": screen_undo.undo_callback,
              "dact": handlers.deal_inbox_action_callback, "dclear": handlers.deals_clear_callback,
              "srej": handlers.sales_reject_callback, "lwdel": handlers.learned_words_callback,
              "rejl": handlers.reject_listing_callback, "udelok": access.delete_user_callback}
    await routes[data.split(":")[0]](upd, ctx)


def _last_undo():
    return db.latest_undo(1, 0)


def _obs_count(wid):
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM listing_obs WHERE watch_id = ?", (wid,)).fetchone()["c"]


# ---------- видалення товару ----------

def test_delete_watch_and_undo_keeps_history(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    w = _watch_with_listings(GOOD)
    press(f"delwatch_yes:{w['id']}")
    assert db.list_watches(chat_id=1) == [] and _obs_count(w["id"]) == 10     # історія ще є
    offer = _last_undo()
    assert offer["label"] == "видалення «PlayStation 5»"
    assert ctx.user_data[undo.OFFER_KEY]["id"] == offer["id"]                 # кнопка на наступному екрані

    press(f"undo:{offer['id']}")
    assert [x["id"] for x in db.list_watches(chat_id=1)] == [w["id"]] and _obs_count(w["id"]) == 10
    assert "Товар повернуто" in shown[-1][0] and "📌" in shown[-1][0]

    press(f"undo:{offer['id']}")                                               # подвійне натискання
    assert "вже скасовано" in shown[-1][0]


def test_deleted_watch_history_purged_after_keep_time():
    w = _watch_with_listings(GOOD)
    db.remove_watch(w["id"], 1)
    assert db.purge_deleted_watches() == 0 and _obs_count(w["id"]) == 10      # ще можна повернути
    assert db.purge_deleted_watches(keep_seconds=-1) == 1 and _obs_count(w["id"]) == 0
    assert not db.restore_watch(w["id"], 1)


def test_deleted_watch_not_checked_for_sales():
    w = _watch_with_listings(GOOD)
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET status = 'gone', gone_at = ?, sold_check = 'pending' "
                     "WHERE watch_id = ?", (int(time.time()), w["id"]))
    assert len(db.get_pending_sold_checks(50)) == 10
    db.remove_watch(w["id"], 1)
    assert db.get_pending_sold_checks(50) == []                                # не витрачаємо ліміт


def test_expired_undo_refused(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    w = _watch_with_listings(GOOD)
    press(f"delwatch_yes:{w['id']}")
    with db.get_conn() as conn:
        conn.execute("UPDATE undo_actions SET created_at = created_at - 25 * 3600")
    press(f"undo:{_last_undo()['id']}")
    assert "Минуло понад 24 год" in shown[-1][0] and db.list_watches(chat_id=1) == []


# ---------- ❌ Інший товар / 🙈 Сховати ----------

def test_undo_reject_restores_listing_words_and_screen(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    w = _watch_with_listings(GOOD)
    items = [{"item_id": "r1", "title": "Sony Playstation 3 PS3 Slim HEN", "price": 90, "currency": "EUR"},
             {"item_id": "r2", "title": "PS3 Super Slim 500GB Sony", "price": 95, "currency": "EUR"},
             {"item_id": "ok0", "title": GOOD[0], "price": 450, "currency": "EUR"}]
    key = handlers._listing_state_key(w["id"])
    ctx.user_data[key] = {"header": "H", "items": list(items), "page": 0, "fetch": None,
                          "nav": [], "query": "PlayStation 5", "fetched_at": time.time()}
    press(f"rejl:{w['id']}:0")
    press(f"rejl:{w['id']}:0")                           # друге відхилення — бот вивчає «ps3»
    assert "ps3" in db.get_watch(w["id"], 1)["exclude"].split()
    assert [it["item_id"] for it in ctx.user_data[key]["items"]] == ["ok0"]

    press(f"undo:{_last_undo()['id']}")
    watch = db.get_watch(w["id"], 1)
    assert "ps3" not in watch["exclude"].split() and "ps3" not in db.get_learned_words(w["id"])
    assert "r2" not in db.get_rejected_ids(w["id"]) and "r1" in db.get_rejected_ids(w["id"])
    assert [it["item_id"] for it in ctx.user_data[key]["items"]] == ["r2", "ok0"]   # повернулось на місце
    assert "Оголошення повернуто" in shown[-1][0] and "«ps3»" in shown[-1][0]


def test_undo_deal_actions(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    wid = db.add_watch(1, "iPhone", "iPhone", "", "", 15)
    ids = [db.add_deal(wid, f"v1|{i}|0", f"iPhone #{i}", 300 + i, "EUR", 620, 20, "u", False) for i in range(3)]

    press(f"dact:buy:{ids[0]}:0")
    press(f"undo:{_last_undo()['id']}")
    assert db.get_deal(ids[0])["status"] == "new" and "повернуто" in shown[-1][0]

    press(f"dact:hide:{ids[1]}:0")
    assert "v1|1|0" in db.get_rejected_ids(wid)
    press(f"undo:{_last_undo()['id']}")
    assert db.get_deal(ids[1])["status"] == "new" and "v1|1|0" not in db.get_rejected_ids(wid)

    press("dclear")
    assert db.get_inbox_deals(1)[1] == 0 and _last_undo()["label"] == "🧹 очищення списку (3)"
    press(f"undo:{_last_undo()['id']}")
    assert db.get_inbox_deals(1)[1] == 3


def test_undo_sales_reject(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    patch_ui(monkeypatch, "is_connected", lambda: True)
    w = sales_watch()
    for i, price in enumerate([600, 610, 620, 630, 640]):
        add_sale(w["id"], f"ok{i}", price, "256GB")
    add_sale(w["id"], "case", 25, "256GB")
    press(f"srej:{w['id']}:0:case")
    assert "Продано: <b>5</b>" in shown[-1][0]
    press(f"undo:{_last_undo()['id']}")
    assert "Продано: <b>6</b>" in shown[-1][0] and "case" not in db.get_rejected_ids(w["id"])


def test_undo_learned_word_removal(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    wid = db.add_watch(1, "PS4", "PS4", "ps3", "", 15)
    db.set_learned_word(wid, "ps3", "excluded")
    press(f"lwdel:{wid}:0")
    assert "ps3" not in (db.get_watch(wid, 1)["exclude"] or "")
    press(f"undo:{_last_undo()['id']}")
    assert "ps3" in db.get_watch(wid, 1)["exclude"].split() and db.get_learned_words(wid)["ps3"] == "excluded"
    assert "«ps3» знову відсіюється" in shown[-1][0]


def test_undo_user_delete(monkeypatch):
    shown, ctx, press = _ui(monkeypatch)
    db.upsert_user_request(42, 42, "anna", "Anna")
    db.set_user_status(42, "approved")
    wid = db.add_watch(42, "PS5", "PS5", "", "", 15)
    press("udelok:42")
    assert db.get_user_row(42) is None and db.list_watches(chat_id=42) == []
    press(f"undo:{_last_undo()['id']}")
    assert db.get_user_row(42)["status"] == "approved"
    assert [w["id"] for w in db.list_watches(chat_id=42)] == [wid]
    assert "Користувача повернуто" in shown[-1][0]
    assert ctx.bot.send_message.await_args.kwargs["chat_id"] == 42          # йому повідомили


# ---------- де з'являється кнопка ----------

def test_offer_attached_once_and_shown_in_menu():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    ctx = MagicMock()
    ctx.user_data = {}
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    undo_id = undo.record(ctx, 1, "watch_delete", "видалення «PS5»", watch_id=wid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Меню", callback_data="menu:home")]])
    first = undo.attach_offer(ctx, kb)
    assert first.inline_keyboard[0][0].callback_data == f"undo:{undo_id}"
    assert undo.attach_offer(ctx, kb) is kb                                   # лише на одному екрані

    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert labels[0] == "↩️ Скасувати: видалення «PS5»"                       # і в меню 15 хвилин
    with db.get_conn() as conn:
        conn.execute("UPDATE undo_actions SET created_at = created_at - 16 * 60")
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert not labels[0].startswith("↩️")
