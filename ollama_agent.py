"""
ollama_agent.py  —  STEAMI AI Insight Generator using Ollama Cloud API
=======================================================================
Replaces gemini_client.py. Drop-in replacement — same public interface:
  generate_ai_insight(article: dict) -> dict

OLLAMA CLOUD SETUP:
  1. Go to https://ollama.com/settings/keys
  2. Create an API key
  3. Set in .env:
       OLLAMA_API_KEY=your_key_here
       OLLAMA_MODEL=gemma4:31b-cloud   (default — runs in Ollama Cloud, no local GPU needed)

OLLAMA CLOUD API:
  Endpoint: POST https://ollama.com/api/chat
  Auth:     Authorization: Bearer <OLLAMA_API_KEY>
  Docs:     https://docs.ollama.com/cloud

MODEL CHOICE:
  --- Via Ollama Cloud API (https://ollama.com/api/chat) — no local install needed ---
  gemma4:31b-cloud  — 256K context, best quality via cloud  ← DEFAULT
  gemma4:26b        — MoE 256K context, great quality/speed (if locally pulled)
  --- Via local Ollama (set OLLAMA_HOST=http://localhost:11434) ---
  gemma4:e4b        — 9.6 GB, 128K context, edge/local only
  gemma4:e2b        — 7.2 GB, 128K context, fastest local option
  gemma4:31b        — 20 GB, 256K context, maximum local quality

  NOTE: gemma4:e4b and gemma4:e2b are EDGE/LOCAL models — they are NOT available
  via the Ollama Cloud API. If you see a 404 "model not found" error, you are
  trying to run a local-only model tag through the cloud endpoint. Switch to
  gemma4:31b-cloud for the cloud API, or set OLLAMA_HOST to your local instance.

GEMMA 4 FEATURES USED:
  - Native system prompt support (system role)
  - Configurable thinking mode via <|think|> token (disabled here for speed)
  - format: "json" for structured JSON output
  - temperature=1.0, top_p=0.95, top_k=64 (recommended by Google)
  - stream=false for synchronous single response

KEY DIFFERENCE FROM GEMINI:
  - Gemini used responseMimeType="application/json" (Google-specific)
  - Ollama uses format="json" (standard across all Ollama models)
  - Response is in data["message"]["content"] (not data["candidates"][0]...)
  - The 4-layer JSON parser is kept identical — same robustness

OUTPUT SCHEMA (returned dict):
  summary          — 150–200 word prose insight
  key_points       — list of 3 one-sentence highlights
  sentiment        — "positive" | "neutral" | "negative"  (raw polarity)
  sentiment_label  — "good_news" | "bad_news" | "neutral_news"  (frontend-friendly)
  emoji            — single emoji character matching the article's tone + domain
  confidence       — float 0.0–1.0
  tags             — list of topic tags
  domain           — article domain / topic category
  reading_time_min — estimated reading time in minutes
  article_url      — source URL
"""

import os
import json
import re
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CLOUD CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Direct Ollama Cloud API endpoint — no local Ollama installation needed.
# Authentication via: Authorization: Bearer <OLLAMA_API_KEY>
# IMPORTANT: Only models with a "-cloud" suffix (e.g. gemma4:31b-cloud) are
# available through this remote endpoint. Local-only tags like gemma4:e4b will
# return a 404. To use local models, set OLLAMA_HOST=http://localhost:11434.
OLLAMA_CLOUD_URL = "https://ollama.com/api/chat"

# Fallback to local Ollama if OLLAMA_HOST is set (e.g. "http://localhost:11434")
# Leave empty to always use the cloud endpoint.
OLLAMA_LOCAL_HOST = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")

# The API endpoint we actually call (local overrides cloud if OLLAMA_HOST is set)
def _api_url() -> str:
    if OLLAMA_LOCAL_HOST:
        return f"{OLLAMA_LOCAL_HOST}/api/chat"
    return OLLAMA_CLOUD_URL


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT  (Gemma 4 supports native system role)
# ─────────────────────────────────────────────────────────────────────────────

# System prompt sets up the AI as STEAMI's insight engine.
# NOTE: We do NOT include <|think|> here because thinking mode produces extra
# tokens that slow down the response and the output parser must strip.
# If you want thinking enabled, add "<|think|>" at the very start of this string.
SYSTEM_PROMPT = """You are STEAMI, a science and technology explainer AI.
Your job is to read a news or research article and return a structured JSON insight.
You must respond with ONLY valid JSON — no markdown, no explanation, no text before or after.
The JSON must follow the exact schema provided in the user message.
Use clear, engaging language appropriate for curious students and researchers.
Do NOT include any thinking tokens or chain-of-thought in your response."""


# ─────────────────────────────────────────────────────────────────────────────
# USER PROMPT TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────

INSIGHT_PROMPT = """Read this article and return ONLY a JSON object. No markdown. No extra text.

Article Title: {title}
Domain: {domain}
Content: {content}

Return this exact JSON structure (fill in ALL values):

{{
  "summary": "Write 150 to 200 words of flowing prose. Explain what happened, why it matters, and what comes next. Plain English, no bullet points.",
  "key_points": [
    "One sentence about the main finding, max 15 words.",
    "One sentence about why it matters, max 15 words.",
    "One sentence about what comes next, max 15 words."
  ],
  "sentiment": "positive",
  "sentiment_label": "good_news",
  "emoji": "🚀",
  "confidence": 0.85,
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "domain": "{domain}",
  "reading_time_min": 4,
  "article_url": "{article_url}"
}}

Rules for each field:

sentiment — pick exactly one of: "positive", "neutral", "negative"
  positive = broadly beneficial, breakthrough, hopeful, promising progress
  neutral  = factual update, mixed implications, no clear valence
  negative = setback, risk, harm, controversy, failure, threat

sentiment_label — pick exactly one of: "good_news", "neutral_news", "bad_news"
  Maps directly to sentiment: positive → good_news, neutral → neutral_news, negative → bad_news

emoji — a single emoji character that best captures the article's tone AND domain together.
  Guidelines by domain (adjust for actual sentiment):
    AI / Machine Learning  → 🤖 🧠 ⚡ (positive) / 🤖 (neutral) / ⚠️ (negative)
    Space / Astronomy      → 🚀 🌌 🛸 (positive) / 🔭 (neutral) / ☄️ (negative)
    Biology / Medicine     → 💊 🧬 🩺 (positive) / 🔬 (neutral) / 🦠 (negative)
    Climate / Environment  → 🌱 ☀️ 🌊 (positive) / 🌍 (neutral) / 🔥 🌪️ (negative)
    Physics / Engineering  → ⚛️ 🔋 🏗️ (positive) / ⚙️ (neutral) / 💥 (negative)
    Computer Science       → 💻 🔐 📡 (positive) / 🖥️ (neutral) / 🐛 (negative)
    Robotics               → 🦾 🤖 🔩 (positive) / ⚙️ (neutral) / 🚨 (negative)
    Mathematics            → 📐 ♾️ 🎯 (positive) / 📊 (neutral) / ❌ (negative)
    Economics / Finance    → 📈 💰 🏦 (positive) / 💹 (neutral) / 📉 (negative)
    General Technology     → ✨ 💡 🔬 (positive) / 🔧 (neutral) / ⚠️ (negative)
  Pick the single most fitting emoji — one character only, no combinations."""


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC INTERFACE  (identical signature to gemini_client.generate_ai_insight)
# ─────────────────────────────────────────────────────────────────────────────

def generate_ai_insight(article: dict) -> dict:
    """
    Generate an AI insight for a news/research article using Ollama Cloud (gemma4).

    Args:
        article: dict with keys: title, content/full_content/description,
                 article_url/url, matched_domains/topic

    Returns:
        dict with keys: summary, key_points, sentiment, sentiment_label, emoji,
                        confidence, tags, domain, reading_time_min, article_url

    Raises:
        RuntimeError: if the API call fails or times out
        ValueError:   if the article has no content
    """
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    model   = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

    # ── Extract article fields ─────────────────────────────────────────────
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
    )

    # Handle list-type content (research articles store content as list of paragraphs)
    if isinstance(content, list):
        content = " ".join(str(p) for p in content)
    content = str(content)[:6000]   # cap at 6000 chars to keep prompt within context

    if not content.strip():
        raise ValueError(f"Article '{title}' has no content to analyse")

    # ── No API key — return mock insight ──────────────────────────────────
    if not api_key:
        log.warning("OLLAMA_API_KEY not set — returning mock insight for '%s'", title)
        return _mock_insight(title, article_url, domain)

    # ── Build prompt ───────────────────────────────────────────────────────
    user_prompt = INSIGHT_PROMPT.format(
        title       = title,
        domain      = domain,
        content     = content,
        article_url = article_url,
    )

    # ── Build request body ─────────────────────────────────────────────────
    # Gemma 4 supports native system role — this is cleaner than embedding
    # the system context inside the user message.
    body = {
        "model":    model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "stream": False,       # get complete response in one shot (not streaming)
        "format": "json",      # tell Ollama to enforce valid JSON output
        "options": {
            "temperature": 1.0,   # Google's recommended value for Gemma 4
            "top_p":       0.95,  # Google's recommended value
            "top_k":       64,    # Google's recommended value
            "num_predict": 4000,  # max tokens in response
        },
    }

    # ── Set auth header ────────────────────────────────────────────────────
    # Cloud endpoint requires:  Authorization: Bearer <key>
    # Local endpoint (OLLAMA_HOST set) doesn't require auth but we send it anyway
    # — local Ollama simply ignores unknown headers.
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    url = _api_url()
    log.info("Ollama request: url=%s model=%s title=%.55s domain=%s",
             url, model, title, domain)

    # ── Call the API ───────────────────────────────────────────────────────
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=120)
    except requests.Timeout:
        raise RuntimeError("Ollama API request timed out after 120s")
    except requests.ConnectionError as e:
        raise RuntimeError(f"Ollama API connection failed: {e}")

    if resp.status_code == 401:
        raise RuntimeError(
            "Ollama API returned 401 Unauthorized. "
            "Check your OLLAMA_API_KEY at https://ollama.com/settings/keys"
        )
    if resp.status_code == 404:
        using_cloud_api = not bool(OLLAMA_LOCAL_HOST)
        if using_cloud_api:
            raise RuntimeError(
                f"Ollama Cloud API: model '{model}' not found. "
                "Only '-cloud' tagged models work via the remote API. "
                "Use: gemma4:31b-cloud  —  or set OLLAMA_HOST=http://localhost:11434 "
                "and pull a local model (gemma4:e4b, gemma4:e2b, gemma4:26b, gemma4:31b)."
            )
        else:
            raise RuntimeError(
                f"Ollama local model '{model}' not found. "
                "Pull it first: ollama pull gemma4:e4b  (or e2b / 26b / 31b)"
            )
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama API HTTP {resp.status_code}: {resp.text[:400]}")

    # ── Extract response content ───────────────────────────────────────────
    # Ollama chat API response structure:
    # { "message": { "role": "assistant", "content": "<json string>" }, "done": true, ... }
    try:
        resp_json = resp.json()
    except Exception as e:
        raise RuntimeError(f"Ollama returned non-JSON response: {e}")

    # Handle both streaming (accumulated) and non-streaming responses
    message = resp_json.get("message", {})
    if not message:
        # Some Ollama versions return a different shape for non-stream responses
        # Try the response field directly
        message = resp_json.get("response", "")
        if isinstance(message, str):
            raw_text = message
        else:
            raise RuntimeError(f"Unexpected Ollama response shape: {list(resp_json.keys())}")
    else:
        raw_text = message.get("content", "")

    # Strip any <think>...</think> blocks that Gemma 4 may produce in thinking mode
    # even when thinking is not explicitly enabled (some model variants still emit it)
    raw_text = re.sub(r"<\|channel>thought\n.*?<channel\|>", "", raw_text, flags=re.DOTALL)
    raw_text = re.sub(r"<think>.*?</think>",                 "", raw_text, flags=re.DOTALL)
    raw_text = raw_text.strip()

    log.debug("Ollama raw_text (%d chars): %.200s", len(raw_text), raw_text)

    # ── Parse the response ─────────────────────────────────────────────────
    result = _parse(raw_text, title, article_url, domain)

    # Guarantee correct metadata regardless of what the model returned
    result["article_url"] = article_url
    result.setdefault("domain", domain)
    result.setdefault("reading_time_min", _reading_time(content))

    # Normalise sentiment_label to ensure it always matches sentiment
    result["sentiment_label"] = _sentiment_to_label(result.get("sentiment", "neutral"))

    # Fallback emoji if model didn't return one or returned garbage
    if not result.get("emoji") or len(str(result.get("emoji", ""))) > 8:
        result["emoji"] = _fallback_emoji(result.get("sentiment", "neutral"), domain)

    log.info(
        "Insight ready — model=%s summary=%d words sentiment=%s emoji=%s domain=%s",
        model,
        len(result.get("summary", "").split()),
        result.get("sentiment", "?"),
        result.get("emoji", "?"),
        result.get("domain", "?"),
    )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 4-LAYER JSON PARSER  (identical to gemini_client — handles model quirks)
# ─────────────────────────────────────────────────────────────────────────────

def _parse(raw: str, title: str, article_url: str, domain: str) -> dict:
    """
    Try 4 strategies in sequence to extract a valid JSON dict.
    Always returns a complete dict even if parsing completely fails.
    """
    if not raw or not raw.strip():
        log.warning("Ollama returned empty string")
        return _mock_insight(title, article_url, domain)

    # ── Layer 1: Strip markdown fences, then direct parse ─────────────────
    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"```",             "", cleaned).strip()

    try:
        obj = json.loads(cleaned)
        log.info("Parse L1 OK (direct json.loads)")
        return _fill_defaults(obj, title, article_url, domain)
    except json.JSONDecodeError:
        pass

    # ── Layer 2: Find outermost { ... } block ─────────────────────────────
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            obj = json.loads(m.group())
            log.info("Parse L2 OK (outermost brace match)")
            return _fill_defaults(obj, title, article_url, domain)
        except json.JSONDecodeError:
            pass

    # ── Layer 3: Regex field extraction — last resort ─────────────────────
    log.warning("All JSON parse layers failed — using field extraction")
    return _extract(cleaned, title, article_url, domain)


def _fill_defaults(obj: dict, title: str, article_url: str, domain: str) -> dict:
    """Ensure every required field is present with a sensible default."""
    obj.setdefault("summary",          f"Analysis of: {title}")
    obj.setdefault("key_points",       [])
    obj.setdefault("sentiment",        "neutral")
    obj.setdefault("confidence",       0.75)
    obj.setdefault("tags",             [])
    obj.setdefault("domain",           domain)
    obj.setdefault("reading_time_min", 3)
    obj.setdefault("article_url",      article_url)

    # sentiment_label — always derived from sentiment so they stay in sync
    obj["sentiment_label"] = _sentiment_to_label(obj["sentiment"])

    # emoji — use model-provided value if valid (single char / short), else fallback
    raw_emoji = obj.get("emoji", "")
    if not raw_emoji or len(str(raw_emoji)) > 8:
        obj["emoji"] = _fallback_emoji(obj["sentiment"], domain)
    else:
        obj["emoji"] = raw_emoji

    # Type coercions — model might return numbers as strings
    if not isinstance(obj["key_points"], list):  obj["key_points"] = []
    if not isinstance(obj["tags"],       list):  obj["tags"]       = []
    try:    obj["confidence"]       = float(obj["confidence"])
    except: obj["confidence"]       = 0.75
    try:    obj["reading_time_min"] = int(obj["reading_time_min"])
    except: obj["reading_time_min"] = 3

    # Guard against summary being the full JSON blob
    s = obj.get("summary", "")
    if isinstance(s, str) and (s.strip().startswith("{") or len(s) > 2000):
        obj["summary"] = f"Analysis of: {title}"

    return obj


def _extract(text: str, title: str, article_url: str, domain: str) -> dict:
    """Regex field extraction — absolute last resort when all JSON parsing fails."""

    def get_str(key: str) -> str:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        return m.group(1) if m else ""

    def get_num(key: str, default: float) -> float:
        m = re.search(rf'"{key}"\s*:\s*([\d.]+)', text)
        return float(m.group(1)) if m else default

    def get_arr(key: str) -> list:
        m = re.search(rf'"{key}"\s*:\s*\[(.*?)\]', text, re.DOTALL)
        if not m: return []
        return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))

    summary         = get_str("summary")
    sentiment       = get_str("sentiment") or "neutral"
    raw_emoji       = get_str("emoji")
    art_url         = get_str("article_url") or article_url
    dom             = get_str("domain") or domain
    key_points      = get_arr("key_points")
    tags            = get_arr("tags")

    if not summary or summary.strip().startswith("{") or len(summary) > 2000:
        summary = f"Analysis of: {title}"

    sentiment = sentiment if sentiment in ("positive", "neutral", "negative") else "neutral"
    emoji     = raw_emoji if raw_emoji and len(raw_emoji) <= 8 else _fallback_emoji(sentiment, dom)

    return {
        "summary":          summary,
        "key_points":       key_points or [
            "Key finding identified in this article.",
            "This development has significant implications.",
            "Further monitoring and research are expected.",
        ],
        "sentiment":        sentiment,
        "sentiment_label":  _sentiment_to_label(sentiment),
        "emoji":            emoji,
        "confidence":       get_num("confidence", 0.6),
        "tags":             tags,
        "domain":           dom,
        "reading_time_min": int(get_num("reading_time_min", 3)),
        "article_url":      art_url,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SENTIMENT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _sentiment_to_label(sentiment: str) -> str:
    """Convert raw sentiment polarity to a frontend-friendly label."""
    return {
        "positive": "good_news",
        "negative": "bad_news",
        "neutral":  "neutral_news",
    }.get(sentiment, "neutral_news")


# Default emoji per (sentiment, domain-keyword) — used when model omits the field
_EMOJI_MAP: dict[tuple[str, str], str] = {
    # ── positive ──────────────────────────────────────────────────────────
    ("positive", "ai"):          "🤖",
    ("positive", "machine"):     "🧠",
    ("positive", "space"):       "🚀",
    ("positive", "astro"):       "🌌",
    ("positive", "bio"):         "🧬",
    ("positive", "medicine"):    "💊",
    ("positive", "health"):      "🩺",
    ("positive", "climate"):     "🌱",
    ("positive", "energy"):      "⚡",
    ("positive", "physics"):     "⚛️",
    ("positive", "engineer"):    "🔋",
    ("positive", "robotics"):    "🦾",
    ("positive", "computer"):    "💻",
    ("positive", "cyber"):       "🔐",
    ("positive", "math"):        "📐",
    ("positive", "finance"):     "📈",
    ("positive", "econom"):      "💰",
    # ── neutral ───────────────────────────────────────────────────────────
    ("neutral",  "ai"):          "🤖",
    ("neutral",  "space"):       "🔭",
    ("neutral",  "bio"):         "🔬",
    ("neutral",  "medicine"):    "🔬",
    ("neutral",  "health"):      "🏥",
    ("neutral",  "climate"):     "🌍",
    ("neutral",  "energy"):      "⚙️",
    ("neutral",  "physics"):     "⚙️",
    ("neutral",  "engineer"):    "🔧",
    ("neutral",  "robotics"):    "⚙️",
    ("neutral",  "computer"):    "🖥️",
    ("neutral",  "math"):        "📊",
    ("neutral",  "finance"):     "💹",
    ("neutral",  "econom"):      "📊",
    # ── negative ──────────────────────────────────────────────────────────
    ("negative", "ai"):          "⚠️",
    ("negative", "space"):       "☄️",
    ("negative", "bio"):         "🦠",
    ("negative", "medicine"):    "🦠",
    ("negative", "health"):      "🚨",
    ("negative", "climate"):     "🔥",
    ("negative", "energy"):      "💥",
    ("negative", "physics"):     "💥",
    ("negative", "engineer"):    "🚨",
    ("negative", "robotics"):    "🚨",
    ("negative", "computer"):    "🐛",
    ("negative", "cyber"):       "🔓",
    ("negative", "math"):        "❌",
    ("negative", "finance"):     "📉",
    ("negative", "econom"):      "📉",
}

# Generic fallbacks per sentiment when no domain keyword matches
_SENTIMENT_DEFAULT_EMOJI: dict[str, str] = {
    "positive": "✨",
    "neutral":  "🔬",
    "negative": "⚠️",
}


def _fallback_emoji(sentiment: str, domain: str) -> str:
    """Return the best-matching emoji for a given sentiment + domain string."""
    domain_lc = domain.lower()
    for (sent, kw), emoji in _EMOJI_MAP.items():
        if sent == sentiment and kw in domain_lc:
            return emoji
    return _SENTIMENT_DEFAULT_EMOJI.get(sentiment, "🔬")


def _reading_time(content: str) -> int:
    """Estimate reading time in minutes (200 words/min)."""
    return max(1, min(20, round(len(content.split()) / 200)))


# ─────────────────────────────────────────────────────────────────────────────
# MOCK  (returned when OLLAMA_API_KEY is not set)
# ─────────────────────────────────────────────────────────────────────────────

def _mock_insight(title: str, article_url: str = "", domain: str = "Technology") -> dict:
    """Return a placeholder insight when no API key is configured."""
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
            "Set OLLAMA_API_KEY in your .env to enable real AI-generated insights using Gemma 4 (gemma4:31b-cloud)."
        ),
        "key_points": [
            "A major development has been identified in this domain.",
            "Multiple stakeholders across research and industry are affected.",
            "Policy implications and long-term impact are still being assessed.",
        ],
        "sentiment":        "neutral",
        "sentiment_label":  "neutral_news",
        "emoji":            _fallback_emoji("neutral", domain),
        "confidence":       0.5,
        "tags":             ["steami", "demo", domain.lower().replace("/", "-")],
        "domain":           domain,
        "reading_time_min": 3,
        "article_url":      article_url,
    }