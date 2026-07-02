import asyncio
import argparse
import logging
import os
import sys
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scrapling.spiders import Request
from spider_cron import AdFungusSpider, _int_env
from cron_runner_old import (
    _setup_logging,
    _database_url,
    _create_run,
    _process_videos_after_crawl,
    _process_embeddings_after_crawl,
    SCHEMA_SQL,
    _ensure_default_workspace,
    _load_local_env,
    _expand_versions_globally,
    _existing_workspace_library_ids,
)
from meta_ads_crawler import (
    build_ads_library_search_url,
    build_ads_library_url,
    _wait_until_min_library_ids,
    _wait_until_stable_cards,
    _collect_summary_popup_groups,
    _wait_for_ads_library_ready,
    AD_CARD_SELECTOR,
)

LOGGER = logging.getLogger("spider-targeted")

DEFAULT_TARGET_IDS = [103, 104, 107, 108, 109] # [14, 31, 79, 80, 82, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 97, 98, 102, 103, 104, 107, 108, 109]


def _target_ids_from_env() -> list[int]:
    raw = os.getenv("TARGET_IDS", "").strip()
    if not raw:
        return list(DEFAULT_TARGET_IDS)

    values: list[int] = []
    for chunk in raw.split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            values.append(int(text))
        except ValueError:
            LOGGER.warning("Ignoring invalid TARGET_IDS entry: %r", text)

    if values:
        return values

    LOGGER.warning("TARGET_IDS was set but no valid integers were found; using defaults")
    return list(DEFAULT_TARGET_IDS)

# 타겟팅할 브랜드의 competitor ID 목록
TARGET_IDS = list(DEFAULT_TARGET_IDS)


class TargetedAdFungusSpider(AdFungusSpider):
    name = "targeted_spider"

    def __init__(self, *args, target_ids: list[int] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_ids = list(target_ids or DEFAULT_TARGET_IDS)

    async def start_requests(self):
        # 지정된 ID만 DB에서 가져오도록 수정
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, workspace_id, page_id, brand
                    FROM competitors
                    WHERE id = ANY(%s)
                    ORDER BY created_at ASC
                    """,
                    (self.target_ids,),
                )
                competitors = cur.fetchall()

        for comp in competitors:
            brand = comp.get("brand")
            page_id = comp.get("page_id")
            competitor_id = comp["id"]
            workspace_id = comp["workspace_id"]
            with psycopg.connect(self.database_url) as conn:
                existing_competitor_library_ids = _existing_workspace_library_ids(
                    conn,
                    workspace_id=workspace_id,
                    competitor_id=competitor_id,
                )

            target_url = (
                build_ads_library_search_url(brand)
                if brand
                else build_ads_library_url(page_id)
            )

            def make_page_action(
                limit_val,
                stable_max_rounds_val,
                scroll_wait_ms_val,
                stable_rounds_val,
                debug_val,
                skip_library_ids,
            ):
                popup_groups = []

                async def page_action(page: Any) -> None:
                    ready_state = await _wait_for_ads_library_ready(
                        page,
                        max_wait_ms=self.challenge_wait_ms,
                        debug=self.debug,
                    )
                    if not ready_state.get("ready"):
                        if self.debug:
                            LOGGER.debug("ads-library-not-ready skip_scroll state=%s", ready_state)
                        return
                    await page.wait_for_timeout(10000)
                    if limit_val > 0:
                        await _wait_until_min_library_ids(
                            page,
                            target_count=limit_val,
                            max_rounds=stable_max_rounds_val,
                            wait_ms=scroll_wait_ms_val,
                            debug=debug_val,
                        )
                    else:
                        await _wait_until_stable_cards(
                            page,
                            selector=AD_CARD_SELECTOR,
                            stable_rounds=stable_rounds_val,
                            max_rounds=stable_max_rounds_val,
                            wait_ms=scroll_wait_ms_val,
                            debug=debug_val,
                        )

                    popup_groups.extend(
                        await _collect_summary_popup_groups(
                            page,
                            debug=debug_val,
                            limit=limit_val,
                            wait_ms=scroll_wait_ms_val,
                            skip_library_ids=skip_library_ids,
                        )
                    )
                    await page.wait_for_timeout(1500)

                return page_action, popup_groups

            page_action, popup_groups = make_page_action(
                self.limit,
                self.stable_max_rounds,
                self.scroll_wait_ms,
                self.stable_rounds,
                self.debug,
                existing_competitor_library_ids,
            )

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
                    "popup_groups": popup_groups,
                    "existing_competitor_library_ids": sorted(existing_competitor_library_ids),
                },
                page_action=page_action,
                timeout=120000,
                network_idle=False,
            )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run targeted Meta Ads crawl.")
    parser.add_argument(
        "--no-gemini",
        action="store_true",
        help="Skip Gemini video extraction and ad embedding after crawl.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    args = _parse_args(argv)
    _load_local_env()
    _setup_logging()
    database_url = _database_url()
    target_ids = _target_ids_from_env()

    # 환경 변수를 무시하고 로컬 강제 설정 적용
    os.environ["LIMIT"] = "0"  # 무제한
    os.environ["STABLE_MAX_ROUNDS"] = "100"  # 최대 100회 스크롤
    os.environ["META_ADS_REAL_CHROME"] = (
        "true"  # (옵션) Headless 해제하고 싶으면 False로 변경
    )
    os.environ["HEADLESS"] = "true"
    os.environ["FETCH_RETRIES"] = "1"
    os.environ["CHALLENGE_WAIT_MS"] = "90000"

    LOGGER.info("--- Starting Targeted Spider Crawl (%s Brands) ---", len(target_ids))
    LOGGER.info("targeted spider target_ids=%s", target_ids)
    start_time = time.perf_counter()

    job_id = uuid.uuid4().hex
    spider = TargetedAdFungusSpider(
        database_url=database_url,
        run_gemini=not args.no_gemini,
        job_id=job_id,
        target_ids=target_ids,
    )
    LOGGER.info("targeted spider job_id=%s", job_id)
    spider.start(concurrency=4, engine="stealthy")

    elapsed = time.perf_counter() - start_time
    LOGGER.info(f"--- Spider Crawl Finished in {elapsed:.2f} seconds ---")

    # FINAL STEP: Expand versions from leaders to followers globally
    LOGGER.info("--- Starting global version expansion (Fan-out) ---")
    try:
        _expand_versions_globally(database_url)
    except Exception:
        LOGGER.exception("Final global version expansion failed")


if __name__ == "__main__":
    main(sys.argv[1:])
