from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from jwt import InvalidTokenError, PyJWKClient, decode as jwt_decode
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from ad_embedding_worker import SCHEMA_SQL as EMBEDDING_SCHEMA_SQL
from cron_runner import SCHEMA_SQL, _database_url, _ensure_default_workspace, sync_competitor
from meta_ads_crawler import search_meta_advertisers


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


class SavedAdRequest(BaseModel):
    library_id: str


def _split_csv_env(name: str) -> List[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _to_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def _init_db() -> int:
    with _connect() as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(EMBEDDING_SCHEMA_SQL)
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
    offset: int = 0
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
                SELECT id, folder_id, page_id, brand, brand_logo_url, monitoring_enabled, created_at
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
                GROUP BY wa.competitor_id
            ) stats ON stats.competitor_id = c.id
            ORDER BY c.created_at DESC
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
        where_clause = "wa.workspace_id = %s"
        params = [workspace_id]
        if status in ("running", "ended"):
            where_clause += " AND wa.status = %s"
            params.append(status)

        # 공통 쿼리 베이스 정의
        query_base = f"""
            SELECT
                a.library_id, wa.competitor_id, a.page_id, a.brand, a.brand_logo_url,
                wa.status, a.body, a.link_title, a.link_url, a.start_date_text,
                a.start_date, a.ad_format, a.same_source_library_ids, a.meta_library_url,
                wa.first_seen_at, wa.last_seen_at, wa.ended_at,
                a.similar_count, a.similar_library_ids,
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
                similars = ad.get("similar_library_ids") or []
                if isinstance(similars, list):
                    for sid in similars:
                        excluded_ids.add(str(sid))
            
            total_count = len(deduped_ads)
            ads = deduped_ads[offset : offset + limit]
        else:
            # 기존 방식: DB에서 개수 조회 및 페이징 처리
            total_row = conn.execute(
                f"SELECT COUNT(*) as count FROM workspace_meta_ads wa WHERE {where_clause}",
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

    payload = {
        "workspace_id": workspace_id,
        "folders": folders,
        "competitors": competitors,
        "ads": ads,
        "total_count": total_count,
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
            row = conn.execute(
                """
                INSERT INTO competitors (
                    workspace_id, folder_id, page_id, brand, brand_logo_url,
                    representative_library_id, search_query, created_by_user_id, created_by_app_user_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, page_id) DO UPDATE SET
                    folder_id = EXCLUDED.folder_id,
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
    if payload.monitoring_enabled is not None:
        updates.append("monitoring_enabled = %s")
        values.append(payload.monitoring_enabled)
    if payload.folder_id is not None:
        updates.append("folder_id = %s")
        values.append(payload.folder_id)
    if not updates:
        raise HTTPException(status_code=400, detail="no updates")
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
            WHERE a.library_id = %s OR a.library_id = TRIM(%s)
            """,
            (workspace_id, workspace_id, library_id, library_id),
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
        variation_ids = list(set((ad.get("same_source_library_ids") or []) + (ad.get("similar_library_ids") or [])))
        if variation_ids:
            variation_rows = conn.execute(
                """
                SELECT 
                    a.library_id, a.status, a.start_date, a.page_id,
                    v.video_url,
                    i.url as image_url,
                    COALESCE(i.url, v.video_url) as thumbnail_url
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
                WHERE a.library_id = ANY(%s)
                """,
                (variation_ids,),
            ).fetchall()
            variations = variation_rows

    return {
        "ad": ad,
        "extractions": extractions,
        "variations": variations
    }


@app.get("/api/auth-config")
def auth_config() -> dict[str, Any]:
    return {
        "enabled": _clerk_auth_enabled(),
        "publishable_key": os.getenv("CLERK_PUBLISHABLE_KEY", "").strip()
        or os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "").strip(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
