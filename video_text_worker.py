from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

import psycopg
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from video_prompts import VIDEO_TEXT_EXTRACTION_PROMPT


LOGGER = logging.getLogger("adfungus-video-worker")
MODEL_NAME = "gemini-2.5-flash-lite"
VIDEO_TIMEOUT_S = 120
DEFAULT_VIDEO_MAX_BYTES = 250 * 1024 * 1024

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


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _setup_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(SCHEMA_SQL)


def _setup_google_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(GOOGLE_SCHEMA_SQL)


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
) -> List[Dict[str, Any]]:
    params: list[Any] = []
    library_filter = ""
    if library_ids is not None:
        params.append(list(library_ids))
        library_filter = "AND m.library_id = ANY(%s)"

    limit_sql = ""
    if limit is not None:
        params.append(limit)
        limit_sql = "LIMIT %s"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT e.media_id, e.library_id, m.url, m.content_type, m.byte_size
            FROM meta_ad_video_extractions e
            JOIN meta_ad_media m ON m.id = e.media_id
            WHERE e.status IN ('pending', 'failed')
              AND m.media_type = 'video'
              AND COALESCE(m.url, '') <> ''
              {library_filter}
            ORDER BY e.updated_at ASC, e.media_id ASC
            {limit_sql}
            FOR UPDATE OF e SKIP LOCKED
            """,
            params,
        )
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
    audio_transcript: str,
    screen_text: str,
    section_types: List[str],
    hooking_audio: List[Dict[str, Any]],
    hooking_screen_text: List[Dict[str, Any]],
    hooking_visual_direction: str,
    hooking_categories: List[str],
    body_audio: List[Dict[str, Any]],
    body_screen_text: List[Dict[str, Any]],
    closing_audio: List[Dict[str, Any]],
    closing_screen_text: List[Dict[str, Any]],
    closing_cta: str,
    result_json: Dict[str, Any],
) -> None:
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
                error = NULL,
                processed_at = now(),
                updated_at = now(),
                model = %s,
                prompt_version = 'video_sections_v1'
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
                Jsonb(result_json),
                MODEL_NAME,
                media_id,
            ),
        )


def _mark_failed(conn: psycopg.Connection[Any], media_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE meta_ad_video_extractions
            SET status = 'failed', error = %s, updated_at = now(), model = %s
            WHERE media_id = %s
            """,
            (error[:4000], MODEL_NAME, media_id),
        )


def _seed_pending_google_videos(
    conn: psycopg.Connection[Any],
    creative_ids: Sequence[str] | None = None,
) -> None:
    params: list[Any] = [MODEL_NAME]
    creative_filter = ""
    if creative_ids is not None:
        params.append(list(creative_ids))
        creative_filter = "AND creative_id = ANY(%s)"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO google_ad_video_extractions (
                extraction_key, representative_media_id, youtube_id, model
            )
            SELECT DISTINCT ON (COALESCE('youtube:' || NULLIF(youtube_id, ''), 'media:' || id::text))
                COALESCE('youtube:' || NULLIF(youtube_id, ''), 'media:' || id::text) AS extraction_key,
                id,
                youtube_id,
                %s
            FROM google_ad_media
            WHERE media_type = 'video'
              AND COALESCE(url, '') <> ''
              {creative_filter}
            ORDER BY COALESCE('youtube:' || NULLIF(youtube_id, ''), 'media:' || id::text), created_at ASC, id ASC
            ON CONFLICT (extraction_key) DO UPDATE SET
                representative_media_id = COALESCE(google_ad_video_extractions.representative_media_id, EXCLUDED.representative_media_id),
                youtube_id = COALESCE(google_ad_video_extractions.youtube_id, EXCLUDED.youtube_id),
                updated_at = google_ad_video_extractions.updated_at
            """,
            params,
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
            SELECT e.extraction_key, e.representative_media_id AS media_id,
                   e.youtube_id, m.creative_id, m.url, m.content_type, m.byte_size
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

        extraction_keys = [row["extraction_key"] for row in rows]
        cur.execute(
            """
            UPDATE google_ad_video_extractions
            SET status = 'processing', error = NULL, updated_at = now(), model = %s
            WHERE extraction_key = ANY(%s)
            """,
            (MODEL_NAME, extraction_keys),
        )
        return rows


def _mark_google_success(
    conn: psycopg.Connection[Any],
    extraction_key: str,
    *,
    audio_transcript: str,
    screen_text: str,
    section_types: List[str],
    hooking_audio: List[Dict[str, Any]],
    hooking_screen_text: List[Dict[str, Any]],
    hooking_visual_direction: str,
    hooking_categories: List[str],
    body_audio: List[Dict[str, Any]],
    body_screen_text: List[Dict[str, Any]],
    closing_audio: List[Dict[str, Any]],
    closing_screen_text: List[Dict[str, Any]],
    closing_cta: str,
    result_json: Dict[str, Any],
) -> None:
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
                error = NULL,
                processed_at = now(),
                updated_at = now(),
                model = %s,
                prompt_version = 'video_sections_v1'
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
                Jsonb(result_json),
                MODEL_NAME,
                extraction_key,
            ),
        )


def _mark_google_failed(conn: psycopg.Connection[Any], extraction_key: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE google_ad_video_extractions
            SET status = 'failed', error = %s, updated_at = now(), model = %s
            WHERE extraction_key = %s
            """,
            (error[:4000], MODEL_NAME, extraction_key),
        )


def _download_video(url: str) -> Path:
    max_bytes = max(_int_env("VIDEO_MAX_BYTES", DEFAULT_VIDEO_MAX_BYTES), 1)
    suffix = Path(url.split("?", 1)[0]).suffix or ".mp4"
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = Path(temp.name)
    temp.close()

    try:
        with requests.get(url, timeout=VIDEO_TIMEOUT_S, stream=True) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise RuntimeError(f"video too large: {content_length} bytes")

            total = 0
            with path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(f"video exceeded VIDEO_MAX_BYTES={max_bytes}")
                    file.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


from pydantic import BaseModel


class TimelineItem(BaseModel):
    time_range: str
    text: str


class HookingSection(BaseModel):
    audio: list[TimelineItem]
    screen_text: list[TimelineItem]
    visual_direction: str
    categories: list[str]


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


def _file_state(file_obj: Any) -> str:
    state = getattr(file_obj, "state", "")
    name = getattr(state, "name", state)
    return str(name).upper()


def _wait_for_file_ready(client: genai.Client, file_obj: Any) -> Any:
    deadline = time.monotonic() + _int_env("GEMINI_FILE_READY_TIMEOUT_S", 300)
    current = file_obj
    while time.monotonic() < deadline:
        state = _file_state(current)
        if state in {"ACTIVE", "SUCCEEDED", "READY"}:
            return current
        if state in {"FAILED", "ERROR"}:
            raise RuntimeError(f"Gemini file processing failed: {state}")
        time.sleep(3)
        current = client.files.get(name=current.name)
    raise RuntimeError("timed out waiting for Gemini file processing")


def _json_from_response_text(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    parsed = json.loads(cleaned, strict=False)
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini response JSON must be an object")
    return _normalize_extraction_result(parsed)


def _timeline_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or text in {"음성 없음", "텍스트 없음"}:
            continue
        items.append(
            {
                "time_range": str(item.get("time_range") or "").strip(),
                "text": text,
            }
        )
    return items


def _join_timeline_text(*groups: List[Dict[str, Any]]) -> str:
    values: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            text = str(item.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            values.append(text)
    return "\n".join(values)


def _normalize_categories(value: Any) -> List[str]:
    allowed = {"사과", "위협", "공감", "호기심", "반전", "정보제공", "혜택강조"}
    if not isinstance(value, list):
        return []
    categories: List[str] = []
    for item in value:
        category = str(item or "").strip()
        if category in allowed and category not in categories:
            categories.append(category)
    return categories[:2]


def _section_has_content(section: Dict[str, Any]) -> bool:
    return bool(
        section.get("audio")
        or section.get("screen_text")
        or str(section.get("visual_direction") or "").strip()
        or section.get("categories")
        or str(section.get("cta") or "").strip()
    )


def _normalize_extraction_result(parsed: Dict[str, Any]) -> Dict[str, Any]:
    sections = parsed.get("sections")
    if not isinstance(sections, dict):
        return {
            "audio_transcript": str(parsed.get("audio_transcript") or ""),
            "screen_text": str(parsed.get("screen_text") or ""),
            "section_types": [],
            "hooking_audio": [],
            "hooking_screen_text": [],
            "hooking_visual_direction": "",
            "hooking_categories": [],
            "body_audio": [],
            "body_screen_text": [],
            "closing_audio": [],
            "closing_screen_text": [],
            "closing_cta": "",
            "result_json": parsed,
        }

    hooking = sections.get("hooking") if isinstance(sections.get("hooking"), dict) else {}
    body = sections.get("body") if isinstance(sections.get("body"), dict) else {}
    closing = sections.get("closing") if isinstance(sections.get("closing"), dict) else {}

    hooking_audio = _timeline_items(hooking.get("audio"))
    hooking_screen_text = _timeline_items(hooking.get("screen_text"))
    body_audio = _timeline_items(body.get("audio"))
    body_screen_text = _timeline_items(body.get("screen_text"))
    closing_audio = _timeline_items(closing.get("audio"))
    closing_screen_text = _timeline_items(closing.get("screen_text"))

    normalized_sections = {
        "hooking": {
            "audio": hooking_audio,
            "screen_text": hooking_screen_text,
            "visual_direction": str(hooking.get("visual_direction") or "").strip(),
            "categories": _normalize_categories(hooking.get("categories")),
        },
        "body": {
            "audio": body_audio,
            "screen_text": body_screen_text,
        },
        "closing": {
            "audio": closing_audio,
            "screen_text": closing_screen_text,
            "cta": str(closing.get("cta") or "").strip(),
        },
    }
    normalized_json = dict(parsed)
    normalized_json["sections"] = normalized_sections

    return {
        "audio_transcript": _join_timeline_text(hooking_audio, body_audio, closing_audio),
        "screen_text": _join_timeline_text(hooking_screen_text, body_screen_text, closing_screen_text),
        "section_types": [
            section_type
            for section_type in ("hooking", "body", "closing")
            if _section_has_content(normalized_sections[section_type])
        ],
        "hooking_audio": hooking_audio,
        "hooking_screen_text": hooking_screen_text,
        "hooking_visual_direction": normalized_sections["hooking"]["visual_direction"],
        "hooking_categories": normalized_sections["hooking"]["categories"],
        "body_audio": body_audio,
        "body_screen_text": body_screen_text,
        "closing_audio": closing_audio,
        "closing_screen_text": closing_screen_text,
        "closing_cta": normalized_sections["closing"]["cta"],
        "result_json": normalized_json,
    }


def _extract_video_text(client: genai.Client, video_path: Path) -> Dict[str, Any]:
    uploaded = client.files.upload(file=video_path)
    try:
        uploaded = _wait_for_file_ready(client, uploaded)
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[uploaded, VIDEO_TEXT_EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoExtractionResponse,
                temperature=0,
            ),
        )
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise RuntimeError("Gemini returned empty response")
        return _json_from_response_text(text)
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as exc:
            LOGGER.warning("failed to delete Gemini file %s: %s", getattr(uploaded, "name", ""), exc)


def _process_video(conn: psycopg.Connection[Any], client: genai.Client, video: Dict[str, Any]) -> bool:
    media_id = int(video["media_id"])
    path: Path | None = None
    try:
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
        LOGGER.info("processed video media_id=%s library_id=%s", media_id, video["library_id"])
        return True
    except Exception as exc:
        conn.rollback()
        _mark_failed(conn, media_id, str(exc))
        conn.commit()
        LOGGER.exception("video extraction failed media_id=%s library_id=%s", media_id, video["library_id"])
        return False
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _process_google_video(conn: psycopg.Connection[Any], client: genai.Client, video: Dict[str, Any]) -> bool:
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
        LOGGER.info("processed google video extraction_key=%s creative_id=%s", extraction_key, video["creative_id"])
        return True
    except Exception as exc:
        conn.rollback()
        _mark_google_failed(conn, extraction_key, str(exc))
        conn.commit()
        LOGGER.exception("google video extraction failed extraction_key=%s creative_id=%s", extraction_key, video.get("creative_id"))
        return False
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract audio and screen text from video ads.")
    parser.add_argument("--limit", type=int, default=_int_env("VIDEO_WORKER_LIMIT", 10))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def process_pending_videos(
    *,
    database_url: str,
    limit: int | None = None,
    library_ids: Sequence[str] | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
) -> tuple[int, int]:
    if library_ids is not None:
        library_ids = [str(value).strip() for value in dict.fromkeys(library_ids) if str(value).strip()]
        if not library_ids:
            LOGGER.info("no library ids to process")
            return 0, 0

    with psycopg.connect(database_url, autocommit=False) as conn:
        _setup_schema(conn)
        _seed_pending_videos(conn, library_ids=library_ids)
        videos = _claim_videos(conn, limit=limit, library_ids=library_ids)
        if dry_run:
            conn.rollback()
            LOGGER.info("dry run would process %s videos", len(videos))
            for video in videos:
                LOGGER.info("candidate media_id=%s library_id=%s url=%s", video["media_id"], video["library_id"], video["url"])
            return 0, len(videos)
        conn.commit()

        if not videos:
            LOGGER.info("no pending videos to process")
            return 0, 0

        gemini_api_key = _required_env("GEMINI_API_KEY")
        client = genai.Client(api_key=gemini_api_key)
        
        # Parallel processing using ThreadPoolExecutor
        successes = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            # We need to use separate connections per thread or ensure thread safety.
            # psycopg.connect() is thread-safe for the connection itself, but 
            # each thread should ideally have its own connection to avoid contention
            # and proper transaction management.
            
            def _worker_task(v: Dict[str, Any]) -> bool:
                # Open a new connection for each worker task to ensure thread isolation
                try:
                    with psycopg.connect(database_url, autocommit=False) as worker_conn:
                        return _process_video(worker_conn, client, v)
                except Exception as e:
                    LOGGER.error("Worker task failed for media_id=%s: %s", v["media_id"], e)
                    return False

            future_to_video = {executor.submit(_worker_task, video): video for video in videos}
            for future in as_completed(future_to_video):
                if future.result():
                    successes += 1

    LOGGER.info("processed %s/%s videos with concurrency=%s", successes, len(videos), concurrency)
    return successes, len(videos)


def process_pending_google_videos(
    *,
    database_url: str,
    limit: int | None = None,
    creative_ids: Sequence[str] | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
) -> tuple[int, int]:
    if creative_ids is not None:
        creative_ids = [str(value).strip() for value in dict.fromkeys(creative_ids) if str(value).strip()]
        if not creative_ids:
            LOGGER.info("no google creative ids to process")
            return 0, 0

    with psycopg.connect(database_url, autocommit=False) as conn:
        _setup_google_schema(conn)
        _seed_pending_google_videos(conn, creative_ids=creative_ids)
        videos = _claim_google_videos(conn, limit=limit, creative_ids=creative_ids)
        if dry_run:
            conn.rollback()
            LOGGER.info("dry run would process %s google videos", len(videos))
            for video in videos:
                LOGGER.info(
                    "google candidate extraction_key=%s creative_id=%s url=%s",
                    video["extraction_key"],
                    video["creative_id"],
                    video["url"],
                )
            return 0, len(videos)
        conn.commit()

        if not videos:
            LOGGER.info("no pending google videos to process")
            return 0, 0

        gemini_api_key = _required_env("GEMINI_API_KEY")
        client = genai.Client(api_key=gemini_api_key)
        
        # Parallel processing using ThreadPoolExecutor
        successes = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            def _worker_task(v: Dict[str, Any]) -> bool:
                try:
                    with psycopg.connect(database_url, autocommit=False) as worker_conn:
                        return _process_google_video(worker_conn, client, v)
                except Exception as e:
                    LOGGER.error("Worker task failed for extraction_key=%s: %s", v["extraction_key"], e)
                    return False

            future_to_video = {executor.submit(_worker_task, video): video for video in videos}
            for future in as_completed(future_to_video):
                if future.result():
                    successes += 1

    LOGGER.info("processed %s/%s google videos with concurrency=%s", successes, len(videos), concurrency)
    return successes, len(videos)


def main(argv: Sequence[str] | None = None) -> int:
    _load_local_env()
    _setup_logging()
    args = _parse_args(argv or sys.argv[1:])
    if args.limit < 1:
        LOGGER.info("nothing to process because limit=%s", args.limit)
        return 0

    database_url = _required_env("DATABASE_URL")
    successes, total = process_pending_videos(
        database_url=database_url,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return 0 if successes == total else 1


if __name__ == "__main__":
    sys.exit(main())
