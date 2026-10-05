"""
Перевірка «справді продано?» через Trading API (GetItem).

Browse API завершених оголошень не показує — вони просто зникають з видачі.
Trading API повертає й завершені оголошення (до 90 днів) зі статусом і
кількістю проданих штук. Тож для кожного зниклого лота бот один раз питає:
  * продано (QuantitySold ≥ 1)  → ціна йде в статистику продажів;
  * знято без продажу           → не рахується;
  * ще активне (випало з видачі) → не рахується.
Потрібен вхід в акаунт eBay власника (ebay_user.py).
"""

import xml.etree.ElementTree as ET
from datetime import datetime

from defusedxml.ElementTree import fromstring as safe_fromstring

from settings import SOLD_CHECK_BATCH, SPEC_BACKFILL_BATCH, TRADING_DAILY_BUDGET, log
from db import (
    apply_discovery_check,
    apply_sold_check,
    get_pending_discovery_checks,
    get_pending_sold_checks,
    get_sold_without_seller_type,
    known_sold_check,
    mark_market_stale,
    record_api_call,
    save_item_aspects,
    set_seller_type,
)
from ebay_api import _request_with_retries, trading_calls_today
from ebay_user import UserAuthError, _token_cache, get_user_access_token, is_connected

TRADING_URL = "https://api.ebay.com/ws/api.dll"
SITE_ID_DE = "77"
COMPATIBILITY_LEVEL = "1349"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}

NOT_FOUND_CODES = {"17", "21916618"}                 # оголошення не існує / недоступне
TRANSIENT_CODES = {"10007", "518", "21919144"}       # внутрішня помилка eBay / ліміт запитів
AUTH_ERROR_CODES = {"931", "932", "16110", "21916984", "21917053"}  # токен недійсний


def legacy_item_id(item_id):
    """«v1|298706741553|0» → «298706741553»."""
    parts = str(item_id or "").split("|")
    return parts[1] if len(parts) >= 2 else parts[0]


def _text(node, path):
    found = node.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


def parse_get_item(xml_text):
    """Відповідь GetItem → {'result': sold|unsold|active|not_found|unknown, ...}."""
    root = safe_fromstring(xml_text)   # захист від «XML-бомб» у відповіді
    codes = {c.text.strip() for c in root.findall("e:Errors/e:ErrorCode", NS) if c.text}
    ack = _text(root, "e:Ack")
    if ack == "Failure" or root.find("e:Item", NS) is None:
        if codes & AUTH_ERROR_CODES:
            raise UserAuthError("eBay не прийняв вхід в акаунт — увійди знову")
        if codes & NOT_FOUND_CODES:
            return {"result": "not_found"}
        message = "; ".join(
            f"{_text(e, 'e:ErrorCode')}: {_text(e, 'e:LongMessage') or _text(e, 'e:ShortMessage') or ''}".strip()
            for e in root.findall("e:Errors", NS)
        ) or ack
        if not codes or codes & TRANSIENT_CODES:
            # збій на боці eBay — лот лишається в черзі, перевіримо наступного разу
            raise RuntimeError(f"Trading API: тимчасова помилка {message}")
        # Помилка саме цього оголошення (напр. недоступне для перегляду) — повторювати
        # марно: позначаємо «eBay не відповів», лишається оцінка за зникненням
        return {"result": "unknown", "error": message}

    item = root.find("e:Item", NS)
    listing_status = _text(item, "e:SellingStatus/e:ListingStatus")
    sold_text = _text(item, "e:SellingStatus/e:QuantitySold")
    info = {"listing_status": listing_status, "quantity_sold": int(sold_text) if sold_text else None,
            "details": _sale_details(item)}
    if listing_status == "Active":
        info["result"] = "active"
    elif sold_text is None:
        info["result"] = "unknown"   # eBay не показав, скільки продано, — не вгадуємо
    else:
        info["result"] = "sold" if info["quantity_sold"] >= 1 else "unsold"
    return info


def _num(text, cast=float):
    try:
        return cast(text) if text is not None else None
    except ValueError:
        return None


def _ts(text):
    """«2026-10-01T12:34:56.000Z» → unix-час."""
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) if text else None
    except ValueError:
        return None


def _sale_details(item):
    """Деталі завершення з тієї самої відповіді GetItem — лише для історії (додаткових запитів немає).
    sold_price — фінальна ціна без доставки, у валюті сайту (EUR)."""
    price = _text(item, "e:SellingStatus/e:ConvertedCurrentPrice") or _text(item, "e:SellingStatus/e:CurrentPrice")
    return {
        "sold_price": _num(price),
        "sold_at": _ts(_text(item, "e:ListingDetails/e:EndTime")),
        "bid_count": _num(_text(item, "e:SellingStatus/e:BidCount"), int),
        "offer_count": _num(_text(item, "e:BestOfferDetails/e:BestOfferCount"), int),
        "quantity": _num(_text(item, "e:Quantity"), int),
        "listing_type": _text(item, "e:ListingType"),
        "watch_count": _num(_text(item, "e:WatchCount"), int),   # скільки людей стежать
        "aspects": _item_specifics(item),
        # 🏪 магазин / 👤 приватний (лише тип; ім'я продавця не зберігаємо)
        "seller_type": {"Commercial": "business", "Private": "individual"}.get(
            _text(item, "e:Seller/e:SellerInfo/e:SellerBusinessType") or "", "unknown"),
    }


def _item_specifics(item):
    """Характеристики оголошення (як у пошуку: {назва в нижньому регістрі: значення}) або None."""
    node = item.find("e:ItemSpecifics", NS)
    if node is None:
        return None
    result = {}
    for pair in node.findall("e:NameValueList", NS):
        name = _text(pair, "e:Name")
        values = [v.text.strip() for v in pair.findall("e:Value", NS) if v.text and v.text.strip()]
        if name and values:
            result[name.lower()] = ", ".join(values)
    return result


def get_item_status(item_id):
    """Статус оголошення в eBay. Кидає UserAuthError, якщо вхід в акаунт недійсний."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{legacy_item_id(item_id)}</ItemID>"
        "<IncludeWatchCount>true</IncludeWatchCount>"
        "<IncludeItemSpecifics>true</IncludeItemSpecifics>"
        "<OutputSelector>Item.ItemID</OutputSelector>"
        "<OutputSelector>Item.SellingStatus</OutputSelector>"
        "<OutputSelector>Item.ListingDetails.EndTime</OutputSelector>"
        "<OutputSelector>Item.BestOfferDetails</OutputSelector>"
        "<OutputSelector>Item.Quantity</OutputSelector>"
        "<OutputSelector>Item.ListingType</OutputSelector>"
        "<OutputSelector>Item.WatchCount</OutputSelector>"
        "<OutputSelector>Item.ItemSpecifics</OutputSelector>"
        "<OutputSelector>Item.Seller.SellerInfo.SellerBusinessType</OutputSelector>"
        "</GetItemRequest>"
    )
    resp = _request_with_retries(
        "POST", TRADING_URL,
        headers={
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": SITE_ID_DE,
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
            "X-EBAY-API-IAF-TOKEN": get_user_access_token(),
            "Content-Type": "text/xml; charset=utf-8",
        },
        data=body.encode("utf-8"), timeout=20,
    )
    record_api_call("trading")
    try:
        return parse_get_item(resp.text)
    except UserAuthError:
        _token_cache.update(token=None, expires_at=0.0)  # наступного разу — свіжий токен
        raise
    except ET.ParseError:
        raise RuntimeError(f"Trading API: незрозуміла відповідь (HTTP {resp.status_code})")


def fetch_buy_it_now(item_id):
    """Ціна «купити зараз» оголошення «аукціон + купити зараз» (EUR) або None."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{legacy_item_id(item_id)}</ItemID>"
        "<OutputSelector>Item.ItemID</OutputSelector>"
        "<OutputSelector>Item.BuyItNowPrice</OutputSelector>"
        "<OutputSelector>Item.ListingDetails.ConvertedBuyItNowPrice</OutputSelector>"
        "</GetItemRequest>"
    )
    resp = _request_with_retries(
        "POST", TRADING_URL,
        headers={
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": SITE_ID_DE,
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
            "X-EBAY-API-IAF-TOKEN": get_user_access_token(),
            "Content-Type": "text/xml; charset=utf-8",
        },
        data=body.encode("utf-8"), timeout=20,
    )
    record_api_call("trading")
    return parse_buy_it_now(resp.text)


def parse_buy_it_now(xml_text):
    root = safe_fromstring(xml_text)
    item = root.find("e:Item", NS)
    if item is None:
        return None
    price = _num(_text(item, "e:ListingDetails/e:ConvertedBuyItNowPrice") or _text(item, "e:BuyItNowPrice"))
    return price if price else None   # 0 — «купити зараз» уже недоступне


def _purpose(apply):
    """Для розбивки запитів у «⚙️ Налаштування»: твої товари чи «💡 Що перепродавати»."""
    return "discovery" if apply is apply_discovery_check else "watch"


def _run_checks(queue):
    """queue: [(apply, (власник, item_id))] → {результат: кількість}."""
    counts = {}
    with_aspects: list = []
    for apply, (owner, item_id) in queue:
        known = known_sold_check(item_id)
        if known:
            # Цей лот уже перевіряли для іншого товару (спільний ринок) — eBay не питаємо
            apply(owner, item_id, known)
            counts[known] = counts.get(known, 0) + 1
            continue
        record_api_call(f"trading:{_purpose(apply)}")
        try:
            info = get_item_status(item_id)
        except UserAuthError as e:
            log.warning("Перевірка продажів зупинена: %s", e)
            break
        except Exception as e:
            log.warning("Не вдалося перевірити оголошення %s: %s", item_id, e)
            continue
        if info.get("error"):
            log.info("eBay не віддав статус оголошення %s (%s) — рахую за зникненням", item_id, info["error"])
        apply(owner, item_id, info["result"], info.get("details"))
        counts[info["result"]] = counts.get(info["result"], 0) + 1
        _keep_seller_type(item_id, info)
        # Характеристики з тієї самої відповіді — у базу: далі бот бере їх звідти, а не з eBay
        if _keep_aspects(item_id, info):
            with_aspects.append(item_id)
    _reclassify(with_aspects)
    return counts


def _keep_aspects(item_id, info):
    """Характеристики з тієї самої відповіді — у кеш (ноутбуки отримають повний клас). True, якщо збережено."""
    aspects = (info.get("details") or {}).get("aspects")
    if aspects is None:
        return False
    save_item_aspects(item_id, aspects)
    return True


def _keep_seller_type(item_id, info):
    """Тип продавця з тієї самої відповіді — в історію (група магазин/приватний). Змінені товари
    перераховуються найближчим циклом."""
    # Немає відповіді з деталями (оголошення вже недоступне) — «невідомо», щоб більше не питати
    seller_type = (info.get("details") or {}).get("seller_type") or "unknown"
    for watch_id in set_seller_type(item_id, seller_type):
        mark_market_stale(watch_id)


SELLER_BACKFILL_DAYS = 85   # eBay віддає завершені оголошення ~90 днів


def backfill_seller_types(limit=SPEC_BACKFILL_BATCH):
    """Стара історія продажів без типу продавця — дочитуємо через Trading API з вільного запасу
    (по `limit` за цикл, кожне оголошення один раз; заодно й характеристики). Повертає кількість запитів."""
    if not is_connected():
        return 0
    ids = get_sold_without_seller_type(SELLER_BACKFILL_DAYS, _budget_left(limit))
    done, with_aspects = 0, []
    for item_id in ids:
        record_api_call("trading:watch")
        try:
            info = get_item_status(item_id)
        except UserAuthError as e:
            log.warning("Дочитування типу продавців зупинено: %s", e)
            break
        except Exception as e:
            log.debug("Не вдалося прочитати продавця %s: %s", item_id, e)
            continue
        done += 1
        if info.get("details"):
            _keep_seller_type(item_id, info)
            if _keep_aspects(item_id, info):
                with_aspects.append(item_id)
        else:
            set_seller_type(item_id, "unknown")   # eBay уже не віддає — більше не питаємо
    _reclassify(with_aspects)
    if done:
        log.info("Дочитано тип продавця (магазин/приватний) для проданих: %s", done)
    return done


def _reclassify(item_ids):
    if not item_ids:
        return
    from market import reclassify_items   # тут, щоб не було циклу імпортів
    try:
        reclassify_items(item_ids)
    except Exception as e:
        log.warning("Не вдалося перерахувати класи за характеристиками: %s", e)


def backfill_laptop_specs(limit=SPEC_BACKFILL_BATCH):
    """Продані ноутбуки з неповним класом (без покоління процесора чи пам'яті), характеристик яких
    бот не встиг прочитати, — читаємо через Trading API (eBay віддає їх до 90 днів після завершення).
    Кожне оголошення — один раз. Повертає кількість запитів. Викликати з потоку."""
    if not is_connected():
        return 0
    from market import laptop_backfill_ids   # тут, щоб не було циклу імпортів
    ids = laptop_backfill_ids(_budget_left(limit))
    done, with_aspects = 0, []
    for item_id in ids:
        record_api_call("trading:watch")
        try:
            info = get_item_status(item_id)
        except UserAuthError as e:
            log.warning("Дочитування характеристик зупинено: %s", e)
            break
        except Exception as e:
            log.debug("Не вдалося прочитати характеристики %s: %s", item_id, e)
            continue
        done += 1
        if not _keep_aspects(item_id, info):
            save_item_aspects(item_id, {})   # eBay не віддав — більше не питаємо
        else:
            with_aspects.append(item_id)
    _reclassify(with_aspects)
    if done:
        log.info("Дочитано характеристики проданих ноутбуків: %s", done)
    return done


def _budget_left(limit):
    return min(limit, TRADING_DAILY_BUDGET - trading_calls_today()[0])


def verify_disappeared(limit=SOLD_CHECK_BATCH):
    """Перевіряє чергу зниклих лотів: спершу твоїх товарів, потім «💡 Що перепродавати».
    Повертає кількість перевірених. Викликати з потоку (asyncio.to_thread)."""
    if not is_connected():
        return 0
    left = _budget_left(limit)
    if left <= 0:
        return 0
    queue = [(apply_sold_check, (r["watch_id"], r["item_id"])) for r in get_pending_sold_checks(left)]
    if len(queue) < left:
        queue += [(apply_discovery_check, (r["candidate"], r["item_id"]))
                  for r in get_pending_discovery_checks(left - len(queue))]
    counts = _run_checks(queue)
    done = sum(counts.values())
    if done:
        log.info("Перевірено зниклих оголошень: %s (%s)", done,
                 ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if done < left:   # черга порожня — вільні запити на дочитування характеристик ноутбуків
        done += backfill_laptop_specs(min(SPEC_BACKFILL_BATCH, left - done))
    if done < left:   # і на тип продавця в старій історії продажів
        done += backfill_seller_types(min(SPEC_BACKFILL_BATCH, left - done))
    return done


def verify_watch_now(watch_id, limit=100):
    """«⏳ Перевірити зараз» — черга одного товару поза розкладом. Повертає {результат: кількість}.
    Викликати з потоку."""
    if not is_connected():
        return {}
    left = _budget_left(limit)
    if left <= 0:
        return {}
    rows = get_pending_sold_checks(left, watch_id=watch_id)
    return _run_checks([(apply_sold_check, (r["watch_id"], r["item_id"])) for r in rows])
