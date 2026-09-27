"""«🔍 Перевірити оголошення»: розпізнавання посилань і пояснення фільтрів."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import access
import checker
import db
import ebay_api
import handlers
import learning
from conftest import FakeResponse


# ---------- посилання ----------

@pytest.mark.parametrize("text", [
    "https://www.ebay.de/itm/298706741553",
    "https://www.ebay.de/itm/298706741553?media=COPY&mkcid=16",
    "Schau mal: https://www.ebay.de/itm/apple-iphone-16-pro/298706741553 cool",
    "298706741553",
])
def test_item_id_from_text(text):
    assert ebay_api.resolve_item_id(text) == "298706741553"


def test_item_id_from_short_link(monkeypatch):
    class Session:
        def get(self, url, allow_redirects, timeout):
            location = {"https://ebay.io/m/stFIkr": "https://www.ebay.de/itm/298706741553?media=COPY"}
            return MagicMock(headers={"Location": location.get(url)} if url in location else {})

    monkeypatch.setattr(ebay_api, "_http_session", lambda: Session())
    assert ebay_api.resolve_item_id("https://ebay.io/m/stFIkr") == "298706741553"


def test_text_without_link():
    assert ebay_api.resolve_item_id("привіт") is None


# ---------- перевірка фільтрів ----------

def good_item(**changes):
    item = {
        "itemId": "v1|298706741553|0",
        "title": "Apple iPhone 16 Pro 128 GB Schwarz",
        "itemWebUrl": "https://www.ebay.de/itm/298706741553",
        "price": {"value": "450.00", "currency": "EUR"},
        "shippingOptions": [{"shippingCost": {"value": "6.19", "currency": "EUR"}}],
        "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
        "condition": "Gebraucht", "conditionId": "3000",
        "itemLocation": {"country": "DE"},
        "categoryId": "9355", "categoryIdPath": "15032|9355",
        "categoryPath": "Handys & Kommunikation|Handys & Smartphones",
        "localizedAspects": [{"name": "Speicherkapazität", "value": "128 GB"},
                             {"name": "Modell", "value": "Apple iPhone 16 Pro"}],
        "seller": {"feedbackScore": 120, "feedbackPercentage": "100.0"},
    }
    item.update(changes)
    return item


@pytest.fixture
def ebay_item(monkeypatch):
    holder = {"item": good_item()}
    monkeypatch.setattr(checker, "fetch_item_by_legacy_id", lambda legacy_id: holder["item"])
    return holder


def iphone_watch(**changes):
    wid = db.add_watch(1, "Iphone 16 pro", "Iphone 16 pro", "defekt kaputt", "1000,1500,2000,2500,3000", 25,
                       categories=[{"id": "9355", "name": "Handys & Smartphones"}])
    for key, value in changes.items():
        with db.get_conn() as conn:
            conn.execute(f"UPDATE watches SET {key} = ? WHERE id = ?", (value, wid))
    return db.get_watch(wid, 1)


def run_check(watch, text="https://www.ebay.de/itm/298706741553"):
    return checker.check_listing(watch, text)


def failed(checks):
    return [t for mark, t in checks if mark == checker.FAIL]


def test_good_listing_without_market_prices(ebay_item):
    title, url, checks, verdict = run_check(iphone_watch())
    assert title == "Apple iPhone 16 Pro 128 GB Schwarz"
    assert failed(checks) == []
    assert verdict.startswith("✅")
    assert any("Доставка в Німеччину: 6.19€" in t for _, t in checks)
    assert any("ще не пораховані" in t for _, t in checks)
    assert "ціни для цієї групи ще не пораховані" in verdict and "Оновити ціни" in verdict


def test_good_listing_that_is_a_deal(ebay_item):
    w = iphone_watch()
    db.upsert_market_stats(w["id"], "used", "128GB", 650, 20, sale_price=620, sale_source="x")
    _, _, checks, verdict = run_check(w)
    assert any(t.startswith("Вигідно") for _, t in checks)
    assert "вигідне" in verdict and "200 найновіших" in verdict


def test_good_listing_that_is_not_a_deal(ebay_item):
    w = iphone_watch()
    db.upsert_market_stats(w["id"], "used", "128GB", 480, 20, sale_price=470, sale_source="x")
    _, _, checks, verdict = run_check(w)
    assert any(t.startswith("Невигідно") for _, t in checks)
    assert "не вигідне" in verdict


@pytest.mark.parametrize("changes,reason", [
    ({"shippingOptions": []}, "Немає доставки в Німеччину"),
    ({"buyingOptions": ["AUCTION"]}, "Аукціон"),
    ({"conditionId": "7000", "condition": "Als Ersatzteil"}, "на запчастини"),
    ({"itemLocation": {"country": "US"}}, "поза ЄС"),
    ({"categoryIdPath": "15032|20349"}, "не входить у вибрані категорії"),
    ({"title": "Apple iPhone 16 Pro 128 GB defekt"}, "виключені слова"),
    ({"title": "Hülle für iPhone 16 Pro"}, "Назва не схожа"),
    ({"localizedAspects": [{"name": "Kompatibles Modell", "value": "iPhone 16 Pro"}]}, "аксесуари"),
    ({"itemEndDate": "2020-01-01T00:00:00.000Z"}, "вже завершене"),
])
def test_rejection_reasons(ebay_item, changes, reason):
    ebay_item["item"] = good_item(**changes)
    _, _, checks, verdict = run_check(iphone_watch())
    assert any(reason in t for t in failed(checks)), failed(checks)
    assert verdict.startswith("❌ Відсіяно")


def test_min_price_reason(ebay_item):
    _, _, checks, _ = run_check(iphone_watch(min_price=500))
    assert any("нижча за мінімальну ціну" in t for t in failed(checks))


def test_missing_required_aspect(ebay_item):
    _, _, checks, _ = run_check(iphone_watch(required_aspect='["Netzwerk"]', require_spec=0))
    assert any("Netzwerk" in t for t in failed(checks))


def test_hidden_listing_reason(ebay_item):
    w = iphone_watch()
    learning.hide_item(w, "v1|298706741553|0", "Apple iPhone 16 Pro 128 GB Schwarz")
    _, _, checks, _ = run_check(w)
    assert any("Сховати" in t for t in failed(checks))


def test_unknown_link_and_missing_listing(monkeypatch):
    with pytest.raises(checker.CheckError):
        run_check(iphone_watch(), "просто текст")
    monkeypatch.setattr(checker, "fetch_item_by_legacy_id", lambda legacy_id: None)
    with pytest.raises(checker.CheckError, match="не знайшов"):
        run_check(iphone_watch())


def test_listing_not_found_status(monkeypatch):
    monkeypatch.setattr(ebay_api, "_get_access_token", lambda: "TOKEN")
    monkeypatch.setattr(ebay_api, "_request_with_retries", lambda *a, **k: FakeResponse({}, status=404))
    assert ebay_api.fetch_item_by_legacy_id("1") is None


# ---------- екран у боті ----------

def test_check_flow_in_bot(monkeypatch, ebay_item):
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [[b.text for b in r] for r in reply_markup.inline_keyboard] if reply_markup else []))

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    w = iphone_watch()
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.data = f"chkl:{w['id']}"
    upd.callback_query.answer = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {}

    state = asyncio.run(handlers.check_listing_start(upd, ctx))
    assert state == handlers.CHECK_LINK and "Надішли посилання" in shown[-1][0]

    upd.message.text = "https://www.ebay.de/itm/298706741553"
    state = asyncio.run(handlers.check_listing_text(upd, ctx))
    text, rows = shown[-1]
    assert state == handlers.ConversationHandler.END
    assert "Apple iPhone 16 Pro 128 GB Schwarz" in text and "✅ Проходить" in text
    assert ["🔍 Перевірити інше"] in rows


def test_watch_screen_has_check_button(monkeypatch):
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append([b.text for r in reply_markup.inline_keyboard for b in r])

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    asyncio.run(handlers._show_watch_details(MagicMock(), MagicMock(), iphone_watch()))
    assert "🔍 Перевірити оголошення" in shown[-1]


def test_new_seller_warning(ebay_item):
    ebay_item["item"] = good_item(seller={"feedbackScore": 0, "feedbackPercentage": "0.0"})
    _, _, checks, verdict = run_check(iphone_watch())
    assert any(m == checker.WARN and "немає відгуків" in t for m, t in checks)
    assert verdict.startswith("✅")  # попередження не відсіює