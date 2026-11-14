"""
Вхід в акаунт eBay власника (OAuth «authorization code»). Потрібен лише для
Trading API — перевірки, чи зникле оголошення справді продали.

Бот не бачить пароля: власник входить на сторінці eBay, а боту надсилає
адресу сторінки «вхід виконано», в якій є одноразовий код. Код обмінюється на
refresh-токен (діє ~18 місяців), з якого бот сам отримує короткі токени доступу.
Токен зберігається в базі бота (папка data на сервері, не в git).
"""

import base64
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import config
from core.settings import EBAY_RUNAME, log
from core.db import get_meta, set_meta
from ebay.ebay_api import OAUTH_URL, _request_with_retries

AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"
USER_SCOPES = "https://api.ebay.com/oauth/api_scope"

_REFRESH_KEY = "ebay_user_refresh_token"
_REFRESH_EXPIRES_KEY = "ebay_user_refresh_expires_at"
_CONNECTED_AT_KEY = "ebay_user_connected_at"
_LOST_KEY = "ebay_login_lost"                   # коли eBay скасував вхід (refresh-токен недійсний)
_LOST_NOTIFIED_KEY = "ebay_login_lost_notified"
LOGIN_WARN_DAYS = (14, 3)                       # нагадати власнику, коли до кінця входу стільки днів

_token_cache = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()


class UserAuthError(Exception):
    """Акаунт eBay не підключено, або вхід застарів — треба увійти знову."""


def is_configured():
    """Чи вказано в .env EBAY_RUNAME (без нього eBay не знає, куди повернути після входу)."""
    return bool(EBAY_RUNAME)


def is_connected():
    return bool(get_meta(_REFRESH_KEY))


def connection_info():
    """(дата підключення, дійсний до) як timestamp або None."""
    def ts(key):
        try:
            return float(get_meta(key) or 0) or None
        except ValueError:
            return None
    return ts(_CONNECTED_AT_KEY), ts(_REFRESH_EXPIRES_KEY)


def consent_url():
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": config.EBAY_CLIENT_ID,
        "redirect_uri": EBAY_RUNAME,
        "response_type": "code",
        "scope": USER_SCOPES,
    })


def extract_code(text):
    """Код з адреси сторінки після входу (або сам код). None — коду немає."""
    text = (text or "").strip()
    if text.startswith("v^1.1"):
        return text.split()[0]
    for word in text.split():
        if "code=" in word:
            values = parse_qs(urlparse(word).query).get("code")
            if values and values[0]:
                return values[0]
    return None


def _basic_auth():
    creds = f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


def _token_request(data):
    resp = _request_with_retries(
        "POST", OAUTH_URL,
        headers={"Authorization": _basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
        data=data, timeout=15,
    )
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    return resp.status_code, payload


def exchange_code(code):
    """Обмінює одноразовий код на токени й зберігає refresh-токен. Кидає UserAuthError."""
    status, data = _token_request({
        "grant_type": "authorization_code", "code": code, "redirect_uri": EBAY_RUNAME,
    })
    if status != 200 or not data.get("refresh_token"):
        log.warning("eBay не прийняв код входу: HTTP %s %s", status, data.get("error"))
        raise UserAuthError(data.get("error_description") or data.get("error") or f"HTTP {status}")
    now = time.time()
    set_meta(_REFRESH_KEY, data["refresh_token"])
    set_meta(_REFRESH_EXPIRES_KEY, now + float(data.get("refresh_token_expires_in") or 0))
    set_meta(_CONNECTED_AT_KEY, now)
    set_meta(_LOST_KEY, "")   # новий вхід — попередні попередження вже не актуальні
    set_meta(_LOST_NOTIFIED_KEY, "")
    with _token_lock:
        _token_cache.update(token=data.get("access_token"), expires_at=now + float(data.get("expires_in") or 0))


def disconnect():
    for key in (_REFRESH_KEY, _REFRESH_EXPIRES_KEY, _CONNECTED_AT_KEY):
        set_meta(key, "")
    with _token_lock:
        _token_cache.update(token=None, expires_at=0.0)


def get_user_access_token():
    """Короткий токен доступу від імені власника (оновлюється сам). Кидає UserAuthError."""
    refresh = get_meta(_REFRESH_KEY)
    if not refresh:
        raise UserAuthError("акаунт eBay не підключено")
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        status, data = _token_request({
            "grant_type": "refresh_token", "refresh_token": refresh, "scope": USER_SCOPES,
        })
        if status == 200 and data.get("access_token"):
            _token_cache.update(token=data["access_token"],
                                expires_at=time.time() + float(data.get("expires_in") or 0))
            return _token_cache["token"]
    if data.get("error") == "invalid_grant":
        log.warning("Вхід в акаунт eBay застарів або скасований — треба увійти знову")
        set_meta(_LOST_KEY, int(time.time()))   # власнику прийде одне сповіщення (login_notice)
        disconnect()
        raise UserAuthError("вхід в акаунт eBay застарів — увійди знову")
    raise RuntimeError(f"eBay не видав токен доступу: HTTP {status} {data.get('error')}")


def login_notice(now=None):
    """Текст сповіщення власнику про вхід в акаунт eBay або None. Кожне — лише один раз:
    eBay скасував вхід (перевірка продажів зупинилась) або до кінця входу ≤ 14 / ≤ 3 днів."""
    now = now or time.time()
    lost = get_meta(_LOST_KEY)
    if lost and get_meta(_LOST_NOTIFIED_KEY) != lost:
        set_meta(_LOST_NOTIFIED_KEY, lost)
        return ("🔐 <b>eBay скасував вхід в акаунт</b>\n\n"
                "Бот більше не може перевіряти, чи зниклі оголошення справді продали, і брати характеристики "
                "через Trading API. Пошук знахідок працює далі.\n\n"
                "Увійди знову: «⚙️ Налаштування» → «🔐 Підключити акаунт eBay».")
    if not is_connected():
        return None
    _, expires = connection_info()
    if not expires:
        return None
    days_left = (expires - now) / 86400
    due = [d for d in sorted(LOGIN_WARN_DAYS) if days_left <= d]
    if not due:
        return None
    key = f"ebay_login_warned:{int(expires)}"
    if get_meta(key) and float(get_meta(key)) <= due[0]:
        return None   # про цей поріг (або ближчий) уже нагадували
    set_meta(key, due[0])
    when = time.strftime("%d.%m.%Y", time.localtime(expires))
    return (f"⏳ <b>Вхід в акаунт eBay закінчується {when}</b> (через {max(0, int(days_left))} дн.)\n\n"
            "Після цього бот перестане перевіряти, чи зниклі оголошення справді продали. "
            "Онови вхід заздалегідь: «⚙️ Налаштування» → «🔄 Увійти заново» — це 1 хвилина.")
