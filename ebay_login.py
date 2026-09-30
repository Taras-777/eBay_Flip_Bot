"""
Вхід в акаунт eBay з терміналу (для сервера) — те саме, що «🔐 Акаунт eBay» у боті.

У Docker на сервері:
    sudo docker exec -it ebay-bot python ebay_login.py

Без Docker (ПК):
    python ebay_login.py

Перевірити стан / відключити:
    python ebay_login.py status
    python ebay_login.py logout
"""

import sys
from datetime import datetime

from settings import LOCAL_TZ
from db import init_db
from ebay_user import (
    UserAuthError,
    connection_info,
    consent_url,
    disconnect,
    exchange_code,
    extract_code,
    get_user_access_token,
    is_configured,
    is_connected,
)


def _date(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m.%Y") if ts else "?"


def status():
    if not is_connected():
        print("❌ Акаунт eBay не підключено.")
        return
    connected_at, expires_at = connection_info()
    try:
        get_user_access_token()
        works = "вхід працює"
    except Exception as e:
        works = f"вхід НЕ працює: {e}"
    print(f"✅ Підключено з {_date(connected_at)}, діє до {_date(expires_at)} — {works}")


def login():
    if not is_configured():
        print("❌ Не задано EBAY_RUNAME (у .env на сервері або в config.py на ПК).")
        return
    if is_connected():
        status()
        if input("Увійти заново? [y/N]: ").strip().lower() not in ("y", "yes", "т", "так"):
            return
    print("\n1. Відкрий це посилання в браузері, увійди в eBay і натисни «Zustimmen / Agree»:\n")
    print(consent_url())
    print("\n2. Скопіюй адресу сторінки, яка відкриється після входу, і встав сюди (є 5 хвилин).")
    for _ in range(3):
        text = input("\nАдреса сторінки: ").strip()
        code = extract_code(text)
        if not code:
            print("⚠️ У цій адресі немає коду входу (code=…). Спробуй ще раз.")
            continue
        try:
            exchange_code(code)
        except UserAuthError as e:
            print(f"⚠️ eBay не прийняв код: {e}. Можливо, минуло більше 5 хвилин — увійди ще раз за посиланням.")
            continue
        print("\n✅ Акаунт eBay підключено. Бот сам почне перевіряти продажі в найближчому циклі.")
        return
    print("\nНе вдалося. Запусти скрипт ще раз.")


def main():
    init_db()
    command = sys.argv[1] if len(sys.argv) > 1 else "login"
    if command == "status":
        status()
    elif command == "logout":
        disconnect()
        print("🔌 Акаунт eBay відключено.")
    else:
        login()


if __name__ == "__main__":
    main()
