"""
routers/content.py  —  Explainers & Research Articles  v8
===========================================================
Images are stored on disk and served via FastAPI StaticFiles.
Frontend accesses them at:  http://localhost:5000/images/explainers/quantum-dog.jpg
                             http://localhost:5000/images/research/physics.jpg

THREE WAYS TO ATTACH AN IMAGE:
  A) Create via JSON + separate image upload (two requests):
       1. POST /api/explainers         { id, title, ... }   → creates doc, image=""
       2. POST /api/explainers/{id}/image  multipart file    → saves file, updates "image" in MongoDB

  B) Upload image first, then create (two requests):
       1. POST /api/images/upload  multipart file, folder="explainers"
                                   → returns { url: "/images/explainers/my-file.jpg" }
       2. POST /api/explainers     { id, title, ..., image: "/images/explainers/my-file.jpg" }

  C) Create with image in one multipart request (one request):
       POST /api/explainers/create-with-image
         Form fields: id, title, subtitle, field, badgeColor, readTime, content (JSON str), keyInsights (JSON str)
         File field:  image
       → saves file + creates MongoDB doc in one shot

ENDPOINTS:
  Generic upload
    POST   /api/images/upload                             — upload file, get URL back (mod/admin)

  Explainers
    POST   /api/explainers/seed                           — bulk seed (admin)
    POST   /api/explainers/create-with-image              — multipart create+upload (mod/admin)
    POST   /api/explainers                                — JSON create (mod/admin)
    GET    /api/explainers                                — list (public)
    GET    /api/explainers/{id}                           — get one (public)
    PUT    /api/explainers/{id}                           — JSON update (mod/admin)
    DELETE /api/explainers/{id}                           — delete (admin)
    POST   /api/explainers/{id}/image                     — upload/replace image (mod/admin)

  Research
    POST   /api/research/seed                             — bulk seed (admin)
    POST   /api/research/articles/create-with-image       — multipart create+upload (mod/admin)
    POST   /api/research/articles                         — JSON create (mod/admin)
    GET    /api/research/articles                         — list (public)
    GET    /api/research/articles/{id}                    — get one (public)
    PUT    /api/research/articles/{id}                    — JSON update (mod/admin)
    DELETE /api/research/articles/{id}                    — delete (admin)
    POST   /api/research/articles/{id}/image              — upload/replace image (mod/admin)
    GET    /api/research/images                           — field → image URL map (public)
    GET    /api/research/fields                           — fields meta (public)
"""

import os
import json
import shutil
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends, UploadFile, File, Form
from pydantic import BaseModel

from mongodb_client import db
from auth import require_mod, require_admin, get_uid
from content_data import (
    EXPLAINERS_SEED,
    RESEARCH_ARTICLES_SEED,
    FIELDS_SEED,
    FIELD_ICONS_SEED,
    FIELD_COLORS_SEED,
    FIELD_IMAGES_SEED,
)

log    = logging.getLogger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# IMAGE STORAGE CONFIG
# Must match the StaticFiles mount in main.py:
#   app.mount("/images", StaticFiles(directory="images"), name="images")
# This makes files stored in  images/research/physics.jpg
# accessible at               http://host:5000/images/research/physics.jpg
# ─────────────────────────────────────────────────────────────────────────────

IMAGES_ROOT  = "images"                        # disk directory (project root)
ALLOWED_MIME = {                               # accepted file types
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}
ALLOWED_EXT  = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    """Current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    """Create image sub-directories if they don't already exist."""
    os.makedirs(os.path.join(IMAGES_ROOT, "explainers"), exist_ok=True)
    os.makedirs(os.path.join(IMAGES_ROOT, "research"),   exist_ok=True)


def _save_file(upload: UploadFile, folder: str, filename: str) -> str:
    """
    Save an UploadFile to disk at images/<folder>/<filename>.
    Returns the public URL path: /images/<folder>/<filename>

    Args:
        upload:   FastAPI UploadFile object from the multipart form.
        folder:   Sub-directory — must be "explainers" or "research".
        filename: Target filename including extension (e.g. "quantum-dog.jpg").

    Raises:
        HTTPException(400) for invalid file type or extension.
        HTTPException(500) if the write fails.
    """
    _ensure_dirs()

    # Validate MIME type
    if upload.content_type and upload.content_type not in ALLOWED_MIME:
        raise HTTPException(
            400,
            detail=f"File type '{upload.content_type}' not allowed. Use JPEG, PNG, WebP, or GIF."
        )

    # Validate extension from filename
    ext = os.path.splitext(filename)[-1].lower()
    if ext not in ALLOWED_EXT:
        # Try to extract extension from the uploaded file's original name
        orig_ext = os.path.splitext(upload.filename or "")[-1].lower()
        if orig_ext in ALLOWED_EXT:
            filename = filename + orig_ext
            ext      = orig_ext
        else:
            raise HTTPException(
                400,
                detail=f"Invalid image extension '{ext}'. Allowed: {', '.join(ALLOWED_EXT)}"
            )

    # Build the full disk path and write the file
    dest_dir  = os.path.join(IMAGES_ROOT, folder)
    dest_path = os.path.join(dest_dir, filename)

    try:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        log.info("Image saved: %s → %s", upload.filename, dest_path)
    except Exception as e:
        log.error("Image save failed for %s: %s", dest_path, e)
        raise HTTPException(500, detail=f"Failed to save image: {e}")

    # Return the public URL path (matches the StaticFiles mount point)
    return f"/images/{folder}/{filename}"


def _fmt_explainer(ex: dict) -> dict:
    """Return a clean explainer dict safe to send to the frontend."""
    return {
        "id":          ex.get("id"),
        "title":       ex.get("title"),
        "subtitle":    ex.get("subtitle"),
        "field":       ex.get("field"),
        "badgeColor":  ex.get("badgeColor"),
        "readTime":    ex.get("readTime"),
        "image":       ex.get("image", ""),     # /images/explainers/quantum-dog.jpg
        "content":     ex.get("content", []),
        "keyInsights": ex.get("keyInsights", []),
    }


def _fmt_article(art: dict) -> dict:
    """Return a clean research article dict safe to send to the frontend."""
    return {
        "id":            art.get("id"),
        "title":         art.get("title"),
        "abstract":      art.get("abstract"),
        "field":         art.get("field"),
        "author":        art.get("author"),
        "date":          art.get("date"),
        "readTime":      art.get("readTime"),
        "image":         art.get("image", ""),  # /images/research/physics.jpg
        "content":       art.get("content", []),
        "quotes":        art.get("quotes", []),
        "keyFindings":   art.get("keyFindings", []),
        "relatedTopics": art.get("relatedTopics", []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────────────

class CreateExplainerBody(BaseModel):
    """
    JSON body for POST /api/explainers.
    Pass image as URL returned by POST /api/images/upload, or leave empty
    and upload later via POST /api/explainers/{id}/image.
    """
    id:          str
    title:       str
    subtitle:    str       = ""
    field:       str       = ""
    badgeColor:  str       = ""
    readTime:    str       = ""
    image:       str       = ""   # e.g. /images/explainers/quantum-dog.jpg
    content:     list      = []
    keyInsights: list      = []


class UpdateExplainerBody(BaseModel):
    """All fields optional — only provided fields are updated."""
    title:       Optional[str]  = None
    subtitle:    Optional[str]  = None
    field:       Optional[str]  = None
    badgeColor:  Optional[str]  = None
    readTime:    Optional[str]  = None
    image:       Optional[str]  = None  # set to new URL to replace image
    content:     Optional[list] = None
    keyInsights: Optional[list] = None


class CreateResearchArticleBody(BaseModel):
    id:            str
    title:         str
    abstract:      str  = ""
    field:         str
    author:        str  = ""
    date:          str  = ""
    readTime:      str  = ""
    image:         str  = ""  # e.g. /images/research/physics.jpg
    content:       list = []
    quotes:        list = []
    keyFindings:   list = []
    relatedTopics: list = []


class UpdateResearchArticleBody(BaseModel):
    title:         Optional[str]  = None
    abstract:      Optional[str]  = None
    field:         Optional[str]  = None
    author:        Optional[str]  = None
    date:          Optional[str]  = None
    readTime:      Optional[str]  = None
    image:         Optional[str]  = None
    content:       Optional[list] = None
    quotes:        Optional[list] = None
    keyFindings:   Optional[list] = None
    relatedTopics: Optional[list] = None


# ══════════════════════════════════════════════════════════════════════════════
# GENERIC IMAGE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/images/upload", tags=["Images"])
async def upload_image(
    file:    UploadFile = File(..., description="Image file (JPEG / PNG / WebP / GIF)"),
    folder:  str        = Query(
        "research",
        description="Sub-folder to save into: 'research' or 'explainers'"
    ),
    payload: dict       = Depends(require_mod),   # mod or admin only
):
    """
    POST /api/images/upload?folder=explainers
    Upload any image to the server's images/<folder>/ directory.
    The returned `url` value is what you store in the MongoDB `image` field
    and use as the `<img src>` value in the frontend.

    Allowed folders: research | explainers
    Allowed types:   JPEG, PNG, WebP, GIF

    Response:
    {
      "url":      "/images/explainers/my-photo.jpg",
      "filename": "my-photo.jpg",
      "folder":   "explainers"
    }

    curl -X POST "http://127.0.0.1:5000/api/images/upload?folder=explainers" \\
      -H "Authorization: Bearer <mod_token>" \\
      -F "file=@/path/to/image.jpg"
    """
    if folder not in ("research", "explainers"):
        raise HTTPException(400, detail="folder must be 'research' or 'explainers'")

    # Use the original filename from the upload, strip path separators for safety
    safe_name = os.path.basename(file.filename or "upload.jpg")
    image_url = _save_file(file, folder, safe_name)

    log.info("Generic upload: %s → %s by uid=%s", safe_name, image_url, get_uid(payload))
    return {
        "url":      image_url,    # store this in MongoDB's "image" field
        "filename": safe_name,
        "folder":   folder,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXPLAINERS — SEED
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/explainers/seed", status_code=201, tags=["Explainers"])
def seed_explainers(payload: dict = Depends(require_admin)):
    """
    POST /api/explainers/seed
    Bulk-seed all 9 explainers from content_data.py.
    Safe to re-run (uses MongoDB upsert — never duplicates).
    Image URLs in seed data point to files that must exist in images/explainers/.

    curl -X POST http://127.0.0.1:5000/api/explainers/seed \\
      -H "Authorization: Bearer <admin_token>"
    """
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


# ══════════════════════════════════════════════════════════════════════════════
# EXPLAINERS — CREATE WITH IMAGE (one multipart request)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/explainers/create-with-image", status_code=201, tags=["Explainers"])
async def create_explainer_with_image(
    # ── Image file ────────────────────────────────────────────────────────
    image:       UploadFile = File(..., description="Hero image file for this explainer"),
    # ── Required text fields as Form fields ───────────────────────────────
    id:          str        = Form(...,  description="Unique ID e.g. 'quantum-dog'"),
    title:       str        = Form(...,  description="Explainer title"),
    # ── Optional text fields ──────────────────────────────────────────────
    subtitle:    str        = Form("",   description="Short subtitle / teaser"),
    field:       str        = Form("",   description="STEM field e.g. 'QUANTUM PHYSICS'"),
    badgeColor:  str        = Form("",   description="Badge colour name e.g. 'cyan'"),
    readTime:    str        = Form("",   description="e.g. '8 MIN READ'"),
    # ── JSON-encoded arrays (pass as JSON strings) ────────────────────────
    content:     str        = Form("[]", description='JSON array of paragraph strings'),
    keyInsights: str        = Form("[]", description='JSON array of insight strings'),
    # ── Auth ──────────────────────────────────────────────────────────────
    payload:     dict       = Depends(require_mod),
):
    """
    POST /api/explainers/create-with-image  (multipart/form-data)
    Create a new explainer AND upload its image in a single request.

    The image is saved as  images/explainers/<id>.<ext>
    and the URL is stored automatically in the `image` field of the new MongoDB doc.

    Form fields:
      image        — file upload (required)
      id           — unique string ID, e.g. "quantum-dog"  (required)
      title        — explainer title  (required)
      subtitle     — short subtitle
      field        — STEM field, e.g. "QUANTUM PHYSICS"
      badgeColor   — e.g. "cyan", "green", "violet"
      readTime     — e.g. "8 MIN READ"
      content      — JSON string: '["Para 1...", "Para 2..."]'
      keyInsights  — JSON string: '["Insight 1...", "Insight 2..."]'

    Example curl:
      curl -X POST http://127.0.0.1:5000/api/explainers/create-with-image \\
        -H "Authorization: Bearer <mod_token>" \\
        -F "image=@/path/to/quantum-dog.jpg" \\
        -F "id=quantum-dog" \\
        -F "title=The Quantum Dog" \\
        -F 'content=["Paragraph one...", "Paragraph two..."]' \\
        -F 'keyInsights=["Insight one", "Insight two"]'
    """
    # Parse JSON arrays (sent as form strings)
    try:
        content_list     = json.loads(content)
        keyInsights_list = json.loads(keyInsights)
    except json.JSONDecodeError as e:
        raise HTTPException(400, detail=f"Invalid JSON in content or keyInsights: {e}")

    # Determine image filename: use the explainer ID as the base name
    ext       = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename  = f"{id}{ext}"

    # Save the image to disk
    image_url = _save_file(image, "explainers", filename)

    # Build and save the MongoDB document
    doc = {
        "id":          id,
        "title":       title,
        "subtitle":    subtitle,
        "field":       field,
        "badgeColor":  badgeColor,
        "readTime":    readTime,
        "image":       image_url,       # automatically set from the upload
        "content":     content_list,
        "keyInsights": keyInsights_list,
        "created_at":  _now(),
        "updated_at":  _now(),
    }

    try:
        db.collection("explainers").document(id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("Explainer created with image: %s → %s by %s", id, image_url, get_uid(payload))
    return _fmt_explainer(doc)


# ══════════════════════════════════════════════════════════════════════════════
# EXPLAINERS — STANDARD JSON CREATE / READ / UPDATE / DELETE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/explainers", status_code=201, tags=["Explainers"])
def create_explainer(
    body:    CreateExplainerBody,
    payload: dict = Depends(require_mod),
):
    """
    POST /api/explainers  (JSON body)
    Create a new explainer.

    Pass `image` as the URL returned by POST /api/images/upload,
    or leave it empty and call POST /api/explainers/{id}/image afterwards.

    curl -X POST http://127.0.0.1:5000/api/explainers \\
      -H "Authorization: Bearer <mod_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"id":"my-explainer","title":"My Title","field":"AI","image":"/images/explainers/my.jpg"}'
    """
    doc = {**body.model_dump(), "created_at": _now(), "updated_at": _now()}
    try:
        db.collection("explainers").document(body.id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("Explainer created: %s by %s", body.id, get_uid(payload))
    return _fmt_explainer(doc)


@router.get("/explainers", tags=["Explainers"])
def list_explainers(field: str = Query("", description="Filter by field e.g. BIOLOGY")):
    """
    GET /api/explainers?field=BIOLOGY
    List all explainers. Optional field filter. Each item includes the image URL.
    No token required.

    curl http://127.0.0.1:5000/api/explainers
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
            explainers.append(_fmt_explainer(ex))
    except Exception as e:
        log.error("list_explainers: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"explainers": explainers, "total": len(explainers)}


@router.get("/explainers/{explainer_id}", tags=["Explainers"])
def get_explainer(explainer_id: str):
    """
    GET /api/explainers/{id}
    Get a single explainer including its image URL.

    curl http://127.0.0.1:5000/api/explainers/quantum-dog
    """
    doc = db.collection("explainers").document(explainer_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Explainer not found")
    return _fmt_explainer(doc.to_dict())


@router.put("/explainers/{explainer_id}", tags=["Explainers"])
def update_explainer(
    explainer_id: str,
    body:         UpdateExplainerBody,
    payload:      dict = Depends(require_mod),
):
    """
    PUT /api/explainers/{id}
    Update explainer fields. Pass new image URL to change the image.
    All fields are optional — only provided fields are updated.

    curl -X PUT http://127.0.0.1:5000/api/explainers/quantum-dog \\
      -H "Authorization: Bearer <mod_token>" \\
      -d '{"readTime":"10 MIN READ","image":"/images/explainers/new-quantum.jpg"}'
    """
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
    payload:      dict = Depends(require_admin),
):
    """
    DELETE /api/explainers/{id}
    Permanently delete an explainer. Admin only.

    curl -X DELETE http://127.0.0.1:5000/api/explainers/quantum-dog \\
      -H "Authorization: Bearer <admin_token>"
    """
    doc_ref = db.collection("explainers").document(explainer_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Explainer not found")
    doc_ref.delete()
    log.info("Explainer deleted: %s by %s", explainer_id, get_uid(payload))
    return {"deleted": True, "id": explainer_id}


@router.post("/explainers/{explainer_id}/image", tags=["Explainers"])
async def upload_explainer_image(
    explainer_id: str,
    image:        UploadFile = File(..., description="New image file for this explainer"),
    payload:      dict       = Depends(require_mod),
):
    """
    POST /api/explainers/{id}/image  (multipart/form-data)
    Upload or replace the image for an existing explainer.

    The file is saved as  images/explainers/<explainer_id>.<ext>
    and the `image` field in MongoDB is updated automatically.

    Response:
    {
      "updated":   true,
      "id":        "quantum-dog",
      "image":     "/images/explainers/quantum-dog.jpg"
    }

    curl -X POST http://127.0.0.1:5000/api/explainers/quantum-dog/image \\
      -H "Authorization: Bearer <mod_token>" \\
      -F "image=@/path/to/quantum-dog.jpg"
    """
    # Verify the explainer exists before saving the file
    doc_ref = db.collection("explainers").document(explainer_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Explainer not found")

    # Use the explainer ID as the base filename to keep things consistent
    ext      = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename = f"{explainer_id}{ext}"

    # Save the file
    image_url = _save_file(image, "explainers", filename)

    # Update only the image field in MongoDB
    try:
        doc_ref.update({"image": image_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Image saved to disk but DB update failed: {e}")

    log.info("Explainer image updated: %s → %s by %s", explainer_id, image_url, get_uid(payload))
    return {"updated": True, "id": explainer_id, "image": image_url}


# ══════════════════════════════════════════════════════════════════════════════
# RESEARCH FIELDS META
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/research/fields", tags=["Research"])
def get_fields():
    """
    GET /api/research/fields
    Returns fields list, icons, badge colors, and field image URLs.
    Frontend can use field_images to replace hardcoded research-images.ts.

    curl http://127.0.0.1:5000/api/research/fields
    """
    return {
        "fields":       FIELDS_SEED,
        "field_icons":  FIELD_ICONS_SEED,
        "field_colors": FIELD_COLORS_SEED,
        "field_images": FIELD_IMAGES_SEED,   # { "PHYSICS": "/images/research/physics.jpg", ... }
    }


@router.get("/research/images", tags=["Research"])
def get_research_images():
    """
    GET /api/research/images
    Returns the complete field → image URL mapping.
    Use this to replace the hardcoded research-images.ts import.

    curl http://127.0.0.1:5000/api/research/images
    """
    return {"images": FIELD_IMAGES_SEED}


# ══════════════════════════════════════════════════════════════════════════════
# RESEARCH ARTICLES — SEED
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/research/seed", status_code=201, tags=["Research"])
def seed_research(payload: dict = Depends(require_admin)):
    """
    POST /api/research/seed
    Bulk-seed all 11 research articles from content_data.py.
    Safe to re-run.

    curl -X POST http://127.0.0.1:5000/api/research/seed \\
      -H "Authorization: Bearer <admin_token>"
    """
    seeded = []
    for art in RESEARCH_ARTICLES_SEED:
        doc = {**art, "created_at": _now(), "updated_at": _now()}
        try:
            db.collection("research_articles").document(art["id"]).set(doc, merge=True)
            seeded.append(art["id"])
        except Exception as e:
            log.error("seed_research failed for %s: %s", art["id"], e)

    # Save fields meta
    try:
        db.collection("research_fields").document("meta").set({
            "fields":       FIELDS_SEED,
            "field_icons":  FIELD_ICONS_SEED,
            "field_colors": FIELD_COLORS_SEED,
            "field_images": FIELD_IMAGES_SEED,
            "updated_at":   _now(),
        })
    except Exception as e:
        log.warning("Could not save research_fields meta: %s", e)

    log.info("Research articles seeded: %d", len(seeded))
    return {"seeded": len(seeded), "ids": seeded}


# ══════════════════════════════════════════════════════════════════════════════
# RESEARCH ARTICLES — CREATE WITH IMAGE (one multipart request)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/research/articles/create-with-image", status_code=201, tags=["Research"])
async def create_research_with_image(
    # ── Image file ────────────────────────────────────────────────────────
    image:         UploadFile = File(..., description="Hero image for this research article"),
    # ── Required fields ───────────────────────────────────────────────────
    id:            str        = Form(...,   description="Unique ID e.g. 'a1'"),
    title:         str        = Form(...,   description="Article title"),
    field:         str        = Form(...,   description="Research field e.g. 'PHYSICS'"),
    # ── Optional fields ───────────────────────────────────────────────────
    abstract:      str        = Form("",   description="Short abstract"),
    author:        str        = Form("",   description="Author name"),
    date:          str        = Form("",   description="Publication date YYYY-MM-DD"),
    readTime:      str        = Form("",   description="e.g. '10 min'"),
    content:       str        = Form("[]", description='JSON array of paragraph strings'),
    quotes:        str        = Form("[]", description='JSON array of quote strings'),
    keyFindings:   str        = Form("[]", description='JSON array of key finding strings'),
    relatedTopics: str        = Form("[]", description='JSON array of topic strings'),
    # ── Auth ──────────────────────────────────────────────────────────────
    payload:       dict       = Depends(require_mod),
):
    """
    POST /api/research/articles/create-with-image  (multipart/form-data)
    Create a new research article AND upload its image in one request.

    For research articles, images are shared per field.
    The image is saved as  images/research/<field-slug>.<ext>
    where field-slug converts "EARTH & SPACE" → "earth-space".

    Form fields:
      image          — file upload (required)
      id             — unique string ID  (required)
      title          — article title  (required)
      field          — research field e.g. "PHYSICS"  (required)
      abstract       — short abstract
      author         — author name
      date           — YYYY-MM-DD
      readTime       — e.g. "10 min"
      content        — JSON string: '["Para 1...", "Para 2..."]'
      quotes         — JSON string: '["Quote 1...", "Quote 2..."]'
      keyFindings    — JSON string: '["Finding 1...", "Finding 2..."]'
      relatedTopics  — JSON string: '["Topic 1...", "Topic 2..."]'

    Example curl:
      curl -X POST http://127.0.0.1:5000/api/research/articles/create-with-image \\
        -H "Authorization: Bearer <mod_token>" \\
        -F "image=@/path/to/physics.jpg" \\
        -F "id=a99" \\
        -F "title=New Physics Discovery" \\
        -F "field=PHYSICS" \\
        -F 'content=["Para 1...", "Para 2..."]'
    """
    # Validate field value
    if field not in FIELDS_SEED:
        raise HTTPException(400, detail=f"field must be one of: {', '.join(FIELDS_SEED)}")

    # Parse JSON arrays
    try:
        content_list       = json.loads(content)
        quotes_list        = json.loads(quotes)
        keyFindings_list   = json.loads(keyFindings)
        relatedTopics_list = json.loads(relatedTopics)
    except json.JSONDecodeError as e:
        raise HTTPException(400, detail=f"Invalid JSON in array field: {e}")

    # Build a URL-safe filename from the field name
    # "EARTH & SPACE" → "earth-space"
    field_slug = field.lower().replace(" & ", "-").replace(" ", "-").replace("&", "")
    ext        = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename   = f"{field_slug}{ext}"

    # Save image to disk
    image_url = _save_file(image, "research", filename)

    # Build and save the MongoDB document
    doc = {
        "id":            id,
        "title":         title,
        "abstract":      abstract,
        "field":         field,
        "author":        author,
        "date":          date,
        "readTime":      readTime,
        "image":         image_url,        # auto-set from upload
        "content":       content_list,
        "quotes":        quotes_list,
        "keyFindings":   keyFindings_list,
        "relatedTopics": relatedTopics_list,
        "created_at":    _now(),
        "updated_at":    _now(),
    }

    try:
        db.collection("research_articles").document(id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("Research article created with image: %s → %s by %s",
             id, image_url, get_uid(payload))
    return _fmt_article(doc)


# ══════════════════════════════════════════════════════════════════════════════
# RESEARCH ARTICLES — STANDARD JSON CRUD
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/research/articles", status_code=201, tags=["Research"])
def create_research_article(
    body:    CreateResearchArticleBody,
    payload: dict = Depends(require_mod),
):
    """
    POST /api/research/articles  (JSON body)
    Create a research article.

    curl -X POST http://127.0.0.1:5000/api/research/articles \\
      -H "Authorization: Bearer <mod_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"id":"a99","title":"New Discovery","field":"PHYSICS","image":"/images/research/physics.jpg"}'
    """
    if body.field not in FIELDS_SEED:
        raise HTTPException(400, detail=f"field must be one of: {', '.join(FIELDS_SEED)}")
    doc = {**body.model_dump(), "created_at": _now(), "updated_at": _now()}
    try:
        db.collection("research_articles").document(body.id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return _fmt_article(doc)


@router.get("/research/articles", tags=["Research"])
def list_research_articles(field: str = Query("", description="Filter by field")):
    """
    GET /api/research/articles?field=PHYSICS
    List all research articles. Each item includes image URL.
    Response also includes field_images map.

    curl http://127.0.0.1:5000/api/research/articles
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
            articles.append(_fmt_article(art))
    except Exception as e:
        log.error("list_research_articles: %s", e)
        raise HTTPException(500, detail=str(e))

    return {
        "articles":     articles,
        "total":        len(articles),
        "fields":       FIELDS_SEED,
        "field_icons":  FIELD_ICONS_SEED,
        "field_colors": FIELD_COLORS_SEED,
        "field_images": FIELD_IMAGES_SEED,
    }


@router.get("/research/articles/{article_id}", tags=["Research"])
def get_research_article(article_id: str):
    """
    GET /api/research/articles/{id}

    curl http://127.0.0.1:5000/api/research/articles/a1
    """
    doc = db.collection("research_articles").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Research article not found")
    return _fmt_article(doc.to_dict())


@router.put("/research/articles/{article_id}", tags=["Research"])
def update_research_article(
    article_id: str,
    body:       UpdateResearchArticleBody,
    payload:    dict = Depends(require_mod),
):
    """
    PUT /api/research/articles/{id}
    Update fields. Pass new image URL to change the image.

    curl -X PUT http://127.0.0.1:5000/api/research/articles/a1 \\
      -H "Authorization: Bearer <mod_token>" \\
      -d '{"readTime":"15 min"}'
    """
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
    payload:    dict = Depends(require_admin),
):
    """
    DELETE /api/research/articles/{id}

    curl -X DELETE http://127.0.0.1:5000/api/research/articles/a1 \\
      -H "Authorization: Bearer <admin_token>"
    """
    doc_ref = db.collection("research_articles").document(article_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Research article not found")
    doc_ref.delete()
    log.info("Research article deleted: %s by %s", article_id, get_uid(payload))
    return {"deleted": True, "id": article_id}


@router.post("/research/articles/{article_id}/image", tags=["Research"])
async def upload_research_image(
    article_id: str,
    image:      UploadFile = File(..., description="New image file"),
    payload:    dict       = Depends(require_mod),
):
    """
    POST /api/research/articles/{id}/image  (multipart/form-data)
    Upload or replace the image for a research article.

    Research images are shared per field — when you upload a new image for
    article a1 (field=PHYSICS), all other PHYSICS articles get the same image URL.
    The file is saved as  images/research/<field-slug>.<ext>

    Response:
    {
      "updated":     true,
      "id":          "a1",
      "image":       "/images/research/physics.jpg",
      "field":       "PHYSICS",
      "also_updated": ["a6"]
    }

    curl -X POST http://127.0.0.1:5000/api/research/articles/a1/image \\
      -H "Authorization: Bearer <mod_token>" \\
      -F "image=@/path/to/physics.jpg"
    """
    doc_ref = db.collection("research_articles").document(article_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Research article not found")

    field      = doc.to_dict().get("field", "")
    field_slug = field.lower().replace(" & ", "-").replace(" ", "-").replace("&", "")
    ext        = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename   = f"{field_slug}{ext}"

    # Save to disk
    image_url = _save_file(image, "research", filename)

    # Update this article
    try:
        doc_ref.update({"image": image_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Image saved but DB update failed: {e}")

    # Update ALL other articles with the same field to share the image
    also_updated = []
    try:
        siblings = (
            db.collection("research_articles")
              .where("field", "==", field)
              .stream()
        )
        for s in siblings:
            if s.id != article_id:
                db.collection("research_articles").document(s.id).update({
                    "image":      image_url,
                    "updated_at": _now(),
                })
                also_updated.append(s.id)
    except Exception as e:
        log.warning("Could not update sibling articles for field %s: %s", field, e)

    log.info("Research image uploaded: field=%s → %s by %s (also updated %s)",
             field, image_url, get_uid(payload), also_updated)
    return {
        "updated":      True,
        "id":           article_id,
        "image":        image_url,
        "field":        field,
        "also_updated": also_updated,
    }