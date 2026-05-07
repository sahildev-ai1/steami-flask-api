"""
routers/content.py  —  Explainers, Research Articles & Blog Posts  v9
======================================================================
Images are stored on disk and served via FastAPI StaticFiles.
Frontend accesses them at:  http://localhost:5000/images/explainers/quantum-dog.jpg
                             http://localhost:5000/images/research/physics.jpg

THREE WAYS TO ATTACH AN IMAGE (explainers & research articles):
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

  Blog Posts
    POST   /api/blog/seed                                 — bulk seed from content_data (admin)
    POST   /api/blog                                      — JSON create (mod/admin)
    GET    /api/blog                                      — list, filter by field/type/tag (public)
    GET    /api/blog/{id}                                 — get one (public)
    PUT    /api/blog/{id}                                 — JSON update (mod/admin)
    DELETE /api/blog/{id}                                 — delete (admin)
    POST   /api/blog/{id}/cover-image                     — upload/replace cover image (mod/admin)

  CMS — edit helpers (mod/admin)
    GET    /api/cms/explainers                            — list slim for CMS table
    GET    /api/cms/explainers/{id}                       — full doc ready for edit form
    GET    /api/cms/research                              — list slim for CMS table
    GET    /api/cms/research/{id}                         — full doc ready for edit form
    GET    /api/cms/blog                                  — list slim for CMS table
    GET    /api/cms/blog/{id}                             — full doc ready for edit form
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
    BLOG_POSTS_SEED,
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

_BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_ROOT = os.path.join(_BASE_DIR, "images")                  # disk directory (project root)
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
    os.makedirs(os.path.join(IMAGES_ROOT, "blog"),       exist_ok=True)


def _delete_file(url_path: str) -> bool:
    """
    Delete an image from disk given its public URL path (e.g. /images/explainers/quantum-dog.jpg).

    - Only deletes files that live inside IMAGES_ROOT (safe against path traversal).
    - Silently skips external URLs (http/https), empty strings, and missing files.
    - Returns True if the file was actually deleted, False otherwise.
    - Never raises — failures are logged as warnings so the calling request still succeeds.
    """
    if not url_path or url_path.startswith(("http://", "https://")):
        return False   # external URL — nothing to clean up on disk

    # Strip leading slash and build absolute path
    rel  = url_path.lstrip("/")            # e.g.  images/explainers/quantum-dog.jpg
    full = os.path.normpath(os.path.join(_BASE_DIR, rel))

    # Safety check — must stay inside IMAGES_ROOT
    if not full.startswith(os.path.normpath(IMAGES_ROOT)):
        log.warning("_delete_file: refused to delete outside IMAGES_ROOT: %s", full)
        return False

    if not os.path.isfile(full):
        return False   # already gone or never existed

    try:
        os.remove(full)
        log.info("_delete_file: removed %s", full)
        return True
    except OSError as exc:
        log.warning("_delete_file: could not remove %s: %s", full, exc)
        return False


def _file_hash(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _find_duplicate(data: bytes, skip_path: str = "") -> "str | None":
    """
    Scan all image sub-directories for a file whose content matches *data*.
    Returns the public URL path of the first match found, or None.

    Allows mods to upload the same image for explainers, research articles,
    or blog posts without creating redundant copies on disk.

    Args:
        data:      Raw bytes of the uploaded image.
        skip_path: Absolute path to skip (e.g. the intended destination,
                   so we don't match the file we're about to overwrite).
    """
    target_hash = _file_hash(data)
    for sub in ("explainers", "research", "blog"):
        folder_path = os.path.join(IMAGES_ROOT, sub)
        if not os.path.isdir(folder_path):
            continue
        for fname in os.listdir(folder_path):
            fpath = os.path.join(folder_path, fname)
            if not os.path.isfile(fpath):
                continue
            if skip_path and os.path.normpath(fpath) == os.path.normpath(skip_path):
                continue
            try:
                with open(fpath, "rb") as fh:
                    if _file_hash(fh.read()) == target_hash:
                        return f"/images/{sub}/{fname}"
            except OSError:
                continue
    return None


def _save_file(upload: UploadFile, folder: str, filename: str) -> str:
    """
    Save an UploadFile to disk at images/<folder>/<filename>.

    Deduplication: before writing, the file bytes are hashed and compared
    against every existing image on disk (across explainers/, research/, and
    blog/).  If an identical file already exists anywhere, its existing URL
    is returned immediately — no new file is written.

    Returns the public URL path: /images/<folder>/<filename>

    Args:
        upload:   FastAPI UploadFile object from the multipart form.
        folder:   Sub-directory — must be "explainers", "research", or "blog".
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

    # Read all bytes so we can hash for deduplication
    try:
        data = upload.file.read()
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to read uploaded file: {e}")

    # Build the full disk path for the intended destination
    dest_dir  = os.path.join(IMAGES_ROOT, folder)
    dest_path = os.path.join(dest_dir, filename)

    # ── Deduplication check ─────────────────────────────────────────────────────────
    # Skip dest_path so we don't match the file we're replacing.
    existing_url = _find_duplicate(data, skip_path=dest_path)
    if existing_url:
        log.info(
            "Image deduplication: '%s' matches existing file → reusing %s",
            upload.filename, existing_url,
        )
        return existing_url
    # ───────────────────────────────────────────────────────────────────────────

    # Write the new file to disk
    try:
        with open(dest_path, "wb") as f:
            f.write(data)
        log.info("Image saved: %s → %s", upload.filename, dest_path)
    except Exception as e:
        log.error("Image save failed for %s: %s", dest_path, e)
        raise HTTPException(500, detail=f"Failed to save image: {e}")

    # Return the public URL path (matches the StaticFiles mount point)
    return f"/images/{folder}/{filename}"


def _fmt_explainer(ex: dict) -> dict:
    """Return a clean explainer dict safe to send to the frontend."""
    return {
        "id":              ex.get("id"),
        "title":           ex.get("title"),
        "subtitle":        ex.get("subtitle"),
        "field":           ex.get("field"),
        "badgeColor":      ex.get("badgeColor"),
        "readTime":        ex.get("readTime"),
        "author":          ex.get("author", ""),
        "image":           ex.get("image", ""),
        "content":         ex.get("content", []),
        "keyInsights":     ex.get("keyInsights", []),
        "context":         ex.get("context", ""),
        "technicalDetail": ex.get("technicalDetail", ""),
        "impact":          ex.get("impact", ""),
        "references":      ex.get("references", []),
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
    id:              str
    title:           str
    subtitle:        str  = ""
    field:           str  = ""
    badgeColor:      str  = ""
    readTime:        str  = ""
    author:          str  = ""
    image:           str  = ""   # e.g. /images/explainers/quantum-dog.jpg
    content:         list = []
    keyInsights:     list = []
    context:         str  = ""
    technicalDetail: str  = ""
    impact:          str  = ""
    references:      list = []   # list of {title, url?, author?, type?}


class UpdateExplainerBody(BaseModel):
    """All fields optional — only provided fields are updated."""
    title:           Optional[str]  = None
    subtitle:        Optional[str]  = None
    field:           Optional[str]  = None
    badgeColor:      Optional[str]  = None
    readTime:        Optional[str]  = None
    author:          Optional[str]  = None
    image:           Optional[str]  = None  # set to new URL to replace image
    content:         Optional[list] = None
    keyInsights:     Optional[list] = None
    context:         Optional[str]  = None
    technicalDetail: Optional[str]  = None
    impact:          Optional[str]  = None
    references:      Optional[list] = None   # list of {title, url?, author?, type?}


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

    Allowed folders: research | explainers | blog
    Deduplication: if the uploaded bytes match an existing file anywhere on disk,
    the existing URL is returned without creating a new file.
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
    if folder not in ("research", "explainers", "blog"):
        raise HTTPException(400, detail="folder must be 'research', 'explainers', or 'blog'")

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
    subtitle:        str        = Form("",   description="Short subtitle / teaser"),
    field:           str        = Form("",   description="STEM field e.g. 'QUANTUM PHYSICS'"),
    badgeColor:      str        = Form("",   description="Badge colour name e.g. 'cyan'"),
    readTime:        str        = Form("",   description="e.g. '8 MIN READ'"),
    author:          str        = Form("",   description="Author name"),
    context:         str        = Form("",   description="Historical context paragraph"),
    technicalDetail: str        = Form("",   description="Technical deep-dive paragraph"),
    impact:          str        = Form("",   description="Real-world impact paragraph"),
    # ── JSON-encoded arrays (pass as JSON strings) ────────────────────────
    content:         str        = Form("[]", description='JSON array of paragraph strings'),
    keyInsights:     str        = Form("[]", description='JSON array of insight strings'),
    references:      str        = Form("[]", description='JSON array of reference objects [{title, url?, author?, type?}]'),
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
        references_list  = json.loads(references)
    except json.JSONDecodeError as e:
        raise HTTPException(400, detail=f"Invalid JSON in content or keyInsights: {e}")

    # Determine image filename: use the explainer ID as the base name
    ext       = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename  = f"{id}{ext}"

    # Save the image to disk
    image_url = _save_file(image, "explainers", filename)

    # Build and save the MongoDB document
    doc = {
        "id":              id,
        "title":           title,
        "subtitle":        subtitle,
        "field":           field,
        "badgeColor":      badgeColor,
        "readTime":        readTime,
        "author":          author,
        "image":           image_url,
        "content":         content_list,
        "keyInsights":     keyInsights_list,
        "context":         context,
        "technicalDetail": technicalDetail,
        "impact":          impact,
        "references":      references_list,
        "created_at":      _now(),
        "updated_at":      _now(),
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
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Explainer not found")

    old_image_url = doc.to_dict().get("image", "")
    doc_ref.delete()

    # Remove the image file from disk
    deleted_file = _delete_file(old_image_url)

    log.info("Explainer deleted: %s by %s (image deleted=%s)",
             explainer_id, get_uid(payload), deleted_file)
    return {"deleted": True, "id": explainer_id, "image_deleted": deleted_file}


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
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Explainer not found")

    # Remember the current image URL so we can delete it after the new one is saved
    old_image_url = doc.to_dict().get("image", "")

    # Use the explainer ID as the base filename to keep things consistent
    ext      = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename = f"{explainer_id}{ext}"

    # Save the new file
    image_url = _save_file(image, "explainers", filename)

    # Update only the image field in MongoDB
    try:
        doc_ref.update({"image": image_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Image saved to disk but DB update failed: {e}")

    # Delete the old file from disk — only when it differs from the new path
    # (same-name replacement already overwrote the file, but different-ext old files must be cleaned up)
    deleted_old = False
    if old_image_url and old_image_url != image_url:
        deleted_old = _delete_file(old_image_url)

    log.info("Explainer image updated: %s → %s by %s (old deleted=%s)",
             explainer_id, image_url, get_uid(payload), deleted_old)
    return {
        "updated":     True,
        "id":          explainer_id,
        "image":       image_url,
        "deleted_old": deleted_old,
    }


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

    Each research article gets its own image file.
    The image is saved as  images/research/<id>.<ext>
    Deduplication is applied — identical bytes reuse the existing file.

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

    # Save image with the article ID as filename (one image per article)
    ext      = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename = f"{id}{ext}"

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
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Research article not found")

    art_data      = doc.to_dict()
    old_image_url = art_data.get("image", "")
    field         = art_data.get("field", "")

    doc_ref.delete()

    # Each research article owns its own image. Only delete the file from disk
    # if no other article still references the same URL (e.g. via deduplication).
    deleted_file = False
    if old_image_url:
        try:
            remaining = db.collection("research_articles").stream()
            still_used = any(
                r.to_dict().get("image") == old_image_url
                for r in remaining
            )
        except Exception:
            still_used = True   # assume still in use if we can't check — safer

        if not still_used:
            deleted_file = _delete_file(old_image_url)

    log.info("Research article deleted: %s by %s (image deleted=%s)",
             article_id, get_uid(payload), deleted_file)
    return {"deleted": True, "id": article_id, "image_deleted": deleted_file}


@router.post("/research/articles/{article_id}/image", tags=["Research"])
async def upload_research_image(
    article_id: str,
    image:      UploadFile = File(..., description="New image file"),
    payload:    dict       = Depends(require_mod),
):
    """
    POST /api/research/articles/{id}/image  (multipart/form-data)
    Upload or replace the image for a specific research article.

    Each research article has its own image (saved as images/research/<id>.<ext>).
    Uploading the same image bytes that already exist anywhere on disk will reuse
    the existing file — no duplicate is created.

    Response:
    {
      "updated":     true,
      "id":          "a1",
      "image":       "/images/research/a1.jpg",
      "deleted_old": false
    }

    curl -X POST http://127.0.0.1:5000/api/research/articles/a1/image \\
      -H "Authorization: Bearer <mod_token>" \\
      -F "image=@/path/to/a1.jpg"
    """
    doc_ref = db.collection("research_articles").document(article_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Research article not found")

    art_data      = doc.to_dict()
    old_image_url = art_data.get("image", "")

    # Each research article gets its own image file (named by article ID)
    ext      = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename = f"{article_id}{ext}"

    # Save new file to disk (deduplication applies automatically)
    image_url = _save_file(image, "research", filename)

    # Update this article's image field in the DB
    try:
        doc_ref.update({"image": image_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Image saved but DB update failed: {e}")

    # Delete the old file only if the path changed AND no other document still references it
    deleted_old = False
    if old_image_url and old_image_url != image_url:
        try:
            others = db.collection("research_articles").stream()
            still_used = any(
                o.to_dict().get("image") == old_image_url
                for o in others
            )
        except Exception:
            still_used = True   # assume in use if we can't check
        if not still_used:
            deleted_old = _delete_file(old_image_url)

    log.info("Research image uploaded: %s → %s by %s (old deleted=%s)",
             article_id, image_url, get_uid(payload), deleted_old)
    return {
        "updated":     True,
        "id":          article_id,
        "image":       image_url,
        "deleted_old": deleted_old,
    }

# ══════════════════════════════════════════════════════════════════════════════
# BLOG POSTS — PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class BlogAuthor(BaseModel):
    name:   str = ""
    role:   str = ""
    avatar: str = ""
    bio:    str = ""


class CreateBlogPostBody(BaseModel):
    """JSON body for POST /api/blog."""
    id:           str
    title:        str
    subtitle:     str        = ""
    description:  str        = ""
    author:       BlogAuthor = BlogAuthor()
    publishDate:  str        = ""
    readingTime:  str        = ""
    coverImage:   str        = ""   # URL — use POST /api/blog/{id}/cover-image for uploads
    field:        str        = ""
    badgeColor:   str        = ""
    tags:         list       = []
    keyInsights:  list       = []
    type:         str        = "article"   # "explainer" | "article" | "simulation"
    simulationUrl: str       = ""
    content:      str        = ""          # Markdown string


class UpdateBlogPostBody(BaseModel):
    """All fields optional — only provided fields are updated."""
    title:         Optional[str]        = None
    subtitle:      Optional[str]        = None
    description:   Optional[str]        = None
    author:        Optional[BlogAuthor] = None
    publishDate:   Optional[str]        = None
    readingTime:   Optional[str]        = None
    coverImage:    Optional[str]        = None
    field:         Optional[str]        = None
    badgeColor:    Optional[str]        = None
    tags:          Optional[list]       = None
    keyInsights:   Optional[list]       = None
    type:          Optional[str]        = None
    simulationUrl: Optional[str]        = None
    content:       Optional[str]        = None


def _fmt_blog(post: dict) -> dict:
    """Return a clean blog post dict safe to send to the frontend."""
    return {
        "id":            post.get("id"),
        "title":         post.get("title"),
        "subtitle":      post.get("subtitle", ""),
        "description":   post.get("description", ""),
        "author":        post.get("author", {}),
        "publishDate":   post.get("publishDate", ""),
        "readingTime":   post.get("readingTime", ""),
        "coverImage":    post.get("coverImage", ""),
        "field":         post.get("field", ""),
        "badgeColor":    post.get("badgeColor", ""),
        "tags":          post.get("tags", []),
        "keyInsights":   post.get("keyInsights", []),
        "type":          post.get("type", "article"),
        "simulationUrl": post.get("simulationUrl", ""),
        "content":       post.get("content", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# BLOG POSTS — SEED
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/blog/seed", status_code=201, tags=["Blog"])
def seed_blog_posts(payload: dict = Depends(require_admin)):
    """
    POST /api/blog/seed
    ADMIN ONLY — bulk-seed all blog posts from content_data.BLOG_POSTS_SEED.
    Safe to re-run (MongoDB upsert — never duplicates).

    curl -X POST http://127.0.0.1:5000/api/blog/seed \\
      -H "Authorization: Bearer <admin_token>"
    """
    seeded = []
    for post in BLOG_POSTS_SEED:
        doc = {**post, "created_at": _now(), "updated_at": _now()}
        try:
            db.collection("blog_posts").document(post["id"]).set(doc, merge=True)
            seeded.append(post["id"])
        except Exception as e:
            log.error("seed_blog_posts failed for %s: %s", post["id"], e)
    log.info("Blog posts seeded: %d", len(seeded))
    return {"seeded": len(seeded), "ids": seeded}


# ══════════════════════════════════════════════════════════════════════════════
# BLOG POSTS — STANDARD JSON CRUD
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/blog", status_code=201, tags=["Blog"])
def create_blog_post(
    body:    CreateBlogPostBody,
    payload: dict = Depends(require_mod),
):
    """
    POST /api/blog  (JSON body)
    Create a new blog post. Set coverImage to a URL, or leave empty and
    upload via POST /api/blog/{id}/cover-image afterwards.

    curl -X POST http://127.0.0.1:5000/api/blog \\
      -H "Authorization: Bearer <mod_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"id":"my-post","title":"My Post","field":"AI","type":"article"}'
    """
    doc = {
        **body.model_dump(),
        "author": body.author.model_dump(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        db.collection("blog_posts").document(body.id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("Blog post created: %s by %s", body.id, get_uid(payload))
    return _fmt_blog(doc)


@router.get("/blog", tags=["Blog"])
def list_blog_posts(
    field: str = Query("", description="Filter by field (e.g. BIOLOGY)"),
    type:  str = Query("", description="Filter by type: article | explainer | simulation"),
    tag:   str = Query("", description="Filter by tag (case-insensitive)"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
):
    """
    GET /api/blog?field=BIOLOGY&type=article&tag=CRISPR
    List all blog posts. Supports optional filtering by field, type, and tag.

    curl http://127.0.0.1:5000/api/blog
    curl "http://127.0.0.1:5000/api/blog?field=AI&type=article"
    """
    try:
        q    = db.collection("blog_posts").order_by("publishDate", direction="DESCENDING")
        docs = q.limit(limit).stream()
        posts = []
        for d in docs:
            p = d.to_dict()
            if field and p.get("field", "").upper() != field.strip().upper():
                continue
            if type and p.get("type", "") != type.strip():
                continue
            if tag:
                tags_lower = [t.lower() for t in p.get("tags", [])]
                if tag.strip().lower() not in tags_lower:
                    continue
            posts.append(_fmt_blog(p))
    except Exception as e:
        log.error("list_blog_posts: %s", e)
        raise HTTPException(500, detail=str(e))

    return {"posts": posts, "total": len(posts)}


@router.get("/blog/{post_id}", tags=["Blog"])
def get_blog_post(post_id: str):
    """
    GET /api/blog/{id}

    curl http://127.0.0.1:5000/api/blog/the-future-of-quantum-computing
    """
    doc = db.collection("blog_posts").document(post_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Blog post not found")
    return _fmt_blog(doc.to_dict())


@router.put("/blog/{post_id}", tags=["Blog"])
def update_blog_post(
    post_id: str,
    body:    UpdateBlogPostBody,
    payload: dict = Depends(require_mod),
):
    """
    PUT /api/blog/{id}
    Update any fields of a blog post. Only supplied fields are changed.

    curl -X PUT http://127.0.0.1:5000/api/blog/the-future-of-quantum-computing \\
      -H "Authorization: Bearer <mod_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"title":"Updated Title","badgeColor":"violet"}'
    """
    doc_ref = db.collection("blog_posts").document(post_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Blog post not found")

    updates = {}
    for k, v in body.model_dump().items():
        if v is None:
            continue
        if k == "author" and isinstance(v, dict):
            updates["author"] = v
        else:
            updates[k] = v
    updates["updated_at"] = _now()

    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("Blog post updated: %s by %s", post_id, get_uid(payload))
    return {"updated": True, "id": post_id}


@router.delete("/blog/{post_id}", tags=["Blog"])
def delete_blog_post(
    post_id: str,
    payload: dict = Depends(require_admin),
):
    """
    DELETE /api/blog/{id}
    ADMIN ONLY.

    curl -X DELETE http://127.0.0.1:5000/api/blog/my-post \\
      -H "Authorization: Bearer <admin_token>"
    """
    doc_ref = db.collection("blog_posts").document(post_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Blog post not found")

    old_cover_url = doc.to_dict().get("coverImage", "")
    doc_ref.delete()

    deleted_file = _delete_file(old_cover_url)

    log.info("Blog post deleted: %s by %s (image deleted=%s)",
             post_id, get_uid(payload), deleted_file)
    return {"deleted": True, "id": post_id, "image_deleted": deleted_file}


@router.post("/blog/{post_id}/cover-image", tags=["Blog"])
async def upload_blog_cover_image(
    post_id: str,
    image:   UploadFile = File(..., description="New cover image file"),
    payload: dict       = Depends(require_mod),
):
    """
    POST /api/blog/{id}/cover-image  (multipart/form-data)
    Upload or replace the cover image for a blog post.
    The file is saved as  images/blog/<post_id>.<ext>

    curl -X POST http://127.0.0.1:5000/api/blog/my-post/cover-image \\
      -H "Authorization: Bearer <mod_token>" \\
      -F "image=@/path/to/cover.jpg"
    """
    # _ensure_dirs() / _save_file handle directory creation automatically

    doc_ref = db.collection("blog_posts").document(post_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Blog post not found")

    old_cover_url = doc.to_dict().get("coverImage", "")

    ext      = os.path.splitext(image.filename or "")[-1].lower() or ".jpg"
    filename = f"{post_id}{ext}"
    img_url  = _save_file(image, "blog", filename)

    try:
        doc_ref.update({"coverImage": img_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Image saved but DB update failed: {e}")

    # Delete old cover image when the path changed (different extension or URL)
    deleted_old = False
    if old_cover_url and old_cover_url != img_url:
        deleted_old = _delete_file(old_cover_url)

    log.info("Blog cover image uploaded: %s → %s by %s (old deleted=%s)",
             post_id, img_url, get_uid(payload), deleted_old)
    return {
        "updated":     True,
        "id":          post_id,
        "coverImage":  img_url,
        "deleted_old": deleted_old,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CMS EDIT HELPERS — for admin / mod content management UI
# Returns full documents ready to pre-populate edit forms.
# ══════════════════════════════════════════════════════════════════════════════

# ── CMS: Explainers ───────────────────────────────────────────────────────────

@router.get("/cms/explainers", tags=["CMS"])
def cms_list_explainers(payload: dict = Depends(require_mod)):
    """
    GET /api/cms/explainers
    MOD/ADMIN — slim list of all explainers for a CMS management table.
    Returns id, title, field, badgeColor, readTime, author, image, updated_at.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/explainers
    """
    try:
        docs = db.collection("explainers").order_by("title").stream()
        rows = []
        for d in docs:
            ex = d.to_dict()
            rows.append({
                "id":         ex.get("id"),
                "title":      ex.get("title"),
                "field":      ex.get("field"),
                "badgeColor": ex.get("badgeColor"),
                "readTime":   ex.get("readTime"),
                "author":     ex.get("author", ""),
                "image":      ex.get("image", ""),
                "updated_at": ex.get("updated_at", ""),
            })
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"explainers": rows, "total": len(rows)}


@router.get("/cms/explainers/{explainer_id}", tags=["CMS"])
def cms_get_explainer(
    explainer_id: str,
    payload: dict = Depends(require_mod),
):
    """
    GET /api/cms/explainers/{id}
    MOD/ADMIN — full explainer document, pre-populated for an edit form.
    Includes all fields: content, keyInsights, context, technicalDetail, impact.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/explainers/quantum-dog
    """
    doc = db.collection("explainers").document(explainer_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Explainer not found")
    ex = doc.to_dict()
    return {
        **_fmt_explainer(ex),
        "created_at": ex.get("created_at", ""),
        "updated_at": ex.get("updated_at", ""),
    }


# ── CMS: Research Articles ────────────────────────────────────────────────────

@router.get("/cms/research", tags=["CMS"])
def cms_list_research(payload: dict = Depends(require_mod)):
    """
    GET /api/cms/research
    MOD/ADMIN — slim list of all research articles for a CMS management table.
    Returns id, title, field, author, date, readTime, image, updated_at.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/research
    """
    try:
        docs = db.collection("research_articles").order_by("date", direction="DESCENDING").stream()
        rows = []
        for d in docs:
            art = d.to_dict()
            rows.append({
                "id":         art.get("id"),
                "title":      art.get("title"),
                "field":      art.get("field"),
                "author":     art.get("author", ""),
                "date":       art.get("date", ""),
                "readTime":   art.get("readTime", ""),
                "image":      art.get("image", ""),
                "updated_at": art.get("updated_at", ""),
            })
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"articles": rows, "total": len(rows)}


@router.get("/cms/research/{article_id}", tags=["CMS"])
def cms_get_research_article(
    article_id: str,
    payload: dict = Depends(require_mod),
):
    """
    GET /api/cms/research/{id}
    MOD/ADMIN — full research article document, pre-populated for an edit form.
    Includes all fields: abstract, content, quotes, keyFindings, relatedTopics.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/research/a1
    """
    doc = db.collection("research_articles").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Research article not found")
    art = doc.to_dict()
    return {
        **_fmt_article(art),
        "created_at": art.get("created_at", ""),
        "updated_at": art.get("updated_at", ""),
    }


# ── CMS: Blog Posts ───────────────────────────────────────────────────────────

@router.get("/cms/blog", tags=["CMS"])
def cms_list_blog(payload: dict = Depends(require_mod)):
    """
    GET /api/cms/blog
    MOD/ADMIN — slim list of all blog posts for a CMS management table.
    Returns id, title, field, type, publishDate, readingTime, tags, coverImage, updated_at.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/blog
    """
    try:
        docs = db.collection("blog_posts").order_by("publishDate", direction="DESCENDING").stream()
        rows = []
        for d in docs:
            p = d.to_dict()
            rows.append({
                "id":          p.get("id"),
                "title":       p.get("title"),
                "field":       p.get("field", ""),
                "type":        p.get("type", "article"),
                "publishDate": p.get("publishDate", ""),
                "readingTime": p.get("readingTime", ""),
                "tags":        p.get("tags", []),
                "coverImage":  p.get("coverImage", ""),
                "updated_at":  p.get("updated_at", ""),
            })
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"posts": rows, "total": len(rows)}


@router.get("/cms/blog/{post_id}", tags=["CMS"])
def cms_get_blog_post(
    post_id: str,
    payload: dict = Depends(require_mod),
):
    """
    GET /api/cms/blog/{id}
    MOD/ADMIN — full blog post document, pre-populated for an edit form.
    Includes all fields: content (markdown), author object, keyInsights, tags, etc.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/blog/the-future-of-quantum-computing
    """
    doc = db.collection("blog_posts").document(post_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Blog post not found")
    p = doc.to_dict()
    return {
        **_fmt_blog(p),
        "created_at": p.get("created_at", ""),
        "updated_at": p.get("updated_at", ""),
    }