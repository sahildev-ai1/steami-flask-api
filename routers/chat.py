"""
Chat router — /api/chat/...
ALL endpoints require authentication (user | mod | admin).
Anonymous users cannot send or read messages.
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from mongodb_client import db
# ALL chat routes are locked — require any logged-in user
from auth import require_auth, get_uid

log = logging.getLogger(__name__)
# Setting dependency at router level means every endpoint inherits it
router = APIRouter(dependencies=[Depends(require_auth)])


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Request bodies ─────────────────────────────────────────────────────────

class UpsertUserBody(BaseModel):
    id:       str
    username: str
    avatar:   str = ""
    email:    str = ""

class SendMessageBody(BaseModel):
    senderId:   str
    receiverId: str
    text:       str

class MarkSeenBody(BaseModel):
    receiverId: str
    senderId:   str


# ══════════════════════════════════════════════════════════════════════════
# USERS
# ══════════════════════════════════════════════════════════════════════════

@router.post("/users")
def upsert_user(body: UpsertUserBody):
    """Create or update a user profile. Call on login/register."""
    uid = body.id.strip()
    if not uid:
        raise HTTPException(400, detail="id is required")
    if not body.username.strip():
        raise HTTPException(400, detail="username is required")

    profile = {
        "id":        uid,
        "username":  body.username.strip(),
        "avatar":    body.avatar or f"https://i.pravatar.cc/150?u={uid}",
        "email":     body.email,
        "online":    True,
        "last_seen": _now_iso(),
    }
    try:
        db.collection("chat_users").document(uid).set(profile, merge=True)
    except Exception as e:
        log.error("upsert_user failed: %s", e)
        raise HTTPException(500, detail=str(e))

    log.info("chat_user upserted: %s (%s)", uid, profile["username"])
    return profile


@router.get("/users")
def get_users(
    uid: str = Query(""),
    q:   str = Query(""),
):
    """Get all users except the current user. Optional ?q= username search."""
    current_uid = uid.strip()
    search_q    = q.strip().lower()
    try:
        docs  = db.collection("chat_users").limit(200).stream()
        users = []
        for d in docs:
            u = d.to_dict()
            if u.get("id") == current_uid:
                continue
            if search_q and search_q not in u.get("username", "").lower():
                continue
            users.append({
                "id":        u.get("id"),
                "username":  u.get("username"),
                "avatar":    u.get("avatar", ""),
                "online":    u.get("online", False),
                "last_seen": u.get("last_seen", ""),
            })
    except Exception as e:
        log.error("get_users failed: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"users": users}


@router.get("/users/{uid}")
def get_user(uid: str):
    """Get a single user profile."""
    doc = db.collection("chat_users").document(uid).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found")
    u = doc.to_dict()
    return {
        "id":        u.get("id"),
        "username":  u.get("username"),
        "avatar":    u.get("avatar", ""),
        "online":    u.get("online", False),
        "last_seen": u.get("last_seen", ""),
    }


# ══════════════════════════════════════════════════════════════════════════
# MESSAGES
# ══════════════════════════════════════════════════════════════════════════

@router.post("/messages", status_code=201)
def send_message(body: SendMessageBody):
    """Send a message from one user to another."""
    sender_id   = body.senderId.strip()
    receiver_id = body.receiverId.strip()
    text        = body.text.strip()

    if not sender_id:
        raise HTTPException(400, detail="senderId is required")
    if not receiver_id:
        raise HTTPException(400, detail="receiverId is required")
    if not text:
        raise HTTPException(400, detail="text is required")

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
        raise HTTPException(500, detail=str(e))

    log.info("Message sent: %s → %s (%d chars)", sender_id, receiver_id, len(text))
    return msg


@router.get("/messages")
def get_messages(
    u1:    str = Query(...),
    u2:    str = Query(...),
    after: int = Query(0),
    limit: int = Query(50, le=200),
):
    """
    Poll messages between two users.
    Pass after=<timestamp_ms> to get only new messages since last poll.
    Auto-marks received messages as seen.
    """
    try:
        docs = (
            db.collection("messages")
              .order_by("timestamp", direction="ASCENDING")
              .limit(500)
              .stream()
        )
        messages       = []
        mark_seen_ids  = []

        for d in docs:
            m  = d.to_dict()
            ts = m.get("timestamp", 0)
            is_between = (
                (m.get("senderId") == u1 and m.get("receiverId") == u2) or
                (m.get("senderId") == u2 and m.get("receiverId") == u1)
            )
            if not is_between:
                continue
            if ts <= after:
                continue
            messages.append(m)
            if m.get("receiverId") == u1 and m.get("status") != "seen":
                mark_seen_ids.append(m["id"])

        messages = messages[-limit:]

        for msg_id in mark_seen_ids:
            try:
                db.collection("messages").document(msg_id).update({"status": "seen"})
            except Exception as e:
                log.warning("Failed to mark seen for %s: %s", msg_id, e)

        if mark_seen_ids:
            seen_set = set(mark_seen_ids)
            for m in messages:
                if m["id"] in seen_set:
                    m["status"] = "seen"

    except Exception as e:
        log.error("get_messages failed: %s", e)
        raise HTTPException(500, detail=str(e))

    return {"messages": messages, "count": len(messages)}


@router.patch("/messages/seen")
def mark_seen(body: MarkSeenBody):
    """Mark all messages from senderId → receiverId as seen."""
    receiver_id = body.receiverId.strip()
    sender_id   = body.senderId.strip()
    if not receiver_id or not sender_id:
        raise HTTPException(400, detail="receiverId and senderId are required")

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
        raise HTTPException(500, detail=str(e))

    log.info("Marked %d messages as seen (%s → %s)", marked, sender_id, receiver_id)
    return {"marked": marked, "receiverId": receiver_id, "senderId": sender_id}


# ══════════════════════════════════════════════════════════════════════════
# CONVERSATIONS SIDEBAR
# ══════════════════════════════════════════════════════════════════════════

@router.get("/conversations")
def get_conversations(uid: str = Query(...)):
    """
    Get all conversations for the sidebar.
    Returns each chat partner with last message and unread count.
    """
    uid = uid.strip()
    if not uid:
        raise HTTPException(400, detail="uid is required")

    try:
        docs = (
            db.collection("messages")
              .order_by("timestamp", direction="ASCENDING")
              .limit(1000)
              .stream()
        )
        conv_map: dict = {}
        for d in docs:
            m        = d.to_dict()
            sender   = m.get("senderId", "")
            receiver = m.get("receiverId", "")
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
            if receiver == uid and m.get("status") != "seen":
                conv_map[other_uid]["unread_count"] += 1

        conversations = []
        for other_uid, conv_data in conv_map.items():
            user_doc = db.collection("chat_users").document(other_uid).get()
            if user_doc.exists:
                u = user_doc.to_dict()
                user_profile = {
                    "id": u.get("id"), "username": u.get("username"),
                    "avatar": u.get("avatar", ""), "online": u.get("online", False),
                }
            else:
                user_profile = {
                    "id": other_uid, "username": other_uid[:8],
                    "avatar": f"https://i.pravatar.cc/150?u={other_uid}", "online": False,
                }
            conversations.append({
                "user":         user_profile,
                "last_message": conv_data["last_message"],
                "unread_count": conv_data["unread_count"],
            })

        conversations.sort(
            key=lambda c: c["last_message"]["timestamp"] if c["last_message"] else 0,
            reverse=True,
        )
    except Exception as e:
        log.error("get_conversations failed: %s", e)
        raise HTTPException(500, detail=str(e))

    return {"conversations": conversations}


# ══════════════════════════════════════════════════════════════════════════
# UNREAD COUNT
# ══════════════════════════════════════════════════════════════════════════

@router.get("/unread")
def get_unread(uid: str = Query(...)):
    """Total unread count + per-sender breakdown. Use for notification badge."""
    uid = uid.strip()
    if not uid:
        raise HTTPException(400, detail="uid is required")

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
        raise HTTPException(500, detail=str(e))

    return {"total_unread": sum(by_sender.values()), "by_sender": by_sender}