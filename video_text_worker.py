import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import psycopg
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel
from psycopg.rows import dict_row


# Setup logging
def _setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


LOGGER = logging.getLogger("adfungus-video-worker")


# Environment
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


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


# Constants
MODEL_NAME = "gemini-2.5-flash-lite"
VIDEO_MAX_BYTES = 50 * 1024 * 1024  # 50MB limit
MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta_ad_video_extractions (
    media_id BIGINT PRIMARY KEY REFERENCES meta_ad_media(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT 'video_sections_v1',
    status TEXT NOT NULL DEFAULT 'pending',
    audio_transcript TEXT,
    screen_text TEXT,
    section_types TEXT[] NOT NULL DEFAULT '{}',
    hooking_audio JSONB NOT NULL DEFAULT '[]'::jsonb,
    hooking_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb,
    hooking_visual_direction TEXT,
    hooking_categories TEXT[] NOT NULL DEFAULT '{}',
    body_audio JSONB NOT NULL DEFAULT '[]'::jsonb,
    body_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb,
    closing_audio JSONB NOT NULL DEFAULT '[]'::jsonb,
    closing_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb,
    closing_cta TEXT,
    result_json JSONB,
    error TEXT,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS prompt_version TEXT NOT NULL DEFAULT 'video_sections_v1';
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS section_types TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS hooking_audio JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS hooking_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS hooking_visual_direction TEXT;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS hooking_categories TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS body_audio JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS body_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS closing_audio JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS closing_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meta_ad_video_extractions ADD COLUMN IF NOT EXISTS closing_cta TEXT;

CREATE INDEX IF NOT EXISTS idx_meta_ad_video_extractions_status
    ON meta_ad_video_extractions(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_meta_ad_video_extractions_library_id
    ON meta_ad_video_extractions(library_id);
CREATE INDEX IF NOT EXISTS idx_meta_ad_video_extractions_section_types
    ON meta_ad_video_extractions USING GIN (section_types);
CREATE INDEX IF NOT EXISTS idx_meta_ad_video_extractions_hooking_categories
    ON meta_ad_video_extractions USING GIN (hooking_categories);
"""

GOOGLE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS google_ad_video_extractions (
    extraction_key TEXT PRIMARY KEY,
    representative_media_id BIGINT REFERENCES google_ad_media(id) ON DELETE SET NULL,
    youtube_id TEXT,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT 'video_sections_v1',
    status TEXT NOT NULL DEFAULT 'pending',
    audio_transcript TEXT,
    screen_text TEXT,
    section_types TEXT[] NOT NULL DEFAULT '{}',
    hooking_audio JSONB NOT NULL DEFAULT '[]'::jsonb,
    hooking_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb,
    hooking_visual_direction TEXT,
    hooking_categories TEXT[] NOT NULL DEFAULT '{}',
    body_audio JSONB NOT NULL DEFAULT '[]'::jsonb,
    body_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb,
    closing_audio JSONB NOT NULL DEFAULT '[]'::jsonb,
    closing_screen_text JSONB NOT NULL DEFAULT '[]'::jsonb,
    closing_cta TEXT,
    result_json JSONB,
    error TEXT,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_google_ad_video_extractions_status
    ON google_ad_video_extractions(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_google_ad_video_extractions_youtube_id
    ON google_ad_video_extractions(youtube_id);
CREATE INDEX IF NOT EXISTS idx_google_ad_video_extractions_section_types
    ON google_ad_video_extractions USING GIN (section_types);
CREATE INDEX IF NOT EXISTS idx_google_ad_video_extractions_hooking_categories
    ON google_ad_video_extractions USING GIN (hooking_categories);
"""


# Pydantic Response Schema
class HookingCategory(str, Enum):
    PAIN_POINT = "페인포인트"
    BEFORE_AFTER = "비포애프터"
    PRODUCT_COMPARISON = "제품비교"
    AUTHORITY = "권위"
    THREAT = "위협"
    CURIOSITY = "호기심"
    PARADOX_IRONY = "역설/반어"
    ENLIGHTENMENT = "계몽"
    BENEFIT = "혜택"
    PRODUCT_EFFECT = "제품효과"
    URGENCY = "긴급성"

    @property
    def description(self) -> str:
        return {
            HookingCategory.PAIN_POINT: "사용자의 불편함이나 고민을 건드려 공감대를 만들고 해결책을 제시하는 방식",
            HookingCategory.BEFORE_AFTER: "사용 전후의 극적인 변화, 효과, 생활 차이를 보여주는 방식",
            HookingCategory.PRODUCT_COMPARISON: "타사 제품이나 기존 대안과 비교해 우위를 직접 드러내는 방식",
            HookingCategory.AUTHORITY: "전문가, 1위 기록, 인증 등을 통해 신뢰도를 높이는 방식",
            HookingCategory.THREAT: "문제를 방치했을 때의 위험성을 강조해 경각심을 주는 방식",
            HookingCategory.CURIOSITY: "사람들의 궁금증, 호기심을 자극하는 방식",
            HookingCategory.PARADOX_IRONY: "기존 상식과 반대되거나 모순적으로 들리는 표현으로 시선을 잡는 방식",
            HookingCategory.ENLIGHTENMENT: "사람들이 몰랐던 사실, 원리, 꿀팁을 알려주며 깨달음을 주는 방식",
            HookingCategory.BENEFIT: "할인, 1+1, 증정 등 구매 이득을 직접적으로 강조하는 방식",
            HookingCategory.PRODUCT_EFFECT: "제품의 핵심 기능이나 눈에 띄는 효과를 직접적으로 보여주거나 강조하는 방식",
            HookingCategory.URGENCY: "마감 임박, 수량 한정 등을 강조해 즉각적인 행동을 유도하는 방식",
        }.get(self, "")


class TimelineItem(BaseModel):
    time_range: str
    text: str


class HookingSection(BaseModel):
    audio: list[TimelineItem]
    screen_text: list[TimelineItem]
    visual_direction: str
    categories: list[HookingCategory]


class BodySection(BaseModel):
    audio: list[TimelineItem]
    screen_text: list[TimelineItem]


class ClosingSection(BaseModel):
    audio: list[TimelineItem]
    screen_text: list[TimelineItem]
    cta: str


class VideoSections(BaseModel):
    hooking: HookingSection
    body: BodySection
    closing: ClosingSection


class VideoExtractionResponse(BaseModel):
    sections: VideoSections


VIDEO_TEXT_EXTRACTION_PROMPT = f"""
?뱀떊? 愿묎퀬 遺꾩꽍 ?꾨Ц媛?낅땲?? 二쇱뼱吏?愿묎퀬 ?곸긽??遺꾩꽍?섏뿬 ?蹂?Audio Transcript)怨??붾㈃???섑??섎뒗 ?띿뒪??Screen Text)瑜?異붿텧?섍퀬, 愿묎퀬??援ъ“瑜?3?④퀎(Hooking, Body, Closing)濡?遺꾨쪟?섏뿬 JSON ?뺤떇?쇰줈 ?묐떟?섏꽭??

紐⑤뱺 ?묐떟? ?쒓뎅?대줈 ?묒꽦?댁빞 ?⑸땲??

1. **Hooking (愿묎퀬???꾩엯遺, 蹂댄넻 0~5珥??댁쇅)**
   - ?쒖껌?먯쓽 ?대ぉ???꾨뒗 ?ㅻ뵒?ㅼ? ?붾㈃ ?띿뒪?몃? 紐⑤몢 湲곗옱?섏꽭??
   - `visual_direction`: ??援ш컙???쒓컖???곗텧 ?뱀쭠???ㅻ챸?섏꽭??
   - `categories`: ?ㅼ쓬 移댄뀒怨좊━ ?ㅻ챸??李멸퀬?섏뿬 媛???곸젅????ぉ?ㅼ쓣 由ъ뒪?몃줈 ?좏깮?섏꽭??
{chr(10).join([f"     - {c.value}: {c.description}" for c in HookingCategory])}

2. **Body (蹂몃줎)**
   - ?쒗뭹???뱀쭠?대굹 ?μ젏???ㅻ챸?섎뒗 援ш컙?낅땲??
   - ?ㅻ뵒???蹂멸낵 ?붾㈃ ?띿뒪?몃? ?쒓컙?蹂꾨줈 ?곸꽭??湲곗옱?섏꽭??

3. **Closing (留덈Т由?**
   - 援щℓ ?좊룄??釉뚮옖??濡쒓퀬媛 ?몄텧?섎뒗 留덉?留?援ш컙?낅땲??
   - `cta`: 留덉?留됱뿉 ?좊룄?섎뒗 ?됰룞(Call to Action) 臾멸뎄瑜?異붿텧?섏꽭?? (?? 吏湲?援щℓ?섍린, ?꾨줈??留곹겕 ?대┃ ??

**二쇱쓽?ы빆:**
- 媛??띿뒪????ぉ?먮뒗 諛섎뱶??`time_range` (?? "00:01 ~ 00:03")瑜??ы븿?댁빞 ?⑸땲??
- 留뚯빟 ?뱀젙 援ш컙???ㅻ뵒?ㅻ굹 ?띿뒪?멸? ?녿떎硫?鍮?由ъ뒪??`[]`)瑜?諛섑솚?섏꽭??
- ?붾㈃??濡쒓퀬???쒗뭹紐낅쭔 ?꾩＜ ?좉퉸 吏?섍???寃쎌슦?먮룄 理쒕???`screen_text`???ы븿?쒗궎?몄슂.
- 紐⑤뱺 ?쒓컙 ?뺤떇? `MM:SS` ?뺤떇???곕Ⅴ硫? ?곸긽???꾩껜 湲몄씠瑜?怨좊젮?섏꽭??
"""


def _setup_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(SCHEMA_SQL)


def _setup_google_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(GOOGLE_SCHEMA_SQL)


def _library_ids_for_run(conn: psycopg.Connection[Any], run_id: int) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT library_id
            FROM meta_ads
            WHERE first_seen_run_id = %s
            ORDER BY library_id ASC
            """,
            (run_id,),
        )
        return [str(row[0]) for row in cur.fetchall() if str(row[0]).strip()]


def _seed_pending_videos(
    conn: psycopg.Connection[Any],
    library_ids: Sequence[str] | None = None,
) -> None:
    params: list[Any] = [MODEL_NAME]
    library_filter = ""
    if library_ids is not None:
        params.append(list(library_ids))
        library_filter = "AND library_id = ANY(%s)"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO meta_ad_video_extractions (media_id, library_id, model)
            SELECT id, library_id, %s
            FROM meta_ad_media
            WHERE media_type = 'video'
              AND COALESCE(url, '') <> ''
              {library_filter}
            ON CONFLICT (media_id) DO NOTHING
            """,
            params,
        )


def _claim_videos(
    conn: psycopg.Connection[Any],
    limit: int | None = None,
    library_ids: Sequence[str] | None = None,
    only_leaders: bool = False,
    only_followers: bool = False,
) -> List[Dict[str, Any]]:
    params: list[Any] = []
    filters = [
        "e.status IN ('pending', 'failed')",
        "m.media_type = 'video'",
        "COALESCE(m.url, '') <> ''",
    ]

    if library_ids is not None:
        params.append(list(library_ids))
        filters.append("m.library_id = ANY(%s)")

    if only_leaders:
        # A leader is when its library_id is the minimum (first) in same_source_library_ids
        # Handle both old [ "ID", ... ] and new [ {"id": "ID"}, ... ] formats
        filters.append("""
            (CASE 
                WHEN jsonb_typeof(a.same_source_library_ids->0) = 'object' THEN a.same_source_library_ids->0->>'id'
                ELSE a.same_source_library_ids->>0
             END) = e.library_id
        """)
    elif only_followers:
        filters.append("""
            (CASE 
                WHEN jsonb_typeof(a.same_source_library_ids->0) = 'object' THEN a.same_source_library_ids->0->>'id'
                ELSE a.same_source_library_ids->>0
             END) <> e.library_id
        """)

    limit_sql = ""
    if limit is not None:
        params.append(limit)
        limit_sql = "LIMIT %s"

    with conn.cursor(row_factory=dict_row) as cur:
        query = f"""
            SELECT e.media_id, e.library_id, m.url, m.content_type, m.byte_size, a.same_source_library_ids
            FROM meta_ad_video_extractions e
            JOIN meta_ad_media m ON m.id = e.media_id
            JOIN meta_ads a ON a.library_id = e.library_id
            WHERE {" AND ".join(filters)}
            ORDER BY e.updated_at ASC, e.media_id ASC
            {limit_sql}
            FOR UPDATE OF e SKIP LOCKED
        """
        cur.execute(query, params)
        rows = list(cur.fetchall())

        if not rows:
            return []

        media_ids = [row["media_id"] for row in rows]
        cur.execute(
            """
            UPDATE meta_ad_video_extractions
            SET status = 'processing', error = NULL, updated_at = now(), model = %s
            WHERE media_id = ANY(%s)
            """,
            (MODEL_NAME, media_ids),
        )
        return rows


def _mark_success(
    conn: psycopg.Connection[Any],
    media_id: int,
    *,
    audio_transcript: Optional[str],
    screen_text: Optional[str],
    section_types: List[str],
    hooking_audio: List[Dict[str, Any]],
    hooking_screen_text: List[Dict[str, Any]],
    hooking_visual_direction: Optional[str],
    hooking_categories: List[str],
    body_audio: List[Dict[str, Any]],
    body_screen_text: List[Dict[str, Any]],
    closing_audio: List[Dict[str, Any]],
    closing_screen_text: List[Dict[str, Any]],
    closing_cta: Optional[str],
    result_json: Optional[Dict[str, Any]],
) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE meta_ad_video_extractions
            SET status = 'success',
                audio_transcript = %s,
                screen_text = %s,
                section_types = %s,
                hooking_audio = %s,
                hooking_screen_text = %s,
                hooking_visual_direction = %s,
                hooking_categories = %s,
                body_audio = %s,
                body_screen_text = %s,
                closing_audio = %s,
                closing_screen_text = %s,
                closing_cta = %s,
                result_json = %s,
                processed_at = now(),
                updated_at = now()
            WHERE media_id = %s
            """,
            (
                audio_transcript,
                screen_text,
                section_types,
                Jsonb(hooking_audio),
                Jsonb(hooking_screen_text),
                hooking_visual_direction,
                hooking_categories,
                Jsonb(body_audio),
                Jsonb(body_screen_text),
                Jsonb(closing_audio),
                Jsonb(closing_screen_text),
                closing_cta,
                Jsonb(result_json) if result_json else None,
                media_id,
            ),
        )


def _mark_failed(conn: psycopg.Connection[Any], media_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE meta_ad_video_extractions
            SET status = 'failed', error = %s, updated_at = now()
            WHERE media_id = %s
            """,
            (error, media_id),
        )


def _download_video(url: str) -> Path:
    response = requests.get(url, headers=MEDIA_HEADERS, timeout=60, stream=True)
    response.raise_for_status()

    max_bytes = VIDEO_MAX_BYTES
    total = 0
    fd, path_str = tempfile.mkstemp(suffix=".mp4")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(
                            f"video exceeded VIDEO_MAX_BYTES={max_bytes}"
                        )
                    f.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _wait_for_file_ready(client: genai.Client, file_name: str) -> Any:
    for _ in range(30):
        f = client.files.get(name=file_name)
        if f.state.name == "ACTIVE":
            return f
        if f.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {f.name}")
        time.sleep(10)
    raise RuntimeError(f"Gemini file processing timeout: {file_name}")


def _flatten_timeline(items: List[TimelineItem]) -> str:
    values: List[str] = []
    seen = set()
    for item in items:
        text = item.text.strip()
        if not text or text in seen:
            continue
        values.append(text)
        seen.add(text)
    return " ".join(values)


def _json_from_response_text(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned, strict=False)
        return parsed
    except json.JSONDecodeError:
        # Fallback: find the first { and last }
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(cleaned[start : end + 1], strict=False)
            except json.JSONDecodeError:
                pass
        raise


def _extract_video_text(client: genai.Client, path: Path) -> Dict[str, Any]:
    # 1. Upload to Gemini
    # Use positional argument or 'file' depending on SDK version. The error was 'path' is unexpected.
    uploaded = client.files.upload(file=path)
    try:
        # 2. Wait for processing
        _wait_for_file_ready(client, uploaded.name)

        # 3. Generate content with structured output
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[uploaded, VIDEO_TEXT_EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoExtractionResponse,
                max_output_tokens=6000,
                temperature=0,
            ),
        )

        parsed_response = getattr(response, "parsed", None)
        if parsed_response is not None:
            if hasattr(parsed_response, "model_dump"):
                data = parsed_response.model_dump(mode="json")
            elif isinstance(parsed_response, dict):
                data = parsed_response
            else:
                raise RuntimeError(
                    f"Gemini returned unsupported parsed response type: {type(parsed_response).__name__}"
                )

        else:
            if not response.text:
                raise RuntimeError("Gemini returned empty response")
            data = _json_from_response_text(response.text)
        sections = data.get("sections", {})

        hooking = (
            sections.get("hooking") if isinstance(sections.get("hooking"), dict) else {}
        )
        body = sections.get("body") if isinstance(sections.get("body"), dict) else {}
        closing = (
            sections.get("closing") if isinstance(sections.get("closing"), dict) else {}
        )

        # Convert Pydantic-like dict structure to final result
        res = {
            "section_types": ["hooking", "body", "closing"],
            "hooking_audio": hooking.get("audio", []),
            "hooking_screen_text": hooking.get("screen_text", []),
            "hooking_visual_direction": hooking.get("visual_direction"),
            "hooking_categories": hooking.get("categories", []),
            "body_audio": body.get("audio", []),
            "body_screen_text": body.get("screen_text", []),
            "closing_audio": closing.get("audio", []),
            "closing_screen_text": closing.get("screen_text", []),
            "closing_cta": closing.get("cta"),
            "result_json": data,
        }

        # Combine transcripts for full text search
        full_audio = []
        full_screen = []
        for s in [hooking, body, closing]:
            full_audio.append(
                _flatten_timeline([TimelineItem(**i) for i in s.get("audio", [])])
            )
            full_screen.append(
                _flatten_timeline([TimelineItem(**i) for i in s.get("screen_text", [])])
            )

        res["audio_transcript"] = " ".join([t for t in full_audio if t]).strip() or None
        res["screen_text"] = " ".join([t for t in full_screen if t]).strip() or None

        return res
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as exc:
            LOGGER.warning(
                "failed to delete Gemini file %s: %s",
                getattr(uploaded, "name", ""),
                exc,
            )


def _process_video(
    conn: psycopg.Connection[Any], client: genai.Client, video: Dict[str, Any]
) -> bool:
    media_id = int(video["media_id"])
    library_id = str(video["library_id"])
    raw_same_source = video.get("same_source_library_ids") or []
    
    # Normalize same_source_ids to a list of strings (hybrid support)
    same_source_ids: List[str] = []
    for item in raw_same_source:
        if isinstance(item, dict):
            if "id" in item:
                same_source_ids.append(str(item["id"]))
        else:
            same_source_ids.append(str(item))

    path: Path | None = None
    try:
        # Check if any sibling already has a successful extraction
        if same_source_ids:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT * FROM meta_ad_video_extractions 
                    WHERE library_id = ANY(%s) AND status = 'success'
                    LIMIT 1
                    """,
                    (same_source_ids,),
                )
                sibling = cur.fetchone()
                if sibling:
                    LOGGER.info(
                        "copying video extraction from sibling library_id=%s to media_id=%s",
                        sibling["library_id"],
                        media_id,
                    )
                    _mark_success(
                        conn,
                        media_id,
                        audio_transcript=sibling["audio_transcript"],
                        screen_text=sibling["screen_text"],
                        section_types=sibling["section_types"],
                        hooking_audio=sibling["hooking_audio"],
                        hooking_screen_text=sibling["hooking_screen_text"],
                        hooking_visual_direction=sibling["hooking_visual_direction"],
                        hooking_categories=sibling["hooking_categories"],
                        body_audio=sibling["body_audio"],
                        body_screen_text=sibling["body_screen_text"],
                        closing_audio=sibling["closing_audio"],
                        closing_screen_text=sibling["closing_screen_text"],
                        closing_cta=sibling["closing_cta"],
                        result_json=sibling["result_json"],
                    )
                    conn.commit()
                    return True

        # Determine leader
        if same_source_ids:
            # Sort IDs as strings to find the minimum (the leader)
            sorted_ids = sorted(same_source_ids)
            leader_id = sorted_ids[0]
            if library_id != leader_id:
                # If we are here, it means the leader hasn't finished yet.
                # Defer this follower.
                raise RuntimeError(
                    f"Deferring extraction: waiting for leader {leader_id}"
                )

        # Start actual extraction (Only for leaders or unique videos)
        path = _download_video(str(video["url"]))
        result = _extract_video_text(client, path)
        _mark_success(
            conn,
            media_id,
            audio_transcript=result["audio_transcript"],
            screen_text=result["screen_text"],
            section_types=result["section_types"],
            hooking_audio=result["hooking_audio"],
            hooking_screen_text=result["hooking_screen_text"],
            hooking_visual_direction=result["hooking_visual_direction"],
            hooking_categories=result["hooking_categories"],
            body_audio=result["body_audio"],
            body_screen_text=result["body_screen_text"],
            closing_audio=result["closing_audio"],
            closing_screen_text=result["closing_screen_text"],
            closing_cta=result["closing_cta"],
            result_json=result["result_json"],
        )
        conn.commit()
        LOGGER.info("processed video media_id=%s library_id=%s", media_id, library_id)
        return True
    except Exception as exc:
        conn.rollback()
        _mark_failed(conn, media_id, str(exc))
        conn.commit()
        LOGGER.error(
            "video extraction failed media_id=%s library_id=%s: %s",
            media_id,
            library_id,
            exc,
        )
        return False
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def process_pending_videos(
    *,
    database_url: str,
    limit: int | None = None,
    library_ids: Sequence[str] | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
) -> tuple[int, int]:
    if library_ids is not None:
        library_ids = [
            str(value).strip()
            for value in dict.fromkeys(library_ids)
            if str(value).strip()
        ]
        if not library_ids:
            LOGGER.info("no library ids to process")
            return 0, 0

    total_successes = 0
    total_found = 0

    gemini_api_key = _required_env("GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_api_key)

    # We run in two phases:
    # Phase 1: Leaders (The primary ad for a unique video)
    # Phase 2: Followers (Ads sharing the same video as a leader)
    for phase_name, only_leaders, only_followers in [
        ("Leaders", True, False),
        ("Followers", False, True),
    ]:
        with psycopg.connect(database_url, autocommit=False) as conn:
            _setup_schema(conn)
            _seed_pending_videos(conn, library_ids=library_ids)
            videos = _claim_videos(
                conn,
                limit=limit,
                library_ids=library_ids,
                only_leaders=only_leaders,
                only_followers=only_followers,
            )

            if dry_run:
                conn.rollback()
                LOGGER.info(
                    "[%s] dry run would process %s videos", phase_name, len(videos)
                )
                total_found += len(videos)
                continue

            conn.commit()

        if not videos:
            LOGGER.info("[%s] no videos to process", phase_name)
            continue

        LOGGER.info("[%s] processing %s videos...", phase_name, len(videos))
        total_found += len(videos)
        phase_successes = 0

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:

            def _worker_task(v: Dict[str, Any]) -> bool:
                try:
                    with psycopg.connect(database_url, autocommit=False) as worker_conn:
                        return _process_video(worker_conn, client, v)
                except Exception as e:
                    LOGGER.error(
                        "Worker task failed for media_id=%s: %s", v["media_id"], e
                    )
                    return False

            futures = [executor.submit(_worker_task, v) for v in videos]
            for future in as_completed(futures):
                if future.result():
                    phase_successes += 1

        total_successes += phase_successes
        LOGGER.info(
            "[%s] finished: %s/%s succeeded", phase_name, phase_successes, len(videos)
        )

    return total_successes, total_found


def _mark_google_success(
    conn: psycopg.Connection[Any],
    extraction_key: str,
    *,
    audio_transcript: Optional[str],
    screen_text: Optional[str],
    section_types: List[str],
    hooking_audio: List[Dict[str, Any]],
    hooking_screen_text: List[Dict[str, Any]],
    hooking_visual_direction: Optional[str],
    hooking_categories: List[str],
    body_audio: List[Dict[str, Any]],
    body_screen_text: List[Dict[str, Any]],
    closing_audio: List[Dict[str, Any]],
    closing_screen_text: List[Dict[str, Any]],
    closing_cta: Optional[str],
    result_json: Optional[Dict[str, Any]],
) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE google_ad_video_extractions
            SET status = 'success',
                audio_transcript = %s,
                screen_text = %s,
                section_types = %s,
                hooking_audio = %s,
                hooking_screen_text = %s,
                hooking_visual_direction = %s,
                hooking_categories = %s,
                body_audio = %s,
                body_screen_text = %s,
                closing_audio = %s,
                closing_screen_text = %s,
                closing_cta = %s,
                result_json = %s,
                processed_at = now(),
                updated_at = now()
            WHERE extraction_key = %s
            """,
            (
                audio_transcript,
                screen_text,
                section_types,
                Jsonb(hooking_audio),
                Jsonb(hooking_screen_text),
                hooking_visual_direction,
                hooking_categories,
                Jsonb(body_audio),
                Jsonb(body_screen_text),
                Jsonb(closing_audio),
                Jsonb(closing_screen_text),
                closing_cta,
                Jsonb(result_json) if result_json else None,
                extraction_key,
            ),
        )


def _mark_google_failed(
    conn: psycopg.Connection[Any], extraction_key: str, error: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE google_ad_video_extractions
            SET status = 'failed', error = %s, updated_at = now()
            WHERE extraction_key = %s
            """,
            (error, extraction_key),
        )


def _claim_google_videos(
    conn: psycopg.Connection[Any],
    limit: int | None = None,
    creative_ids: Sequence[str] | None = None,
) -> List[Dict[str, Any]]:
    params: list[Any] = []
    creative_filter = ""
    if creative_ids is not None:
        params.append(list(creative_ids))
        creative_filter = "AND m.creative_id = ANY(%s)"

    limit_sql = ""
    if limit is not None:
        params.append(limit)
        limit_sql = "LIMIT %s"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT e.extraction_key, m.creative_id, m.url, m.youtube_id
            FROM google_ad_video_extractions e
            JOIN google_ad_media m ON m.id = e.representative_media_id
            WHERE e.status IN ('pending', 'failed')
              AND m.media_type = 'video'
              AND COALESCE(m.url, '') <> ''
              {creative_filter}
            ORDER BY e.updated_at ASC, e.extraction_key ASC
            {limit_sql}
            FOR UPDATE OF e SKIP LOCKED
            """,
            params,
        )
        rows = list(cur.fetchall())

        if not rows:
            return []

        keys = [row["extraction_key"] for row in rows]
        cur.execute(
            """
            UPDATE google_ad_video_extractions
            SET status = 'processing', error = NULL, updated_at = now(), model = %s
            WHERE extraction_key = ANY(%s)
            """,
            (MODEL_NAME, keys),
        )
        return rows


def _process_google_video(
    conn: psycopg.Connection[Any], client: genai.Client, video: Dict[str, Any]
) -> bool:
    extraction_key = str(video["extraction_key"])
    path: Path | None = None
    try:
        path = _download_video(str(video["url"]))
        result = _extract_video_text(client, path)
        _mark_google_success(
            conn,
            extraction_key,
            audio_transcript=result["audio_transcript"],
            screen_text=result["screen_text"],
            section_types=result["section_types"],
            hooking_audio=result["hooking_audio"],
            hooking_screen_text=result["hooking_screen_text"],
            hooking_visual_direction=result["hooking_visual_direction"],
            hooking_categories=result["hooking_categories"],
            body_audio=result["body_audio"],
            body_screen_text=result["body_screen_text"],
            closing_audio=result["closing_audio"],
            closing_screen_text=result["closing_screen_text"],
            closing_cta=result["closing_cta"],
            result_json=result["result_json"],
        )
        conn.commit()
        LOGGER.info(
            "processed google video extraction_key=%s creative_id=%s",
            extraction_key,
            video["creative_id"],
        )
        return True
    except Exception as exc:
        conn.rollback()
        _mark_google_failed(conn, extraction_key, str(exc))
        conn.commit()
        LOGGER.exception(
            "google video extraction failed extraction_key=%s creative_id=%s",
            extraction_key,
            video.get("creative_id"),
        )
        return False
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def process_pending_google_videos(
    *,
    database_url: str,
    limit: int | None = None,
    creative_ids: Sequence[str] | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
) -> tuple[int, int]:
    if creative_ids is not None:
        creative_ids = [
            str(value).strip()
            for value in dict.fromkeys(creative_ids)
            if str(value).strip()
        ]
        if not creative_ids:
            LOGGER.info("no google creative ids to process")
            return 0, 0

    with psycopg.connect(database_url, autocommit=False) as conn:
        _setup_google_schema(conn)
        # Seeding logic for google ads is usually handled elsewhere (google_cron_runner)
        videos = _claim_google_videos(conn, limit=limit, creative_ids=creative_ids)
        if dry_run:
            conn.rollback()
            LOGGER.info("dry run would process %s google videos", len(videos))
            return 0, len(videos)
        conn.commit()

        if not videos:
            LOGGER.info("no pending google videos to process")
            return 0, 0

        gemini_api_key = _required_env("GEMINI_API_KEY")
        client = genai.Client(api_key=gemini_api_key)

        successes = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:

            def _worker_task(v: Dict[str, Any]) -> bool:
                try:
                    with psycopg.connect(database_url, autocommit=False) as worker_conn:
                        return _process_google_video(worker_conn, client, v)
                except Exception as e:
                    LOGGER.error(
                        "Worker task failed for google extraction_key=%s: %s",
                        v["extraction_key"],
                        e,
                    )
                    return False

            futures = [executor.submit(_worker_task, video) for video in videos]
            for future in as_completed(futures):
                if future.result():
                    successes += 1

    LOGGER.info(
        "processed %s/%s google videos with concurrency=%s",
        successes,
        len(videos),
        concurrency,
    )
    return successes, len(videos)


def main():
    _load_local_env()
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Extract audio and screen text from video ads."
    )
    parser.add_argument("--limit", type=int, default=_int_env("VIDEO_WORKER_LIMIT", 0))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id", type=int, help="Only process ads first seen in this crawl run.")
    args = parser.parse_args()

    database_url = _required_env("DATABASE_URL")
    concurrency = _int_env("VIDEO_CONCURRENCY", 10)
    library_ids = None

    if args.run_id is not None:
        with psycopg.connect(database_url, autocommit=False) as conn:
            library_ids = _library_ids_for_run(conn, args.run_id)
        LOGGER.info("video worker run_id=%s library_ids=%s", args.run_id, len(library_ids))

    success, total = process_pending_videos(
        database_url=database_url,
        limit=args.limit if args.limit > 0 else None,
        dry_run=args.dry_run,
        concurrency=concurrency,
        library_ids=library_ids,
    )
    LOGGER.info("Meta video processing finished: %s/%s successes", success, total)


if __name__ == "__main__":
    main()

