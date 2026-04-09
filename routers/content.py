"""
Content router — /api/explainers/... and /api/research/...
Ported from Flask Blueprint to FastAPI APIRouter.
Seed data imported from static_content_data.py to avoid duplication.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from mongodb_client import db
# Auth guards — GET routes are public, writes require mod/admin, seed requires admin
from auth import require_auth, require_mod, require_admin, get_uid
from content_data import (
    EXPLAINERS_SEED,
    RESEARCH_ARTICLES_SEED,
    FIELDS_SEED,
    FIELD_ICONS_SEED,
    FIELD_COLORS_SEED,
)

log = logging.getLogger(__name__)
router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Request bodies ─────────────────────────────────────────────────────────

class CreateExplainerBody(BaseModel):
    id:          str
    title:       str
    subtitle:    str        = ""
    field:       str        = ""
    badgeColor:  str        = ""
    readTime:    str        = ""
    content:     list[str]  = []
    keyInsights: list[str]  = []

class UpdateExplainerBody(BaseModel):
    title:       Optional[str]       = None
    subtitle:    Optional[str]       = None
    field:       Optional[str]       = None
    badgeColor:  Optional[str]       = None
    readTime:    Optional[str]       = None
    content:     Optional[list[str]] = None
    keyInsights: Optional[list[str]] = None

class CreateResearchArticleBody(BaseModel):
    id:            str
    title:         str
    abstract:      str        = ""
    field:         str
    author:        str        = ""
    date:          str        = ""
    readTime:      str        = ""
    content:       list[str]  = []
    quotes:        list[str]  = []
    keyFindings:   list[str]  = []
    relatedTopics: list[str]  = []

class UpdateResearchArticleBody(BaseModel):
    title:         Optional[str]       = None
    abstract:      Optional[str]       = None
    field:         Optional[str]       = None
    author:        Optional[str]       = None
    date:          Optional[str]       = None
    readTime:      Optional[str]       = None
    content:       Optional[list[str]] = None
    quotes:        Optional[list[str]] = None
    keyFindings:   Optional[list[str]] = None
    relatedTopics: Optional[list[str]] = None


# ══════════════════════════════════════════════════════════════════════════
# EXPLAINERS
# ══════════════════════════════════════════════════════════════════════════

@router.post("/explainers/seed", status_code=201, tags=["Explainers"])
def seed_explainers(
    payload: dict = Depends(require_admin),  # admin only
):
    """Bulk-seed all explainers from explainers.ts. Safe to re-run."""
    seeded = []
    for ex in EXPLAINERS_SEED:
        doc = {**ex, "created_at": _now(), "updated_at": _now()}
        try:
            db.collection("explainers").document(ex["id"]).set(doc, merge=True)
            seeded.append(ex["id"])
        except Exception as e:
            log.error("seed_explainers failed for %s: %s", ex["id"], e)
    log.info("Explainers seeded: %d", len(seeded))
    return {"seeded": len(seeded), "ids": seeded}


@router.post("/explainers", status_code=201, tags=["Explainers"])
def create_explainer(
    body: CreateExplainerBody,
    payload: dict = Depends(require_mod),  # mod or admin
):
    """Create a single explainer."""
    doc = {**body.model_dump(), "created_at": _now(), "updated_at": _now()}
    try:
        db.collection("explainers").document(body.id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return doc


@router.get("/explainers", tags=["Explainers"])
def list_explainers(field: str = Query("")):
    """
    List all explainers. Optional ?field= filter.
    Response mirrors explainers.ts export exactly.
    """
    field_filter = field.strip().upper()
    try:
        q    = db.collection("explainers").order_by("created_at", direction="ASCENDING")
        docs = q.limit(100).stream()
        explainers = []
        for d in docs:
            ex = d.to_dict()
            if field_filter and ex.get("field", "").upper() != field_filter:
                continue
            explainers.append({
                "id":          ex.get("id"),
                "title":       ex.get("title"),
                "subtitle":    ex.get("subtitle"),
                "field":       ex.get("field"),
                "badgeColor":  ex.get("badgeColor"),
                "readTime":    ex.get("readTime"),
                "content":     ex.get("content", []),
                "keyInsights": ex.get("keyInsights", []),
            })
    except Exception as e:
        log.error("list_explainers failed: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"explainers": explainers, "total": len(explainers)}


@router.get("/explainers/{explainer_id}", tags=["Explainers"])
def get_explainer(explainer_id: str):
    """Get single Explainer — same shape as one item in explainers.ts."""
    doc = db.collection("explainers").document(explainer_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Explainer not found")
    ex = doc.to_dict()
    return {
        "id":          ex.get("id"),
        "title":       ex.get("title"),
        "subtitle":    ex.get("subtitle"),
        "field":       ex.get("field"),
        "badgeColor":  ex.get("badgeColor"),
        "readTime":    ex.get("readTime"),
        "content":     ex.get("content", []),
        "keyInsights": ex.get("keyInsights", []),
    }


@router.put("/explainers/{explainer_id}", tags=["Explainers"])
def update_explainer(
    explainer_id: str,
    body: UpdateExplainerBody,
    payload: dict = Depends(require_mod),  # mod or admin
):
    """Update an explainer's fields."""
    doc_ref = db.collection("explainers").document(explainer_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Explainer not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    updates["updated_at"] = _now()
    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"updated": True, "id": explainer_id}


@router.delete("/explainers/{explainer_id}", tags=["Explainers"])
def delete_explainer(
    explainer_id: str,
    payload: dict = Depends(require_admin),  # admin only
):
    """Delete an explainer."""
    doc_ref = db.collection("explainers").document(explainer_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Explainer not found")
    doc_ref.delete()
    return {"deleted": True, "id": explainer_id}


# ══════════════════════════════════════════════════════════════════════════
# RESEARCH FIELDS META
# ══════════════════════════════════════════════════════════════════════════

@router.get("/research/fields", tags=["Research"])
def get_fields():
    """Returns FIELDS, FIELD_ICONS, FIELD_COLORS from research-articles.ts."""
    return {
        "fields":       FIELDS_SEED,
        "field_icons":  FIELD_ICONS_SEED,
        "field_colors": FIELD_COLORS_SEED,
    }


# ══════════════════════════════════════════════════════════════════════════
# RESEARCH ARTICLES
# ══════════════════════════════════════════════════════════════════════════

@router.post("/research/seed", status_code=201, tags=["Research"])
def seed_research(
    payload: dict = Depends(require_admin),  # admin only
):
    """Bulk-seed all research articles. Safe to re-run."""
    seeded = []
    for art in RESEARCH_ARTICLES_SEED:
        doc = {**art, "created_at": _now(), "updated_at": _now()}
        try:
            db.collection("research_articles").document(art["id"]).set(doc, merge=True)
            seeded.append(art["id"])
        except Exception as e:
            log.error("seed_research failed for %s: %s", art["id"], e)
    try:
        db.collection("research_fields").document("meta").set({
            "fields": FIELDS_SEED, "field_icons": FIELD_ICONS_SEED,
            "field_colors": FIELD_COLORS_SEED, "updated_at": _now(),
        })
    except Exception as e:
        log.warning("Could not save research_fields meta: %s", e)
    log.info("Research articles seeded: %d", len(seeded))
    return {"seeded": len(seeded), "ids": seeded}


@router.post("/research/articles", status_code=201, tags=["Research"])
def create_research_article(
    body: CreateResearchArticleBody,
    payload: dict = Depends(require_mod),  # mod or admin
):
    """Create a single research article."""
    if body.field not in FIELDS_SEED:
        raise HTTPException(400, detail=f"field must be one of: {', '.join(FIELDS_SEED)}")
    doc = {**body.model_dump(), "created_at": _now(), "updated_at": _now()}
    try:
        db.collection("research_articles").document(body.id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return doc


@router.get("/research/articles", tags=["Research"])
def list_research_articles(field: str = Query("")):
    """
    List all research articles. Optional ?field= filter.
    Response mirrors research-articles.ts export exactly — includes fields meta.
    """
    field_filter = field.strip().upper()
    try:
        q    = db.collection("research_articles").order_by("date", direction="DESCENDING")
        docs = q.limit(100).stream()
        articles = []
        for d in docs:
            art = d.to_dict()
            if field_filter and art.get("field", "").upper() != field_filter:
                continue
            articles.append({
                "id":            art.get("id"),
                "title":         art.get("title"),
                "abstract":      art.get("abstract"),
                "field":         art.get("field"),
                "author":        art.get("author"),
                "date":          art.get("date"),
                "readTime":      art.get("readTime"),
                "content":       art.get("content", []),
                "quotes":        art.get("quotes", []),
                "keyFindings":   art.get("keyFindings", []),
                "relatedTopics": art.get("relatedTopics", []),
            })
    except Exception as e:
        log.error("list_research_articles failed: %s", e)
        raise HTTPException(500, detail=str(e))
    return {
        "articles":     articles,
        "total":        len(articles),
        "fields":       FIELDS_SEED,
        "field_icons":  FIELD_ICONS_SEED,
        "field_colors": FIELD_COLORS_SEED,
    }


@router.get("/research/articles/{article_id}", tags=["Research"])
def get_research_article(article_id: str):
    """Get single Article — same shape as one item in research-articles.ts."""
    doc = db.collection("research_articles").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Research article not found")
    art = doc.to_dict()
    return {
        "id":            art.get("id"),
        "title":         art.get("title"),
        "abstract":      art.get("abstract"),
        "field":         art.get("field"),
        "author":        art.get("author"),
        "date":          art.get("date"),
        "readTime":      art.get("readTime"),
        "content":       art.get("content", []),
        "quotes":        art.get("quotes", []),
        "keyFindings":   art.get("keyFindings", []),
        "relatedTopics": art.get("relatedTopics", []),
    }


@router.put("/research/articles/{article_id}", tags=["Research"])
def update_research_article(
    article_id: str,
    body: UpdateResearchArticleBody,
    payload: dict = Depends(require_mod),  # mod or admin
):
    """Update a research article's fields."""
    doc_ref = db.collection("research_articles").document(article_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Research article not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    updates["updated_at"] = _now()
    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"updated": True, "id": article_id}


@router.delete("/research/articles/{article_id}", tags=["Research"])
def delete_research_article(
    article_id: str,
    payload: dict = Depends(require_admin),  # admin only
):
    """Delete a research article."""
    doc_ref = db.collection("research_articles").document(article_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Research article not found")
    doc_ref.delete()
    return {"deleted": True, "id": article_id}