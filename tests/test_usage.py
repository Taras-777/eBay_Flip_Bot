"""📡 Статистика запитів: по днях, Browse/Trading/Taxonomy, сторінки, кнопка в налаштуваннях."""
import asyncio
from datetime import date, timedelta

import db
import screen_usage


def _seed(day, **counts):
    with db.get_conn() as conn:
        for api, n in counts.items():
            conn.execute("INSERT INTO api_usage (day, api, count) VALUES (?, ?, ?)", (day, api.replace("_", ":"), n))


def test_usage_by_day_text():
    today = date.today()
    _seed(today.isoformat(), browse=900, item=120, trading=40, trading_watch=30, trading_deals=10)
    _seed((today - timedelta(days=1)).isoformat(), browse=4500, browse_deals=60, taxonomy=3)
    _seed((today - timedelta(days=2)).isoformat(), browse=2500)
    _seed((today - timedelta(days=90)).isoformat(), browse=1)          # старіше за 60 днів — не показуємо
    data = db.api_usage_by_day(60)
    assert list(data)[0] == today.isoformat() and len(data) == 3
    text, pages = screen_usage.usage_text(0, today=today)
    assert pages == 1
    assert "(сьогодні)" in text and "(вчора)" in text
    assert "Browse: <b>900</b>" in text and "getItem): 120" in text
    assert "Trading: <b>40</b>" in text and "мої товари 30" in text and "🔥 10" in text
    assert "▰▰▰▰▰▰▰▰▰▰ 100%" in text and "перевірка 🔥: 60" in text and "Taxonomy): 3" in text
    assert "У середньому за повну добу: Browse <b>3500</b>, найбільше 4500 (2 дн.)" in text
    assert text.index("(вчора)") < text.index("(сьогодні)")            # сьогодні — внизу


def test_usage_pages_and_empty():
    text, pages = screen_usage.usage_text(0)
    assert "ще не записано" in text and pages == 1
    today = date.today()
    for i in range(23):
        _seed((today - timedelta(days=i)).isoformat(), browse=100 + i)
    _, pages = screen_usage.usage_text(0, today=today)
    assert pages == 3
    text, _ = screen_usage.usage_text(2, today=today)
    assert "Сторінка 3 з 3" in text and "Browse: <b>122</b>" in text and "Browse: <b>100</b>" not in text
    assert text.index("<b>122</b>") < text.index("<b>120</b>")         # старіший день — вище
    kb = screen_usage.usage_keyboard(1, 3)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert labels[:2] == ["◀️ Старіші", "Новіші ▶️"] and "◀️ До налаштувань" in labels


def test_settings_button_and_callback(monkeypatch):
    import account
    from test_deal_reprice import _record
    from test_sales import _screen
    _, shown, press = _screen(monkeypatch)
    monkeypatch.setattr(account, "show_panel", lambda u, c, text, reply_markup=None, parse_mode=None:
                        _record(shown, text, reply_markup))
    monkeypatch.setattr(screen_usage, "show_panel", lambda u, c, text, reply_markup=None, parse_mode=None:
                        _record(shown, text, reply_markup))
    upd, ctx = press("soldonly")
    asyncio.run(account.sold_only_callback(upd, ctx))
    assert "📡 Статистика запитів" in shown[-1][1]
    _seed(date.today().isoformat(), browse=5)
    upd, ctx = press("apistat:0")
    asyncio.run(screen_usage.usage_callback(upd, ctx))
    assert "Статистика запитів до eBay" in shown[-1][0] and "🔄 Оновити" in shown[-1][1]
