"""
Article fetcher v4
— 3 RSS sources: MIT Tech Review, BBC Tech, NYTimes Tech
— DOMAIN_KEYWORDS map: 10 domains (Robotics, Space, AI, Finance, Physics,
  Chemistry, Biology/Medicine, Engineering, Mathematics, Computer Science)
— fetch_articles_by_domains(): fetches 20-25 articles, ensures every
  active domain has ≥1 article, caps at 30-35 filtered before dedup
— Each article enriched with: image_url, short_summary (30-40 words),
  full_content (for Gemini), article_url, matched_domains list
— fetch_articles_from_url() kept for the "From Source" button
"""

import uuid
import re
import logging
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}
MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/112.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────────────────────────
# 3 PRIMARY RSS SOURCES
# ─────────────────────────────────────────────────────────────────────────────

PRIMARY_SOURCES = [
    {
        "name": "MIT Technology Review",
        "url":  "https://www.technologyreview.com/feed/",
    },
    {
        "name": "BBC Tech",
        "url":  "https://feeds.bbci.co.uk/news/technology/rss.xml",
    },
    {
        "name": "NYTimes Tech",
        "url":  "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    },
]

# Legacy full list kept for /api/sources endpoint & fetch_articles_from_source
RSS_SOURCES = PRIMARY_SOURCES + [
    {"name": "The Verge",    "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Wired",        "url": "https://www.wired.com/feed/rss"},
    {"name": "TechCrunch",   "url": "https://techcrunch.com/feed/"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Reuters Tech", "url": "https://feeds.reuters.com/reuters/technologyNews"},
]

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN → KEYWORDS MAP  (canonical; used everywhere)
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "Robotics": [
        "robot", "robotics", "automation", "drone", "autonomous vehicle",
        "self-driving", "mechanical arm", "cobots", "industrial robot",
    ],
    "Space": [
        "space", "nasa", "astronomy", "rocket", "satellite", "mars",
        "moon", "orbit", "telescope", "astrophysics", "spacex", "isro",
        "galaxy", "universe", "cosmos", "black hole",
    ],
    "AI": [
        "artificial intelligence", "machine learning", "deep learning",
        "neural network", "large language model", "llm", "chatgpt",
        "generative ai", "ai model", "transformer", "diffusion model",
        "reinforcement learning", "computer vision", "nlp",
    ],
    "Finance": [
        "stock", "market", "finance", "investment", "cryptocurrency",
        "bitcoin", "blockchain", "economy", "gdp", "interest rate",
        "inflation", "venture capital", "ipo", "fintech", "banking",
    ],
    "Physics": [
        "physics", "quantum", "particle", "electron", "photon",
        "semiconductor", "superconductor", "fusion", "nuclear",
        "relativity", "dark matter", "dark energy", "hadron",
    ],
    "Chemistry": [
        "chemistry", "molecule", "compound", "polymer", "catalyst",
        "chemical", "reaction", "synthesis", "nanotechnology",
        "material science", "carbon", "hydrogen", "protein",
    ],
    "Biology/Medicine": [
        "biology", "medicine", "gene", "dna", "rna", "vaccine",
        "crispr", "cancer", "drug", "clinical trial", "bacteria",
        "virus", "cell", "brain", "neuroscience", "genomics",
        "biotech", "pharmaceutical", "health", "disease",
    ],
    "Engineering": [
        "engineering", "infrastructure", "bridge", "circuit",
        "processor", "chip", "microchip", "3d printing",
        "manufacturing", "architecture", "renewable energy",
        "solar panel", "battery", "electric vehicle", "ev",
    ],
    "Mathematics": [
        "mathematics", "algorithm", "computation", "theorem",
        "statistics", "probability", "cryptography", "topology",
        "calculus", "graph theory", "optimization", "simulation",
    ],
    "Computer Science": [
        "software", "programming", "cybersecurity", "cloud",
        "database", "operating system", "compiler", "open source",
        "api", "microservices", "kubernetes", "devops", "web",
        "mobile app", "internet", "network", "hack", "data science",
    ],
}

ALL_DOMAINS: list[str] = list(DOMAIN_KEYWORDS.keys())


# ─────────────────────────────────────────────────────────────────────────────
# MAIN: domain-aware fetch  (called by the new /api/articles/refresh endpoint)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_articles_by_domains(
    active_domains: list[str] | None = None,
    target_total: int = 25,
) -> list[dict]:
    """
    Fetch articles from the 3 primary sources.
    1. Pull ~15 entries per source (45 raw).
    2. Filter: keep only articles matching any keyword of any active domain.
    3. Cap the filtered pool at 35.
    4. Guarantee ≥1 article per active domain (fill from pool or skip).
    5. Enrich every kept article: image, short_summary, matched_domains.
    Returns 20-25 articles (target_total).
    """
    domains = active_domains or ALL_DOMAINS
    kw_map  = {d: [k.lower() for k in DOMAIN_KEYWORDS.get(d, [])] for d in domains}

    # ── 1. Fetch raw entries from 3 primary sources ────────────────────────
    raw: list[dict] = []
    for src in PRIMARY_SOURCES:
        try:
            entries = _fetch_rss_raw(src["url"], src["name"], limit=18)
            raw.extend(entries)
            log.info("RSS %s: %d entries", src["name"], len(entries))
        except Exception as e:
            log.warning("RSS failed %s: %s", src["name"], e)

    if not raw:
        log.error("All primary RSS sources failed")
        return []

    # ── 2. Filter + score by domain match ─────────────────────────────────
    scored: list[tuple[int, list[str], dict]] = []
    for art in raw:
        text = (art["title"] + " " + art.get("content", "")).lower()
        matched = [d for d, kws in kw_map.items() if any(k in text for k in kws)]
        if matched:
            scored.append((len(matched), matched, art))

    # Sort: most-domains-matched first
    scored.sort(key=lambda x: x[0], reverse=True)

    # Cap filtered pool at 35
    pool = scored[:35]

    # ── 3. Guarantee ≥1 per domain ─────────────────────────────────────────
    domain_covered: set[str] = set()
    selected: list[dict] = []
    pool_ids: set[str] = set()

    def _add(art: dict, matched_doms: list[str]) -> None:
        art["matched_domains"] = matched_doms
        selected.append(art)
        pool_ids.add(art["id"])
        domain_covered.update(matched_doms)

    # First pass: pick best article per domain
    for domain in domains:
        if domain in domain_covered:
            continue
        for _, matched, art in pool:
            if art["id"] in pool_ids:
                continue
            if domain in matched:
                _add(art, matched)
                break

    # Second pass: fill up to target_total from remaining pool
    for _, matched, art in pool:
        if len(selected) >= target_total:
            break
        if art["id"] not in pool_ids:
            _add(art, matched)

    # ── 4. Enrich: image + short_summary ──────────────────────────────────
    enriched = []
    for art in selected:
        enriched.append(_enrich_article(art))

    log.info(
        "fetch_articles_by_domains: raw=%d filtered=%d selected=%d enriched=%d",
        len(raw), len(pool), len(selected), len(enriched),
    )
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# Article enrichment
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_article(art: dict) -> dict:
    """
    Fetch the article page to extract:
    - image_url   (og:image > first <img> > RSS thumbnail)
    - short_summary (og:description or first 30-40 words up to full stop)
    - full_content  (longest text block from the page, for Gemini)
    """
    url = art.get("url", "")

    # Start with what RSS already gave us
    image_url     = art.get("image_url", "")
    short_summary = art.get("content", "")
    full_content  = art.get("content", "")

    if url:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Image: og:image → twitter:image → first article img
            if not image_url:
                for prop in ["og:image", "twitter:image"]:
                    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                    if tag and tag.get("content"):
                        image_url = tag["content"]
                        break
            if not image_url:
                img = soup.find("article", recursive=True)
                if img:
                    first_img = img.find("img", src=True)
                    if first_img:
                        src = first_img["src"]
                        image_url = src if src.startswith("http") else ""

            # Short summary: og:description first
            og_desc = soup.find("meta", property="og:description") or \
                      soup.find("meta", attrs={"name": "description"})
            if og_desc and og_desc.get("content"):
                short_summary = _trim_to_sentence(og_desc["content"], max_words=40)
            else:
                # Fall back to first paragraph text
                paras = soup.find_all("p")
                for p in paras:
                    txt = p.get_text(separator=" ", strip=True)
                    if len(txt.split()) >= 15:
                        short_summary = _trim_to_sentence(txt, max_words=40)
                        break

            # Full content: main article body
            article_tag = soup.find("article") or soup.find(
                "div", class_=re.compile(r"article|story|content|body|post", re.I)
            )
            if article_tag:
                paras = article_tag.find_all("p")
                full_content = " ".join(
                    p.get_text(separator=" ", strip=True)
                    for p in paras if len(p.get_text(strip=True)) > 30
                )[:8000]

        except Exception as e:
            log.debug("Enrich fetch failed for %s: %s", url, e)

    # Ensure short_summary is set (fallback to truncated content)
    if not short_summary and full_content:
        short_summary = _trim_to_sentence(full_content, max_words=40)

    art["image_url"]     = image_url
    art["short_summary"] = short_summary
    art["full_content"]  = full_content or art.get("content", "")
    art["article_url"]   = url
    return art


def _trim_to_sentence(text: str, max_words: int = 40) -> str:
    """
    Return at most `max_words` words, ending at the last full stop within range.
    If no full stop found, cut at max_words.
    """
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    chunk = " ".join(words[:max_words])
    # Find last sentence-ending punctuation
    last_stop = max(chunk.rfind("."), chunk.rfind("!"), chunk.rfind("?"))
    if last_stop > 20:
        return chunk[:last_stop + 1].strip()
    return chunk.strip() + "…"


# ─────────────────────────────────────────────────────────────────────────────
# Legacy fetch (used by /api/articles/fetch and pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_articles_from_source(
    topic: str = "",
    source: str = "rss",
    limit: int = 20,
    keywords: list[str] | None = None,
) -> list[dict]:
    kws = keywords or ([topic] if topic else [])
    kws_lower = [k.lower() for k in kws]
    all_articles: list[dict] = []
    for src in RSS_SOURCES:
        try:
            arts = _fetch_rss_raw(src["url"], src["name"], limit=limit * 2)
            all_articles.extend(arts)
        except Exception as e:
            log.warning("RSS fetch failed for %s: %s", src["name"], e)
    if not all_articles:
        return []
    if kws_lower:
        filtered = [
            a for a in all_articles
            if any(kw in (a["title"] + " " + a.get("content", "")).lower() for kw in kws_lower)
        ]
        if filtered:
            return _deduplicate(filtered)[:limit]
    return _deduplicate(all_articles)[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# URL-based fetch (From Source button)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_articles_from_url(url: str, limit: int = 20) -> list[dict]:
    host = urlparse(url.lower()).netloc.replace("www.", "")
    if "x.com" in host or "twitter.com" in host:
        return _fetch_x(url, limit)
    if "linkedin.com" in host:
        return _fetch_linkedin(url, limit)
    if "facebook.com" in host or "fb.com" in host:
        return _scrape_html(url, limit)
    rss = _try_rss(url, limit)
    if rss:
        return rss
    return _scrape_html(url, limit)


# ─────────────────────────────────────────────────────────────────────────────
# RSS low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_rss_raw(feed_url: str, source_name: str, limit: int = 20) -> list[dict]:
    feed = feedparser.parse(feed_url)
    if not feed.entries:
        raise RuntimeError(f"No entries in feed: {feed_url}")
    articles = []
    for entry in feed.entries[:limit]:
        link    = getattr(entry, "link", "")
        title   = getattr(entry, "title", "No title")
        content = _clean(
            getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        )
        pub     = getattr(entry, "published", "") or getattr(entry, "updated", "") or ""
        img_url = _extract_image(entry)
        articles.append({
            "id":           str(uuid.uuid5(uuid.NAMESPACE_URL, link or title)),
            "title":        title,
            "content":      content[:1200],
            "url":          link,
            "article_url":  link,
            "published_at": pub,
            "author":       getattr(entry, "author", ""),
            "source":       source_name,
            "image_url":    img_url,
            "short_summary": "",
            "full_content":  content,
            "matched_domains": [],
        })
    return articles


def _try_rss(url: str, limit: int) -> list[dict]:
    candidates = [
        url,
        url.rstrip("/") + "/feed",
        url.rstrip("/") + "/rss",
        url.rstrip("/") + "/feed.xml",
        url.rstrip("/") + "/rss.xml",
        url.rstrip("/") + "/atom.xml",
    ]
    host = urlparse(url).netloc.replace("www.", "")
    for candidate in candidates:
        try:
            arts = _fetch_rss_raw(candidate, host, limit)
            if arts:
                return arts
        except Exception:
            continue
    return []


def _extract_image(entry) -> str:
    media = getattr(entry, "media_thumbnail", None)
    if media and isinstance(media, list) and media:
        return media[0].get("url", "")
    enc = getattr(entry, "enclosures", [])
    for e in enc:
        if "image" in e.get("type", ""):
            return e.get("href", "")
    content = getattr(entry, "summary", "") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
    if m:
        return m.group(1)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Platform fetchers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_x(url: str, limit: int) -> list[dict]:
    handle = _extract_x_handle(url)
    if not handle:
        return _scrape_html(url, limit)
    for host in ["nitter.net", "nitter.privacydev.net", "nitter.poast.org"]:
        try:
            arts = _fetch_rss_raw(f"https://{host}/{handle}/rss", f"X/@{handle}", limit)
            if arts:
                return arts
        except Exception:
            pass
    return _platform_placeholder("X (Twitter)", url)


def _fetch_linkedin(url: str, limit: int) -> list[dict]:
    company_slug = _extract_linkedin_company(url)
    company_name = company_slug.replace("-", " ").title() if company_slug else "LinkedIn Company"
    if company_slug:
        rss = _try_rss(f"https://www.linkedin.com/company/{company_slug}/", limit)
        if rss:
            return rss
    arts = _scrape_linkedin_page(url, company_name, limit)
    if arts:
        return arts
    return _linkedin_placeholder(company_name, company_slug, url)


def _scrape_linkedin_page(url: str, company_name: str, limit: int) -> list[dict]:
    import json as _json
    arts: list[dict] = []
    for headers in (MOBILE_HEADERS, HEADERS):
        try:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            if any(x in resp.url for x in ["/login", "/authwall", "/checkpoint"]):
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data  = _json.loads(script.string or "{}")
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        name = item.get("name") or item.get("headline", "")
                        desc = item.get("description") or item.get("text", "")
                        if name and len(name) > 5:
                            item_url = item.get("url") or url
                            arts.append({
                                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, item_url)),
                                "title": name, "content": desc[:800],
                                "url": item_url, "article_url": item_url,
                                "published_at": item.get("datePublished", ""),
                                "author": company_name, "source": "LinkedIn",
                                "image_url": item.get("image", ""),
                                "short_summary": _trim_to_sentence(desc, 40),
                                "full_content": desc,
                                "matched_domains": [],
                            })
                except Exception:
                    pass
            if arts:
                return _deduplicate(arts)[:limit]
        except Exception:
            continue
    return []


def _scrape_html(url: str, limit: int) -> list[dict]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        host = urlparse(url).netloc.replace("www.", "")
        arts: list[dict] = []
        og_title = soup.find("meta", property="og:title")
        og_desc  = soup.find("meta", property="og:description")
        og_img   = soup.find("meta", property="og:image")
        if og_title and og_title.get("content"):
            desc = (og_desc or {}).get("content", "")
            arts.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
                "title": og_title["content"], "content": desc,
                "url": url, "article_url": url, "published_at": "",
                "author": "", "source": host,
                "image_url": (og_img or {}).get("content", ""),
                "short_summary": _trim_to_sentence(desc, 40),
                "full_content": desc, "matched_domains": [],
            })
        for tag in soup.find_all(
            ["article", "div"],
            class_=re.compile(r"post|article|entry|story|card|news|item", re.I),
        )[:limit * 2]:
            headline = tag.find(["h1", "h2", "h3"])
            anchor   = tag.find("a", href=True)
            para     = tag.find("p")
            if not headline:
                continue
            link = anchor["href"] if anchor else url
            if link.startswith("/"):
                link = f"{urlparse(url).scheme}://{urlparse(url).netloc}{link}"
            body = para.get_text(strip=True)[:500] if para else ""
            arts.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, link)),
                "title": headline.get_text(strip=True), "content": body,
                "url": link, "article_url": link, "published_at": "",
                "author": "", "source": host, "image_url": "",
                "short_summary": _trim_to_sentence(body, 40),
                "full_content": body, "matched_domains": [],
            })
        return _deduplicate(arts)[:limit]
    except Exception as e:
        log.error("scrape_html error: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _clean(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _deduplicate(articles: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for a in articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)
    return unique


def _extract_x_handle(url: str) -> str:
    m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", url)
    return m.group(1) if m else ""


def _extract_linkedin_company(url: str) -> str:
    m = re.search(r"linkedin\.com/company/([^/?#]+)", url)
    return m.group(1) if m else ""


def _linkedin_placeholder(company_name: str, company_slug: str, url: str) -> list[dict]:
    note = (
        f"{company_name} is a LinkedIn company page. "
        "LinkedIn requires authentication for most content. "
        "Use their official API with OAuth, or provide a direct RSS feed URL instead."
    )
    return [{
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
        "title": f"{company_name} — LinkedIn", "content": note,
        "url": url, "article_url": url, "published_at": "",
        "author": "", "source": "LinkedIn", "image_url": "",
        "short_summary": note[:120], "full_content": note, "matched_domains": [],
    }]


def _platform_placeholder(platform: str, url: str) -> list[dict]:
    note = (
        f"{platform} restricts automated access. "
        "Use the official API or provide a direct RSS feed URL instead."
    )
    return [{
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
        "title": f"{platform} — {url}", "content": note,
        "url": url, "article_url": url, "published_at": "",
        "author": "", "source": platform, "image_url": "",
        "short_summary": note[:120], "full_content": note, "matched_domains": [],
    }]


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_rss_sources() -> list[dict]:
    return [{"name": s["name"], "url": s["url"]} for s in RSS_SOURCES]


def get_domain_keywords() -> dict[str, list[str]]:
    return DOMAIN_KEYWORDS