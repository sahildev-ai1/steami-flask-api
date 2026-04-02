"""
STEAMI Chat API  — chat.py
Blueprint: /api/chat/...

All APIs needed by ChatWindow.tsx, UserSearch.tsx, ChatDashboard.tsx
replacing the Firebase Firestore realtime listener with HTTP polling.

ENDPOINTS
─────────────────────────────────────────────────────────────────────
POST   /api/chat/messages                  — send a message
GET    /api/chat/messages?u1=&u2=&after=   — poll messages between 2 users
PATCH  /api/chat/messages/seen             — mark messages as seen
GET    /api/chat/users?uid=                — get all users except self
GET    /api/chat/users/<uid>               — get single user profile
POST   /api/chat/users                     — upsert user profile (on login)
GET    /api/chat/conversations?uid=        — sidebar: all recent conversations
GET    /api/chat/unread?uid=               — unread message count per sender
─────────────────────────────────────────────────────────────────────
"""

import uuid
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from firestore_client import db

log = logging.getLogger(__name__)
chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")


def _now_ms() -> int:
    """Current time as milliseconds since epoch — matches Date.now() in JS."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════════

@chat_bp.route("/users", methods=["POST"])
def upsert_user():
    """
    POST /api/chat/users
    Create or update a user profile. Call this on login/register.

    Body:
    {
      "id":       "firebase_uid_or_uuid",
      "username": "sahil",
      "avatar":   "https://example.com/avatar.jpg",   // optional
      "email":    "sahil@example.com"                 // optional
    }

    Response:
    {
      "id":        "...",
      "username":  "sahil",
      "avatar":    "https://...",
      "email":     "...",
      "online":    true,
      "last_seen": "2026-04-01T10:00:00+00:00"
    }

    curl -X POST http://127.0.0.1:5000/api/chat/users \\
      -H "Content-Type: application/json" \\
      -d '{"id":"user123","username":"sahil","avatar":"https://i.pravatar.cc/150?u=sahil"}'
    """
    data = request.get_json(silent=True) or {}
    uid  = (data.get("id") or "").strip()

    if not uid:
        return jsonify({"error": "id is required"}), 400
    if not data.get("username"):
        return jsonify({"error": "username is required"}), 400

    profile = {
        "id":        uid,
        "username":  data["username"].strip(),
        "avatar":    data.get("avatar", f"https://i.pravatar.cc/150?u={uid}"),
        "email":     data.get("email", ""),
        "online":    True,
        "last_seen": _now_iso(),
    }

    try:
        db.collection("chat_users").document(uid).set(profile, merge=True)
    except Exception as e:
        log.error("upsert_user failed: %s", e)
        return jsonify({"error": str(e)}), 500

    log.info("chat_user upserted: %s (%s)", uid, profile["username"])
    return jsonify(profile), 200


@chat_bp.route("/users", methods=["GET"])
def get_users():
    """
    GET /api/chat/users?uid=<current_user_id>&q=<search_query>
    Returns all users except the current user.
    Optional ?q= filters by username (case-insensitive, client-side via query).

    Response:
    {
      "users": [
        {
          "id":        "user456",
          "username":  "arjun",
          "avatar":    "https://i.pravatar.cc/150?u=user456",
          "online":    false,
          "last_seen": "2026-04-01T09:00:00+00:00"
        }, ...
      ]
    }

    curl "http://127.0.0.1:5000/api/chat/users?uid=user123"
    curl "http://127.0.0.1:5000/api/chat/users?uid=user123&q=arj"
    """
    current_uid = request.args.get("uid", "").strip()
    search_q    = request.args.get("q", "").strip().lower()

    try:
        docs  = db.collection("chat_users").limit(200).stream()
        users = []
        for d in docs:
            u = d.to_dict()
            # Exclude self
            if u.get("id") == current_uid:
                continue
            # Optional username filter
            if search_q and search_q not in u.get("username", "").lower():
                continue
            # Strip sensitive fields
            users.append({
                "id":        u.get("id"),
                "username":  u.get("username"),
                "avatar":    u.get("avatar", ""),
                "online":    u.get("online", False),
                "last_seen": u.get("last_seen", ""),
            })
    except Exception as e:
        log.error("get_users failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"users": users}), 200


@chat_bp.route("/users/<uid>", methods=["GET"])
def get_user(uid: str):
    """
    GET /api/chat/users/<uid>
    Get a single user profile by ID.

    Response:
    {
      "id":        "user123",
      "username":  "sahil",
      "avatar":    "https://...",
      "online":    true,
      "last_seen": "2026-04-01T10:00:00+00:00"
    }

    curl http://127.0.0.1:5000/api/chat/users/user123
    """
    doc = db.collection("chat_users").document(uid).get()
    if not doc.exists:
        return jsonify({"error": "User not found"}), 404
    u = doc.to_dict()
    return jsonify({
        "id":        u.get("id"),
        "username":  u.get("username"),
        "avatar":    u.get("avatar", ""),
        "online":    u.get("online", False),
        "last_seen": u.get("last_seen", ""),
    }), 200


# ═══════════════════════════════════════════════════════════════════
# MESSAGES
# ═══════════════════════════════════════════════════════════════════

@chat_bp.route("/messages", methods=["POST"])
def send_message():
    """
    POST /api/chat/messages
    Send a message from one user to another.
    This replaces addDoc(collection(db, "messages"), {...}) in chat.ts

    Body:
    {
      "senderId":   "user123",
      "receiverId": "user456",
      "text":       "Hey! How are you?"
    }

    Response:
    {
      "id":         "msg-uuid",
      "senderId":   "user123",
      "receiverId": "user456",
      "text":       "Hey! How are you?",
      "status":     "sent",
      "timestamp":  1743505200000
    }

    curl -X POST http://127.0.0.1:5000/api/chat/messages \\
      -H "Content-Type: application/json" \\
      -d '{"senderId":"user123","receiverId":"user456","text":"Hey!"}'
    """
    data = request.get_json(silent=True) or {}

    sender_id   = (data.get("senderId")   or "").strip()
    receiver_id = (data.get("receiverId") or "").strip()
    text        = (data.get("text")        or "").strip()

    if not sender_id:
        return jsonify({"error": "senderId is required"}), 400
    if not receiver_id:
        return jsonify({"error": "receiverId is required"}), 400
    if not text:
        return jsonify({"error": "text is required"}), 400

    msg_id = str(uuid.uuid4())
    msg = {
        "id":         msg_id,
        "senderId":   sender_id,
        "receiverId": receiver_id,
        "text":       text,
        "status":     "sent",
        "timestamp":  _now_ms(),
        "created_at": _now_iso(),
    }

    try:
        db.collection("messages").document(msg_id).set(msg)
    except Exception as e:
        log.error("send_message failed: %s", e)
        return jsonify({"error": str(e)}), 500

    log.info("Message sent: %s → %s  (%d chars)", sender_id, receiver_id, len(text))
    return jsonify(msg), 201


@chat_bp.route("/messages", methods=["GET"])
def get_messages():
    """
    GET /api/chat/messages?u1=<id>&u2=<id>&after=<timestamp_ms>&limit=50
    Poll messages between two users.
    This replaces onSnapshot / subscribeToMessages in chat.ts

    Params:
      u1    — current user ID (required)
      u2    — other user ID  (required)
      after — only return messages with timestamp > this value (default 0)
              Use the timestamp of the last message you have for efficient polling.
      limit — max messages to return (default 50, max 200)

    Response:
    {
      "messages": [
        {
          "id":         "msg-uuid",
          "senderId":   "user123",
          "receiverId": "user456",
          "text":       "Hey!",
          "status":     "seen",
          "timestamp":  1743505200000,
          "created_at": "2026-04-01T10:00:00+00:00"
        }, ...
      ],
      "count": 3
    }

    curl "http://127.0.0.1:5000/api/chat/messages?u1=user123&u2=user456"
    curl "http://127.0.0.1:5000/api/chat/messages?u1=user123&u2=user456&after=1743505200000"
    """
    u1    = request.args.get("u1", "").strip()
    u2    = request.args.get("u2", "").strip()
    after = int(request.args.get("after", 0))
    limit = min(int(request.args.get("limit", 50)), 200)

    if not u1 or not u2:
        return jsonify({"error": "u1 and u2 are required"}), 400

    try:
        # Fetch recent messages ordered by timestamp
        docs = (
            db.collection("messages")
              .order_by("timestamp", direction="ASCENDING")
              .limit(500)
              .stream()
        )

        messages = []
        mark_seen_ids = []

        for d in docs:
            m = d.to_dict()
            ts = m.get("timestamp", 0)

            # Filter: only between these two users
            is_between = (
                (m.get("senderId") == u1 and m.get("receiverId") == u2) or
                (m.get("senderId") == u2 and m.get("receiverId") == u1)
            )
            if not is_between:
                continue

            # Filter: only newer than `after`
            if ts <= after:
                continue

            messages.append(m)

            # Auto-mark as seen: message sent TO u1 (u1 is now reading)
            if m.get("receiverId") == u1 and m.get("status") != "seen":
                mark_seen_ids.append(m["id"])

        # Apply limit after filtering
        messages = messages[-limit:]

        # Batch mark as seen
        for msg_id in mark_seen_ids:
            try:
                db.collection("messages").document(msg_id).update({"status": "seen"})
            except Exception as e:
                log.warning("Failed to mark seen for %s: %s", msg_id, e)

        if mark_seen_ids:
            # Update status in the returned messages list too
            for m in messages:
                if m["id"] in mark_seen_ids:
                    m["status"] = "seen"

    except Exception as e:
        log.error("get_messages failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"messages": messages, "count": len(messages)}), 200


@chat_bp.route("/messages/seen", methods=["PATCH"])
def mark_seen():
    """
    PATCH /api/chat/messages/seen
    Mark all messages from a sender to a receiver as seen.
    Call this when the chat window for that conversation is opened.

    Body:
    {
      "receiverId": "user123",   // the user who is NOW reading
      "senderId":   "user456"    // whose messages to mark as seen
    }

    Response:
    {
      "marked": 4,
      "receiverId": "user123",
      "senderId":   "user456"
    }

    curl -X PATCH http://127.0.0.1:5000/api/chat/messages/seen \\
      -H "Content-Type: application/json" \\
      -d '{"receiverId":"user123","senderId":"user456"}'
    """
    data        = request.get_json(silent=True) or {}
    receiver_id = (data.get("receiverId") or "").strip()
    sender_id   = (data.get("senderId")   or "").strip()

    if not receiver_id or not sender_id:
        return jsonify({"error": "receiverId and senderId are required"}), 400

    try:
        docs = (
            db.collection("messages")
              .order_by("timestamp", direction="ASCENDING")
              .limit(500)
              .stream()
        )

        marked = 0
        for d in docs:
            m = d.to_dict()
            if (
                m.get("receiverId") == receiver_id
                and m.get("senderId") == sender_id
                and m.get("status") != "seen"
            ):
                try:
                    db.collection("messages").document(m["id"]).update({"status": "seen"})
                    marked += 1
                except Exception as e:
                    log.warning("mark_seen update failed for %s: %s", m["id"], e)

    except Exception as e:
        log.error("mark_seen failed: %s", e)
        return jsonify({"error": str(e)}), 500

    log.info("Marked %d messages as seen (%s → %s)", marked, sender_id, receiver_id)
    return jsonify({"marked": marked, "receiverId": receiver_id, "senderId": sender_id}), 200


# ═══════════════════════════════════════════════════════════════════
# CONVERSATIONS SIDEBAR
# ═══════════════════════════════════════════════════════════════════

@chat_bp.route("/conversations", methods=["GET"])
def get_conversations():
    """
    GET /api/chat/conversations?uid=<current_user_id>
    Returns all users this person has ever chatted with,
    showing the last message and unread count per conversation.
    Use this to populate the sidebar conversation list.

    Response:
    {
      "conversations": [
        {
          "user": {
            "id":       "user456",
            "username": "arjun",
            "avatar":   "https://...",
            "online":   false
          },
          "last_message": {
            "text":      "See you tomorrow!",
            "timestamp": 1743505200000,
            "senderId":  "user456"
          },
          "unread_count": 2
        }, ...
      ]
    }

    curl "http://127.0.0.1:5000/api/chat/conversations?uid=user123"
    """
    uid = request.args.get("uid", "").strip()
    if not uid:
        return jsonify({"error": "uid is required"}), 400

    try:
        docs = (
            db.collection("messages")
              .order_by("timestamp", direction="ASCENDING")
              .limit(1000)
              .stream()
        )

        # Build conversation map: other_user_id → {last_msg, unread}
        conv_map: dict = {}

        for d in docs:
            m = d.to_dict()
            sender   = m.get("senderId", "")
            receiver = m.get("receiverId", "")

            # Only messages involving this user
            if sender != uid and receiver != uid:
                continue

            other_uid = receiver if sender == uid else sender

            if other_uid not in conv_map:
                conv_map[other_uid] = {"last_message": None, "unread_count": 0}

            conv_map[other_uid]["last_message"] = {
                "text":      m.get("text", ""),
                "timestamp": m.get("timestamp", 0),
                "senderId":  sender,
            }

            # Count unread: messages sent TO uid that are not seen
            if receiver == uid and m.get("status") != "seen":
                conv_map[other_uid]["unread_count"] += 1

        # Fetch user profiles for each conversation partner
        conversations = []
        for other_uid, conv_data in conv_map.items():
            user_doc = db.collection("chat_users").document(other_uid).get()
            if user_doc.exists:
                u = user_doc.to_dict()
                user_profile = {
                    "id":        u.get("id"),
                    "username":  u.get("username"),
                    "avatar":    u.get("avatar", ""),
                    "online":    u.get("online", False),
                }
            else:
                user_profile = {
                    "id":       other_uid,
                    "username": other_uid[:8],
                    "avatar":   f"https://i.pravatar.cc/150?u={other_uid}",
                    "online":   False,
                }
            conversations.append({
                "user":         user_profile,
                "last_message": conv_data["last_message"],
                "unread_count": conv_data["unread_count"],
            })

        # Sort by last message timestamp descending
        conversations.sort(
            key=lambda c: c["last_message"]["timestamp"] if c["last_message"] else 0,
            reverse=True,
        )

    except Exception as e:
        log.error("get_conversations failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"conversations": conversations}), 200


# ═══════════════════════════════════════════════════════════════════
# UNREAD COUNT
# ═══════════════════════════════════════════════════════════════════

@chat_bp.route("/unread", methods=["GET"])
def get_unread():
    """
    GET /api/chat/unread?uid=<current_user_id>
    Returns total unread count + per-sender breakdown.
    Use this for the notification badge on the chat icon.

    Response:
    {
      "total_unread": 5,
      "by_sender": {
        "user456": 3,
        "user789": 2
      }
    }

    curl "http://127.0.0.1:5000/api/chat/unread?uid=user123"
    """
    uid = request.args.get("uid", "").strip()
    if not uid:
        return jsonify({"error": "uid is required"}), 400

    try:
        docs = (
            db.collection("messages")
              .order_by("timestamp", direction="ASCENDING")
              .limit(500)
              .stream()
        )

        by_sender: dict[str, int] = {}
        for d in docs:
            m = d.to_dict()
            if m.get("receiverId") == uid and m.get("status") != "seen":
                sender = m.get("senderId", "unknown")
                by_sender[sender] = by_sender.get(sender, 0) + 1

    except Exception as e:
        log.error("get_unread failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "total_unread": sum(by_sender.values()),
        "by_sender":    by_sender,
    }), 200