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
