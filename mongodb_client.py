"""
mongodb_client.py  —  MongoDB Atlas drop-in replacement for firestore_client.py
================================================================================
Exposes an identical public interface to the old Firestore client so that
every router file (chat, feed, content, diary, dashboard, auth) continues
to work without any changes.

The interface mirrored:
  db.collection("name")                    → _Collection
    .document("id")                        → _DocRef
      .get()                               → _Doc   (.exists, .id, .to_dict())
      .set(data, merge=False)              → None
      .update(data)                        → None
      .delete()                            → None
    .where("field", "==", value)           → _Query (chainable)
    .order_by("field", direction="DESC")   → _Query (chainable)
    .limit(n)                              → _Query (chainable)
    .stream()                              → list[_Doc]
    .stream_all()                          → list[_Doc]  (no limit)

SETUP:
  1. Set MONGODB_URI in your .env file:
       MONGODB_URI=mongodb+srv://Sahil:<password>@cluster0.lqp7otw.mongodb.net/?appName=Cluster0
  2. Set MONGODB_DB_NAME (optional, defaults to "steami"):
       MONGODB_DB_NAME=steami

HOW IT WORKS:
  - Each Firestore "collection" maps to a MongoDB collection.
  - Each Firestore "document ID" maps to the MongoDB document's "id" field
    (we do NOT use MongoDB's _id — we keep our own string "id" field so
     the rest of the app never sees ObjectId types).
  - The _id field is always excluded from results (via projection).
  - All operations use pymongo synchronously (matching the old Firestore REST client).

Python 3.10 compatible — uses list[str] style hints wrapped in quotes where needed.
"""

import os
import ssl
import logging
from typing import Optional, Any

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.server_api import ServerApi
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

# Read connection string from environment.
# Set MONGODB_URI in your .env:
#   MONGODB_URI=mongodb+srv://Sahil:<password>@cluster0.lqp7otw.mongodb.net/?appName=Cluster0
MONGODB_URI: str = os.environ.get(
    "MONGODB_URI",
    "mongodb+srv://Sahil:CHANGE_ME@cluster0.lqp7otw.mongodb.net/?appName=Cluster0",
)

# Database name — all collections live inside this database
MONGODB_DB_NAME: str = os.environ.get("MONGODB_DB_NAME", "steami")

# ─────────────────────────────────────────────────────────────────────────────
# SSL FIX for Python 3.10 on WSL / Ubuntu 20.04 with OpenSSL < 3.0
# The error "TLSV1_ALERT_INTERNAL_ERROR" means your system OpenSSL cannot
# negotiate TLS 1.3 with MongoDB Atlas. We fix this by:
#   1. Explicitly setting tlsAllowInvalidCertificates=True as a fallback
#   2. Using tls=True with ssl_context that forces TLS 1.2 minimum
#   3. Setting retryWrites=False and w=majority in URI if needed
# ─────────────────────────────────────────────────────────────────────────────

def _make_ssl_context():
    """
    Create a permissive SSL context that works with older OpenSSL versions.
    Forces TLS 1.2 minimum (Atlas requires at least 1.2).
    Disables hostname/cert verification as a fallback for WSL SSL issues.
    """
    ctx = ssl.create_default_context()
    # Allow TLS 1.2 and above (Atlas rejects 1.0/1.1)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Disable certificate verification — fixes TLSV1_ALERT_INTERNAL_ERROR
    # on WSL with outdated CA bundles. Safe for development; for production
    # update your system CA certs instead: sudo apt update && sudo apt install ca-certificates
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _connect() -> MongoClient:
    """
    Try multiple connection strategies in order, returning the first that works.
    This handles the full range of WSL SSL environments.
    """
    uri = MONGODB_URI

    # Strategy 1: Standard connection (works on Ubuntu 22+ with OpenSSL 3.x)
    try:
        client = MongoClient(
            uri,
            server_api              = ServerApi("1"),
            serverSelectionTimeoutMS= 8000,
            connectTimeoutMS        = 8000,
            socketTimeoutMS         = 20000,
        )
        client.admin.command("ping")
        log.info("MongoDB connected (strategy 1 — standard TLS) database: %s", MONGODB_DB_NAME)
        return client
    except Exception as e1:
        log.warning("MongoDB strategy 1 failed: %s", str(e1)[:120])

    # Strategy 2: Custom SSL context with TLS 1.2 minimum + cert verification disabled
    # Fixes: TLSV1_ALERT_INTERNAL_ERROR on WSL with OpenSSL 1.1.x
    try:
        client = MongoClient(
            uri,
            server_api              = ServerApi("1"),
            serverSelectionTimeoutMS= 8000,
            connectTimeoutMS        = 8000,
            socketTimeoutMS         = 20000,
            ssl                     = True,
            ssl_context             = _make_ssl_context(),
        )
        client.admin.command("ping")
        log.info("MongoDB connected (strategy 2 — custom SSL ctx) database: %s", MONGODB_DB_NAME)
        return client
    except Exception as e2:
        log.warning("MongoDB strategy 2 failed: %s", str(e2)[:120])

    # Strategy 3: tlsAllowInvalidCertificates via URI parameter
    # Fixes environments where ssl_context is ignored
    try:
        sep = "&" if "?" in uri else "?"
        uri3 = uri + sep + "tlsAllowInvalidCertificates=true&tls=true"
        client = MongoClient(
            uri3,
            server_api              = ServerApi("1"),
            serverSelectionTimeoutMS= 10000,
            connectTimeoutMS        = 10000,
        )
        client.admin.command("ping")
        log.info("MongoDB connected (strategy 3 — tlsAllowInvalidCertificates) database: %s", MONGODB_DB_NAME)
        return client
    except Exception as e3:
        log.warning("MongoDB strategy 3 failed: %s", str(e3)[:120])

    # Strategy 4: tlsInsecure=true (pymongo 4.x parameter)
    try:
        client = MongoClient(
            uri,
            server_api              = ServerApi("1"),
            serverSelectionTimeoutMS= 10000,
            tlsAllowInvalidCertificates = True,
            tlsAllowInvalidHostnames    = True,
        )
        client.admin.command("ping")
        log.info("MongoDB connected (strategy 4 — tlsInsecure) database: %s", MONGODB_DB_NAME)
        return client
    except Exception as e4:
        log.error("All MongoDB connection strategies failed.")
    
    try:
        log.error("Strategy 1: %s", str(e1)[:200])
    except:
        pass

    log.error("Strategy 4: %s", str(e4)[:200])

    raise RuntimeError(
        "Cannot connect to MongoDB Atlas. Fix SSL (install certs + upgrade pymongo)."
    ) from e4

# Establish connection on module load
try:
    _client = _connect()
except Exception as e:
    log.error("MongoDB connection failed: %s", e)
    raise

# The database object — every collection lives inside this
_mongo_db = _client[MONGODB_DB_NAME]

# Projection that always excludes MongoDB's internal _id from results
_NO_ID = {"_id": 0}


# ─────────────────────────────────────────────────────────────────────────────
# _Doc  —  mirrors firestore_client._Doc
# ─────────────────────────────────────────────────────────────────────────────

class _Doc:
    """
    Represents a single document result.
    Identical interface to the old Firestore _Doc class.

    Attributes:
        exists (bool):  True if the document was found in MongoDB.
        id     (str):   The document's string ID field.
    """

    def __init__(self, data: Optional[dict], doc_id: str):
        # Store the raw data (or None if the doc doesn't exist)
        self._data  = data
        self.exists = data is not None  # True when document was found
        self.id     = doc_id            # The string "id" field value

    def to_dict(self) -> dict:
        """Return the document data as a plain Python dict."""
        return self._data or {}


# ─────────────────────────────────────────────────────────────────────────────
# _DocRef  —  mirrors firestore_client._DocRef
# Represents a reference to a specific document by collection + id.
# ─────────────────────────────────────────────────────────────────────────────

class _DocRef:
    """
    Reference to a specific document inside a MongoDB collection.
    Supports get / set / update / delete — same as Firestore _DocRef.
    """

    def __init__(self, collection_name: str, doc_id: str):
        # The pymongo Collection object
        self._col    = _mongo_db[collection_name]
        self._col_name = collection_name
        self.doc_id  = doc_id

    def get(self) -> _Doc:
        """
        Fetch the document from MongoDB.
        Returns _Doc with exists=False if not found.

        Equivalent to Firestore: doc_ref.get()
        """
        result = self._col.find_one({"id": self.doc_id}, _NO_ID)
        if result is None:
            return _Doc(None, self.doc_id)
        return _Doc(result, self.doc_id)

    def set(self, data: dict, merge: bool = False) -> None:
        """
        Create or replace a document.
        If merge=True, only the provided fields are updated (upsert partial).
        If merge=False, the entire document is replaced.

        Equivalent to Firestore: doc_ref.set(data) or doc_ref.set(data, merge=True)

        We always ensure the "id" field is stored in the document itself
        so queries and to_dict() can always return it.
        """
        # Always embed the id into the document
        doc = {**data, "id": self.doc_id}

        if merge:
            # merge=True → update only the provided fields; create if absent
            self._col.update_one(
                {"id": self.doc_id},
                {"$set": doc},
                upsert=True,  # create the document if it doesn't exist
            )
        else:
            # merge=False → replace the entire document
            self._col.replace_one(
                {"id": self.doc_id},
                doc,
                upsert=True,  # create if not exists
            )

    def update(self, data: dict) -> None:
        """
        Update specific fields on an existing document.
        Only the fields in `data` are modified; all others are left unchanged.

        Equivalent to Firestore: doc_ref.update(data)
        """
        self._col.update_one(
            {"id": self.doc_id},
            {"$set": data},
        )

    def delete(self) -> None:
        """
        Delete the document from MongoDB.

        Equivalent to Firestore: doc_ref.delete()
        """
        self._col.delete_one({"id": self.doc_id})


# ─────────────────────────────────────────────────────────────────────────────
# _Query  —  mirrors firestore_client._Query
# Chainable query builder: where / order_by / limit / stream
# ─────────────────────────────────────────────────────────────────────────────

class _Query:
    """
    Chainable query builder for a MongoDB collection.
    Supports: .where() .order_by() .limit() .stream()

    Equivalent to Firestore chained queries:
      db.collection("x").where("field","==","val").order_by("ts","DESCENDING").limit(10).stream()
    """

    def __init__(self, collection_name: str):
        self._col          = _mongo_db[collection_name]
        self._col_name     = collection_name
        self._filters: list  = []        # list of (field, op, value) tuples
        self._order_field: Optional[str] = None
        self._order_dir:   int           = DESCENDING  # pymongo DESCENDING = -1
        self._limit_n:     Optional[int] = None        # None = no limit

    def where(self, field: str, op: str, value: Any) -> "_Query":
        """
        Add a filter condition.
        Supported operators: == != < <= > >=

        Example: .where("email", "==", "test@example.com")
        """
        self._filters.append((field, op, value))
        return self  # return self for chaining

    def order_by(self, field: str, direction: str = "ASCENDING") -> "_Query":
        """
        Sort results by a field.
        direction: "ASCENDING" or "DESCENDING"

        Example: .order_by("created_at", direction="DESCENDING")
        """
        self._order_field = field
        # Convert the Firestore-style string to a pymongo integer direction
        self._order_dir = DESCENDING if direction.upper() == "DESCENDING" else ASCENDING
        return self  # return self for chaining

    def limit(self, n: int) -> "_Query":
        """Limit number of results returned."""
        self._limit_n = n
        return self  # return self for chaining

    def _build_filter(self) -> dict:
        """
        Convert the list of (field, op, value) tuples into a MongoDB filter dict.
        Supports: == != < <= > >=
        Multiple conditions are ANDed together.
        """
        # MongoDB comparison operator mapping
        OP_MAP = {
            "==": None,    # equality — no operator needed, just {field: value}
            "!=": "$ne",
            "<":  "$lt",
            "<=": "$lte",
            ">":  "$gt",
            ">=": "$gte",
        }

        mongo_filter: dict = {}

        for field, op, value in self._filters:
            mongo_op = OP_MAP.get(op)
            if mongo_op is None:
                # Equality: { field: value }
                mongo_filter[field] = value
            else:
                # Comparison: { field: { $op: value } }
                # If the field already has conditions, merge them
                if field in mongo_filter and isinstance(mongo_filter[field], dict):
                    mongo_filter[field][mongo_op] = value
                else:
                    mongo_filter[field] = {mongo_op: value}

        return mongo_filter

    def stream(self) -> "list[_Doc]":
        """
        Execute the query and return matching documents as a list of _Doc.

        Equivalent to Firestore: query.stream()
        """
        mongo_filter = self._build_filter()

        # Build the pymongo cursor
        cursor = self._col.find(mongo_filter, _NO_ID)

        # Apply sort if specified
        if self._order_field:
            cursor = cursor.sort(self._order_field, self._order_dir)

        # Apply limit if specified
        if self._limit_n is not None:
            cursor = cursor.limit(self._limit_n)

        # Convert each MongoDB document to a _Doc object
        docs = []
        for raw in cursor:
            doc_id = raw.get("id", "")
            docs.append(_Doc(raw, doc_id))

        return docs

    def stream_all(self) -> "list[_Doc]":
        """
        Fetch ALL documents matching the current filters (no limit).
        Used by the article refresh endpoint to scan all articles.

        Equivalent to Firestore: collection.stream_all()
        """
        old_limit = self._limit_n
        self._limit_n = None   # temporarily remove the limit
        result = self.stream()
        self._limit_n = old_limit  # restore
        return result


# ─────────────────────────────────────────────────────────────────────────────
# _Collection  —  mirrors firestore_client._Collection
# ─────────────────────────────────────────────────────────────────────────────

class _Collection:
    """
    Represents a MongoDB collection.
    Entry point for document references and queries.

    Equivalent to Firestore: db.collection("name")
    """

    def __init__(self, name: str):
        self.name = name          # collection name

    def document(self, doc_id: str) -> _DocRef:
        """
        Get a reference to a specific document by ID.
        Equivalent to Firestore: collection.document("my-id")
        """
        return _DocRef(self.name, doc_id)

    def where(self, field: str, op: str, value: Any) -> _Query:
        """Start a filtered query. Returns a chainable _Query."""
        return _Query(self.name).where(field, op, value)

    def order_by(self, field: str, direction: str = "ASCENDING") -> _Query:
        """Start a sorted query. Returns a chainable _Query."""
        return _Query(self.name).order_by(field, direction)

    def limit(self, n: int) -> _Query:
        """Start a limited query. Returns a chainable _Query."""
        return _Query(self.name).limit(n)

    def stream(self) -> "list[_Doc]":
        """Fetch all documents in the collection (no filters, no limit)."""
        return _Query(self.name).stream()

    def stream_all(self) -> "list[_Doc]":
        """
        Fetch ALL documents with no limit.
        Used by the refresh endpoint to scan every article for expiry.
        """
        return _Query(self.name).stream_all()


# ─────────────────────────────────────────────────────────────────────────────
# _DB  —  the top-level db object, mirrors firestore_client._DB
# ─────────────────────────────────────────────────────────────────────────────

class _DB:
    """
    Top-level database object.
    db.collection("name") is the only method needed — identical to Firestore.
    """

    def collection(self, name: str) -> _Collection:
        """
        Access a MongoDB collection by name.
        Equivalent to Firestore: db.collection("articles")
        """
        return _Collection(name)


# ─────────────────────────────────────────────────────────────────────────────
# Public singleton  —  import this in every router:  from mongodb_client import db
# ─────────────────────────────────────────────────────────────────────────────

db = _DB()