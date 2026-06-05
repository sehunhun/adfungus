from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=True)

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import random
import re
import socket
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError, PyJWKClient, decode as jwt_decode
from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:
    from scrapling.fetchers import AsyncStealthySession, StealthyFetcher
    from scrapling.parser import Selector
except ImportError as exc:
    raise SystemExit(
        "scrapling fetcher dependencies are missing. "
        'Install with: pip install "scrapling[fetchers]" && scrapling install'
    ) from exc

ProxyConfig = Dict[str, str]

DEFAULT_WORKERS = 1
DEFAULT_TABS = 4
DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_STABILIZE_MS = 500
DEFAULT_RESULT_TIMEOUT_S = 180
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_ALI_HELPER_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_AFFILIATE_CACHE_TTL_HOURS = 24
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_VERIFY_FIXED_PROXY_SERVER = "http://82.22.93.131:7838"
DEFAULT_REQUEST_JITTER_MIN_MS = 80
DEFAULT_REQUEST_JITTER_MAX_MS = 420
DEFAULT_PRODUCT_READY_SELECTOR = "h1.product-title span"
DEFAULT_PRODUCT_FALLBACK_SELECTOR = ".prod-title"
DEFAULT_PRODUCT_READY_TIMEOUT_MS = 10_000
DEFAULT_CHALLENGE_BYPASS_ATTEMPTS = 3
DEFAULT_REAL_CHROME = True
DEFAULT_HTML_RETRY_THRESHOLD = 3000
DEFAULT_SESSION_FETCH_ATTEMPTS = 2
DEFAULT_INIT_SCRIPT_ENABLED = False
DEFAULT_USER_DATA_DIR = ""
DEFAULT_FPJS_CDN_URL = "https://openfpcdn.io/fingerprintjs/v5/iife.min.js"
DEFAULT_FPJS_TIMEOUT_MS = 12_000
DEFAULT_FPJS_BUNDLE_FETCH_TIMEOUT_S = 20
DEFAULT_ALI_TOP_API_URL = "https://api-sg.aliexpress.com/sync"
DEFAULT_ALI_TOP_FALLBACK_API_URL = "https://eco.taobao.com/router/rest"
DEFAULT_ALI_TOP_TIMEOUT_S = 25
DEFAULT_ALI_TOP_SIGN_METHOD = "hmac"
DEFAULT_ALI_TOP_FORMAT = "json"
DEFAULT_ALI_TOP_VERSION = "2.0"
DEFAULT_ALI_TARGET_CURRENCY = "KRW"
DEFAULT_ALI_TARGET_LANGUAGE = "KO"
DEFAULT_ALI_SHIP_TO_COUNTRY = "KR"
# Temporary fixed category filter requested by user.
DEFAULT_ALI_CATEGORY_IDS = "200001081"
DEFAULT_ALI_PAGE_NO = 1
DEFAULT_ALI_PAGE_SIZE = 10

AFFILIATE_REASON_MATCHED = "matched"
AFFILIATE_REASON_NO_CANDIDATE_UNDER_PRICE = "no_candidate_under_price"
AFFILIATE_REASON_SCRAPE_DATA_INCOMPLETE = "scrape_data_incomplete"
AFFILIATE_REASON_AFFILIATE_API_EMPTY = "affiliate_api_empty"

PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json"}

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
ALI_HELPER_BASE_URL = os.getenv("ALI_HELPER_BASE_URL", DEFAULT_ALI_HELPER_BASE_URL).strip() or DEFAULT_ALI_HELPER_BASE_URL
AFFILIATE_CACHE_TTL_HOURS = int(os.getenv("AFFILIATE_CACHE_TTL_HOURS", str(DEFAULT_AFFILIATE_CACHE_TTL_HOURS)))

_fpjs_bundle_cache: Dict[str, str] = {}
_fpjs_bundle_cache_lock = threading.Lock()
_clerk_jwks_client: Optional[PyJWKClient] = None

DEFAULT_FINGERPRINT_PROFILE_SETS = [
    {
        "profile_id": "win11_chrome137_rtx3060",
        "os_name": "Windows 11",
        "browser_name": "Chrome 137",
        "platform": "Win32",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "hardware_concurrency": 12,
        "device_memory": 16,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 " "vs_5_0 ps_5_0, D3D11)"
        ),
        "canvas_noise_base": 7,
    },
    {
        "profile_id": "win11_chrome136_uhd630",
        "os_name": "Windows 11",
        "browser_name": "Chrome 136",
        "platform": "Win32",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": (
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 "
            "vs_5_0 ps_5_0, D3D11)"
        ),
        "canvas_noise_base": 10,
    },
    {
        "profile_id": "win10_chrome135_rx580",
        "os_name": "Windows 10",
        "browser_name": "Chrome 135",
        "platform": "Win32",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        "hardware_concurrency": 6,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (ATI Technologies Inc.)",
        "webgl_renderer": (
            "ANGLE (AMD, Radeon RX 580 Series Direct3D11 " "vs_5_0 ps_5_0, D3D11)"
        ),
        "canvas_noise_base": 13,
    },
]

# User-provided proxy pool (host:port:user:pass)
DEFAULT_PROXY_LINES = [
    "9.142.23.251:6408:mbjmfzbc:yty1ijjy6f4o",
    "82.22.93.131:7838:mbjmfzbc:yty1ijjy6f4o",
    "82.22.73.224:7430:mbjmfzbc:yty1ijjy6f4o",
    "82.22.89.94:7800:mbjmfzbc:yty1ijjy6f4o",
    "82.22.73.47:7253:mbjmfzbc:yty1ijjy6f4o",
]

# Direct Coupang product URLs (preferred over shortened links)
DEFAULT_PRODUCT_URLS = [
    "https://www.coupang.com/vp/products/6685056297",
    "https://www.coupang.com/vp/products/8290484543",
    "https://www.coupang.com/vp/products/1905108986",
    "https://www.coupang.com/vp/products/8909480742",
    "https://www.coupang.com/vp/products/7964259641",
]


def _setup_logging() -> logging.Logger:
    level_name = os.getenv("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    configured = logging.getLogger("scrapling-api")
    configured.setLevel(level)
    return configured


logger = _setup_logging()


def _log_event(level: int, event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "ts_ms": int(time.time() * 1000),
        **fields,
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class ScrapeTask:
    request_id: str
    url: str
    timeout_ms: int
    stabilize_ms: int
    use_proxy: bool
    proxy_override: Optional[ProxyConfig]


@dataclass(frozen=True)
class WorkerFingerprintProfile:
    profile_id: str
    os_name: str
    browser_name: str
    platform: str
    user_agent: str
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    canvas_noise: int


@dataclass
class SessionRuntimeState:
    generation: int
    session_ctx: Any
    session: Any
    in_flight: int = 0
    retired: bool = False


class ScrapeRequest(BaseModel):
    product_url: str = Field(..., description="Product detail URL")
    timeout_ms: int = Field(DEFAULT_TIMEOUT_MS, ge=5000, le=300000)
    stabilize_ms: int = Field(DEFAULT_STABILIZE_MS, ge=0, le=10000)
    result_timeout_s: int = Field(DEFAULT_RESULT_TIMEOUT_S, ge=1, le=600)
    use_proxy: bool = Field(True, description="Use proxy for this request")
    proxy: Optional[str] = Field(
        None,
        description="Optional per-request proxy override. Format: host:port:user:pass or http(s)://host:port",
    )


class ScrapeResult(BaseModel):
    request_id: str
    url: str
    status: Optional[int]
    worker_id: int
    tab_id: Optional[int] = None
    replica_id: str
    proxy_used: str
    elapsed_ms: int
    final_url: Optional[str]
    page_title: Optional[str]
    html_length: int
    fingerprint_profile: Optional[Dict[str, Any]] = None
    wait_meta: Optional[Dict[str, Any]] = None
    data: Dict[str, Any]


class FpjsDebugRequest(BaseModel):
    url: str = Field(
        "https://example.com", description="Target URL for browser context"
    )
    timeout_ms: int = Field(DEFAULT_TIMEOUT_MS, ge=5000, le=300000)
    stabilize_ms: int = Field(DEFAULT_STABILIZE_MS, ge=0, le=10000)
    result_timeout_s: int = Field(DEFAULT_RESULT_TIMEOUT_S, ge=1, le=600)
    use_proxy: bool = Field(True, description="Use configured session proxy")
    fpjs_script_url: str = Field(
        DEFAULT_FPJS_CDN_URL,
        description="FingerprintJS OSS IIFE script URL",
    )
    fpjs_timeout_ms: int = Field(DEFAULT_FPJS_TIMEOUT_MS, ge=1000, le=60000)


class FpjsDebugResult(BaseModel):
    request_id: str
    url: str
    status: Optional[int]
    worker_id: int
    tab_id: Optional[int] = None
    replica_id: str
    proxy_used: str
    elapsed_ms: int
    final_url: Optional[str]
    page_title: Optional[str]
    html_length: int
    fingerprint_profile: Optional[Dict[str, Any]] = None
    wait_meta: Optional[Dict[str, Any]] = None
    fpjs: Dict[str, Any]


class FpjsDebugSuccessResponse(BaseModel):
    ok: bool = True
    result: FpjsDebugResult


class ScrapeSuccessResponse(BaseModel):
    ok: bool = True
    result: ScrapeResult


class AffiliateMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool = Field(..., description="Must be true for a successful /scrape response")
    result: ScrapeResult


class AffiliateScrapeSummary(BaseModel):
    product_name: str
    image: str
    reference_price_krw: Optional[int]


class AffiliateMatchedProduct(BaseModel):
    product_id: str
    product_name: str
    image: str
    price_krw: int
    product_url: str
    affiliate_url: str


class AffiliateMatchResult(BaseModel):
    request_id: str
    matched: bool
    reason: str
    scrape: AffiliateScrapeSummary
    affiliate: Optional[AffiliateMatchedProduct] = None


class AffiliateMatchSuccessResponse(BaseModel):
    ok: bool = True
    result: AffiliateMatchResult


class AffiliateConfigError(ValueError):
    pass


class AliAffiliateApiError(RuntimeError):
    pass


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _resolve_server_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0] or "")
    except Exception:
        try:
            return str(socket.gethostbyname(socket.gethostname()) or "")
        except Exception:
            return ""


def _normalize_product_url(url: str) -> str:
    trimmed = url.strip()
    return trimmed


def _require_supabase_config() -> Tuple[str, str]:
    if not SUPABASE_URL:
        raise ValueError("SUPABASE_URL is required for affiliate cache pipeline")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise ValueError("SUPABASE_SERVICE_ROLE_KEY is required for affiliate cache pipeline")
    return SUPABASE_URL.rstrip("/"), SUPABASE_SERVICE_ROLE_KEY


def _extract_canonical_product_key(url: str) -> Optional[str]:
    normalized = (url or "").strip()
    if not normalized:
        return None

    try:
        parsed = urlparse(normalized)
    except Exception:
        return None

    product_match = re.search(r"/(?:vp/)?products/(\d+)", parsed.path or "", flags=re.IGNORECASE)
    product_id = (product_match.group(1).strip() if product_match else "")
    item_id = ""

    query_values = parse_qs(parsed.query or "", keep_blank_values=False)
    item_candidates = query_values.get("itemId") or query_values.get("itemid") or []
    if item_candidates:
        item_id = str(item_candidates[0]).strip()

    if not product_id or not item_id:
        return None

    return f"{product_id}-{item_id}"


def _extract_last_price_krw(scrape_data: Dict[str, Any]) -> Optional[int]:
    prices = scrape_data.get("prices")
    if not isinstance(prices, list) or not prices:
        return None

    last_value = prices[-1]
    parsed = _extract_price_number(last_value)
    if parsed is None:
        return None
    return int(round(parsed))


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout_s: int = 15) -> Dict[str, Any]:
    request_obj = UrlRequest(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with build_opener().open(request_obj, timeout=timeout_s) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected JSON shape from {url}")
    return parsed


def _supabase_rpc(function_name: str, payload: Dict[str, Any], timeout_s: int = 15) -> Any:
    base_url, service_role_key = _require_supabase_config()
    url = f"{base_url}/rest/v1/rpc/{function_name}"
    headers = {
        "Content-Type": "application/json",
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }
    request_obj = UrlRequest(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with build_opener().open(request_obj, timeout=timeout_s) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _supabase_rest_request(
    method: str,
    resource_path: str,
    *,
    query: Optional[str] = None,
    payload: Optional[Any] = None,
    prefer: Optional[str] = None,
    timeout_s: int = 15,
) -> Any:
    base_url, service_role_key = _require_supabase_config()
    resource = resource_path.strip().lstrip("/")
    if not resource:
        raise ValueError("resource_path is required")

    url = f"{base_url}/rest/v1/{resource}"
    if query:
        url = f"{url}?{query.lstrip('?')}"

    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }
    data: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if prefer:
        headers["Prefer"] = prefer

    request_obj = UrlRequest(url, data=data, headers=headers, method=method.upper())
    with build_opener().open(request_obj, timeout=timeout_s) as response:
        raw = response.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return None
    return json.loads(raw)


def _nullable_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"not found", "none", "null", "nan"}:
        return None
    return text


def _nullable_int(value: Any) -> Optional[int]:
    parsed = _extract_price_number(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _split_canonical_product_key(canonical_product_key: str) -> Tuple[str, str]:
    left, separator, right = canonical_product_key.partition("-")
    product_id = left.strip()
    item_id = right.strip() if separator else ""
    if not product_id or not item_id:
        raise ValueError(
            f"Invalid canonical_product_key for products upsert: {canonical_product_key}"
        )
    return product_id, item_id


def _supabase_ensure_product_exists(
    *,
    canonical_product_key: str,
    scrape_data: Dict[str, Any],
    timeout_s: int = 15,
) -> None:
    base_url, service_role_key = _require_supabase_config()
    product_id, item_id = _split_canonical_product_key(canonical_product_key)
    query = urlencode({"on_conflict": "product_id,item_id"})
    url = f"{base_url}/rest/v1/products?{query}"
    headers = {
        "Content-Type": "application/json",
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    payload = {
        "product_id": product_id,
        "item_id": item_id,
        "product_name": _nullable_text(scrape_data.get("product_name")),
        "image_url": _nullable_text(scrape_data.get("image")),
        "category": _nullable_text(scrape_data.get("category")),
        "option_label": _nullable_text(scrape_data.get("option_label")),
        "selected_option": _nullable_text(scrape_data.get("selected_option")),
        "shipping_fee": _nullable_int(scrape_data.get("shipping_fee")),
        "rating": _extract_price_number(scrape_data.get("rating")),
        "review_count": _nullable_int(scrape_data.get("review_count")),
    }
    request_obj = UrlRequest(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with build_opener().open(request_obj, timeout=timeout_s):
        return


def _extract_price_list_krw(scrape_data: Dict[str, Any]) -> List[int]:
    raw_prices = scrape_data.get("prices")
    if not isinstance(raw_prices, list):
        return []

    parsed_prices: List[int] = []
    for item in raw_prices:
        parsed = _extract_price_number(item)
        if parsed is None:
            continue
        price_int = int(round(parsed))
        if price_int > 0:
            parsed_prices.append(price_int)
    return parsed_prices


def _normalize_single_row(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _supabase_upsert_scraped_product(
    *,
    canonical_product_key: str,
    scrape_data: Dict[str, Any],
    source_request_id: Optional[str],
    raw_payload: Dict[str, Any],
    timeout_s: int = 15,
) -> None:
    product_id, item_id = _split_canonical_product_key(canonical_product_key)
    representative_price = _extract_last_price_krw(scrape_data)
    raw_prices = _extract_price_list_krw(scrape_data)
    shipping_fee = _nullable_int(scrape_data.get("shipping_fee"))

    _supabase_rest_request(
        "POST",
        "products",
        query=urlencode({"on_conflict": "product_id,item_id"}),
        payload={
            "product_id": product_id,
            "item_id": item_id,
            "product_name": _nullable_text(scrape_data.get("product_name")),
            "category": _nullable_text(scrape_data.get("category")),
            "image_url": _nullable_text(scrape_data.get("image")),
            "shipping_fee": shipping_fee,
            "rating": _extract_price_number(scrape_data.get("rating")),
            "review_count": _nullable_int(scrape_data.get("review_count")),
            "option_label": _nullable_text(scrape_data.get("option_label")),
            "selected_option": _nullable_text(scrape_data.get("selected_option")),
        },
        prefer="resolution=merge-duplicates,return=minimal",
        timeout_s=timeout_s,
    )

    _supabase_rest_request(
        "POST",
        "product_price_observations",
        payload={
            "canonical_product_key": canonical_product_key,
            "representative_price": representative_price,
            "prices_raw": raw_prices,
            "shipping_fee": shipping_fee,
            "source_request_id": source_request_id,
            "raw_payload": raw_payload,
        },
        prefer="return=minimal",
        timeout_s=timeout_s,
    )

    if representative_price is None:
        return

    current_response = _supabase_rest_request(
        "GET",
        "product_price_current",
        query=(
            "select=current_price,lowest_price_all"
            f"&canonical_product_key=eq.{canonical_product_key}"
        ),
        timeout_s=timeout_s,
    )
    current_row = _normalize_single_row(current_response)
    previous_price = (
        _nullable_int(current_row.get("current_price")) if current_row else None
    )
    lowest_price_all_existing = (
        _nullable_int(current_row.get("lowest_price_all")) if current_row else None
    )
    lowest_price_all = representative_price
    if lowest_price_all_existing is not None:
        lowest_price_all = min(lowest_price_all, lowest_price_all_existing)

    _supabase_rest_request(
        "POST",
        "product_price_current",
        query=urlencode({"on_conflict": "canonical_product_key"}),
        payload={
            "canonical_product_key": canonical_product_key,
            "current_price": representative_price,
            "previous_price": previous_price,
            "lowest_price_all": lowest_price_all,
        },
        prefer="resolution=merge-duplicates,return=minimal",
        timeout_s=timeout_s,
    )


def _supabase_update_affiliate_job(
    *,
    canonical_product_key: str,
    job_id: str,
    patch_payload: Dict[str, Any],
    timeout_s: int = 15,
) -> None:
    base_url, service_role_key = _require_supabase_config()
    query = (
        f"canonical_product_key=eq.{canonical_product_key}"
        f"&job_id=eq.{job_id}"
    )
    url = f"{base_url}/rest/v1/product_affiliate_matches?{query}"
    headers = {
        "Content-Type": "application/json",
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Prefer": "return=representation",
    }
    request_obj = UrlRequest(
        url,
        data=json.dumps(patch_payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="PATCH",
    )
    with build_opener().open(request_obj, timeout=timeout_s):
        return


def _call_ali_helper_scrape(product_name: str, reference_price_krw: int) -> Dict[str, Any]:
    helper_url = f"{ALI_HELPER_BASE_URL.rstrip('/')}/scrape"
    payload = {
        "product_name": product_name,
        "reference_price_krw": max(reference_price_krw, 1),
    }
    headers = {"Content-Type": "application/json"}
    return _post_json(helper_url, payload, headers, timeout_s=40)


def _normalize_cache_row(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


async def _run_affiliate_pipeline_async(
    *,
    canonical_product_key: str,
    product_name: str,
    reference_price_krw: int,
    job_id: str,
    trace_id: Optional[str],
    scrape_request_id: Optional[str],
) -> None:
    _log_event(
        logging.INFO,
        "affiliate.pipeline.start",
        trace_id=trace_id,
        scrape_request_id=scrape_request_id,
        canonical_product_key=canonical_product_key,
        job_id=job_id,
    )

    try:
        _supabase_update_affiliate_job(
            canonical_product_key=canonical_product_key,
            job_id=job_id,
            patch_payload={"status": "running", "started_at": datetime.now(timezone.utc).isoformat()},
        )

        helper_result = _call_ali_helper_scrape(product_name, reference_price_krw)
        if not isinstance(helper_result, dict):
            raise RuntimeError("8001 /scrape returned invalid payload")

        ok = bool(helper_result.get("ok"))
        status_value = "done" if ok else "failed"
        patch_payload = {
            "status": status_value,
            "scrape_ok": ok,
            "search_url": helper_result.get("search_url"),
            "selector": helper_result.get("selector"),
            "first_href": helper_result.get("first_href") or "",
            "aliexpress_product_id": helper_result.get("product_id") or "",
            "product_main_image_url": helper_result.get("product_main_image_url"),
            "target_sale_price": helper_result.get("target_sale_price"),
            "product_title": helper_result.get("product_title"),
            "promotion_link": helper_result.get("promotion_link"),
            "top_api_url": helper_result.get("top_api_url"),
            "elapsed_ms": helper_result.get("elapsed_ms"),
            "error": helper_result.get("error"),
            "raw_payload": helper_result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _supabase_update_affiliate_job(
            canonical_product_key=canonical_product_key,
            job_id=job_id,
            patch_payload=patch_payload,
        )
        _log_event(
            logging.INFO,
            "affiliate.pipeline.finished",
            trace_id=trace_id,
            scrape_request_id=scrape_request_id,
            canonical_product_key=canonical_product_key,
            job_id=job_id,
            ok=ok,
            status=status_value,
        )
    except Exception as exc:
        _log_event(
            logging.ERROR,
            "affiliate.pipeline.failed",
            trace_id=trace_id,
            scrape_request_id=scrape_request_id,
            canonical_product_key=canonical_product_key,
            job_id=job_id,
            error=str(exc),
        )
        try:
            _supabase_update_affiliate_job(
                canonical_product_key=canonical_product_key,
                job_id=job_id,
                patch_payload={
                    "status": "failed",
                    "scrape_ok": False,
                    "error": str(exc),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass


def _run_affiliate_pipeline_sync(**kwargs: Any) -> None:
    asyncio.run(_run_affiliate_pipeline_async(**kwargs))


def _clean_text(node: Any) -> str:
    if not node:
        return ""
    try:
        text = node.get_all_text()
    except Exception:
        return ""
    return text.strip() if text else ""


def _first_css(document: Any, selector: str) -> Any:
    try:
        elements = document.css(selector)
    except Exception:
        return None
    return elements[0] if elements else None


def _normalize_rating_value(value: Any) -> str:
    if value is None:
        return "Not Found"
    text = str(value).strip()
    return text or "Not Found"


def _find_rating_value_in_json_ld(payload: Any) -> str:
    if isinstance(payload, dict):
        aggregate = payload.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating = _normalize_rating_value(aggregate.get("ratingValue"))
            if rating != "Not Found":
                return rating
        elif isinstance(aggregate, list):
            for item in aggregate:
                if isinstance(item, dict):
                    rating = _normalize_rating_value(item.get("ratingValue"))
                    if rating != "Not Found":
                        return rating

        graph = payload.get("@graph")
        if graph is not None:
            rating = _find_rating_value_in_json_ld(graph)
            if rating != "Not Found":
                return rating

        for value in payload.values():
            if isinstance(value, (dict, list)):
                rating = _find_rating_value_in_json_ld(value)
                if rating != "Not Found":
                    return rating
        return "Not Found"

    if isinstance(payload, list):
        for item in payload:
            rating = _find_rating_value_in_json_ld(item)
            if rating != "Not Found":
                return rating

    return "Not Found"


def _extract_json_ld_rating(document: Any) -> str:
    try:
        script_nodes = document.css('script[type="application/ld+json"]')
    except Exception:
        return "Not Found"

    for node in script_nodes:
        raw_candidates: List[str] = []
        try:
            text = node.get_all_text()
            if text and text.strip():
                raw_candidates.append(text.strip())
        except Exception:
            pass

        node_text = getattr(node, "text", None)
        if callable(node_text):
            try:
                node_text = node_text()
            except Exception:
                node_text = None
        if isinstance(node_text, str) and node_text.strip():
            raw_candidates.append(node_text.strip())

        for raw in raw_candidates:
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            rating = _find_rating_value_in_json_ld(parsed)
            if rating != "Not Found":
                return rating

    return "Not Found"


def _extract_product_fields_from_doc(document: Any) -> Dict[str, Any]:
    category_nodes = document.css(
        "li.twc-flex.twc-items-center.twc-pb-\\[16px\\].twc-pt-\\[12px\\]"
    )
    categories = [_clean_text(node) for node in category_nodes]
    categories = [c for c in categories if c]
    category = " > ".join(categories) if categories else "Not Found"

    image_node = _first_css(document, 'img[alt="Product image"]') or _first_css(
        document, "img.prod-image"
    )
    image_url = ""
    if image_node:
        attrib = getattr(image_node, "attrib", {}) or {}
        image_url = str(attrib.get("src", "")).strip()
    if image_url.startswith("//"):
        image_url = f"https:{image_url}"
    if not image_url:
        image_url = "Not Found"

    product_name_node = _first_css(document, "h1.product-title span")
    product_name = _clean_text(product_name_node) or "Not Found"

    review_node = _first_css(document, "#prod-review-nav-link")
    review_text = _clean_text(review_node)
    review_match = re.search(r"([\d,]+)", review_text)
    review_count = review_match.group(1) if review_match else "Not Found"

    shipping_fee_node = _first_css(
        document, ".price-shipping-fee-info-container > div:last-child"
    )
    shipping_fee_text = _clean_text(shipping_fee_node)
    shipping_fee_match = re.search(r"[\d][\d,]*", shipping_fee_text)
    shipping_fee = shipping_fee_match.group(0) if shipping_fee_match else "Not Found"

    rocket_node = _first_css(document, ".price-badge [data-badge-id]")
    rocket_delivery = (
        str((getattr(rocket_node, "attrib", {}) or {}).get("data-badge-id", "")).strip()
        or "Not Found"
    )

    rating_node = _first_css(document, "#prod-review-nav-link div[aria-label]")
    rating = (
        str((getattr(rating_node, "attrib", {}) or {}).get("aria-label", "")).strip()
        or "Not Found"
    )
    if rating == "Not Found":
        rating = _extract_json_ld_rating(document)

    price_nodes = document.css(
        ".price-amount, .total-price strong, .sales-price-amount"
    )
    prices: List[str] = []
    for node in price_nodes:
        text = _clean_text(node)
        match = re.search(r"[\d][\d,]*", text)
        if match:
            prices.append(match.group(0))
    prices = list(dict.fromkeys(prices))

    option_node = _first_css(
        document, ".option-picker-select div:not(.twc-font-bold)"
    ) or _first_css(document, ".option-table-v2 .toggle-header span")
    selected_option_node = _first_css(
        document, ".option-picker-select .twc-font-bold"
    ) or _first_css(
        document, ".option-table-list__option--selected .option-table-list__option-name"
    )

    return {
        "category": category,
        "image": image_url,
        "product_name": product_name,
        "review_count": review_count,
        "shipping_fee": shipping_fee,
        "rocket_delivery": rocket_delivery,
        "rating": rating,
        "prices": prices,
        "option_label": _clean_text(option_node) or "Not Found",
        "selected_option": _clean_text(selected_option_node) or "Not Found",
    }


def extract_product_fields_from_html(html: str) -> Dict[str, Any]:
    if not html:
        return {
            "category": "Not Found",
            "image": "Not Found",
            "product_name": "Not Found",
            "review_count": "Not Found",
            "shipping_fee": "Not Found",
            "rocket_delivery": "Not Found",
            "rating": "Not Found",
            "prices": [],
            "option_label": "Not Found",
            "selected_option": "Not Found",
        }
    document = Selector(html)
    return _extract_product_fields_from_doc(document)


def _extract_html_from_fetch_result(fetch_result: Any) -> str:
    candidates = (
        "html",
        "html_content",
        "raw_html",
        "content",
        "text",
        "body",
    )
    for attr_name in candidates:
        value = getattr(fetch_result, attr_name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="replace")
            except Exception:
                value = ""
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_title(fetch_result: Any) -> Optional[str]:
    title_value = getattr(fetch_result, "title", None)
    if callable(title_value):
        try:
            title_value = title_value()
        except Exception:
            return None
    if isinstance(title_value, str):
        return title_value
    return None


def _is_affiliate_env_configured() -> bool:
    return bool(
        os.getenv("ALI_APP_KEY", "").strip()
        and os.getenv("ALI_APP_SECRET", "").strip()
        and os.getenv("ALI_TRACKING_ID", "").strip()
    )


def _mask_sensitive_value(value: str, head: int = 3, tail: int = 3) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}...{text[-tail:]}"


def _require_affiliate_config() -> Dict[str, str]:
    app_key = os.getenv("ALI_APP_KEY", "").strip()
    app_secret = os.getenv("ALI_APP_SECRET", "").strip()
    tracking_id = os.getenv("ALI_TRACKING_ID", "").strip()

    missing: List[str] = []
    if not app_key:
        missing.append("ALI_APP_KEY")
    if not app_secret:
        missing.append("ALI_APP_SECRET")
    if not tracking_id:
        missing.append("ALI_TRACKING_ID")
    if missing:
        raise AffiliateConfigError(
            f"Missing affiliate configuration: {', '.join(missing)}"
        )

    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "tracking_id": tracking_id,
    }


def _build_top_timestamp_gmt8() -> str:
    gmt8 = timezone(timedelta(hours=8))
    return datetime.now(gmt8).strftime("%Y-%m-%d %H:%M:%S")


def _build_top_sign(params: Dict[str, Any], app_secret: str, sign_method: str) -> str:
    sortable_items = [
        (str(key), str(value))
        for key, value in params.items()
        if key != "sign"
        and value is not None
        and not isinstance(value, (bytes, bytearray))
    ]
    sortable_items.sort(key=lambda item: item[0])
    base = "".join(f"{key}{value}" for key, value in sortable_items)

    normalized_method = sign_method.strip().lower()
    if normalized_method == "hmac":
        digest = hmac.new(
            app_secret.encode("utf-8"),
            base.encode("utf-8"),
            hashlib.md5,
        ).hexdigest()
        return digest.upper()

    md5_payload = f"{app_secret}{base}{app_secret}"
    return hashlib.md5(md5_payload.encode("utf-8")).hexdigest().upper()


def _format_top_error_response(error_response: Dict[str, Any]) -> str:
    error_code = error_response.get("code")
    sub_code = error_response.get("sub_code")
    sub_msg = error_response.get("sub_msg")
    msg = error_response.get("msg")
    return f"code={error_code}, sub_code={sub_code}, msg={msg}, sub_msg={sub_msg}"


def _is_app_key_not_found_error(error_response: Dict[str, Any]) -> bool:
    combined = " ".join(
        str(value).lower()
        for value in (
            error_response.get("code"),
            error_response.get("sub_code"),
            error_response.get("msg"),
            error_response.get("sub_msg"),
        )
        if value is not None
    )
    return (
        "appkey-not-exists" in combined
        or "invalid app key" in combined
        or "app key not exists" in combined
    )


def _call_ali_top_api(
    method: str,
    service_params: Dict[str, Any],
    app_key: str,
    app_secret: str,
    timeout_s: int = DEFAULT_ALI_TOP_TIMEOUT_S,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "method": method,
        "app_key": app_key,
        "timestamp": _build_top_timestamp_gmt8(),
        "format": DEFAULT_ALI_TOP_FORMAT,
        "v": DEFAULT_ALI_TOP_VERSION,
        "sign_method": DEFAULT_ALI_TOP_SIGN_METHOD,
    }
    params.update(service_params)
    params["sign"] = _build_top_sign(
        params=params,
        app_secret=app_secret,
        sign_method=DEFAULT_ALI_TOP_SIGN_METHOD,
    )

    api_urls = [DEFAULT_ALI_TOP_API_URL]
    if (
        DEFAULT_ALI_TOP_FALLBACK_API_URL
        and DEFAULT_ALI_TOP_FALLBACK_API_URL not in api_urls
    ):
        api_urls.append(DEFAULT_ALI_TOP_FALLBACK_API_URL)

    encoded = urlencode(params).encode("utf-8")
    attempt_errors: List[str] = []
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}

    for index, api_url in enumerate(api_urls):
        request_obj = UrlRequest(
            api_url,
            data=encoded,
            headers=headers,
            method="POST",
        )

        try:
            with build_opener().open(request_obj, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            attempt_errors.append(
                f"url={api_url}, http_status={exc.code}, body={error_body[:400]}"
            )
            if index < len(api_urls) - 1:
                continue
            raise AliAffiliateApiError(
                "AliExpress API HTTPError: " + " | ".join(attempt_errors)
            ) from exc
        except URLError as exc:
            attempt_errors.append(f"url={api_url}, connection_error={str(exc)[:400]}")
            if index < len(api_urls) - 1:
                continue
            raise AliAffiliateApiError(
                "AliExpress API connection failed: " + " | ".join(attempt_errors)
            ) from exc

        try:
            payload = json.loads(raw)
        except Exception as exc:
            attempt_errors.append(f"url={api_url}, invalid_json={raw[:400]}")
            if index < len(api_urls) - 1:
                continue
            raise AliAffiliateApiError(
                "AliExpress API returned invalid JSON: " + " | ".join(attempt_errors)
            ) from exc

        if not isinstance(payload, dict):
            attempt_errors.append(
                f"url={api_url}, non_object_payload_type={type(payload).__name__}"
            )
            if index < len(api_urls) - 1:
                continue
            raise AliAffiliateApiError("AliExpress API response is not a JSON object")

        error_response = payload.get("error_response")
        if isinstance(error_response, dict):
            formatted_error = _format_top_error_response(error_response)
            attempt_errors.append(f"url={api_url}, {formatted_error}")
            if (
                _is_app_key_not_found_error(error_response)
                and index < len(api_urls) - 1
            ):
                _log_event(
                    logging.WARNING,
                    "affiliate.top_api.request.retry_fallback",
                    method=method,
                    failed_url=api_url,
                    fallback_url=api_urls[index + 1],
                    error=formatted_error,
                )
                continue
            raise AliAffiliateApiError(
                "AliExpress API error_response: " + " | ".join(attempt_errors)
            )

        return payload

    raise AliAffiliateApiError(
        "AliExpress API failed after trying all endpoints: "
        + " | ".join(attempt_errors)
    )


def _extract_top_method_response(
    payload: Dict[str, Any], method: str
) -> Dict[str, Any]:
    preferred_key = f"{method.replace('.', '_')}_response"
    response_obj = payload.get(preferred_key)
    if isinstance(response_obj, dict):
        return response_obj

    for key, value in payload.items():
        if (
            isinstance(key, str)
            and key.endswith("_response")
            and isinstance(value, dict)
        ):
            return value

    raise AliAffiliateApiError(
        f"AliExpress API response missing method response object for: {method}"
    )


def _to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_price_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
    if not match:
        return None
    numeric = match.group(1).replace(",", "")
    try:
        return float(numeric)
    except Exception:
        return None


def _extract_reference_price_krw(scrape_data: Dict[str, Any]) -> Optional[int]:
    prices = scrape_data.get("prices")
    if not isinstance(prices, list):
        return None

    parsed_prices: List[float] = []
    for item in prices:
        parsed = _extract_price_number(item)
        if parsed is not None:
            parsed_prices.append(parsed)

    if not parsed_prices:
        return None
    return int(round(min(parsed_prices)))


def _validate_affiliate_match_payload(
    payload: Optional[Dict[str, Any]],
) -> AffiliateMatchRequest:
    if payload is None:
        raise ValueError("Request body is required")
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")

    try:
        parsed = AffiliateMatchRequest.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid payload shape: {exc}") from exc

    if parsed.ok is not True:
        raise ValueError("payload.ok must be true")

    scrape_data = parsed.result.data
    if not isinstance(scrape_data, dict):
        raise ValueError("payload.result.data must be an object")

    product_name = scrape_data.get("product_name")
    image = scrape_data.get("image")
    prices = scrape_data.get("prices")

    if not isinstance(product_name, str):
        raise ValueError("payload.result.data.product_name must be a string")
    if not isinstance(image, str):
        raise ValueError("payload.result.data.image must be a string")
    if not isinstance(prices, list):
        raise ValueError("payload.result.data.prices must be an array")

    return parsed


def _normalize_match_text(value: str) -> str:
    lowered = value.lower()
    normalized = re.sub(r"[^\w]+", " ", lowered, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _meaningful_tokens(value: str) -> List[str]:
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "new",
        "of",
        "to",
        "in",
        "on",
        "by",
        "a",
        "an",
    }
    tokens = value.split()
    filtered: List[str] = []
    for token in tokens:
        if len(token) < 2:
            continue
        if token.isdigit():
            continue
        if token in stopwords:
            continue
        filtered.append(token)
    return filtered


def _titles_match(scrape_title: str, candidate_title: str) -> bool:
    normalized_scrape = _normalize_match_text(scrape_title)
    normalized_candidate = _normalize_match_text(candidate_title)
    if not normalized_scrape or not normalized_candidate:
        return False

    scrape_tokens = set(_meaningful_tokens(normalized_scrape))
    candidate_tokens = set(_meaningful_tokens(normalized_candidate))
    if len(scrape_tokens & candidate_tokens) >= 2:
        return True

    compact_scrape = normalized_scrape.replace(" ", "")
    compact_candidate = normalized_candidate.replace(" ", "")
    if compact_scrape and compact_candidate:
        if compact_scrape in compact_candidate or compact_candidate in compact_scrape:
            return True

    for token in scrape_tokens:
        if len(token) >= 4 and token in compact_candidate:
            return True
    for token in candidate_tokens:
        if len(token) >= 4 and token in compact_scrape:
            return True

    return False


def _extract_affiliate_products(
    response_payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    response_obj = _extract_top_method_response(
        payload=response_payload,
        method="aliexpress.affiliate.product.query",
    )
    resp_result = response_obj.get("resp_result", {})
    if not isinstance(resp_result, dict):
        return []

    resp_code = resp_result.get("resp_code")
    if resp_code not in {None, 200, "200"}:
        raise AliAffiliateApiError(
            f"Affiliate product query returned non-success resp_code: {resp_code}"
        )

    result = resp_result.get("result", {})
    if not isinstance(result, dict):
        return []

    products_obj = result.get("products")
    if isinstance(products_obj, dict):
        raw_products = _to_list(products_obj.get("product"))
    else:
        raw_products = _to_list(products_obj)

    normalized_products: List[Dict[str, Any]] = []
    for item in raw_products:
        if not isinstance(item, dict):
            continue

        title = str(item.get("product_title", "")).strip()
        detail_url = str(item.get("product_detail_url", "")).strip()
        product_id = str(item.get("product_id", "")).strip()
        image_url = str(item.get("product_main_image_url", "")).strip()
        price_value = (
            _extract_price_number(item.get("target_sale_price"))
            or _extract_price_number(item.get("target_app_sale_price"))
            or _extract_price_number(item.get("sale_price"))
            or _extract_price_number(item.get("app_sale_price"))
        )
        affiliate_url = ""
        for key in ("promotion_link", "promotion_url", "link"):
            candidate_value = item.get(key)
            if isinstance(candidate_value, str) and candidate_value.strip():
                affiliate_url = candidate_value.strip()
                break

        if not title or not detail_url or not product_id or price_value is None:
            continue

        normalized_products.append(
            {
                "product_id": product_id,
                "product_name": title,
                "image": image_url or "Not Found",
                "price_krw_float": float(price_value),
                "price_krw": int(round(float(price_value))),
                "product_url": detail_url,
                "affiliate_url": affiliate_url,
            }
        )

    return normalized_products


def _query_affiliate_products(
    keyword: str,
    max_sale_price_krw: Optional[int],
    app_key: str,
    app_secret: str,
    tracking_id: str,
    debug_request_id: Optional[str] = None,
    debug_trace_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    service_params: Dict[str, Any] = {
        "keywords": keyword,
        "category_ids": DEFAULT_ALI_CATEGORY_IDS,
        "tracking_id": tracking_id,
        "target_currency": DEFAULT_ALI_TARGET_CURRENCY,
        "target_language": DEFAULT_ALI_TARGET_LANGUAGE,
        "ship_to_country": DEFAULT_ALI_SHIP_TO_COUNTRY,
        "page_no": DEFAULT_ALI_PAGE_NO,
        "page_size": DEFAULT_ALI_PAGE_SIZE,
        "fields": (
            "product_id,product_title,product_main_image_url,product_detail_url,"
            "target_sale_price,target_app_sale_price,sale_price,app_sale_price,"
            "promotion_link"
        ),
    }
    if isinstance(max_sale_price_krw, int) and max_sale_price_krw > 0:
        service_params["max_sale_price"] = max_sale_price_krw

    payload = _call_ali_top_api(
        method="aliexpress.affiliate.product.query",
        service_params=service_params,
        app_key=app_key,
        app_secret=app_secret,
    )
    _log_event(
        logging.INFO,
        "affiliate.top_api.product_query.response",
        trace_id=debug_trace_id,
        request_id=debug_request_id,
        keyword=keyword,
        max_sale_price_krw=max_sale_price_krw,
        max_sale_price_param=service_params.get("max_sale_price"),
        payload=payload,
    )
    return _extract_affiliate_products(payload)


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_maybe_async(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return await _maybe_await(result)


async def _safe_call_maybe_async(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await _call_maybe_async(fn, *args, **kwargs)
    except Exception:
        return None


async def _read_body_excerpt_from_page(page: Any, max_len: int = 1500) -> str:
    body_locator = await _safe_call_maybe_async(page.locator, "body")
    if body_locator is None:
        return ""
    body_text = await _safe_call_maybe_async(body_locator.inner_text, timeout=2000)
    if not body_text:
        return ""
    normalized = " ".join(str(body_text).split())
    return normalized[:max_len]


async def _is_challenge_page(page: Any) -> Tuple[bool, Dict[str, str]]:
    title = await _safe_call_maybe_async(page.title) or ""
    url = getattr(page, "url", "") or ""
    excerpt = await _read_body_excerpt_from_page(page)
    haystack = f"{title}\n{url}\n{excerpt}".lower()

    markers = [
        "akamai",
        "access denied",
        "reference #",
        "forbidden",
        "verify you are human",
        "captcha",
        "bot",
        "robot",
    ]

    detected = any(marker in haystack for marker in markers)
    return detected, {
        "title": str(title),
        "url": str(url),
        "body_excerpt": excerpt,
    }


async def _simulate_virtual_mouse(page: Any, attempt_no: int) -> None:
    viewport = getattr(page, "viewport_size", None)
    width = 1280
    height = 720
    if isinstance(viewport, dict):
        width = int(viewport.get("width", width))
        height = int(viewport.get("height", height))

    center_x = max(120, min(width - 120, width // 2))
    center_y = max(120, min(height - 120, int(height * 0.38)))

    _log_event(
        logging.INFO,
        "worker.challenge.action",
        action="virtual_mouse",
        attempt_no=attempt_no,
        center_x=center_x,
        center_y=center_y,
    )
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        return

    await _call_maybe_async(mouse.move, center_x - 140, center_y - 80, steps=18)
    await _call_maybe_async(page.wait_for_timeout, 100)
    await _call_maybe_async(mouse.move, center_x, center_y, steps=16)
    await _call_maybe_async(page.wait_for_timeout, 80)
    await _call_maybe_async(mouse.down)
    await _call_maybe_async(page.wait_for_timeout, 80)
    await _call_maybe_async(mouse.up)
    await _call_maybe_async(page.wait_for_timeout, 180)
    await _call_maybe_async(mouse.move, center_x + 120, center_y + 60, steps=14)
    await _call_maybe_async(page.wait_for_timeout, 200)


async def _wait_for_product_or_challenge_bypass(
    page: Any,
    timeout_ms: int = DEFAULT_PRODUCT_READY_TIMEOUT_MS,
    product_selector: str = DEFAULT_PRODUCT_READY_SELECTOR,
    fallback_selector: str = DEFAULT_PRODUCT_FALLBACK_SELECTOR,
) -> Dict[str, Any]:
    try:
        await _call_maybe_async(
            page.wait_for_selector, product_selector, timeout=timeout_ms
        )
        return {
            "ready": True,
            "ready_selector": product_selector,
            "challenge_detected": False,
            "challenge_bypassed": False,
            "challenge_attempts": 0,
        }
    except Exception:
        pass

    challenge_detected, challenge_meta = await _is_challenge_page(page)
    if not challenge_detected:
        try:
            await _call_maybe_async(
                page.wait_for_selector, fallback_selector, timeout=5000
            )
            return {
                "ready": True,
                "ready_selector": fallback_selector,
                "challenge_detected": False,
                "challenge_bypassed": False,
                "challenge_attempts": 0,
            }
        except Exception:
            return {
                "ready": False,
                "ready_selector": None,
                "challenge_detected": False,
                "challenge_bypassed": False,
                "challenge_attempts": 0,
                "challenge_meta": challenge_meta,
            }

    return {
        "ready": False,
        "ready_selector": None,
        "challenge_detected": True,
        "challenge_bypassed": False,
        "challenge_attempts": 0,
        "challenge_meta": challenge_meta,
    }


async def _run_default_page_action(
    page: Any,
    fingerprint_profile: Optional[WorkerFingerprintProfile],
    stabilize_ms: int,
    timeout_ms: int,
) -> Dict[str, Any]:
    if fingerprint_profile is not None:
        await _apply_fingerprint_profile_to_page(page, fingerprint_profile)

    runtime_snapshot = await _collect_runtime_fingerprint_snapshot(page)

    if stabilize_ms > 0:
        await _call_maybe_async(page.wait_for_timeout, stabilize_ms)
    wait_meta = await _wait_for_product_or_challenge_bypass(
        page,
        timeout_ms=min(timeout_ms, DEFAULT_PRODUCT_READY_TIMEOUT_MS),
        product_selector=DEFAULT_PRODUCT_READY_SELECTOR,
        fallback_selector=DEFAULT_PRODUCT_FALLBACK_SELECTOR,
    )
    if stabilize_ms > 0:
        await _call_maybe_async(page.wait_for_timeout, stabilize_ms)
    wait_meta["runtime_snapshot"] = runtime_snapshot
    return wait_meta


async def _collect_runtime_fingerprint_snapshot(page: Any) -> Dict[str, Any]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"ok": False, "error": "page.evaluate is unavailable"}

    script = """
() => {
  const result = {
    ok: true,
    user_agent: navigator.userAgent || null,
    platform: navigator.platform || null,
    hardware_concurrency: navigator.hardwareConcurrency || null,
    device_memory: navigator.deviceMemory || null,
    language: navigator.language || null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
    webgl_vendor: null,
    webgl_renderer: null,
  };

  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) {
        result.webgl_vendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || null;
        result.webgl_renderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || null;
      }
    }
  } catch (_) {}

  return result;
}
"""

    try:
        raw = await _call_maybe_async(evaluate, script)
        if isinstance(raw, dict):
            return raw
        return {"ok": False, "error": "runtime snapshot has unexpected shape"}
    except Exception as exc:
        return {"ok": False, "error": f"runtime snapshot failed: {exc}"}


def _summarize_fpjs_components(components: Any) -> Dict[str, Any]:
    if not isinstance(components, dict):
        return {}

    def _component_value(key: str) -> Any:
        value = components.get(key)
        if isinstance(value, dict) and "value" in value:
            return value.get("value")
        return None

    def _shorten(text: Any, limit: int = 20) -> Any:
        if not isinstance(text, str):
            return text
        return text[:limit]

    canvas_value = _component_value("canvas")
    if isinstance(canvas_value, dict):
        canvas_value = dict(canvas_value)
        canvas_value["geometry"] = _shorten(canvas_value.get("geometry"))
        canvas_value["text"] = _shorten(canvas_value.get("text"))

    return {
        "user_agent": _component_value("userAgent"),
        "platform": _component_value("platform"),
        "languages": _component_value("languages"),
        "hardware_concurrency": _component_value("hardwareConcurrency"),
        "device_memory": _component_value("deviceMemory"),
        "timezone": _component_value("timezone"),
        "canvas": canvas_value,
        "webgl": _component_value("webGlBasics") or _component_value("webGl"),
    }


def _load_fpjs_bundle_text(
    script_url: str, timeout_s: int = DEFAULT_FPJS_BUNDLE_FETCH_TIMEOUT_S
) -> str:
    with _fpjs_bundle_cache_lock:
        cached = _fpjs_bundle_cache.get(script_url)
    if cached:
        return cached

    request_obj = UrlRequest(
        script_url,
        headers={"User-Agent": "best-price-fpjs-bundle/1.0", "Accept": "*/*"},
    )
    with build_opener().open(request_obj, timeout=timeout_s) as response:
        raw = response.read()

    text = raw.decode("utf-8", errors="replace")
    if "FingerprintJS" not in text:
        raise ValueError("FPJS bundle download succeeded but content validation failed")

    with _fpjs_bundle_cache_lock:
        _fpjs_bundle_cache[script_url] = text
    return text


async def _inject_fpjs_bundle_to_page(page: Any, bundle_text: str) -> None:
    add_init_script = getattr(page, "add_init_script", None)
    if callable(add_init_script):
        try:
            await _maybe_await(add_init_script(bundle_text))
        except Exception:
            pass

    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            await _maybe_await(evaluate(bundle_text))
        except Exception:
            pass


async def _collect_fpjs_via_openfpcdn(
    page: Any,
    script_url: str = DEFAULT_FPJS_CDN_URL,
    timeout_ms: int = DEFAULT_FPJS_TIMEOUT_MS,
) -> Dict[str, Any]:
    if timeout_ms < 1000:
        timeout_ms = 1000

    bundle_timeout_s = max(5, min(60, int(timeout_ms / 1000)))
    try:
        bundle_text = _load_fpjs_bundle_text(script_url, timeout_s=bundle_timeout_s)
    except Exception as exc:
        return {
            "ok": False,
            "visitor_id": None,
            "confidence_score": None,
            "confidence_comment": None,
            "version": None,
            "components": {},
            "components_summary": {},
            "error": f"fpjs bundle load failed: {exc}",
        }

    await _inject_fpjs_bundle_to_page(page, bundle_text)

    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {
            "ok": False,
            "visitor_id": None,
            "confidence_score": None,
            "confidence_comment": None,
            "version": None,
            "components": {},
            "components_summary": {},
            "error": "page.evaluate is unavailable",
        }

    script = """
async ({ timeoutMs }) => {
  const withTimeout = (promise, ms) => {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`fpjs timeout after ${ms}ms`)), ms);
      promise
        .then((value) => {
          clearTimeout(timer);
          resolve(value);
        })
        .catch((error) => {
          clearTimeout(timer);
          reject(error);
        });
    });
  };

  try {
    if (!window.FingerprintJS || typeof window.FingerprintJS.load !== "function") {
      throw new Error("FingerprintJS is not available on window");
    }

    const agent = await withTimeout(window.FingerprintJS.load({ monitoring: false }), timeoutMs);
    const result = await withTimeout(agent.get(), timeoutMs);

    return {
      ok: true,
      visitor_id: result?.visitorId || null,
      confidence_score: result?.confidence?.score ?? null,
      confidence_comment: result?.confidence?.comment ?? null,
      version: result?.version || null,
      components: result?.components || {},
      error: null,
    };
  } catch (error) {
    return {
      ok: false,
      visitor_id: null,
      confidence_score: null,
      confidence_comment: null,
      version: null,
      components: {},
      error: String(error && error.message ? error.message : error),
    };
  }
}
"""

    try:
        raw_result = await _call_maybe_async(
            evaluate,
            script,
            {
                "timeoutMs": int(timeout_ms),
            },
        )
    except Exception as exc:
        return {
            "ok": False,
            "visitor_id": None,
            "confidence_score": None,
            "confidence_comment": None,
            "version": None,
            "components": {},
            "components_summary": {},
            "error": f"page.evaluate failed: {exc}",
        }
    if not isinstance(raw_result, dict):
        return {
            "ok": False,
            "visitor_id": None,
            "confidence_score": None,
            "confidence_comment": None,
            "version": None,
            "components": {},
            "components_summary": {},
            "error": "Unexpected FPJS result shape",
        }

    components = raw_result.get("components")
    return {
        "ok": bool(raw_result.get("ok", False)),
        "visitor_id": raw_result.get("visitor_id"),
        "confidence_score": raw_result.get("confidence_score"),
        "confidence_comment": raw_result.get("confidence_comment"),
        "version": raw_result.get("version"),
        "components": components if isinstance(components, dict) else {},
        "components_summary": _summarize_fpjs_components(components),
        "error": raw_result.get("error"),
    }


def _build_worker_fingerprint_profiles(
    worker_count: int, seed: Optional[int] = None
) -> List[WorkerFingerprintProfile]:
    if worker_count < 1:
        return []

    rng = random.Random(
        seed if seed is not None else random.SystemRandom().randint(1, 10_000_000)
    )
    profile_sets = list(DEFAULT_FINGERPRINT_PROFILE_SETS)
    rng.shuffle(profile_sets)

    profiles: List[WorkerFingerprintProfile] = []
    for worker_id in range(worker_count):
        profile_set = profile_sets[worker_id % len(profile_sets)]
        base_noise = int(profile_set.get("canvas_noise_base", 7))
        noise = max(1, min(15, base_noise + rng.randint(-2, 2)))
        profiles.append(
            WorkerFingerprintProfile(
                profile_id=str(profile_set["profile_id"]),
                os_name=str(profile_set["os_name"]),
                browser_name=str(profile_set["browser_name"]),
                platform=str(profile_set["platform"]),
                user_agent=str(profile_set["user_agent"]),
                hardware_concurrency=int(profile_set["hardware_concurrency"]),
                device_memory=int(profile_set["device_memory"]),
                webgl_vendor=str(profile_set["webgl_vendor"]),
                webgl_renderer=str(profile_set["webgl_renderer"]),
                canvas_noise=noise,
            )
        )
    return profiles


def _build_fingerprint_injection_script(profile: WorkerFingerprintProfile) -> str:
    profile_payload = json.dumps(
        {
            "profileId": profile.profile_id,
            "osName": profile.os_name,
            "browserName": profile.browser_name,
            "platform": profile.platform,
            "userAgent": profile.user_agent,
            "hardwareConcurrency": profile.hardware_concurrency,
            "deviceMemory": profile.device_memory,
            "webglVendor": profile.webgl_vendor,
            "webglRenderer": profile.webgl_renderer,
            "canvasNoise": profile.canvas_noise,
        },
        ensure_ascii=True,
    )

    script = """
(() => {
  const profile = __PROFILE__;
  const patchFlag = "__best_price_fp_patched";
  if (window[patchFlag]) {
    return;
  }
  window[patchFlag] = true;

  const safeDefineGetter = (obj, key, getter) => {
    try {
      Object.defineProperty(obj, key, { get: getter, configurable: true });
    } catch (_) {}
  };

  const hc = Number(profile.hardwareConcurrency) || 8;
  const dm = Number(profile.deviceMemory) || 8;
  const platform = String(profile.platform || "Win32");
  const userAgent = String(profile.userAgent || "");
  safeDefineGetter(Navigator.prototype, "platform", () => platform);
  if (userAgent) {
    safeDefineGetter(Navigator.prototype, "userAgent", () => userAgent);
    safeDefineGetter(Navigator.prototype, "appVersion", () => userAgent);
  }
  safeDefineGetter(Navigator.prototype, "vendor", () => "Google Inc.");
  safeDefineGetter(Navigator.prototype, "language", () => "ko-KR");
  safeDefineGetter(Navigator.prototype, "languages", () => ["ko-KR", "ko", "en-US", "en"]);
  safeDefineGetter(Navigator.prototype, "hardwareConcurrency", () => hc);
  safeDefineGetter(Navigator.prototype, "deviceMemory", () => dm);

  const canvasNoise = Number(profile.canvasNoise) || 1;
  if (!HTMLCanvasElement.prototype.__best_price_canvas_patched) {
    HTMLCanvasElement.prototype.__best_price_canvas_patched = true;
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {
      try {
        const ctx = this.getContext("2d");
        if (ctx && this.width > 0 && this.height > 0) {
          const img = ctx.getImageData(0, 0, 1, 1);
          img.data[0] = (img.data[0] + canvasNoise) % 256;
          ctx.putImageData(img, 0, 0);
        }
      } catch (_) {}
      return originalToDataURL.apply(this, args);
    };
  }

  const patchWebGL = (proto) => {
    if (!proto || proto.__best_price_webgl_patched) {
      return;
    }
    proto.__best_price_webgl_patched = true;
    const originalGetParameter = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) {
        return profile.webglVendor;
      }
      if (parameter === 37446) {
        return profile.webglRenderer;
      }
      return originalGetParameter.apply(this, [parameter]);
    };
  };
  patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
  patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);
})();
"""
    return script.replace("__PROFILE__", profile_payload)


def _write_init_script_file(script: str) -> str:
    fd, file_path = tempfile.mkstemp(prefix="best-price-init-", suffix=".js")
    os.close(fd)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(script)
    return file_path


async def _apply_fingerprint_profile_to_page(
    page: Any, profile: WorkerFingerprintProfile
) -> None:
    script = _build_fingerprint_injection_script(profile)

    add_init_script = getattr(page, "add_init_script", None)
    if callable(add_init_script):
        try:
            await _maybe_await(add_init_script(script))
        except Exception:
            pass

    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            await _maybe_await(evaluate(script))
        except Exception:
            pass


def _header_value_case_insensitive(
    headers: Dict[str, Any], header_name: str
) -> Optional[str]:
    expected = header_name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == expected:
            return str(value) if value is not None else None
    return None


def _proxy_auth_header_value(proxy: ProxyConfig) -> Optional[str]:
    username = proxy.get("username", "")
    password = proxy.get("password", "")
    if not username:
        return None
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _probe_proxy_header_visibility(
    proxy: ProxyConfig,
    probe_url: str = "https://httpbin.org/anything",
    timeout_s: int = 20,
) -> Dict[str, Any]:
    parsed = urlparse(probe_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("probe_url must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("probe_url must include host")

    proxy_server = proxy.get("server", "").strip()
    if not proxy_server:
        raise ValueError("Proxy server is empty")

    handler = ProxyHandler({"http": proxy_server, "https": proxy_server})
    opener = build_opener(handler)

    request = UrlRequest(
        probe_url, headers={"User-Agent": "best-price-proxy-probe/1.0"}
    )
    proxy_auth = _proxy_auth_header_value(proxy)
    if proxy_auth:
        request.add_header("Proxy-Authorization", proxy_auth)

    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout_s) as response:
            status_code = getattr(response, "status", None)
            raw = response.read()
    except URLError as exc:
        raise ValueError(f"Probe request failed: {exc}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise ValueError("Probe response is not valid JSON") from exc

    body_headers = payload.get("headers", {})
    if not isinstance(body_headers, dict):
        body_headers = {}

    return {
        "probe_url": probe_url,
        "http_status": status_code,
        "elapsed_ms": elapsed_ms,
        "origin": payload.get("origin"),
        "x_forwarded_for": _header_value_case_insensitive(
            body_headers, "x-forwarded-for"
        ),
        "forwarded": _header_value_case_insensitive(body_headers, "forwarded"),
        "via": _header_value_case_insensitive(body_headers, "via"),
        "x_real_ip": _header_value_case_insensitive(body_headers, "x-real-ip"),
    }


def _compose_proxy_server(host: str, port: str) -> str:
    normalized_host = host.strip()
    normalized_port = port.strip()
    if normalized_host.startswith("http://") or normalized_host.startswith("https://"):
        return f"{normalized_host}:{normalized_port}"
    return f"http://{normalized_host}:{normalized_port}"


def _line_to_proxy(line: str) -> Optional[ProxyConfig]:
    value = line.strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) == 4:
        host, port, username, password = parts
        return {
            "server": _compose_proxy_server(host, port),
            "username": username,
            "password": password,
        }
    if len(parts) == 2:
        host, port = parts
        return {"server": _compose_proxy_server(host, port)}
    if value.startswith("http://") or value.startswith("https://"):
        return {"server": value}
    return None


def _normalize_proxy(proxy: Any) -> Optional[ProxyConfig]:
    if isinstance(proxy, str):
        return _line_to_proxy(proxy)

    if isinstance(proxy, dict):
        server = str(proxy.get("server", "")).strip()
        if not server:
            return None
        normalized: ProxyConfig = {"server": server}
        username = str(proxy.get("username", "")).strip()
        password = str(proxy.get("password", "")).strip()
        if username:
            normalized["username"] = username
        if password:
            normalized["password"] = password
        return normalized

    return None


def load_proxy_list() -> List[ProxyConfig]:
    raw_json = os.getenv("SCRAPLING_PROXIES_JSON", "").strip()
    raw_lines = os.getenv("SCRAPLING_PROXIES", "").strip()
    proxies: List[ProxyConfig] = []

    if raw_json:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, list):
            raise ValueError("SCRAPLING_PROXIES_JSON must be a JSON array")
        for item in parsed:
            normalized = _normalize_proxy(item)
            if normalized:
                proxies.append(normalized)
    elif raw_lines:
        for line in raw_lines.splitlines():
            normalized = _line_to_proxy(line)
            if normalized:
                proxies.append(normalized)
    else:
        for line in DEFAULT_PROXY_LINES:
            normalized = _line_to_proxy(line)
            if normalized:
                proxies.append(normalized)

    deduped: List[ProxyConfig] = []
    seen_servers = set()
    for proxy in proxies:
        server = proxy.get("server", "")
        if server and server not in seen_servers:
            seen_servers.add(server)
            deduped.append(proxy)

    if not deduped:
        raise ValueError("No valid proxies found")

    return deduped


def load_single_proxy_from_env() -> Optional[ProxyConfig]:
    raw = os.getenv("SCRAPER_PROXY", "").strip()
    if not raw:
        return None
    normalized = _normalize_proxy(raw)
    if not normalized:
        normalized = _line_to_proxy(raw)
    if not normalized:
        raise ValueError(
            "SCRAPER_PROXY is invalid. Use host:port:user:pass, host:port, or http(s)://host:port"
        )
    return normalized


class AsyncSessionScraper:
    def __init__(
        self,
        worker_count: int,
        tab_count: int,
        proxy: Optional[ProxyConfig],
        real_chrome: bool = DEFAULT_REAL_CHROME,
        headless: bool = True,
        network_idle: bool = True,
        default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
        request_jitter_min_ms: int = DEFAULT_REQUEST_JITTER_MIN_MS,
        request_jitter_max_ms: int = DEFAULT_REQUEST_JITTER_MAX_MS,
        fingerprint_profiles: Optional[List[WorkerFingerprintProfile]] = None,
        init_script_enabled: bool = DEFAULT_INIT_SCRIPT_ENABLED,
        user_data_dir: str = DEFAULT_USER_DATA_DIR,
    ):
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        if tab_count < 1:
            raise ValueError("tab_count must be >= 1")

        self.worker_count = 1
        self.tab_count = tab_count
        self.real_chrome = real_chrome
        self.headless = headless
        self.network_idle = network_idle
        self.default_timeout_ms = default_timeout_ms
        self.request_jitter_min_ms = max(0, request_jitter_min_ms)
        self.request_jitter_max_ms = max(0, request_jitter_max_ms)
        if self.request_jitter_max_ms < self.request_jitter_min_ms:
            self.request_jitter_max_ms = self.request_jitter_min_ms

        self.proxy = proxy
        self.proxies: List[ProxyConfig] = [proxy] if proxy else []
        self.init_script_enabled = bool(init_script_enabled)
        self.user_data_dir = user_data_dir.strip()

        if fingerprint_profiles:
            self.fingerprint_profiles = fingerprint_profiles
        else:
            self.fingerprint_profiles = _build_worker_fingerprint_profiles(
                self.tab_count
            )
        if not self.fingerprint_profiles:
            self.fingerprint_profiles = _build_worker_fingerprint_profiles(1)
        self.session_fingerprint_profile = self.fingerprint_profiles[0]
        self._session_init_script_path = ""
        if self.init_script_enabled:
            self._session_init_script_path = _write_init_script_file(
                _build_fingerprint_injection_script(self.session_fingerprint_profile)
            )

        self._started = False
        self._effective_real_chrome = self.real_chrome
        self._session_generation_counter = 0
        self._active_session_state: Optional[SessionRuntimeState] = None
        self._retired_session_states: Dict[int, SessionRuntimeState] = {}

        self._semaphore = asyncio.Semaphore(self.tab_count)
        self._slot_queue: "asyncio.Queue[int]" = asyncio.Queue()
        for slot_id in range(self.tab_count):
            self._slot_queue.put_nowait(slot_id)

        self._state_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._rotation_lock = asyncio.Lock()
        self._waiting_count = 0

    async def _update_waiting(self, delta: int) -> int:
        async with self._state_lock:
            self._waiting_count = max(0, self._waiting_count + delta)
            return self._waiting_count

    def queue_size(self) -> int:
        return self._waiting_count

    def _build_session_config(
        self, real_chrome: Optional[bool] = None
    ) -> Dict[str, Any]:
        session_config: Dict[str, Any] = {
            "headless": self.headless,
            "real_chrome": self.real_chrome if real_chrome is None else real_chrome,
            "timeout": self.default_timeout_ms,
            "max_pages": self.tab_count,
        }
        if self._session_init_script_path:
            session_config["init_script"] = self._session_init_script_path
        if self.user_data_dir:
            session_config["user_data_dir"] = self.user_data_dir
        if self.proxy is not None:
            session_config["proxy"] = self.proxy
        return session_config

    async def _create_session_context(
        self, prefer_real_chrome: Optional[bool] = None
    ) -> Tuple[Any, Any, bool]:
        StealthyFetcher.configure(adaptive=True)
        requested_real_chrome = (
            self.real_chrome if prefer_real_chrome is None else prefer_real_chrome
        )
        session_config = self._build_session_config(real_chrome=requested_real_chrome)

        try:
            session_ctx = AsyncStealthySession(**session_config)
            session = await session_ctx.__aenter__()
            return session_ctx, session, bool(session_config["real_chrome"])
        except Exception as exc:
            error_text = str(exc)
            chrome_missing = "Chromium distribution 'chrome' is not found" in error_text
            if requested_real_chrome and chrome_missing:
                _log_event(
                    logging.WARNING,
                    "pool.start.real_chrome_fallback",
                    reason="chrome_binary_missing",
                    fallback_real_chrome=False,
                    error=error_text,
                )
                fallback_config = self._build_session_config(real_chrome=False)
                session_ctx = AsyncStealthySession(**fallback_config)
                session = await session_ctx.__aenter__()
                return session_ctx, session, False
            raise

    async def _close_session_ctx(
        self, session_ctx: Any, reason: str, generation: Optional[int] = None
    ) -> None:
        try:
            await session_ctx.__aexit__(None, None, None)
        except Exception as exc:
            _log_event(
                logging.WARNING,
                "pool.shutdown.session_close_error",
                reason=reason,
                generation=generation,
                error=str(exc),
            )

    async def _acquire_active_session(self) -> Tuple[int, Any]:
        async with self._session_lock:
            if not self._started or self._active_session_state is None:
                raise RuntimeError("AsyncSessionScraper.start() must be called first")
            state = self._active_session_state
            state.in_flight += 1
            return state.generation, state.session

    async def _release_session_generation(self, generation: int) -> None:
        close_ctx: Optional[Any] = None
        async with self._session_lock:
            state: Optional[SessionRuntimeState] = None
            if (
                self._active_session_state is not None
                and self._active_session_state.generation == generation
            ):
                state = self._active_session_state
            else:
                state = self._retired_session_states.get(generation)

            if state is None:
                return

            state.in_flight = max(0, state.in_flight - 1)
            if state.retired and state.in_flight == 0:
                retired_state = self._retired_session_states.pop(generation, None)
                if retired_state is not None:
                    close_ctx = retired_state.session_ctx

        if close_ctx is not None:
            await self._close_session_ctx(
                close_ctx, reason="retired_session_drained", generation=generation
            )

    async def _rotate_active_session_from_generation(
        self,
        expected_generation: int,
        *,
        reason: str,
        request_id: str,
        tab_id: Optional[int],
        html_length: Optional[int] = None,
    ) -> int:
        async with self._rotation_lock:
            async with self._session_lock:
                if self._active_session_state is None:
                    raise RuntimeError("No active session is available for rotation")
                if self._active_session_state.generation != expected_generation:
                    return self._active_session_state.generation

            _log_event(
                logging.WARNING,
                "pool.session.rotate.begin",
                request_id=request_id,
                tab_id=tab_id,
                reason=reason,
                expected_generation=expected_generation,
                html_length=html_length,
            )

            new_ctx, new_session, new_real_chrome = await self._create_session_context(
                prefer_real_chrome=self._effective_real_chrome
            )

            close_new_ctx = False
            close_old_ctx: Optional[Any] = None
            current_generation = expected_generation

            async with self._session_lock:
                if self._active_session_state is None:
                    close_new_ctx = True
                elif self._active_session_state.generation != expected_generation:
                    close_new_ctx = True
                    current_generation = self._active_session_state.generation
                else:
                    old_state = self._active_session_state
                    old_state.retired = True
                    self._retired_session_states[old_state.generation] = old_state

                    self._session_generation_counter += 1
                    current_generation = self._session_generation_counter
                    self._active_session_state = SessionRuntimeState(
                        generation=current_generation,
                        session_ctx=new_ctx,
                        session=new_session,
                        in_flight=0,
                        retired=False,
                    )
                    self._effective_real_chrome = new_real_chrome

                    if old_state.in_flight == 0:
                        drained = self._retired_session_states.pop(
                            old_state.generation, None
                        )
                        if drained is not None:
                            close_old_ctx = drained.session_ctx

            if close_new_ctx:
                await self._close_session_ctx(
                    new_ctx, reason="rotation_new_session_discarded", generation=None
                )
                return current_generation

            _log_event(
                logging.INFO,
                "pool.session.rotate.complete",
                request_id=request_id,
                tab_id=tab_id,
                reason=reason,
                old_generation=expected_generation,
                new_generation=current_generation,
                html_length=html_length,
            )

            if close_old_ctx is not None:
                await self._close_session_ctx(
                    close_old_ctx,
                    reason="rotation_old_session_drained_immediately",
                    generation=expected_generation,
                )

            return current_generation

    async def start(self) -> None:
        if self._started:
            return

        session_ctx, session, effective_real_chrome = (
            await self._create_session_context(prefer_real_chrome=self.real_chrome)
        )
        async with self._session_lock:
            self._session_generation_counter = 1
            self._active_session_state = SessionRuntimeState(
                generation=1,
                session_ctx=session_ctx,
                session=session,
                in_flight=0,
                retired=False,
            )
            self._retired_session_states = {}
            self._effective_real_chrome = effective_real_chrome
            self._started = True

        _log_event(
            logging.INFO,
            "pool.start",
            workers=self.worker_count,
            tabs=self.tab_count,
            proxy_count=len(self.proxies),
            proxy_servers=[p.get("server", "") for p in self.proxies],
            real_chrome=self._effective_real_chrome,
            headless=self.headless,
            network_idle=self.network_idle,
            request_jitter_min_ms=self.request_jitter_min_ms,
            request_jitter_max_ms=self.request_jitter_max_ms,
            mode="async_stealthy_session",
            session_generation=1,
        )
        init_script_exists = False
        if self.init_script_enabled:
            init_script_exists = bool(
                self._session_init_script_path
                and os.path.exists(self._session_init_script_path)
                and os.path.isfile(self._session_init_script_path)
            )
        init_script_size = 0
        if init_script_exists:
            try:
                init_script_size = int(os.path.getsize(self._session_init_script_path))
            except Exception:
                init_script_size = 0
        _log_event(
            (
                logging.INFO
                if (not self.init_script_enabled or init_script_exists)
                else logging.WARNING
            ),
            "pool.init_script.status",
            init_script_enabled=self.init_script_enabled,
            init_script_path=self._session_init_script_path,
            init_script_exists=init_script_exists,
            init_script_size=init_script_size,
            profile_id=self.session_fingerprint_profile.profile_id,
            profile_platform=self.session_fingerprint_profile.platform,
            profile_hardware_concurrency=self.session_fingerprint_profile.hardware_concurrency,
            profile_webgl_vendor=self.session_fingerprint_profile.webgl_vendor,
            profile_webgl_renderer=self.session_fingerprint_profile.webgl_renderer,
        )
        _log_event(
            logging.INFO,
            "worker.ready",
            worker_id=0,
            mode="async_stealthy_session",
            tabs=self.tab_count,
        )

    async def shutdown(self) -> None:
        if not self._started:
            return

        contexts_to_close: List[Tuple[int, Any]] = []
        async with self._session_lock:
            if self._active_session_state is not None:
                contexts_to_close.append(
                    (
                        self._active_session_state.generation,
                        self._active_session_state.session_ctx,
                    )
                )
            for generation, state in list(self._retired_session_states.items()):
                contexts_to_close.append((generation, state.session_ctx))

            self._active_session_state = None
            self._retired_session_states = {}
            self._started = False

        for generation, session_ctx in contexts_to_close:
            await self._close_session_ctx(
                session_ctx, reason="pool_shutdown", generation=generation
            )

        _log_event(logging.INFO, "pool.shutdown")

    async def _session_fetch(
        self, session: Any, url: str, timeout_ms: int, page_action: Any
    ) -> Any:
        try:
            return await session.fetch(
                url,
                page_action=page_action,
                timeout=timeout_ms,
                network_idle=self.network_idle,
            )
        except TypeError:
            return await session.fetch(url, page_action=page_action)

    async def scrape(
        self,
        url: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        stabilize_ms: int = DEFAULT_STABILIZE_MS,
        use_proxy: bool = True,
        proxy_override: Optional[ProxyConfig] = None,
    ) -> Dict[str, Any]:
        if not self._started:
            raise RuntimeError("AsyncSessionScraper.start() must be called first")
        if proxy_override is not None:
            raise ValueError(
                "Per-request proxy override is not supported in shared AsyncStealthySession mode"
            )
        if self.proxy is not None and not use_proxy:
            raise ValueError(
                "use_proxy=false is not supported while SCRAPER_PROXY is configured"
            )

        request_id = str(uuid.uuid4())
        acquired = False
        tab_id: Optional[int] = None
        proxy_used = self.proxy.get("server", "unknown") if self.proxy else "DIRECT"

        await self._update_waiting(1)
        try:
            await self._semaphore.acquire()
            acquired = True
        finally:
            await self._update_waiting(-1)

        try:
            tab_id = await self._slot_queue.get()
            fingerprint_profile = self.session_fingerprint_profile

            jitter_ms = 0
            if self.request_jitter_max_ms > 0:
                jitter_ms = random.randint(
                    self.request_jitter_min_ms, self.request_jitter_max_ms
                )
                if jitter_ms > 0:
                    _log_event(
                        logging.INFO,
                        "worker.task.jitter",
                        worker_id=0,
                        tab_id=tab_id,
                        request_id=request_id,
                        jitter_ms=jitter_ms,
                    )
                    await asyncio.sleep(jitter_ms / 1000)

            started_at = time.perf_counter()
            _log_event(
                logging.INFO,
                "worker.task.start",
                worker_id=0,
                tab_id=tab_id,
                request_id=request_id,
                url=url,
                queue_size=self.queue_size(),
                jitter_ms=jitter_ms,
                proxy_used=proxy_used,
            )

            wait_meta: Dict[str, Any] = {}
            fetch_result: Any = None
            html = ""
            html_length = 0
            fetch_attempts = 0
            last_session_generation: Optional[int] = None

            for attempt in range(1, DEFAULT_SESSION_FETCH_ATTEMPTS + 1):
                fetch_attempts = attempt
                current_wait_meta: Dict[str, Any] = {}
                fetch_error: Optional[Exception] = None

                async def _page_action(page: Any) -> None:
                    current_wait_meta.update(
                        await _run_default_page_action(
                            page=page,
                            fingerprint_profile=fingerprint_profile,
                            stabilize_ms=stabilize_ms,
                            timeout_ms=timeout_ms,
                        )
                    )

                session_generation, session = await self._acquire_active_session()
                last_session_generation = session_generation
                try:
                    fetch_result = await self._session_fetch(
                        session=session,
                        url=url,
                        timeout_ms=timeout_ms,
                        page_action=_page_action,
                    )
                except Exception as exc:
                    fetch_error = exc
                finally:
                    await self._release_session_generation(session_generation)

                if fetch_error is not None:
                    if attempt >= DEFAULT_SESSION_FETCH_ATTEMPTS:
                        raise fetch_error
                    await self._rotate_active_session_from_generation(
                        session_generation,
                        reason="fetch_error",
                        request_id=request_id,
                        tab_id=tab_id,
                    )
                    _log_event(
                        logging.WARNING,
                        "worker.task.retry.session_rotated",
                        worker_id=0,
                        tab_id=tab_id,
                        request_id=request_id,
                        attempt=attempt,
                        max_attempts=DEFAULT_SESSION_FETCH_ATTEMPTS,
                        retry_reason="fetch_error",
                        error=str(fetch_error),
                    )
                    continue

                html = _extract_html_from_fetch_result(fetch_result)
                html_length = len(html) if html else 0
                wait_meta = current_wait_meta

                if html_length > DEFAULT_HTML_RETRY_THRESHOLD:
                    break

                if attempt < DEFAULT_SESSION_FETCH_ATTEMPTS:
                    await self._rotate_active_session_from_generation(
                        session_generation,
                        reason="small_html",
                        request_id=request_id,
                        tab_id=tab_id,
                        html_length=html_length,
                    )
                    _log_event(
                        logging.WARNING,
                        "worker.task.retry.session_rotated",
                        worker_id=0,
                        tab_id=tab_id,
                        request_id=request_id,
                        attempt=attempt,
                        max_attempts=DEFAULT_SESSION_FETCH_ATTEMPTS,
                        retry_reason="small_html",
                        html_length=html_length,
                        threshold=DEFAULT_HTML_RETRY_THRESHOLD,
                    )
                    await asyncio.sleep(0.2)

            if fetch_result is None:
                raise RuntimeError("Fetch failed before response object was created")
            wait_meta["fetch_attempts"] = fetch_attempts
            if last_session_generation is not None:
                wait_meta["session_generation"] = last_session_generation

            final_url = (
                getattr(fetch_result, "url", None)
                or getattr(fetch_result, "final_url", None)
                or url
            )
            page_title = _extract_title(fetch_result)

            if hasattr(fetch_result, "css"):
                data = _extract_product_fields_from_doc(fetch_result)
            else:
                data = extract_product_fields_from_html(html)

            result = {
                "request_id": request_id,
                "url": url,
                "status": getattr(fetch_result, "status", None),
                "worker_id": 0,
                "tab_id": tab_id,
                "replica_id": os.getenv("RAILWAY_REPLICA_ID", ""),
                "proxy_used": proxy_used,
                "final_url": final_url,
                "page_title": page_title,
                "html_length": html_length,
                "fingerprint_profile": {
                    "profile_id": fingerprint_profile.profile_id,
                    "os_name": fingerprint_profile.os_name,
                    "browser_name": fingerprint_profile.browser_name,
                    "platform": fingerprint_profile.platform,
                    "user_agent": fingerprint_profile.user_agent,
                    "hardware_concurrency": fingerprint_profile.hardware_concurrency,
                    "device_memory": fingerprint_profile.device_memory,
                    "webgl_vendor": fingerprint_profile.webgl_vendor,
                    "webgl_renderer": fingerprint_profile.webgl_renderer,
                    "canvas_noise": fingerprint_profile.canvas_noise,
                },
                "wait_meta": wait_meta,
                "data": data,
            }
            result["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)

            _log_event(
                logging.INFO,
                "worker.task.success",
                worker_id=0,
                tab_id=tab_id,
                request_id=request_id,
                status=result.get("status"),
                elapsed_ms=result.get("elapsed_ms"),
                proxy_used=proxy_used,
                html_length=result.get("html_length"),
                final_url=result.get("final_url"),
                jitter_ms=jitter_ms,
                ready=wait_meta.get("ready"),
                ready_selector=wait_meta.get("ready_selector"),
                challenge_detected=wait_meta.get("challenge_detected"),
                challenge_bypassed=wait_meta.get("challenge_bypassed"),
                fetch_attempts=fetch_attempts,
                runtime_platform=(wait_meta.get("runtime_snapshot") or {}).get(
                    "platform"
                ),
                runtime_hardware_concurrency=(
                    wait_meta.get("runtime_snapshot") or {}
                ).get("hardware_concurrency"),
                runtime_webgl_vendor=(wait_meta.get("runtime_snapshot") or {}).get(
                    "webgl_vendor"
                ),
                runtime_webgl_renderer=(wait_meta.get("runtime_snapshot") or {}).get(
                    "webgl_renderer"
                ),
            )
            return result
        except Exception as exc:
            _log_event(
                logging.ERROR,
                "worker.task.error",
                worker_id=0,
                tab_id=tab_id,
                request_id=request_id,
                proxy_used=proxy_used,
                error=str(exc),
            )
            raise
        finally:
            if tab_id is not None:
                self._slot_queue.put_nowait(tab_id)
            if acquired:
                self._semaphore.release()

    async def diagnose_fpjs(
        self,
        url: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        stabilize_ms: int = DEFAULT_STABILIZE_MS,
        use_proxy: bool = True,
        fpjs_script_url: str = DEFAULT_FPJS_CDN_URL,
        fpjs_timeout_ms: int = DEFAULT_FPJS_TIMEOUT_MS,
    ) -> Dict[str, Any]:
        if not self._started:
            raise RuntimeError("AsyncSessionScraper.start() must be called first")
        if self.proxy is not None and not use_proxy:
            raise ValueError(
                "use_proxy=false is not supported while SCRAPER_PROXY is configured"
            )

        request_id = str(uuid.uuid4())
        acquired = False
        tab_id: Optional[int] = None
        proxy_used = self.proxy.get("server", "unknown") if self.proxy else "DIRECT"

        await self._update_waiting(1)
        try:
            await self._semaphore.acquire()
            acquired = True
        finally:
            await self._update_waiting(-1)

        try:
            tab_id = await self._slot_queue.get()
            fingerprint_profile = self.session_fingerprint_profile

            jitter_ms = 0
            if self.request_jitter_max_ms > 0:
                jitter_ms = random.randint(
                    self.request_jitter_min_ms, self.request_jitter_max_ms
                )
                if jitter_ms > 0:
                    await asyncio.sleep(jitter_ms / 1000)

            started_at = time.perf_counter()
            current_wait_meta: Dict[str, Any] = {}
            current_fpjs: Dict[str, Any] = {}

            async def _page_action(page: Any) -> None:
                current_wait_meta.update(
                    await _run_default_page_action(
                        page=page,
                        fingerprint_profile=fingerprint_profile,
                        stabilize_ms=stabilize_ms,
                        timeout_ms=timeout_ms,
                    )
                )
                current_fpjs.update(
                    await _collect_fpjs_via_openfpcdn(
                        page=page,
                        script_url=fpjs_script_url,
                        timeout_ms=fpjs_timeout_ms,
                    )
                )

            session_generation, session = await self._acquire_active_session()
            try:
                fetch_result = await self._session_fetch(
                    session=session,
                    url=url,
                    timeout_ms=timeout_ms,
                    page_action=_page_action,
                )
            finally:
                await self._release_session_generation(session_generation)

            html = _extract_html_from_fetch_result(fetch_result)
            html_length = len(html) if html else 0
            wait_meta = dict(current_wait_meta)
            wait_meta["session_generation"] = session_generation

            final_url = (
                getattr(fetch_result, "url", None)
                or getattr(fetch_result, "final_url", None)
                or url
            )
            page_title = _extract_title(fetch_result)

            result = {
                "request_id": request_id,
                "url": url,
                "status": getattr(fetch_result, "status", None),
                "worker_id": 0,
                "tab_id": tab_id,
                "replica_id": os.getenv("RAILWAY_REPLICA_ID", ""),
                "proxy_used": proxy_used,
                "final_url": final_url,
                "page_title": page_title,
                "html_length": html_length,
                "fingerprint_profile": {
                    "profile_id": fingerprint_profile.profile_id,
                    "os_name": fingerprint_profile.os_name,
                    "browser_name": fingerprint_profile.browser_name,
                    "platform": fingerprint_profile.platform,
                    "user_agent": fingerprint_profile.user_agent,
                    "hardware_concurrency": fingerprint_profile.hardware_concurrency,
                    "device_memory": fingerprint_profile.device_memory,
                    "webgl_vendor": fingerprint_profile.webgl_vendor,
                    "webgl_renderer": fingerprint_profile.webgl_renderer,
                    "canvas_noise": fingerprint_profile.canvas_noise,
                },
                "wait_meta": wait_meta,
                "fpjs": {
                    "source": "openfpcdn",
                    "script_url": fpjs_script_url,
                    "timeout_ms": fpjs_timeout_ms,
                    **current_fpjs,
                },
            }
            result["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)

            _log_event(
                logging.INFO,
                "debug.fpjs.worker.success",
                worker_id=0,
                tab_id=tab_id,
                request_id=request_id,
                elapsed_ms=result.get("elapsed_ms"),
                status=result.get("status"),
                final_url=result.get("final_url"),
                html_length=result.get("html_length"),
                fpjs_ok=result.get("fpjs", {}).get("ok"),
                visitor_id=result.get("fpjs", {}).get("visitor_id"),
                jitter_ms=jitter_ms,
                runtime_platform=(result.get("wait_meta") or {})
                .get("runtime_snapshot", {})
                .get("platform"),
                runtime_hardware_concurrency=(result.get("wait_meta") or {})
                .get("runtime_snapshot", {})
                .get("hardware_concurrency"),
                runtime_webgl_vendor=(result.get("wait_meta") or {})
                .get("runtime_snapshot", {})
                .get("webgl_vendor"),
                runtime_webgl_renderer=(result.get("wait_meta") or {})
                .get("runtime_snapshot", {})
                .get("webgl_renderer"),
            )
            return result
        except Exception as exc:
            _log_event(
                logging.ERROR,
                "debug.fpjs.worker.error",
                worker_id=0,
                tab_id=tab_id,
                request_id=request_id,
                proxy_used=proxy_used,
                error=str(exc),
            )
            raise
        finally:
            if tab_id is not None:
                self._slot_queue.put_nowait(tab_id)
            if acquired:
                self._semaphore.release()


def _to_bool(value: str, default: bool) -> bool:
    parsed = value.strip().lower()
    if parsed in {"1", "true", "yes", "y", "on"}:
        return True
    if parsed in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _split_csv_env(name: str) -> List[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _clerk_auth_enabled() -> bool:
    default_enabled = bool(
        os.getenv("CLERK_JWKS_URL", "").strip()
        or os.getenv("CLERK_FRONTEND_API_URL", "").strip()
    )
    return _to_bool(
        os.getenv("CLERK_AUTH_ENABLED", "1" if default_enabled else "0"),
        default_enabled,
    )


def _clerk_jwks_url() -> str:
    configured = os.getenv("CLERK_JWKS_URL", "").strip()
    if configured:
        return configured

    frontend_api_url = os.getenv("CLERK_FRONTEND_API_URL", "").strip().rstrip("/")
    if frontend_api_url:
        return f"{frontend_api_url}/.well-known/jwks.json"

    raise RuntimeError("CLERK_JWKS_URL or CLERK_FRONTEND_API_URL is required")


def _get_clerk_jwks_client() -> PyJWKClient:
    global _clerk_jwks_client
    if _clerk_jwks_client is None:
        _clerk_jwks_client = PyJWKClient(_clerk_jwks_url())
    return _clerk_jwks_client


def _extract_clerk_token(request: Request) -> Optional[str]:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get("__session")


def _verify_clerk_request(request: Request) -> Dict[str, Any]:
    token = _extract_clerk_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Clerk session token")

    try:
        signing_key = _get_clerk_jwks_client().get_signing_key_from_jwt(token)
        decode_kwargs: Dict[str, Any] = {
            "algorithms": ["RS256"],
            "options": {"verify_aud": False},
        }
        issuer = os.getenv("CLERK_ISSUER", "").strip()
        if issuer:
            decode_kwargs["issuer"] = issuer
        claims = jwt_decode(token, signing_key.key, **decode_kwargs)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid Clerk session token") from exc

    authorized_parties = _split_csv_env("CLERK_AUTHORIZED_PARTIES")
    azp = claims.get("azp")
    if azp and authorized_parties and azp not in authorized_parties:
        raise HTTPException(status_code=401, detail="Invalid Clerk authorized party")
    if claims.get("sts") == "pending":
        raise HTTPException(status_code=403, detail="Clerk session is pending")
    return claims


def build_pool_from_env() -> AsyncSessionScraper:
    requested_workers = int(os.getenv("SCRAPER_WORKERS", str(DEFAULT_WORKERS)))
    tabs = int(os.getenv("SCRAPER_TABS", str(DEFAULT_TABS)))
    if tabs < 1:
        tabs = 1

    headless = _to_bool(os.getenv("SCRAPER_HEADLESS", "0"), False)
    real_chrome = DEFAULT_REAL_CHROME
    network_idle = _to_bool(os.getenv("SCRAPER_NETWORK_IDLE", "1"), True)
    timeout_ms = int(os.getenv("SCRAPER_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    jitter_min_ms = int(
        os.getenv("SCRAPER_REQUEST_JITTER_MIN_MS", str(DEFAULT_REQUEST_JITTER_MIN_MS))
    )
    jitter_max_ms = int(
        os.getenv("SCRAPER_REQUEST_JITTER_MAX_MS", str(DEFAULT_REQUEST_JITTER_MAX_MS))
    )
    init_script_enabled = _to_bool(
        os.getenv(
            "SCRAPER_INIT_SCRIPT_ENABLED",
            "1" if DEFAULT_INIT_SCRIPT_ENABLED else "0",
        ),
        DEFAULT_INIT_SCRIPT_ENABLED,
    )
    user_data_dir = os.getenv("SCRAPER_USER_DATA_DIR", DEFAULT_USER_DATA_DIR).strip()
    verify_mode = _to_bool(os.getenv("SCRAPER_VERIFY_MODE", "0"), False)
    fixed_proxy_raw = os.getenv("SCRAPER_FIXED_PROXY", "").strip()
    fixed_proxy_index_raw = os.getenv("SCRAPER_FIXED_PROXY_INDEX", "").strip()
    fingerprint_seed_raw = os.getenv("SCRAPER_FINGERPRINT_SEED", "").strip()

    if requested_workers != 1:
        _log_event(
            logging.WARNING,
            "pool.config.force_single_worker",
            requested_workers=requested_workers,
            effective_workers=1,
            tabs=tabs,
        )
    if verify_mode:
        _log_event(
            logging.WARNING,
            "pool.verify_mode.ignored",
            reason="single shared AsyncStealthySession mode ignores SCRAPER_VERIFY_MODE",
        )
    if fixed_proxy_raw or fixed_proxy_index_raw:
        _log_event(
            logging.WARNING,
            "pool.fixed_proxy.ignored",
            reason="Use SCRAPER_PROXY for a single session-level proxy",
        )

    proxy = load_single_proxy_from_env()
    fingerprint_seed = int(fingerprint_seed_raw) if fingerprint_seed_raw else None
    fingerprint_profiles = _build_worker_fingerprint_profiles(
        tabs, seed=fingerprint_seed
    )

    _log_event(
        logging.INFO,
        "pool.config",
        workers=1,
        requested_workers=requested_workers,
        tabs=tabs,
        max_concurrency=tabs,
        proxy_count=1 if proxy else 0,
        proxy_servers=[proxy.get("server", "")] if proxy else [],
        timeout_ms=timeout_ms,
        real_chrome=real_chrome,
        headless=headless,
        network_idle=network_idle,
        request_jitter_min_ms=jitter_min_ms,
        request_jitter_max_ms=jitter_max_ms,
        init_script_enabled=init_script_enabled,
        user_data_dir_configured=bool(user_data_dir),
        user_data_dir=user_data_dir or None,
        fingerprint_profile_count=len(fingerprint_profiles),
        fingerprint_profile_mode="coherent_profile_set",
        url_normalization=True,
        mode="async_stealthy_session",
    )

    return AsyncSessionScraper(
        worker_count=1,
        tab_count=tabs,
        proxy=proxy,
        real_chrome=real_chrome,
        headless=headless,
        network_idle=network_idle,
        default_timeout_ms=timeout_ms,
        request_jitter_min_ms=jitter_min_ms,
        request_jitter_max_ms=jitter_max_ms,
        fingerprint_profiles=fingerprint_profiles,
        init_script_enabled=init_script_enabled,
        user_data_dir=user_data_dir,
    )


app = FastAPI(
    title="Scrapling Worker Pool API",
    description="AsyncStealthySession Pool + Single Worker + Tab-based Concurrency",
    version="1.3.0",
)
scraper_pool: Optional[AsyncSessionScraper] = None


@app.middleware("http")
async def clerk_auth_middleware(request: Request, call_next):
    if _clerk_auth_enabled() and request.url.path not in PUBLIC_PATHS:
        try:
            request.state.clerk_claims = _verify_clerk_request(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        except Exception as exc:
            _log_event(
                logging.ERROR,
                "clerk.auth.error",
                path=request.url.path,
                error=str(exc),
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Clerk authentication is not configured correctly"},
            )
    return await call_next(request)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    trace_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.trace_id = trace_id
    started_at = time.perf_counter()

    _log_event(
        logging.INFO,
        "http.request.start",
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
        client=request.client.host if request.client else None,
    )

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        _log_event(
            logging.ERROR,
            "http.request.unhandled_error",
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
            elapsed_ms=elapsed_ms,
            error=str(exc),
        )
        raise

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    level = logging.INFO if response.status_code < 400 else logging.WARNING
    _log_event(
        level,
        "http.request.end",
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        elapsed_ms=elapsed_ms,
    )
    response.headers["x-request-id"] = trace_id
    return response


@app.on_event("startup")
async def _startup() -> None:
    global scraper_pool
    scraper_pool = build_pool_from_env()
    await scraper_pool.start()
    _log_event(logging.INFO, "api.startup.complete")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global scraper_pool
    if scraper_pool is not None:
        await scraper_pool.shutdown()
        scraper_pool = None
    _log_event(logging.INFO, "api.shutdown.complete")


@app.get("/health")
def health() -> Dict[str, Any]:
    server_ip = _resolve_server_ip()
    if scraper_pool is None:
        return {"status": "starting", "server_ip": server_ip}
    return {
        "status": "ok",
        "server_ip": server_ip,
        "workers": scraper_pool.worker_count,
        "tabs": scraper_pool.tab_count,
        "max_concurrency": scraper_pool.tab_count,
        "queue_size": scraper_pool.queue_size(),
        "proxy_count": len(scraper_pool.proxies),
        "proxy_reuse": bool(scraper_pool.proxies)
        and scraper_pool.tab_count > len(scraper_pool.proxies),
        "proxy_configured": scraper_pool.proxy is not None,
        "aliexpress_affiliate_configured": _is_affiliate_env_configured(),
        "real_chrome": scraper_pool.real_chrome,
        "init_script_enabled": scraper_pool.init_script_enabled,
        "user_data_dir_configured": bool(scraper_pool.user_data_dir),
        "user_data_dir": scraper_pool.user_data_dir or None,
        "verify_mode": _to_bool(os.getenv("SCRAPER_VERIFY_MODE", "0"), False),
        "request_jitter_min_ms": scraper_pool.request_jitter_min_ms,
        "request_jitter_max_ms": scraper_pool.request_jitter_max_ms,
        "proxy_servers": [proxy.get("server", "") for proxy in scraper_pool.proxies],
        "fingerprint_profiles": [
            {
                "profile_id": profile.profile_id,
                "os_name": profile.os_name,
                "browser_name": profile.browser_name,
                "platform": profile.platform,
                "hardware_concurrency": profile.hardware_concurrency,
                "device_memory": profile.device_memory,
                "webgl_vendor": profile.webgl_vendor,
                "webgl_renderer": profile.webgl_renderer,
                "canvas_noise": profile.canvas_noise,
            }
            for profile in scraper_pool.fingerprint_profiles
        ],
    }


@app.get("/debug/proxy-headers")
def debug_proxy_headers(
    request: Request,
    proxy_index: int = 0,
    probe_url: str = "https://httpbin.org/anything",
    timeout_s: int = 20,
) -> Dict[str, Any]:
    if scraper_pool is None:
        raise HTTPException(status_code=503, detail="Scraper pool is not ready")
    if timeout_s < 1 or timeout_s > 60:
        raise HTTPException(
            status_code=400, detail="timeout_s must be between 1 and 60"
        )
    if scraper_pool.proxy is None:
        raise HTTPException(status_code=400, detail="SCRAPER_PROXY is not configured")
    if proxy_index != 0:
        raise HTTPException(status_code=400, detail="Only proxy_index=0 is valid")

    trace_id = getattr(request.state, "trace_id", None)
    proxy = scraper_pool.proxy
    proxy_server = proxy.get("server", "")

    try:
        probe_result = _probe_proxy_header_visibility(
            proxy=proxy,
            probe_url=probe_url,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        _log_event(
            logging.ERROR,
            "debug.proxy_headers.error",
            trace_id=trace_id,
            proxy_index=proxy_index,
            proxy_server=proxy_server,
            probe_url=probe_url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502, detail=f"Proxy probe failed: {exc}"
        ) from exc

    _log_event(
        logging.INFO,
        "debug.proxy_headers.success",
        trace_id=trace_id,
        proxy_index=proxy_index,
        proxy_server=proxy_server,
        origin=probe_result.get("origin"),
        x_forwarded_for=probe_result.get("x_forwarded_for"),
        forwarded=probe_result.get("forwarded"),
        via=probe_result.get("via"),
        x_real_ip=probe_result.get("x_real_ip"),
    )

    return {
        "ok": True,
        "proxy_index": proxy_index,
        "proxy_server": proxy_server,
        "probe": probe_result,
    }


@app.post("/debug/fingerprint/fpjs", response_model=FpjsDebugSuccessResponse)
async def debug_fpjs(
    payload: FpjsDebugRequest, request: Request
) -> FpjsDebugSuccessResponse:
    if scraper_pool is None:
        raise HTTPException(status_code=503, detail="Scraper pool is not ready")

    trace_id = getattr(request.state, "trace_id", None)
    normalized_url = _normalize_product_url(payload.url)

    if not _is_valid_url(normalized_url):
        _log_event(
            logging.WARNING,
            "debug.fpjs.invalid_url",
            trace_id=trace_id,
            url=payload.url,
            normalized_url=normalized_url,
        )
        raise HTTPException(status_code=400, detail="Invalid url")

    if payload.fpjs_script_url.strip() != DEFAULT_FPJS_CDN_URL:
        _log_event(
            logging.WARNING,
            "debug.fpjs.script_url_overridden",
            trace_id=trace_id,
            requested_script_url=payload.fpjs_script_url,
            enforced_script_url=DEFAULT_FPJS_CDN_URL,
        )

    debug_request_id: Optional[str] = None

    try:
        _log_event(
            logging.INFO,
            "debug.fpjs.request.start",
            trace_id=trace_id,
            request_id=debug_request_id,
            url=payload.url,
            normalized_url=normalized_url,
            queue_size=scraper_pool.queue_size(),
            timeout_ms=payload.timeout_ms,
            stabilize_ms=payload.stabilize_ms,
            result_timeout_s=payload.result_timeout_s,
            use_proxy=payload.use_proxy,
            fpjs_script_url=DEFAULT_FPJS_CDN_URL,
            fpjs_timeout_ms=payload.fpjs_timeout_ms,
        )

        raw_result = await asyncio.wait_for(
            scraper_pool.diagnose_fpjs(
                url=normalized_url,
                timeout_ms=payload.timeout_ms,
                stabilize_ms=payload.stabilize_ms,
                use_proxy=payload.use_proxy,
                fpjs_script_url=DEFAULT_FPJS_CDN_URL,
                fpjs_timeout_ms=payload.fpjs_timeout_ms,
            ),
            timeout=payload.result_timeout_s,
        )

        debug_request_id = str(raw_result.get("request_id", ""))
        response_payload = FpjsDebugSuccessResponse(
            ok=True,
            result=FpjsDebugResult(**raw_result),
        )

        _log_event(
            logging.INFO,
            "debug.fpjs.request.success",
            trace_id=trace_id,
            request_id=response_payload.result.request_id,
            worker_id=response_payload.result.worker_id,
            tab_id=response_payload.result.tab_id,
            proxy_used=response_payload.result.proxy_used,
            status=response_payload.result.status,
            elapsed_ms=response_payload.result.elapsed_ms,
            final_url=response_payload.result.final_url,
            page_title=response_payload.result.page_title,
            html_length=response_payload.result.html_length,
            fpjs_ok=response_payload.result.fpjs.get("ok"),
            visitor_id=response_payload.result.fpjs.get("visitor_id"),
            fpjs_version=response_payload.result.fpjs.get("version"),
            ready=(
                response_payload.result.wait_meta.get("ready")
                if response_payload.result.wait_meta
                else None
            ),
            challenge_detected=(
                response_payload.result.wait_meta.get("challenge_detected")
                if response_payload.result.wait_meta
                else None
            ),
            runtime_platform=(response_payload.result.wait_meta or {})
            .get("runtime_snapshot", {})
            .get("platform"),
            runtime_hardware_concurrency=(response_payload.result.wait_meta or {})
            .get("runtime_snapshot", {})
            .get("hardware_concurrency"),
            runtime_webgl_vendor=(response_payload.result.wait_meta or {})
            .get("runtime_snapshot", {})
            .get("webgl_vendor"),
            runtime_webgl_renderer=(response_payload.result.wait_meta or {})
            .get("runtime_snapshot", {})
            .get("webgl_renderer"),
        )
        return response_payload
    except asyncio.TimeoutError as exc:
        _log_event(
            logging.ERROR,
            "debug.fpjs.request.timeout",
            trace_id=trace_id,
            request_id=debug_request_id,
            url=payload.url,
            normalized_url=normalized_url,
            result_timeout_s=payload.result_timeout_s,
        )
        raise HTTPException(status_code=504, detail="FPJS debug timed out") from exc
    except ValueError as exc:
        _log_event(
            logging.ERROR,
            "debug.fpjs.request.invalid_config",
            trace_id=trace_id,
            request_id=debug_request_id,
            url=payload.url,
            normalized_url=normalized_url,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValidationError as exc:
        _log_event(
            logging.ERROR,
            "debug.fpjs.request.invalid_result_shape",
            trace_id=trace_id,
            request_id=debug_request_id,
            url=payload.url,
            normalized_url=normalized_url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500, detail="Invalid internal response shape"
        ) from exc
    except Exception as exc:
        _log_event(
            logging.ERROR,
            "debug.fpjs.request.failed",
            trace_id=trace_id,
            request_id=debug_request_id,
            url=payload.url,
            normalized_url=normalized_url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500, detail=f"FPJS debug failed: {exc}"
        ) from exc


@app.get("/sample-product-urls")
def sample_product_urls() -> Dict[str, List[str]]:
    return {"urls": DEFAULT_PRODUCT_URLS}


@app.post("/affiliate/match", response_model=AffiliateMatchSuccessResponse)
async def affiliate_match(
    request: Request,
    payload: Optional[Dict[str, Any]] = None,
) -> AffiliateMatchSuccessResponse:
    trace_id = getattr(request.state, "trace_id", None)
    source = "client_scrape_payload"
    parsed_payload: Optional[AffiliateMatchRequest] = None
    scrape_request_id: Optional[str] = None

    try:
        parsed_payload = _validate_affiliate_match_payload(payload)
        scrape_request_id = parsed_payload.result.request_id
    except ValueError as exc:
        _log_event(
            logging.WARNING,
            "affiliate.match.request.invalid_payload",
            trace_id=trace_id,
            source=source,
            scrape_request_id=scrape_request_id,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        affiliate_config = _require_affiliate_config()
    except AffiliateConfigError as exc:
        _log_event(
            logging.ERROR,
            "affiliate.match.request.config_missing",
            trace_id=trace_id,
            source=source,
            scrape_request_id=scrape_request_id,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _log_event(
        logging.INFO,
        "affiliate.match.request.config_loaded",
        trace_id=trace_id,
        source=source,
        scrape_request_id=scrape_request_id,
        ali_app_key_masked=_mask_sensitive_value(
            affiliate_config["app_key"], head=3, tail=3
        ),
        ali_app_key_len=len(affiliate_config["app_key"]),
        ali_app_secret_masked=_mask_sensitive_value(
            affiliate_config["app_secret"], head=3, tail=3
        ),
        ali_app_secret_len=len(affiliate_config["app_secret"]),
        ali_tracking_id_masked=_mask_sensitive_value(
            affiliate_config["tracking_id"], head=3, tail=3
        ),
        ali_tracking_id_len=len(affiliate_config["tracking_id"]),
    )

    request_id = str(parsed_payload.result.request_id)
    scrape_result = parsed_payload.result
    scrape_data = scrape_result.data

    try:
        _log_event(
            logging.INFO,
            "affiliate.match.request.start",
            trace_id=trace_id,
            source=source,
            scrape_request_id=request_id,
            request_id=request_id,
            scrape_url=scrape_result.url,
        )

        scrape_product_name = str(scrape_data.get("product_name", "")).strip()
        scrape_image = str(scrape_data.get("image", "")).strip() or "Not Found"
        reference_price_krw = _extract_reference_price_krw(scrape_data)

        scrape_summary = AffiliateScrapeSummary(
            product_name=scrape_product_name or "Not Found",
            image=scrape_image,
            reference_price_krw=reference_price_krw,
        )

        if (
            not scrape_product_name
            or scrape_product_name == "Not Found"
            or reference_price_krw is None
        ):
            response_payload = AffiliateMatchSuccessResponse(
                ok=True,
                result=AffiliateMatchResult(
                    request_id=request_id,
                    matched=False,
                    reason=AFFILIATE_REASON_SCRAPE_DATA_INCOMPLETE,
                    scrape=scrape_summary,
                    affiliate=None,
                ),
            )
            _log_event(
                logging.INFO,
                "affiliate.match.request.no_match",
                trace_id=trace_id,
                source=source,
                scrape_request_id=request_id,
                request_id=request_id,
                reason=AFFILIATE_REASON_SCRAPE_DATA_INCOMPLETE,
                scrape_product_name=scrape_summary.product_name,
                reference_price_krw=scrape_summary.reference_price_krw,
            )
            return response_payload

        candidates = _query_affiliate_products(
            keyword=scrape_product_name,
            max_sale_price_krw=reference_price_krw,
            app_key=affiliate_config["app_key"],
            app_secret=affiliate_config["app_secret"],
            tracking_id=affiliate_config["tracking_id"],
            debug_request_id=request_id,
            debug_trace_id=trace_id,
        )
        if not candidates:
            response_payload = AffiliateMatchSuccessResponse(
                ok=True,
                result=AffiliateMatchResult(
                    request_id=request_id,
                    matched=False,
                    reason=AFFILIATE_REASON_AFFILIATE_API_EMPTY,
                    scrape=scrape_summary,
                    affiliate=None,
                ),
            )
            _log_event(
                logging.INFO,
                "affiliate.match.request.no_match",
                trace_id=trace_id,
                source=source,
                scrape_request_id=request_id,
                request_id=request_id,
                reason=AFFILIATE_REASON_AFFILIATE_API_EMPTY,
                scrape_product_name=scrape_summary.product_name,
                reference_price_krw=scrape_summary.reference_price_krw,
                candidate_count=0,
            )
            return response_payload

        link_ready_candidates = [
            candidate
            for candidate in candidates
            if str(candidate.get("affiliate_url", "")).strip()
        ]
        if not link_ready_candidates:
            response_payload = AffiliateMatchSuccessResponse(
                ok=True,
                result=AffiliateMatchResult(
                    request_id=request_id,
                    matched=False,
                    reason=AFFILIATE_REASON_AFFILIATE_API_EMPTY,
                    scrape=scrape_summary,
                    affiliate=None,
                ),
            )
            _log_event(
                logging.INFO,
                "affiliate.match.request.no_match",
                trace_id=trace_id,
                source=source,
                scrape_request_id=request_id,
                request_id=request_id,
                reason=AFFILIATE_REASON_AFFILIATE_API_EMPTY,
                scrape_product_name=scrape_summary.product_name,
                reference_price_krw=scrape_summary.reference_price_krw,
                candidate_count=len(candidates),
                promotion_link_candidate_count=0,
            )
            return response_payload

        selected = link_ready_candidates[0]
        affiliate_url = str(selected.get("affiliate_url", "")).strip()

        matched_product = AffiliateMatchedProduct(
            product_id=str(selected["product_id"]),
            product_name=str(selected["product_name"]),
            image=str(selected["image"]),
            price_krw=int(selected["price_krw"]),
            product_url=str(selected["product_url"]),
            affiliate_url=affiliate_url,
        )
        response_payload = AffiliateMatchSuccessResponse(
            ok=True,
            result=AffiliateMatchResult(
                request_id=request_id,
                matched=True,
                reason=AFFILIATE_REASON_MATCHED,
                scrape=scrape_summary,
                affiliate=matched_product,
            ),
        )
        _log_event(
            logging.INFO,
            "affiliate.match.request.success",
            trace_id=trace_id,
            source=source,
            scrape_request_id=request_id,
            request_id=request_id,
            reason=AFFILIATE_REASON_MATCHED,
            scrape_product_name=scrape_summary.product_name,
            reference_price_krw=scrape_summary.reference_price_krw,
            candidate_count=len(candidates),
            promotion_link_candidate_count=len(link_ready_candidates),
            matched_product_id=matched_product.product_id,
            matched_price_krw=matched_product.price_krw,
            matched_product_url=matched_product.product_url,
        )
        return response_payload

    except AliAffiliateApiError as exc:
        _log_event(
            logging.ERROR,
            "affiliate.match.request.failed",
            trace_id=trace_id,
            source=source,
            scrape_request_id=request_id,
            request_id=request_id,
            scrape_url=scrape_result.url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502, detail=f"Affiliate API failed: {exc}"
        ) from exc
    except ValidationError as exc:
        _log_event(
            logging.ERROR,
            "affiliate.match.request.invalid_result_shape",
            trace_id=trace_id,
            source=source,
            scrape_request_id=request_id,
            request_id=request_id,
            scrape_url=scrape_result.url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500, detail="Invalid internal response shape"
        ) from exc
    except Exception as exc:
        _log_event(
            logging.ERROR,
            "affiliate.match.request.failed",
            trace_id=trace_id,
            source=source,
            scrape_request_id=request_id,
            request_id=request_id,
            scrape_url=scrape_result.url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500, detail=f"Affiliate match failed: {exc}"
        ) from exc


@app.post("/scrape", response_model=ScrapeSuccessResponse)
async def scrape_product(
    payload: ScrapeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> ScrapeSuccessResponse:
    if scraper_pool is None:
        raise HTTPException(status_code=503, detail="Scraper pool is not ready")

    trace_id = getattr(request.state, "trace_id", None)

    normalized_product_url = _normalize_product_url(payload.product_url)

    if not _is_valid_url(normalized_product_url):
        _log_event(
            logging.WARNING,
            "scrape.request.invalid_url",
            trace_id=trace_id,
            url=payload.product_url,
            normalized_url=normalized_product_url,
        )
        raise HTTPException(status_code=400, detail="Invalid product_url")

    proxy_override: Optional[ProxyConfig] = None
    if payload.proxy:
        proxy_override = _normalize_proxy(payload.proxy)
        if not proxy_override:
            _log_event(
                logging.WARNING,
                "scrape.request.invalid_proxy",
                trace_id=trace_id,
                proxy=payload.proxy,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid proxy format. Use host:port:user:pass, host:port, "
                    "or http(s)://host:port"
                ),
            )
        _log_event(
            logging.WARNING,
            "scrape.request.proxy_override_unsupported",
            trace_id=trace_id,
            proxy=proxy_override.get("server"),
        )
        raise HTTPException(
            status_code=400,
            detail="Per-request proxy override is disabled. Set SCRAPER_PROXY environment variable.",
        )

    if scraper_pool.proxy is not None and not payload.use_proxy:
        _log_event(
            logging.WARNING,
            "scrape.request.use_proxy_mismatch",
            trace_id=trace_id,
            configured_proxy=scraper_pool.proxy.get("server"),
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "use_proxy=false is not supported while SCRAPER_PROXY is configured. "
                "Clear SCRAPER_PROXY to run in direct mode."
            ),
        )

    scrape_request_id: Optional[str] = None

    try:
        _log_event(
            logging.INFO,
            "scrape.request.queued",
            trace_id=trace_id,
            request_id=scrape_request_id,
            url=payload.product_url,
            normalized_url=normalized_product_url,
            queue_size=scraper_pool.queue_size(),
            timeout_ms=payload.timeout_ms,
            stabilize_ms=payload.stabilize_ms,
            use_proxy=payload.use_proxy,
            requested_proxy=proxy_override.get("server") if proxy_override else None,
        )

        raw_result = await asyncio.wait_for(
            scraper_pool.scrape(
                url=normalized_product_url,
                timeout_ms=payload.timeout_ms,
                stabilize_ms=payload.stabilize_ms,
                use_proxy=payload.use_proxy,
                proxy_override=proxy_override,
            ),
            timeout=payload.result_timeout_s,
        )
        scrape_request_id = str(raw_result.get("request_id", ""))
        response_payload = ScrapeSuccessResponse(
            ok=True,
            result=ScrapeResult(**raw_result),
        )

        scrape_data = (
            response_payload.result.data
            if isinstance(response_payload.result.data, dict)
            else {}
        )
        affiliate_info: Dict[str, Any] = {
            "status": "skipped",
            "reason": "not_attempted",
        }

        canonical_product_key: Optional[str] = None
        key_candidates = [
            response_payload.result.final_url,
            normalized_product_url,
            payload.product_url,
        ]
        for candidate in key_candidates:
            extracted = _extract_canonical_product_key(str(candidate or ""))
            if extracted:
                canonical_product_key = extracted
                break

        scrape_product_name = str(scrape_data.get("product_name", "")).strip()
        reference_price_krw = _extract_last_price_krw(scrape_data)

        if canonical_product_key:
            _supabase_upsert_scraped_product(
                canonical_product_key=canonical_product_key,
                scrape_data=scrape_data,
                source_request_id=response_payload.result.request_id,
                raw_payload=response_payload.model_dump(),
            )

        if not canonical_product_key:
            affiliate_info = {
                "status": "skipped",
                "reason": "missing_canonical_product_key",
            }
        elif not scrape_product_name or reference_price_krw is None:
            affiliate_info = {
                "status": "skipped",
                "reason": "scrape_data_incomplete",
                "product_name": scrape_product_name or None,
                "reference_price_krw": reference_price_krw,
            }
        else:
            try:
                _require_supabase_config()
                cache_response = _supabase_rpc(
                    "get_product_affiliate_cache",
                    {
                        "p_canonical_product_key": canonical_product_key,
                        "p_ttl_hours": AFFILIATE_CACHE_TTL_HOURS,
                    },
                )
                cache_row = _normalize_cache_row(cache_response)
                if cache_row is None:
                    raise RuntimeError("get_product_affiliate_cache returned empty response")

                cache_hit = bool(cache_row.get("cache_hit"))
                should_enqueue = bool(cache_row.get("should_enqueue"))
                job_id_value = str(cache_row.get("job_id") or "").strip() or None
                cache_status = str(cache_row.get("status") or "").strip()
                normalized_status = cache_status or ("done" if cache_hit else "pending")
                if normalized_status in {"running", "queued"}:
                    normalized_status = "pending"

                affiliate_info = {
                    "status": normalized_status,
                    "cache_hit": cache_hit,
                    "should_enqueue": should_enqueue,
                    "canonical_product_key": canonical_product_key,
                    "job_id": job_id_value,
                    "source_product_name": cache_row.get("source_product_name"),
                    "source_reference_price_krw": cache_row.get("source_reference_price_krw"),
                    "first_href": cache_row.get("first_href"),
                    "aliexpress_product_id": cache_row.get("aliexpress_product_id"),
                    "product_main_image_url": cache_row.get("product_main_image_url"),
                    "target_sale_price": cache_row.get("target_sale_price"),
                    "product_title": cache_row.get("product_title"),
                    "promotion_link": cache_row.get("promotion_link"),
                    "top_api_url": cache_row.get("top_api_url"),
                    "elapsed_ms": cache_row.get("elapsed_ms"),
                    "scrape_ok": cache_row.get("scrape_ok"),
                    "error": cache_row.get("error"),
                    "updated_at": cache_row.get("updated_at"),
                }

                if should_enqueue:
                    generated_job_id = str(uuid.uuid4())
                    enqueue_response = _supabase_rpc(
                        "enqueue_product_affiliate_match_job",
                        {
                            "p_canonical_product_key": canonical_product_key,
                            "p_source_product_name": scrape_product_name,
                            "p_source_reference_price_krw": reference_price_krw,
                            "p_job_id": generated_job_id,
                        },
                    )
                    enqueue_row = _normalize_cache_row(enqueue_response)
                    job_id = generated_job_id
                    if enqueue_row is not None:
                        db_job_id = str(enqueue_row.get("job_id") or "").strip()
                        if db_job_id:
                            job_id = db_job_id

                    background_tasks.add_task(
                        _run_affiliate_pipeline_sync,
                        canonical_product_key=canonical_product_key,
                        product_name=scrape_product_name,
                        reference_price_krw=reference_price_krw,
                        job_id=job_id,
                        trace_id=trace_id,
                        scrape_request_id=response_payload.result.request_id,
                    )
                    affiliate_info.update(
                        {
                            "status": "pending",
                            "cache_hit": False,
                            "should_enqueue": False,
                            "job_id": job_id,
                            "source_product_name": scrape_product_name,
                            "source_reference_price_krw": reference_price_krw,
                            "scrape_ok": False,
                            "error": None,
                        }
                    )
                    _log_event(
                        logging.INFO,
                        "affiliate.pipeline.enqueued",
                        trace_id=trace_id,
                        scrape_request_id=response_payload.result.request_id,
                        canonical_product_key=canonical_product_key,
                        job_id=job_id,
                    )

            except Exception as exc:
                affiliate_info = {
                    "status": "skipped",
                    "reason": "affiliate_pipeline_error",
                    "canonical_product_key": canonical_product_key,
                    "error": str(exc),
                }
                _log_event(
                    logging.WARNING,
                    "affiliate.pipeline.unavailable",
                    trace_id=trace_id,
                    scrape_request_id=response_payload.result.request_id,
                    canonical_product_key=canonical_product_key,
                    error=str(exc),
                )

        scrape_data["affiliate"] = affiliate_info
        response_payload.result.data = scrape_data

        _log_event(
            logging.INFO,
            "scrape.request.success",
            trace_id=trace_id,
            request_id=response_payload.result.request_id,
            worker_id=response_payload.result.worker_id,
            tab_id=response_payload.result.tab_id,
            replica_id=response_payload.result.replica_id,
            proxy_used=response_payload.result.proxy_used,
            status=response_payload.result.status,
            elapsed_ms=response_payload.result.elapsed_ms,
            final_url=response_payload.result.final_url,
            page_title=response_payload.result.page_title,
            html_length=response_payload.result.html_length,
            response_top_keys=list(response_payload.model_dump().keys()),
            result_keys=list(response_payload.result.model_dump().keys()),
            data_keys=list(response_payload.result.data.keys()),
            ready=(
                response_payload.result.wait_meta.get("ready")
                if response_payload.result.wait_meta
                else None
            ),
            ready_selector=(
                response_payload.result.wait_meta.get("ready_selector")
                if response_payload.result.wait_meta
                else None
            ),
            challenge_detected=(
                response_payload.result.wait_meta.get("challenge_detected")
                if response_payload.result.wait_meta
                else None
            ),
            affiliate_status=str(scrape_data.get("affiliate", {}).get("status")),
            affiliate_job_id=scrape_data.get("affiliate", {}).get("job_id"),
            affiliate_cache_hit=scrape_data.get("affiliate", {}).get("cache_hit"),
        )
        return response_payload

    except asyncio.TimeoutError as exc:
        _log_event(
            logging.ERROR,
            "scrape.request.timeout",
            trace_id=trace_id,
            request_id=scrape_request_id,
            url=payload.product_url,
            normalized_url=normalized_product_url,
            result_timeout_s=payload.result_timeout_s,
        )
        raise HTTPException(status_code=504, detail="Scrape job timed out") from exc
    except ValueError as exc:
        _log_event(
            logging.ERROR,
            "scrape.request.invalid_config",
            trace_id=trace_id,
            request_id=scrape_request_id,
            url=payload.product_url,
            normalized_url=normalized_product_url,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValidationError as exc:
        _log_event(
            logging.ERROR,
            "scrape.request.invalid_result_shape",
            trace_id=trace_id,
            request_id=scrape_request_id,
            url=payload.product_url,
            normalized_url=normalized_product_url,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500, detail="Invalid internal response shape"
        ) from exc
    except Exception as exc:
        _log_event(
            logging.ERROR,
            "scrape.request.failed",
            trace_id=trace_id,
            request_id=scrape_request_id,
            url=payload.product_url,
            normalized_url=normalized_product_url,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"Scrape failed: {exc}") from exc


@app.get("/")
def root() -> Dict[str, str]:
    return {"message": "POST product_url to /scrape or /affiliate/match"}


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", DEFAULT_HOST)
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    uvicorn.run(app, host=host, port=port)
