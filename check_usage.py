"""
Перевірка використання eBay API.

1) Офіційні дані eBay (Developer Analytics API, getRateLimits) — по ВСІХ
   запитах з цих ключів, звідки б вони не йшли (сервер, ПК, тести).
2) Власний лічильник бота з локальної бази — лише запити цієї копії бота.

Запуск у папці бота (з активованим venv):
    python check_usage.py
"""
import sqlite3
from datetime import datetime

import ebay_api
import settings

RATE_LIMIT_URL = "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/"


def fmt_window(seconds):
    seconds = int(seconds or 0)
    if seconds % 86400 == 0 and seconds:
        return f"{seconds // 86400} доб."
    if seconds % 3600 == 0 and seconds:
        return f"{seconds // 3600} год"
    return f"{seconds} с"


def official_usage():
    print("=== Офіційні дані eBay (усі запити з цих ключів) ===")
    token = ebay_api._get_access_token()
    resp = ebay_api._request_with_retries("GET", RATE_LIMIT_URL, headers={"Authorization": "Bearer " + token}, timeout=20)
    resp.raise_for_status()
    rows = []
    for api in resp.json().get("rateLimits") or []:
        api_name = f"{api.get('apiContext', '')}/{api.get('apiName', '')}"
        for resource in api.get("resources") or []:
            for rate in resource.get("rates") or []:
                limit = int(rate.get("limit") or 0)
                remaining = int(rate.get("remaining") or 0)
                count = rate.get("count")
                count = int(count) if count is not None else max(limit - remaining, 0)
                reset = ebay_api._parse_ebay_ts(rate.get("reset"))
                reset_txt = datetime.fromtimestamp(reset, settings.LOCAL_TZ).strftime("%d.%m %H:%M") if reset else "—"
                rows.append((api_name, resource.get("name", ""), fmt_window(rate.get("timeWindow")),
                             count, limit, remaining, reset_txt))
    if not rows:
        print("eBay не повернув даних.")
        return
    header = ("API", "Ресурс", "Вікно", "Зроблено", "Ліміт", "Залишилось", "Скидання")
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(len(header))]
    line = "  ".join(f"{{:<{w}}}" for w in widths)
    print(line.format(*header))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line.format(*r))


def local_usage(days=7):
    print(f"\n=== Власний лічильник цієї копії бота (UTC-доби, останні {days}) ===")
    try:
        conn = sqlite3.connect(settings.DB_PATH)
        data = conn.execute(
            "SELECT day, api, count FROM api_usage ORDER BY day DESC, api LIMIT ?", (days * 4,)
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"Не вдалося прочитати базу: {e}")
        return
    if not data:
        print("Записів ще немає.")
        return
    names = {"browse": "Browse (пошук + характеристики)", "item": "   з них характеристики лотів",
             "taxonomy": "Taxonomy (категорії)"}
    for day, api, count in data:
        print(f"{day}  {names.get(api, api):<34} {count}")


if __name__ == "__main__":
    try:
        official_usage()
    except Exception as e:
        print(f"Не вдалося отримати офіційні дані eBay: {e}")
    local_usage()