"""🏪 магазин / 👤 приватний замість «нові / вживані»."""
import db
import market
import trading_api
from textparse import _group_label, market_group
from test_sold_check import SOLD_XML, make_gone


def test_group_by_seller_type_with_condition_fallback():
    assert market_group({"conditionId": "3000", "seller": {"sellerAccountType": "BUSINESS"}}) == "new"
    assert market_group({"conditionId": "1000", "seller": {"sellerAccountType": "INDIVIDUAL"}}) == "used"
    assert market_group({"conditionId": "7000", "seller": {"sellerAccountType": "INDIVIDUAL"}}) == "parts"
    assert market_group({"conditionId": "2500"}) == "new"           # eBay не віддав тип — за станом
    assert market_group({"conditionId": "3000"}) == "used"
    assert _group_label("new", "*") == "🏪 магазин, усі конфігурації"
    assert _group_label("used", "256GB") == "👤 приватні, 256GB"


def test_private_sealed_not_in_private_price_when_minority():
    rows = [{"cond_group": "used", "condition_id": "3000", "p": i} for i in range(8)]
    rows += [{"cond_group": "used", "condition_id": "1000", "p": 99}, {"cond_group": "new", "condition_id": "1000"}]
    kept = market.drop_private_sealed(rows)
    assert len(kept) == 9 and not any(r.get("p") == 99 for r in kept)        # магазинне нове лишається
    mostly_new = [{"cond_group": "used", "condition_id": "1000"}] * 5 + [{"cond_group": "used", "condition_id": "3000"}]
    assert market.drop_private_sealed(mostly_new) == mostly_new             # свіжа модель — нове і є ринок


def test_trading_seller_type_and_history_backfill(monkeypatch):
    xml = SOLD_XML.replace("<ItemID>", "<Seller><SellerInfo><SellerBusinessType>Commercial</SellerBusinessType>"
                                       "</SellerInfo></Seller><ItemID>")
    assert trading_api.parse_get_item(xml)["details"]["seller_type"] == "business"

    make_gone(1, "v1|555|0", 400)                  # стара історія: вживане, тип продавця невідомий
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET sold_check = 'sold' WHERE item_id = 'v1|555|0'")
    assert db.get_sold_without_seller_type(85, 10) == ["v1|555|0"]
    monkeypatch.setattr(trading_api, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "get_item_status", lambda item_id: trading_api.parse_get_item(xml))
    assert trading_api.backfill_seller_types(10) == 1
    with db.get_conn() as conn:
        row = conn.execute("SELECT cond_group, seller_type FROM listing_obs WHERE item_id = 'v1|555|0'").fetchone()
    assert (row["cond_group"], row["seller_type"]) == ("new", "business")   # насправді — магазин
    assert db.get_sold_without_seller_type(85, 10) == []                       # більше не питаємо
