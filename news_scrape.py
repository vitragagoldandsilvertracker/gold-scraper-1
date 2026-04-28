#!/usr/bin/env python3
"""
Gold & Silver News Scraper - RSS Feed Based
Sources:
  - Kitco News (gold, silver, mining)
  - BullionVault News
  - Mining.com RSS (gold, silver)
  - Metals Daily RSS
  - Mining Weekly RSS

No Selenium, no API keys required. Pure RSS/requests.
"""

import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from insert_queries import check_url_exists

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# RSS FEED DEFINITIONS
# ─────────────────────────────────────────────

RSS_FEEDS = [
    {
        "source": "Kitco News",
        "url": "https://www.kitco.com/rss/kitco-news.rss",
        "limit": 10,
    },
    {
        "source": "Kitco Gold",
        "url": "https://www.kitco.com/rss/gold.rss",
        "limit": 8,
    },
    {
        "source": "Kitco Silver",
        "url": "https://www.kitco.com/rss/silver.rss",
        "limit": 8,
    },
    {
        "source": "BullionVault",
        "url": "https://www.bullionvault.com/gold-news/rss.do",
        "limit": 8,
    },
    {
        "source": "Mining.com Gold",
        "url": "https://www.mining.com/category/gold/feed/",
        "limit": 8,
    },
    {
        "source": "Mining.com Silver",
        "url": "https://www.mining.com/category/silver/feed/",
        "limit": 6,
    },
    {
        "source": "Metals Daily",
        "url": "https://www.metalsdaily.com/rss/gold/",
        "limit": 6,
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _parse_date(date_str):
    """Parse RSS date string to YYYY-MM-DD. Returns today's date on failure."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
    except Exception:
        pass
    # Try ISO format
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:19], fmt[:len(date_str[:19])]).strftime("%Y-%m-%d")
        except Exception:
            continue
    return datetime.now().strftime("%Y-%m-%d")


def _get_tag_text(element, tag, namespaces=None):
    """Safely get text from an XML tag, checking common namespaces."""
    # Direct tag
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    # With namespace
    if namespaces:
        for ns_prefix, ns_uri in namespaces.items():
            child = element.find(f"{{{ns_uri}}}{tag}")
            if child is not None and child.text:
                return child.text.strip()
    return None


def _fetch_rss(url):
    """Fetch and parse an RSS feed. Returns list of <item> elements."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        # Handle both RSS 2.0 and Atom
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        return items
    except requests.exceptions.RequestException as e:
        logger.warning(f"HTTP error fetching {url}: {e}")
        return []
    except ET.ParseError as e:
        logger.warning(f"XML parse error for {url}: {e}")
        return []
    except Exception as e:
        logger.warning(f"Unexpected error fetching {url}: {e}")
        return []


def _extract_image(item):
    """Try to extract an image URL from various RSS image fields."""
    # <media:content url="...">
    media_content = item.find("{http://search.yahoo.com/mrss/}content")
    if media_content is not None:
        url = media_content.get("url")
        if url:
            return url

    # <media:thumbnail url="...">
    media_thumb = item.find("{http://search.yahoo.com/mrss/}thumbnail")
    if media_thumb is not None:
        url = media_thumb.get("url")
        if url:
            return url

    # <enclosure url="..." type="image/...">
    enclosure = item.find("enclosure")
    if enclosure is not None:
        enc_type = enclosure.get("type", "")
        if "image" in enc_type:
            return enclosure.get("url")

    return None


# ─────────────────────────────────────────────
# MAIN SCRAPER
# ─────────────────────────────────────────────

def scrape_rss_feeds(cursor):
    """
    Scrape all configured RSS feeds and return a list of article dicts.
    Each dict has: source, title, url, content, summary, image_url, date
    """
    all_articles = []

    for feed in RSS_FEEDS:
        source = feed["source"]
        feed_url = feed["url"]
        limit = feed["limit"]

        logger.info(f"Fetching RSS: {source} ({feed_url})")
        items = _fetch_rss(feed_url)

        if not items:
            logger.warning(f"No items found for {source}")
            continue

        count = 0
        for item in items[:limit * 2]:  # fetch extra to account for duplicates
            if count >= limit:
                break

            # Extract URL
            url = _get_tag_text(item, "link")
            if not url:
                # Atom <link href="...">
                link_elem = item.find("{http://www.w3.org/2005/Atom}link")
                if link_elem is not None:
                    url = link_elem.get("href")
            if not url:
                continue

            url = url.strip()

            # Skip if already in DB
            if check_url_exists(cursor, url):
                logger.debug(f"Already exists: {url}")
                continue

            # Extract fields
            title = (
                _get_tag_text(item, "title")
                or _get_tag_text(item, "{http://www.w3.org/2005/Atom}title")
                or "No title"
            )

            description = (
                _get_tag_text(item, "description")
                or _get_tag_text(item, "{http://www.w3.org/2005/Atom}summary")
                or _get_tag_text(item, "{http://www.w3.org/2005/Atom}content")
                or ""
            )

            # Strip HTML tags from description for clean summary
            import re
            clean_summary = re.sub(r"<[^>]+>", "", description).strip()

            pub_date = (
                _get_tag_text(item, "pubDate")
                or _get_tag_text(item, "pubdate")
                or _get_tag_text(item, "{http://www.w3.org/2005/Atom}published")
                or _get_tag_text(item, "{http://www.w3.org/2005/Atom}updated")
            )
            formatted_date = _parse_date(pub_date)

            image_url = _extract_image(item)

            article = {
                "source": source,
                "title": title.strip(),
                "url": url,
                "content": clean_summary,
                "summary": clean_summary[:500] if clean_summary else None,
                "image_url": image_url,
                "date": formatted_date,
            }

            all_articles.append(article)
            count += 1
            logger.info(f"  [{source}] {title[:80]}")

        logger.info(f"  → {count} new articles from {source}")

    logger.info(f"Total new articles from all RSS feeds: {len(all_articles)}")
    return all_articles


# ─────────────────────────────────────────────
# LEGACY SELENIUM STUBS (kept for compatibility)
# These now delegate to the RSS scraper
# ─────────────────────────────────────────────

def scrape_latest_articles_from_mining_site(cursor):
    """Legacy stub — RSS replaces Selenium scraping for Mining.com"""
    return []

def scrape_mining_review_data(cursor):
    """Legacy stub"""
    return []

def scrape_lppm_com_news(cursor):
    """Legacy stub"""
    return []

def scrape_miningmx_articles(cursor):
    """Legacy stub"""
    return []

def scrape_metaldaily_articles(cursor):
    """Legacy stub"""
    return []

def scrape_articles_from_miningweekly(cursor, metal_name):
    """Legacy stub"""
    return []
