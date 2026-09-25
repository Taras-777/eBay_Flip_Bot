"""
Які ePID (номери продуктів каталогу eBay) трапляються в оголошеннях за запитом.

Запуск у папці бота (з активованим venv):
    python find_epid.py "iPhone 15 Pro 128GB"
    python find_epid.py "PS5 Slim" 139971        # другий аргумент — ID категорії (необов'язково)

Робить 1 запит до eBay Browse API (до 100 найновіших оголошень).
"""
import sys
from collections import Counter

import ebay_api
import settings


def find_epids(query, category_id=None):
    data = ebay_api._browse_search(query, condition_ids=settings.DEFAULT_CONDITION_IDS, limit=100, category_id=category_id)
    items = data.get("itemSummaries") or []
    counts, examples, prices = Counter(), {}, {}
    without = 0
    for it in items:
        epid = it.get("epid")
        if not epid:
            without += 1
            continue
        counts[epid] += 1
        examples.setdefault(epid, it.get("title") or "")
        try:
            prices.setdefault(epid, []).append(float((it.get("price") or {}).get("value")))
        except (TypeError, ValueError):
            pass

    print(f"Запит: {query!r}" + (f", категорія {category_id}" if category_id else ""))
    print(f"Оголошень: {len(items)}, з них прив'язані до каталогу: {len(items) - without}, без ePID: {without}\n")
    if not counts:
        print("Жодне оголошення не має ePID.")
        return
    for epid, n in counts.most_common(20):
        p = sorted(prices.get(epid, []))
        price_txt = f"{p[len(p) // 2]:.0f}€" if p else "—"
        print(f"ePID {epid:<12} {n:>3} огол.  медіана {price_txt:>6}  напр.: {examples[epid][:70]}")
        print(f"                  https://www.ebay.de/p/{epid}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Формат: python find_epid.py "назва товару" [ID категорії]')
        sys.exit(1)
    find_epids(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)