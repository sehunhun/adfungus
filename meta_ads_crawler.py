from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse, unquote

from scrapling.fetchers import AsyncStealthySession, StealthyFetcher
from scrapling.parser import Selector

DEFAULT_PAGE_ID = "654845641046548"  # 946841158506716
LOGGER = logging.getLogger("meta-ads-crawler")
AD_LIST_CONTAINER_SELECTOR = (
    "div.xrvj5dj.x18m771g.x1p5oq8j.xp48ta0.x18d9i69.xtssl2i.xtqikln"
    ".x1na6gtj.xjewof7.x1l48g3s.x1vql8b3.x1m5622i"
)
AD_CARD_SELECTOR = f"{AD_LIST_CONTAINER_SELECTOR} div.xh8yej3"
SUMMARY_DETAIL_TEXT_RE = re.compile(
    r"(요약\s*세부\s*사항\s*보기|See\s+summary\s+details)",
    flags=re.IGNORECASE,
)


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


def build_ads_library_search_url(query: str, country: str = "KR") -> str:
    normalized_country = re.sub(r"[^A-Za-z]", "", _clean(country)).upper() or "KR"
    return (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
        f"&country={normalized_country}&q={quote(_clean(query))}"
    )


def build_ads_library_detail_url(library_id: str) -> str:
    return f"https://www.facebook.com/ads/library/?id={quote(_clean(library_id))}"


def build_default_output_path(page_id: str) -> str:
    normalized_page_id = re.sub(r"[^0-9A-Za-z_-]+", "", _clean(page_id)) or "unknown"
    timestamp = datetime.now().strftime("%m%d%H%M")
    return f"result-{normalized_page_id}-{timestamp}.json"


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


def _normalize_brand_key(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", _clean(value).lower())


def _normalize_logo_key(value: str) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    parsed = urlparse(raw)
    return f"{parsed.netloc.lower()}{parsed.path}"


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
        if re.search(
            r"(?:_s60x60|s60x60|profile|t51\.82787-19)", src, flags=re.IGNORECASE
        ):
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
    match = re.search(
        r"\b([A-Z0-9][A-Z0-9.-]+\.[A-Z]{2,})\b", value, flags=re.IGNORECASE
    )
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
    if re.search(
        r"(Library ID|Started running|Platforms|Open Dropdown|See ad details|Sponsored)",
        tail,
        flags=re.IGNORECASE,
    ):
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
        href = _normalize_outbound_url(
            (getattr(anchor, "attrib", {}) or {}).get("href", "")
        )
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
        if (
            not link_description
            and 8 <= len(text) <= 180
            and text not in {cta_text, link_title}
        ):
            if not re.search(
                r"(라이브러리 ID|Library ID|게재 시작|Started running|플랫폼|Platforms|광고 상세 정보 보기|See ad details|Open Dropdown)",
                text,
                flags=re.IGNORECASE,
            ):
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
    body = re.sub(
        r"\b\d{1,2}:\d{2}(?::\d{2})?\s*/\s*\d{1,2}:\d{2}(?::\d{2})?\b", " ", body
    )
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

    active = "활성" in card_text or bool(
        re.search(r"\bActive\b", card_text, flags=re.IGNORECASE)
    )
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
        "sameSourceLibraryIDs": [],
    }


def _parse_ads_from_html(
    html: str,
    *,
    debug: bool = False,
    limit: int = 0,
    context: str = "page",
) -> List[Dict[str, Any]]:
    if not html:
        return []

    doc = Selector(html)
    raw_cards = doc.css(AD_CARD_SELECTOR)
    card_candidates = _select_ad_card_candidates(doc, debug=debug)
    if limit > 0:
        card_candidates = card_candidates[:limit]

    if debug:
        LOGGER.debug("%s raw_cards(%s)=%s", context, AD_CARD_SELECTOR, len(raw_cards))
        LOGGER.debug("%s selected_card_candidates=%s", context, len(card_candidates))

    parsed: List[Dict[str, Any]] = []
    seen_ids = set()
    id_hits = 0

    for idx, card in enumerate(card_candidates):
        card_text = _node_text(card)
        if LIBRARY_ID_RE.search(card_text):
            id_hits += 1
        if debug and idx < 3:
            LOGGER.debug(
                "%s card[%s] text_len=%s preview=%s",
                context,
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
        LOGGER.debug("%s cards_with_library_id_regex=%s", context, id_hits)
        LOGGER.debug("%s parsed_ads=%s", context, len(parsed))
        LOGGER.debug("%s card_count_from_html=%s", context, _card_count_from_html(html))

    return parsed


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
        count = await page.evaluate(r"""
            () => {
              const text = document.body ? document.body.innerText : '';
              const matches = text.match(/(?:라이브러리\s*ID|Library\s+ID|Ad\s+library\s+ID)\s*:\s*\d+/gi);
              return matches ? matches.length : 0;
            }
            """)
        return int(count)
    except Exception:
        return -1


async def _wait_until_stable_cards(
    page: Any,
    *,
    selector: str,
    stable_rounds: int,
    max_rounds: int,
    wait_ms: int,
    debug: bool,
) -> None:
    idle_rounds = 0
    prev_library_id_count = -1
    prev_scroll_height = -1

    for idx in range(max_rounds):
        try:
            metrics = await page.evaluate(
                r"""
                (sel) => {
                  window.scrollTo(0, document.body.scrollHeight);
                  const text = document.body ? document.body.innerText : '';
                  const matches = text.match(/(?:라이브러리\s*ID|Library\s+ID|Ad\s+library\s+ID)\s*:\s*\d+/gi);
                  const scrollHeight = document.body ? document.body.scrollHeight : 0;
                  const bottomReached = window.scrollY + window.innerHeight >= scrollHeight - 8;
                  return {
                    count: document.querySelectorAll(sel).length,
                    libraryIdCount: matches ? matches.length : 0,
                    scrollHeight,
                    scrollY: window.scrollY,
                    innerHeight: window.innerHeight,
                    bottomReached,
                  };
                }
                """,
                selector,
            )
        except Exception:
            metrics = {
                "count": -1,
                "libraryIdCount": -1,
                "scrollHeight": -1,
                "scrollY": -1,
                "innerHeight": -1,
                "bottomReached": False,
            }

        count = int(metrics.get("count", -1))
        library_id_count = int(metrics.get("libraryIdCount", -1))
        scroll_height = int(metrics.get("scrollHeight", -1))
        scroll_y = int(metrics.get("scrollY", -1))
        inner_height = int(metrics.get("innerHeight", -1))
        bottom_reached = bool(metrics.get("bottomReached"))

        height_stable = scroll_height == prev_scroll_height and scroll_height >= 0
        ids_stable = library_id_count == prev_library_id_count and library_id_count > 0
        if height_stable and ids_stable and bottom_reached:
            idle_rounds += 1
        else:
            idle_rounds = 0

        if debug:
            LOGGER.debug(
                "stable-scroll round=%s selector=%s count=%s library_id_text_count=%s scroll_height=%s scroll_y=%s inner_height=%s bottom_reached=%s idle_rounds=%s/%s",
                idx + 1,
                selector,
                count,
                library_id_count,
                scroll_height,
                scroll_y,
                inner_height,
                bottom_reached,
                idle_rounds,
                stable_rounds,
            )

        if idle_rounds >= stable_rounds:
            if debug:
                LOGGER.debug(
                    "stable-scroll satisfied at round=%s count=%s library_id_text_count=%s scroll_height=%s",
                    idx + 1,
                    count,
                    library_id_count,
                    scroll_height,
                )
            return

        prev_library_id_count = library_id_count
        prev_scroll_height = scroll_height
        try:
            await page.wait_for_timeout(wait_ms)
        except Exception:
            await asyncio.sleep(wait_ms / 1000)


async def _wait_until_min_library_ids(
    page: Any,
    *,
    target_count: int,
    max_rounds: int,
    wait_ms: int,
    debug: bool,
) -> None:
    for idx in range(max_rounds):
        library_id_count = await _count_library_ids_on_page(page)
        card_count = await _count_dom_matches(page, AD_CARD_SELECTOR)
        if debug:
            LOGGER.debug(
                "target-load round=%s scoped_card_selector_count=%s library_id_text_count=%s target=%s",
                idx + 1,
                card_count,
                library_id_count,
                target_count,
            )
        if library_id_count >= target_count:
            if debug:
                LOGGER.debug(
                    "target-load satisfied library_id_text_count=%s target=%s",
                    library_id_count,
                    target_count,
                )
            return
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(wait_ms)
        except Exception:
            await asyncio.sleep(wait_ms / 1000)


async def _mark_top_ad_cards(page: Any, *, limit: int, debug: bool) -> List[str]:
    try:
        ids = await page.evaluate(
            r"""
            ({ selector, limit }) => {
              const idRegex = /(?:라이브러리\s*ID|Library\s+ID|Ad\s+library\s+ID)\s*:\s*(\d+)/i;
              document.querySelectorAll('[data-meta-ad-crawl-index]').forEach((el) => {
                el.removeAttribute('data-meta-ad-crawl-index');
              });

              const byId = new Map();
              document.querySelectorAll(selector).forEach((el) => {
                const text = el.innerText || '';
                const match = text.match(idRegex);
                if (!match) return;
                const id = match[1];
                const current = byId.get(id);
                if (!current || text.length > current.textLength) {
                  byId.set(id, { el, textLength: text.length });
                }
              });

              const ids = Array.from(byId.keys()).slice(0, limit > 0 ? limit : byId.size);
              ids.forEach((id, index) => {
                const item = byId.get(id);
                if (item && item.el) {
                  item.el.setAttribute('data-meta-ad-crawl-index', String(index));
                }
              });
              return ids;
            }
            """,
            {"selector": AD_CARD_SELECTOR, "limit": limit},
        )
        marked_ids = [str(value) for value in ids or []]
        if debug:
            LOGGER.debug("marked_top_ad_cards=%s ids=%s", len(marked_ids), marked_ids)
        return marked_ids
    except Exception as exc:
        if debug:
            LOGGER.debug("mark_top_ad_cards_failed error=%s", exc)
        return []


async def _close_active_dialog(page: Any) -> None:
    close_selectors = [
        '[aria-label="Close"]',
        '[aria-label="닫기"]',
        '[role="button"][aria-label="Close"]',
        '[role="button"][aria-label="닫기"]',
    ]
    for selector in close_selectors:
        try:
            locator = page.locator(selector).last
            if callable(locator):
                locator = locator()
            if await locator.count() > 0:
                await locator.click(timeout=1500)
                await page.wait_for_timeout(700)
                return
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(700)
    except Exception:
        pass


async def _extract_dialog_html(page: Any) -> str:
    selectors = ['div[role="dialog"]', '[aria-modal="true"]']
    for selector in selectors:
        try:
            locator = page.locator(selector).last
            if callable(locator):
                locator = locator()
            if await locator.count() > 0:
                html = await locator.inner_html(timeout=3000)
                if html:
                    return str(html)
        except Exception:
            pass
    try:
        content = await page.content()
        return str(content or "")
    except Exception:
        return ""


async def _extract_page_text(page: Any) -> str:
    for selector in ['div[role="dialog"]', '[aria-modal="true"]', "body"]:
        try:
            locator = page.locator(selector).last if selector != "body" else page.locator(selector)
            if callable(locator):
                locator = locator()
            if await locator.count() > 0:
                text = await locator.inner_text(timeout=3000)
                if text:
                    return _clean(text)
        except Exception:
            pass
    try:
        return _clean(await page.content())
    except Exception:
        return ""


async def _click_first_text(page: Any, texts: List[str], *, timeout: int = 3500) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=False)
            if int(await locator.count()) > 0:
                await locator.nth(0).scroll_into_view_if_needed(timeout=timeout)
                await locator.nth(0).click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


def _parse_advertiser_info_text(text: str) -> Dict[str, str]:
    cleaned = _clean(text)
    page_id_match = re.search(r"\bID\s*:\s*(\d{6,})", cleaned)

    handle_match = re.search(r"@[A-Za-z0-9._]+", cleaned)
    follower_matches = list(
        re.finditer(
            r"(?:팔로워\s*([0-9,.]+\s*(?:천|만|억)?\s*명?)|([0-9,.]+\s*[KMB]?)\s+followers)",
            cleaned,
            flags=re.IGNORECASE,
        )
    )

    followers = [_clean(match.group(1) or match.group(2)) for match in follower_matches]
    handle_index = handle_match.start() if handle_match else -1
    instagram_followers = ""
    if handle_index >= 0:
        for match in follower_matches:
            if match.start() > handle_index:
                instagram_followers = _clean(match.group(1) or match.group(2))
                break

    return {
        "page_id": page_id_match.group(1) if page_id_match else "",
        "facebook_followers_text": followers[0] if followers else "",
        "instagram_handle": handle_match.group(0) if handle_match else "",
        "instagram_followers_text": instagram_followers,
    }


def _group_advertiser_candidates(ads: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for ad in ads:
        brand = _clean(ad.get("brand"))
        brand_logo = _clean(ad.get("brandLogo"))
        library_id = _clean(ad.get("libraryID"))
        if not brand or not library_id:
            continue
        key = _normalize_brand_key(brand)
        if key not in grouped:
            grouped[key] = {
                "brand": brand,
                "brand_logo_url": brand_logo,
                "representative_library_id": library_id,
                "library_ids": [],
                "matched_ads_count": 0,
            }
        grouped[key]["matched_ads_count"] += 1
        grouped[key]["library_ids"].append(library_id)

    candidates = sorted(
        grouped.values(),
        key=lambda item: item["matched_ads_count"],
        reverse=True,
    )
    return candidates[:limit] if limit > 0 else candidates


async def _fetch_advertiser_detail(
    candidate: Dict[str, Any],
    *,
    debug: bool,
) -> Dict[str, Any] | None:
    async with AsyncStealthySession(
        headless=True,
        real_chrome=os.getenv("META_ADS_REAL_CHROME", "true").lower()
        not in {"0", "false", "no"},
        timeout=120000,
        max_pages=1,
    ) as session:
        return await _fetch_advertiser_detail_with_session(
            session,
            candidate,
            debug=debug,
        )


async def _fetch_advertiser_detail_with_session(
    session: Any,
    candidate: Dict[str, Any],
    *,
    debug: bool,
) -> Dict[str, Any] | None:
    library_ids = [str(value) for value in candidate.get("library_ids", []) if value]
    if not library_ids:
        return None

    for library_id in library_ids[:2]:
        detail: Dict[str, str] = {}

        async def page_action(page: Any) -> None:
            nonlocal detail
            for _ in range(40):
                try:
                    current_url = str(getattr(page, "url", "") or "")
                    page_id = parse_qs(urlparse(current_url).query).get("view_all_page_id", [""])[0]
                    if page_id:
                        detail["page_id"] = page_id
                        return
                except Exception:
                    pass
                await page.wait_for_timeout(100)

        try:
            await session.fetch(
                build_ads_library_detail_url(library_id),
                page_action=page_action,
                timeout=30000,
                network_idle=False,
            )
        except Exception as exc:
            if debug:
                LOGGER.debug(
                    "advertiser_detail_fetch_failed library_id=%s error=%s",
                    library_id,
                    exc,
                )
            continue

        if detail.get("page_id"):
            merged = dict(candidate)
            merged.update(detail)
            merged["representative_library_id"] = library_id
            merged["source"] = "meta_ads_library"
            return merged

    return None


def _dedupe_advertiser_results(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_page_id: Dict[str, Dict[str, Any]] = {}
    for item in items:
        page_id = _clean(item.get("page_id"))
        if not page_id:
            continue
        current = by_page_id.get(page_id)
        if current is None:
            by_page_id[page_id] = item
            continue
        current["matched_ads_count"] += int(item.get("matched_ads_count") or 0)
        current["library_ids"] = sorted(
            set(current.get("library_ids", [])) | set(item.get("library_ids", []))
        )
        if not current.get("instagram_handle") and item.get("instagram_handle"):
            current["instagram_handle"] = item["instagram_handle"]
            current["instagram_followers_text"] = item.get("instagram_followers_text", "")
    return sorted(
        by_page_id.values(),
        key=lambda item: item.get("matched_ads_count", 0),
        reverse=True,
    )


def _merge_same_source_groups(
    main_items: List[Dict[str, Any]],
    popup_groups: List[List[Dict[str, Any]]],
    debug: bool,
) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    same_source_by_id: Dict[str, set[str]] = {}

    for item in main_items:
        library_id = str(item.get("libraryID", "")).strip()
        if not library_id:
            continue
        by_id[library_id] = item
        same_source_by_id.setdefault(library_id, set()).update(
            str(value) for value in item.get("sameSourceLibraryIDs", []) if value
        )

    for group_idx, group in enumerate(popup_groups):
        group_ids = [str(item.get("libraryID", "")).strip() for item in group]
        group_ids = [value for value in dict.fromkeys(group_ids) if value]
        if debug:
            LOGGER.debug("summary_group[%s] ids=%s", group_idx, group_ids)
        if len(group_ids) < 2:
            continue

        for item in group:
            library_id = str(item.get("libraryID", "")).strip()
            if not library_id:
                continue
            if library_id not in by_id:
                by_id[library_id] = item
            same_source_by_id.setdefault(library_id, set()).update(
                other_id for other_id in group_ids if other_id != library_id
            )

    merged: List[Dict[str, Any]] = []
    emitted = set()
    for item in main_items:
        library_id = str(item.get("libraryID", "")).strip()
        if not library_id or library_id in emitted:
            continue
        item["sameSourceLibraryIDs"] = sorted(same_source_by_id.get(library_id, set()))
        merged.append(item)
        emitted.add(library_id)

    for library_id, item in by_id.items():
        if library_id in emitted:
            continue
        item["sameSourceLibraryIDs"] = sorted(same_source_by_id.get(library_id, set()))
        merged.append(item)
        emitted.add(library_id)

    if debug:
        LOGGER.debug("same_source_groups=%s merged_ads=%s", len(popup_groups), len(merged))
    return merged


async def _collect_summary_popup_groups(
    page: Any,
    *,
    debug: bool,
    limit: int,
    wait_ms: int,
) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    seen_group_keys = set()
    summary_texts = ["See summary details", "요약 세부 사항 보기"]
    marked_ids = await _mark_top_ad_cards(page, limit=limit, debug=debug)
    clicked = 0

    for card_index, library_id in enumerate(marked_ids):
        card = page.locator(f'[data-meta-ad-crawl-index="{card_index}"]')
        button = None
        matched_text = ""
        for text in summary_texts:
            try:
                candidate = card.get_by_text(text, exact=False)
                if int(await candidate.count()) > 0:
                    button = candidate.nth(0)
                    matched_text = text
                    break
            except Exception:
                pass

        if button is None:
            if debug:
                LOGGER.debug(
                    "summary_button_not_found card_index=%s library_id=%s",
                    card_index,
                    library_id,
                )
            continue

        try:
            await button.scroll_into_view_if_needed(timeout=3000)
            await button.click(timeout=5000)
            await page.wait_for_timeout(wait_ms)
        except Exception as exc:
            if debug:
                LOGGER.debug(
                    "summary_click_failed card_index=%s library_id=%s text=%s error=%s",
                    card_index,
                    library_id,
                    matched_text,
                    exc,
                )
            continue

        dialog_html = await _extract_dialog_html(page)
        popup_items = _parse_ads_from_html(
            dialog_html,
            debug=debug,
            limit=0,
            context=f"summary_popup[{clicked}]",
        )
        if popup_items:
            group_key = tuple(
                sorted(
                    str(item.get("libraryID", "")).strip()
                    for item in popup_items
                    if item.get("libraryID")
                )
            )
            if group_key and group_key not in seen_group_keys:
                seen_group_keys.add(group_key)
                groups.append(popup_items)
        if debug:
            LOGGER.debug(
                "summary_popup[%s] source_library_id=%s parsed_ads=%s ids=%s",
                clicked,
                library_id,
                len(popup_items),
                [item.get("libraryID") for item in popup_items],
            )

        clicked += 1
        await _close_active_dialog(page)

    return groups


async def crawl_meta_ads(
    url: str,
    scroll_wait_ms: int = 1200,
    debug: bool = False,
    stable_rounds: int = 3,
    stable_max_rounds: int = 18,
    dump_html_path: str = "",
    limit: int = 0,
    filter_page_id: Optional[str] = None,  # Backward-compatible no-op (q search keeps all results).
) -> List[Dict[str, Any]]:
    StealthyFetcher.configure(adaptive=True)

    session_config: Dict[str, Any] = {
        "headless": True,
        "real_chrome": os.getenv("META_ADS_REAL_CHROME", "true").lower()
        not in {"0", "false", "no"},
        "timeout": 120000,
        "max_pages": 1,
    }

    if debug:
        LOGGER.debug("fetch url=%s filter_page_id(no-op)=%s", url, filter_page_id)

    popup_groups: List[List[Dict[str, Any]]] = []

    async with AsyncStealthySession(**session_config) as session:

        async def page_action(page: Any) -> None:
            await page.wait_for_timeout(3000)
            if limit > 0:
                await _wait_until_min_library_ids(
                    page,
                    target_count=limit,
                    max_rounds=stable_max_rounds,
                    wait_ms=scroll_wait_ms,
                    debug=debug,
                )
            else:
                await _wait_until_stable_cards(
                    page,
                    selector=AD_CARD_SELECTOR,
                    stable_rounds=stable_rounds,
                    max_rounds=stable_max_rounds,
                    wait_ms=scroll_wait_ms,
                    debug=debug,
                )
            popup_groups.extend(
                await _collect_summary_popup_groups(
                    page,
                    debug=debug,
                    limit=limit,
                    wait_ms=scroll_wait_ms,
                )
            )
            await page.wait_for_timeout(1500)

        result = await session.fetch(
            url, page_action=page_action, timeout=120000, network_idle=True
        )

    html = _extract_html(result)
    if debug:
        LOGGER.debug("html_length=%s", len(html))
        LOGGER.debug(
            "library_id_regex_matches_in_html=%s", _library_id_count_from_html(html)
        )
    if not html:
        return []

    if dump_html_path:
        with open(dump_html_path, "w", encoding="utf-8") as fp:
            fp.write(html)
        LOGGER.info("saved html dump: %s", dump_html_path)

    parsed = _parse_ads_from_html(html, debug=debug, limit=limit, context="page")
    return _merge_same_source_groups(parsed, popup_groups, debug=debug)


async def search_meta_advertisers(
    query: str,
    *,
    country: str = "KR",
    limit: int = 10,
    scroll_wait_ms: int = 1200,
    debug: bool = False,
    include_timings: bool = False,
    **kwargs,  # Accept detail_concurrency for backward compatibility
) -> List[Dict[str, Any]]:
    timings: Dict[str, float] = {}
    started = time.perf_counter()
    StealthyFetcher.configure(adaptive=True)
    url = build_ads_library_search_url(query, country=country)
    if debug:
        LOGGER.info(f"[Search] Starting search for query: '{query}' at URL: {url}")

    results: List[Dict[str, Any]] = []

    async with AsyncStealthySession(
        headless=True,
        real_chrome=os.getenv("META_ADS_REAL_CHROME", "true").lower()
        not in {"0", "false", "no"},
        timeout=120000,
        max_pages=1,
    ) as session:
        if debug:
            LOGGER.info("[Search] AsyncStealthySession started")

        async def page_action(page: Any) -> None:
            if debug:
                LOGGER.info("[Search] Page action triggered, waiting for initial load...")
            await page.wait_for_timeout(3000)
            
            # 0. 일반적인 방해 요소(쿠키 배너 등) 닫기 시도
            try:
                overlay_selectors = ['[aria-label="닫기"]', '[aria-label="Close"]', 'button:has-text("모두 허용")', 'button:has-text("Allow all cookies")']
                for sel in overlay_selectors:
                    overlay = page.locator(sel).first
                    if await overlay.count() > 0:
                        if debug: LOGGER.info(f"[Search] Closing overlay: {sel}")
                        await overlay.click(timeout=2000)
            except Exception:
                pass

            # 1. 검색창 클릭하여 팝업 활성화
            if debug:
                LOGGER.info("[Search] Looking for keyword/advertiser search input...")
            
            # 사용자가 제공한 HTML 구조 반영: type="search" 및 관련 placeholder 사용
            search_input = page.locator('input[type="search"][placeholder*="검색"], input[type="search"][placeholder*="Search"]').first
            
            try:
                if await search_input.count() > 0:
                    if debug:
                        LOGGER.info("[Search] Keyword search input found, clicking (forced)...")
                    # scrollTo를 명시적으로 수행하여 가시성 확보 시도
                    await search_input.scroll_into_view_if_needed()
                    await search_input.click(force=True, timeout=5000)
                    await page.wait_for_timeout(2000)
                else:
                    if debug:
                        LOGGER.warning("[Search] Keyword search input NOT found. Retrying with fallback selector...")
                    # 백업: 일반적인 input 태그 중 검색 관련
                    search_input = page.locator('input[placeholder*="광고주"], input[placeholder*="advertiser"]').first
                    if await search_input.count() > 0:
                        await search_input.click(force=True)
                        await page.wait_for_timeout(2000)
            except Exception as e:
                LOGGER.error(f"[Search] Error clicking search input: {e}")
                # 클릭 실패 시 JavaScript로 직접 클릭 시도
                try:
                    if debug: LOGGER.info("[Search] Attempting JS click as fallback...")
                    await page.evaluate('(el) => el.click()', await search_input.element_handle())
                    await page.wait_for_timeout(2000)
                except Exception as js_e:
                    LOGGER.error(f"[Search] JS click also failed: {js_e}")
            
            # 2. 광고주 팝업 요소 파싱
            if debug:
                LOGGER.info("[Search] Looking for advertiser options (li[id^='pageID:'])...")
            advertiser_options = page.locator('li[id^="pageID:"]')
            count = await advertiser_options.count()
            
            if debug:
                LOGGER.info(f"[Search] Found {count} advertiser options in popup")
                
            for i in range(min(count, limit)):
                option = advertiser_options.nth(i)
                try:
                    html = await option.inner_html()
                    option_id = await option.get_attribute("id") or ""
                    page_id = option_id.replace("pageID:", "").strip()
                    
                    doc = Selector(html)
                    brand = _node_text(doc.css('[role="heading"]').first)
                    logo_node = doc.css('img').first
                    logo = ""
                    if logo_node:
                        logo = _clean(logo_node.attrib.get("src", ""))
                    
                    extra_text = _node_text(doc)
                    info = _parse_advertiser_info_text(extra_text)
                    
                    if debug:
                        LOGGER.info(f"[Search] Extracted brand: {brand} (Page ID: {page_id}), Logo: {logo[:30]}...")

                    results.append({
                        "page_id": page_id,
                        "brand": brand or info.get("instagram_handle") or f"Page {page_id}",
                        "brand_logo_url": logo,
                        "facebook_followers_text": info.get("facebook_followers_text"),
                        "instagram_handle": info.get("instagram_handle"),
                        "instagram_followers_text": info.get("instagram_followers_text"),
                        "source": "advertiser_popup",
                        "matched_ads_count": 0 
                    })
                except Exception as e:
                    LOGGER.error(f"[Search] Error extracting option {i}: {e}")

        if debug:
            LOGGER.info(f"[Search] Fetching URL: {url}")
        try:
            await session.fetch(
                url,
                page_action=page_action,
                timeout=120000,
                network_idle=True,
                wait_selector='input[type="search"]',
                wait_selector_state="visible",
            )
        except Exception as e:
            LOGGER.error(f"[Search] Session fetch failed: {e}")

    timings["total_seconds"] = round(time.perf_counter() - started, 2)
    if debug:
        LOGGER.info(f"[Search] Search completed in {timings['total_seconds']}s. Found {len(results)} results.")
    
    if include_timings and results:
        results[0]["_timings"] = timings
        
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meta Ads Library crawler with scrapling"
    )
    parser.add_argument("--page_id", default=DEFAULT_PAGE_ID)
    parser.add_argument("--scroll-wait-ms", type=int, default=1200)
    parser.add_argument("--stable-rounds", type=int, default=3)
    parser.add_argument("--stable-max-rounds", type=int, default=18)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dump-html", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--no-output", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--search-query", default="")
    parser.add_argument("--search-country", default="KR")
    parser.add_argument("--detail-concurrency", type=int, default=4)
    args = parser.parse_args()

    _setup_logging(args.debug)

    if args.search_query:
        started = datetime.now()
        items = asyncio.run(
            search_meta_advertisers(
                args.search_query,
                country=args.search_country,
                limit=args.limit if args.limit > 0 else 30,
                detail_concurrency=max(args.detail_concurrency, 1),
                scroll_wait_ms=max(args.scroll_wait_ms, 300),
                debug=args.debug,
                include_timings=True,
            )
        )
        timings = items[0].pop("_timings", {}) if items and "_timings" in items[0] else {}
        for item in items:
            item.pop("_timings", None)
        payload = {
            "ok": True,
            "query": args.search_query,
            "country": args.search_country,
            "count": len(items),
            "elapsed_seconds": round((datetime.now() - started).total_seconds(), 2),
            "timings": timings,
            "items": items,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    url = build_ads_library_url(args.page_id)
    LOGGER.info("page_id=%s", _clean(args.page_id))
    LOGGER.info("url=%s", url)

    started = datetime.now()
    items = asyncio.run(
        crawl_meta_ads(
            url=url,
            scroll_wait_ms=max(args.scroll_wait_ms, 300),
            debug=args.debug,
            stable_rounds=max(args.stable_rounds, 1),
            stable_max_rounds=max(args.stable_max_rounds, 1),
            dump_html_path=args.dump_html,
            limit=max(args.limit, 0),
        )
    )

    payload = {
        "ok": True,
        "count": len(items),
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 2),
        "items": items,
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    output_path = (
        ""
        if args.no_output
        else (args.output or build_default_output_path(args.page_id))
    )
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fp:
            fp.write(text)
        print(f"Saved: {output_path} ({len(items)} ads)")
        return

    print(text)


if __name__ == "__main__":
    main()
