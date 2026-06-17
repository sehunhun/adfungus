from __future__ import annotations

import logging
import mimetypes
import os
import time
import uuid
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse, quote

from jwt import InvalidTokenError, PyJWKClient, decode as jwt_decode
import psycopg
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from ad_embedding_worker import SCHEMA_SQL as EMBEDDING_SCHEMA_SQL
from cron_runner_old import SCHEMA_SQL, _database_url, _ensure_default_workspace, sync_competitor
from meta_ads_crawler import search_meta_advertisers
from google import genai
from google.genai import types
import numpy as np
from sklearn.cluster import KMeans

# 분석 결과 캐시 테이블 스키마
ANALYSIS_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hooking_analysis_cache (
    workspace_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    result_json JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, category)
);
"""


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web_static"
_clerk_jwks_client: PyJWKClient | None = None

load_dotenv(dotenv_path=ROOT / ".env", override=False)


logger = logging.getLogger("adfungus.timing")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _trace_id(request: Request) -> str:
    return request.headers.get("x-trace-id") or str(uuid.uuid4())


def _ms(start_ns: float) -> float:
    return (time.perf_counter() - start_ns) * 1000


class BrandSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    country: str = "KR"
    limit: int = Field(default=10, ge=1, le=30)


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class FolderPatchRequest(BaseModel):
    name: str | None = None
    sort_order: int | None = None


class FolderReorderRequest(BaseModel):
    folder_ids: List[int]


class CompetitorInput(BaseModel):
    page_id: str
    brand: str
    brand_logo_url: str = ""
    representative_library_id: str = ""


class CompetitorCreateRequest(BaseModel):
    folder_id: int | None = None
    search_query: str = ""
    items: List[CompetitorInput]


class CompetitorPatchRequest(BaseModel):
    monitoring_enabled: bool | None = None
    folder_id: int | None = None


class CompetitorReorderRequest(BaseModel):
    folder_id: int | None = None
    competitor_ids: List[int]


class SavedAdRequest(BaseModel):
    library_id: str


def _split_csv_env(name: str) -> List[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _to_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _library_id_values(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    ids: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("id") or value.get("library_id") or value.get("libraryID")
        if value is None:
            continue
        library_id = str(value).strip()
        if library_id:
            ids.append(library_id)
    return ids


def _normalize_brand_name(value: str) -> str:
    return " ".join((value or "").split()).strip().lower()


def _brand_normalized_sql(expr: str) -> str:
    return f"LOWER(BTRIM(COALESCE({expr}, '')))"


def _hidden_brand_not_exists_clause(*, workspace_expr: str, brand_expr: str) -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM workspace_hidden_brands whb
            WHERE whb.workspace_id = {workspace_expr}
              AND whb.brand_normalized = {_brand_normalized_sql(brand_expr)}
        )
    """


def _hooking_category_member_rows(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: int,
    category: str,
) -> list[dict[str, Any]]:
    return conn.execute(
        """
        WITH successful_extractions AS (
            SELECT DISTINCT e.library_id, e.hooking_audio, e.hooking_screen_text, e.hooking_categories
            FROM workspace_meta_ads wa
            JOIN meta_ads a ON a.library_id = wa.library_id
            JOIN meta_ad_video_extractions e ON e.library_id = wa.library_id
            WHERE wa.workspace_id = %s
              AND e.status = 'success'
              AND {_hidden_brand_not_exists_clause(workspace_expr='wa.workspace_id', brand_expr='a.brand')}
        ),
        category_members AS (
            SELECT DISTINCT
                e.library_id,
                e.hooking_audio,
                e.hooking_screen_text
            FROM successful_extractions e
            CROSS JOIN LATERAL unnest(COALESCE(e.hooking_categories, '{}'::text[])) AS raw(category_value)
            CROSS JOIN LATERAL regexp_split_to_table(raw.category_value, '\\s*,\\s*') AS split(split_category)
            WHERE btrim(split.split_category) = %s
        )
        SELECT library_id, hooking_audio, hooking_screen_text
        FROM category_members
        """,
        (workspace_id, category),
    ).fetchall()


def _clerk_auth_enabled() -> bool:
    default_enabled = bool(
        os.getenv("CLERK_JWKS_URL", "").strip()
        or os.getenv("CLERK_FRONTEND_API_URL", "").strip()
    )
    return _to_bool(os.getenv("CLERK_AUTH_ENABLED", "1" if default_enabled else "0"), default_enabled)


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


def _extract_clerk_token(request: Request) -> str | None:
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
        decode_kwargs: Dict[str, Any] = {"algorithms": ["RS256"], "options": {"verify_aud": False}}
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


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(_database_url(), row_factory=dict_row)


def _sanitize_download_name(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in (value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def _guess_extension_from_url_or_type(url: str, content_type: str | None, fallback: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix

    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            if guessed == ".jpe":
                return ".jpg"
            return guessed

    return fallback


def _init_db() -> int:
    with _connect() as conn:
        with conn.cursor() as cur:
            # check if basic tables exist
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'app_users' LIMIT 1")
            if not cur.fetchone():
                cur.execute(SCHEMA_SQL)
            
            # check if embedding table exists
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'meta_ad_embeddings' LIMIT 1")
            if not cur.fetchone():
                cur.execute(EMBEDDING_SCHEMA_SQL)
            
            # check if cache table exists
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'hooking_analysis_cache' LIMIT 1")
            if not cur.fetchone():
                cur.execute(ANALYSIS_CACHE_SCHEMA)
            cur.execute("ALTER TABLE competitors ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0")

            workspace_id = _ensure_default_workspace(conn)
            conn.commit()
            return workspace_id


def _workspace_context(request: Request, conn: psycopg.Connection[Any]) -> dict[str, Any]:
    if not _clerk_auth_enabled():
        return {"workspace_id": _ensure_default_workspace(conn), "user_id": None, "clerk_user_id": None, "role": "owner"}

    claims = _verify_clerk_request(request)
    clerk_user_id = str(claims.get("sub") or "").strip()
    if not clerk_user_id:
        raise HTTPException(status_code=401, detail="Clerk token is missing subject")
    email = claims.get("email") or claims.get("primary_email_address")
    name = claims.get("name") or claims.get("full_name")
    with conn.cursor() as cur:
        user = cur.execute(
            """
            INSERT INTO app_users (clerk_user_id, email, name, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (clerk_user_id) DO UPDATE SET
                email = COALESCE(EXCLUDED.email, app_users.email),
                name = COALESCE(EXCLUDED.name, app_users.name),
                updated_at = now()
            RETURNING id
            """,
            (clerk_user_id, email, name),
        ).fetchone()
        user_id = int(user["id"])
        membership = cur.execute(
            """
            SELECT workspace_id, role
            FROM workspace_members
            WHERE user_id = %s OR clerk_user_id = %s
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (user_id, clerk_user_id),
        ).fetchone()
        if not membership:
            workspace = cur.execute(
                """
                INSERT INTO workspaces (name, owner_user_id)
                VALUES (%s, %s)
                RETURNING id
                """,
                ("My Workspace", user_id),
            ).fetchone()
            workspace_id = int(workspace["id"])
            cur.execute(
                """
                INSERT INTO workspace_members (workspace_id, clerk_user_id, user_id, role)
                VALUES (%s, %s, %s, 'owner')
                ON CONFLICT (workspace_id, clerk_user_id) DO UPDATE SET user_id = EXCLUDED.user_id
                """,
                (workspace_id, clerk_user_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO folders (workspace_id, parent_id, name)
                VALUES (%s, NULL, 'Default')
                ON CONFLICT (workspace_id, parent_id, name) DO NOTHING
                """,
                (workspace_id,),
            )
            conn.commit()
            return {"workspace_id": workspace_id, "user_id": user_id, "clerk_user_id": clerk_user_id, "role": "owner"}

        workspace_id = int(membership["workspace_id"])
        role = str(membership["role"])
        cur.execute(
            "UPDATE workspace_members SET user_id = %s WHERE workspace_id = %s AND clerk_user_id = %s",
            (user_id, workspace_id, clerk_user_id),
        )
        conn.commit()
        return {"workspace_id": workspace_id, "user_id": user_id, "clerk_user_id": clerk_user_id, "role": role}


def _require_write_role(ctx: dict[str, Any]) -> None:
    if ctx["role"] not in {"owner", "admin", "member"}:
        raise HTTPException(status_code=403, detail="Workspace write access required")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    yield


app = FastAPI(title="AdFungus Monitor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _to_bool(os.getenv("CORS_ALLOW_ALL", "0")) else (_split_csv_env("FRONTEND_ORIGINS") or [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Global error: %s", exc, exc_info=True)
    
    # CORS 헤더를 수동으로 포함하여 에러 응답 반환
    origins = _split_csv_env("FRONTEND_ORIGINS") or ["*"]
    origin = request.headers.get("origin")
    allow_origin = origin if origin in origins or "*" in origins else origins[0]
    
    status_code = 500
    detail = "Internal Server Error"
    
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
        
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "type": type(exc).__name__},
        headers={
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Credentials": "true",
        }
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def state(
    request: Request, 
    response: Response, 
    sort_by: str = "newest", 
    status: str = "all",
    limit: int = 1000,
    offset: int = 0,
    competitor_ids: str = "",
) -> dict[str, Any]:
    trace_id = _trace_id(request)
    request_started_at = time.perf_counter()
    logger.info("[BE][%s] state.request.start path=%s sort_by=%s status=%s", trace_id, request.url.path, sort_by, status)

    connect_started_at = time.perf_counter()
    with _connect() as conn:
        logger.info("[BE][%s] state.connect_ms=%.2f", trace_id, _ms(connect_started_at))

        context_started_at = time.perf_counter()
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]
        logger.info(
            "[BE][%s] state.workspace_context_ms=%.2f workspace_id=%s",
            trace_id,
            _ms(context_started_at),
            workspace_id,
        )

        query_started_at = time.perf_counter()
        folders = conn.execute(
            """
            SELECT id, name, parent_id, sort_order
            FROM folders
            WHERE workspace_id = %s
            ORDER BY sort_order ASC, created_at ASC
            """,
            (workspace_id,),
        ).fetchall()
        logger.info(
            "[DB][%s] state.folders_ms=%.2f rows=%s",
            trace_id,
            _ms(query_started_at),
            len(folders),
        )

        query_started_at = time.perf_counter()
        # 브랜드 목록 및 통계 최적화: 작업 공간에 속한 경쟁사로 먼저 필터링
        competitors = conn.execute(
            """
            WITH workspace_competitors AS (
                SELECT id, folder_id, sort_order, page_id, brand, brand_logo_url, monitoring_enabled, created_at
                FROM competitors
                WHERE workspace_id = %s
            )
            SELECT
                c.*,
                COALESCE(stats.total_ads, 0)::int AS total_ads,
                COALESCE(stats.image_ads, 0)::int AS image_ads,
                COALESCE(stats.video_ads, 0)::int AS video_ads,
                stats.last_seen_at
            FROM workspace_competitors c
            LEFT JOIN (
                SELECT 
                    wa.competitor_id,
                    COUNT(a.library_id) AS total_ads,
                    COUNT(a.library_id) FILTER (WHERE a.ad_format = 'image') AS image_ads,
                    COUNT(a.library_id) FILTER (WHERE a.ad_format IN ('video', 'carousel')) AS video_ads,
                    MAX(wa.last_seen_at) AS last_seen_at
                FROM workspace_meta_ads wa
                JOIN meta_ads a ON a.library_id = wa.library_id
                WHERE wa.workspace_id = %s
                  AND """ + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand") + """
                GROUP BY wa.competitor_id
            ) stats ON stats.competitor_id = c.id
            ORDER BY c.folder_id NULLS FIRST, c.sort_order ASC, c.created_at ASC
            """,
            (workspace_id, workspace_id),
        ).fetchall()
        logger.info(
            "[DB][%s] state.competitors_ms=%.2f rows=%s",
            trace_id,
            _ms(query_started_at),
            len(competitors),
        )

        query_started_at = time.perf_counter()
        
        # 정렬 기준 매핑
        order_clause = "wa.last_seen_at DESC"
        if sort_by == "oldest":
            order_clause = "wa.first_seen_at ASC"
        elif sort_by == "reference":
            order_clause = "a.similar_count DESC, wa.last_seen_at DESC"
        elif sort_by == "duration":
            order_clause = "(COALESCE(wa.ended_at, wa.last_seen_at, now()) - a.start_date) DESC"

        # 상태 필터링 추가
        where_clause = "wa.workspace_id = %s AND " + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand")
        params = [workspace_id]
        if status in ("running", "ended"):
            where_clause += " AND wa.status = %s"
            params.append(status)
        selected_competitor_ids = [
            int(value)
            for value in competitor_ids.split(",")
            if value.strip().isdigit()
        ]
        if selected_competitor_ids:
            where_clause += " AND wa.competitor_id = ANY(%s)"
            params.append(selected_competitor_ids)

        # 공통 쿼리 베이스 정의
        query_base = f"""
            SELECT
                a.library_id, wa.competitor_id, a.page_id, a.brand, a.brand_logo_url,
                wa.status, a.body, a.link_title, a.link_url, a.start_date_text,
                a.start_date, a.ad_format, a.same_source_library_ids, a.meta_library_url,
                wa.first_seen_at, wa.last_seen_at, wa.ended_at,
                a.similar_count, a.similar_library_ids,
                a.influencer_instagram_username,
                ig.like_count AS instagram_like_count,
                ig.comments_count AS instagram_comments_count,
                ig.view_count AS instagram_view_count,
                ig.match_status AS instagram_metrics_status,
                ig.created_at AS instagram_metrics_matched_at,
                ig.permalink AS instagram_permalink,
                EXISTS (
                    SELECT 1 FROM saved_ads s
                    WHERE s.workspace_id = %s AND s.library_id = a.library_id
                ) AS saved,
                COALESCE(i.url, v.url) AS media_url,
                i.url AS thumbnail_url,
                v.url AS video_url,
                CASE
                    WHEN v.url IS NOT NULL THEN 'video'
                    WHEN i.url IS NOT NULL THEN 'image'
                    ELSE a.ad_format
                END AS media_type,
                v.duration_seconds
            FROM workspace_meta_ads wa
            JOIN meta_ads a ON a.library_id = wa.library_id
            LEFT JOIN LATERAL (
                SELECT url, duration_seconds
                FROM meta_ad_media
                WHERE library_id = a.library_id
                  AND media_type = 'video'
                LIMIT 1
            ) v ON true
            LEFT JOIN LATERAL (
                SELECT url
                FROM meta_ad_media
                WHERE library_id = a.library_id
                  AND media_type = 'image'
                ORDER BY id ASC
                LIMIT 1
            ) i ON true
            LEFT JOIN LATERAL (
                SELECT like_count, comments_count, view_count, match_status, created_at, permalink
                FROM meta_ad_instagram_metric_snapshots
                WHERE library_id = a.library_id
                ORDER BY created_at DESC
                LIMIT 1
            ) ig ON true
            WHERE {where_clause}
            ORDER BY {order_clause}
        """

        if sort_by == "reference":
            # 레퍼런스 순일 때는 '탐욕적 중복 제거(Greedy Deduplication)' 적용
            # 1. 일단 전체 광고를 가져옴 (필터 적용된 것들만)
            all_candidates = conn.execute(query_base, [workspace_id] + params).fetchall()
            
            # 2. 파이썬 레벨에서 중복 제거
            deduped_ads = []
            excluded_ids = set()
            for ad in all_candidates:
                lib_id = ad["library_id"]
                if lib_id in excluded_ids:
                    continue
                
                deduped_ads.append(ad)
                
                # 이 광고와 유사하다고 판명된 광고들을 모두 제외 목록에 추가
                for sid in _library_id_values(ad.get("similar_library_ids")):
                    excluded_ids.add(sid)
            
            total_count = len(deduped_ads)
            ads = deduped_ads[offset : offset + limit]
        else:
            # 기존 방식: DB에서 개수 조회 및 페이징 처리
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) as count
                FROM workspace_meta_ads wa
                JOIN meta_ads a ON a.library_id = wa.library_id
                WHERE {where_clause}
                """,
                params
            ).fetchone()
            total_count = int(total_row["count"])

            ads = conn.execute(
                query_base + " LIMIT %s OFFSET %s",
                [workspace_id] + params + [limit, offset],
            ).fetchall()
        logger.info(
            "[DB][%s] state.ads_ms=%.2f rows=%s total=%s",
            trace_id,
            _ms(query_started_at),
            len(ads),
            total_count
        )

        # 총 저장된 광고 개수 조회
        saved_row = conn.execute(
            f"""
            SELECT COUNT(*)::int as count
            FROM saved_ads s
            JOIN meta_ads a ON a.library_id = s.library_id
            WHERE s.workspace_id = %s
              AND {_hidden_brand_not_exists_clause(workspace_expr='s.workspace_id', brand_expr='a.brand')}
            """,
            (workspace_id,)
        ).fetchone()
        saved_count = int(saved_row["count"] or 0)

    payload = {
        "workspace_id": workspace_id,
        "folders": folders,
        "competitors": competitors,
        "ads": ads,
        "total_count": total_count,
        "saved_count": saved_count,
    }
    total_ms = _ms(request_started_at)
    logger.info(
        "[BE][%s] state.request.done total_ms=%.2f folders=%s competitors=%s ads=%s",
        trace_id,
        total_ms,
        len(folders),
        len(competitors),
        len(ads),
    )
    
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Trace-Path"] = str(request.url.path)
    return payload


@app.get("/api/creative-composition/top-hooking-categories")
def top_hooking_categories(request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]

        total_row = conn.execute(
            """
            SELECT COUNT(DISTINCT e.library_id)::int AS total_analyzed_ads
            FROM workspace_meta_ads wa
            JOIN meta_ads a ON a.library_id = wa.library_id
            JOIN meta_ad_video_extractions e ON e.library_id = wa.library_id
            WHERE wa.workspace_id = %s
              AND e.status = 'success'
              AND """ + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand") + """
            """,
            (workspace_id,),
        ).fetchone()

        rows = conn.execute(
            """
            WITH successful_extractions AS (
                SELECT DISTINCT e.library_id, e.hooking_categories
                FROM workspace_meta_ads wa
                JOIN meta_ads a ON a.library_id = wa.library_id
                JOIN meta_ad_video_extractions e ON e.library_id = wa.library_id
                WHERE wa.workspace_id = %s
                  AND e.status = 'success'
                  AND """ + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand") + """
            ),
            categories AS (
                SELECT DISTINCT
                    e.library_id,
                    btrim(split.split_category) AS category
                FROM successful_extractions e
                CROSS JOIN LATERAL unnest(COALESCE(e.hooking_categories, '{}'::text[])) AS raw(category_value)
                CROSS JOIN LATERAL regexp_split_to_table(raw.category_value, '\\s*,\\s*') AS split(split_category)
            ),
            ranked AS (
                SELECT
                    category,
                    COUNT(DISTINCT library_id)::int AS count
                FROM categories
                WHERE category IS NOT NULL
                  AND category <> ''
                GROUP BY category
            )
            SELECT
                ROW_NUMBER() OVER (ORDER BY count DESC, category ASC)::int AS rank,
                category,
                count
            FROM ranked
            ORDER BY count DESC, category ASC
            LIMIT 10
            """,
            (workspace_id,),
        ).fetchall()

    return {
        "total_analyzed_ads": int(total_row["total_analyzed_ads"] or 0),
        "items": rows,
    }


@app.get("/api/creative-composition/by-type")
def creative_composition_by_type(request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]

        # 1. Get all categories and counts
        categories_rows = conn.execute(
            """
            WITH successful_extractions AS (
                SELECT DISTINCT e.library_id, e.hooking_categories
                FROM workspace_meta_ads wa
                JOIN meta_ads a ON a.library_id = wa.library_id
                JOIN meta_ad_video_extractions e ON e.library_id = wa.library_id
                WHERE wa.workspace_id = %s
                  AND e.status = 'success'
                  AND """ + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand") + """
            ),
            categories AS (
                SELECT DISTINCT
                    e.library_id,
                    btrim(split.split_category) AS category
                FROM successful_extractions e
                CROSS JOIN LATERAL unnest(COALESCE(e.hooking_categories, '{}'::text[])) AS raw(category_value)
                CROSS JOIN LATERAL regexp_split_to_table(raw.category_value, '\\s*,\\s*') AS split(split_category)
            )
            SELECT
                category,
                COUNT(DISTINCT library_id)::int AS count
            FROM categories
            WHERE category IS NOT NULL
              AND category <> ''
            GROUP BY category
            ORDER BY count DESC, category ASC
            """,
            (workspace_id,),
        ).fetchall()

        # 2. For each category, prefetch representative ads for the collapsed view/expand handoff.
        items = []
        for row in categories_rows:
            category = row["category"]
            ads = conn.execute(
                """
                WITH category_members AS (
                    SELECT DISTINCT e.library_id
                    FROM workspace_meta_ads wa
                    JOIN meta_ads a ON a.library_id = wa.library_id
                    JOIN meta_ad_video_extractions e ON e.library_id = wa.library_id
                    CROSS JOIN LATERAL unnest(COALESCE(e.hooking_categories, '{}'::text[])) AS raw(category_value)
                    CROSS JOIN LATERAL regexp_split_to_table(raw.category_value, '\\s*,\\s*') AS split(split_category)
                    WHERE wa.workspace_id = %s
                      AND e.status = 'success'
                      AND """ + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand") + """
                      AND btrim(split.split_category) = %s
                )
                SELECT 
                    a.library_id, 
                    a.brand,
                    a.brand_logo_url,
                    a.body,
                    a.start_date,
                    a.status,
                    COALESCE(i.url, v.url) AS thumbnail_url
                FROM workspace_meta_ads wa
                JOIN meta_ads a ON a.library_id = wa.library_id
                JOIN meta_ad_video_extractions e ON e.library_id = a.library_id
                LEFT JOIN LATERAL (
                    SELECT url FROM meta_ad_media 
                    WHERE library_id = a.library_id AND media_type = 'video' LIMIT 1
                ) v ON true
                LEFT JOIN LATERAL (
                    SELECT url FROM meta_ad_media 
                    WHERE library_id = a.library_id AND media_type = 'image' ORDER BY id ASC LIMIT 1
                ) i ON true
                JOIN category_members cm ON cm.library_id = a.library_id
                ORDER BY a.start_date DESC, a.library_id DESC
                LIMIT 10
                """,
                (workspace_id, category),
            ).fetchall()
            
            items.append({
                "category": category,
                "count": row["count"],
                "ads": ads
            })

    return {"items": items}


@app.get("/api/creative-composition/category-ads/{category}")
def category_ads(category: str, request: Request, page: int = 1, limit: int = 40) -> dict[str, Any]:
    offset = (page - 1) * limit
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]

        rows = conn.execute(
            """
            WITH category_members AS (
                SELECT DISTINCT e.library_id
                FROM workspace_meta_ads wa
                JOIN meta_ads a ON a.library_id = wa.library_id
                JOIN meta_ad_video_extractions e ON e.library_id = wa.library_id
                CROSS JOIN LATERAL unnest(COALESCE(e.hooking_categories, '{}'::text[])) AS raw(category_value)
                CROSS JOIN LATERAL regexp_split_to_table(raw.category_value, '\\s*,\\s*') AS split(split_category)
                WHERE wa.workspace_id = %s
                  AND e.status = 'success'
                  AND """ + _hidden_brand_not_exists_clause(workspace_expr="wa.workspace_id", brand_expr="a.brand") + """
                  AND btrim(split.split_category) = %s
            )
            SELECT 
                a.library_id, 
                a.brand,
                a.brand_logo_url,
                a.body,
                a.start_date,
                a.status,
                COALESCE(i.url, v.url) AS thumbnail_url
            FROM workspace_meta_ads wa
            JOIN meta_ads a ON a.library_id = wa.library_id
            JOIN meta_ad_video_extractions e ON e.library_id = a.library_id
            LEFT JOIN LATERAL (
                SELECT url FROM meta_ad_media 
                WHERE library_id = a.library_id AND media_type = 'video' LIMIT 1
            ) v ON true
            LEFT JOIN LATERAL (
                SELECT url FROM meta_ad_media 
                WHERE library_id = a.library_id AND media_type = 'image' ORDER BY id ASC LIMIT 1
            ) i ON true
            JOIN category_members cm ON cm.library_id = a.library_id
            ORDER BY a.start_date DESC, a.library_id DESC
            LIMIT %s OFFSET %s
            """,
            (workspace_id, category, limit, offset),
        ).fetchall()

    return {"items": rows}


@app.post("/api/meta/brand-search")
async def brand_search(payload: BrandSearchRequest, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        _workspace_context(request, conn)
    try:
        brands = await search_meta_advertisers(
            payload.query,
            country=payload.country,
            limit=payload.limit,
            detail_concurrency=int(os.getenv("SEARCH_DETAIL_CONCURRENCY", "4")),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"query": payload.query, "count": len(brands), "brands": brands}

@app.post("/api/folders")
def create_folder(payload: FolderCreateRequest, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        row = conn.execute(
            """
            INSERT INTO folders (workspace_id, parent_id, name)
            VALUES (%s, NULL, %s)
            ON CONFLICT (workspace_id, parent_id, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id, name, parent_id
            """,
            (workspace_id, payload.name.strip()),
        ).fetchone()
        conn.commit()
    return {"folder": row}


@app.patch("/api/folders/{folder_id}")
def update_folder(folder_id: int, payload: FolderPatchRequest, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        
        updates = []
        values = []
        if payload.name is not None:
            updates.append("name = %s")
            values.append(payload.name.strip())
        if payload.sort_order is not None:
            updates.append("sort_order = %s")
            values.append(payload.sort_order)
            
        if not updates:
            raise HTTPException(status_code=400, detail="no updates provided")
            
        values.extend([folder_id, workspace_id])
        row = conn.execute(
            f"""
            UPDATE folders
            SET {", ".join(updates)}
            WHERE id = %s AND workspace_id = %s
            RETURNING id, name, parent_id, sort_order
            """,
            values,
        ).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="folder not found")
    return {"folder": row}


@app.post("/api/folders/reorder")
def reorder_folders(payload: FolderReorderRequest, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        
        # Batch update sort order
        for index, folder_id in enumerate(payload.folder_ids):
            conn.execute(
                "UPDATE folders SET sort_order = %s WHERE id = %s AND workspace_id = %s",
                (index, folder_id, workspace_id),
            )
        conn.commit()
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        competitor_result = conn.execute(
            """
            WITH RECURSIVE folder_tree AS (
                SELECT id
                FROM folders
                WHERE id = %s AND workspace_id = %s
                UNION ALL
                SELECT child.id
                FROM folders child
                JOIN folder_tree parent ON parent.id = child.parent_id
                WHERE child.workspace_id = %s
            )
            DELETE FROM competitors
            WHERE workspace_id = %s
              AND folder_id IN (SELECT id FROM folder_tree)
            """,
            (folder_id, workspace_id, workspace_id, workspace_id),
        )
        res = conn.execute(
            "DELETE FROM folders WHERE id = %s AND workspace_id = %s",
            (folder_id, workspace_id),
        )
        conn.commit()
    return {"ok": True, "deleted_competitors": competitor_result.rowcount}


@app.post("/api/competitors")
def create_competitors(payload: CompetitorCreateRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    created = []
    hydrated_ads_count = 0
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        if payload.folder_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM folders WHERE id = %s AND workspace_id = %s",
                (payload.folder_id, workspace_id),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="folder not found")
        for item in payload.items:
            next_sort_order = conn.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM competitors
                WHERE workspace_id = %s
                  AND folder_id IS NOT DISTINCT FROM %s
                """,
                (workspace_id, payload.folder_id),
            ).fetchone()["next_sort_order"]
            row = conn.execute(
                """
                INSERT INTO competitors (
                    workspace_id, folder_id, sort_order, page_id, brand, brand_logo_url,
                    representative_library_id, search_query, created_by_user_id, created_by_app_user_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, page_id) DO UPDATE SET
                    folder_id = EXCLUDED.folder_id,
                    sort_order = EXCLUDED.sort_order,
                    brand = EXCLUDED.brand,
                    brand_logo_url = EXCLUDED.brand_logo_url,
                    representative_library_id = EXCLUDED.representative_library_id,
                    search_query = EXCLUDED.search_query,
                    updated_at = now()
                RETURNING *
                """,
                (
                    workspace_id,
                    payload.folder_id,
                    next_sort_order,
                    item.page_id,
                    item.brand,
                    item.brand_logo_url,
                    item.representative_library_id,
                    payload.search_query,
                    ctx["clerk_user_id"],
                    ctx["user_id"],
                ),
            ).fetchone()
            hydrated = conn.execute(
                """
                INSERT INTO workspace_meta_ads (
                    workspace_id, competitor_id, library_id, first_seen_run_id, last_seen_run_id,
                    status, missing_count, ended_at, first_seen_at, last_seen_at, updated_at
                )
                SELECT
                    %s, %s, a.library_id, a.first_seen_run_id, a.last_seen_run_id,
                    a.status, a.missing_count, a.ended_at, a.first_seen_at, a.last_seen_at, now()
                FROM meta_ads a
                WHERE a.page_id = %s
                ON CONFLICT (workspace_id, competitor_id, library_id) DO NOTHING
                RETURNING library_id
                """,
                (workspace_id, row["id"], item.page_id),
            ).fetchall()
            hydrated_ads_count += len(hydrated)
            created.append(row)
        conn.commit()

        # Schedule immediate sync in the background for each new competitor
        for comp in created:
            logger.info("Scheduling immediate sync for competitor_id=%s", comp["id"])
            background_tasks.add_task(sync_competitor, comp["id"])

    return {"competitors": created, "hydrated_ads_count": hydrated_ads_count}


@app.patch("/api/competitors/{competitor_id}")
def update_competitor(competitor_id: int, payload: CompetitorPatchRequest, request: Request) -> dict[str, Any]:
    updates = []
    values: list[Any] = []
    requested_fields = payload.model_fields_set
    if payload.monitoring_enabled is not None:
        updates.append("monitoring_enabled = %s")
        values.append(payload.monitoring_enabled)
    if "folder_id" in requested_fields:
        updates.append("folder_id = %s")
        values.append(payload.folder_id)
    if not updates:
        raise HTTPException(status_code=400, detail="no updates")
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        if "folder_id" in requested_fields and payload.folder_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM folders WHERE id = %s AND workspace_id = %s",
                (payload.folder_id, workspace_id),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="folder not found")
        if "folder_id" in requested_fields:
            next_sort_order = conn.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM competitors
                WHERE workspace_id = %s
                  AND folder_id IS NOT DISTINCT FROM %s
                  AND id <> %s
                """,
                (workspace_id, payload.folder_id, competitor_id),
            ).fetchone()["next_sort_order"]
            updates.append("sort_order = %s")
            values.append(next_sort_order)
        values.extend([competitor_id, workspace_id])
        row = conn.execute(
            f"""
            UPDATE competitors
            SET {', '.join(updates)}, updated_at = now()
            WHERE id = %s AND workspace_id = %s
            RETURNING *
            """,
            values,
        ).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="competitor not found")
    return {"competitor": row}


@app.post("/api/competitors/reorder")
def reorder_competitors(payload: CompetitorReorderRequest, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        rows = conn.execute(
            """
            SELECT id
            FROM competitors
            WHERE workspace_id = %s
              AND folder_id IS NOT DISTINCT FROM %s
            """,
            (workspace_id, payload.folder_id),
        ).fetchall()
        existing_ids = {row["id"] for row in rows}
        requested_ids = set(payload.competitor_ids)
        if existing_ids != requested_ids:
            raise HTTPException(status_code=400, detail="competitor_ids must match the target folder contents")
        for index, competitor_id in enumerate(payload.competitor_ids):
            conn.execute(
                "UPDATE competitors SET sort_order = %s, updated_at = now() WHERE id = %s AND workspace_id = %s",
                (index, competitor_id, workspace_id),
            )
        conn.commit()
    return {"ok": True}


@app.delete("/api/competitors/{competitor_id}")
def delete_competitor(competitor_id: int, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        # ON DELETE CASCADE handles workspace_meta_ads
        res = conn.execute(
            "DELETE FROM competitors WHERE id = %s AND workspace_id = %s",
            (competitor_id, workspace_id),
        )
        conn.commit()
    return {"ok": True}


@app.post("/api/saved-ads")
def save_ad(payload: SavedAdRequest, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        exists = conn.execute(
            "SELECT 1 FROM workspace_meta_ads WHERE workspace_id = %s AND library_id = %s LIMIT 1",
            (workspace_id, payload.library_id),
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="ad not found in workspace")
        conn.execute(
            """
            INSERT INTO saved_ads (workspace_id, library_id, clerk_user_id, saved_by_user_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace_id, library_id) DO NOTHING
            """,
            (workspace_id, payload.library_id, ctx["clerk_user_id"], ctx["user_id"]),
        )
        conn.commit()
    return {"ok": True}


@app.delete("/api/saved-ads/{library_id}")
def unsave_ad(library_id: str, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        _require_write_role(ctx)
        workspace_id = ctx["workspace_id"]
        conn.execute(
            "DELETE FROM saved_ads WHERE workspace_id = %s AND library_id = %s",
            (workspace_id, library_id),
        )
        conn.commit()
    return {"ok": True}


@app.get("/api/ads/{library_id}")
def get_ad_detail(library_id: str, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]
        
        # 1. 광고 기본 정보 및 미디어 조회
        ad = conn.execute(
            """
            SELECT 
                a.*,
                wa.status as workspace_status,
                ig.like_count AS instagram_like_count,
                ig.comments_count AS instagram_comments_count,
                ig.view_count AS instagram_view_count,
                ig.match_status AS instagram_metrics_status,
                ig.created_at AS instagram_metrics_matched_at,
                ig.permalink AS instagram_permalink,
                EXISTS (
                    SELECT 1 FROM saved_ads s
                    WHERE s.workspace_id = %s AND s.library_id = a.library_id
                ) AS saved,
                COALESCE(i.url, v.url) AS media_url,
                i.url AS thumbnail_url,
                v.url AS video_url
            FROM meta_ads a
            LEFT JOIN workspace_meta_ads wa ON wa.library_id = a.library_id AND wa.workspace_id = %s
            LEFT JOIN LATERAL (
                SELECT url
                FROM meta_ad_media
                WHERE library_id = a.library_id AND media_type = 'video'
                LIMIT 1
            ) v ON true
            LEFT JOIN LATERAL (
                SELECT url
                FROM meta_ad_media
                WHERE library_id = a.library_id AND media_type = 'image'
                ORDER BY id ASC
                LIMIT 1
            ) i ON true
            LEFT JOIN LATERAL (
                SELECT like_count, comments_count, view_count, match_status, created_at, permalink
                FROM meta_ad_instagram_metric_snapshots
                WHERE library_id = a.library_id
                ORDER BY created_at DESC
                LIMIT 1
            ) ig ON true
            WHERE (a.library_id = %s OR a.library_id = TRIM(%s))
              AND """ + _hidden_brand_not_exists_clause(workspace_expr="%s", brand_expr="a.brand") + """
            """,
            (workspace_id, workspace_id, library_id, library_id, workspace_id),
        ).fetchone()

        if not ad:
            raise HTTPException(status_code=404, detail="ad not found")

        # 2. 비디오 추출 데이터(스크립트 등) 조회
        extractions = conn.execute(
            """
            SELECT *
            FROM meta_ad_video_extractions
            WHERE library_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (library_id,),
        ).fetchone()

        # 3. 연관 소재(변형 광고 + 유사 광고) 정보 조회
        variations = []
        variation_ids = list(
            dict.fromkeys(
                _library_id_values(ad.get("same_source_library_ids"))
                + _library_id_values(ad.get("similar_library_ids"))
            )
        )
        # 현재 광고 본인은 제외
        if library_id in variation_ids:
            variation_ids.remove(library_id)
            
        if variation_ids:
            variation_rows = conn.execute(
                """
                SELECT 
                    a.library_id, a.status, a.start_date, a.start_date_text, a.page_id, a.brand, a.brand_logo_url,
                    v.video_url,
                    i.url as image_url,
                    COALESCE(i.url, v.video_url) as thumbnail_url,
                    ig.like_count AS instagram_like_count,
                    ig.comments_count AS instagram_comments_count,
                    ig.view_count AS instagram_view_count
                FROM meta_ads a
                LEFT JOIN LATERAL (
                    SELECT url as video_url
                    FROM meta_ad_media
                    WHERE library_id = a.library_id AND media_type = 'video'
                    LIMIT 1
                ) v ON true
                LEFT JOIN LATERAL (
                    SELECT url
                    FROM meta_ad_media
                    WHERE library_id = a.library_id AND media_type = 'image'
                    ORDER BY id ASC
                    LIMIT 1
                ) i ON true
                LEFT JOIN LATERAL (
                    SELECT like_count, comments_count, view_count
                    FROM meta_ad_instagram_metric_snapshots
                    WHERE library_id = a.library_id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) ig ON true
                WHERE a.library_id = ANY(%s)
                  AND """ + _hidden_brand_not_exists_clause(workspace_expr="%s", brand_expr="a.brand") + """
                """,
                (variation_ids, workspace_id),
            ).fetchall()
            variations = variation_rows

        metric_history = conn.execute(
            """
            SELECT
                created_at,
                like_count,
                comments_count,
                view_count
            FROM meta_ad_instagram_metric_snapshots
            WHERE library_id = %s
              AND match_status = 'matched'
            ORDER BY created_at ASC
            """,
            (library_id,),
        ).fetchall()

    return {
        "ad": ad,
        "extractions": extractions,
        "variations": variations,
        "metric_history": metric_history,
    }


@app.get("/api/ads/{library_id}/media-download")
async def download_ad_media(library_id: str, request: Request) -> Response:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]
        ad = conn.execute(
            """
            SELECT
                a.library_id,
                a.brand,
                COALESCE(i.url, v.url) AS media_url,
                v.url AS video_url
            FROM meta_ads a
            LEFT JOIN LATERAL (
                SELECT url
                FROM meta_ad_media
                WHERE library_id = a.library_id AND media_type = 'video'
                LIMIT 1
            ) v ON true
            LEFT JOIN LATERAL (
                SELECT url
                FROM meta_ad_media
                WHERE library_id = a.library_id AND media_type = 'image'
                ORDER BY id ASC
                LIMIT 1
            ) i ON true
            WHERE (a.library_id = %s OR a.library_id = TRIM(%s))
              AND """ + _hidden_brand_not_exists_clause(workspace_expr="%s", brand_expr="a.brand") + """
            LIMIT 1
            """,
            (library_id, library_id, workspace_id),
        ).fetchone()

    if not ad:
        raise HTTPException(status_code=404, detail="ad not found")

    media_url = (ad.get("video_url") or ad.get("media_url") or "").strip()
    if not media_url:
        raise HTTPException(status_code=404, detail="media not found")

    is_video = bool(ad.get("video_url"))
    fallback_ext = ".mp4" if is_video else ".jpg"
    base_name = _sanitize_download_name(
        f"{ad.get('brand') or 'ad'}_{ad.get('library_id') or library_id}",
        "ad_media",
    )

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            upstream = await client.get(media_url)
            upstream.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="failed to download upstream media") from exc

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    extension = _guess_extension_from_url_or_type(media_url, content_type, fallback_ext)
    filename = f"{base_name}{extension}"
    encoded_filename = quote(filename)

    return Response(
        content=upstream.content,
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        },
    )


@app.post("/api/ads/batch")
def get_ads_batch(payload: List[str], request: Request) -> dict[str, Any]:
    if not payload:
        return {"items": []}
    
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]
        
        rows = conn.execute(
            """
            SELECT
                a.library_id, wa.competitor_id, a.page_id, a.brand, a.brand_logo_url,
                wa.status, a.body, a.link_title, a.link_url, a.start_date_text,
                a.start_date, a.ad_format, a.meta_library_url,
                e.hooking_audio, e.hooking_screen_text,
                wa.first_seen_at, wa.last_seen_at,
                EXISTS (
                    SELECT 1 FROM saved_ads s
                    WHERE s.workspace_id = %s AND s.library_id = a.library_id
                ) AS saved,
                COALESCE(i.url, v.url) AS media_url,
                i.url AS thumbnail_url,
                v.url AS video_url,
                v.duration_seconds,
                CASE
                    WHEN v.url IS NOT NULL THEN 'video'
                    WHEN i.url IS NOT NULL THEN 'image'
                    ELSE a.ad_format
                END AS media_type
            FROM meta_ads a
            LEFT JOIN workspace_meta_ads wa ON wa.library_id = a.library_id AND wa.workspace_id = %s
            LEFT JOIN LATERAL (
                SELECT hooking_audio, hooking_screen_text
                FROM meta_ad_video_extractions
                WHERE library_id = a.library_id
                  AND status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
            ) e ON true
            LEFT JOIN LATERAL (
                SELECT url, duration_seconds FROM meta_ad_media 
                WHERE library_id = a.library_id AND media_type = 'video' LIMIT 1
            ) v ON true
            LEFT JOIN LATERAL (
                SELECT url FROM meta_ad_media 
                WHERE library_id = a.library_id AND media_type = 'image' ORDER BY id ASC LIMIT 1
            ) i ON true
            WHERE a.library_id = ANY(%s)
              AND """ + _hidden_brand_not_exists_clause(workspace_expr="%s", brand_expr="a.brand") + """
            ORDER BY a.start_date DESC
            """,
            (workspace_id, workspace_id, payload, workspace_id),
        ).fetchall()

    return {"items": rows}


@app.get("/api/auth-config")
def auth_config() -> dict[str, Any]:
    return {
        "enabled": _clerk_auth_enabled(),
        "publishable_key": os.getenv("CLERK_PUBLISHABLE_KEY", "").strip()
        or os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "").strip(),
    }


@app.get("/api/analysis/hooking-subtypes/{category}")
def get_hooking_subtypes(category: str, request: Request) -> dict[str, Any]:
    with _connect() as conn:
        ctx = _workspace_context(request, conn)
        workspace_id = ctx["workspace_id"]
        
        # 1. 캐시 확인 (1일 이내 데이터)
        cache = conn.execute(
            """
            SELECT result_json
            FROM hooking_analysis_cache
            WHERE workspace_id = %s
              AND category = %s
              AND updated_at > now() - interval '1 days'
            """,
            (workspace_id, category),
        ).fetchone()
        
        if cache:
            return cache["result_json"]

        # 2. 데이터 수집 (카테고리 일치하는 광고의 후킹 텍스트)
        rows = _hooking_category_member_rows(conn, workspace_id=workspace_id, category=category)

        if not rows:
            return {"total_count": 0, "items": []}

        # 3. 데이터 전처리 및 후킹 텍스트 추출
        texts = []
        library_ids = []
        valid_rows = []
        
        for row in rows:
            h_audio = row["hooking_audio"] or []
            h_screen = row["hooking_screen_text"] or []
            t_list = [t.get("text", "") for t in h_audio if t.get("text")]
            if not t_list:
                t_list = [t.get("text", "") for t in h_screen if t.get("text")]
            
            hook_text = " ".join(t_list).strip()
            if not hook_text:
                continue
                
            texts.append(hook_text)
            library_ids.append(row["library_id"])
            valid_rows.append(row)

        if not texts:
            return {"total_count": len(rows), "items": []}

        # 4. 실시간 Batch Embedding (후킹 텍스트만, 최대 100개씩 나눠서 요청)
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            return {"total_count": len(rows), "items": []}
            
        client = genai.Client(api_key=gemini_api_key)
        
        try:
            embeddings = []
            # Gemini Batch Embedding limit is 100
            for i in range(0, len(texts), 100):
                batch_texts = texts[i:i+100]
                emb_res = client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=batch_texts
                )
                embeddings.extend([e.values for e in emb_res.embeddings])
        except Exception as e:
            logger.error("Batch embedding failed: %s", e)
            return {"total_count": len(rows), "items": []}

        X = np.array(embeddings)
        n_clusters = min(10, len(valid_rows))
        
        # 5. K-Means 클러스터링
        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = kmeans.fit_predict(X)

        # 6. 군집별 정보 수집
        all_samples_text = ""
        cluster_base_info = []

        for i in range(n_clusters):
            idx = np.where(labels == i)[0]
            if len(idx) == 0: continue
            
            group_library_ids = [library_ids[j] for j in idx]
            
            # 중심점 대표 문구
            centroid = kmeans.cluster_centers_[i]
            dists = np.linalg.norm(X[idx] - centroid, axis=1)
            rep_idx_local = np.argmin(dists)
            rep_text = texts[idx[rep_idx_local]]
            
            group_texts = [texts[j] for j in idx]
            samples = "\n".join([f"  * {s}" for s in group_texts[:8]])
            
            all_samples_text += f"\n[그룹 {i+1}]\n{samples}\n"
            cluster_base_info.append({
                "id": i,
                "rep_text": rep_text,
                "count": len(idx),
                "library_ids": group_library_ids
            })

        # 6. 모든 그룹을 한꺼번에 Gemini에게 전달하여 중복 없는 이름 짓기
        class ClusterNaming(BaseModel):
            type_name: str
            reason: str

        class AllClusterNames(BaseModel):
            names: list[ClusterNaming]

        naming_prompt = f"""당신은 세계적인 마케팅 전략가입니다. 
다음은 특정 카테고리('{category}') 내에서 다시 10개의 세부 그룹으로 분류된 광고 문구들입니다.
각 그룹이 가진 '독특한 소구점'이나 '심리적 기법'을 분석하여, **서로 절대 중복되지 않는** 10개의 고유한 유형 이름을 지어주세요.

[작성 규칙]
1. 이름은 반드시 '~형'으로 끝나야 합니다. (예: '위협형', '공감 유도형', '후회형', '효과 강조형', '경험 공유형', '이벤트형' 등)
2. 10개 그룹의 이름은 서로 의미가 겹치지 않고 명확히 구별되어야 합니다.
3. 각 그룹의 특징을 가장 잘 설명하는 단어를 선택하세요.
4. 광고의 상세 내용(제품유형, 솔루션 등) 자체가 아닌 후킹 메커니즘을 분석하세요.

[광고 그룹 데이터]
{all_samples_text}
"""
        
        cluster_info = []
        try:
            res = client.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=naming_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AllClusterNames,
                    temperature=0.2,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            parsed_names = res.parsed.names if res.parsed else []
            
            for i, base in enumerate(cluster_base_info):
                name_data = parsed_names[i] if i < len(parsed_names) else None
                type_name = name_data.type_name if name_data else f"분석 유형 {i+1}"
                
                cluster_info.append({
                    "rank": i + 1,
                    "type_name": type_name,
                    "representative_text": base["rep_text"],
                    "count": base["count"],
                    "library_ids": base["library_ids"]
                })
        except Exception as e:
            logger.error("Global cluster naming failed: %s", e, exc_info=True)
            for i, base in enumerate(cluster_base_info):
                cluster_info.append({
                    "rank": i + 1,
                    "type_name": f"유형 {i+1}",
                    "representative_text": base["rep_text"],
                    "count": base["count"],
                    "library_ids": base["library_ids"]
                })

        # 7. count 기준 정렬
        cluster_info.sort(key=lambda x: x["count"], reverse=True)
        for i, info in enumerate(cluster_info):
            info["rank"] = i + 1

        result = {
            "total_count": len(rows),
            "items": cluster_info
        }

        # 8. 캐시 저장
        conn.execute(
            """
            INSERT INTO hooking_analysis_cache (workspace_id, category, result_json, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (workspace_id, category) DO UPDATE SET
                result_json = EXCLUDED.result_json,
                updated_at = now()
            """,
            (workspace_id, category, json.dumps(result))
        )
        conn.commit()

        return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
