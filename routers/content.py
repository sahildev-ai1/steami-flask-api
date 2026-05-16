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
import logging
from datetime import datetime, timezone
from typing import Optional

import cloudinary
import cloudinary.uploader

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
# CLOUDINARY CONFIG
# Set these three variables in your .env / Render environment:
#   CLOUDINARY_CLOUD_NAME=your_cloud_name
#   CLOUDINARY_API_KEY=your_api_key
#   CLOUDINARY_API_SECRET=your_api_secret
# Images are uploaded to the  steami/<folder>  folder in your Cloudinary account.
# The returned URL is a full https:// CDN URL — no StaticFiles mount needed.
# ─────────────────────────────────────────────────────────────────────────────

cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.environ.get("CLOUDINARY_API_KEY"),
    api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
    secure     = True,   # always return https:// URLs
)

ALLOWED_MIME = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    """Current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _delete_file(url_path: str) -> bool:
    """
    Delete an image from Cloudinary given its public URL or Cloudinary public_id.

    - Skips empty strings silently.
    - For Cloudinary URLs (https://res.cloudinary.com/...), extracts the public_id
      and calls the Cloudinary destroy API.
    - Returns True if deleted successfully, False otherwise.
    - Never raises — failures are logged as warnings.
    """
    if not url_path:
        return False

    try:
        # Extract the public_id from a Cloudinary URL.
        # URL format: https://res.cloudinary.com/<cloud>/image/upload/v<ver>/<public_id>.<ext>
        if "res.cloudinary.com" in url_path:
            # Strip everything up to and including "/upload/"
            after_upload = url_path.split("/upload/")[-1]
            # Drop the version segment if present (e.g. "v1234567890/")
            if after_upload.startswith("v") and "/" in after_upload:
                after_upload = after_upload.split("/", 1)[1]
            # Strip the file extension to get the public_id
            public_id = os.path.splitext(after_upload)[0]
        else:
            # Treat the value as a raw public_id (e.g. "steami/explainers/quantum-dog")
            public_id = os.path.splitext(url_path.lstrip("/"))[0]

        result = cloudinary.uploader.destroy(public_id)
        if result.get("result") == "ok":
            log.info("_delete_file: Cloudinary deleted public_id=%s", public_id)
            return True
        else:
            log.warning("_delete_file: Cloudinary could not delete public_id=%s result=%s", public_id, result)
            return False
    except Exception as exc:
        log.warning("_delete_file: error deleting from Cloudinary url=%s: %s", url_path, exc)
        return False


def _save_file(upload: UploadFile, folder: str, filename: str) -> str:
    """
    Upload an image to Cloudinary under the  steami/<folder>  folder.

    Cloudinary handles deduplication automatically via its asset pipeline.
    The public_id is set to  steami/<folder>/<stem>  (no extension) so
    re-uploading the same filename overwrites the existing asset in place.

    Returns the full https:// CDN URL of the uploaded image.

    Args:
        upload:   FastAPI UploadFile from the multipart form.
        folder:   Sub-folder name — "explainers", "research", or "blog".
        filename: Target filename including extension (e.g. "quantum-dog.jpg").

    Raises:
        HTTPException(400) for invalid file type or extension.
        HTTPException(500) if the Cloudinary upload fails.
    """
    # Validate MIME type
    if upload.content_type and upload.content_type not in ALLOWED_MIME:
        raise HTTPException(
            400,
            detail=f"File type '{upload.content_type}' not allowed. Use JPEG, PNG, WebP, or GIF."
        )

    # Validate / infer extension
    ext = os.path.splitext(filename)[-1].lower()
    if ext not in ALLOWED_EXT:
        orig_ext = os.path.splitext(upload.filename or "")[-1].lower()
        if orig_ext in ALLOWED_EXT:
            filename = filename + orig_ext
            ext      = orig_ext
        else:
            raise HTTPException(
                400,
                detail=f"Invalid image extension '{ext}'. Allowed: {', '.join(ALLOWED_EXT)}"
            )

    # Read bytes
    try:
        data = upload.file.read()
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to read uploaded file: {e}")

    # Build a stable public_id so re-uploads overwrite the same asset
    stem      = os.path.splitext(filename)[0]          # e.g. "quantum-dog"
    public_id = f"steami/{folder}/{stem}"              # e.g. "steami/explainers/quantum-dog"

    # Upload to Cloudinary (overwrite=True replaces existing asset with same public_id)
    try:
        result = cloudinary.uploader.upload(
            data,
            public_id  = public_id,
            folder     = "",          # public_id already contains the folder path
            overwrite  = True,
            resource_type = "image",
        )
        secure_url = result["secure_url"]
        log.info("Cloudinary upload: %s → %s", filename, secure_url)
        return secure_url
    except Exception as e:
        log.error("Cloudinary upload failed for %s: %s", filename, e)
        raise HTTPException(500, detail=f"Failed to upload image to Cloudinary: {e}")


def _find_duplicate(data: bytes, skip_path: str = "") -> "str | None":
    """
    Stub kept for API compatibility — Cloudinary handles deduplication
    via public_id overwriting, so this always returns None.
    """
    return None


# ── dead code kept so nothing below breaks ───────────────────────────────────
def _ensure_dirs() -> None:
    pass  # No local directories needed with Cloudinary


def _file_hash(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()





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

# ══════════════════════════════════════════════════════════════════════════════
# SIMULATIONS
# ══════════════════════════════════════════════════════════════════════════════
# Endpoints:
#   POST   /api/simulations/seed                          — bulk seed (admin)
#   POST   /api/simulations                               — JSON create (mod/admin)
#   GET    /api/simulations                               — list (PUBLIC — every user)
#   GET    /api/simulations/{id}                          — get one (PUBLIC)
#   PUT    /api/simulations/{id}                          — update (mod/admin)
#   DELETE /api/simulations/{id}                          — delete (admin)
#   POST   /api/simulations/{id}/snapshot                 — upload canvas PNG snapshot (mod/admin)
#   POST   /api/simulations/{id}/glb                      — upload raw .glb / .gltf file (mod/admin)
#   GET    /api/cms/simulations                           — CMS slim list (mod/admin)
#   GET    /api/cms/simulations/{id}                      — CMS full doc (mod/admin)
# ══════════════════════════════════════════════════════════════════════════════

import base64
import uuid as _uuid

# ── Allowed 3-D / snapshot MIME types ────────────────────────────────────────

ALLOWED_3D_EXT = {".glb", ".gltf", ".obj", ".fbx", ".stl"}

# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────────────

class CreateSimulationBody(BaseModel):
    """
    JSON body for POST /api/simulations.
    simulation_type: 'bloch_sphere' | 'three_body' | 'custom'
    component_id   : matches the React component key used in SimulationsPage
                     e.g. 'quantum' | 'threebody' | custom string
    """
    id:              str
    title:           str
    field:           str              = ""
    fieldColor:      str              = ""   # e.g. 'steami-badge-violet'
    description:     str              = ""
    caption:         str              = ""
    readTime:        str              = "10 min interactive"
    simulation_type: str              = "custom"
    component_id:    str              = ""   # React component key: 'quantum' | 'threebody'
    insights:        list             = []
    snapshot_url:    str              = ""   # Cloudinary CDN URL for the preview image
    glb_url:         str              = ""   # Cloudinary CDN URL for the .glb file (if any)
    tags:            list             = []


class UpdateSimulationBody(BaseModel):
    title:           Optional[str]  = None
    field:           Optional[str]  = None
    fieldColor:      Optional[str]  = None
    description:     Optional[str]  = None
    caption:         Optional[str]  = None
    readTime:        Optional[str]  = None
    simulation_type: Optional[str]  = None
    component_id:    Optional[str]  = None
    insights:        Optional[list] = None
    snapshot_url:    Optional[str]  = None
    glb_url:         Optional[str]  = None
    tags:            Optional[list] = None


class SnapshotUploadBody(BaseModel):
    """Base64 PNG captured from a Three.js canvas."""
    image_data: str   # data:image/png;base64,<…>  OR raw base64


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_simulation(s: dict) -> dict:
    """Return a clean simulation dict safe to send to the frontend."""
    return {
        "id":              s.get("id"),
        "title":           s.get("title"),
        "field":           s.get("field", ""),
        "fieldColor":      s.get("fieldColor", "steami-badge-cyan"),
        "description":     s.get("description", ""),
        "caption":         s.get("caption", ""),
        "readTime":        s.get("readTime", "10 min interactive"),
        "simulation_type": s.get("simulation_type", "custom"),
        "component_id":    s.get("component_id", ""),
        "insights":        s.get("insights", []),
        "snapshot_url":    s.get("snapshot_url", ""),
        "glb_url":         s.get("glb_url", ""),
        "tags":            s.get("tags", []),
    }


def _upload_snapshot_to_cloudinary(b64_data: str, sim_id: str) -> str:
    """
    Upload a base64 PNG snapshot to Cloudinary.
    Returns the secure CDN URL.
    """
    # Strip the data-URI prefix if present
    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]

    try:
        base64.b64decode(b64_data)   # validate
    except Exception:
        raise HTTPException(400, detail="Invalid base64 image data")

    public_id = f"steami/simulations/{sim_id}/snapshot"
    try:
        result = cloudinary.uploader.upload(
            f"data:image/png;base64,{b64_data}",
            public_id     = public_id,
            resource_type = "image",
            overwrite     = True,
            tags          = ["simulation", "snapshot", sim_id],
        )
        return result["secure_url"]
    except Exception as exc:
        log.error("Cloudinary snapshot upload failed for %s: %s", sim_id, exc)
        raise HTTPException(500, detail=f"Cloudinary upload failed: {exc}")


def _upload_glb_to_cloudinary(data: bytes, sim_id: str, filename: str) -> str:
    """
    Upload a raw .glb/.gltf file to Cloudinary as a raw resource.
    Returns the secure CDN URL.
    """
    ext       = os.path.splitext(filename)[-1].lower()
    public_id = f"steami/simulations/{sim_id}/model_{_uuid.uuid4().hex[:8]}{ext}"
    try:
        result = cloudinary.uploader.upload(
            data,
            public_id     = public_id,
            resource_type = "raw",   # required for binary 3-D files
            overwrite     = True,
            tags          = ["simulation", "glb", sim_id],
        )
        return result["secure_url"]
    except Exception as exc:
        log.error("Cloudinary GLB upload failed for %s: %s", sim_id, exc)
        raise HTTPException(500, detail=f"Cloudinary upload failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# SEED
# ─────────────────────────────────────────────────────────────────────────────

_SIMULATION_SEED = [
    {
        "id":              "quantum",
        "title":           "How does Qubits react in Quantum Space",
        "field":           "QUANTUM COMPUTING",
        "fieldColor":      "steami-badge-violet",
        "description":     "Explore the Bloch sphere — the geometric representation of a qubit's quantum state. Unlike classical bits locked to 0 or 1, a qubit can exist in any superposition, represented as a point anywhere on the sphere's surface.",
        "caption":         "Interactive Bloch Sphere — drag to rotate, toggle superposition mode, or manually set θ and φ angles.",
        "readTime":        "12 min interactive",
        "simulation_type": "bloch_sphere",
        "component_id":    "quantum",
        "insights": [
            "A qubit is like a coin spinning in the air — it's both heads and tails until it lands (is measured).",
            "The Bloch sphere is a map of all possible qubit states — the north pole is '0', the south pole is '1', and everywhere else is a mix.",
            "Quantum computers use qubits to test many answers at once, like reading every book in a library simultaneously.",
            "When you measure a qubit, its superposition 'collapses' to a definite answer — just like catching the spinning coin.",
        ],
        "snapshot_url": "",
        "glb_url":      "",
        "tags":         ["quantum", "physics", "interactive"],
    },
    {
        "id":              "threebody",
        "title":           "Three Body Problem",
        "field":           "PHYSICS",
        "fieldColor":      "steami-badge-cyan",
        "description":     "The three-body problem has no general closed-form solution — three masses interacting gravitationally produce chaotic, unpredictable trajectories. This simulation demonstrates why even tiny changes in initial conditions lead to wildly divergent orbits.",
        "caption":         "Gravitational N-body simulation — adjust mass ratios and simulation speed to observe chaotic dynamics.",
        "readTime":        "10 min interactive",
        "simulation_type": "three_body",
        "component_id":    "threebody",
        "insights": [
            "Predicting the motion of three objects pulling on each other with gravity is one of the oldest unsolved problems in physics.",
            "Even the tiniest change in starting position can lead to a completely different outcome — this is called 'chaos'.",
            "We can predict Earth orbiting the Sun easily (two bodies), but add a third and the math becomes nearly impossible to solve exactly.",
            "Scientists use computers to approximate solutions step-by-step, which is exactly what this simulation does.",
        ],
        "snapshot_url": "",
        "glb_url":      "",
        "tags":         ["physics", "gravity", "chaos", "interactive"],
    },
]


@router.post("/simulations/seed", status_code=201, tags=["Simulations"])
def seed_simulations(payload: dict = Depends(require_admin)):
    """
    POST /api/simulations/seed
    Bulk-seed the two built-in simulations. Admin only. Safe to re-run (upsert).

    curl -X POST http://127.0.0.1:5000/api/simulations/seed \\
      -H "Authorization: Bearer <admin_token>"
    """
    seeded = []
    for sim in _SIMULATION_SEED:
        doc = {**sim, "created_at": _now(), "updated_at": _now()}
        try:
            db.collection("simulations").document(sim["id"]).set(doc, merge=True)
            seeded.append(sim["id"])
        except Exception as e:
            log.error("seed_simulations failed for %s: %s", sim["id"], e)
    log.info("Simulations seeded: %d", len(seeded))
    return {"seeded": len(seeded), "ids": seeded}


# ─────────────────────────────────────────────────────────────────────────────
# CREATE / READ / UPDATE / DELETE
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulations", status_code=201, tags=["Simulations"])
def create_simulation(
    body:    CreateSimulationBody,
    payload: dict = Depends(require_mod),
):
    """
    POST /api/simulations  (JSON body)
    MOD/ADMIN — create a new simulation record.
    Use POST /api/simulations/{id}/snapshot or /glb to upload media afterwards.

    curl -X POST http://127.0.0.1:5000/api/simulations \\
      -H "Authorization: Bearer <mod_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"id":"wave-function","title":"Wave Function Collapse","field":"QUANTUM PHYSICS","simulation_type":"custom","component_id":"wavefn"}'
    """
    doc = {**body.model_dump(), "created_at": _now(), "updated_at": _now()}
    try:
        db.collection("simulations").document(body.id).set(doc, merge=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("Simulation created: %s by %s", body.id, get_uid(payload))
    return _fmt_simulation(doc)


@router.get("/simulations", tags=["Simulations"])
def list_simulations(
    field:           str = Query("", description="Filter by field e.g. PHYSICS"),
    simulation_type: str = Query("", description="Filter by simulation_type e.g. bloch_sphere"),
):
    """
    GET /api/simulations
    PUBLIC — list all simulations. No auth required.
    Optional query params: field, simulation_type.

    curl http://127.0.0.1:5000/api/simulations
    curl http://127.0.0.1:5000/api/simulations?field=PHYSICS
    """
    try:
        q    = db.collection("simulations").order_by("created_at", direction="ASCENDING")
        docs = q.limit(100).stream()
        sims = []
        for d in docs:
            s = d.to_dict()
            if field and s.get("field", "").upper() != field.strip().upper():
                continue
            if simulation_type and s.get("simulation_type", "") != simulation_type.strip():
                continue
            sims.append(_fmt_simulation(s))
    except Exception as e:
        log.error("list_simulations: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"simulations": sims, "total": len(sims)}


@router.get("/simulations/{simulation_id}", tags=["Simulations"])
def get_simulation(simulation_id: str):
    """
    GET /api/simulations/{id}
    PUBLIC — get a single simulation. No auth required.

    curl http://127.0.0.1:5000/api/simulations/quantum
    """
    doc = db.collection("simulations").document(simulation_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Simulation not found")
    return _fmt_simulation(doc.to_dict())


@router.put("/simulations/{simulation_id}", tags=["Simulations"])
def update_simulation(
    simulation_id: str,
    body:          UpdateSimulationBody,
    payload:       dict = Depends(require_mod),
):
    """
    PUT /api/simulations/{id}
    MOD/ADMIN — update simulation fields. All fields optional.

    curl -X PUT http://127.0.0.1:5000/api/simulations/quantum \\
      -H "Authorization: Bearer <mod_token>" \\
      -d '{"readTime":"15 min interactive"}'
    """
    doc_ref = db.collection("simulations").document(simulation_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Simulation not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    updates["updated_at"] = _now()
    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("Simulation updated: %s by %s", simulation_id, get_uid(payload))
    return {"updated": True, "id": simulation_id}


@router.delete("/simulations/{simulation_id}", tags=["Simulations"])
def delete_simulation(
    simulation_id: str,
    payload:       dict = Depends(require_admin),
):
    """
    DELETE /api/simulations/{id}
    ADMIN only — permanently delete a simulation and its Cloudinary assets.

    curl -X DELETE http://127.0.0.1:5000/api/simulations/quantum \\
      -H "Authorization: Bearer <admin_token>"
    """
    doc_ref = db.collection("simulations").document(simulation_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Simulation not found")
    data = doc.to_dict()
    doc_ref.delete()
    # Clean up Cloudinary assets
    _delete_file(data.get("snapshot_url", ""))
    _delete_file(data.get("glb_url", ""))
    log.info("Simulation deleted: %s by %s", simulation_id, get_uid(payload))
    return {"deleted": True, "id": simulation_id}


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT UPLOAD  (base64 PNG from canvas.toDataURL)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulations/{simulation_id}/snapshot", tags=["Simulations"])
async def upload_simulation_snapshot(
    simulation_id: str,
    body:          SnapshotUploadBody,
    payload:       dict = Depends(require_mod),
):
    """
    POST /api/simulations/{id}/snapshot
    MOD/ADMIN — capture a Three.js canvas PNG and upload it to Cloudinary.
    The returned `snapshot_url` is stored in the simulation document.

    Body JSON:
      { "image_data": "data:image/png;base64,<…>" }

    curl -X POST http://127.0.0.1:5000/api/simulations/quantum/snapshot \\
      -H "Authorization: Bearer <mod_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"image_data":"data:image/png;base64,iVBORw0KG..."}'
    """
    doc_ref = db.collection("simulations").document(simulation_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Simulation not found")

    # Delete old snapshot from Cloudinary
    old_url = doc_ref.get().to_dict().get("snapshot_url", "")
    if old_url:
        _delete_file(old_url)

    snapshot_url = _upload_snapshot_to_cloudinary(body.image_data, simulation_id)

    try:
        doc_ref.update({"snapshot_url": snapshot_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Uploaded to Cloudinary but DB update failed: {e}")

    log.info("Simulation snapshot uploaded: %s → %s by %s", simulation_id, snapshot_url, get_uid(payload))
    return {"updated": True, "id": simulation_id, "snapshot_url": snapshot_url}


# ─────────────────────────────────────────────────────────────────────────────
# GLB / 3-D FILE UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulations/{simulation_id}/glb", tags=["Simulations"])
async def upload_simulation_glb(
    simulation_id: str,
    file:          UploadFile = File(..., description="3-D file (.glb / .gltf / .obj / .fbx / .stl)"),
    payload:       dict       = Depends(require_mod),
):
    """
    POST /api/simulations/{id}/glb  (multipart/form-data)
    MOD/ADMIN — upload a raw 3-D file (.glb etc.) to Cloudinary as a 'raw' resource.
    Requires a Cloudinary paid plan for 3-D asset support.

    curl -X POST http://127.0.0.1:5000/api/simulations/quantum/glb \\
      -H "Authorization: Bearer <mod_token>" \\
      -F "file=@/path/to/bloch.glb"
    """
    doc_ref = db.collection("simulations").document(simulation_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Simulation not found")

    ext = os.path.splitext(file.filename or "")[-1].lower()
    if ext not in ALLOWED_3D_EXT:
        raise HTTPException(400, detail=f"Unsupported 3-D file type '{ext}'. Allowed: {ALLOWED_3D_EXT}")

    data = await file.read()
    glb_url = _upload_glb_to_cloudinary(data, simulation_id, file.filename or f"model{ext}")

    # Delete old GLB from Cloudinary
    old_url = doc_ref.get().to_dict().get("glb_url", "")
    if old_url:
        _delete_file(old_url)

    try:
        doc_ref.update({"glb_url": glb_url, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=f"Uploaded to Cloudinary but DB update failed: {e}")

    log.info("Simulation GLB uploaded: %s → %s by %s", simulation_id, glb_url, get_uid(payload))
    return {"updated": True, "id": simulation_id, "glb_url": glb_url}


# ─────────────────────────────────────────────────────────────────────────────
# CMS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/cms/simulations", tags=["CMS"])
def cms_list_simulations(payload: dict = Depends(require_mod)):
    """
    GET /api/cms/simulations
    MOD/ADMIN — slim list of all simulations for the CMS table.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/simulations
    """
    try:
        docs = db.collection("simulations").order_by("created_at").stream()
        rows = []
        for d in docs:
            s = d.to_dict()
            rows.append({
                "id":              s.get("id"),
                "title":           s.get("title"),
                "field":           s.get("field", ""),
                "simulation_type": s.get("simulation_type", ""),
                "component_id":    s.get("component_id", ""),
                "readTime":        s.get("readTime", ""),
                "snapshot_url":    s.get("snapshot_url", ""),
                "glb_url":         s.get("glb_url", ""),
                "updated_at":      s.get("updated_at", ""),
            })
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"simulations": rows, "total": len(rows)}


@router.get("/cms/simulations/{simulation_id}", tags=["CMS"])
def cms_get_simulation(
    simulation_id: str,
    payload:       dict = Depends(require_mod),
):
    """
    GET /api/cms/simulations/{id}
    MOD/ADMIN — full simulation document, ready to pre-populate an edit form.

    curl -H "Authorization: Bearer <mod_token>" \\
      http://127.0.0.1:5000/api/cms/simulations/quantum
    """
    doc = db.collection("simulations").document(simulation_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Simulation not found")
    s = doc.to_dict()
    return {
        **_fmt_simulation(s),
        "created_at": s.get("created_at", ""),
        "updated_at": s.get("updated_at", ""),
    }