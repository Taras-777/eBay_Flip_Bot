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
    deals = [m for m in app.bot.messages if "вигідн" in m.lower()]
    assert len(deals) == 1 and "250€" in deals[0]

    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))       # той самий лот — без повтору
    assert not [m for m in app.bot.messages if "вигідн" in m.lower()]


def test_accessory_with_compat_aspect_is_not_a_deal(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))

    fake_ebay.listings = [listing("case", "PlayStation 5 Slim 1TB Konsole Hülle Cover", 60)] + consoles()
    fake_ebay.aspects["case"] = {"Kompatibles Modell": "PlayStation 5 Slim"}
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    assert not [m for m in app.bot.messages if "вигідн" in m.lower()]


def test_price_drop_notifies_again(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))

    fake_ebay.listings = [listing("deal", "Sony PlayStation 5 Slim 1TB Konsole", 300)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    app.bot.messages.clear()
    fake_ebay.listings = [listing("deal", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    deals = [m for m in app.bot.messages if "вигідн" in m.lower()]
    assert len(deals) == 1 and "Продавець знизив ціну" in deals[0]


def test_many_deals_come_as_one_message(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))

    cheap = [listing(f"cheap{i}", "Sony PlayStation 5 Slim 1TB Konsole", 250 + i) for i in range(7)]
    fake_ebay.listings = cheap + consoles()
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    notices = [m for m in app.bot.messages if "вигідн" in m.lower()]
    assert len(notices) == 1 and "Нові вигідні пропозиції: 7" in notices[0]   # одне сповіщення, не 7
    deals, total = db.get_inbox_deals(1, limit=10)
    assert total == 7 and all(d["seen_at"] is None for d in deals)       # усі — у «🔥 Вигідні пропозиції»


def test_second_batch_edits_unread_notice_instead_of_new_message(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    edits = []

    async def edit_message_text(text, chat_id=None, message_id=None, reply_markup=None):
        edits.append(text)

    app.bot.edit_message_text = edit_message_text
    asyncio.run(scheduler.check_all_watches(app))
    fake_ebay.listings = [listing("d1", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    app.bot.messages.clear()
    fake_ebay.listings = [listing("d2", "Sony PlayStation 5 Slim 1TB Konsole", 255),
                          listing("d1", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    assert not [m for m in app.bot.messages if "вигідн" in m.lower()]   # без нового дзвіночка
    assert edits and "Нові вигідні пропозиції: 2" in edits[-1]