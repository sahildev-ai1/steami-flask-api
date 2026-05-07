"""
ollama_agent.py  —  STEAMI AI Insight Generator using Ollama Cloud API
=======================================================================
V3 CHANGES (Chart.js overhaul):
  generate_newsletter_chart() now returns a Chart.js config JSON instead
  of generating SVG or running matplotlib. This means:
    - No subprocess sandbox
    - No cairosvg / svglib dependencies
    - No /tmp file storage
    - Chart config is stored in MongoDB and rendered server-side via
      QuickChart.io (free PNG API) — universally supported in all email clients
    - Frontend renders the same config via Chart.js canvas for live preview

PUBLIC INTERFACE (complete):
  generate_ai_insight(article)             — unchanged
  generate_cover_story(article)            — unchanged
  generate_newsletter_chart(article, cover_story)  — UPDATED: returns Chart.js config
"""

import os
import json
import re
import logging
import base64
import tempfile
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CLOUD CONFIG  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

OLLAMA_CLOUD_URL  = "https://ollama.com/api/chat"
OLLAMA_LOCAL_HOST = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")

def _api_url() -> str:
    if OLLAMA_LOCAL_HOST:
        return f"{OLLAMA_LOCAL_HOST}/api/chat"
    return OLLAMA_CLOUD_URL

# Keep for backward compat — no longer written to, but imported by newsletter.py
CHART_STORE_DIR = Path(tempfile.gettempdir()) / "steami_charts"
CHART_STORE_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# ORIGINAL SYSTEM PROMPT  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are STEAMI, a science and technology explainer AI.
Your job is to read a news or research article and return a structured JSON insight.
You must respond with ONLY valid JSON — no markdown, no explanation, no text before or after.
The JSON must follow the exact schema provided in the user message.
Use clear, engaging language appropriate for curious students and researchers.
Do NOT include any thinking tokens or chain-of-thought in your response."""


# ─────────────────────────────────────────────────────────────────────────────
# ORIGINAL INSIGHT PROMPT  (unchanged)
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
# COVER STORY SYSTEM PROMPT  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

COVER_STORY_SYSTEM = """You are the lead science journalist for STEAMI, a premium science and technology newsletter.
Your job is to transform a news article into a compelling, long-form newsletter cover story.
You must respond with ONLY valid JSON — no markdown, no explanation, no text before or after.
Write with authority, clarity, and genuine scientific curiosity.
Avoid hype — be precise, analytical, and human.
Do NOT include any thinking tokens or chain-of-thought in your response."""


# ─────────────────────────────────────────────────────────────────────────────
# COVER STORY PROMPT TEMPLATE  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

COVER_STORY_PROMPT = """Transform this article into a long-form newsletter cover story. Return ONLY valid JSON.

Article Title: {title}
Domain: {domain}
Date: {date}
Source URL: {article_url}
Content: {content}

Return this exact JSON structure:

{{
  "headline": "A punchy, specific headline (max 12 words). Not clickbait — precise and compelling.",
  "standfirst": "A single sentence (20-30 words) that explains what happened and why it matters. Think The Economist opening line.",
  "body_paragraphs": [
    "First paragraph (60-80 words): The core finding — what happened, who did it, where, when. No fluff.",
    "Second paragraph (60-80 words): Why it matters — real-world implications, scientific significance, what changes because of this.",
    "Third paragraph (50-70 words): Context — how this fits into the bigger picture, what came before, competing approaches.",
    "Fourth paragraph (40-60 words): What comes next — unanswered questions, next steps, timeline for impact."
  ],
  "pull_quote": "A memorable single sentence (max 20 words) from the analysis that could stand alone as a pull quote.",
  "key_stats": [
    {{"label": "short label (2-4 words)", "value": "numeric or concise value", "context": "one line of context"}},
    {{"label": "short label (2-4 words)", "value": "numeric or concise value", "context": "one line of context"}},
    {{"label": "short label (2-4 words)", "value": "numeric or concise value", "context": "one line of context"}}
  ],
  "chart_subject": "What the accompanying chart should visualize — be specific about data type and axis values.",
  "chart_data": {{
    "type": "bar",
    "title": "Chart title (max 8 words)",
    "x_label": "X axis label",
    "y_label": "Y axis label",
    "labels": ["label1", "label2", "label3", "label4", "label5"],
    "values": [0, 0, 0, 0, 0],
    "unit": "% or number or other unit"
  }},
  "closing_line": "A forward-looking closing sentence (max 20 words) that leaves the reader thinking.",
  "reading_time_min": 3,
  "domain": "{domain}"
}}

Rules:
- key_stats: extract or estimate 3 specific numbers from the article.
- chart_data values must be actual numbers (floats or ints), not strings. Make them realistic.
- chart_data type: use "bar" for comparisons/categories, "line" for trends over time.
- body_paragraphs: exactly 4 paragraphs. Each must be self-contained prose — no bullet points, no headers.
- headline: do NOT start with 'The'. Be specific — include a number or name if possible.
- pull_quote: make it the single most quotable insight from your analysis."""


# ─────────────────────────────────────────────────────────────────────────────
# NEW V3: CHART.JS CONFIG SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

CHARTJS_SYSTEM = """You are a data visualization expert generating Chart.js configuration objects for STEAMI newsletter.
You must respond with ONLY valid JSON — no markdown, no explanation, no text before or after.
The Chart.js config must be clean, accurate, and render beautifully as a PNG in email clients.
Do NOT include any thinking tokens or chain-of-thought in your response."""


CHARTJS_PROMPT = """Generate a Chart.js configuration for this newsletter chart. Return ONLY valid JSON.

Chart specification:
  Type:    {chart_type}
  Title:   {chart_title}
  X-axis:  {x_label}
  Y-axis:  {y_label}
  Labels:  {labels}
  Values:  {values}
  Unit:    {unit}
  Article: {article_title}
  Domain:  {domain}

Return this exact JSON structure (a complete Chart.js config object):
{{
  "chartjs_config": {{
    "type": "bar",
    "data": {{
      "labels": ["label1", "label2"],
      "datasets": [{{
        "label": "Dataset label",
        "data": [0, 0],
        "backgroundColor": ["#1d4ed8", "#2563eb", "#3b82f6", "#60a5fa", "#93c5fd"],
        "borderColor": "#1d4ed8",
        "borderWidth": 2,
        "borderRadius": 6,
        "tension": 0.4
      }}]
    }},
    "options": {{
      "responsive": true,
      "plugins": {{
        "title": {{
          "display": true,
          "text": "Chart Title",
          "font": {{"size": 15, "weight": "bold"}},
          "color": "#0f172a",
          "padding": {{"bottom": 16}}
        }},
        "legend": {{"display": false}},
        "tooltip": {{
          "callbacks": {{}}
        }}
      }},
      "scales": {{
        "x": {{
          "title": {{"display": true, "text": "X Label", "color": "#475569"}},
          "grid": {{"display": false}},
          "ticks": {{"color": "#64748b"}}
        }},
        "y": {{
          "title": {{"display": true, "text": "Y Label", "color": "#475569"}},
          "grid": {{"color": "#e2e8f0"}},
          "ticks": {{"color": "#64748b"}},
          "beginAtZero": true
        }}
      }}
    }}
  }},
  "explanation": "Two sentences explaining what this chart shows and why it matters for the article."
}}

Rules:
- Use "bar" for category comparisons, "line" for trends over time.
- For bar charts: backgroundColor should be an array of blues (#1d4ed8 → #93c5fd gradient).
- For line charts: use backgroundColor: "rgba(29,78,216,0.1)" (fill), borderColor: "#1d4ed8".
- Fill in the actual labels and values from the chart specification above.
- Set plugins.title.text to the chart title from the specification.
- Set scales.x.title.text and scales.y.title.text from the specification.
- All values in data.datasets[0].data must be numbers, not strings.
- Keep options clean — only include fields listed above, no extra properties."""


# ─────────────────────────────────────────────────────────────────────────────
# CORE OLLAMA CALLER  (shared by all generators — unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _call_ollama(system_prompt: str, user_prompt: str, timeout: int = 120) -> str:
    """
    Call Ollama API (cloud or local) with a system + user message.
    Returns raw string content from the model response.
    Raises RuntimeError on HTTP/network failures.
    """
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    model   = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model":    model,
        "stream":   False,
        "format":   "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "options": {
            "temperature": 1.0,
            "top_p":       0.95,
            "top_k":       64,
            "num_predict": 6000,
        },
    }

    url = _api_url()
    log.info("Ollama call: url=%s model=%s", url, model)

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.Timeout:
        raise RuntimeError(f"Ollama API timed out after {timeout}s")
    except requests.ConnectionError as e:
        raise RuntimeError(f"Ollama API connection failed: {e}")

    if resp.status_code == 401:
        raise RuntimeError(
            "Ollama API 401 Unauthorized. "
            "Check OLLAMA_API_KEY at https://ollama.com/settings/keys"
        )
    if resp.status_code == 404:
        model_name = body["model"]
        if not OLLAMA_LOCAL_HOST:
            raise RuntimeError(
                f"Ollama Cloud: model '{model_name}' not found. "
                "Use gemma4:31b-cloud for cloud API."
            )
        raise RuntimeError(
            f"Ollama local model '{model_name}' not found. "
            "Run: ollama pull gemma4:e4b"
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:400]}")

    return resp.json().get("message", {}).get("content", "")


def _parse_json_response(raw: str, context: str = "") -> dict:
    """
    Robust 4-layer JSON parser.
    Raises RuntimeError if all layers fail.
    """
    if not raw:
        raise RuntimeError(f"Empty response from Ollama ({context})")

    # Layer 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Layer 2: strip markdown fences
    cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'\s*```$', '', cleaned.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Layer 3: extract first {...} block
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # Layer 4: fix common escaping issues
    try:
        fixed = raw.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        m2 = re.search(r'\{.*\}', fixed, re.DOTALL)
        if m2:
            return json.loads(m2.group())
    except Exception:
        pass

    raise RuntimeError(f"Could not parse Ollama JSON response ({context}): {raw[:300]}")


# ─────────────────────────────────────────────────────────────────────────────
# generate_cover_story  (unchanged from v2)
# ─────────────────────────────────────────────────────────────────────────────

def generate_cover_story(article: dict) -> dict:
    """
    Generate a long-form newsletter cover story for the given article.

    Returns dict with keys:
      headline, standfirst, body_paragraphs (list[str]), pull_quote,
      key_stats (list[{label, value, context}]), chart_subject,
      chart_data ({type, title, x_label, y_label, labels, values, unit}),
      closing_line, reading_time_min, domain
    """
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()

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
    if isinstance(content, list):
        content = " ".join(str(p) for p in content)
    content = str(content)[:8000]

    raw_date = article.get("fetched_at") or article.get("published_at") or ""
    date_str = ""
    if raw_date:
        try:
            from datetime import datetime, timezone
            date_str = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%B %d, %Y")
        except Exception:
            date_str = raw_date[:10]

    if not content.strip():
        raise ValueError(f"Article '{title}' has no content for cover story generation")

    if not api_key and not OLLAMA_LOCAL_HOST:
        log.warning("Ollama not configured — returning mock cover story for '%s'", title)
        return _mock_cover_story(title, domain, article_url)

    user_prompt = COVER_STORY_PROMPT.format(
        title       = title,
        domain      = domain,
        date        = date_str or "Recent",
        article_url = article_url,
        content     = content,
    )

    raw = _call_ollama(COVER_STORY_SYSTEM, user_prompt, timeout=180)

    try:
        obj = _parse_json_response(raw, context=f"cover_story:{title[:40]}")
    except RuntimeError as e:
        log.error("Cover story parse failed: %s", e)
        return _mock_cover_story(title, domain, article_url)

    # Validate & clean
    paras = obj.get("body_paragraphs", [])
    if isinstance(paras, str):
        paras = [paras]
    obj["body_paragraphs"] = [str(p) for p in paras if p][:4]

    stats = obj.get("key_stats", [])
    if not isinstance(stats, list):
        stats = []
    obj["key_stats"] = [
        {
            "label":   s.get("label", "Stat"),
            "value":   str(s.get("value", "—")),
            "context": s.get("context", ""),
        }
        for s in stats if isinstance(s, dict)
    ][:3]

    chart_data = obj.get("chart_data", {})
    if isinstance(chart_data, dict):
        vals = chart_data.get("values", [])
        try:
            chart_data["values"] = [float(v) for v in vals]
        except (TypeError, ValueError):
            chart_data["values"] = [0.0] * len(chart_data.get("labels", []))
    obj["chart_data"] = chart_data

    obj.setdefault("headline",         title)
    obj.setdefault("standfirst",       "")
    obj.setdefault("pull_quote",       "")
    obj.setdefault("closing_line",     "")
    obj.setdefault("reading_time_min", 3)
    obj.setdefault("domain",           domain)
    obj.setdefault("chart_subject",    "")

    log.info("Cover story generated for '%s'", title[:55])
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# NEW V3: generate_newsletter_chart — returns Chart.js config JSON
# ─────────────────────────────────────────────────────────────────────────────

def generate_newsletter_chart(article: dict, cover_story: dict) -> dict:
    """
    Generate a Chart.js config JSON for a newsletter cover story.

    V3 CHANGE: Instead of generating SVG or running matplotlib, this function
    asks Ollama for a Chart.js config object. The config is:
      - Stored directly in MongoDB (cover_chart_config field)
      - Rendered server-side as PNG via QuickChart.io for email delivery
      - Rendered client-side via Chart.js canvas for live preview

    Args:
        article:     Original article dict (needs 'id', 'title', 'matched_domains')
        cover_story: Output of generate_cover_story() — needs 'chart_data', 'domain'

    Returns:
        dict with keys:
          chartjs_config  (dict)  — complete Chart.js config object
          explanation     (str)   — two-sentence explanation of the chart
          chart_type      (str)   — "bar" | "line"
          success         (bool)  — False if chart generation failed (fallback used)
          error           (str)   — error message if success=False, else ""

    Never raises — returns success=False with error message on failure.
    """
    api_key    = os.environ.get("OLLAMA_API_KEY", "").strip()
    article_id = str(article.get("id", "unknown"))
    title      = article.get("title", "Untitled")
    domain     = cover_story.get("domain") or (
        (article.get("matched_domains") or ["Technology"])[0]
    )

    chart_data = cover_story.get("chart_data", {})

    # ── Fallback if no Ollama ─────────────────────────────────────────────────
    if not api_key and not OLLAMA_LOCAL_HOST:
        log.warning("Ollama not configured — generating fallback Chart.js config for '%s'", article_id)
        config = _fallback_chartjs_config(chart_data, title)
        chart_file_path = _render_chartjs_to_png_file(config, article_id)
        chart_image_url = ""
        if chart_file_path and API_BASE_URL:
            chart_image_url = f"{API_BASE_URL}/{Path(chart_file_path).as_posix().lstrip('./')}"
        png_b64 = _render_chartjs_to_png_b64(config)
        render_ok = bool(chart_image_url or png_b64)
        return {
            "chartjs_config":  config,
            "chart_png_b64":   png_b64,
            "chart_image_url": chart_image_url,
            "chart_file_path": chart_file_path,
            "explanation":     cover_story.get("chart_subject", "Chart showing data from the article."),
            "chart_type":      chart_data.get("type", "bar"),
            "render_ok":       render_ok,
            "success":         True,
            "error":           "" if render_ok else "QuickChart.io render failed — chart will not appear in email",
        }

    # ── Build prompt ──────────────────────────────────────────────────────────
    labels = chart_data.get("labels", [])
    values = chart_data.get("values", [])

    user_prompt = CHARTJS_PROMPT.format(
        chart_type    = chart_data.get("type", "bar"),
        chart_title   = chart_data.get("title", title[:50]),
        x_label       = chart_data.get("x_label", ""),
        y_label       = chart_data.get("y_label", ""),
        labels        = json.dumps(labels),
        values        = json.dumps(values),
        unit          = chart_data.get("unit", ""),
        article_title = title[:80],
        domain        = domain,
    )

    try:
        raw = _call_ollama(CHARTJS_SYSTEM, user_prompt, timeout=90)
        obj = _parse_json_response(raw, context=f"chartjs:{article_id}")
    except RuntimeError as e:
        log.error("Chart.js config generation failed: %s — using fallback", e)
        config = _fallback_chartjs_config(chart_data, title)
        chart_file_path = _render_chartjs_to_png_file(config, article_id)
        chart_image_url = ""
        if chart_file_path and API_BASE_URL:
            chart_image_url = f"{API_BASE_URL}/{Path(chart_file_path).as_posix().lstrip('./')}"
        png_b64 = _render_chartjs_to_png_b64(config)
        render_ok = bool(chart_image_url or png_b64)
        return {
            "chartjs_config":  config,
            "chart_png_b64":   png_b64,
            "chart_image_url": chart_image_url,
            "chart_file_path": chart_file_path,
            "explanation":     cover_story.get("chart_subject", ""),
            "chart_type":      chart_data.get("type", "bar"),
            "render_ok":       render_ok,
            "success":         True,
            "error":           str(e),
        }

    chartjs_config = obj.get("chartjs_config", {})
    explanation    = obj.get("explanation", cover_story.get("chart_subject", ""))

    # ── Validate the config has required structure ─────────────────────────────
    if not chartjs_config or "type" not in chartjs_config or "data" not in chartjs_config:
        log.warning("Ollama returned invalid Chart.js config for %s — using fallback", article_id)
        chartjs_config = _fallback_chartjs_config(chart_data, title)

    # Ensure data values are numbers, not strings
    try:
        datasets = chartjs_config.get("data", {}).get("datasets", [])
        for ds in datasets:
            ds["data"] = [float(v) for v in ds.get("data", [])]
    except (TypeError, ValueError) as e:
        log.warning("Chart.js data coercion failed: %s", e)

    log.info("Chart.js config generated for article '%s' (type=%s)", article_id, chartjs_config.get("type", "?"))

    # ── Render to PNG via QuickChart.io for email delivery ────────────────────
    # Strategy (two parallel outputs — both are attempted):
    #
    #   1. DISK FILE  →  public URL  (http://API_BASE_URL/images/newsletter/charts/<id>.png)
    #      Same pattern as explainer images (images/explainers/epigenetics.jpg).
    #      Gmail loads it as a plain <img src="...">, no base64 bloat in the DB.
    #      Requires API_BASE_URL to be set in .env.
    #
    #   2. BASE64 DATA URI  →  inline fallback
    #      Works even without a public URL (e.g. local dev, no static file server).
    #      Stored in cover_chart_png_b64 in MongoDB and used directly in the HTML.
    #
    # newsletter.py _build_custom_html() prefers the URL if available (Priority 0),
    # then falls back to the base64 data URI (Priority 1).

    # 1. Save PNG to disk and build a public URL
    chart_file_path = _render_chartjs_to_png_file(chartjs_config, article_id)
    chart_image_url = ""
    if chart_file_path and API_BASE_URL:
        # Normalise to forward slashes and strip any leading "./" or "images/"
        # so the URL always looks like: {API_BASE_URL}/images/newsletter/charts/<id>.png
        relative = Path(chart_file_path).as_posix()
        if not relative.startswith("images/"):
            # chart_file_path is already images/newsletter/charts/<id>.png
            relative = relative.lstrip("./")
        chart_image_url = f"{API_BASE_URL}/{relative}"
        log.info("Chart PNG saved → public URL: %s", chart_image_url)
    elif chart_file_path:
        log.warning("Chart PNG saved to disk but API_BASE_URL is not set — no public URL available")

    # 2. Render to base64 for inline fallback
    png_b64 = _render_chartjs_to_png_b64(chartjs_config)
    if not png_b64 and not chart_image_url:
        log.warning("QuickChart.io PNG render failed for article '%s' — chart will not appear in email", article_id)

    render_ok = bool(chart_image_url or png_b64)
    error = "" if render_ok else "QuickChart.io render failed — chart will not appear in email"

    return {
        "chartjs_config":   chartjs_config,
        "chart_png_b64":    png_b64,
        "chart_image_url":  chart_image_url,   # NEW: public URL like http://localhost:5000/images/newsletter/charts/xyz.png
        "chart_file_path":  chart_file_path,   # raw filesystem path (for newsletter.py fallback)
        "explanation":      explanation,
        "chart_type":       chartjs_config.get("type", "bar"),
        "render_ok":        render_ok,
        "success":          True,
        "error":            error,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK CHART.JS CONFIG  (pure Python — no Ollama needed)
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_chartjs_config(chart_data: dict, title: str) -> dict:
    """
    Build a valid Chart.js config from chart_data without calling Ollama.
    Returns a complete Chart.js config dict. Never raises.
    """
    try:
        labels      = [str(l) for l in chart_data.get("labels", ["A", "B", "C", "D", "E"])]
        values      = chart_data.get("values", [1, 2, 3, 4, 5])
        chart_title = chart_data.get("title", title[:60])
        x_label     = chart_data.get("x_label", "")
        y_label     = chart_data.get("y_label", "")
        chart_type  = chart_data.get("type", "bar")

        try:
            values = [float(v) for v in values]
        except (TypeError, ValueError):
            values = list(range(1, len(labels) + 1))

        min_len = min(len(labels), len(values))
        labels  = labels[:min_len]
        values  = values[:min_len]

        # STEAMI brand blue palette
        blues = ["#1d4ed8", "#2563eb", "#3b82f6", "#60a5fa", "#93c5fd",
                 "#1e40af", "#1d4ed8", "#2563eb", "#3b82f6", "#60a5fa"]
        bg_colors = blues[:len(labels)] if len(labels) <= len(blues) else blues * (len(labels) // len(blues) + 1)
        bg_colors = bg_colors[:len(labels)]

        if chart_type == "line":
            dataset = {
                "label":           y_label or "Value",
                "data":            values,
                "borderColor":     "#1d4ed8",
                "backgroundColor": "rgba(29,78,216,0.1)",
                "borderWidth":     2,
                "tension":         0.4,
                "fill":            True,
                "pointBackgroundColor": "#1d4ed8",
                "pointRadius":     5,
            }
        else:
            dataset = {
                "label":           y_label or "Value",
                "data":            values,
                "backgroundColor": bg_colors,
                "borderColor":     "#1d4ed8",
                "borderWidth":     0,
                "borderRadius":    6,
            }

        return {
            "type": "line" if chart_type == "line" else "bar",
            "data": {
                "labels":   labels,
                "datasets": [dataset],
            },
            "options": {
                "responsive": True,
                "plugins": {
                    "title": {
                        "display": True,
                        "text":    chart_title,
                        "font":    {"size": 15, "weight": "bold"},
                        "color":   "#0f172a",
                        "padding": {"bottom": 16},
                    },
                    "legend": {"display": False},
                },
                "scales": {
                    "x": {
                        "title": {"display": bool(x_label), "text": x_label, "color": "#475569"},
                        "grid":  {"display": False},
                        "ticks": {"color": "#64748b"},
                    },
                    "y": {
                        "title": {"display": bool(y_label), "text": y_label, "color": "#475569"},
                        "grid":  {"color": "#e2e8f0"},
                        "ticks": {"color": "#64748b"},
                        "beginAtZero": True,
                    },
                },
            },
        }
    except Exception as e:
        log.error("Fallback Chart.js config generation failed: %s", e)
        # Absolute minimal valid config
        return {
            "type": "bar",
            "data": {
                "labels":   ["Data"],
                "datasets": [{"label": "Value", "data": [1], "backgroundColor": ["#1d4ed8"]}],
            },
            "options": {"plugins": {"title": {"display": True, "text": title[:60]}}},
        }


# ─────────────────────────────────────────────────────────────────────────────
# QUICKCHART.IO PNG RENDERER
# ─────────────────────────────────────────────────────────────────────────────

QUICKCHART_URL = "https://quickchart.io/chart"

# Base URL of this FastAPI backend — used to build public image URLs for email
# e.g.  API_BASE_URL=http://localhost:5000  →  http://localhost:5000/images/newsletter/charts/xyz.png
API_BASE_URL = os.environ.get("API_BASE_URL", "").strip().rstrip("/")

# Directory where chart PNGs are saved — served as static files at /images/newsletter/charts/
# Mirrors the explainer image pattern:  images/explainers/epigenetics.jpg
CHART_IMAGE_DIR = Path("images") / "newsletter" / "charts"
CHART_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


def _render_chartjs_to_png_b64(chartjs_config: dict,
                                width: int = 596, height: int = 280) -> str:
    """
    Render a Chart.js config to a PNG via QuickChart.io and return a
    base64 data URI (data:image/png;base64,...).

    Used as an email-safe inline fallback when no public URL is available.
    Returns "" on failure.
    """
    if not chartjs_config:
        return ""

    try:
        config_copy = json.loads(json.dumps(chartjs_config))
        try:
            config_copy["options"]["plugins"]["tooltip"].pop("callbacks", None)
        except (KeyError, TypeError, AttributeError):
            pass
    except (TypeError, ValueError) as e:
        log.warning("QuickChart b64: could not serialise Chart.js config: %s", e)
        return ""

    payload = {
        "chart":            config_copy,
        "width":            width,
        "height":           height,
        "backgroundColor":  "white",
        "format":           "png",
        "devicePixelRatio": 2,
    }

    try:
        resp = requests.post(
            QUICKCHART_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
    except requests.Timeout:
        log.warning("QuickChart b64: request timed out after 20s")
        return ""
    except requests.ConnectionError as e:
        log.warning("QuickChart b64: connection error: %s", e)
        return ""

    if resp.status_code != 200:
        log.warning("QuickChart b64: HTTP %d — %s", resp.status_code, resp.text[:200])
        return ""

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if "png" not in content_type and "image" not in content_type:
        log.warning("QuickChart b64: unexpected content-type '%s'", content_type)
        return ""

    b64 = base64.b64encode(resp.content).decode("ascii")
    log.info("QuickChart b64: PNG rendered (%d bytes)", len(resp.content))
    return f"data:image/png;base64,{b64}"


def _render_chartjs_to_png_file(chartjs_config: dict, article_id: str,
                                 width: int = 596, height: int = 280) -> str:
    """
    Render a Chart.js config to a PNG via QuickChart.io and save it to disk at:
      images/newsletter/charts/<article_id>.png

    This mirrors how explainer images are stored (e.g. images/explainers/epigenetics.jpg)
    and served as static files by FastAPI, so Gmail can load them via a plain <img src="...">.

    Returns the saved file path string on success, or "" on failure.
    """
    if not chartjs_config:
        return ""

    # Strip non-serialisable fields (e.g. JS callback functions)
    try:
        config_copy = json.loads(json.dumps(chartjs_config))
        try:
            config_copy["options"]["plugins"]["tooltip"].pop("callbacks", None)
        except (KeyError, TypeError, AttributeError):
            pass
    except (TypeError, ValueError) as e:
        log.warning("QuickChart: could not serialise Chart.js config: %s", e)
        return ""

    payload = {
        "chart":            config_copy,
        "width":            width,
        "height":           height,
        "backgroundColor":  "white",
        "format":           "png",
        "devicePixelRatio": 2,
    }

    try:
        resp = requests.post(
            QUICKCHART_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
    except requests.Timeout:
        log.warning("QuickChart: request timed out after 20s")
        return ""
    except requests.ConnectionError as e:
        log.warning("QuickChart: connection error: %s", e)
        return ""

    if resp.status_code != 200:
        log.warning("QuickChart: HTTP %d — %s", resp.status_code, resp.text[:200])
        return ""

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if "png" not in content_type and "image" not in content_type:
        log.warning("QuickChart: unexpected content-type '%s'", content_type)
        return ""

    # Save PNG to disk — same pattern as explainer images
    safe_id   = re.sub(r'[^a-zA-Z0-9_\-]', '', article_id)
    file_path = CHART_IMAGE_DIR / f"{safe_id}.png"
    try:
        file_path.write_bytes(resp.content)
    except Exception as e:
        log.warning("QuickChart: could not write PNG to disk at %s: %s", file_path, e)
        return ""

    log.info("QuickChart: PNG saved to %s (%d bytes)", file_path, len(resp.content))
    return str(file_path)


# ─────────────────────────────────────────────────────────────────────────────
# MOCK COVER STORY  (returned when Ollama is not configured)
# ─────────────────────────────────────────────────────────────────────────────

def _mock_cover_story(title: str, domain: str, article_url: str = "") -> dict:
    return {
        "headline":    f"Breaking Ground in {domain}: {title[:50]}",
        "standfirst":  (
            f"A significant development in {domain} is reshaping how researchers "
            f"approach this critical field — here's what you need to know."
        ),
        "body_paragraphs": [
            f"This article covers a major development in {domain}. "
            "The findings represent a meaningful step forward in our understanding of the field. "
            "Researchers have been working on this problem for several years, "
            "and the results are beginning to bear fruit.",
            "The implications for the broader scientific community are significant. "
            "This work opens new avenues of research and raises important questions about "
            "how we approach related problems. Industry stakeholders are paying close attention.",
            "This development sits within a rich landscape of ongoing research. "
            "Several competing teams have pursued similar goals, and this result is likely "
            "to accelerate the broader field. Previous breakthroughs laid the groundwork.",
            "The road ahead involves further validation, peer review, and real-world application. "
            "Set OLLAMA_API_KEY in your .env to enable real AI-generated cover stories.",
        ],
        "pull_quote":  f"This development in {domain} could reshape how we approach the field entirely.",
        "key_stats": [
            {"label": "Domain",   "value": domain,   "context": "Primary research area"},
            {"label": "Impact",   "value": "High",   "context": "Expert consensus"},
            {"label": "Timeline", "value": "2026+",  "context": "Expected deployment"},
        ],
        "chart_subject": f"Key metrics related to {domain} development trends",
        "chart_data": {
            "type":    "bar",
            "title":   f"{domain} Growth Indicators",
            "x_label": "Category",
            "y_label": "Score",
            "labels":  ["Research", "Funding", "Adoption", "Impact", "Potential"],
            "values":  [72.0, 85.0, 61.0, 78.0, 91.0],
            "unit":    "score",
        },
        "closing_line": f"The story of {domain} is still being written — and this chapter matters.",
        "reading_time_min": 3,
        "domain": domain,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ORIGINAL: generate_ai_insight  (100% unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def generate_ai_insight(article: dict) -> dict:
    """
    Generate an AI insight for a news/research article using Ollama Cloud (gemma4).
    UNCHANGED from v1 — same signature, same output schema.
    """
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    model   = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

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
    if isinstance(content, list):
        content = " ".join(str(p) for p in content)
    content = str(content)[:6000]

    if not content.strip():
        raise ValueError(f"Article '{title}' has no content to analyse")

    if not api_key:
        log.warning("OLLAMA_API_KEY not set — returning mock insight for '%s'", title)
        return _mock_insight(title, article_url, domain)

    user_prompt = INSIGHT_PROMPT.format(
        title=title, domain=domain, content=content, article_url=article_url,
    )

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "model":    model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "num_predict": 4000},
    }

    url = _api_url()
    log.info("Ollama insight: url=%s model=%s title=%.55s", url, model, title)

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=120)
    except requests.Timeout:
        raise RuntimeError("Ollama API timed out after 120s")
    except requests.ConnectionError as e:
        raise RuntimeError(f"Ollama API connection failed: {e}")

    if resp.status_code == 401:
        raise RuntimeError("Ollama API 401 — check OLLAMA_API_KEY")
    if resp.status_code == 404:
        if not OLLAMA_LOCAL_HOST:
            raise RuntimeError(f"Ollama Cloud: model '{model}' not found. Use gemma4:31b-cloud.")
        raise RuntimeError(f"Local model '{model}' not found. Run: ollama pull {model}")
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:400]}")

    raw = resp.json().get("message", {}).get("content", "")

    try:
        obj = _parse_json_response(raw, context=f"insight:{title[:40]}")
    except RuntimeError:
        return _extract(raw, title, article_url, domain)

    return _validate_insight(obj, title, article_url, domain)


def _validate_insight(obj: dict, title: str, article_url: str, domain: str) -> dict:
    """Clean and validate insight output — unchanged from v1."""
    sentiment = obj.get("sentiment", "neutral")
    if sentiment not in ("positive", "neutral", "negative"):
        sentiment = "neutral"
    obj["sentiment"] = sentiment
    obj["sentiment_label"] = _sentiment_to_label(sentiment)

    raw_emoji = obj.get("emoji", "")
    if not raw_emoji or len(raw_emoji.encode("utf-8")) > 16:
        obj["emoji"] = _fallback_emoji(sentiment, domain)

    if not isinstance(obj.get("key_points"), list) or not obj["key_points"]:
        obj["key_points"] = [
            "Key finding identified in this article.",
            "This development has significant implications.",
            "Further monitoring and research are expected.",
        ]

    if not isinstance(obj.get("tags"), list):
        obj["tags"] = []

    try:
        obj["confidence"] = float(obj.get("confidence", 0.7))
        obj["confidence"] = max(0.0, min(1.0, obj["confidence"]))
    except (TypeError, ValueError):
        obj["confidence"] = 0.7

    try:
        obj["reading_time_min"] = int(obj.get("reading_time_min", 3))
    except (TypeError, ValueError):
        obj["reading_time_min"] = 3

    obj.setdefault("article_url", article_url)
    obj.setdefault("domain", domain)

    s = obj.get("summary", "")
    if isinstance(s, str) and (s.strip().startswith("{") or len(s) > 2000):
        obj["summary"] = f"Analysis of: {title}"

    return obj


def _extract(text: str, title: str, article_url: str, domain: str) -> dict:
    """Regex field extraction — last resort — unchanged from v1."""
    def get_str(key):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        return m.group(1) if m else ""

    def get_num(key, default):
        m = re.search(rf'"{key}"\s*:\s*([\d.]+)', text)
        return float(m.group(1)) if m else default

    def get_arr(key):
        m = re.search(rf'"{key}"\s*:\s*\[(.*?)\]', text, re.DOTALL)
        if not m: return []
        return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))

    summary   = get_str("summary")
    sentiment = get_str("sentiment") or "neutral"
    art_url   = get_str("article_url") or article_url
    dom       = get_str("domain") or domain
    key_points = get_arr("key_points")
    tags       = get_arr("tags")
    raw_emoji  = get_str("emoji")

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
# SENTIMENT HELPERS  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _sentiment_to_label(sentiment: str) -> str:
    return {"positive": "good_news", "negative": "bad_news", "neutral": "neutral_news"}.get(
        sentiment, "neutral_news"
    )

_EMOJI_MAP: dict[tuple[str, str], str] = {
    ("positive", "ai"): "🤖", ("positive", "machine"): "🧠", ("positive", "space"): "🚀",
    ("positive", "astro"): "🌌", ("positive", "bio"): "🧬", ("positive", "medicine"): "💊",
    ("positive", "health"): "🩺", ("positive", "climate"): "🌱", ("positive", "energy"): "⚡",
    ("positive", "physics"): "⚛️", ("positive", "engineer"): "🔋", ("positive", "robotics"): "🦾",
    ("positive", "computer"): "💻", ("positive", "cyber"): "🔐", ("positive", "math"): "📐",
    ("positive", "finance"): "📈", ("positive", "econom"): "💰",
    ("neutral", "ai"): "🤖", ("neutral", "space"): "🔭", ("neutral", "bio"): "🔬",
    ("neutral", "medicine"): "🔬", ("neutral", "health"): "🏥", ("neutral", "climate"): "🌍",
    ("neutral", "energy"): "⚙️", ("neutral", "physics"): "⚙️", ("neutral", "engineer"): "🔧",
    ("neutral", "robotics"): "⚙️", ("neutral", "computer"): "🖥️", ("neutral", "math"): "📊",
    ("neutral", "finance"): "💹", ("neutral", "econom"): "📊",
    ("negative", "ai"): "⚠️", ("negative", "space"): "☄️", ("negative", "bio"): "🦠",
    ("negative", "medicine"): "🦠", ("negative", "health"): "🚨", ("negative", "climate"): "🔥",
    ("negative", "energy"): "💥", ("negative", "physics"): "💥", ("negative", "engineer"): "🚨",
    ("negative", "robotics"): "🚨", ("negative", "computer"): "🐛", ("negative", "cyber"): "🔓",
    ("negative", "math"): "❌", ("negative", "finance"): "📉", ("negative", "econom"): "📉",
}

_SENTIMENT_DEFAULT_EMOJI: dict[str, str] = {
    "positive": "✨", "neutral": "🔬", "negative": "⚠️",
}

def _fallback_emoji(sentiment: str, domain: str) -> str:
    domain_lc = domain.lower()
    for (sent, kw), emoji in _EMOJI_MAP.items():
        if sent == sentiment and kw in domain_lc:
            return emoji
    return _SENTIMENT_DEFAULT_EMOJI.get(sentiment, "🔬")

def _reading_time(content: str) -> int:
    return max(1, min(20, round(len(content.split()) / 200)))


# ─────────────────────────────────────────────────────────────────────────────
# MOCK INSIGHT  (unchanged)
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
            "Set OLLAMA_API_KEY in your .env to enable real AI-generated insights."
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