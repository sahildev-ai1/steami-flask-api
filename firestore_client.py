"""
Firestore client — uses Firebase REST API with your web API key.
No serviceAccountKey.json required.
Just set FIREBASE_API_KEY and FIREBASE_PROJECT_ID in .env
"""

import os
import json
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

FIREBASE_API_KEY  = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_PROJECT  = os.environ.get("FIREBASE_PROJECT_ID", "basicchat-19d4a")
FIRESTORE_BASE    = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"


# ─────────────────────────────────────────────────────────────────────────────
# Low-level REST helpers
# ─────────────────────────────────────────────────────────────────────────────

def _url(collection: str, doc_id: str = "") -> str:
    base = f"{FIRESTORE_BASE}/{collection}"
    return f"{base}/{doc_id}" if doc_id else base


def _params() -> dict:
    """Add API key to every request."""
    return {"key": FIREBASE_API_KEY} if FIREBASE_API_KEY else {}


def _to_firestore(value) -> dict:
    """Convert a Python value to Firestore REST value format."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: _to_firestore(v) for k, v in value.items()}}}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_to_firestore(i) for i in value]}}
    return {"stringValue": str(value)}


def _from_firestore(fields: dict) -> dict:
    """Convert Firestore REST fields back to a plain Python dict."""
    out = {}
    for k, v in fields.items():
        out[k] = _parse_value(v)
    return out


def _parse_value(v: dict):
    if "nullValue"     in v: return None
    if "booleanValue"  in v: return v["booleanValue"]
    if "integerValue"  in v: return int(v["integerValue"])
    if "doubleValue"   in v: return v["doubleValue"]
    if "stringValue"   in v: return v["stringValue"]
    if "arrayValue"    in v:
        vals = v["arrayValue"].get("values", [])
        return [_parse_value(i) for i in vals]
    if "mapValue"      in v:
        return _from_firestore(v["mapValue"].get("fields", {}))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API  (mirrors the firebase-admin interface used in app.py)
# ─────────────────────────────────────────────────────────────────────────────

class _Doc:
    def __init__(self, data: dict | None, doc_id: str):
        self._data   = data
        self.exists  = data is not None
        self.id      = doc_id

    def to_dict(self) -> dict:
        return self._data or {}


class _Collection:
    def __init__(self, name: str):
        self.name = name

    def document(self, doc_id: str) -> "_DocRef":
        return _DocRef(self.name, doc_id)

    def order_by(self, field: str, direction: str = "ASCENDING") -> "_Query":
        return _Query(self.name).order_by(field, direction)

    def where(self, field: str, op: str, value) -> "_Query":
        return _Query(self.name).where(field, op, value)

    def limit(self, n: int) -> "_Query":
        return _Query(self.name).limit(n)


class _DocRef:
    def __init__(self, collection: str, doc_id: str):
        self.collection = collection
        self.doc_id     = doc_id

    def get(self) -> _Doc:
        url  = _url(self.collection, self.doc_id)
        resp = requests.get(url, params=_params(), timeout=10)
        if resp.status_code == 404:
            return _Doc(None, self.doc_id)
        resp.raise_for_status()
        raw  = resp.json()
        data = _from_firestore(raw.get("fields", {}))
        data["id"] = self.doc_id
        return _Doc(data, self.doc_id)

    def set(self, data: dict, merge: bool = False) -> None:
        """Create or overwrite a document (merge flag supported via PATCH)."""
        fields = {k: _to_firestore(v) for k, v in data.items() if k != "id"}
        body   = {"fields": fields}
        url    = _url(self.collection, self.doc_id)
        params = dict(_params())
        if merge:
            # PATCH without updateMask = merge-like behaviour for top-level keys
            resp = requests.patch(url, params=params, json=body, timeout=10)
        else:
            resp = requests.patch(url, params=params, json=body, timeout=10)
        if resp.status_code not in (200, 201):
            log.error("Firestore set failed %s: %s", resp.status_code, resp.text[:200])
        resp.raise_for_status()

    def update(self, data: dict) -> None:
        """Patch specific fields on an existing document."""
        existing_resp = requests.get(_url(self.collection, self.doc_id),
                                     params=_params(), timeout=10)
        if existing_resp.status_code == 200:
            existing_fields = existing_resp.json().get("fields", {})
        else:
            existing_fields = {}

        for k, v in data.items():
            existing_fields[k] = _to_firestore(v)

        body   = {"fields": existing_fields}
        url    = _url(self.collection, self.doc_id)
        resp   = requests.patch(url, params=_params(), json=body, timeout=10)
        resp.raise_for_status()


class _Query:
    def __init__(self, collection: str):
        self._collection    = collection
        self._order_field   = None
        self._order_dir     = "DESCENDING"
        self._limit_n       = 30
        self._filters       = []   # list of (field, op, value)

    def order_by(self, field: str, direction: str = "ASCENDING") -> "_Query":
        self._order_field = field
        self._order_dir   = direction.upper()
        return self

    def where(self, field: str, op: str, value) -> "_Query":
        self._filters.append((field, op, value))
        return self

    def limit(self, n: int) -> "_Query":
        self._limit_n = n
        return self

    def stream(self):
        """Execute a structured query via the Firestore REST runQuery endpoint."""
        query: dict = {
            "from": [{"collectionId": self._collection}],
            "limit": self._limit_n,
        }

        if self._filters:
            filter_clauses = []
            op_map = {
                "==": "EQUAL",
                "!=": "NOT_EQUAL",
                "<":  "LESS_THAN",
                "<=": "LESS_THAN_OR_EQUAL",
                ">":  "GREATER_THAN",
                ">=": "GREATER_THAN_OR_EQUAL",
            }
            for field, op, value in self._filters:
                filter_clauses.append({
                    "fieldFilter": {
                        "field": {"fieldPath": field},
                        "op":    op_map.get(op, "EQUAL"),
                        "value": _to_firestore(value),
                    }
                })
            if len(filter_clauses) == 1:
                query["where"] = filter_clauses[0]
            else:
                query["where"] = {
                    "compositeFilter": {
                        "op":     "AND",
                        "filters": filter_clauses,
                    }
                }

        if self._order_field:
            query["orderBy"] = [{
                "field":     {"fieldPath": self._order_field},
                "direction": self._order_dir,
            }]

        url  = f"{FIRESTORE_BASE}:runQuery"
        body = {"structuredQuery": query}
        resp = requests.post(url, params=_params(), json=body, timeout=15)

        if resp.status_code != 200:
            log.error("Firestore query failed %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        results = resp.json()
        docs = []
        for item in results:
            doc = item.get("document")
            if not doc:
                continue
            # Extract doc id from resource name
            doc_id = doc.get("name", "").split("/")[-1]
            data   = _from_firestore(doc.get("fields", {}))
            data["id"] = doc_id
            docs.append(_Doc(data, doc_id))

        return docs


# ─────────────────────────────────────────────────────────────────────────────
# Public db object  — mimics firebase_admin.firestore.client()
# ─────────────────────────────────────────────────────────────────────────────

class _DB:
    def collection(self, name: str) -> _Collection:
        return _Collection(name)


db = _DB()

log.info("Firestore REST client ready — project: %s", FIREBASE_PROJECT)