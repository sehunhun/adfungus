from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np
import psycopg
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


LOGGER = logging.getLogger("adfungus-embedding-worker")
MODEL_NAME = "gemini-embedding-2"
DEFAULT_TOP_K = 10
SIMILARITY_THRESHOLD = 0.96

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta_ad_similarity_runs (
    id BIGSERIAL PRIMARY KEY,
    model TEXT NOT NULL,
    ad_count INTEGER NOT NULL,
    top_k INTEGER NOT NULL DEFAULT 10,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta_ad_embeddings (
    library_id TEXT PRIMARY KEY REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    embedding JSONB NOT NULL,
    similarity_sum DOUBLE PRECISION,
    similarity_rank INTEGER,
    similarity_run_id BIGINT REFERENCES meta_ad_similarity_runs(id) ON DELETE SET NULL,
    similar_count INTEGER NOT NULL DEFAULT 0,
    similar_library_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE meta_ad_embeddings ADD COLUMN IF NOT EXISTS similar_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meta_ad_embeddings ADD COLUMN IF NOT EXISTS similar_library_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS similar_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meta_ads ADD COLUMN IF NOT EXISTS similar_library_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS meta_ad_similarity_edges (
    run_id BIGINT NOT NULL REFERENCES meta_ad_similarity_runs(id) ON DELETE CASCADE,
    source_library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    target_library_id TEXT NOT NULL REFERENCES meta_ads(library_id) ON DELETE CASCADE,
    similarity DOUBLE PRECISION NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (run_id, source_library_id, target_library_id)
);

CREATE INDEX IF NOT EXISTS idx_meta_ad_embeddings_similarity_rank
    ON meta_ad_embeddings(similarity_rank);
CREATE INDEX IF NOT EXISTS idx_meta_ad_embeddings_similarity_run_id
    ON meta_ad_embeddings(similarity_run_id);
CREATE INDEX IF NOT EXISTS idx_meta_ad_similarity_edges_source
    ON meta_ad_similarity_edges(run_id, source_library_id, rank);
"""


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load_local_env() -> None:
    load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=False)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        LOGGER.warning("invalid integer env %s=%r; using %s", name, value, default)
        return default


def _setup_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(SCHEMA_SQL)


def _pending_embedding_ads(conn: psycopg.Connection[Any], library_ids: List[str] | None = None) -> List[Dict[str, Any]]:
    params: list[Any] = []
    library_filter = ""
    if library_ids is not None:
        params.append(list(library_ids))
        library_filter = "AND a.library_id = ANY(%s)"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                a.library_id,
                a.ad_format,
                COALESCE(a.body, '') AS body,
                m.url AS image_url,
                m.content_type,
                v.audio_transcript,
                v.screen_text
            FROM meta_ads a
            LEFT JOIN LATERAL (
                SELECT url, content_type
                FROM meta_ad_media
                WHERE library_id = a.library_id
                  AND media_type = 'image'
                  AND url LIKE '%%r2.dev%%'
                ORDER BY id ASC
                LIMIT 1
            ) m ON true
            LEFT JOIN meta_ad_video_extractions v ON v.library_id = a.library_id AND v.status = 'success'
            LEFT JOIN meta_ad_embeddings e ON e.library_id = a.library_id
            WHERE e.library_id IS NULL
              {library_filter}
            ORDER BY a.first_seen_at ASC, a.library_id ASC
            """,
            params,
        )
        return list(cur.fetchall())

def _mime_type(value: Any) -> str:
    text = str(value or "").split(";", 1)[0].strip().lower()
    return text if text.startswith("image/") else "image/jpeg"


def _embedding_values(response: Any) -> List[float]:
    embeddings = getattr(response, "embeddings", None)
    first = embeddings[0] if embeddings else getattr(response, "embedding", None)
    values = getattr(first, "values", None) if first is not None else None
    if not values:
        raise RuntimeError("Gemini returned no embedding values")
    return [float(value) for value in values]


def _embed_ad(client: genai.Client, ad: Dict[str, Any]) -> List[float]:
    # Construct text part: Body + Transcript (if available)
    text_parts = [str(ad.get("body") or "").strip() or " "]
    transcript = str(ad.get("audio_transcript") or "").strip()
    if not transcript:
        transcript = str(ad.get("screen_text") or "").strip()
    
    if transcript:
        text_parts.append(f"\n[Video Content]: {transcript}")
    
    combined_text = "\n".join(text_parts)
    
    image_url = str(ad.get("image_url") or "").strip()
    parts = [types.Part.from_text(text=combined_text)]

    # For image ads or video ads without transcript, use the image (if hosted on R2)
    # But if we have a transcript for a video ad, the user prefers Body + Transcript over Image.
    # Actually, user said "Body + Voice (if not, screen text)". 
    # So for videos with transcript, we can skip the image to be safe and efficient.
    
    is_video = ad.get("ad_format") == "video"
    has_transcript = bool(transcript)

    if image_url and (not is_video or not has_transcript):
        try:
            # Download image to send as bytes to avoid robots.txt issues
            resp = requests.get(image_url, timeout=30)
            resp.raise_for_status()
            parts.append(
                types.Part.from_bytes(
                    data=resp.content, 
                    mime_type=_mime_type(ad.get("content_type"))
                )
            )
        except Exception as exc:
            LOGGER.warning("failed to download image for embedding library_id=%s: %s", ad["library_id"], exc)
            # Proceed with text only if image download fails

    content = types.Content(parts=parts)
    response = client.models.embed_content(model=MODEL_NAME, contents=content)
    return _embedding_values(response)


def _store_embedding(conn: psycopg.Connection[Any], library_id: str, embedding: List[float]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta_ad_embeddings (library_id, embedding)
            VALUES (%s, %s)
            ON CONFLICT (library_id) DO NOTHING
            """,
            (library_id, Jsonb(embedding)),
        )


def _create_similarity_run(conn: psycopg.Connection[Any], *, ad_count: int, top_k: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta_ad_similarity_runs (model, ad_count, top_k)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (MODEL_NAME, ad_count, top_k),
        )
        return int(cur.fetchone()[0])


def _load_embeddings(conn: psycopg.Connection[Any]) -> List[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT library_id, embedding
            FROM meta_ad_embeddings
            ORDER BY library_id ASC
            """
        )
        rows = list(cur.fetchall())

    valid: List[Dict[str, Any]] = []
    for row in rows:
        values = row["embedding"]
        if not isinstance(values, list) or not values:
            LOGGER.warning("skipping invalid embedding library_id=%s", row["library_id"])
            continue
        vector = [float(value) for value in values]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            LOGGER.warning("skipping zero embedding library_id=%s", row["library_id"])
            continue
        valid.append(
            {
                "library_id": row["library_id"],
                "vector": [value / norm for value in vector],
            }
        )
    return valid


def _store_similarity_scores(
    conn: psycopg.Connection[Any],
    embeddings: List[Dict[str, Any]],
    *,
    run_id: int,
    top_k: int,
) -> None:
    similar_data: Dict[str, Dict[str, Any]] = {}
    edges: List[tuple[int, str, str, float, int]] = []
    library_ids = [str(item["library_id"]) for item in embeddings]
    vectors = np.asarray([item["vector"] for item in embeddings], dtype=np.float32)
    similarities = vectors @ vectors.T
    np.fill_diagonal(similarities, -np.inf)

    finite_similarities = np.where(np.isfinite(similarities), similarities, 0.0)
    scores = {
        library_id: float(score)
        for library_id, score in zip(library_ids, finite_similarities.sum(axis=1).tolist())
    }
    top_k_count = min(top_k, max(len(library_ids) - 1, 0))

    for source_index, source_id in enumerate(library_ids):
        row = similarities[source_index]
        # 정렬하여 top_k 추출
        if top_k_count:
            candidate_indexes = np.argpartition(row, -top_k_count)[-top_k_count:]
            top_indexes = candidate_indexes[np.argsort(row[candidate_indexes])[::-1]]
        else:
            top_indexes = np.asarray([], dtype=np.int64)
        
        # 0.96 임계값 기반 카운트 및 ID 추출
        threshold_indexes = np.flatnonzero(row >= SIMILARITY_THRESHOLD)
        if threshold_indexes.size:
            threshold_indexes = threshold_indexes[np.argsort(row[threshold_indexes])[::-1]]
        threshold_matches = [library_ids[int(index)] for index in threshold_indexes]
        similar_data[source_id] = {
            "count": len(threshold_matches),
            "ids": threshold_matches
        }

        for rank, target_index in enumerate(top_indexes, start=1):
            target_id = library_ids[int(target_index)]
            edges.append((run_id, source_id, target_id, float(row[target_index]), rank))

    ranked_ids = [
        library_id
        for library_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ]
    ranks = {library_id: rank for rank, library_id in enumerate(ranked_ids, start=1)}
    update_rows = [
        (
            library_id,
            scores[library_id],
            ranks[library_id],
            run_id,
            similar_data[library_id]["count"],
            Jsonb(similar_data[library_id]["ids"]),
        )
        for library_id in library_ids
    ]

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp.meta_ad_similarity_updates")
        cur.execute(
            """
            CREATE TEMP TABLE meta_ad_similarity_updates (
                library_id TEXT PRIMARY KEY,
                similarity_sum DOUBLE PRECISION NOT NULL,
                similarity_rank INTEGER NOT NULL,
                similarity_run_id BIGINT NOT NULL,
                similar_count INTEGER NOT NULL,
                similar_library_ids JSONB NOT NULL
            ) ON COMMIT DROP
            """
        )
        cur.executemany(
            """
            INSERT INTO meta_ad_similarity_updates (
                library_id, similarity_sum, similarity_rank, similarity_run_id,
                similar_count, similar_library_ids
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            update_rows,
        )
        cur.execute(
            """
            UPDATE meta_ad_embeddings e
            SET similarity_sum = u.similarity_sum,
                similarity_rank = u.similarity_rank,
                similarity_run_id = u.similarity_run_id,
                similar_count = u.similar_count,
                similar_library_ids = u.similar_library_ids,
                updated_at = now()
            FROM meta_ad_similarity_updates u
            WHERE e.library_id = u.library_id
            """
        )
        cur.execute(
            """
            UPDATE meta_ads a
            SET similar_count = u.similar_count,
                similar_library_ids = u.similar_library_ids,
                updated_at = now()
            FROM meta_ad_similarity_updates u
            WHERE a.library_id = u.library_id
            """
        )

        if edges:
            cur.executemany(
                """
                INSERT INTO meta_ad_similarity_edges (
                    run_id, source_library_id, target_library_id, similarity, rank
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (run_id, source_library_id, target_library_id) DO UPDATE SET
                    similarity = EXCLUDED.similarity,
                    rank = EXCLUDED.rank
                """,
                edges,
            )


def process_ad_embeddings(*, database_url: str, top_k: int = DEFAULT_TOP_K, library_ids: List[str] | None = None) -> tuple[int, int]:
    top_k = max(top_k, 1)
    with psycopg.connect(database_url, autocommit=False) as conn:
        _setup_schema(conn)
        conn.commit()

        pending = _pending_embedding_ads(conn, library_ids=library_ids)
        if pending:
            client = genai.Client(api_key=_required_env("GEMINI_API_KEY"))
            embedded = 0
            for ad in pending:
                library_id = str(ad["library_id"])
                try:
                    embedding = _embed_ad(client, ad)
                    _store_embedding(conn, library_id, embedding)
                    conn.commit()
                    embedded += 1
                    LOGGER.info("embedded ad library_id=%s", library_id)
                except Exception:
                    conn.rollback()
                    LOGGER.exception("ad embedding failed library_id=%s", library_id)
        else:
            embedded = 0
            LOGGER.info("no ads need embeddings")

        if library_ids is not None:
            # Skip global similarity scoring when doing targeted embedding for a specific brand/batch.
            # Global cleanup at the end of the crawl process will handle similarity for everyone.
            return embedded, 0

        embeddings = _load_embeddings(conn)
        if not embeddings:
            LOGGER.info("similarity scoring skipped because no embeddings exist")
            return embedded, 0

        run_id = _create_similarity_run(conn, ad_count=len(embeddings), top_k=top_k)
        _store_similarity_scores(conn, embeddings, run_id=run_id, top_k=top_k)

        # Cleanup old runs to prevent row explosion in meta_ad_similarity_edges.
        # CASCADE delete will take care of the edges.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM meta_ad_similarity_runs WHERE id < %s", (run_id,))

        conn.commit()
        LOGGER.info(
            "similarity scoring complete run_id=%s ad_count=%s top_k=%s",
            run_id,
            len(embeddings),
            top_k,
        )
        return embedded, len(embeddings)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed Meta ads and score ad similarity.")
    parser.add_argument("--top-k", type=int, default=_int_env("AD_EMBEDDING_TOP_K", DEFAULT_TOP_K))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _load_local_env()
    _setup_logging()
    args = _parse_args(argv or sys.argv[1:])
    database_url = _required_env("DATABASE_URL")
    process_ad_embeddings(database_url=database_url, top_k=args.top_k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
