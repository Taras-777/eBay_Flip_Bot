"""Тексти екранів — коректний HTML для Telegram (усі теги закриті, лише дозволені)."""
import asyncio
from html.parser import HTMLParser

from core import db
from tests.test_sales import _inbox_setup, _screen

ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}


class _Check(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, (tag, self.stack)
        self.stack.pop()


def valid(text):
    p = _Check()
    p.feed(text)
    p.close()
    assert not p.stack, p.stack


def test_screens_have_valid_html(monkeypatch):
    from ui import account, panel, screen_profit, screen_stats
    _inbox_setup(3)
    db.add_watch(1, "PlayStation 5", "PlayStation 5", "", "", 15)
    handlers, shown, press = _screen(monkeypatch)
    valid(panel.main_menu_text(1))
    valid(account._settings_text("✅ ok"))
    valid(screen_stats.stats_text(1))
    for data, fn in (("deals:0", handlers.deals_callback), ("mprof:show", screen_profit.min_profit_callback),
                     ("cprof:consoles", screen_profit.category_profit_callback), ("menu:list", handlers.cmd_list),
                     ("menu:discover", handlers.discover_callback)):
        upd, ctx = press(data)
        ctx.user_data = {}
        asyncio.run(fn(upd, ctx))
        valid(shown[-1][0])
    wid = db.list_watches(chat_id=1)[0]["id"]
    for data, fn in ((f"watch_details:{wid}", handlers.watch_details_callback), (f"editw:{wid}", handlers.edit_menu_callback),
                     (f"wmp:{wid}", handlers.watch_min_profit_callback)):
        upd, ctx = press(data)
        ctx.user_data = {}
        asyncio.run(fn(upd, ctx))
        valid(shown[-1][0])


def test_appraise_texts_valid_html():
    from ui import screen_appraise
    valid(screen_appraise.PROMPT)
    base = {"title": "Apple <MacBook> & Pro", "spec": 'M3 Pro · 14" · 18GB', "total": 1250.0, "buy_limit": 1343.0,
            "profit": 153.0, "min_profit": 100.0}
    for est in ({"sale": 1450.0, "source": "sold", "group": 'M3 Pro · 14" · 18GB', "sold_count": 12,
                 "sold_range": (1380.0, 1520.0), "sold_days": 4.0},
                {"sale": 1500.0, "source": "listings", "group": 'M3 Pro · 14"', "listings_count": 9,
                 "few_sold": (2, 1450.0, 'M3 Pro · 14"')},
                {"sale": None, "few_sold": None}):
        valid(screen_appraise.estimate_text(dict(base, estimate=est)))
