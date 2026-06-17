import asyncio
import logging
import os
import sys
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scrapling.spiders import Spider, Request
from scrapling.fetchers import AsyncStealthySession

from cron_runner_old import (
    _setup_logging, _database_url, _monitoring_competitors, _create_run,
    _library_ids_from_ads, _existing_library_ids, _split_ads_by_existing,
    _existing_influencer_instagram_usernames, _fallback_target_ads,
    _fill_new_ad_influencer_instagram_usernames, _prepare_media_for_storage,
    _store_existing_ad_observations, _store_ads,
    _mark_missing_ads, _finish_run,
    _process_videos_after_crawl, _process_embeddings_after_crawl,
    SCHEMA_SQL, _ensure_default_workspace, _load_local_env
)
from meta_ads_crawler import (
    build_ads_library_search_url, build_ads_library_url,
    _wait_until_min_library_ids, _wait_until_stable_cards, _collect_summary_popup_groups,
    _extract_html, _parse_ads_from_html, _merge_same_source_groups,
    _apply_session_env_options, _wait_for_ads_library_ready, AD_CARD_SELECTOR
)

LOGGER = logging.getLogger("spider-cron")

def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}

class AdFungusSpider(Spider):
    name = "adfungus_cron_spider"

    def __init__(self, database_url: str, *args, run_gemini: bool = True, job_id: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.database_url = database_url
        self.run_gemini = run_gemini
        self.job_id = (job_id or uuid.uuid4().hex).strip()
        self.limit = max(_int_env("LIMIT", 0), 0)
        self.scroll_wait_ms = max(_int_env("SCROLL_WAIT_MS", 3000), 300)
        self.stable_rounds = max(_int_env("STABLE_ROUNDS", 3), 1)
        self.stable_max_rounds = max(_int_env("STABLE_MAX_ROUNDS", 100), 1) # Set high for full collection
        self.challenge_wait_ms = max(_int_env("CHALLENGE_WAIT_MS", 90000), 0)
        self.debug = os.getenv("DEBUG", "").lower() in {"1", "true", "yes"}

    def configure_sessions(self, manager):
        session_config = {
            "headless": _bool_env("HEADLESS", True),
            "real_chrome": os.getenv("META_ADS_REAL_CHROME", "true").lower() not in {"0", "false", "no"},
            "timeout": 120000,
            "max_pages": 4,
            "retries": max(_int_env("FETCH_RETRIES", 1), 1),
            "retry_delay": max(_int_env("FETCH_RETRY_DELAY_MS", 1000), 0),
        }
        _apply_session_env_options(session_config)
        manager.add("stealth_session", AsyncStealthySession(**session_config))

    async def start_requests(self):
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            competitors = _monitoring_competitors(conn)

        for comp in competitors:
            brand = comp.get("brand")
            page_id = comp.get("page_id")
            competitor_id = comp["id"]
            workspace_id = comp["workspace_id"]
            
            target_url = build_ads_library_search_url(brand) if brand else build_ads_library_url(page_id)
            
            # We use a factory to bind closure variables properly
            def make_page_action(limit_val, stable_max_rounds_val, scroll_wait_ms_val, stable_rounds_val, debug_val):
                popup_groups = []
                async def page_action(page: Any) -> None:
                    ready_state = await _wait_for_ads_library_ready(
                        page,
                        max_wait_ms=self.challenge_wait_ms,
                        debug=debug_val,
                    )
                    if not ready_state.get("ready"):
                        if debug_val:
                            LOGGER.debug("ads-library-not-ready skip_scroll state=%s", ready_state)
                        return
                    await page.wait_for_timeout(10000)
                    if limit_val > 0:
                        await _wait_until_min_library_ids(page, target_count=limit_val, max_rounds=stable_max_rounds_val, wait_ms=scroll_wait_ms_val, debug=debug_val)
                    else:
                        await _wait_until_stable_cards(page, selector=AD_CARD_SELECTOR, stable_rounds=stable_rounds_val, max_rounds=stable_max_rounds_val, wait_ms=scroll_wait_ms_val, debug=debug_val)
                    
                    popup_groups.extend(
                        await _collect_summary_popup_groups(page, debug=debug_val, limit=limit_val, wait_ms=scroll_wait_ms_val)
                    )
                    await page.wait_for_timeout(1500)
                return page_action, popup_groups

            page_action, popup_groups = make_page_action(self.limit, self.stable_max_rounds, self.scroll_wait_ms, self.stable_rounds, self.debug)

            LOGGER.info(f"[*] Queuing request for {brand} (ID: {competitor_id})")
            
            with psycopg.connect(self.database_url, autocommit=False) as conn:
                run_id = _create_run(
                    conn,
                    page_id,
                    target_url,
                    job_id=self.job_id,
                    workspace_id=workspace_id,
                    competitor_id=competitor_id,
                )
                conn.commit()

            yield Request(
                url=target_url,
                callback=self.parse,
                meta={
                    "brand": brand, 
                    "page_id": page_id,
                    "competitor_id": competitor_id,
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "popup_groups": popup_groups
                },
                page_action=page_action,
                timeout=120000,
                network_idle=False
            )

    async def parse(self, response):
        meta = response.meta
        brand = meta["brand"]
        competitor_id = meta["competitor_id"]
        run_id = meta["run_id"]
        popup_groups = meta["popup_groups"]
        
        LOGGER.info(f"[+] Response received for {brand} (ID: {competitor_id})")

        html = _extract_html(response)
        if not html:
            LOGGER.error(f"[!] No HTML extracted for {brand}")
            with psycopg.connect(self.database_url, autocommit=False) as conn:
                _finish_run(conn, run_id, status="failed", error="No HTML extracted")
                conn.commit()
            yield None
            return

        parsed = _parse_ads_from_html(html, debug=self.debug, limit=self.limit, context="page")
        ads = _merge_same_source_groups(parsed, popup_groups, debug=self.debug)

        # Offload CPU/IO blocking tasks to thread pool
        await asyncio.to_thread(self.process_ads, ads, meta)
        yield {"brand": brand, "processed": True}

    def process_ads(self, ads, meta):
        brand = meta["brand"]
        page_id = meta["page_id"]
        competitor_id = meta["competitor_id"]
        workspace_id = meta["workspace_id"]
        run_id = meta["run_id"]

        try:
            observed_library_ids = _library_ids_from_ads(ads)
            with psycopg.connect(self.database_url) as conn:
                existing_library_ids = _existing_library_ids(conn, observed_library_ids)
                existing_usernames = _existing_influencer_instagram_usernames(conn, observed_library_ids)
            existing_ads, new_ads = _split_ads_by_existing(ads, existing_library_ids)
            fallback_ads = _fallback_target_ads(new_ads, existing_ads, existing_usernames)
            fallback_matches = _fill_new_ad_influencer_instagram_usernames(fallback_ads)
            if fallback_matches:
                LOGGER.info(
                    "influencer fallback filled %s ads for competitor_id=%s targets=%s",
                    len(fallback_matches),
                    competitor_id,
                    len(fallback_ads),
                )
            prepared_new_ads = _prepare_media_for_storage(new_ads)
            LOGGER.info(
                f"crawl split competitor_id={competitor_id} observed={len(observed_library_ids)} existing={len(existing_ads)} new={len(prepared_new_ads)}"
            )
        except Exception as exc:
            LOGGER.exception(f"crawl prep failed for competitor_id={competitor_id}")
            with psycopg.connect(self.database_url, autocommit=False) as conn:
                _finish_run(conn, run_id, status="failed", error=str(exc))
                conn.commit()
            return

        stored = 0
        new_library_ids = []
        with psycopg.connect(self.database_url, autocommit=False) as conn:
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
                    page_id=page_id,
                    workspace_id=workspace_id,
                    competitor_id=competitor_id,
                    ads=prepared_new_ads,
                )
                stored = existing_stored + new_stored
                _mark_missing_ads(
                    conn,
                    workspace_id=workspace_id,
                    competitor_id=competitor_id,
                    page_id=page_id,
                    observed_library_ids=observed_library_ids,
                )
                _finish_run(conn, run_id, status="success", ad_count=stored)
                conn.commit()
                LOGGER.info(
                    f"stored {stored} observed ads for competitor_id={competitor_id} run_id={run_id} new={len(new_library_ids)}"
                )
            except Exception as exc:
                conn.rollback()
                _finish_run(conn, run_id, status="failed", error=str(exc))
                conn.commit()
                LOGGER.exception(f"storage failed for competitor_id={competitor_id}")
                return

        # Phase 3: Post-crawl processing (Independent Tasks)
        LOGGER.info(
            "instagram metrics skipped in crawl path for competitor_id=%s run_id=%s",
            competitor_id,
            run_id,
        )

        if new_library_ids and self.run_gemini:
            try:
                _process_videos_after_crawl(self.database_url, new_library_ids)
            except Exception:
                LOGGER.exception(f"video extraction failed for competitor_id={competitor_id}")
            
            try:
                _process_embeddings_after_crawl(self.database_url, new_library_ids)
            except Exception:
                LOGGER.exception(f"embedding generation failed for competitor_id={competitor_id}")
        elif new_library_ids:
            LOGGER.info(
                "gemini post-processing skipped for competitor_id=%s new_library_ids=%s",
                competitor_id,
                len(new_library_ids),
            )


def main():
    _load_local_env()
    _setup_logging()
    database_url = _database_url()

    with psycopg.connect(database_url, autocommit=False) as conn:
        conn.execute(SCHEMA_SQL)
        _ensure_default_workspace(conn)
        conn.commit()

    LOGGER.info("--- Starting Spider-based Parallel Crawl ---")
    start_time = time.perf_counter()
    
    spider = AdFungusSpider(database_url=database_url)
    LOGGER.info("spider crawl job_id=%s", spider.job_id)
    # Keep spider concurrency conservative for Meta crawl stability.
    spider.start(concurrency=2, engine="stealthy")
    
    elapsed = time.perf_counter() - start_time
    LOGGER.info(f"--- Spider Crawl Finished in {elapsed:.2f} seconds ---")

    LOGGER.info("--- Starting final global video extraction cleanup ---")
    try:
        from video_text_worker import process_pending_videos
        process_pending_videos(database_url=database_url, concurrency=int(os.getenv("VIDEO_CONCURRENCY", "10")))
    except Exception:
        LOGGER.exception("Final global video extraction failed")

    LOGGER.info("--- Starting final global embedding and similarity cleanup ---")
    try:
        _process_embeddings_after_crawl(database_url)
    except Exception:
        LOGGER.exception("Final global embedding cleanup failed")

if __name__ == "__main__":
    main()
