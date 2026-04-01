"""
Gemini client v4.1
- Model  : gemini-2.5-flash  (v1beta — required for responseMimeType)
- Forces : responseMimeType="application/json"  so Gemini MUST return valid JSON
- Prompt : SVG asked last, uses single quotes in attributes to avoid JSON breakage
- Parser : 4-layer recovery so frontend always gets a usable dict
"""

import os
import json
import re
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# v1beta is required for responseMimeType
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT
# ─────────────────────────────────────────────────────────────────────────────

INSIGHT_PROMPT = """You are STEAMI, a science and technology explainer.

Read the article and return ONLY a JSON object. No markdown. No text before or after.

Article Title: {title}
Domain: {domain}
Content: {content}

Return this exact JSON structure (fill in all values):

{{
  "summary": "Write 150 to 200 words of flowing prose here. Explain what happened, why it matters, and what comes next. Plain English, no bullet points.",
  "key_points": [
    "One sentence about the main finding, max 15 words.",
    "One sentence about why it matters, max 15 words.",
    "One sentence about what comes next, max 15 words."
  ],
  "sentiment": "positive",
  "confidence": 0.85,
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "domain": "{domain}",
  "reading_time_min": 4,
  "article_url": "{article_url}",
  "svg": "WRITE_FULL_SVG_HERE"
}}

For the svg field — replace WRITE_FULL_SVG_HERE with a complete SVG string:
- Use width='400' height='400' (single quotes only inside SVG — no double quotes)
- Dark rect background fill '#0f0f1a'
- Colors: #6366f1 #818cf8 #a5b4fc #f59e0b #34d399 #f87171
- Draw a diagram explaining the article concept:
    AI / machine learning  -> neural network nodes and weighted edges
    Space / astronomy      -> planets, orbits, or star diagram
    Biology / Medicine     -> cell structure or DNA double helix
    Robotics               -> robot arm with joints labeled
    Finance                -> bar chart or trend line with axis labels
    Physics                -> wave diagram or particle collision
    Chemistry              -> molecule with atoms and bonds
    Engineering            -> circuit schematic or gear system
    Mathematics            -> graph with curve or geometric proof
    Computer Science       -> flowchart boxes with arrows
- Add domain label text near top in color #818cf8 font-size='11'
- Add STEAMI text at bottom right in color #6366f1 font-size='10'
- Keep SVG under 2500 characters
- CRITICAL: Use ONLY single quotes inside the SVG string. Zero double quotes inside the SVG.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_ai_insight(article: dict) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    model   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    title       = article.get("title", "Untitled")
    article_url = article.get("article_url") or article.get("url", "")
    domain      = (
        (article.get("matched_domains") or [article.get("topic", "Technology")])[0]
    )
    content = (
        article.get("full_content")
        or article.get("content")
        or article.get("description")
        or article.get("text", "")
    )[:6000]

    if not content:
        raise ValueError(f"Article '{title}' has no content to analyse")

    if not api_key:
        log.warning("GEMINI_API_KEY not set — returning mock insight")
        return _mock_insight(title, article_url, domain)

    prompt = INSIGHT_PROMPT.format(
        title=title,
        domain=domain,
        content=content,
        article_url=article_url,
    )

    url  = GEMINI_URL.format(model=model, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":      0.3,
            "maxOutputTokens":  4000,
            "responseMimeType": "application/json",   # forces valid JSON output
        },
    }

    log.info("Gemini request: model=%s  title=%.55s  domain=%s", model, title, domain)

    try:
        resp = requests.post(url, json=body, timeout=90)
    except requests.Timeout:
        raise RuntimeError("Gemini request timed out after 90s")

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:400]}")

    raw_text = (
        resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
    )

    log.debug("Gemini raw_text (%d chars): %.200s", len(raw_text), raw_text)

    result = _parse(raw_text, title, article_url, domain)

    # Guarantee correct article_url regardless of what Gemini returned
    result["article_url"] = article_url
    result.setdefault("domain", domain)
    result.setdefault("reading_time_min", _reading_time(content))

    # Fallback SVG if missing or too short
    if not result.get("svg") or len(str(result.get("svg", ""))) < 60:
        result["svg"] = _fallback_svg(title, domain)

    log.info(
        "Insight ready — summary=%d words  svg=%d chars  domain=%s  cached=False",
        len(result.get("summary", "").split()),
        len(str(result.get("svg", ""))),
        result.get("domain", "?"),
    )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 4-layer parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse(raw: str, title: str, article_url: str, domain: str) -> dict:
    """Try 4 strategies in order. Always returns a complete dict."""

    if not raw or not raw.strip():
        log.warning("Gemini returned empty string")
        return _mock_insight(title, article_url, domain)

    # ── Layer 1: strip fences then direct parse ────────────────────────────
    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"```", "", cleaned).strip()

    try:
        obj = json.loads(cleaned)
        log.info("Parse L1 OK (direct json.loads)")
        return _fill_defaults(obj, title, article_url, domain)
    except json.JSONDecodeError:
        pass

    # ── Layer 2: find outermost {...} block ────────────────────────────────
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            obj = json.loads(m.group())
            log.info("Parse L2 OK (outermost brace match)")
            return _fill_defaults(obj, title, article_url, domain)
        except json.JSONDecodeError:
            pass

    # ── Layer 3: isolate SVG, parse the rest, re-attach ───────────────────
    try:
        # SVG often contains chars that break JSON — pull it out first
        svg_pat = re.search(
            r'"svg"\s*:\s*"(<svg[\s\S]*?</svg>)"', cleaned, re.IGNORECASE
        )
        if not svg_pat:
            # Try single-quote SVG value
            svg_pat = re.search(
                r'"svg"\s*:\s*\'(<svg[\s\S]*?</svg>)\'', cleaned, re.IGNORECASE
            )

        if svg_pat:
            svg_content = svg_pat.group(1)
            # Replace the whole svg field with a safe placeholder
            safe = cleaned[:svg_pat.start()] + '"svg":"__SVG__"' + cleaned[svg_pat.end():]
            # Remove trailing commas before } or ]
            safe = re.sub(r",\s*([}\]])", r"\1", safe)
            obj  = json.loads(safe)
            obj["svg"] = svg_content
            log.info("Parse L3 OK (svg isolated)")
            return _fill_defaults(obj, title, article_url, domain)
    except Exception:
        pass

    # ── Layer 4: regex field extraction ───────────────────────────────────
    log.warning("All JSON parse layers failed — using field extraction")
    return _extract(cleaned, title, article_url, domain)


def _fill_defaults(obj: dict, title: str, article_url: str, domain: str) -> dict:
    obj.setdefault("summary",          f"Analysis of: {title}")
    obj.setdefault("key_points",       [])
    obj.setdefault("sentiment",        "neutral")
    obj.setdefault("confidence",       0.75)
    obj.setdefault("tags",             [])
    obj.setdefault("domain",           domain)
    obj.setdefault("reading_time_min", 3)
    obj.setdefault("article_url",      article_url)
    obj.setdefault("svg",              "")

    # Type coercions
    if not isinstance(obj["key_points"], list):
        obj["key_points"] = []
    if not isinstance(obj["tags"], list):
        obj["tags"] = []
    try:
        obj["confidence"] = float(obj["confidence"])
    except (TypeError, ValueError):
        obj["confidence"] = 0.75
    try:
        obj["reading_time_min"] = int(obj["reading_time_min"])
    except (TypeError, ValueError):
        obj["reading_time_min"] = 3

    # If summary accidentally contains the whole JSON blob
    s = obj.get("summary", "")
    if isinstance(s, str) and (s.strip().startswith("{") or len(s) > 2000):
        obj["summary"] = f"Analysis of: {title}"

    return obj


def _extract(text: str, title: str, article_url: str, domain: str) -> dict:
    """Regex field extraction — last resort."""

    def get_str(key: str) -> str:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        return m.group(1) if m else ""

    def get_num(key: str, default: float) -> float:
        m = re.search(rf'"{key}"\s*:\s*([\d.]+)', text)
        return float(m.group(1)) if m else default

    def get_arr(key: str) -> list[str]:
        m = re.search(rf'"{key}"\s*:\s*\[(.*?)\]', text, re.DOTALL)
        if not m:
            return []
        return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))

    def get_svg() -> str:
        # Try to extract SVG from between "svg": " and the closing </svg>"
        m = re.search(r'"svg"\s*:\s*"(<svg[\s\S]*?</svg>)"', text, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r'(<svg[\s\S]*?</svg>)', text, re.IGNORECASE)
        if m:
            return m.group(1)
        return ""

    summary    = get_str("summary")
    svg        = get_svg() or get_str("svg")
    sentiment  = get_str("sentiment") or "neutral"
    art_url    = get_str("article_url") or article_url
    dom        = get_str("domain") or domain
    key_points = get_arr("key_points")
    tags       = get_arr("tags")

    # Sanity check summary
    if not summary or summary.strip().startswith("{") or len(summary) > 2000:
        summary = f"Analysis of: {title}"

    return {
        "summary":          summary,
        "svg":              svg or _fallback_svg(title, domain),
        "key_points":       key_points or [
            "Key finding identified in this article.",
            "This development has significant implications.",
            "Further monitoring and research are expected.",
        ],
        "sentiment":        sentiment if sentiment in ("positive", "neutral", "negative") else "neutral",
        "confidence":       get_num("confidence", 0.6),
        "tags":             tags,
        "domain":           dom,
        "reading_time_min": int(get_num("reading_time_min", 3)),
        "article_url":      art_url,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fallback SVG — single quotes only, safe to embed in JSON
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_svg(title: str, domain: str = "Technology") -> str:
    safe    = (
        title[:40]
        .replace("'", "")
        .replace("<", "")
        .replace(">", "")
        .replace("&", "and")
        .replace('"', "")
    )
    ellipsis = "..." if len(title) > 40 else ""
    dom_lbl  = domain.upper()[:24]

    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='400'>"
        "<defs>"
        "<linearGradient id='bg' x1='0' y1='0' x2='0' y2='1'>"
        "<stop offset='0%' stop-color='#0f0f1a'/>"
        "<stop offset='100%' stop-color='#1a1a3e'/>"
        "</linearGradient>"
        "<radialGradient id='glow' cx='50%' cy='45%' r='40%'>"
        "<stop offset='0%' stop-color='#6366f1' stop-opacity='0.5'/>"
        "<stop offset='100%' stop-color='#0f0f1a' stop-opacity='0'/>"
        "</radialGradient>"
        "</defs>"
        "<rect width='400' height='400' fill='url(#bg)' rx='12'/>"
        "<rect width='400' height='400' fill='url(#glow)'/>"
        f"<text x='200' y='36' text-anchor='middle' fill='#818cf8' "
        f"font-size='11' font-family='system-ui,sans-serif' letter-spacing='2' font-weight='600'>{dom_lbl}</text>"
        "<circle cx='200' cy='180' r='72' fill='none' stroke='#6366f1' stroke-width='1.5' opacity='0.4'/>"
        "<circle cx='200' cy='180' r='50' fill='#6366f1' opacity='0.12'/>"
        "<circle cx='200' cy='180' r='30' fill='#6366f1' opacity='0.65'/>"
        "<circle cx='200' cy='180' r='14' fill='#a5b4fc'/>"
        "<circle cx='272' cy='180' r='7' fill='#f59e0b'/>"
        "<circle cx='128' cy='180' r='7' fill='#34d399'/>"
        "<circle cx='200' cy='108' r='7' fill='#f87171'/>"
        "<circle cx='200' cy='252' r='7' fill='#818cf8'/>"
        "<line x1='200' y1='166' x2='272' y2='180' stroke='#6366f1' stroke-width='1' opacity='0.5'/>"
        "<line x1='200' y1='166' x2='128' y2='180' stroke='#6366f1' stroke-width='1' opacity='0.5'/>"
        "<line x1='200' y1='166' x2='200' y2='108' stroke='#6366f1' stroke-width='1' opacity='0.5'/>"
        "<line x1='200' y1='194' x2='200' y2='252' stroke='#6366f1' stroke-width='1' opacity='0.5'/>"
        f"<text x='200' y='308' text-anchor='middle' fill='#e2e8f0' "
        f"font-size='12' font-family='system-ui,sans-serif' font-weight='600'>{safe}{ellipsis}</text>"
        "<rect x='0' y='362' width='400' height='38' fill='#0a0a14'/>"
        "<text x='14' y='386' fill='#475569' font-size='10' font-family='system-ui,sans-serif'>STEAMI AI Insight</text>"
        "<text x='386' y='386' text-anchor='end' fill='#6366f1' font-size='10' "
        "font-family='system-ui,sans-serif' font-weight='700'>STEAMI</text>"
        "</svg>"
    )


def _reading_time(content: str) -> int:
    return max(1, min(20, round(len(content.split()) / 200)))


# ─────────────────────────────────────────────────────────────────────────────
# Mock (no API key)
# ─────────────────────────────────────────────────────────────────────────────

def _mock_insight(title: str, article_url: str = "", domain: str = "Technology") -> dict:
    return {
        "summary": (
            f"This article titled '{title[:55]}' explores a significant development in {domain}. "
            "The findings highlight key trends and their potential impact on both industry and "
            "broader society. Researchers and practitioners are watching closely as new data "
            "emerges from ongoing studies. Early results suggest this could reshape how we "
            "approach the core problem entirely. Several competing teams have announced parallel "
            "efforts, signalling strong community interest. Policy implications are still being "
            "debated, but initial expert reactions have been cautiously optimistic. The timeline "
            "for real-world deployment remains uncertain, though early prototypes have shown "
            "promising results in controlled settings. Stakeholders across academia, industry, "
            "and government are expected to respond with new investments and updated guidelines. "
            "Set GEMINI_API_KEY in your .env to get real AI-generated insights."
        ),
        "svg":              _fallback_svg(title, domain),
        "key_points":       [
            "A major development has been identified in this domain.",
            "Multiple stakeholders across research and industry are affected.",
            "Policy implications and long-term impact are still being assessed.",
        ],
        "sentiment":        "neutral",
        "confidence":       0.5,
        "tags":             ["steami", "demo", domain.lower().replace("/", "-")],
        "domain":           domain,
        "reading_time_min": 3,
        "article_url":      article_url,
    }