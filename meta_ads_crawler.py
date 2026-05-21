from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse, unquote

from scrapling.fetchers import AsyncStealthySession, StealthyFetcher
from scrapling.parser import Selector


DEFAULT_PAGE_ID = "654845641046548"
LOGGER = logging.getLogger("meta-ads-crawler")
AD_LIST_CONTAINER_SELECTOR = (
    "div.xrvj5dj.x18m771g.x1p5oq8j.xp48ta0.x18d9i69.xtssl2i.xtqikln"
    ".x1na6gtj.xjewof7.x1l48g3s.x1vql8b3.x1m5622i"
)
AD_CARD_SELECTOR = f"{AD_LIST_CONTAINER_SELECTOR} div.xh8yej3"


def _setup_logging(debug: bool) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_ads_library_url(page_id: str) -> str:
    normalized_page_id = _clean(page_id)
    return (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
        f"&country=KR&view_all_page_id={normalized_page_id}"
    )


def _card_count_from_html(html: str) -> int:
    if not html:
        return 0
    try:
        return len(Selector(html).css(AD_CARD_SELECTOR))
    except Exception:
        return 0


LIBRARY_ID_RE = re.compile(
    r"(?:라이브러리\s*ID|Library\s+ID|Ad\s+library\s+ID)\s*:\s*(\d+)",
    flags=re.IGNORECASE,
)


def _library_id_count_from_html(html: str) -> int:
    return len(LIBRARY_ID_RE.findall(html or ""))


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", str(value))
    return re.sub(r"\s+", " ", text).strip()


def _node_text(node: Any) -> str:
    if node is None:
        return ""
    try:
        return _clean(node.get_all_text())
    except Exception:
        return ""


def _extract_html(fetch_result: Any) -> str:
    for name in ("html", "html_content", "raw_html", "content", "text", "body"):
        value = getattr(fetch_result, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value:
            return value
    return ""


def _normalize_outbound_url(url: str) -> str:
    raw = _clean(url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if "l.facebook.com" in parsed.netloc and parsed.path.startswith("/l.php"):
        q = parse_qs(parsed.query)
        target = q.get("u", [""])[0]
        if target:
            return unquote(target)
    return raw


def _parse_library_id(card_text: str) -> str:
    match = LIBRARY_ID_RE.search(card_text)
    return match.group(1) if match else ""


def _parse_start_date(card_text: str) -> str:
    match = re.search(r"(\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.)에\s*게재\s*시작", card_text)
    if match:
        return _clean(match.group(1))
    match = re.search(
        r"Started\s+running\s+on\s+(.+?)(?=\s+Platforms\b|\s+Open\s+Dropdown\b|$)",
        card_text,
        flags=re.IGNORECASE,
    )
    return _clean(match.group(1)) if match else ""


def _parse_video_duration_seconds(card: Any) -> Optional[int]:
    durations: List[int] = []
    for node in card.css("span"):
        text = _node_text(node)
        if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
            continue
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 2:
            durations.append(parts[0] * 60 + parts[1])
        if len(parts) == 3:
            durations.append(parts[0] * 3600 + parts[1] * 60 + parts[2])
    positive_durations = [value for value in durations if value > 0]
    return max(positive_durations) if positive_durations else None


def _infer_platforms(card_text: str) -> List[str]:
    lowered = card_text.lower()
    platforms: List[str] = []
    if "facebook" in lowered or "페이스북" in lowered:
        platforms.append("Facebook")
    if "instagram" in lowered or "인스타그램" in lowered:
        platforms.append("Instagram")
    if "messenger" in lowered or "메신저" in lowered:
        platforms.append("Messenger")
    if "audience network" in lowered or "오디언스" in lowered:
        platforms.append("Audience Network")
    return platforms


def _collect_media(card: Any) -> Dict[str, Any]:
    images: List[Dict[str, str]] = []
    videos: List[Dict[str, Any]] = []

    for img in card.css("img"):
        src = _clean((getattr(img, "attrib", {}) or {}).get("src", ""))
        if not src:
            continue
        if re.search(r"(?:_s60x60|s60x60|profile|t51\.82787-19)", src, flags=re.IGNORECASE):
            continue
        images.append({"url": src})

    duration = _parse_video_duration_seconds(card)
    for video in card.css("video"):
        src = _clean((getattr(video, "attrib", {}) or {}).get("src", ""))
        poster = _clean((getattr(video, "attrib", {}) or {}).get("poster", ""))
        if src:
            item: Dict[str, Any] = {"url": src}
            if duration is not None:
                item["duration"] = duration
            videos.append(item)
        if poster:
            images.append({"url": poster})

    seen = set()
    deduped_images: List[Dict[str, str]] = []
    for item in images:
        key = item.get("url", "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped_images.append(item)

    seen.clear()
    deduped_videos: List[Dict[str, Any]] = []
    for item in videos:
        key = item.get("url", "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped_videos.append(item)

    return {"images": deduped_images, "videos": deduped_videos}


CTA_PATTERNS = [
    "더 알아보기",
    "지금 구매",
    "구매하기",
    "신청하기",
    "예약하기",
    "문의하기",
    "바로가기",
    "Learn more",
    "Shop now",
    "Sign up",
    "Apply now",
    "Contact us",
    "Book now",
]


def _extract_cta_text(value: str) -> str:
    for pattern in CTA_PATTERNS:
        if re.search(re.escape(pattern), value, flags=re.IGNORECASE):
            return pattern
    return ""


def _extract_link_title_from_text(value: str) -> str:
    match = re.search(r"\b([A-Z0-9][A-Z0-9.-]+\.[A-Z]{2,})\b", value, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _extract_link_title_from_url(value: str) -> str:
    parsed = urlparse(value or "")
    host = parsed.netloc.lower().removeprefix("www.")
    return host.upper() if host else ""


def _extract_link_description_from_text(card_text: str, link_title: str) -> str:
    if not link_title or link_title not in card_text:
        return ""
    tail = card_text.split(link_title, 1)[1]
    for cta in CTA_PATTERNS:
        tail = re.split(re.escape(cta), tail, maxsplit=1, flags=re.IGNORECASE)[0]
    tail = _clean(tail)
    if not tail:
        return ""
    if re.search(r"(Library ID|Started running|Platforms|Open Dropdown|See ad details|Sponsored)", tail, flags=re.IGNORECASE):
        return ""
    return tail[:180]


def _parse_cta_and_link(card: Any) -> Dict[str, str]:
    card_text = _node_text(card)
    cta_text = ""
    cta_url = ""
    link_title = _extract_link_title_from_text(card_text)
    link_url = ""
    link_description = ""

    anchors = card.css("a[href]")
    for anchor in anchors:
        href = _normalize_outbound_url((getattr(anchor, "attrib", {}) or {}).get("href", ""))
        text = _node_text(anchor)
        if not href:
            continue

        extracted_cta = _extract_cta_text(text)
        if extracted_cta:
            if not cta_text:
                cta_text = extracted_cta
            if not cta_url:
                cta_url = href

        if not link_url and not href.startswith("https://www.facebook.com/"):
            link_url = href

        if text and re.search(r"\.[a-z]{2,}$", text.lower()):
            if not link_title:
                link_title = text

    if not cta_text:
        cta_text = _extract_cta_text(card_text)

    if not link_title and link_url:
        link_title = _extract_link_title_from_url(link_url)

    link_description = _extract_link_description_from_text(card_text, link_title)

    text_blocks = card.css("div, span")
    for block in text_blocks:
        text = _node_text(block)
        if not text:
            continue
        if not link_title and re.fullmatch(r"[A-Z0-9.-]+\.[A-Z]{2,}", text):
            link_title = text
        if not link_description and 8 <= len(text) <= 180 and text not in {cta_text, link_title}:
            if not re.search(r"(라이브러리 ID|Library ID|게재 시작|Started running|플랫폼|Platforms|광고 상세 정보 보기|See ad details|Open Dropdown)", text, flags=re.IGNORECASE):
                link_description = text
                break

    if not cta_url:
        cta_url = link_url

    return {
        "linkTitle": link_title,
        "linkUrl": link_url,
        "linkDescription": link_description,
        "ctaText": cta_text,
        "ctaUrl": cta_url,
    }


def _parse_brand(card: Any) -> Dict[str, str]:
    brand = ""
    brand_logo = ""

    for img in card.css("img"):
        alt = _clean((getattr(img, "attrib", {}) or {}).get("alt", ""))
        src = _clean((getattr(img, "attrib", {}) or {}).get("src", ""))
        if not brand and alt:
            brand = alt
        if not brand_logo and src:
            brand_logo = src
        if brand and brand_logo:
            break

    if not brand:
        for anchor in card.css("a"):
            text = _node_text(anchor)
            if text and "facebook.com" not in text.lower():
                brand = text
                break

    return {"brand": brand, "brandLogo": brand_logo}


def _parse_body_text(card: Any) -> str:
    text = unescape(_node_text(card))
    parts = re.split(r"\bSponsored\b|광고", text, maxsplit=1, flags=re.IGNORECASE)
    body = parts[1] if len(parts) > 1 else text
    body = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*/\s*\d{1,2}:\d{2}(?::\d{2})?\b", " ", body)
    link_title = _extract_link_title_from_text(body)
    if link_title:
        body = body.split(link_title, 1)[0]
    for cta in CTA_PATTERNS:
        body = re.split(re.escape(cta), body, maxsplit=1, flags=re.IGNORECASE)[0]
    return _clean(body)


def _detect_format(images: List[Dict[str, str]], videos: List[Dict[str, Any]]) -> str:
    if videos:
        return "video"
    if len(images) > 1:
        return "carousel"
    return "image"


def _select_ad_card_candidates(document: Any, debug: bool = False) -> List[Any]:
    selectors = [
        AD_CARD_SELECTOR,
    ]
    best_by_library_id: Dict[str, Dict[str, Any]] = {}
    selector_counts: Dict[str, int] = {}

    for selector in selectors:
        try:
            nodes = document.css(selector)
        except Exception:
            nodes = []
        selector_counts[selector] = len(nodes)

        for node in nodes:
            text = _node_text(node)
            library_id = _parse_library_id(text)
            if not library_id:
                continue

            current = best_by_library_id.get(library_id)
            text_len = len(text)
            if current is None or text_len > current["text_len"]:
                best_by_library_id[library_id] = {
                    "node": node,
                    "selector": selector,
                    "text_len": text_len,
                    "preview": text[:220],
                }

    if debug:
        LOGGER.debug("selector_counts=%s", selector_counts)
        LOGGER.debug("unique_library_id_candidates=%s", len(best_by_library_id))
        for idx, (library_id, meta) in enumerate(list(best_by_library_id.items())[:3]):
            LOGGER.debug(
                "candidate[%s] library_id=%s selector=%s text_len=%s preview=%s",
                idx,
                library_id,
                meta["selector"],
                meta["text_len"],
                meta["preview"],
            )

    return [meta["node"] for meta in best_by_library_id.values()]


def parse_ad_card(card: Any) -> Optional[Dict[str, Any]]:
    card_text = _node_text(card)
    library_id = _parse_library_id(card_text)
    if not library_id:
        return None

    media = _collect_media(card)
    links = _parse_cta_and_link(card)
    brand_data = _parse_brand(card)
    body = _parse_body_text(card)

    active = "활성" in card_text or bool(re.search(r"\bActive\b", card_text, flags=re.IGNORECASE))
    start_date = _parse_start_date(card_text)
    platforms = _infer_platforms(card_text)
    ad_format = _detect_format(media["images"], media["videos"])

    return {
        "libraryID": library_id,
        "brand": brand_data["brand"],
        "brandLogo": brand_data["brandLogo"],
        "active": active,
        "platforms": platforms,
        "body": body,
        "linkTitle": links["linkTitle"],
        "linkUrl": links["linkUrl"],
        "linkDescription": links["linkDescription"],
        "ctaText": links["ctaText"],
        "ctaUrl": links["ctaUrl"],
        "images": media["images"],
        "videos": media["videos"],
        "startDate": start_date,
        "format": ad_format,
    }


async def _count_dom_matches(page: Any, selector: str) -> int:
    try:
        count = await page.evaluate(
            "(sel) => document.querySelectorAll(sel).length", selector
        )
        return int(count)
    except Exception:
        return -1


async def _count_library_ids_on_page(page: Any) -> int:
    try:
        count = await page.evaluate(
            r"""
            () => {
              const text = document.body ? document.body.innerText : '';
              const matches = text.match(/(?:라이브러리\s*ID|Library\s+ID|Ad\s+library\s+ID)\s*:\s*\d+/gi);
              return matches ? matches.length : 0;
            }
            """
        )
        return int(count)
    except Exception:
        return -1


async def _scroll_page(page: Any, rounds: int, wait_ms: int, debug: bool) -> None:
    for idx in range(rounds):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        try:
            await page.wait_for_timeout(wait_ms)
        except Exception:
            await asyncio.sleep(wait_ms / 1000)

        if debug:
            xh8_count = await _count_dom_matches(page, AD_CARD_SELECTOR)
            library_id_count = await _count_library_ids_on_page(page)
            LOGGER.debug(
                "scroll round=%s scoped_card_selector_count=%s library_id_text_count=%s",
                idx + 1,
                xh8_count,
                library_id_count,
            )


async def _wait_until_stable_cards(
    page: Any,
    *,
    selector: str,
    stable_rounds: int,
    max_rounds: int,
    wait_ms: int,
    debug: bool,
) -> None:
    timeout_ms = max_rounds * wait_ms
    try:
        await page.wait_for_function(
            r"""
            ({ selector, stableRounds, intervalMs }) => {
              window.__metaAdsStableState = window.__metaAdsStableState || {};
              const key = `${selector}:library-id`;
              const state = window.__metaAdsStableState[key] || { last: -1, hits: 0, lastTs: 0 };
              const now = Date.now();
              if (now - state.lastTs < intervalMs) {
                window.__metaAdsStableState[key] = state;
                return false;
              }

              window.scrollTo(0, document.body.scrollHeight);
              const text = document.body ? document.body.innerText : '';
              const matches = text.match(/(?:라이브러리\s*ID|Library\s+ID|Ad\s+library\s+ID)\s*:\s*\d+/gi);
              const current = matches ? matches.length : 0;
              if (current === state.last) {
                state.hits += 1;
              } else {
                state.hits = 0;
              }
              state.last = current;
              state.lastTs = now;
              window.__metaAdsStableState[key] = state;
              return current > 0 && state.hits >= stableRounds;
            }
            """,
            arg={"selector": selector, "stableRounds": stable_rounds, "intervalMs": wait_ms},
            timeout=timeout_ms,
            polling=wait_ms,
        )
        if debug:
            count = await _count_dom_matches(page, selector)
            library_id_count = await _count_library_ids_on_page(page)
            LOGGER.debug(
                "wait_for_function library-id stable satisfied selector=%s count=%s library_id_text_count=%s",
                selector,
                count,
                library_id_count,
            )
        return
    except Exception as exc:
        if debug:
            LOGGER.debug("wait_for_function stable fallback reason=%s", exc)

    stable_hits = 0
    prev_count = -1

    for idx in range(max_rounds):
        try:
            count = await page.evaluate(
                "(sel) => document.querySelectorAll(sel).length", selector
            )
        except Exception:
            count = -1

        library_id_count = await _count_library_ids_on_page(page)
        stable_count = library_id_count if library_id_count >= 0 else count

        if stable_count == prev_count and stable_count >= 0:
            stable_hits += 1
        else:
            stable_hits = 0

        if debug:
            LOGGER.debug(
                "stable-fallback round=%s selector=%s count=%s library_id_text_count=%s stable_hits=%s/%s",
                idx + 1,
                selector,
                count,
                library_id_count,
                stable_hits,
                stable_rounds,
            )

        if stable_hits >= stable_rounds:
            if debug:
                LOGGER.debug(
                    "stable-check satisfied at round=%s count=%s library_id_text_count=%s",
                    idx + 1,
                    count,
                    library_id_count,
                )
            return

        prev_count = stable_count
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(wait_ms)
        except Exception:
            await asyncio.sleep(wait_ms / 1000)


async def crawl_meta_ads(
    url: str,
    scroll_rounds: int = 8,
    scroll_wait_ms: int = 1200,
    debug: bool = False,
    stable_rounds: int = 3,
    stable_max_rounds: int = 18,
    dump_html_path: str = "",
) -> List[Dict[str, Any]]:
    StealthyFetcher.configure(adaptive=True)

    session_config: Dict[str, Any] = {
        "headless": True,
        "real_chrome": True,
        "timeout": 120000,
        "max_pages": 1,
    }

    if debug:
        LOGGER.debug("fetch url=%s", url)
        LOGGER.debug("session_config=%s", session_config)

    async with AsyncStealthySession(**session_config) as session:
        async def page_action(page: Any) -> None:
            await page.wait_for_timeout(3000)
            await _scroll_page(page, rounds=scroll_rounds, wait_ms=scroll_wait_ms, debug=debug)
            await _wait_until_stable_cards(
                page,
                selector=AD_CARD_SELECTOR,
                stable_rounds=stable_rounds,
                max_rounds=stable_max_rounds,
                wait_ms=scroll_wait_ms,
                debug=debug,
            )
            await page.wait_for_timeout(1500)

        result = await session.fetch(url, page_action=page_action, timeout=120000, network_idle=True)

    html = _extract_html(result)
    if debug:
        LOGGER.debug("html_length=%s", len(html))
        LOGGER.debug("library_id_regex_matches_in_html=%s", _library_id_count_from_html(html))
    if not html:
        return []

    if dump_html_path:
        with open(dump_html_path, "w", encoding="utf-8") as fp:
            fp.write(html)
        LOGGER.info("saved html dump: %s", dump_html_path)

    doc = Selector(html)
    raw_cards = doc.css(AD_CARD_SELECTOR)
    card_candidates = _select_ad_card_candidates(doc, debug=debug)
    if debug:
        LOGGER.debug("raw_cards(%s)=%s", AD_CARD_SELECTOR, len(raw_cards))
        LOGGER.debug("selected_card_candidates=%s", len(card_candidates))

    parsed: List[Dict[str, Any]] = []
    seen_ids = set()
    id_hits = 0

    for idx, card in enumerate(card_candidates):
        card_text = _node_text(card)
        if LIBRARY_ID_RE.search(card_text):
            id_hits += 1
        if debug and idx < 3:
            LOGGER.debug(
                "card[%s] text_len=%s preview=%s",
                idx,
                len(card_text),
                card_text[:220],
            )
        item = parse_ad_card(card)
        if not item:
            continue
        key = item.get("libraryID", "")
        if not key or key in seen_ids:
            continue
        seen_ids.add(key)
        parsed.append(item)

    if debug:
        LOGGER.debug("cards_with_library_id_regex=%s", id_hits)
        LOGGER.debug("parsed_ads=%s", len(parsed))
        LOGGER.debug("card_count_from_html=%s", _card_count_from_html(html))

    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Meta Ads Library crawler with scrapling")
    parser.add_argument("--page_id", default=DEFAULT_PAGE_ID)
    parser.add_argument("--scroll-rounds", type=int, default=8)
    parser.add_argument("--scroll-wait-ms", type=int, default=1200)
    parser.add_argument("--stable-rounds", type=int, default=3)
    parser.add_argument("--stable-max-rounds", type=int, default=18)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dump-html", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    _setup_logging(args.debug)

    url = build_ads_library_url(args.page_id)
    LOGGER.info("page_id=%s", _clean(args.page_id))
    LOGGER.info("url=%s", url)

    started = datetime.now()
    items = asyncio.run(
        crawl_meta_ads(
            url=url,
            scroll_rounds=max(args.scroll_rounds, 1),
            scroll_wait_ms=max(args.scroll_wait_ms, 300),
            debug=args.debug,
            stable_rounds=max(args.stable_rounds, 1),
            stable_max_rounds=max(args.stable_max_rounds, 1),
            dump_html_path=args.dump_html,
        )
    )

    payload = {
        "ok": True,
        "count": len(items),
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 2),
        "items": items,
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fp:
            fp.write(text)
        print(f"Saved: {args.output} ({len(items)} ads)")
        return

    print(text)


if __name__ == "__main__":
    main()
