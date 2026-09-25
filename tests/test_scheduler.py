"""
Наскрізний сценарій фонової перевірки з підробним eBay: ринкове сканування,
знахідка вигідного лота, відсутність дубля, відсіювання аксесуарів.
"""
import asyncio
import types

import db
import scheduler
from conftest import listing


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.messages.append(text)
        return types.SimpleNamespace(message_id=len(self.messages))

    async def delete_message(self, **kwargs):
        pass


def make_app():
    return types.SimpleNamespace(bot=FakeBot(), bot_data={}, user_data={})


def consoles(n=20):
    return [listing(f"c{i}", "Sony PlayStation 5 Slim 1TB Konsole", 450 + i * 5, created_ago_s=600 + i)
            for i in range(n)]


def test_deal_found_once(fake_ebay):
    fake_ebay.listings = consoles()
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()

    asyncio.run(scheduler.check_all_watches(app))       # ринок проаналізовано
    assert db.get_market_stats(wid)

    fake_ebay.listings = [listing("deal", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    deals = [m for m in app.bot.messages if "Вигідний лот" in m]
    assert len(deals) == 1 and "250€" in deals[0]

    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))       # той самий лот — без повтору
    assert not [m for m in app.bot.messages if "Вигідний лот" in m]


def test_accessory_with_compat_aspect_is_not_a_deal(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))

    fake_ebay.listings = [listing("case", "PlayStation 5 Slim 1TB Konsole Hülle Cover", 60)] + consoles()
    fake_ebay.aspects["case"] = {"Kompatibles Modell": "PlayStation 5 Slim"}
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    assert not [m for m in app.bot.messages if "Вигідний лот" in m]