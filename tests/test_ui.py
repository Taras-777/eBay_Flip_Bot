"""
Екрани й кнопки бота без Telegram: список товарів, картка товару, гортання
оголошень, «🙈 Сховати» / «❌ Інший товар», головне меню, панель без дублів.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

import access
import db
import handlers
import notifications
import panel


# ---------- допоміжне ----------

class Screen:
    """Підміна show_panel: запам'ятовує текст і кнопки останнього екрана."""

    def __init__(self):
        self.text, self.rows, self.calls = "", [], 0

    async def __call__(self, update, context, text, reply_markup=None, parse_mode=None):
        self.calls += 1
        self.text = text
        self.rows = [[b.text for b in r] for r in reply_markup.inline_keyboard] if reply_markup else []
        self.markup = reply_markup

    def buttons(self):
        return [b for r in self.rows for b in r]


def make_update(data=None, user_id=1):
    upd = MagicMock()
    upd.effective_chat.id = user_id
    upd.effective_user.id = user_id
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    upd.callback_query.edit_message_text = AsyncMock()
    upd.callback_query.edit_message_reply_markup = AsyncMock()
    return upd


def make_context():
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


@pytest.fixture
def screen(monkeypatch):
    s = Screen()
    monkeypatch.setattr(handlers, "show_panel", s)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    return s


def item(n, title=None, price=None):
    return {"item_id": f"i{n}", "title": title or f"Sony PlayStation 5 Slim nr{n}",
            "price": price or 300 + n, "currency": "EUR", "condition": "Gebraucht",
            "url": f"https://www.ebay.de/itm/{n}"}


def run(coro):
    return asyncio.run(coro)


# ---------- підпис кнопки оголошення ----------

@pytest.mark.parametrize("title,expected", [
    ("Sony PlayStation 5 Slim 825GB Digital Edition Weiß", "Slim 825GB Digital Weiß"),
    ("Sony PS5 Konsole 825GB", "PS5 825GB"),
])
def test_short_listing_label_drops_common_words(title, expected):
    assert handlers._short_listing_label(title, "PlayStation 5") == expected


def test_short_listing_label_is_truncated():
    label = handlers._short_listing_label("Sony PlayStation 5 " + "sehr langer Titel " * 5, "PlayStation 5")
    assert len(label) <= 26 and label.endswith("…")


# ---------- список товарів і картка ----------

def test_list_shows_names_not_ids(screen):
    db.add_watch(1, "PlayStation 5 Pro", "PlayStation 5 Pro", "", "", 25, categories=[{"id": "1", "name": "K"}])
    db.add_watch(1, "ThinkPad X1 Carbon Gen 10 i7 32GB 1TB Windows 11 Pro", "ThinkPad X1", "", "", 25)
    run(handlers.cmd_list(make_update(), make_context()))
    labels = screen.buttons()
    assert "📦 PlayStation 5 Pro" in labels
    assert any(b.startswith("📦 ThinkPad") and b.endswith("⚠️") for b in labels)  # без категорії
    assert all("#" not in b for b in labels)
    assert all(len(b) <= 45 for b in labels)
    assert "Мої товари (2)" in screen.text


def test_watch_details_text():
    wid = db.add_watch(1, "PS5", "PS5", "", "", 25)
    db.set_learned_word(wid, "ps3", "excluded")
    text = handlers._watch_details_text(db.get_watch(wid, 1))
    assert "Вигідно, якщо чистий прибуток" in text
    assert "Ціни ще не пораховані" in text and "8 оголошень" in text
    assert "ps3" in text
    assert f"#{wid}" not in text


def test_delete_asks_for_confirmation_by_name(screen):
    wid = db.add_watch(1, "PS5 Digital", "PS5 Digital", "", "", 25)
    run(handlers.delwatch_ask_callback(make_update(f"delwatch_ask:{wid}"), make_context()))
    assert "«PS5 Digital»" in screen.text
    assert screen.buttons() == ["✅ Так, видалити", "❌ Ні"]


# ---------- список оголошень: гортання ----------

def _state(n_items, fetch=None):
    return {"header": "<b>H</b>", "items": [item(i) for i in range(n_items)], "page": 0,
            "fetch": fetch, "nav": [("◀️ До товару", "x"), ("🏠 Меню", "menu:home")],
            "query": "PlayStation 5", "fetched_at": time.time()}


def test_paging_row_sits_right_above_navigation(screen):
    state = _state(25)
    run(handlers._render_listing_panel(make_update(), make_context(), 7, state))
    assert screen.rows[-3] == ["➡️ Наступні 10"]
    assert screen.rows[-2:] == [["◀️ До товару"], ["🏠 Меню"]]
    assert screen.rows[0][1:] == ["🙈 Сховати", "❌ Інший товар"]
    assert "Показано <b>1–10</b> з 25" in screen.text


def test_last_page_has_only_previous_button(screen):
    state = _state(25)
    state["page"] = 2
    run(handlers._render_listing_panel(make_update(), make_context(), 7, state))
    assert ["⬅️ Попередні 10"] in screen.rows
    assert "Показано <b>21–25</b> з 25" in screen.text
    assert "<b>21." in screen.text  # нумерація наскрізна


def test_more_on_ebay_shows_plus_and_next(screen):
    fetch = {"offset": 100, "exhausted": False, "seen": [], "pending": []}
    run(handlers._render_listing_panel(make_update(), make_context(), 7, _state(10, fetch)))
    assert "з 10+" in screen.text
    assert ["➡️ Наступні 10"] in screen.rows


def _fake_ebay_pages(monkeypatch, total=250, every=2):
    """Підміна пошуку: `total` оголошень від дешевших, підходить кожне `every`-те (0 — жодне)."""
    calls = []
    catalogue = [{"item_id": f"e{n}", "title": f"Sony PlayStation 5 nr{n}", "total_price": 300 + n,
                  "currency": "EUR", "condition": "Gebraucht", "url": f"https://x/{n}"} for n in range(total)]

    def fake_search(cats, limit, offset, fresh, sort, stats, **kw):
        calls.append(offset)
        page = catalogue[offset:offset + limit]
        stats["raw"] = len(page)
        return [dict(p) for p in page if every and int(p["item_id"][1:]) % every == 0]

    monkeypatch.setattr(handlers, "search_in_categories", fake_search)
    monkeypatch.setattr(handlers, "_annotate_items", lambda items, **kw: items)
    return calls


def test_view_listings_then_next_pages_load_lazily(screen, monkeypatch):
    calls = _fake_ebay_pages(monkeypatch)
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 25, categories=[{"id": "1", "name": "K"}])
    ctx = make_context()
    run(handlers.view_listings_callback(make_update(f"view_listings:{wid}"), ctx))
    assert calls == [0]
    assert "Показано <b>1–10</b>" in screen.text
    # друга сторінка — з уже завантаженої сотні, без нового запиту
    run(handlers.listing_page_callback(make_update(f"lpage:{wid}:1"), ctx))
    assert calls == [0] and "Показано <b>11–20</b>" in screen.text
    # шоста сторінка — перша сотня вичерпана, догружаємо наступну
    run(handlers.listing_page_callback(make_update(f"lpage:{wid}:5"), ctx))
    assert calls == [0, 100] and "Показано <b>51–60</b>" in screen.text


def test_reopening_listings_within_two_minutes_uses_cache(screen, monkeypatch):
    calls = _fake_ebay_pages(monkeypatch)
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 25, categories=[{"id": "1", "name": "K"}])
    ctx = make_context()
    run(handlers.view_listings_callback(make_update(f"view_listings:{wid}"), ctx))
    run(handlers.view_listings_callback(make_update(f"view_listings:{wid}"), ctx))
    assert calls == [0]


def test_no_listings_found_message(screen, monkeypatch):
    _fake_ebay_pages(monkeypatch, total=40, every=0)  # нічого не підходить
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 25, categories=[{"id": "1", "name": "K"}])
    run(handlers.view_listings_callback(make_update(f"view_listings:{wid}"), make_context()))
    assert "Підходящих оголошень не знайдено" in screen.text


# ---------- «🙈 Сховати» / «❌ Інший товар» ----------

@pytest.mark.parametrize("action,reason", [("hidel", "hidden"), ("rejl", "wrong")])
def test_listing_buttons_remove_item_and_remember(screen, action, reason):
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 25)
    ctx = make_context()
    ctx.user_data[handlers._listing_state_key(wid)] = _state(3)
    upd = make_update(f"{action}:{wid}:1")
    run(handlers.reject_listing_callback(upd, ctx))
    assert "i1" in db.get_rejected_ids(wid)
    assert "nr1" not in screen.text and "nr2" in screen.text
    with db.get_conn() as conn:
        assert conn.execute("SELECT reason FROM rejected_items WHERE item_id='i1'").fetchone()["reason"] == reason


def test_stale_listing_state_shows_alert(screen):
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 25)
    upd = make_update(f"rejl:{wid}:5")
    run(handlers.reject_listing_callback(upd, make_context()))
    assert upd.callback_query.answer.call_args.kwargs.get("show_alert") is True


def test_hide_from_deal_notification(screen):
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 25)
    deal_id = db.add_deal(wid, "d1", "Sony PlayStation 5 Riss", 200, "EUR", 450, 50, "https://x", False)
    upd = make_update(f"hided:{deal_id}")
    upd.callback_query.message.text = "🔥 Вигідна пропозиція: PlayStation 5"
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🙈 Сховати", callback_data=f"hided:{deal_id}"),
        InlineKeyboardButton("❌ Інший товар", callback_data=f"rejd:{deal_id}"),
    ]])
    run(handlers.reject_deal_callback(upd, make_context()))
    assert "d1" in db.get_rejected_ids(wid)
    assert "🙈 Сховано" in upd.callback_query.edit_message_text.call_args.args[0]


# ---------- головне меню й панель ----------

def test_owner_menu_has_refresh_button(monkeypatch):
    monkeypatch.setattr(panel, "is_owner", lambda uid: True)
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert labels == ["🔥 Вигідні пропозиції", "➕ Додати товар", "📦 Мої товари", "💡 Що перепродавати", "🔄 Оновити запити", "🔐 Акаунт eBay"]


def test_regular_user_menu_has_no_owner_buttons(monkeypatch):
    monkeypatch.setattr(panel, "is_owner", lambda uid: False)
    labels = [b.text for r in panel.build_main_menu(2).inline_keyboard for b in r]
    assert labels == ["🔥 Вигідні пропозиції", "➕ Додати товар", "📦 Мої товари", "💡 Що перепродавати"]


def _panel_context(panel_id=10, edit_error=None):
    ctx = make_context()
    ctx.user_data["panel_message_id"] = panel_id
    ctx.bot.edit_message_text = AsyncMock(side_effect=edit_error)
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    ctx.bot.delete_message = AsyncMock()
    return ctx


def test_panel_not_modified_does_not_send_second_menu():
    ctx = _panel_context(edit_error=BadRequest("Message is not modified"))
    upd = make_update()
    upd.callback_query = None
    upd.message.delete = AsyncMock()
    run(panel.show_panel(upd, ctx, "menu"))
    ctx.bot.send_message.assert_not_called()


def test_button_on_old_message_removes_previous_panel():
    ctx = _panel_context(panel_id=10)
    upd = make_update()
    upd.callback_query.message.message_id = 5
    run(panel.show_panel(upd, ctx, "menu"))
    ctx.bot.delete_message.assert_called_once_with(chat_id=1, message_id=10)
    assert ctx.user_data["panel_message_id"] == 5


def test_new_panel_replaces_broken_one():
    ctx = _panel_context(panel_id=10, edit_error=BadRequest("Message to edit not found"))
    upd = make_update()
    upd.callback_query = None
    upd.message.delete = AsyncMock()
    run(panel.show_panel(upd, ctx, "menu"))
    assert ctx.user_data["panel_message_id"] == 99
    ctx.bot.delete_message.assert_called_once_with(chat_id=1, message_id=10)


def test_refresh_usage_button_fetches_limits(monkeypatch):
    fetched = []
    monkeypatch.setattr(panel, "is_owner", lambda uid: True)
    monkeypatch.setattr(panel, "fetch_browse_rate_limit", lambda: fetched.append(1))
    monkeypatch.setattr(panel, "show_main_menu", AsyncMock())
    upd = make_update("menu:refresh_usage")
    run(panel.refresh_usage_callback(upd, make_context()))
    assert fetched == [1]
    assert upd.callback_query.answer.call_args.args[0] == "Оновлено ✅"


# ---------- сповіщення ----------

def test_grouped_deals_header_and_buttons(monkeypatch):
    sent = []

    async def fake_notify(app, chat_id, text, reply_markup=None, parse_mode=None):
        sent.append((text, reply_markup))

    monkeypatch.setattr(notifications, "notify", fake_notify)
    w = {"chat_id": 1, "label": "PS5", "id": 1}
    stat = {"sale_price": 450, "median_price": 480, "sale_source": "x"}
    deals = [(n, {"title": f"PS5 nr{n}", "total_price": 300, "suspicious": False, "has_best_offer": False,
                  "spec_group": "825GB", "url": "https://x"}, stat) for n in range(1, 7)]
    run(notifications._send_grouped_deals(MagicMock(), w, deals))
    text, markup = sent[0]
    assert text.startswith("🔥 Знайдено 6 вигідних пропозицій: PS5")
    first_row = [b.text for b in markup.inline_keyboard[0]]
    assert first_row == ["🙈 #1 сховати", "❌ #1 інший товар"]


# ---------- додавання товару: крок характеристик ----------

def _new_watch_context(categories=({"id": "139971", "name": "Konsolen"},)):
    ctx = make_context()
    ctx.user_data.update({
        "new_watch_query": "PlayStation 5",
        "new_watch_category_options": [dict(c, count=10) for c in categories],
        "new_watch_category_selected": {c["id"] for c in categories},
    })
    return ctx


ASPECTS = [{"name": "Speicherkapazität", "required": False}, {"name": "Plattform", "required": True}]


def test_after_categories_bot_asks_for_required_aspects(screen, monkeypatch):
    monkeypatch.setattr(handlers, "aspect_options_for_categories", lambda ids, q: list(ASPECTS))
    ctx = _new_watch_context()
    state = run(handlers.addwatch_category_choice(make_update("cat:done"), ctx))
    assert state == handlers.ASK_ASPECT
    assert "обов'язкові характеристики" in screen.text
    assert screen.rows[0] == ["⬜ Speicherkapazität"] and screen.rows[1] == ["⬜ Plattform ❗"]


def test_chosen_aspects_are_saved_with_new_watch(screen, monkeypatch):
    monkeypatch.setattr(handlers, "aspect_options_for_categories", lambda ids, q: list(ASPECTS))
    monkeypatch.setattr(handlers, "suggest_min_price", lambda q, ids: None)  # без кроку мін. ціни
    ctx = _new_watch_context()
    run(handlers.addwatch_category_choice(make_update("cat:done"), ctx))
    run(handlers.addwatch_aspect_choice(make_update("nasp:0"), ctx))
    assert screen.rows[0] == ["☑️ Speicherkapazität"]
    run(handlers.addwatch_aspect_choice(make_update("nasp:save"), ctx))
    watch = db.list_watches(chat_id=1)[0]
    assert db.get_required_aspects(watch) == ["Speicherkapazität"]
    assert "Обов'язкові характеристики: Speicherkapazität" in screen.text


@pytest.mark.parametrize("choice,require_spec", [("auto", None), ("none", 0)])
def test_auto_and_none_aspect_choices(screen, monkeypatch, choice, require_spec):
    monkeypatch.setattr(handlers, "aspect_options_for_categories", lambda ids, q: list(ASPECTS))
    monkeypatch.setattr(handlers, "suggest_min_price", lambda q, ids: None)
    ctx = _new_watch_context()
    run(handlers.addwatch_category_choice(make_update("cat:done"), ctx))
    run(handlers.addwatch_aspect_choice(make_update(f"nasp:{choice}"), ctx))
    watch = db.list_watches(chat_id=1)[0]
    assert db.get_required_aspects(watch) == [] and watch["require_spec"] == require_spec


def test_aspect_step_skipped_when_ebay_has_none(screen, monkeypatch):
    monkeypatch.setattr(handlers, "aspect_options_for_categories", lambda ids, q: [])
    monkeypatch.setattr(handlers, "suggest_min_price", lambda q, ids: (200, 500, 30))
    state = run(handlers.addwatch_category_choice(make_update("cat:done"), _new_watch_context()))
    assert state == handlers.ASK_MIN_PRICE_CHOICE


def test_refresh_prices_button_on_watch_screen_not_in_edit(screen):
    wid = db.add_watch(1, "PS5", "PS5", "", "", 25)
    run(handlers.watch_details_callback(make_update(f"watch_details:{wid}"), make_context()))
    assert "🔄 Оновити ціни" in screen.buttons()
    run(handlers.edit_menu_callback(make_update(f"editw:{wid}"), make_context()))
    assert "🔄 Оновити ціни" not in screen.buttons()