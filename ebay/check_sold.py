"""
Ручна перевірка Trading API: чи продано оголошення.

    python -m ebay.check_sold https://www.ebay.de/itm/298706741553
    python -m ebay.check_sold 298706741553

Потрібен підключений акаунт eBay («🔐 Акаунт eBay» у боті) у цій же базі.
"""

import sys

from core.db import init_db
from ebay.ebay_api import resolve_item_id
from ebay.ebay_user import UserAuthError, is_connected
from ebay.trading_api import get_item_status

LABELS = {
    "sold": "✅ ПРОДАНО",
    "unsold": "🚫 знято без продажу",
    "active": "↩️ ще продається",
    "not_found": "❔ eBay не знайшов оголошення (старше 90 днів або видалене)",
    "unknown": "❔ eBay не показав, скільки продано",
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    init_db()
    if not is_connected():
        print("Акаунт eBay не підключено — спершу увійди через «🔐 Акаунт eBay» в боті.")
        return
    item_id = resolve_item_id(" ".join(sys.argv[1:]))
    if not item_id:
        print("Не бачу номера оголошення.")
        return
    try:
        info = get_item_status(item_id)
    except UserAuthError as e:
        print(f"Помилка входу: {e}")
        return
    print(f"Оголошення {item_id}: {LABELS.get(info['result'], info['result'])}")
    print(f"  ListingStatus: {info.get('listing_status')}, QuantitySold: {info.get('quantity_sold')}")


if __name__ == "__main__":
    main()
