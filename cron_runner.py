from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

import boto3
import psycopg
import requests
from botocore.config import Config
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from meta_ads_crawler import (
    DEFAULT_PAGE_ID,
    build_ads_library_search_url,
    build_ads_library_url,
    crawl_meta_ads,
)


LOGGER = logging.getLogger("adfungus-cron")
MEDIA_TIMEOUT_S = 45
DEFAULT_MAX_MEDIA_BYTES = 250 * 1024 * 1024
MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,video/*,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ad_crawl_runs (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT,
    competitor_id BIGINT,
    page_id TEXT NOT NULL,
    library_url TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    ad_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE ad_crawl_runs ADD COLUMN IF NOT EXISTS workspace_id BIGINT;
ALTER TABLE ad_crawl_runs ADD COLUMN IF NOT EXISTS competitor_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_ad_crawl_runs_workspace ON ad_crawl_runs(workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ad_crawl_runs_competitor ON ad_crawl_runs(competitor_id, started_at DESC);

CREATE TABLE IF NOT EXISTS meta_ads (
    library_id TEXT PRIMARY KEY,
    first_seen_run_id BIGINT REFERENCES ad_crawl_runs(id) ON DELETE SET NULL,
    last_seen_run_id BIGINT REFERENCES ad_crawl_runs(id) ON DELETE SET NULL,
    page_id TEXT NOT NULL,
    brand TEXT,
    brand_logo_url TEXT,
    active BOOLEAN,
    platforms JSONB NOT NULL DEFAULT '[]'::jsonb,
    body TEXT,
    link_title TEXT,
    link_url TEXT,
    link_description TEXT,
    cta_text TEXT,
    cta_url TEXT,
    start_date_text TEXT,
    start_date DATE,
    ad_format TEXT,
    same_source_library_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    meta_library_url TEXT,
    competitor_id BIGINT,
    status TEXT NOT NULL DEFAULT 'running',
    missing_count INTEGER NOT NULL DEFAULT 0,
    ended_at TIMESTAMPTZ,
    raw_json JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS start_date DATE;
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS same_source_library_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS meta_library_url TEXT;
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS competitor_id BIGINT;
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'running';
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS missing_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_meta_ads_page_id ON meta_ads(page_id);
CREATE INDEX IF NOT EXISTS idx_meta_ads_brand ON meta_ads(brand);
CREATE INDEX IF NOT EXISTS idx_meta_ads_last_seen_at ON meta_ads(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_meta_ads_status ON meta_ads(status);
CREATE INDEX IF NOT EXISTS idx_meta_ads_start_date ON meta_ads(start_date);

CREATE TABLE IF NOT EXISTS app_users (
    id BIGSERIAL PRIMARY KEY,
    clerk_user_id TEXT NOT NULL UNIQUE,
    email TEXT,
    name TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspaces (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL DEFAULT 'Default Workspace',
    owner_user_id BIGINT REFERENCES app_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES app_users(id) ON DELETE SET NULL;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    clerk_user_id TEXT NOT NULL,
    user_id BIGINT REFERENCES app_users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'owner',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, clerk_user_id)
);

ALTER TABLE workspace_members ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES app_users(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_workspace_members_user_id ON workspace_members(user_id);

CREATE TABLE IF NOT EXISTS folders (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    parent_id BIGINT REFERENCES folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, parent_id, name)
);

ALTER TABLE folders ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS competitors (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    folder_id BIGINT REFERENCES folders(id) ON DELETE SET NULL,
    page_id TEXT NOT NULL,
    brand TEXT NOT NULL,
    brand_logo_url TEXT,
    representative_library_id TEXT,
    search_query TEXT,
    monitoring_enabled BOOLEAN NOT NULL DEFAULT true,
    created_by_user_id TEXT,
    created_by_app_user_id BIGINT REFERENCES app_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, page_id)
);

ALTER TABLE competitors ADD COLUMN IF NOT EXISTS created_by_app_user_id BIGINT REFERENCES app_users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_competitors_workspace ON competitors(workspace_id);
CREATE INDEX IF NOT EXISTS idx_competitors_monitoring ON competitors(monitoring_enabled);

CREATE TABLE IF NOT EXISTS saved_ads (
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    clerk_user_id TEXT,
    saved_by_user_id BIGINT REFERENCES app_users(id) ON DELETE SET NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, library_id)
);

ALTER TABLE saved_ads ADD COLUMN IF NOT EXISTS saved_by_user_id BIGINT REFERENCES app_users(id) ON DELETE SET NULL;
ALTER TABLE saved_ads ADD COLUMN IF NOT EXISTS note TEXT;

CREATE TABLE IF NOT EXISTS workspace_meta_ads (
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    competitor_id BIGINT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    first_seen_run_id BIGINT REFERENCES ad_crawl_runs(id) ON DELETE SET NULL,
    last_seen_run_id BIGINT REFERENCES ad_crawl_runs(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'running',
    missing_count INTEGER NOT NULL DEFAULT 0,
    ended_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, competitor_id, library_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_meta_ads_workspace ON workspace_meta_ads(workspace_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_workspace_meta_ads_competitor ON workspace_meta_ads(competitor_id, status);
CREATE INDEX IF NOT EXISTS idx_workspace_meta_ads_library_id ON workspace_meta_ads(library_id);

INSERT INTO workspace_meta_ads (
    workspace_id, competitor_id, library_id, first_seen_run_id, last_seen_run_id,
    status, missing_count, ended_at, first_seen_at, last_seen_at, updated_at
)
SELECT
    c.workspace_id, c.id, a.library_id, a.first_seen_run_id, a.last_seen_run_id,
    a.status, a.missing_count, a.ended_at, a.first_seen_at, a.last_seen_at, a.updated_at
FROM meta_ads a
JOIN competitors c ON c.id = a.competitor_id
ON CONFLICT (workspace_id, competitor_id, library_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS meta_ad_media (
    id BIGSERIAL PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    media_type TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
    url TEXT NOT NULL,
    source_url TEXT,
    storage_key TEXT,
    content_type TEXT,
    byte_size BIGINT,
    duration_seconds INTEGER,
    raw_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (library_id, media_type, url)
);

ALTER TABLE meta_ad_media ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE meta_ad_media ADD COLUMN IF NOT EXISTS storage_key TEXT;
ALTER TABLE meta_ad_media ADD COLUMN IF NOT EXISTS content_type TEXT;
ALTER TABLE meta_ad_media ADD COLUMN IF NOT EXISTS byte_size BIGINT;

CREATE INDEX IF NOT EXISTS idx_meta_ad_media_library_id ON meta_ad_media(library_id);
CREATE INDEX IF NOT EXISTS idx_meta_ad_media_type ON meta_ad_media(media_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_ad_media_source_url
    ON meta_ad_media(library_id, media_type, source_url)
    WHERE source_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS meta_ad_observations (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES ad_crawl_runs(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    active BOOLEAN,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_json JSONB NOT NULL,
    UNIQUE (run_id, library_id)
);

CREATE INDEX IF NOT EXISTS idx_meta_ad_observations_library_id ON meta_ad_observations(library_id);
CREATE INDEX IF NOT EXISTS idx_meta_ad_observations_run_id ON meta_ad_observations(run_id);
CREATE INDEX IF NOT EXISTS idx_meta_ad_observations_observed_at ON meta_ad_observations(observed_at DESC);
"""


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load_local_env() -> None:
    load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=False)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        LOGGER.warning("invalid integer env %s=%r; using %s", name, value, default)
        return default


def _r2_required_env() -> Dict[str, str]:
    values = {
        "endpoint_url": os.getenv("R2_S3_ENDPOINT", "").strip(),
        "access_key_id": os.getenv("R2_ACCESS_KEY_ID", "").strip(),
        "secret_access_key": os.getenv("R2_SECRET_ACCESS_KEY", "").strip(),
        "bucket": os.getenv("R2_BUCKET", "").strip(),
        "public_base_url": os.getenv("R2_PUBLIC_BASE_URL", "").strip().rstrip("/"),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError("missing R2 environment variables: " + ", ".join(missing))
    return values


def _r2_client() -> Any:
    config = _r2_required_env()
    return boto3.client(
        "s3",
        endpoint_url=config["endpoint_url"],
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _extension_from_media(source_url: str, content_type: str, media_type: str) -> str:
    path = urlparse(source_url).path.lower()
    suffix = Path(path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if guessed:
        return guessed
    return ".mp4" if media_type == "video" else ".jpg"


def _download_media(source_url: str) -> tuple[bytes, str]:
    max_bytes = max(_int_env("MAX_MEDIA_BYTES", DEFAULT_MAX_MEDIA_BYTES), 1)
    with requests.get(
        source_url,
        headers=MEDIA_HEADERS,
        timeout=MEDIA_TIMEOUT_S,
        stream=True,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "application/octet-stream")
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise RuntimeError(f"media too large: {content_length} bytes")

        chunks: List[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError(f"media exceeded MAX_MEDIA_BYTES={max_bytes}")
            chunks.append(chunk)
        return b"".join(chunks), content_type


def _upload_media_to_r2(
    client: Any,
    *,
    library_id: str,
    media_type: str,
    source_url: str,
) -> Dict[str, Any]:
    config = _r2_required_env()
    body, content_type = _download_media(source_url)
    digest = hashlib.sha256(body).hexdigest()
    ext = _extension_from_media(source_url, content_type, media_type)
    key = f"meta-ads/{library_id}/{media_type}s/{digest}{ext}"

    client.put_object(
        Bucket=config["bucket"],
        Key=key,
        Body=body,
        ContentType=content_type,
    )
    return {
        "url": f"{config['public_base_url']}/{key}",
        "source_url": source_url,
        "storage_key": key,
        "content_type": content_type,
        "byte_size": len(body),
        "sha256": digest,
    }


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def _library_id(ad: Dict[str, Any]) -> str:
    return str(ad.get("libraryID") or ad.get("library_id") or "").strip()


def _parse_start_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    
    # 쨌 (Middle Dot) 또는 다른 구분자가 있는 경우 앞부분만 취함
    text = re.split(r"[쨌\u00b7\u2022\u2219\xb7]", text)[0].strip()

    # 1. YYYY. MM. DD. or YYYY-MM-DD
    for pattern in (r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.", r"(\d{4})-(\d{1,2})-(\d{1,2})"):
        match = re.search(pattern, text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                pass
    
    # 2. English: May 14, 2026
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _meta_library_url(library_id: str) -> str:
    return f"https://www.facebook.com/ads/library/?id={library_id}"


def _ensure_default_workspace(conn: psycopg.Connection[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspaces (id, name)
            VALUES (1, 'Default Workspace')
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """
        )
        row = cur.fetchone()
        workspace_id = int(row["id"] if isinstance(row, dict) else row[0])
        return workspace_id


def _monitoring_competitors(conn: psycopg.Connection[Any]) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, workspace_id, page_id, brand
            FROM competitors
            WHERE monitoring_enabled = true
            ORDER BY created_at ASC
            """
        )
        return [
            {"id": int(row[0]), "workspace_id": int(row[1]), "page_id": str(row[2]), "brand": row[3]}
            for row in cur.fetchall()
        ]


def _create_run(
    conn: psycopg.Connection[Any],
    page_id: str,
    library_url: str,
    *,
    workspace_id: int | None = None,
    competitor_id: int | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ad_crawl_runs (workspace_id, competitor_id, page_id, library_url)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (workspace_id, competitor_id, page_id, library_url),
        )
        return int(cur.fetchone()[0])


def _finish_run(
    conn: psycopg.Connection[Any],
    run_id: int,
    *,
    status: str,
    ad_count: int = 0,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ad_crawl_runs
            SET finished_at = %s, status = %s, ad_count = %s, error = %s
            WHERE id = %s
            """,
            (datetime.now(timezone.utc), status, ad_count, error, run_id),
        )


def _iter_media(ad: Dict[str, Any]) -> Iterable[tuple[str, str, int | None, Dict[str, Any]]]:
    for image in ad.get("images") or []:
        url = str((image or {}).get("url") or "").strip()
        if url:
            yield "image", url, None, image
    for video in ad.get("videos") or []:
        url = str((video or {}).get("url") or "").strip()
        if not url:
            continue
        duration = video.get("duration")
        yield "video", url, int(duration) if isinstance(duration, int) else None, video


def _prepare_media_for_storage(ads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    client = _r2_client()
    
    # 1. Identify "Leaders" (the representative ad for each unique video/image source)
    unique_media: Dict[str, Dict[str, Any]] = {}  # source_url -> {library_id, media_type}
    
    for ad in ads:
        library_id = _library_id(ad)
        if not library_id:
            continue
            
        same_source = ad.get("sameSourceLibraryIDs") or []
        # If same_source exists and it's a list of dicts, check if we are the first one
        if same_source and isinstance(same_source[0], dict):
            leader_id = same_source[0]["id"]
            if library_id != leader_id:
                # This is a follower. Skip its media processing; we will clone it later.
                continue
        
        for collection_name, media_type in (("images", "image"), ("videos", "video")):
            for m in ad.get(collection_name) or []:
                url = str((m or {}).get("url") or "").strip()
                if url and url not in unique_media:
                    unique_media[url] = {"library_id": library_id, "media_type": media_type}
        
        logo_url = str(ad.get("brandLogo") or "").strip()
        if logo_url and "fbcdn.net" in logo_url and logo_url not in unique_media:
            unique_media[logo_url] = {"library_id": library_id, "media_type": "logo"}

    # 2. Upload unique media from leaders in parallel
    media_cache: Dict[str, Dict[str, Any]] = {}
    if unique_media:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        concurrency = _int_env("MEDIA_UPLOAD_CONCURRENCY", 10)
        LOGGER.info("Parallel media storage: uploading %s unique items with %s threads", len(unique_media), concurrency)
        
        def _worker_task(source_url: str, info: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            try:
                uploaded = _upload_media_to_r2(
                    client,
                    library_id=info["library_id"],
                    media_type=info["media_type"],
                    source_url=source_url,
                )
                return source_url, uploaded
            except Exception as exc:
                LOGGER.warning("media upload failed library_id=%s type=%s url=%s error=%s", 
                               info["library_id"], info["media_type"], source_url, exc)
                return source_url, None

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_url = {executor.submit(_worker_task, url, info): url for url, info in unique_media.items()}
            for future in as_completed(future_to_url):
                url, result = future.result()
                if result:
                    media_cache[url] = result

    # 3. Update ads with R2 URLs (primarily leaders, followers are skipped here)
    prepared: List[Dict[str, Any]] = []
    for ad in ads:
        library_id = _library_id(ad)
        if not library_id:
            prepared.append(ad)
            continue
            
        copied = dict(ad)
        for collection_name, media_type in (("images", "image"), ("videos", "video")):
            items = []
            for m in ad.get(collection_name) or []:
                raw = dict(m or {})
                source_url = str(raw.get("url") or "").strip()
                if source_url in media_cache:
                    raw.update(media_cache[source_url])
                items.append(raw)
            copied[collection_name] = items
            
        logo_url = str(copied.get("brandLogo") or "").strip()
        if logo_url in media_cache:
            copied["brandLogo"] = media_cache[logo_url]["url"]
            
        prepared.append(copied)

    return prepared


def _library_ids_from_ads(ads: List[Dict[str, Any]]) -> List[str]:
    library_ids: List[str] = []
    seen: set[str] = set()
    for ad in ads:
        library_id = _library_id(ad)
        if library_id and library_id not in seen:
            library_ids.append(library_id)
            seen.add(library_id)
    return library_ids


def _existing_library_ids(
    conn: psycopg.Connection[Any],
    library_ids: List[str],
) -> set[str]:
    if not library_ids:
        return set()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT library_id FROM meta_ads WHERE library_id = ANY(%s)",
            (library_ids,),
        )
        return {str(row[0]) for row in cur.fetchall()}


def _split_ads_by_existing(
    ads: List[Dict[str, Any]],
    existing_library_ids: set[str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    existing_ads: List[Dict[str, Any]] = []
    new_ads: List[Dict[str, Any]] = []
    for ad in ads:
        library_id = _library_id(ad)
        if library_id and library_id in existing_library_ids:
            existing_ads.append(ad)
        else:
            new_ads.append(ad)
    return existing_ads, new_ads


def _store_existing_ad_observations(
    conn: psycopg.Connection[Any],
    *,
    run_id: int,
    workspace_id: int | None = None,
    competitor_id: int | None = None,
    ads: List[Dict[str, Any]],
) -> tuple[int, List[str]]:
    LOGGER.info("refreshing %d existing ads for run_id=%s", len(ads), run_id)
    stored = 0
    stored_library_ids: List[str] = []
    with conn.cursor() as cur:
        for ad in ads:
            library_id = _library_id(ad)
            if not library_id:
                LOGGER.warning("skipping existing ad without libraryID: %s", ad)
                continue

            cur.execute(
                """
                UPDATE meta_ads
                SET last_seen_run_id = %s,
                    status = 'running',
                    missing_count = 0,
                    ended_at = NULL,
                    last_seen_at = now(),
                    updated_at = now()
                WHERE library_id = %s
                """,
                (run_id, library_id),
            )

            cur.execute(
                """
                INSERT INTO meta_ad_observations (
                    run_id, library_id, active, raw_json
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, library_id) DO NOTHING
                """,
                (run_id, library_id, ad.get("active"), Jsonb(ad)),
            )

            stored += 1
            stored_library_ids.append(library_id)

            if workspace_id is not None and competitor_id is not None:
                cur.execute(
                    """
                    INSERT INTO workspace_meta_ads (
                        workspace_id, competitor_id, library_id, first_seen_run_id, last_seen_run_id,
                        status, missing_count, ended_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'running', 0, NULL)
                    ON CONFLICT (workspace_id, competitor_id, library_id) DO UPDATE SET
                        last_seen_run_id = EXCLUDED.last_seen_run_id,
                        status = 'running',
                        missing_count = 0,
                        ended_at = NULL,
                        last_seen_at = now(),
                        updated_at = now()
                    """,
                    (workspace_id, competitor_id, library_id, run_id, run_id),
                )

    return stored, stored_library_ids


def _store_ads(
    conn: psycopg.Connection[Any],
    *,
    run_id: int,
    page_id: str,
    ads: List[Dict[str, Any]],
    workspace_id: int | None = None,
    competitor_id: int | None = None,
) -> tuple[int, List[str]]:
    from psycopg.types.json import Jsonb

    stored = 0
    stored_library_ids: List[str] = []
    with conn.cursor() as cur:
        for ad in ads:
            library_id = _library_id(ad)
            if not library_id:
                LOGGER.warning("skipping ad without libraryID: %s", ad)
                continue

            cur.execute(
                """
                INSERT INTO meta_ads (
                    library_id, first_seen_run_id, last_seen_run_id, page_id,
                    brand, brand_logo_url, active, platforms, body,
                    link_title, link_url, link_description, cta_text, cta_url,
                    start_date_text, start_date, ad_format, same_source_library_ids,
                    meta_library_url, competitor_id, status, missing_count, ended_at, raw_json
                )
                VALUES (
                    %(library_id)s, %(run_id)s, %(run_id)s, %(page_id)s,
                    %(brand)s, %(brand_logo_url)s, %(active)s, %(platforms)s, %(body)s,
                    %(link_title)s, %(link_url)s, %(link_description)s, %(cta_text)s, %(cta_url)s,
                    %(start_date_text)s, %(start_date)s, %(ad_format)s, %(same_source_library_ids)s,
                    %(meta_library_url)s, %(competitor_id)s, 'running', 0, NULL, %(raw_json)s
                )
                ON CONFLICT (library_id) DO UPDATE SET
                    last_seen_run_id = EXCLUDED.last_seen_run_id,
                    page_id = EXCLUDED.page_id,
                    brand = EXCLUDED.brand,
                    brand_logo_url = EXCLUDED.brand_logo_url,
                    active = EXCLUDED.active,
                    platforms = EXCLUDED.platforms,
                    body = EXCLUDED.body,
                    link_title = EXCLUDED.link_title,
                    link_url = EXCLUDED.link_url,
                    link_description = EXCLUDED.link_description,
                    cta_text = EXCLUDED.cta_text,
                    cta_url = EXCLUDED.cta_url,
                    start_date_text = EXCLUDED.start_date_text,
                    start_date = EXCLUDED.start_date,
                    ad_format = EXCLUDED.ad_format,
                    same_source_library_ids = EXCLUDED.same_source_library_ids,
                    meta_library_url = EXCLUDED.meta_library_url,
                    competitor_id = COALESCE(EXCLUDED.competitor_id, meta_ads.competitor_id),
                    status = 'running',
                    missing_count = 0,
                    ended_at = NULL,
                    raw_json = EXCLUDED.raw_json,
                    last_seen_at = now(),
                    updated_at = now()
                """,
                {
                    "library_id": library_id,
                    "run_id": run_id,
                    "page_id": page_id,
                    "brand": ad.get("brand"),
                    "brand_logo_url": ad.get("brandLogo"),
                    "active": ad.get("active"),
                    "platforms": Jsonb(ad.get("platforms") or []),
                    "body": ad.get("body"),
                    "link_title": ad.get("linkTitle"),
                    "link_url": ad.get("linkUrl"),
                    "link_description": ad.get("linkDescription"),
                    "cta_text": ad.get("ctaText"),
                    "cta_url": ad.get("ctaUrl"),
                    "start_date_text": ad.get("startDate"),
                    "start_date": _parse_start_date(ad.get("startDate")),
                    "ad_format": ad.get("format"),
                    "same_source_library_ids": Jsonb(ad.get("sameSourceLibraryIDs") or []),
                    "meta_library_url": _meta_library_url(library_id),
                    "competitor_id": competitor_id,
                    "raw_json": Jsonb(ad),
                },
            )

            cur.execute(
                """
                INSERT INTO meta_ad_observations (
                    run_id, library_id, active, raw_json
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, library_id) DO NOTHING
                """,
                (run_id, library_id, ad.get("active"), Jsonb(ad)),
            )

            for media_type, url, duration_seconds, raw in _iter_media(ad):
                cur.execute(
                    """
                    INSERT INTO meta_ad_media (
                        library_id, media_type, url, source_url, storage_key,
                        content_type, byte_size, duration_seconds, raw_json
                    )
                    VALUES (%(library_id)s, %(media_type)s, %(url)s, %(source_url)s, %(storage_key)s,
                            %(content_type)s, %(byte_size)s, %(duration_seconds)s, %(raw_json)s)
                    ON CONFLICT (library_id, media_type, url) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        storage_key = EXCLUDED.storage_key,
                        content_type = EXCLUDED.content_type,
                        byte_size = EXCLUDED.byte_size,
                        duration_seconds = EXCLUDED.duration_seconds,
                        raw_json = EXCLUDED.raw_json
                    """,
                    {
                        "library_id": library_id,
                        "media_type": media_type,
                        "url": url,
                        "source_url": raw.get("source_url"),
                        "storage_key": raw.get("storage_key"),
                        "content_type": raw.get("content_type"),
                        "byte_size": raw.get("byte_size"),
                        "duration_seconds": duration_seconds,
                        "raw_json": Jsonb(raw),
                    },
                )

            stored += 1
            stored_library_ids.append(library_id)

            if workspace_id is not None and competitor_id is not None:
                cur.execute(
                    """
                    INSERT INTO workspace_meta_ads (
                        workspace_id, competitor_id, library_id, first_seen_run_id, last_seen_run_id,
                        status, missing_count, ended_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'running', 0, NULL)
                    ON CONFLICT (workspace_id, competitor_id, library_id) DO UPDATE SET
                        last_seen_run_id = EXCLUDED.last_seen_run_id,
                        status = 'running',
                        missing_count = 0,
                        ended_at = NULL,
                        last_seen_at = now(),
                        updated_at = now()
                    """,
                    (workspace_id, competitor_id, library_id, run_id, run_id),
                )

    return stored, stored_library_ids


def _expand_versions_globally(database_url: str) -> None:
    """
    Final step of the Lean Pipeline: Clones results from Leaders to Followers for all ads.
    This creates rows for ad versions that were skipped during the main crawl.
    """
    LOGGER.info("--- Starting global version expansion (Fan-out) ---")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            # 1. Identify missing versions across all ads
            cur.execute("""
                WITH versions AS (
                    SELECT 
                        library_id as leader_id,
                        jsonb_array_elements(same_source_library_ids) as ver
                    FROM meta_ads
                    WHERE jsonb_array_length(same_source_library_ids) > 0
                ),
                to_create AS (
                    SELECT 
                        leader_id,
                        ver->>'id' as version_id,
                        ver->>'startDate' as start_date_text
                    FROM versions
                    WHERE ver->>'id' IS NOT NULL AND ver->>'id' <> leader_id
                )
                SELECT DISTINCT ON (version_id) 
                    leader_id, version_id, start_date_text
                FROM to_create
                WHERE NOT EXISTS (SELECT 1 FROM meta_ads WHERE library_id = version_id)
            """)
            missing = cur.fetchall()
            
            if not missing:
                LOGGER.info("No missing versions to expand.")
                return

            LOGGER.info(f"Expanding {len(missing)} missing versions from leaders...")
            
            for leader_id, version_id, start_date_text in missing:
                try:
                    # Clone meta_ads row
                    cur.execute("""
                        INSERT INTO meta_ads (
                            library_id, page_id, brand, brand_logo_url, active, platforms, body,
                            link_title, link_url, link_description, cta_text, cta_url,
                            start_date_text, start_date, ad_format, same_source_library_ids,
                            meta_library_url, competitor_id, status, raw_json,
                            first_seen_run_id, last_seen_run_id
                        )
                        SELECT 
                            %s, page_id, brand, brand_logo_url, active, platforms, body,
                            link_title, link_url, link_description, cta_text, cta_url,
                            %s, NULL, ad_format, same_source_library_ids,
                            NULL, competitor_id, status, raw_json,
                            first_seen_run_id, last_seen_run_id
                        FROM meta_ads WHERE library_id = %s
                        ON CONFLICT (library_id) DO NOTHING
                    """, (version_id, start_date_text, leader_id))
                    
                    # Clone meta_ad_media rows
                    cur.execute("""
                        INSERT INTO meta_ad_media (
                            library_id, media_type, url, source_url, storage_key, 
                            content_type, byte_size, duration_seconds, raw_json
                        )
                        SELECT 
                            %s, media_type, url, source_url, storage_key, 
                            content_type, byte_size, duration_seconds, raw_json
                        FROM meta_ad_media WHERE library_id = %s
                        ON CONFLICT DO NOTHING
                    """, (version_id, leader_id))
                    
                    # Clone video extractions (if exist)
                    cur.execute("""
                        INSERT INTO meta_ad_video_extractions (
                            media_id, library_id, model, prompt_version, status,
                            audio_transcript, screen_text, section_types,
                            hooking_audio, hooking_screen_text, hooking_visual_direction, hooking_categories,
                            body_audio, body_screen_text, closing_audio, closing_screen_text, closing_cta,
                            result_json, processed_at
                        )
                        SELECT 
                            m_new.id, %s, e.model, e.prompt_version, e.status,
                            e.audio_transcript, e.screen_text, e.section_types,
                            e.hooking_audio, e.hooking_screen_text, e.hooking_visual_direction, e.hooking_categories,
                            e.body_audio, e.body_screen_text, e.closing_audio, e.closing_screen_text, e.closing_cta,
                            e.result_json, e.processed_at
                        FROM meta_ad_video_extractions e
                        JOIN meta_ad_media m_old ON m_old.id = e.media_id
                        JOIN meta_ad_media m_new ON m_new.library_id = %s AND m_new.url = m_old.url
                        WHERE e.library_id = %s
                        ON CONFLICT (media_id) DO NOTHING
                    """, (version_id, version_id, leader_id))
                    
                    # Clone embeddings (if exist)
                    cur.execute("""
                        INSERT INTO meta_ad_embeddings (
                            library_id, embedding, similarity_sum, similarity_rank, 
                            similarity_run_id, similar_count, similar_library_ids
                        )
                        SELECT 
                            %s, embedding, similarity_sum, similarity_rank, 
                            similarity_run_id, similar_count, similar_library_ids
                        FROM meta_ad_embeddings WHERE library_id = %s
                        ON CONFLICT (library_id) DO NOTHING
                    """, (version_id, leader_id))

                    # Clone workspace associations
                    cur.execute("""
                        INSERT INTO workspace_meta_ads (
                            workspace_id, competitor_id, library_id, 
                            first_seen_run_id, last_seen_run_id, status
                        )
                        SELECT 
                            workspace_id, competitor_id, %s,
                            first_seen_run_id, last_seen_run_id, status
                        FROM workspace_meta_ads WHERE library_id = %s
                        ON CONFLICT DO NOTHING
                    """, (version_id, leader_id))

                except Exception:
                    LOGGER.exception(f"Failed to expand version {version_id} from leader {leader_id}")
            
            conn.commit()
            LOGGER.info("Global version expansion completed.")


def _mark_missing_ads(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: int | None,
    competitor_id: int | None,
    page_id: str,
    observed_library_ids: List[str],
) -> None:
    if workspace_id is None or competitor_id is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workspace_meta_ads wa
            SET missing_count = wa.missing_count + 1,
                status = CASE WHEN wa.missing_count + 1 >= 2 THEN 'ended' ELSE wa.status END,
                ended_at = CASE WHEN wa.missing_count + 1 >= 2 AND wa.ended_at IS NULL THEN now() ELSE wa.ended_at END,
                updated_at = now()
            WHERE wa.workspace_id = %s
              AND wa.competitor_id = %s
              AND wa.status = 'running'
              AND NOT (wa.library_id = ANY(%s))
              AND EXISTS (
                SELECT 1 FROM meta_ads a 
                WHERE a.library_id = wa.library_id 
                  AND a.page_id = %s
              )
            """,
            (workspace_id, competitor_id, observed_library_ids, page_id),
        )


def _process_videos_after_crawl(database_url: str, library_ids: List[str] | None = None) -> None:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        LOGGER.warning("video extraction skipped because GEMINI_API_KEY is missing")
        return

    from video_text_worker import process_pending_videos

    successes, total = process_pending_videos(
        database_url=database_url,
        library_ids=library_ids,
    )
    LOGGER.info(
        "video extraction after crawl processed %s/%s pending videos",
        successes,
        total,
    )


def _process_embeddings_after_crawl(database_url: str, library_ids: List[str] | None = None) -> None:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        LOGGER.warning("ad embedding skipped because GEMINI_API_KEY is missing")
        return

    from ad_embedding_worker import DEFAULT_TOP_K, process_ad_embeddings

    embedded, scored = process_ad_embeddings(
        database_url=database_url,
        top_k=max(_int_env("AD_EMBEDDING_TOP_K", DEFAULT_TOP_K), 1),
        library_ids=library_ids,
    )
    LOGGER.info("ad embedding after crawl embedded=%s scored=%s", embedded, scored)


async def _crawl() -> tuple[str, str, List[Dict[str, Any]]]:
    page_id = os.getenv("PAGE_ID", DEFAULT_PAGE_ID).strip() or DEFAULT_PAGE_ID
    library_url = build_ads_library_url(page_id)
    ads = await crawl_meta_ads(
        url=library_url,
        scroll_wait_ms=max(_int_env("SCROLL_WAIT_MS", 1200), 300),
        debug=os.getenv("DEBUG", "").lower() in {"1", "true", "yes"},
        stable_rounds=max(_int_env("STABLE_ROUNDS", 3), 1),
        stable_max_rounds=max(_int_env("STABLE_MAX_ROUNDS", 18), 1),
        limit=max(_int_env("LIMIT", 0), 0),
    )
    return page_id, library_url, ads


async def _crawl_page(page_id: str, brand_name: str = "") -> tuple[str, str, List[Dict[str, Any]]]:
    normalized_page_id = page_id.strip() or DEFAULT_PAGE_ID
    if brand_name:
        library_url = build_ads_library_search_url(brand_name)
    else:
        library_url = build_ads_library_url(normalized_page_id)

    ads = await crawl_meta_ads(
        url=library_url,
        scroll_wait_ms=max(_int_env("SCROLL_WAIT_MS", 1200), 300),
        debug=os.getenv("DEBUG", "").lower() in {"1", "true", "yes"},
        stable_rounds=max(_int_env("STABLE_ROUNDS", 3), 1),
        stable_max_rounds=max(_int_env("STABLE_MAX_ROUNDS", 18), 1),
        limit=max(_int_env("LIMIT", 0), 0),
    )
    return normalized_page_id, library_url, ads


def sync_competitor(competitor_id: int) -> int:
    """Sync a single competitor: crawl, store, transcribe, embed."""
    _setup_logging()
    database_url = _database_url()

    # Phase 1: Fetch details and Crawl (No DB transaction during crawl)
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT workspace_id, page_id, brand FROM competitors WHERE id = %s",
                (competitor_id,)
            )
            row = cur.fetchone()
            if not row:
                LOGGER.error("competitor not found: id=%s", competitor_id)
                return 0
            
            workspace_id, page_id, brand = row["workspace_id"], row["page_id"], row["brand"]

    target_url = build_ads_library_search_url(brand) if brand else build_ads_library_url(page_id)
    
    try:
        LOGGER.info("--- Starting targeted sync for %s (ID: %s, Page: %s) ---", brand, competitor_id, page_id)
        crawled_page_id, library_url, ads = asyncio.run(_crawl_page(page_id, brand))
        observed_library_ids = _library_ids_from_ads(ads)
        with psycopg.connect(database_url) as conn:
            existing_library_ids = _existing_library_ids(conn, observed_library_ids)
        existing_ads, new_ads = _split_ads_by_existing(ads, existing_library_ids)
        prepared_new_ads = _prepare_media_for_storage(new_ads)
        LOGGER.info(
            "crawl split competitor_id=%s observed=%s existing=%s new=%s",
            competitor_id,
            len(observed_library_ids),
            len(existing_ads),
            len(prepared_new_ads),
        )
    except Exception as exc:
        LOGGER.exception("crawl failed for competitor_id=%s", competitor_id)
        return 0

    # Phase 2: Store Ads (Independent Transaction)
    stored = 0
    new_library_ids = []
    with psycopg.connect(database_url, autocommit=False) as conn:
        run_id = _create_run(
            conn,
            page_id,
            target_url,
            workspace_id=workspace_id,
            competitor_id=competitor_id,
        )
        conn.commit()

        try:
            existing_stored, _ = _store_existing_ad_observations(
                conn,
                run_id=run_id,
                workspace_id=workspace_id,
                competitor_id=competitor_id,
                ads=existing_ads,
            )
            new_stored, new_library_ids = _store_ads(
                conn,
                run_id=run_id,
                page_id=crawled_page_id,
                workspace_id=workspace_id,
                competitor_id=competitor_id,
                ads=prepared_new_ads,
            )
            stored = existing_stored + new_stored
            _mark_missing_ads(
                conn,
                workspace_id=workspace_id,
                competitor_id=competitor_id,
                page_id=crawled_page_id,
                observed_library_ids=observed_library_ids,
            )
            _finish_run(conn, run_id, status="success", ad_count=stored)
            conn.commit()
            LOGGER.info(
                "stored %s observed ads for competitor_id=%s run_id=%s new=%s",
                stored,
                competitor_id,
                run_id,
                len(new_library_ids),
            )
        except Exception as exc:
            conn.rollback()
            _finish_run(conn, run_id, status="failed", error=str(exc))
            conn.commit()
            LOGGER.exception("storage failed for competitor_id=%s", competitor_id)
            return 0

    # Phase 3: Post-crawl processing (Independent Tasks)
    if new_library_ids:
        try:
            _process_videos_after_crawl(database_url, new_library_ids)
        except Exception:
            LOGGER.exception("video extraction failed for competitor_id=%s", competitor_id)
        
        try:
            _process_embeddings_after_crawl(database_url, new_library_ids)
        except Exception:
            LOGGER.exception("embedding generation failed for competitor_id=%s", competitor_id)

        try:
            _expand_versions_globally(database_url)
        except Exception:
            LOGGER.exception("global version expansion failed for competitor_id=%s", competitor_id)
    
    return stored


def main() -> int:
    _load_local_env()
    _setup_logging()
    database_url = _database_url()
    page_id = os.getenv("PAGE_ID", DEFAULT_PAGE_ID).strip() or DEFAULT_PAGE_ID

    with psycopg.connect(database_url, autocommit=False) as conn:
        conn.execute(SCHEMA_SQL)
        _ensure_default_workspace(conn)
        conn.commit()

        competitors = _monitoring_competitors(conn)
        
        if not competitors:
            # Fallback to env PAGE_ID if no competitors found
            target_url = build_ads_library_url(page_id)
            run_id = _create_run(conn, page_id, target_url)
            conn.commit()
            try:
                crawled_page_id, library_url, ads = asyncio.run(_crawl_page(page_id, ""))
                observed_library_ids = _library_ids_from_ads(ads)
                existing_library_ids = _existing_library_ids(conn, observed_library_ids)
                conn.commit()
                existing_ads, new_ads = _split_ads_by_existing(ads, existing_library_ids)
                prepared_new_ads = _prepare_media_for_storage(new_ads)
                existing_stored, _ = _store_existing_ad_observations(
                    conn,
                    run_id=run_id,
                    ads=existing_ads,
                )
                new_stored, new_library_ids = _store_ads(
                    conn,
                    run_id=run_id,
                    page_id=crawled_page_id,
                    ads=prepared_new_ads,
                )
                stored = existing_stored + new_stored
                _finish_run(conn, run_id, status="success", ad_count=stored)
                conn.commit()
                if new_library_ids:
                    _process_videos_after_crawl(database_url, new_library_ids)
                    _process_embeddings_after_crawl(database_url, new_library_ids)
                    _expand_versions_globally(database_url)
                return 0
            except Exception as exc:
                conn.rollback()
                _finish_run(conn, run_id, status="failed", error=str(exc))
                conn.commit()
                return 1

        stored_total = 0
        for target in competitors:
            stored_total += sync_competitor(target["id"])
            
        # FINAL STEP: Expand versions from leaders to followers globally
        try:
            _expand_versions_globally(database_url)
        except Exception:
            LOGGER.exception("Final global version expansion failed")

        return 0 if stored_total or competitors else 1


if __name__ == "__main__":
    sys.exit(main())
