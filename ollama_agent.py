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
  "confidence": 0.85,
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "domain": "{domain}",
  "reading_time_min": 4,
  "article_url": "{article_url}",
  "svg": "WRITE_FULL_SVG_HERE"
}}

For the svg field — replace WRITE_FULL_SVG_HERE with a complete inline SVG:
- Use width='400' height='400' (single quotes ONLY inside SVG — never double quotes)
- Dark background rect fill '#0f0f1a'
- Colors: #6366f1 #818cf8 #a5b4fc #f59e0b #34d399 #f87171
- Draw a diagram that visually explains the article concept:
    AI / machine learning  → neural network nodes and weighted edges
    Space / astronomy      → planets, orbits, or star field diagram
    Biology / Medicine     → cell structure or DNA double helix
    Robotics               → robot arm with joints labeled
    Physics                → wave diagram or particle collision
    Chemistry              → molecule with atoms and bonds labeled
    Engineering            → circuit schematic or gear system
    Mathematics            → graph with curve or geometric proof diagram
    Computer Science       → flowchart boxes with arrows
    Climate / Energy       → earth diagram or energy flow chart
- Add domain label text near top in color #818cf8 font-size='11'
- Add STEAMI text at bottom right in color #6366f1 font-size='10'
- Keep SVG under 2500 characters total
- CRITICAL: Use ONLY single quotes inside the SVG. Zero double quotes anywhere inside the SVG string."""


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
        dict with keys: summary, key_points, sentiment, confidence, tags,
                        domain, reading_time_min, article_url, svg

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

    # Fallback SVG if missing or malformed
    if not result.get("svg") or len(str(result.get("svg", ""))) < 60:
        result["svg"] = _fallback_svg(title, domain)

    log.info(
        "Insight ready — model=%s summary=%d words svg=%d chars domain=%s",
        model,
        len(result.get("summary", "").split()),
        len(str(result.get("svg", ""))),
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

    # ── Layer 3: Isolate SVG, parse the rest, re-attach ───────────────────
    try:
        svg_pat = re.search(
            r'"svg"\s*:\s*"(<svg[\s\S]*?</svg>)"',
            cleaned, re.IGNORECASE
        )
        if svg_pat:
            svg_content = svg_pat.group(1)
            safe = cleaned[:svg_pat.start()] + '"svg":"__SVG__"' + cleaned[svg_pat.end():]
            safe = re.sub(r",\s*([}\]])", r"\1", safe)   # strip trailing commas
            obj  = json.loads(safe)
            obj["svg"] = svg_content
            log.info("Parse L3 OK (svg isolated)")
            return _fill_defaults(obj, title, article_url, domain)
    except Exception:
        pass

    # ── Layer 4: Regex field extraction — last resort ─────────────────────
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
    obj.setdefault("svg",              "")

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

    def get_svg() -> str:
        m = re.search(r'"svg"\s*:\s*"(<svg[\s\S]*?</svg>)"', text, re.IGNORECASE)
        if m: return m.group(1)
        m = re.search(r'(<svg[\s\S]*?</svg>)', text, re.IGNORECASE)
        if m: return m.group(1)
        return ""

    summary    = get_str("summary")
    svg        = get_svg() or get_str("svg")
    sentiment  = get_str("sentiment") or "neutral"
    art_url    = get_str("article_url") or article_url
    dom        = get_str("domain") or domain
    key_points = get_arr("key_points")
    tags       = get_arr("tags")

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
# FALLBACK SVG  (single quotes only — safe to embed in JSON)
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_svg(title: str, domain: str = "Technology") -> str:
    """Generate a minimal SVG diagram when the AI fails to produce one."""
    safe     = (title[:40].replace("'","").replace("<","").replace(">","")
                          .replace("&","and").replace('"',""))
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
        "svg":              _fallback_svg(title, domain),
        "key_points": [
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