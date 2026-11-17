import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

try:
    from curl_cffi import requests as cr
except ImportError:
    cr = None
    import httpx

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

app = FastAPI(
    title="MovieBox 1080p API",
    description="REST API for MovieBox with 1080p Stream & Subtitle Extraction",
    version="2.5.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY_DEFAULT_B64 = "NzZpUmwwN3MweFNOOWpxbUVXQXQ3OUVCSlp1bElRSXNWNjRGWnIyTw=="
SECRET_KEY_ALT_B64 = "WHFuMm5uTzQxL0w5Mm8xaXVYaFNMSFRiWHZZNFo1Wlo2Mm04bVNMQQ=="
WEB_USER_AGENT = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"

# --- FIX 2026-11-17: session/host layer replaced with the MovieBox-Tui flow ---
# Before: the token was bootstrapped with a GET to
# https://apig.inmoviebox.com/wefeed-mobile-bff/tab/ranking-list and harvested
# from the `x-user` response header, and all native calls went to the single
# host api3.aoneroom.com with the old com.community.mbox.in identity. The
# bootstrap host now answers 403 "Service not available in current region"
# from many egress IPs, so no token was ever issued and every signed endpoint
# failed. The flow below mirrors MovieBox-Tui (src/providers/moviebox):
# visitor-login for the token, a 7-host failover pool, the newer
# com.community.oneroom identity, an X-Client-Build stamp, and a spoofed
# x-forwarded-for header (required — without it some egress IPs get region
# 403s even on the pool hosts). Signing itself is unchanged.
HOST_POOL = [
    "https://api6.aoneroom.com",
    "https://api5.aoneroom.com",
    "https://api4.aoneroom.com",
    "https://api4sg.aoneroom.com",
    "https://api3.aoneroom.com",
    "https://api6sg.aoneroom.com",
    "https://api.inmoviebox.com",
]
NATIVE_API_BASE = HOST_POOL[0]
VISITOR_LOGIN_PATH = "/wefeed-mobile-bff/user-api/visitor-login"
RETRY_STATUS_CODES = {401, 403, 406, 407, 429, 500, 502, 503, 504}
STOCK_CERT_SHA256 = "03d4b741632d136bc23e2de4c6adcdb7712f1980b619c183221801730bbd0410"
BUILD_TAG_SALT = "oneroom"

WEB_DOMAINS = [
    "https://123moviesfree.club",
    "https://movie-box.co",
    "https://movieboxonline.net",
    "https://sflix.film",
    "https://h5-api.aoneroom.com",
    "https://netnaija.film",
    "https://movieboxhd.net",
]

def _b64decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)

def get_secret_bytes(use_alt_key: bool = False) -> bytes:
    outer = SECRET_KEY_ALT_B64 if use_alt_key else SECRET_KEY_DEFAULT_B64
    inner = _b64decode(outer).decode("utf-8")
    return _b64decode(inner)

def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def generate_device_id() -> str:
    return secrets.token_hex(16)

def generate_x_client_token(timestamp: int) -> str:
    ts_str = str(timestamp)
    reversed_ts = ts_str[::-1].encode("utf-8")
    return f"{ts_str},{md5_hex(reversed_ts)}"

_ANDROID_VERSIONS = [
    ("9", "PQ3A.190605.03081104"),
    ("10", "QP1A.191005.007.A3"),
    ("11", "RP1A.200720.011"),
    ("12", "S1B.220414.015"),
    ("13", "TQ2A.230405.003"),
]
_REDMI_DEVICES = [
    ("23078RKD5C", "Redmi"),
    ("2201117TY", "Redmi"),
    ("2201117TG", "Redmi"),
    ("22101316G", "Redmi"),
    ("21121210G", "Redmi"),
    ("M2012K11AG", "Redmi"),
    ("M2007J20CG", "Redmi"),
]
_VERSION_CODES = [50020117, 50020118, 50020119, 50020120, 50020121]
_TIMEZONES = ["Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "America/New_York", "Europe/London"]
_SPOOFED_IP_PREFIXES = ["103.241", "49.36", "117.195", "106.198", "122.162", "157.32", "182.70", "103.58", "27.60", "59.90"]

def generate_spoofed_ip() -> str:
    prefix = secrets.choice(_SPOOFED_IP_PREFIXES)
    return f"{prefix}.{secrets.randbelow(253) + 1}.{secrets.randbelow(253) + 1}"

def generate_x_client_build(timestamp_ms: int) -> str:
    # Mirrors MovieBox-Tui crypto.rs: stamp = sec + millis + "000" + rand,
    # tag = "<stamp>.<md5(cert_sha256 + 'oneroom' + stamp)>".
    rand_part = secrets.randbelow(1000)
    stamp = f"{timestamp_ms // 1000}{timestamp_ms % 1000:03d}000{rand_part}"
    digest = md5_hex(f"{STOCK_CERT_SHA256}{BUILD_TAG_SALT}{stamp}".encode("utf-8"))
    return f"{stamp}.{digest}"

def client_info_with_build_tag(client_info: str, tag: str) -> str:
    try:
        value = json.loads(client_info)
        if isinstance(value, dict):
            value["X-Client-Build"] = tag
            return json.dumps(value, separators=(",", ":"))
    except Exception:
        pass
    return client_info

def generate_client_info(device_id: str) -> str:
    # Newer app identity (com.community.oneroom), mirroring MovieBox-Tui.
    android = secrets.choice(_ANDROID_VERSIONS)
    model, brand = secrets.choice(_REDMI_DEVICES)
    gaid = "-".join([secrets.token_hex(4), secrets.token_hex(2), secrets.token_hex(2), secrets.token_hex(2), secrets.token_hex(6)])
    return json.dumps(
        {
            "package_name": "com.community.oneroom",
            "version_name": "4.0.01.0813.03",
            "version_code": secrets.choice(_VERSION_CODES),
            "os": "android",
            "os_version": android[0],
            "install_ch": "ps",
            "device_id": device_id,
            "install_store": "ps",
            "gaid": gaid,
            "brand": brand,
            "model": model,
            "system_language": "en",
            "net": secrets.choice(["NETWORK_WIFI", "NETWORK_MOBILE"]),
            "region": "US",
            "timezone": secrets.choice(_TIMEZONES),
            "sp_code": "40401",
            "X-Play-Mode": "2",
        },
        separators=(",", ":"),
    )

def generate_user_agent(client_info: str) -> str:
    try:
        info = json.loads(client_info)
        version_code = info.get("version_code", 50020119)
        os_version = info.get("os_version", "13")
        model = info.get("model", "2201117TY")
    except Exception:
        version_code, os_version, model = 50020119, "13", "2201117TY"
    build = dict(_ANDROID_VERSIONS).get(os_version, "TQ2A.230405.003")
    return (
        f"com.community.oneroom/{version_code} (Linux; U; Android {os_version}; "
        f"en_US; {model}; Build/{build}; Cronet/135.0.7012.3)"
    )

def generate_client_profile(device_id: str) -> Dict[str, str]:
    """One stable per-engine identity: UA + client-info + spoofed IP."""
    client_info = generate_client_info(device_id)
    return {
        "user_agent": generate_user_agent(client_info),
        "client_info": client_info,
        "spoofed_ip": generate_spoofed_ip(),
    }

def build_canonical_string(method: str, accept: str | None, content_type: str | None, url: str, body: str | None, timestamp: int) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        pairs = []
        for segment in parsed.query.split("&"):
            if not segment:
                continue
            pieces = segment.split("=", 1)
            pairs.append((pieces[0], pieces[1] if len(pieces) > 1 else ""))
        pairs.sort(key=lambda kv: kv[0])
        canonical_query = "&".join(f"{k}={v}" for k, v in pairs)
        canonical_url = f"{path}?{canonical_query}"
    else:
        canonical_url = path

    body_bytes = body.encode("utf-8") if body is not None else None
    if body_bytes:
        body_length = str(len(body_bytes))
        body_hash = md5_hex(body_bytes[:102400])
    else:
        body_length = ""
        body_hash = ""

    return "\n".join(
        [
            method.upper(),
            accept or "",
            content_type or "",
            body_length,
            str(timestamp),
            body_hash,
            canonical_url,
        ]
    )

def generate_x_tr_signature(method: str, accept: str | None, content_type: str | None, url: str, body: str | None, timestamp: int, use_alt_key: bool = False) -> str:
    canonical = build_canonical_string(method, accept, content_type, url, body, timestamp)
    key = get_secret_bytes(use_alt_key)
    digest = hmac.new(key, canonical.encode("utf-8"), hashlib.md5).digest()
    sig_b64 = base64.b64encode(digest).decode("ascii")
    return f"{timestamp}|2|{sig_b64}"

def build_auth_headers(method: str, url: str, body: str | None = None, token: str | None = None, device_id: str | None = None, use_alt_key: bool = False, content_type: str = "application/json", accept: str = "application/json", profile: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    ts = int(time.time() * 1000)
    dev_id = device_id or generate_device_id()
    if profile is None:
        profile = generate_client_profile(dev_id)
    build_tag = generate_x_client_build(ts)
    headers = {
        "user-agent": profile["user_agent"],
        "accept": accept,
        "content-type": content_type,
        "connection": "keep-alive",
        "x-client-token": generate_x_client_token(ts),
        "x-tr-signature": generate_x_tr_signature(method, accept, content_type, url, body, ts, use_alt_key),
        "x-client-build": build_tag,
        "x-client-info": client_info_with_build_tag(profile["client_info"], build_tag),
        "x-client-status": "0",
        "x-forwarded-for": profile["spoofed_ip"],
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers

def extract_policy_resource(sign_cookie: str | None) -> str | None:
    if not sign_cookie:
        return None
    m_edge = re.search(r"Edge-Cache-Cookie=urlprefix=([^:;\s]+)", sign_cookie)
    if m_edge:
        try:
            decoded = _b64decode(m_edge.group(1)).decode("utf-8").rstrip("/")
            return decoded + "/index.mpd"
        except Exception:
            pass
    m_cf = re.search(r"CloudFront-Policy=([^;]+)", sign_cookie)
    if m_cf:
        raw = m_cf.group(1)
        for t in ("cf", "std"):
            try:
                b = raw.replace("-", "+").replace("_", "=") if t == "cf" else raw
                b += "=" * (-len(b) % 4)
                data = json.loads(base64.b64decode(b).decode("utf-8"))
                for s in data.get("Statement", []):
                    res = s.get("Resource")
                    if res:
                        return res.rstrip("*").rstrip("/") + "/index.mpd"
            except Exception:
                pass
    return None

class _HostPoolExhausted(Exception):
    def __init__(self, last_status: Optional[int] = None):
        super().__init__(f"all MovieBox hosts exhausted (last HTTP status: {last_status})")
        self.last_status = last_status

class MovieBoxEngine:
    _instance = None

    def __init__(self):
        self.device_id = generate_device_id()
        self.bearer_token: Optional[str] = None
        self.profile = generate_client_profile(self.device_id)
        self.active_host_idx = 0
        if cr:
            self.session = cr.Session(impersonate="chrome120")
        else:
            self.session = httpx.Client(timeout=25, follow_redirects=True)

    @classmethod
    def get_instance(cls) -> "MovieBoxEngine":
        if cls._instance is None:
            cls._instance = MovieBoxEngine()
        return cls._instance

    # --- session handling (MovieBox-Tui flow) ---

    def _token_valid(self) -> bool:
        if not self.bearer_token:
            return False
        parts = self.bearer_token.split(".")
        if len(parts) == 3:
            try:
                payload = json.loads(_b64decode(parts[1]).decode("utf-8"))
                exp = payload.get("exp")
                if isinstance(exp, (int, float)):
                    return time.time() + 60 < exp
            except Exception:
                pass
        return True

    def _absorb_x_user(self, headers) -> None:
        # Upstream sometimes rotates the session via an `x-user` response
        # header (JSON with a fresh token); adopt it like MovieBox-Tui does.
        try:
            x_user = headers.get("x-user")
            if not x_user:
                return
            token = json.loads(x_user).get("token")
            if token:
                self.bearer_token = token
        except Exception:
            pass

    def _visitor_login(self, host: str) -> Optional[str]:
        url = f"{host}{VISITOR_LOGIN_PATH}"
        body_str = "{}"
        headers = build_auth_headers("POST", url, body=body_str, device_id=self.device_id, profile=self.profile)
        resp = self.session.post(url, headers=headers, data=body_str.encode("utf-8"), timeout=20)
        self._absorb_x_user(resp.headers)
        if resp.status_code != 200:
            raise RuntimeError(f"visitor-login HTTP {resp.status_code} on {host}: {resp.text[:200]}")
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        token = None
        if isinstance(data, dict):
            token = data.get("token")
        if not token and isinstance(payload, dict):
            token = payload.get("token")
        return token or self.bearer_token

    def bootstrap_token(self, force: bool = False) -> str:
        # FIX 2026-11-17: previously this GET-harvested a token from the
        # `x-user` header of apig.inmoviebox.com's ranking-list endpoint,
        # which now returns 403 "Service not available in current region".
        # Now: POST /wefeed-mobile-bff/user-api/visitor-login (guest JWT in
        # the response body), walking the host pool until one answers.
        if self.bearer_token and not force and self._token_valid():
            return self.bearer_token
        if force:
            self.bearer_token = None
        last_error: Optional[Exception] = None
        for i in range(len(HOST_POOL)):
            idx = (self.active_host_idx + i) % len(HOST_POOL)
            try:
                token = self._visitor_login(HOST_POOL[idx])
            except Exception as e:
                last_error = e
                continue
            if token:
                self.bearer_token = token
                self.active_host_idx = idx
                return token
        raise HTTPException(status_code=502, detail=f"Failed to obtain MovieBox visitor token: {last_error}")

    def _send(self, method: str, url: str, headers: Dict[str, str], body_str: Optional[str]):
        data_bytes = body_str.encode("utf-8") if body_str is not None else None
        if method.upper() == "POST":
            return self.session.post(url, headers=headers, data=data_bytes, timeout=25)
        return self.session.get(url, headers=headers, timeout=25)

    def _request_over_pool(self, method: str, path: str, body_str: Optional[str], token: Optional[str]) -> Dict[str, Any]:
        last_status: Optional[int] = None
        start = self.active_host_idx
        for i in range(len(HOST_POOL)):
            idx = (start + i) % len(HOST_POOL)
            host = HOST_POOL[idx]
            url = f"{host}{path}"
            headers = build_auth_headers(method, url, body=body_str, token=token, device_id=self.device_id, profile=self.profile)
            try:
                resp = self._send(method, url, headers, body_str)
            except Exception:
                self.active_host_idx = (idx + 1) % len(HOST_POOL)
                continue
            self._absorb_x_user(resp.headers)
            status = resp.status_code
            if status in RETRY_STATUS_CODES:
                last_status = status
                self.active_host_idx = (idx + 1) % len(HOST_POOL)
                if status == 429:
                    try:
                        retry_after = float(resp.headers.get("retry-after") or 0.4)
                    except (TypeError, ValueError):
                        retry_after = 0.4
                    time.sleep(min(retry_after, 3.0))
                continue
            if status != 200:
                raise HTTPException(status_code=502, detail=f"API returned HTTP {status}")
            self.active_host_idx = idx
            return resp.json()
        raise _HostPoolExhausted(last_status)

    def api_request(self, method: str, path_or_url: str, body_dict: Optional[Dict[str, Any]] = None, use_token: bool = True, retry_auth: bool = True) -> Dict[str, Any]:
        body_str = json.dumps(body_dict, separators=(",", ":")) if body_dict is not None else None
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            # Absolute URL (legacy callers): single-host request, same
            # re-login-on-401 behaviour as before.
            if use_token and not self._token_valid():
                self.bootstrap_token()
            headers = build_auth_headers(method, path_or_url, body=body_str, token=self.bearer_token if use_token else None, device_id=self.device_id, profile=self.profile)
            resp = self._send(method, path_or_url, headers, body_str)
            self._absorb_x_user(resp.headers)
            if resp.status_code in (401, 441) and retry_auth:
                self.bootstrap_token(force=True)
                return self.api_request(method, path_or_url, body_dict, use_token, retry_auth=False)
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"API returned HTTP {resp.status_code}")
            return resp.json()
        if use_token:
            self.bootstrap_token()
        token = self.bearer_token if use_token else None
        try:
            return self._request_over_pool(method, path_or_url, body_str, token)
        except _HostPoolExhausted as e:
            # Mirror MovieBox-Tui: on auth-flavoured failure, drop the
            # session, log in again, and walk the pool once more.
            if retry_auth and use_token:
                self.bootstrap_token(force=True)
                try:
                    return self._request_over_pool(method, path_or_url, body_str, self.bearer_token)
                except _HostPoolExhausted:
                    pass
            raise HTTPException(status_code=502, detail=f"API unreachable on all hosts (last HTTP {e.last_status})")

    def search(self, query: str, page: int = 1, per_page: int = 20) -> List[Dict[str, Any]]:
        payload = {"keyword": query, "page": page, "perPage": per_page}
        url = "/wefeed-mobile-bff/subject-api/search"
        data = self.api_request("POST", url, body_dict=payload).get("data", {})
        raw_items = data.get("items", []) or data.get("list", [])
        results = []
        for item in raw_items:
            sub = item.get("subject", item)
            results.append({
                "subject_id": str(sub.get("subjectId")),
                "title": sub.get("title"),
                "type": "Movie" if sub.get("subjectType") == 1 else "Series",
                "subject_type": sub.get("subjectType"),
                "release_date": sub.get("releaseDate"),
                "imdb_rating": sub.get("imdbRatingValue"),
                "genre": sub.get("genre"),
                "cover": sub.get("cover", {}).get("url") if isinstance(sub.get("cover"), dict) else None,
                "detail_path": sub.get("detailPath"),
                "language": sub.get("language")
            })
        return results

    def get_details(self, subject_id: str) -> Dict[str, Any]:
        url = f"/wefeed-mobile-bff/subject-api/get?subjectId={subject_id}"
        data = self.api_request("GET", url).get("data", {})
        detail_url_str = data.get("detailUrl", "")
        detail_path = detail_url_str.split("/")[-1] if "/" in detail_url_str else detail_url_str
        return {
            "subject_id": str(data.get("subjectId")),
            "title": data.get("title"),
            "subject_type": data.get("subjectType"),
            "type": "Movie" if data.get("subjectType") == 1 else "Series",
            "description": data.get("description"),
            "release_date": data.get("releaseDate"),
            "duration": data.get("duration"),
            "imdb_rating": data.get("imdbRatingValue"),
            "genre": data.get("genre"),
            "country": data.get("countryName"),
            "language": data.get("language"),
            "cover": data.get("cover", {}).get("url") if isinstance(data.get("cover"), dict) else None,
            "detail_path": detail_path,
            "dubs": [
                {
                    "subject_id": str(d.get("subjectId")),
                    "lang_name": d.get("lanName"),
                    "language": d.get("language"),
                }
                for d in data.get("dubs", [])
                if isinstance(d, dict)
            ]
        }

    def get_episodes(self, subject_id: str) -> List[Dict[str, Any]]:
        url = f"/wefeed-mobile-bff/subject-api/season-info?subjectId={subject_id}"
        res = self.api_request("GET", url)
        seasons = res.get("data", {}).get("seasons", [])
        season_list = []
        for s in seasons:
            season_list.append({
                "season": s.get("se", 1),
                "episode_count": s.get("maxEp", 1),
                "resolutions": s.get("resolutions", [])
            })
        return season_list

    def get_streams(self, subject_id: str, season: int = 0, episode: int = 0, detail_path: Optional[str] = None) -> List[Dict[str, Any]]:
        if not detail_path:
            try:
                details = self.get_details(subject_id)
                detail_path = details.get("detail_path", "")
            except Exception:
                detail_path = ""
        token = self.bootstrap_token()
        streams: List[Dict[str, Any]] = []
        seen_urls = set()

        for domain in WEB_DOMAINS:
            play_url = (
                f"{domain}/wefeed-h5api-bff/subject/play?"
                f"subjectId={subject_id}&se={season}&ep={episode}&detailPath={detail_path}&"
                f"streamSignType=1&supportCodecs%5Bhevc%5D=1&supportCodecs%5Bh264%5D=1"
            )
            referer = f"{domain}/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe=&detailEp=&lang=en"
            headers = {
                "User-Agent": WEB_USER_AGENT,
                "Referer": referer,
                "Accept": "application/json",
                "x-client-info": '{"timezone":"Asia/Calcutta"}',
                "x-request-lang": "en",
                "x-vip-restrict": "0",
                "x-no-high-risk-restrict": "0",
                "x-source": "moviebox",
                "Authorization": f"Bearer {token}",
            }
            try:
                resp = self.session.get(play_url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    continue
                data = resp.json().get("data", {})
                raw_streams = data.get("streams", [])
                dash_items = data.get("dash", [])

                if raw_streams or dash_items:
                    for st in raw_streams:
                        url = st.get("url")
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        res_str = str(st.get("resolutions", "unknown"))
                        size_bytes = int(st.get("size") or 0)
                        size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                        quality_val = 0
                        for q in (2160, 1440, 1080, 720, 480, 360, 240):
                            if str(q) in res_str:
                                quality_val = q
                                break
                        streams.append({
                            "source": f"Web CDN ({domain})",
                            "format": st.get("format", "MP4"),
                            "quality": f"{quality_val}p" if quality_val else res_str,
                            "quality_value": quality_val,
                            "resolution_tag": res_str,
                            "url": url,
                            "size_mb": round(size_mb, 1),
                            "codec": st.get("codecName"),
                            "duration_sec": st.get("duration"),
                            "headers": {
                                "User-Agent": "Mozilla/5.0",
                                "Referer": f"{domain}/",
                                "Origin": domain,
                            }
                        })

                    for d in dash_items:
                        url = d.get("url")
                        cookie = d.get("signCookie")
                        extracted_mpd = extract_policy_resource(cookie) if cookie else None
                        final_url = extracted_mpd or url
                        if final_url and final_url not in seen_urls:
                            seen_urls.add(final_url)
                            res_tag = str(d.get("resolutions", "adaptive"))
                            streams.append({
                                "source": f"Web DASH ({domain})",
                                "format": "DASH",
                                "quality": "1080p (Adaptive)" if "1080" in res_tag else "Adaptive",
                                "quality_value": 1080 if "1080" in res_tag else 999,
                                "resolution_tag": res_tag,
                                "url": final_url,
                                "size_mb": 0,
                                "codec": "hevc/h264",
                                "duration_sec": None,
                                "cookie": cookie,
                                "headers": {
                                    "User-Agent": "Mozilla/5.0",
                                    "Referer": f"{domain}/",
                                    "Origin": domain,
                                }
                            })
                    break
            except Exception:
                continue

        try:
            native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
            native_data = self.api_request("GET", native_url).get("data", {})
            for st in native_data.get("streams", []):
                raw_url = st.get("url")
                if not raw_url or "b164fbfb4347792950bdfbfb563d39d9" in raw_url:
                    continue
                cookie = st.get("signCookie")
                policy_url = extract_policy_resource(cookie) if cookie else None
                final_url = policy_url or raw_url
                if final_url in seen_urls:
                    continue
                seen_urls.add(final_url)
                res_str = str(st.get("resolutions", ""))
                quality_val = 0
                for q in (2160, 1440, 1080, 720, 480, 360, 240):
                    if str(q) in res_str:
                        quality_val = q
                        break
                size_bytes = int(st.get("size") or 0)
                size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                streams.append({
                    "source": "Mobile Native CDN",
                    "format": "DASH" if (policy_url or ".mpd" in final_url) else st.get("format", "MP4"),
                    "quality": f"{quality_val}p" if quality_val else (res_str or "Default"),
                    "quality_value": quality_val,
                    "resolution_tag": res_str,
                    "url": final_url,
                    "size_mb": round(size_mb, 1),
                    "codec": st.get("codecName"),
                    "duration_sec": st.get("duration"),
                    "headers": {
                        "User-Agent": self.profile["user_agent"],
                        "Referer": "https://api3.aoneroom.com/",
                    }
                })
        except Exception:
            pass

        streams.sort(key=lambda s: s["quality_value"], reverse=True)
        return streams

    def get_subtitles(self, subject_id: str, stream_id: Optional[str] = None, season: int = 0, episode: int = 0) -> List[Dict[str, Any]]:
        subs: List[Dict[str, Any]] = []
        seen_urls = set()

        if not stream_id:
            try:
                native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
                raw_streams = self.api_request("GET", native_url).get("data", {}).get("streams", [])
                if raw_streams and raw_streams[0].get("id"):
                    stream_id = str(raw_streams[0]["id"])
            except Exception:
                pass

        def _add_sub(item: Dict[str, Any]):
            u = item.get("url")
            if u and u not in seen_urls:
                seen_urls.add(u)
                subs.append({
                    "id": str(item.get("id", "")),
                    "language": item.get("lanName") or item.get("language") or "English",
                    "code": item.get("lan", ""),
                    "url": u,
                    "size": item.get("size")
                })

        if stream_id:
            try:
                url = f"/wefeed-mobile-bff/subject-api/get-stream-captions?subjectId={subject_id}&streamId={stream_id}"
                data = self.api_request("GET", url).get("data", {})
                captions = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
                for s in captions:
                    if isinstance(s, dict):
                        _add_sub(s)
            except Exception:
                pass

        try:
            res_id = stream_id or "0"
            url = f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={res_id}&episode={episode}"
            data = self.api_request("GET", url).get("data", {})
            ext = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
            for s in ext:
                if isinstance(s, dict):
                    _add_sub(s)
        except Exception:
            pass

        return subs

engine = MovieBoxEngine.get_instance()

@app.get("/legacy-dashboard", response_class=HTMLResponse)
async def dashboard():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MovieBox API</title>
    <style>
        :root {
            --bg: #09090b;
            --surface: #121215;
            --surface-hover: #18181b;
            --border: #27272a;
            --text: #fafafa;
            --text-dim: #71717a;
            --accent: #3b82f6;
            --accent-soft: rgba(59, 130, 246, 0.12);
            --green: #10b981;
            --green-soft: rgba(16, 185, 129, 0.12);
            --radius: 8px;
            --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: var(--sans);
            font-size: 14px;
            line-height: 1.5;
            padding: 32px 20px;
        }
        .wrap {
            max-width: 980px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 28px;
        }
        header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }
        .brand {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .brand h1 {
            font-size: 18px;
            font-weight: 600;
            letter-spacing: -0.3px;
        }
        .pill {
            font-size: 11px;
            font-family: var(--mono);
            padding: 2px 7px;
            border-radius: 4px;
            background: var(--border);
            color: var(--text-dim);
        }
        .pill.active {
            background: var(--green-soft);
            color: var(--green);
        }
        .section-title {
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: var(--text-dim);
            margin-bottom: 12px;
            font-weight: 600;
        }
        .box {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 18px;
        }
        .search-bar {
            display: flex;
            gap: 8px;
            position: relative;
        }
        .input-text {
            flex: 1;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 10px 14px;
            border-radius: var(--radius);
            font-size: 14px;
            font-family: inherit;
            outline: none;
        }
        .input-text:focus {
            border-color: var(--accent);
        }
        .input-num {
            width: 80px;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 10px 12px;
            border-radius: var(--radius);
            font-size: 14px;
            outline: none;
        }
        .btn-action {
            background: var(--text);
            color: var(--bg);
            border: none;
            padding: 10px 18px;
            border-radius: var(--radius);
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            white-space: nowrap;
        }
        .btn-action:hover {
            opacity: 0.9;
        }
        .btn-ghost {
            background: transparent;
            color: var(--text);
            border: 1px solid var(--border);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
        }
        .btn-ghost:hover {
            background: var(--surface-hover);
        }
        .dropdown {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            margin-top: 4px;
            max-height: 240px;
            overflow-y: auto;
            z-index: 50;
            display: none;
        }
        .dropdown-item {
            padding: 10px 14px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #1c1c20;
        }
        .dropdown-item:hover {
            background: var(--surface-hover);
        }
        .media-row {
            display: flex;
            gap: 14px;
            align-items: center;
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--border);
        }
        .media-poster {
            width: 44px;
            height: 62px;
            border-radius: 4px;
            object-fit: cover;
            background: var(--border);
        }
        .stream-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 14px;
            border-bottom: 1px solid var(--border);
            font-size: 13px;
        }
        .stream-row:last-child {
            border-bottom: none;
        }
        .stream-spec {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .stream-badge {
            font-family: var(--mono);
            font-weight: 600;
            font-size: 11px;
            padding: 2px 6px;
            border-radius: 4px;
            background: var(--accent-soft);
            color: var(--accent);
        }
        .stream-badge.hd {
            background: var(--green-soft);
            color: var(--green);
        }
        .video-box {
            display: none;
            flex-direction: column;
            gap: 10px;
            margin-top: 16px;
        }
        video {
            width: 100%;
            max-height: 480px;
            background: #000;
            border-radius: var(--radius);
            border: 1px solid var(--border);
        }
        .endpoint-table {
            width: 100%;
            border-collapse: collapse;
        }
        .endpoint-table th, .endpoint-table td {
            padding: 12px 14px;
            text-align: left;
            border-bottom: 1px solid var(--border);
            font-size: 13px;
        }
        .endpoint-table tr:last-child td {
            border-bottom: none;
        }
        .endpoint-method {
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 700;
            color: var(--accent);
        }
        .endpoint-path {
            font-family: var(--mono);
            color: var(--text);
        }
    </style>
</head>
<body>
    <div class="wrap">
        <header>
            <div class="brand">
                <h1>MovieBox API</h1>
                <span class="pill">v2.5.0</span>
                <span class="pill active">1080p Online</span>
            </div>
            <div>
                <a class="btn-ghost" href="/docs" target="_blank">Swagger Docs</a>
            </div>
        </header>

        <div>
            <div class="section-title">Stream & Subtitle Resolver</div>
            <div class="box">
                <div class="search-bar">
                    <input type="text" id="searchInput" class="input-text" placeholder="Search title or paste Subject ID (e.g. Avatar, 8906247916759695608)..." autocomplete="off">
                    <input type="number" id="seasonInput" class="input-num" placeholder="S" title="Season" value="0">
                    <input type="number" id="episodeInput" class="input-num" placeholder="E" title="Episode" value="0">
                    <button class="btn-action" onclick="resolveStreams()">Resolve</button>
                    <div id="dropdownList" class="dropdown"></div>
                </div>

                <div id="mediaHeader" style="display: none;" class="media-row">
                    <img id="mediaPoster" class="media-poster">
                    <div>
                        <div id="mediaTitle" style="font-weight: 600; font-size: 15px;"></div>
                        <div id="mediaMeta" style="font-size: 12px; color: var(--text-dim); margin-top: 2px;"></div>
                    </div>
                </div>

                <div class="video-box" id="playerContainer">
                    <video id="player" controls playsinline></video>
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span id="playerStatus" style="font-size: 12px; color: var(--text-dim); font-family: var(--mono);"></span>
                        <select id="subPicker" onchange="switchSubtitle(this.value)" class="input-text" style="max-width: 220px; padding: 6px 10px; font-size: 12px;">
                            <option value="">Subtitles Off</option>
                        </select>
                    </div>
                </div>

                <div id="streamList" style="margin-top: 14px;"></div>
            </div>
        </div>

        <div>
            <div class="section-title">Endpoints (Click Test to Open Raw JSON)</div>
            <div class="box" style="padding: 0;">
                <table class="endpoint-table">
                    <thead>
                        <tr style="color: var(--text-dim); font-size: 11px; text-transform: uppercase;">
                            <th style="width: 70px;">Method</th>
                            <th>Endpoint Path</th>
                            <th>Description</th>
                            <th style="width: 110px; text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path"><span class="pill" style="margin-right: 6px;">Movie</span>/api/stream/8906247916759695608</td>
                            <td style="color: var(--text-dim);">Movie Streams 1080p, 720p, 480p (Avatar)</td>
                            <td style="text-align: right;"><a href="/api/stream/8906247916759695608" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path"><span class="pill" style="margin-right: 6px;">Movie</span>/api/stream/8906247916759695608/captions</td>
                            <td style="color: var(--text-dim);">Movie Subtitle Tracks .srt (Avatar)</td>
                            <td style="text-align: right;"><a href="/api/stream/8906247916759695608/captions" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path"><span class="pill active" style="margin-right: 6px;">Series</span>/episodes/6207982430134357800</td>
                            <td style="color: var(--text-dim);">TV Seasons & Episode Counts (Breaking Bad)</td>
                            <td style="text-align: right;"><a href="/episodes/6207982430134357800" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path"><span class="pill active" style="margin-right: 6px;">Series</span>/api/stream/6207982430134357800?se=1&ep=1</td>
                            <td style="color: var(--text-dim);">TV Episode Stream: Season 1, Episode 1</td>
                            <td style="text-align: right;"><a href="/api/stream/6207982430134357800?se=1&ep=1" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path"><span class="pill active" style="margin-right: 6px;">Series</span>/api/stream/6207982430134357800?se=1&ep=2</td>
                            <td style="color: var(--text-dim);">TV Episode Stream: Season 1, Episode 2</td>
                            <td style="text-align: right;"><a href="/api/stream/6207982430134357800?se=1&ep=2" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path"><span class="pill active" style="margin-right: 6px;">Series</span>/api/stream/6207982430134357800/captions?se=1&ep=1</td>
                            <td style="color: var(--text-dim);">TV Episode Subtitles: Season 1, Episode 1</td>
                            <td style="text-align: right;"><a href="/api/stream/6207982430134357800/captions?se=1&ep=1" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path">/search?q=Avatar</td>
                            <td style="color: var(--text-dim);">Search catalog with metadata & IDs</td>
                            <td style="text-align: right;"><a href="/search?q=Avatar" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path">/search/suggest?q=Avatar</td>
                            <td style="color: var(--text-dim);">Real-time search autocomplete suggestions</td>
                            <td style="text-align: right;"><a href="/search/suggest?q=Avatar" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path">/detail/8906247916759695608</td>
                            <td style="color: var(--text-dim);">Deep metadata, audio dubs, and plot summary</td>
                            <td style="text-align: right;"><a href="/detail/8906247916759695608" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path">/movies?page=1</td>
                            <td style="color: var(--text-dim);">Paginated movie feed</td>
                            <td style="text-align: right;"><a href="/movies?page=1" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path">/tv-series?page=1</td>
                            <td style="color: var(--text-dim);">Paginated TV series feed</td>
                            <td style="text-align: right;"><a href="/tv-series?page=1" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                        <tr>
                            <td><span class="endpoint-method">GET</span></td>
                            <td class="endpoint-path">/home</td>
                            <td style="color: var(--text-dim);">Banners, hot sections, and operating feeds</td>
                            <td style="text-align: right;"><a href="/home" target="_blank" class="btn-ghost">Test</a></td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let searchTimer = null;
        let activeSubjectId = "";
        const searchInput = document.getElementById("searchInput");
        const dropdownList = document.getElementById("dropdownList");

        searchInput.addEventListener("input", (e) => {
            clearTimeout(searchTimer);
            const val = e.target.value.trim();
            if (val.length < 2) {
                dropdownList.style.display = "none";
                return;
            }
            if (/^\\d{15,}$/.test(val)) {
                activeSubjectId = val;
                dropdownList.style.display = "none";
                return;
            }
            searchTimer = setTimeout(async () => {
                try {
                    const res = await fetch(`/search/suggest?q=${encodeURIComponent(val)}`);
                    const data = await res.json();
                    const list = data.suggestions || [];
                    if (!list.length) {
                        dropdownList.style.display = "none";
                        return;
                    }
                    dropdownList.innerHTML = list.map(item => `
                        <div class="dropdown-item" onclick="selectItem('${item.subject_id}', '${item.title.replace(/'/g, "\\\\'")}')">
                            <span style="font-weight: 500;">${item.title}</span>
                            <span style="font-family: var(--mono); font-size: 11px; color: var(--text-dim);">${item.subject_id}</span>
                        </div>
                    `).join("");
                    dropdownList.style.display = "block";
                } catch (err) {}
            }, 200);
        });

        function selectItem(id, title) {
            activeSubjectId = id;
            searchInput.value = title;
            dropdownList.style.display = "none";
            resolveStreams();
        }

        async function resolveStreams() {
            const val = searchInput.value.trim();
            const subjectId = /^\\d{15,}$/.test(val) ? val : (activeSubjectId || val);
            const se = parseInt(document.getElementById("seasonInput").value) || 0;
            const ep = parseInt(document.getElementById("episodeInput").value) || 0;

            if (!subjectId) {
                alert("Please enter a title or Subject ID");
                return;
            }

            const streamList = document.getElementById("streamList");
            streamList.innerHTML = '<div style="padding: 12px; color: var(--text-dim); font-size: 13px;">Resolving streams...</div>';

            try {
                const [streamRes, capRes, detailRes] = await Promise.all([
                    fetch(`/api/stream/${subjectId}?se=${se}&ep=${ep}`).then(r => r.json()),
                    fetch(`/api/stream/${subjectId}/captions?se=${se}&ep=${ep}`).then(r => r.json()),
                    fetch(`/detail/${subjectId}`).then(r => r.json()).catch(() => null)
                ]);

                if (detailRes && detailRes.title) {
                    document.getElementById("mediaHeader").style.display = "flex";
                    document.getElementById("mediaTitle").textContent = `${detailRes.title} (${detailRes.type})`;
                    document.getElementById("mediaMeta").textContent = `${detailRes.release_date || ""} • IMDb: ${detailRes.imdb_rating || "N/A"} • ${detailRes.genre || ""}`;
                    if (detailRes.cover) {
                        const p = document.getElementById("mediaPoster");
                        p.src = detailRes.cover;
                        p.style.display = "block";
                    }
                }

                const streams = streamRes.streams || [];
                if (!streams.length) {
                    streamList.innerHTML = '<div style="padding: 12px; color: var(--text-dim); font-size: 13px;">No streams found for this request.</div>';
                    return;
                }

                streamList.innerHTML = streams.map((s, idx) => `
                    <div class="stream-row">
                        <div class="stream-spec">
                            <span class="stream-badge ${s.quality.includes('1080') ? 'hd' : ''}">${s.quality}</span>
                            <span style="font-weight: 500;">${s.format}</span>
                            <span style="color: var(--text-dim); font-size: 12px;">${s.size_mb ? s.size_mb + ' MB' : 'DASH Master'}</span>
                            <span style="color: var(--text-dim); font-size: 12px;">${s.codec || ''}</span>
                        </div>
                        <div style="display: flex; gap: 6px;">
                            <button class="btn-ghost" onclick="play('${encodeURIComponent(JSON.stringify(s))}')">Play</button>
                            <a class="btn-ghost" href="${s.url}" target="_blank">Open Raw</a>
                            <button class="btn-ghost" onclick="copyUrl('${s.url}')">Copy</button>
                        </div>
                    </div>
                `).join("");

                const subs = capRes.captions || [];
                const subPicker = document.getElementById("subPicker");
                subPicker.innerHTML = '<option value="">Subtitles Off</option>' + subs.map(sub => `
                    <option value="${sub.url}">${sub.language}</option>
                `).join("");

            } catch (err) {
                streamList.innerHTML = `<div style="padding: 12px; color: #ef4444; font-size: 13px;">Failed: ${err.message}</div>`;
            }
        }

        function play(encoded) {
            const stream = JSON.parse(decodeURIComponent(encoded));
            const player = document.getElementById("player");
            const container = document.getElementById("playerContainer");
            container.style.display = "flex";
            document.getElementById("playerStatus").textContent = `Playing: ${stream.quality} ${stream.format}`;
            player.src = stream.url;
            player.play().catch(() => {});
        }

        function copyUrl(url) {
            navigator.clipboard.writeText(url).then(() => alert("Stream URL copied to clipboard!"));
        }

        function switchSubtitle(url) {
            const player = document.getElementById("player");
            player.querySelectorAll("track").forEach(t => t.remove());
            if (!url) return;
            const t = document.createElement("track");
            t.kind = "captions";
            t.label = "Selected";
            t.srclang = "en";
            t.src = url;
            t.default = true;
            player.appendChild(t);
        }
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

@app.get("/search")
def search(q: str = Query(..., min_length=1), page: int = 1, per_page: int = 20):
    return {"query": q, "page": page, "results": engine.search(q, page=page, per_page=per_page)}

@app.get("/search/suggest")
def search_suggest(q: str = Query(..., min_length=1)):
    url = f"{API_BASE}/subject/search-suggest"
    headers = {"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}
    try:
        resp = engine.session.post(url, headers=headers, json={"keyword": q, "perPage": 10}, timeout=15)
        raw = resp.json().get("data", {}).get("items", [])
    except Exception:
        raw = []
    suggestions = []
    for item in raw:
        sub = item.get("subject") or {}
        suggestions.append({
            "title": sub.get("title") or item.get("word") or item.get("title"),
            "slug": sub.get("detailPath") or item.get("detailPath"),
            "subject_id": str(sub.get("subjectId") or item.get("subjectId"))
        })
    return {"suggestions": suggestions}

@app.get("/detail/{subject_id}")
def get_detail(subject_id: str):
    return engine.get_details(subject_id)

@app.get("/episodes/{subject_id}")
def get_episodes(subject_id: str):
    return {"subject_id": subject_id, "seasons": engine.get_episodes(subject_id)}

@app.get("/api/stream/{subject_id}")
def get_streams(subject_id: str, se: int = 0, ep: int = 0, detail_path: Optional[str] = None):
    streams = engine.get_streams(subject_id, season=se, episode=ep, detail_path=detail_path)
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(streams),
        "streams": streams
    }

@app.get("/api/stream/{subject_id}/captions")
def get_captions(subject_id: str, se: int = 0, ep: int = 0, stream_id: Optional[str] = None):
    subs = engine.get_subtitles(subject_id, stream_id=stream_id, season=se, episode=ep)
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(subs),
        "captions": subs
    }

@app.get("/movies")
def get_movies(page: int = 1, per_page: int = 24, sort: str = "RECOMMEND"):
    url = f"{API_BASE}/subject/filter"
    payload = {"tabId": 2, "filter": {"sort": sort, "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}, "page": page, "perPage": per_page}
    resp = engine.session.post(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, json=payload, timeout=20)
    data = resp.json().get("data", {})
    raw = data.get("items") or data.get("subjects") or []
    return {
        "page": page,
        "per_page": per_page,
        "total": data.get("pager", {}).get("totalCount") or len(raw),
        "items": [
            {
                "title": s.get("title"),
                "subject_id": str(s.get("subjectId")),
                "detail_path": s.get("detailPath"),
                "cover": s.get("cover", {}).get("url") if isinstance(s.get("cover"), dict) else None,
                "rating": s.get("imdbRatingValue"),
                "year": s.get("releaseDate", "")[:4] if s.get("releaseDate") else None
            }
            for s in raw
        ]
    }

@app.get("/tv-series")
def get_tv_series(page: int = 1, per_page: int = 24, sort: str = "RECOMMEND"):
    url = f"{API_BASE}/subject/filter"
    payload = {"tabId": 5, "filter": {"sort": sort, "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}, "page": page, "perPage": per_page}
    resp = engine.session.post(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, json=payload, timeout=20)
    data = resp.json().get("data", {})
    raw = data.get("items") or data.get("subjects") or []
    return {
        "page": page,
        "per_page": per_page,
        "total": data.get("pager", {}).get("totalCount") or len(raw),
        "items": [
            {
                "title": s.get("title"),
                "subject_id": str(s.get("subjectId")),
                "detail_path": s.get("detailPath"),
                "cover": s.get("cover", {}).get("url") if isinstance(s.get("cover"), dict) else None,
                "rating": s.get("imdbRatingValue"),
                "year": s.get("releaseDate", "")[:4] if s.get("releaseDate") else None
            }
            for s in raw
        ]
    }

@app.get("/home")
def get_home():
    url = f"{API_BASE}/home?host=moviebox.ph"
    resp = engine.session.get(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, timeout=20)
    return resp.json()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)


# =====================================================================
# TNxFlix Web additions (2026-11-17)
# - /            -> the TNxFlix web frontend (files in ./web)
# - /proxy       -> media byte proxy (browsers cannot fetch the CDN
#                   directly: wrong UA gets 428, CORS blocks the rest)
# - /dash        -> DASH manifest fetch + segment-URL rewrite to /proxy
# - /sub         -> subtitle fetch + SRT -> WebVTT conversion
# Header rules mirror the Android app (ApiClient.playbackHeaderFields):
#   direct file (MP4) -> spoofed User-Agent ONLY, no Referer, no Cookie
#                        (the direct-file CDN rejects the DASH referer
#                        with HTTP 429)
#   DASH (.mpd + its segments) -> UA + Referer https://sportslive.wine
#                        + the sign Cookie when the stream carries one
# Security: /proxy, /dash and /sub ONLY accept URLs on the known media
# CDN hosts (*.hakunaymatata.com, *.aoneroom.com). Anything else -> 403.
# This is deliberately NOT an open proxy.
# =====================================================================
from pathlib import Path as _Path
from urllib.parse import quote as _quote, urljoin as _urljoin

from fastapi import Request as _Request
from fastapi.responses import FileResponse as _FileResponse
from fastapi.responses import PlainTextResponse as _PlainTextResponse
from fastapi.responses import StreamingResponse as _StreamingResponse
from fastapi.staticfiles import StaticFiles as _StaticFiles

_WEB_DIR = _Path(__file__).resolve().parent / "web"
_STREAM_REFERER = "https://sportslive.wine"
_ALLOWED_MEDIA_SUFFIXES = ("hakunaymatata.com", "aoneroom.com")


def _media_host_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_MEDIA_SUFFIXES)


def _playback_headers(url: str, cookie: Optional[str] = None, dash: bool = False) -> Dict[str, str]:
    """Playback headers exactly as the app sends them (see header note)."""
    headers = {"User-Agent": engine.profile["user_agent"]}
    is_dash = dash or urlsplit(url).path.lower().endswith(".mpd")
    if is_dash:
        headers["Referer"] = _STREAM_REFERER
        if cookie:
            headers["Cookie"] = cookie
    return headers


def _open_media_stream(url: str, headers: Dict[str, str]):
    """Open a streaming GET against the media CDN.

    Prefers curl_cffi (Chrome TLS fingerprint, the same transport the
    API calls use); falls back to httpx when curl_cffi is unavailable.
    Returns a response object exposing .status_code, .headers,
    .iter_content() and .close() in both cases.
    """
    if cr is not None:
        return cr.get(url, headers=headers, stream=True, timeout=30,
                      impersonate="chrome120")
    import httpx as _httpx

    client = _httpx.Client(timeout=_httpx.Timeout(30.0, read=120.0),
                           follow_redirects=True)
    resp = client.send(_httpx.Request("GET", url, headers=headers), stream=True)
    _orig_close = resp.close

    def _close_both():
        try:
            _orig_close()
        finally:
            client.close()

    resp.close = _close_both  # type: ignore[method-assign]
    return resp


@app.get("/proxy")
def proxy_media(url: str = Query(...), cookie: Optional[str] = None,
                dash: int = 0, request: _Request = None):
    """Stream a media file/segment through this server.

    Forwards the browser's Range header and returns the upstream status
    (200/206/...), Content-Type, Content-Length and Content-Range
    faithfully so the <video> element can seek.
    """
    if not _media_host_allowed(url):
        raise HTTPException(status_code=403, detail="Host not allowed")
    headers = _playback_headers(url, cookie=cookie, dash=bool(dash))
    range_header = request.headers.get("range") if request is not None else None
    if range_header:
        headers["Range"] = range_header

    upstream = _open_media_stream(url, headers)

    def _body():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    resp_headers = {}
    for name in ("content-length", "content-range", "accept-ranges"):
        value = upstream.headers.get(name)
        if value:
            resp_headers[name.title()] = value
    return _StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type") or "application/octet-stream",
    )


@app.get("/dash")
def dash_manifest(url: str = Query(...), cookie: Optional[str] = None):
    """Fetch a DASH manifest and rewrite its media URLs to /proxy.

    SegmentTemplate media/initialization, SegmentList sourceURL and
    BaseURL resolution are all handled: every media URL is resolved
    against the manifest (honouring BaseURL) and replaced with a
    /proxy URL carrying dash=1 (so segments get the DASH headers).
    BaseURL elements are then removed so dash.js does not re-base the
    already-absolute proxy URLs.
    """
    if not _media_host_allowed(url):
        raise HTTPException(status_code=403, detail="Host not allowed")
    headers = _playback_headers(url, cookie=cookie, dash=True)
    try:
        resp = engine.session.get(url, headers=headers, timeout=20)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Manifest fetch failed: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"Manifest fetch returned {resp.status_code}")
    mpd = resp.text

    base_match = re.search(r"<BaseURL>(.*?)</BaseURL>", mpd, flags=re.S)
    effective_base = _urljoin(url, base_match.group(1).strip()) if base_match else url

    def _proxied(raw: str) -> str:
        resolved = raw if raw.lower().startswith(("http://", "https://")) else _urljoin(effective_base, raw)
        out = "/proxy?url=" + _quote(resolved, safe="$%-._~") + "&dash=1"
        if cookie:
            out += "&cookie=" + _quote(cookie, safe="")
        return out

    def _rewrite_attr(match):
        return f'{match.group(1)}="{_proxied(match.group(2))}"'

    mpd = re.sub(r'\b(media|initialization|sourceURL)="([^"]+)"', _rewrite_attr, mpd)
    mpd = re.sub(r"<BaseURL>.*?</BaseURL>", "", mpd, flags=re.S)
    return _PlainTextResponse(mpd, media_type="application/dash+xml")


def _srt_to_vtt(srt: str) -> str:
    text = srt.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if text.lstrip().startswith("WEBVTT"):
        return text
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text.strip() + "\n"


@app.get("/sub")
def subtitle_proxy(url: str = Query(...)):
    """Fetch an .srt subtitle and serve it as WebVTT for <track>."""
    if not _media_host_allowed(url):
        raise HTTPException(status_code=403, detail="Host not allowed")
    headers = {"User-Agent": engine.profile["user_agent"]}
    try:
        resp = engine.session.get(url, headers=headers, timeout=20)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Subtitle fetch failed: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"Subtitle fetch returned {resp.status_code}")
    return _PlainTextResponse(_srt_to_vtt(resp.text), media_type="text/vtt")


@app.get("/")
def tnxflix_home():
    return _FileResponse(_WEB_DIR / "index.html")


if _WEB_DIR.is_dir():
    app.mount("/assets", _StaticFiles(directory=str(_WEB_DIR)), name="web-assets")
