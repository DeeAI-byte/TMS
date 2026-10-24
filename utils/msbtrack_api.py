"""
Fleet tracking API client + data layer for the Streamlit dashboard.

AUTH
----
The upstream portal uses ASP.NET session cookies (no API key). Cookies are read, in
this order, from:
  1. st.secrets            (top-level keys, or inside a [fleet] / [msb] table)
  2. environment variables / a local .env file (python-dotenv, optional)

    FLEET_ORG_ID        = "599"
    FLEET_SESSION_ID    = "<ASP.NET_SessionId cookie value>"     # required
    FLEET_AWSELB        = "<AWSELB cookie value>"                # optional (sticky session)
    FLEET_AWSELBCORS    = "<AWSELBCORS cookie value>"            # optional (sticky session)

Legacy MSB_* names are still accepted as a fallback so existing Streamlit secrets
keep working without a change.

Optional overrides: FLEET_BASE_URL, FLEET_TIMEOUT, FLEET_MAX_WORKERS and per-endpoint
path templates FLEET_EP_VEHICLE_LIST / FLEET_EP_VEHICLE_STATUS / FLEET_EP_TELEMETRY /
FLEET_EP_DISTANCE / FLEET_EP_OVERSPEED (placeholders: {org_id}, {vehicle_id}).

Endpoint HTTP methods can be overridden with:
    FLEET_EP_METHOD_VEHICLE_LIST
    FLEET_EP_METHOD_VEHICLE_STATUS
    FLEET_EP_METHOD_TELEMETRY
    FLEET_EP_METHOD_DISTANCE
    FLEET_EP_METHOD_OVERSPEED

ASSUMPTIONS
-----------
* GetVehicleList2 is POST.
* GetLatestDeviceData accepts GET or POST.
* GetVehicleStatus / GetTotalDistance / GetOverspeed are under /Home/ and take the
  org as ?OrgId=<id>. If the live system differs, override with FLEET_EP_*.
* GetOverspeed's response shape is unknown. The parser accepts either one row per
  vehicle with a count column, or one row per overspeed event (rows are then counted).
* Latitude/Longitude in the sample telemetry look SWAPPED (lat 81.1, lon 26.9 is
  Lucknow's lon/lat reversed). normalize_coords() swaps them when that is detected.
* Distance/Fuel strings carry units ("19858.78 km", "0.00 l") and are parsed to floats.
* Optional manual override file: data/fleet_vehicle_mapping.csv with columns
  "Vehicle Number","Vehicle ID" (wins over the master list for those vehicles).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import streamlit as st
except Exception:  # pragma: no cover - lets the module import outside Streamlit
    st = None

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv is optional
    pass

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://gpsv1.msbtrack.com"

DEFAULT_ENDPOINTS = {
    "vehicle_list": "/Home/GetVehicleList2?ORG_ID={org_id}",
    "vehicle_status": "/Home/GetVehicleStatus?OrgId={org_id}",
    "telemetry": "/Home/GetLatestDeviceData?TRACCARID={vehicle_id}&vORGID={org_id}",
    "distance": "/Home/GetTotalDistance?OrgId={org_id}",
    "overspeed": "/Home/GetOverspeed?OrgId={org_id}",
}

DEFAULT_METHODS = {
    "vehicle_list": "POST",
    "vehicle_status": "GET",
    "telemetry": "GET",
    "distance": "GET",
    "overspeed": "GET",
}

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL_SECONDS = 55

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

ONLINE_STATUSES = ("running", "idle", "online")
STATUS_ORDER = ["running", "idle", "online", "offline", "disconnected", "unknown"]
STATUS_COLORS = {
    "running": "#2ecc71",
    "idle": "#f39c12",
    "online": "#3498db",
    "offline": "#7f8c8d",
    "disconnected": "#e74c3c",
    "unknown": "#34495e",
}
FOLIUM_COLORS = {
    "running": "green",
    "idle": "orange",
    "online": "blue",
    "offline": "gray",
    "disconnected": "red",
    "unknown": "black",
}
_STATUS_SYNONYMS = {
    "moving": "running",
    "stop": "idle",
    "stopped": "idle",
    "parked": "idle",
    "disconnect": "disconnected",
    "not connected": "disconnected",
    "inactive": "offline",
}
LIVE_TABLE_COLUMNS = [
    "Vehicle Number", "Vehicle ID", "Device Status", "Motion", "Speed", "Latitude",
    "Longitude", "Distance", "Fuel", "Ignition", "GPS", "Power", "MakeModel", "Last Update",
]
_MAPPING_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fleet_vehicle_mapping.csv"
)

_LOGIN_PATH_RE = re.compile(r"(^|/)(login|signin|sign-in|logon)(/|$|\.aspx)", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------
class FleetAPIError(Exception):
    """Any non-auth failure talking to the tracking API."""


class FleetAuthError(FleetAPIError):
    """The ASP.NET session cookie was rejected / expired."""


_AUTH_HELP = (
    "Fleet tracking session rejected (cookies expired or invalid). Log in to the tracking "
    "portal in a browser, copy fresh ASP.NET_SessionId (plus AWSELB / AWSELBCORS) cookie "
    "values into Streamlit secrets, and refresh."
)


# --------------------------------------------------------------------------------------
# Diagnostics helpers
# --------------------------------------------------------------------------------------
def _safe_headers(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in dict(headers or {}).items():
        if str(k).lower() in {"cookie", "set-cookie", "authorization"}:
            continue
        out[str(k)] = str(v)
    return out


def _body_preview(resp: Optional[requests.Response], limit: int = 500) -> str:
    if resp is None:
        return ""
    text = resp.text or ""
    return text[:limit].replace("\n", "\\n").replace("\r", "\\r")


def _response_diagnostics(
    resp: Optional[requests.Response],
    method: str,
    url: str,
    redirects: Optional[List[str]] = None,
    extra: str = "",
) -> str:
    lines = [
        f"HTTP method: {method}",
        f"URL: {url}",
    ]
    if redirects:
        lines.append("Redirect chain: " + " -> ".join(redirects))
    if resp is not None:
        lines.append(f"Status code: {resp.status_code}")
        lines.append(f"Response headers: {_safe_headers(resp.headers)}")
        location = resp.headers.get("Location")
        if location:
            lines.append(f"Redirect target: {location}")
        lines.append(f"Response body[0:500]: {_body_preview(resp)}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _looks_like_login_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    if "checkuserloging" in path or "checkuserlogin" in path:
        return False
    return bool(_LOGIN_PATH_RE.search(path))


def _looks_like_login_body(text: str) -> bool:
    sample = (text or "")[:8000].lower()
    strong_markers = (
        'type="password"',
        "type='password'",
        'name="password"',
        "name='password'",
        'id="password"',
        "id='password'",
        'name="username"',
        "name='username'",
        "loginform",
        "login-form",
        "signin-form",
    )
    if any(marker in sample for marker in strong_markers):
        return True
    if "password" in sample and ("sign in" in sample or "log in" in sample or "login" in sample):
        return True
    return False


def _is_html_response(resp: requests.Response) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    text = (resp.text or "").lstrip()
    return "html" in content_type or text.startswith("<")


def _is_method_or_html_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "http 405" in msg or "html response" in msg


# --------------------------------------------------------------------------------------
# Configuration (st.secrets -> env/.env)
# --------------------------------------------------------------------------------------
def _read_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a setting from st.secrets (top-level or nested) and env vars.

    Accepts both FLEET_* (preferred) and legacy MSB_* names.
    """
    upper = name.upper()
    candidates = [upper]
    if upper.startswith("FLEET_"):
        candidates.append("MSB_" + upper[len("FLEET_"):])

    nested_keys = set()
    for n in candidates:
        lower = n.lower()
        nested_keys.add(lower)
        for prefix in ("fleet_", "msb_"):
            if lower.startswith(prefix):
                nested_keys.add(lower[len(prefix):])
                break

    nested_tables = ("fleet", "msb")

    if st is not None:
        try:
            for n in candidates:
                val = st.secrets.get(n)
                if val not in (None, ""):
                    return str(val)
            for table in nested_tables:
                try:
                    nested = st.secrets.get(table)
                except Exception:
                    nested = None
                if not nested:
                    continue
                for key in nested_keys:
                    val = nested.get(key)
                    if val not in (None, ""):
                        return str(val)
        except Exception:
            pass

    for n in candidates:
        val = os.environ.get(n)
        if val not in (None, ""):
            return val

    return default


def _to_float(val: Optional[str], default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@dataclass
class FleetConfig:
    org_id: str = "599"
    session_id: str = ""
    awselb: str = ""
    awselbcors: str = ""
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 15.0
    max_workers: int = 8
    endpoints: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENDPOINTS))
    endpoint_methods: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_METHODS))

    @property
    def is_configured(self) -> bool:
        return bool(self.session_id)

    def fingerprint(self) -> str:
        """Stable hash used as the cache key (never exposes cookie values)."""
        raw = "|".join([
            self.org_id,
            self.session_id,
            self.awselb,
            self.awselbcors,
            self.base_url,
            json.dumps(self.endpoints, sort_keys=True),
            json.dumps(self.endpoint_methods, sort_keys=True),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def get_config() -> FleetConfig:
    endpoints = {
        key: _read_setting(f"FLEET_EP_{key.upper()}", tmpl) or tmpl
        for key, tmpl in DEFAULT_ENDPOINTS.items()
    }
    methods = {
        key: (_read_setting(f"FLEET_EP_METHOD_{key.upper()}", default) or default).upper()
        for key, default in DEFAULT_METHODS.items()
    }
    return FleetConfig(
        org_id=_read_setting("FLEET_ORG_ID", "599") or "599",
        session_id=_read_setting("FLEET_SESSION_ID", "") or "",
        awselb=_read_setting("FLEET_AWSELB", "") or "",
        awselbcors=_read_setting("FLEET_AWSELBCORS", "") or "",
        base_url=(_read_setting("FLEET_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        timeout=_to_float(_read_setting("FLEET_TIMEOUT"), 15.0),
        max_workers=max(1, int(_to_float(_read_setting("FLEET_MAX_WORKERS"), 8))),
        endpoints=endpoints,
        endpoint_methods=methods,
    )


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------
def normalize_status(value: Any) -> str:
    s = str(value or "").strip().lower()
    if not s or s in ("nan", "none"):
        return ""
    return _STATUS_SYNONYMS.get(s, s)


def normalize_vehicle_number(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _yn(value: Any) -> str:
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return "Yes"
    if s in ("false", "0", "no", "off"):
        return "No"
    return "—"


def _num_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(",", "", regex=False)
    return pd.to_numeric(cleaned.str.extract(r"(-?\d+(?:\.\d+)?)")[0], errors="coerce")


def normalize_coords(lat: float, lon: float) -> Tuple[float, float]:
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        return float("nan"), float("nan")
    if lat == 0 and lon == 0:
        return float("nan"), float("nan")
    if abs(lat) > 90 and abs(lon) <= 90:
        lat, lon = lon, lat
    elif 68 <= lat <= 98 and 6 <= lon <= 38:
        lat, lon = lon, lat
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return float("nan"), float("nan")
    return float(lat), float(lon)


_REACH_FORMATS = ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def parse_reach_time(value: Any):
    s = str(value or "").strip()
    if not s or s.lower() in ("nan", "none"):
        return pd.NaT
    for fmt in _REACH_FORMATS:
        try:
            return pd.Timestamp(datetime.strptime(s, fmt))
        except ValueError:
            continue
    return pd.NaT


def _records(data: Any) -> List[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        return [data]
    return []


def _pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lowered = {str(c).lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------
class FleetClient:
    def __init__(self, cfg: FleetConfig):
        self.cfg = cfg
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.6,
            status_forcelist=(500, 502, 503, 504),
            # All POSTs here are read-only lookups (Get* endpoints), so retrying on 5xx is safe.
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=self.cfg.max_workers,
            pool_maxsize=self.cfg.max_workers,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)

        host = urlparse(self.cfg.base_url).hostname
        if not host:
            raise FleetAPIError("FLEET_BASE_URL must include a valid hostname.")

        for name, val in (
            ("ASP.NET_SessionId", self.cfg.session_id),
            ("AWSELB", self.cfg.awselb),
            ("AWSELBCORS", self.cfg.awselbcors),
        ):
            if val:
                s.cookies.set(name, val, domain=host, path="/")

        s.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; TMS-Dashboard/1.0)",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.cfg.base_url + "/Home/VehicleDashboard?orgId=" + self.cfg.org_id,
            "Origin": self.cfg.base_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        })
        return s

    def _path(self, key: str, **extra: Any) -> str:
        return self.cfg.endpoints[key].format(org_id=self.cfg.org_id, **extra)

    def _absolute_url(self, path_or_url: str) -> str:
        url = path_or_url if path_or_url.startswith("http") else self.cfg.base_url + path_or_url
        if urlparse(url).hostname != urlparse(self.cfg.base_url).hostname:
            raise ValueError("Refusing to send session cookies to a different host.")
        return url

    def _request(
        self,
        method: str,
        url: str,
        data: Any = None,
    ) -> Tuple[requests.Response, str, str, List[str]]:
        method = method.upper()
        current_url = url
        current_data = data
        redirects: List[str] = []

        for _ in range(6):
            try:
                resp = self.session.request(
                    method,
                    current_url,
                    timeout=self.cfg.timeout,
                    allow_redirects=False,
                    data=current_data,
                )
            except requests.exceptions.RequestException as exc:
                raise FleetAPIError(
                    f"Request failed for {urlparse(current_url).path}: {exc.__class__.__name__}"
                ) from exc

            location = resp.headers.get("Location")
            if resp.status_code in _REDIRECT_STATUSES and location:
                target = urljoin(current_url, location)
                if urlparse(target).hostname != urlparse(self.cfg.base_url).hostname:
                    raise FleetAPIError(
                        "Refusing redirect to a different host.\n"
                        + _response_diagnostics(resp, method, current_url, redirects, f"Target: {target}")
                    )

                redirects.append(target)

                if _looks_like_login_url(target):
                    raise FleetAuthError(
                        _AUTH_HELP
                        + "\n\n"
                        + _response_diagnostics(resp, method, current_url, redirects)
                    )

                # Browser-compatible redirect method rules:
                # 303 -> GET; 301/302 on POST -> GET; 307/308 preserve method.
                if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                    method = "GET"
                    current_data = None

                current_url = target
                continue

            return resp, current_url, method, redirects

        raise FleetAPIError(
            "Too many redirects.\n"
            + _response_diagnostics(None, method, current_url, redirects)
        )

    def _is_auth_response(self, resp: requests.Response, final_url: str) -> bool:
        if resp.status_code == 401:
            return True

        text = resp.text or ""

        if _looks_like_login_url(final_url):
            return True

        if resp.status_code in (401, 403) and _looks_like_login_body(text):
            return True

        if _is_html_response(resp) and _looks_like_login_body(text):
            return True

        return False

    def _request_json_with_method(
        self,
        path_or_url: str,
        method: str = "GET",
        data: Any = None,
    ) -> Any:
        url = self._absolute_url(path_or_url)
        resp, final_url, final_method, redirects = self._request(method, url, data=data)
        diag = _response_diagnostics(resp, final_method, final_url, redirects)

        if self._is_auth_response(resp, final_url):
            raise FleetAuthError(_AUTH_HELP + "\n\n" + diag)

        if resp.status_code >= 400:
            raise FleetAPIError(f"HTTP {resp.status_code} from {urlparse(final_url).path}\n{diag}")

        text = (resp.text or "").strip()
        if not text:
            return []

        if text[:1] == "<":
            raise FleetAPIError(f"HTML response from {urlparse(final_url).path}\n{diag}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise FleetAPIError(f"Non-JSON response from {urlparse(final_url).path}\n{diag}") from exc

        if isinstance(data, dict) and set(data.keys()) == {"d"}:
            data = data["d"]

        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass

        return data

    def _request_endpoint(self, key: str, **extra: Any) -> Any:
        path = self._path(key, **extra)
        primary = self.cfg.endpoint_methods.get(key, "GET").upper()

        methods = [primary]
        if key != "vehicle_list":
            alternate = "POST" if primary != "POST" else "GET"
            methods.append(alternate)

        last_exc: Optional[Exception] = None
        for idx, method in enumerate(methods):
            try:
                return self._request_json_with_method(path, method=method)
            except FleetAuthError:
                raise
            except FleetAPIError as exc:
                last_exc = exc
                if idx == 0 and _is_method_or_html_error(exc):
                    continue
                raise

        if last_exc is not None:
            raise last_exc
        raise FleetAPIError(f"Unable to call endpoint {key}.")

    def get_json(self, path_or_url: str, method: str = "GET", data: Any = None) -> Any:
        """GET/POST + JSON decode with true session-expiry detection and diagnostics."""
        return self._request_json_with_method(path_or_url, method=method, data=data)

    # ---- API 1: vehicle master ------------------------------------------------------
    def fetch_vehicle_master(self) -> pd.DataFrame:
        recs = _records(self._request_endpoint("vehicle_list"))
        df = pd.DataFrame(recs)
        id_col = _pick_col(df, ["ID", "VehicleId", "Id"]) if not df.empty else None
        name_col = _pick_col(df, ["NAME", "VehicleName", "VEHICLE_NAME"]) if not df.empty else None
        if not id_col or not name_col:
            return pd.DataFrame(columns=["Vehicle ID", "Vehicle Number"])
        out = pd.DataFrame({
            "Vehicle ID": pd.to_numeric(df[id_col], errors="coerce"),
            "Vehicle Number": df[name_col].astype(str).str.strip().str.upper(),
        }).dropna(subset=["Vehicle ID"])
        out["Vehicle ID"] = out["Vehicle ID"].astype("int64")
        return out.drop_duplicates(subset=["Vehicle ID"]).reset_index(drop=True)

    # ---- API 2: status --------------------------------------------------------------
    def fetch_status(self) -> pd.DataFrame:
        recs = _records(self._request_endpoint("vehicle_status"))
        df = pd.DataFrame(recs)
        id_col = _pick_col(df, ["VehicleId", "ID"]) if not df.empty else None
        st_col = _pick_col(df, ["status", "deviceStatus"]) if not df.empty else None
        if not id_col or not st_col:
            return pd.DataFrame(columns=["Vehicle ID", "API Status"])
        out = pd.DataFrame({
            "Vehicle ID": pd.to_numeric(df[id_col], errors="coerce"),
            "API Status": df[st_col].map(normalize_status),
        }).dropna(subset=["Vehicle ID"])
        out["Vehicle ID"] = out["Vehicle ID"].astype("int64")
        return out.drop_duplicates(subset=["Vehicle ID"], keep="last").reset_index(drop=True)

    # ---- API 3: telemetry -----------------------------------------------------------
    def fetch_telemetry_all(self, vehicle_ids: List[int]) -> Tuple[List[dict], List[str]]:
        records: List[dict] = []
        errors: List[str] = []

        def one(vid: int) -> Optional[dict]:
            recs = _records(self._request_endpoint("telemetry", vehicle_id=vid))
            if not recs:
                return None
            rec = dict(recs[0])
            rec["_requested_id"] = vid
            return rec

        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as pool:
            futures = {pool.submit(one, int(v)): int(v) for v in vehicle_ids}
            for fut in as_completed(futures):
                vid = futures[fut]
                try:
                    rec = fut.result()
                except FleetAuthError:
                    for f in futures:
                        f.cancel()
                    raise
                except Exception as exc:
                    errors.append(f"{vid}: {exc}")
                else:
                    if rec:
                        records.append(rec)
                    else:
                        errors.append(f"{vid}: empty telemetry response")
        return records, errors

    # ---- API 4 / 5: analytics -------------------------------------------------------
    def fetch_distance(self) -> pd.DataFrame:
        df = pd.DataFrame(_records(self._request_endpoint("distance")))
        cols = ["Vehicle Number", "Distance (km)", "Fuel (l)"]
        if df.empty:
            return pd.DataFrame(columns=cols)
        name_col = _pick_col(df, ["VEHICLE_NAME", "VehicleName", "NAME", "Vehicle"])
        dist_col = _pick_col(df, ["DISTANCECOUNT", "Distance", "TotalDistance"])
        fuel_col = _pick_col(df, ["FUELCOUNT", "Fuel"])
        if not name_col:
            return pd.DataFrame(columns=cols)
        out = pd.DataFrame({
            "Vehicle Number": df[name_col].astype(str).str.strip().str.upper(),
            "Distance (km)": _num_series(df[dist_col]) if dist_col else float("nan"),
            "Fuel (l)": _num_series(df[fuel_col]) if fuel_col else float("nan"),
        })
        return out.groupby("Vehicle Number", as_index=False).sum(min_count=1)

    def fetch_overspeed(self) -> pd.DataFrame:
        df = pd.DataFrame(_records(self._request_endpoint("overspeed")))
        cols = ["Vehicle Number", "Overspeed Events"]
        if df.empty:
            return pd.DataFrame(columns=cols)
        name_col = _pick_col(df, ["VEHICLE_NAME", "VehicleName", "Vehicle", "NAME", "Name", "VehicleNumber"])
        if not name_col:
            name_col = next((c for c in df.columns if "vehicle" in str(c).lower() or "name" in str(c).lower()), None)
        if not name_col:
            return pd.DataFrame(columns=cols)
        count_col = _pick_col(df, [
            "OVERSPEEDCOUNT", "OverspeedCount", "OVERSPEED_COUNT", "COUNT", "Count", "OVERSPEED",
            "TotalOverspeed", "Events",
        ])
        names = df[name_col].astype(str).str.strip().str.upper()
        if count_col and _num_series(df[count_col]).notna().any():
            out = pd.DataFrame({"Vehicle Number": names, "Overspeed Events": _num_series(df[count_col])})
            return out.groupby("Vehicle Number", as_index=False).sum(min_count=1)
        out = names.value_counts().rename_axis("Vehicle Number").reset_index(name="Overspeed Events")
        return out


# --------------------------------------------------------------------------------------
# Mapping layer: vehicle number <-> vehicle/device/traccar ID
# --------------------------------------------------------------------------------------
class VehicleMapper:
    """Vehicle number <-> ID lookup built from the master list (+ optional CSV overrides)."""

    def __init__(self, master: pd.DataFrame, overrides: Optional[pd.DataFrame] = None):
        self.id_to_name: Dict[int, str] = {}
        self.key_to_id: Dict[str, int] = {}
        for vid, name in zip(master["Vehicle ID"], master["Vehicle Number"]):
            self.id_to_name[int(vid)] = name
            self.key_to_id[normalize_vehicle_number(name)] = int(vid)
        if overrides is not None and not overrides.empty:
            for vid, name in zip(overrides["Vehicle ID"], overrides["Vehicle Number"]):
                try:
                    self.id_to_name[int(vid)] = str(name).strip().upper()
                    self.key_to_id[normalize_vehicle_number(name)] = int(vid)
                except (TypeError, ValueError):
                    continue

    def id_for(self, vehicle_number: Any) -> Optional[int]:
        return self.key_to_id.get(normalize_vehicle_number(vehicle_number))

    def name_for(self, vehicle_id: Any) -> Optional[str]:
        try:
            return self.id_to_name.get(int(vehicle_id))
        except (TypeError, ValueError):
            return None


def load_mapping_overrides() -> pd.DataFrame:
    if not os.path.exists(_MAPPING_OVERRIDE_FILE):
        return pd.DataFrame(columns=["Vehicle Number", "Vehicle ID"])
    try:
        df = pd.read_csv(_MAPPING_OVERRIDE_FILE)
        df.columns = [str(c).strip() for c in df.columns]
        return df[["Vehicle Number", "Vehicle ID"]].dropna()
    except Exception:
        return pd.DataFrame(columns=["Vehicle Number", "Vehicle ID"])


# --------------------------------------------------------------------------------------
# Snapshot assembly
# --------------------------------------------------------------------------------------
_TELEMETRY_KEYS = [
    "VehicleId", "deviceStatus", "Motion", "Latitude", "Longitude", "VehicleSpeed", "Distance",
    "fuel", "Ignition", "MakeModel", "ReachTime", "Power", "GPS", "OrgName",
]


def _telemetry_frame(records: List[dict]) -> pd.DataFrame:
    cols = [
        "Vehicle ID", "Telemetry Vehicle ID", "Telemetry Status", "Motion", "Speed", "Latitude",
        "Longitude", "Distance", "Fuel", "Ignition", "GPS", "Power", "MakeModel", "Last Update",
    ]
    if not records:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(records)
    for key in _TELEMETRY_KEYS:
        if key not in df.columns:
            df[key] = None
    lat = _num_series(df["Latitude"])
    lon = _num_series(df["Longitude"])
    coords = [normalize_coords(a, b) for a, b in zip(lat, lon)]
    return pd.DataFrame({
        "Vehicle ID": pd.to_numeric(df["_requested_id"], errors="coerce").astype("int64"),
        "Telemetry Vehicle ID": pd.to_numeric(df["VehicleId"], errors="coerce"),
        "Telemetry Status": df["deviceStatus"].map(normalize_status),
        "Motion": df["Motion"].map(_yn),
        "Speed": _num_series(df["VehicleSpeed"]),
        "Latitude": [c[0] for c in coords],
        "Longitude": [c[1] for c in coords],
        "Distance": _num_series(df["Distance"]),
        "Fuel": _num_series(df["fuel"]),
        "Ignition": df["Ignition"].map(_yn),
        "GPS": df["GPS"].map(_yn),
        "Power": df["Power"].map(_yn),
        "MakeModel": df["MakeModel"].fillna("").astype(str),
        "Last Update": pd.to_datetime(pd.Series([parse_reach_time(v) for v in df["ReachTime"]]), errors="coerce"),
    })


def build_fleet_snapshot(cfg: FleetConfig) -> Dict[str, Any]:
    """Fetches everything and returns {fleet, distance, overspeed, warnings, timings, fetched_at}.
    Vehicle master is mandatory (errors propagate); every other source degrades to a warning."""
    client = FleetClient(cfg)
    warnings: List[str] = []
    timings: Dict[str, float] = {}

    t = time.perf_counter()
    master = client.fetch_vehicle_master()
    timings["vehicle_list"] = round(time.perf_counter() - t, 2)
    if master.empty:
        raise FleetAPIError("Empty vehicle list returned - check FLEET_ORG_ID and the session cookies.")

    mapper = VehicleMapper(master, load_mapping_overrides())
    master = pd.DataFrame({
        "Vehicle ID": list(mapper.id_to_name.keys()),
        "Vehicle Number": list(mapper.id_to_name.values()),
    })

    def optional(name: str, fn, empty: pd.DataFrame) -> pd.DataFrame:
        t0 = time.perf_counter()
        try:
            return fn()
        except FleetAuthError:
            raise
        except Exception as exc:
            warnings.append(f"{name}: {exc}")
            return empty
        finally:
            timings[name] = round(time.perf_counter() - t0, 2)

    status = optional("vehicle_status", client.fetch_status, pd.DataFrame(columns=["Vehicle ID", "API Status"]))

    t = time.perf_counter()
    records, tele_errors = client.fetch_telemetry_all(master["Vehicle ID"].tolist())
    timings["telemetry"] = round(time.perf_counter() - t, 2)
    if tele_errors:
        warnings.append(f"telemetry failed for {len(tele_errors)} vehicle(s), e.g. " + "; ".join(tele_errors[:3]))
    tele = _telemetry_frame(records)

    distance = optional("distance", client.fetch_distance, pd.DataFrame(columns=["Vehicle Number", "Distance (km)", "Fuel (l)"]))
    overspeed = optional("overspeed", client.fetch_overspeed, pd.DataFrame(columns=["Vehicle Number", "Overspeed Events"]))

    status["Vehicle ID"] = pd.to_numeric(status["Vehicle ID"]).astype("int64")
    tele["Vehicle ID"] = pd.to_numeric(tele["Vehicle ID"]).astype("int64")

    fleet = master.merge(status, on="Vehicle ID", how="left").merge(tele, on="Vehicle ID", how="left")
    fleet["Device Status"] = fleet["API Status"].replace("", pd.NA).fillna(fleet["Telemetry Status"].replace("", pd.NA)).fillna("unknown")

    mismatched = fleet[fleet["Telemetry Vehicle ID"].notna() & (fleet["Telemetry Vehicle ID"] != fleet["Vehicle ID"])]
    if len(mismatched):
        warnings.append(
            f"ID mapping check: telemetry returned a different VehicleId than requested for {len(mismatched)} "
            f"vehicle(s) (e.g. {mismatched.iloc[0]['Vehicle Number']}) - the ID == TRACCARID assumption may be wrong."
        )
    for col in ("Motion", "Ignition", "GPS", "Power"):
        fleet[col] = fleet[col].fillna("—")
    fleet["MakeModel"] = fleet["MakeModel"].fillna("")

    def canon(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["Vehicle Number"] = [
            mapper.name_for(mapper.id_for(v)) or v for v in df["Vehicle Number"]
        ]
        value_cols = [c for c in df.columns if c != "Vehicle Number"]
        return df.groupby("Vehicle Number", as_index=False)[value_cols].sum(min_count=1)

    distance, overspeed = canon(distance), canon(overspeed)
    fleet["Has Location"] = fleet["Latitude"].notna() & fleet["Longitude"].notna()
    fleet = fleet.sort_values("Vehicle Number").reset_index(drop=True)

    return {
        "fleet": fleet,
        "distance": distance,
        "overspeed": overspeed,
        "warnings": warnings,
        "timings": timings,
        "fetched_at": datetime.now(IST),
    }


def _cache(fn):
    if st is not None:
        return st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)(fn)
    return fn


@_cache
def load_fleet_snapshot(config_fingerprint: str) -> Dict[str, Any]:
    """Cached entry point (the fingerprint only keys the cache so changed cookies refetch)."""
    return build_fleet_snapshot(get_config())


# --------------------------------------------------------------------------------------
# View helpers (pure functions, used by the dashboard page)
# --------------------------------------------------------------------------------------
def compute_kpis(fleet: pd.DataFrame) -> Dict[str, int]:
    s = fleet["Device Status"] if "Device Status" in fleet.columns else pd.Series(dtype=str)
    return {
        "Total Vehicles": int(len(fleet)),
        "Running Vehicles": int((s == "running").sum()),
        "Idle Vehicles": int((s == "idle").sum()),
        "Online Vehicles": int(s.isin(ONLINE_STATUSES).sum()),
        "Offline Vehicles": int((s == "offline").sum()),
        "Disconnected Vehicles": int((s == "disconnected").sum()),
    }


def filter_fleet(fleet: pd.DataFrame, query: str = "", statuses: Optional[List[str]] = None) -> pd.DataFrame:
    out = fleet
    if statuses:
        out = out[out["Device Status"].isin(statuses)]
    q = (query or "").strip()
    if q:
        hay = out[["Vehicle Number", "Vehicle ID", "MakeModel"]].astype(str)
        mask = hay.apply(lambda col: col.str.contains(q, case=False, na=False, regex=False)).any(axis=1)
        out = out[mask]
    return out
