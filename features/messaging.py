import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, session, jsonify
from werkzeug.utils import secure_filename
from flask_socketio import join_room, emit

from database import db_helper

# =========================
# Socket helpers / presence
# =========================

online_users = {}  # user_id -> sid

def dm_room(a: int, b: int) -> str:
    return f"dm_{min(a,b)}_{max(a,b)}"


# =========================
# DM streak helpers
# Rule (SGT): Each SGT day resets at 00:00 Asia/Singapore.
# Streak increases ONLY when BOTH users send >=2 messages EACH that day.
# =========================

_SGT = ZoneInfo("Asia/Singapore")

def _pair_key(a: int, b: int) -> str:
    x, y = (a, b) if a < b else (b, a)
    return f"{x}:{y}"

def _today_sgt_from_ts(ts_str: str | None) -> str:
    """
    ts_str: "YYYY-MM-DD HH:MM:SS" in server local time.
    We'll interpret it as local time and convert to SGT date.
    If parse fails, fall back to SGT 'now'.
    """
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        # treat dt as naive local; assume it's already in SGT if your server runs in SGT.
        # If server timezone is not SGT, set it explicitly in your app.
        dt = dt.replace(tzinfo=_SGT)
        return dt.astimezone(_SGT).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(_SGT).strftime("%Y-%m-%d")

def _ensure_dm_streak_table():
    """
    Creates (or migrates) dm_streak_state table.
    """
    conn = db_helper.get_connection()
    try:
        # 🔥 Automatically drop the old corrupted table if 'room' exists instead of 'pair_key'
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(dm_streak_state)").fetchall()]
        if "room" in cols and "pair_key" not in cols:
            conn.execute("DROP TABLE dm_streak_state")
            conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_streak_state (
                pair_key TEXT PRIMARY KEY,
                a_id INTEGER NOT NULL,
                b_id INTEGER NOT NULL,
                last_credit_day TEXT,
                day TEXT,
                a_count INTEGER NOT NULL DEFAULT 0,
                b_count INTEGER NOT NULL DEFAULT 0,
                streak INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

        # --- Migration ---
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(dm_streak_state)").fetchall()]
        def addcol(name, sqltype):
            if name not in cols2:
                conn.execute(f"ALTER TABLE dm_streak_state ADD COLUMN {name} {sqltype}")
        addcol("last_credit_day", "TEXT")
        addcol("day", "TEXT")
        addcol("a_count", "INTEGER NOT NULL DEFAULT 0")
        addcol("b_count", "INTEGER NOT NULL DEFAULT 0")
        addcol("streak", "INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()

def get_dm_streak(a: int, b: int) -> int:
    _ensure_dm_streak_table()
    pk = _pair_key(a, b)
    conn = db_helper.get_connection()
    try:
        row = conn.execute(
            "SELECT streak FROM dm_streak_state WHERE pair_key=?",
            (pk,)
        ).fetchone()
        return int(row["streak"]) if row else 0
    finally:
        conn.close()

def update_dm_streak_on_send(sender_id: int, receiver_id: int, ts: str) -> int:
    """
    Call this after saving a DM message.
    Rule: streak increases when BOTH users reached >=2 messages in the SAME SGT day.
    Returns the new streak value for the pair (may be unchanged).
    """
    _ensure_dm_streak_table()

    pk = _pair_key(sender_id, receiver_id)
    a_id, b_id = (sender_id, receiver_id) if sender_id < receiver_id else (receiver_id, sender_id)
    day = _today_sgt_from_ts(ts)

    conn = db_helper.get_connection()
    try:
        row = conn.execute(
            "SELECT last_credit_day, day, a_count, b_count, streak FROM dm_streak_state WHERE pair_key=?",
            (pk,)
        ).fetchone()

        if not row:
            conn.execute(
                "INSERT INTO dm_streak_state(pair_key,a_id,b_id,last_credit_day,day,a_count,b_count,streak) VALUES (?,?,?,?,?,?,?,0)",
                (pk, a_id, b_id, None, day, 0, 0)
            )
            conn.commit()
            row = {"last_credit_day": None, "day": day, "a_count": 0, "b_count": 0, "streak": 0}

        last_credit_day = row["last_credit_day"]
        cur_day         = row["day"]
        a_count         = int(row["a_count"] or 0)
        b_count         = int(row["b_count"] or 0)
        streak          = int(row["streak"] or 0)

        # If day changed, reset counts for new day
        if cur_day != day:
            cur_day = day
            a_count = 0
            b_count = 0

        # Increment sender's per-day count
        if sender_id == a_id:
            a_count += 1
        else:
            b_count += 1

        # Check requirement met
        if a_count >= 1 and b_count >= 1:
            # only credit once per day
            if last_credit_day != day:
                try:
                    yday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception:
                    yday = None

                if last_credit_day == yday and streak > 0:
                    streak += 1
                else:
                    streak = 1

                last_credit_day = day
                
                # 🔥 TRIGGER SEED LOGIC: Check if this new streak helps earn a seed
                if streak >= 1:
                    check_and_award_streak_seed(sender_id)
                    # We also check for the receiver in case they just hit their 3rd streak too
                    check_and_award_streak_seed(receiver_id)

        conn.execute(
            "UPDATE dm_streak_state SET last_credit_day=?, day=?, a_count=?, b_count=?, streak=? WHERE pair_key=?",
            (last_credit_day, cur_day, a_count, b_count, streak, pk)
        )
        conn.commit()
        return streak
    finally:
        conn.close()

def get_dm_streak_map(me_id: int, other_ids: list[int]) -> dict[int, int]:
    _ensure_dm_streak_table()
    out = {}
    for oid in other_ids:
        try:
            out[int(oid)] = get_dm_streak(me_id, int(oid))
        except Exception:
            out[int(oid)] = 0
    return out

def check_and_award_streak_seed(user_id):
    """
    Checks if the user has 3 or more separate DM streaks of 10 days or higher.
    If so, awards 1 flower seed.
    """
    _ensure_dm_streak_table()
    conn = db_helper.get_connection()
    try:
        # Count how many separate chats have a streak >= 10
        row = conn.execute("""
            SELECT COUNT(*) as count FROM dm_streak_state 
            WHERE (a_id = ? OR b_id = ?) AND streak >= 1
        """, (user_id, user_id)).fetchone()
        
        count = row["count"] if row else 0

        if count >= 3:
            # Update user inventory (flower seeds)
            conn.execute("""
                UPDATE user_inventory 
                SET seed_flower = seed_flower + 1 
                WHERE user_id = ?
            """, (user_id,))
            
            # Log the achievement in garden history
            db_helper.log_garden_history(
                user_id, 
                'flower', 
                'Earned 1x Flower Seed for 3 separate 1-day streaks!', 
                1
            )
            conn.commit()
            return True
        return False
    except Exception as e:
        print(f"⚠️ SEED AWARD ERROR: {e}")
        return False
    finally:
        conn.close()
def init_messaging(socketio):

    @socketio.on("presence_join")
    def presence_join(_data=None):
        if "user_id" not in session:
            return
        uid = int(session["user_id"])
        join_room(f"user_{uid}")
        online_users[uid] = request.sid
        socketio.emit("online_list", list(online_users.keys()))

    @socketio.on("disconnect")
    def on_disconnect():
        dead = None
        for uid, sid in list(online_users.items()):
            if sid == request.sid:
                dead = uid
                break
        if dead is not None:
            online_users.pop(dead, None)
            socketio.emit("online_list", list(online_users.keys()))

    @socketio.on("dm_join")
    def dm_join(data):
        if "user_id" not in session: 
            return

        region_name = (data or {}).get("region_name")
        if region_name:
            region_name = str(region_name).strip().lower()   # ✅ normalize
            join_room(f"region_{region_name}")
            return

        # (Keep your existing DM code below for private chats)
        me = int(session["user_id"])
        other = int((data or {}).get("other_id") or 0)
        join_room(dm_room(me, other))

        # 🔥 send current streak for this pair to the joining client
        try:
            s = get_dm_streak(me, other)
            emit("streak_update", {"peer_id": other, "streak": s}, room=request.sid)
        except Exception:
            pass

        emit("online_list", list(online_users.keys()))

        # ✅ FIX: Do NOT auto-mark messages as read here.
        # The client will send dm_mark_read explicitly only when the user
        # is actually looking at the chat (tab visible). This prevents
        # premature blue ticks when the user isn't on the page.
        # We still send the badge count so unread badge is accurate.
        try:
            unread_ids = db_helper.get_unread_ids(other, me)
            if unread_ids:
                emit("badge_update", {"from_id": other, "count": len(unread_ids)}, room=request.sid)
            
            # Also emit already-read message IDs so sender's ticks turn blue correctly
            conn = db_helper.get_connection()
            try:
                all_read_ids = [r["id"] for r in conn.execute("""
                    SELECT id FROM messages
                    WHERE sender_id=? AND receiver_id=?
                    AND region_name IS NULL AND read_at IS NOT NULL
                """, (other, me)).fetchall()]
            finally:
                conn.close()

            if all_read_ids:
                emit("dm_read", {"ids": all_read_ids}, room=dm_room(me, other))
        except Exception:
            pass

    @socketio.on("dm_mark_read")
    def dm_mark_read(data):
        if "user_id" not in session:
            return
        me = int(session["user_id"])
        sender_id = int((data or {}).get("sender_id") or 0)
        mark_all = (data or {}).get("mark_all", False)
        msg_id = (data or {}).get("msg_id")
        if not sender_id:
            return
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Get unread IDs BEFORE marking so we can emit them
            unread_ids = db_helper.get_unread_ids(sender_id, me)
            if unread_ids or mark_all:
                db_helper.mark_read_for_chat(sender_id, me, ts)
                # Emit to the DM room so sender sees blue ticks
                if unread_ids:
                    emit("dm_read", {"ids": unread_ids}, room=dm_room(me, sender_id))
                # Clear unread badge for me
                emit("badge_update", {"from_id": sender_id, "count": 0}, room=request.sid)
        except Exception:
            pass


    @socketio.on("dm_send_message")
    def dm_send_message(data):
            if "user_id" not in session:
                return

            sender_id = int(session["user_id"])
            
            # 🌍 Handle both Community (region) and Private (receiver) messages
            region_name = (data or {}).get("region_name")
            if region_name:
                region_name = str(region_name).strip().lower()
            receiver_id = int((data or {}).get("receiver_id") or 0) if not region_name else None
            
            message_type = ((data or {}).get("message_type") or "text").strip().lower()
            message_text = ((data or {}).get("message_text") or "").strip()
            media_path = ((data or {}).get("media_path") or "").strip()
            audio_path = ((data or {}).get("audio_path") or "").strip()
            file_name = ((data or {}).get("file_name") or "").strip()
            temp_key  = (data or {}).get("_tempKey")

            # ✅ FIXED VALIDATION: Must have either a region OR a receiver
            if not region_name and not receiver_id:
                return
            if message_type == "text" and not message_text:
                return

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 1. Save to Database (passes region_name if it exists)
            msg_id = db_helper.save_message(
                sender_id=sender_id,
                receiver_id=receiver_id,
                region_name=region_name,
                message_type=message_type,
                message_text=message_text,
                media_path=media_path,
                audio_path=audio_path,
                file_name=file_name,
                timestamp=ts
            )

            # 2. Define Payload fully before emitting
            sender_info = _get_user_basic(sender_id)
            payload = {
                "id": msg_id,
                "sender_id": sender_id,
                "sender_username": sender_info.get("username", ""),
                "sender_pfp": sender_info.get("pfp", "profile_pic.png"),
                "receiver_id": receiver_id,
                "region_name": region_name,
                "message_type": message_type,
                "message_text": message_text,
                "media_path": media_path,
                "audio_path": audio_path,
                "file_name": file_name,
                "timestamp": ts,
                "delivered_at": None,
                "read_at": None,
                "_tempKey": temp_key
            }

            # 3. Calculate Streaks & Rewards (Private DMs Only)
            new_streak = 0
            if not region_name and receiver_id:
                try:
                    new_streak = update_dm_streak_on_send(sender_id, receiver_id, ts)
                    if new_streak == 1:
                        socketio.emit("reward_notification", {
                            "message": "Congratulations! You earned a Flower Seed for your streaks! 🌱"
                        }, to=request.sid)
                except Exception as e:
                    print(f"⚠️ STREAK ERROR: {e}")

            # 4. SEND/BROADCAST MESSAGE
            if region_name:
                # ✅ BROADCAST: Everyone in the region room sees this
                emit("dm_receive_message", payload, room=f"region_{region_name}")
            else:
                # ✅ PRIVATE DM logic
                emit("dm_receive_message", payload, room=request.sid)
                
                receiver_sid = online_users.get(receiver_id)
                if receiver_sid:
                    socketio.emit("dm_receive_message", payload, to=receiver_sid)
                    
                    # Emit preview update to both users' inbox views
                    try:
                        def _preview_for(msg_type, msg_text, sender_name):
                            if msg_type == "image":
                                return f"{sender_name}: 📷 Image"
                            elif msg_type == "audio":
                                return f"{sender_name}: 🎵 Audio"
                            else:
                                t = (msg_text or "")[:30]
                                return f"{sender_name}: {t}"

                        preview_str = _preview_for(message_type, message_text, sender_info.get("username",""))
                        # Tell receiver's inbox to update preview for this sender
                        socketio.emit("inbox_preview_update", {"peer_id": sender_id, "preview": preview_str}, to=receiver_sid)
                        # Tell sender's own inbox to update preview for receiver
                        socketio.emit("inbox_preview_update", {"peer_id": receiver_id, "preview": preview_str}, to=request.sid)
                    except Exception:
                        pass

                    try:
                        db_helper.mark_delivered(msg_id, ts)
                        emit("dm_delivered", {"id": msg_id, "delivered_at": ts}, room=request.sid)
                        
                        unread_ids = db_helper.get_unread_ids(sender_id, receiver_id)
                        socketio.emit("badge_update", {"from_id": sender_id, "count": len(unread_ids)}, to=receiver_sid)
                        
                        emit("streak_update", {"peer_id": receiver_id, "streak": new_streak}, room=request.sid)
                        socketio.emit("streak_update", {"peer_id": sender_id, "streak": new_streak}, to=receiver_sid)
                    except Exception:
                        pass
    @socketio.on("dm_edit_message")
    def dm_edit_message(data):
        if "user_id" not in session:
            return
        sender_id  = int(session["user_id"])
        msg_id     = int((data or {}).get("msg_id") or 0)
        new_text   = ((data or {}).get("new_text") or "").strip()
        receiver_id = int((data or {}).get("receiver_id") or 0)

        if not msg_id or not new_text or not receiver_id:
            return

        ok = db_helper.edit_message(msg_id, sender_id, new_text)
        if ok:
            emit("dm_message_edited", {"msg_id": msg_id, "new_text": new_text}, room=dm_room(sender_id, receiver_id))

    @socketio.on("dm_delete_message")
    def dm_delete_message(data):
        if "user_id" not in session:
            return
        sender_id   = int(session["user_id"])
        msg_id      = int((data or {}).get("msg_id") or 0)
        receiver_id = int((data or {}).get("receiver_id") or 0)

        if not msg_id or not receiver_id:
            return

        msg_type = db_helper.delete_message(msg_id, sender_id)
        if msg_type:
            emit("dm_message_deleted", {"msg_id": msg_id}, room=dm_room(sender_id, receiver_id))

    @socketio.on("group_edit_message")
    def group_edit_message(data):
        if "user_id" not in session:
            return
        sender_id   = int(session["user_id"])
        msg_id      = int((data or {}).get("msg_id") or 0)
        new_text    = ((data or {}).get("new_text") or "").strip()
        region_name = str((data or {}).get("region_name") or "").strip().lower()

        if not msg_id or not new_text or not region_name:
            return

        ok = db_helper.edit_message(msg_id, sender_id, new_text)
        if ok:
            emit("group_message_edited", {"msg_id": msg_id, "new_text": new_text},
                 room=f"region_{region_name}")

    @socketio.on("group_delete_message")
    def group_delete_message(data):
        if "user_id" not in session:
            return
        sender_id   = int(session["user_id"])
        msg_id      = int((data or {}).get("msg_id") or 0)
        region_name = str((data or {}).get("region_name") or "").strip().lower()

        if not msg_id or not region_name:
            return

        msg_type = db_helper.delete_message(msg_id, sender_id)
        if msg_type:
            emit("group_message_deleted", {"msg_id": msg_id},
                 room=f"region_{region_name}")


# =========================
# Blueprint + HTTP routes
# =========================

messaging_bp = Blueprint("messaging", __name__, url_prefix="/messages")

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "..", "static")

UPLOAD_IMG_FOLDER   = os.path.normpath(os.path.join(_STATIC, "uploads", "chat_images"))
UPLOAD_AUDIO_FOLDER = os.path.normpath(os.path.join(_STATIC, "uploads", "chat_audio"))
os.makedirs(UPLOAD_IMG_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_AUDIO_FOLDER, exist_ok=True)

ALLOWED_IMG = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_AUDIO = {"mp3", "wav", "m4a", "ogg", "webm"}

def _ext_ok(filename, allowed):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in allowed

def _my_uid():
    return int(session.get("user_id", 1))

def _get_user_basic(uid: int):
    conn = db_helper.get_connection()
    try:
        row = conn.execute("""
            SELECT u.id, u.username, u.role,
                   COALESCE(p.profile_pic, 'profile_pic.png') AS pfp
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE u.id = ?
        """, (uid,)).fetchone()
    finally:
        conn.close()

    if not row:
        return {"id": uid, "username": f"user{uid}", "role": "unknown", "pfp": "profile_pic.png"}

    return {"id": row["id"], "username": row["username"] or f"user{uid}", "role": (row["role"] or "unknown"), "pfp": row["pfp"] or "profile_pic.png"}

def _get_people_for_sidebar(me_id: int, my_role: str):
    conn = db_helper.get_connection()
    try:
        rows = conn.execute("""
            SELECT u.id, u.username, u.role,
                   COALESCE(p.profile_pic, 'profile_pic.png') AS pfp,
                   lm.timestamp AS last_msg,
                   lm.sender_id AS last_sender_id,
                   lm.message_type AS last_msg_type,
                   lm.content AS last_msg_text,
                   su.username AS last_sender_username
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            LEFT JOIN (
                SELECT m.*, ROW_NUMBER() OVER (
                    PARTITION BY
                        CASE WHEN m.sender_id < m.receiver_id THEN m.sender_id ELSE m.receiver_id END,
                        CASE WHEN m.sender_id < m.receiver_id THEN m.receiver_id ELSE m.sender_id END
                    ORDER BY m.timestamp DESC
                ) AS rn
                FROM messages m
                WHERE m.region_name IS NULL
                  AND (m.sender_id = ? OR m.receiver_id = ?)
            ) lm ON (
                (lm.sender_id = u.id AND lm.receiver_id = ?) OR
                (lm.sender_id = ? AND lm.receiver_id = u.id)
            ) AND lm.rn = 1
            LEFT JOIN users su ON su.id = lm.sender_id
            WHERE u.id != ?
            GROUP BY u.id
            ORDER BY lm.timestamp DESC NULLS LAST, u.username ASC
        """, (me_id, me_id, me_id, me_id, me_id)).fetchall()
    except Exception:
        # Fallback: simple query without window functions (older SQLite)
        conn2 = db_helper.get_connection()
        try:
            rows = conn2.execute("""
                SELECT u.id, u.username, u.role,
                       COALESCE(p.profile_pic, 'profile_pic.png') AS pfp,
                       MAX(m.timestamp) AS last_msg,
                       NULL AS last_sender_id,
                       NULL AS last_msg_type,
                       NULL AS last_msg_text,
                       NULL AS last_sender_username
                FROM users u
                LEFT JOIN profiles p ON p.user_id = u.id
                LEFT JOIN messages m
                    ON m.region_name IS NULL
                    AND (
                        (m.sender_id = u.id   AND m.receiver_id = ?)
                     OR (m.sender_id = ?     AND m.receiver_id = u.id)
                    )
                WHERE u.id != ?
                GROUP BY u.id
                ORDER BY last_msg DESC, u.username ASC
            """, (me_id, me_id, me_id)).fetchall()
        finally:
            conn2.close()
    finally:
        conn.close()

    people = []
    my_role = (my_role or "").lower().strip()
    for r in rows:
        role = (r["role"] or "").lower().strip()
        last_msg = r["last_msg"] or ""
        # Build preview text: "SenderName: Message" or "SenderName: 📷 Image"
        last_preview = ""
        try:
            sender_name = r["last_sender_username"] or ""
            msg_type = (r["last_msg_type"] or "text").lower()
            if sender_name and last_msg:
                if msg_type == "image":
                    last_preview = f"{sender_name}: 📷 Image"
                elif msg_type == "audio":
                    last_preview = f"{sender_name}: 🎵 Audio"
                else:
                    msg_text = (r["last_msg_text"] or "")[:30]
                    last_preview = f"{sender_name}: {msg_text}"
        except Exception:
            last_preview = ""

        if my_role == "youth" and role == "senior":
            people.append({"id": r["id"], "username": r["username"], "role": role, "pfp": r["pfp"], "last_msg": last_msg, "last_preview": last_preview})
        elif my_role == "senior" and role == "youth":
            people.append({"id": r["id"], "username": r["username"], "role": role, "pfp": r["pfp"], "last_msg": last_msg, "last_preview": last_preview})
    return people

@messaging_bp.route("/")
def inbox():
    uid = _my_uid()
    me = _get_user_basic(uid)
    people = _get_people_for_sidebar(uid, me.get("role"))

    try:
        unread_counts = db_helper.get_unread_counts(uid)
    except Exception:
        unread_counts = {}

    for p in people:
        p["unread"] = unread_counts.get(p["id"], 0)

    # attach streaks for each conversation peer
    try:
        streak_map = get_dm_streak_map(uid, [p["id"] for p in people])
    except Exception:
        streak_map = {}
    for p in people:
        p["streak"] = int(streak_map.get(p["id"], 0) or 0)

    return render_template("messages/inbox.html", me=me, people=people)

@messaging_bp.route("/chat/<int:other_id>")
def chat(other_id):
    uid = _my_uid()
    me = _get_user_basic(uid)
    other = _get_user_basic(other_id)
    people = _get_people_for_sidebar(uid, me.get("role"))

    # mark read
    try:
        db_helper.mark_read(sender_id=other_id, receiver_id=uid)
    except Exception:
        pass

    try:
        unread_counts = db_helper.get_unread_counts(uid)
    except Exception:
        unread_counts = {}

    for p in people:
        p["unread"] = unread_counts.get(p["id"], 0) if p["id"] != other_id else 0

    # attach streaks for sidebar list
    try:
        streak_map = get_dm_streak_map(uid, [p["id"] for p in people])
    except Exception:
        streak_map = {}
    for p in people:
        p["streak"] = int(streak_map.get(p["id"], 0) or 0)

    # current chat streak
    current_streak = 0
    try:
        current_streak = get_dm_streak(uid, other_id)
    except Exception:
        current_streak = 0

    try:
        messages = db_helper.get_chat_history(uid, other_id)
    except Exception as e:
        print(f"❌ get_chat_history error (uid={uid}, other={other_id}): {e}")
        messages = []

    return render_template(
        "messages/chat.html",
        me=me,
        conversations=people,
        current_user_id=uid,
        other_user=other,
        messages=messages,
        active_id=other_id,
        streak=current_streak,
    )

@messaging_bp.route("/clear_chat/<int:other_id>", methods=["POST"])
def clear_chat(other_id):
    uid = _my_uid()
    try:
        db_helper.clear_chat_for_user(uid, other_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@messaging_bp.route("/upload/image", methods=["POST"])
def upload_image():
    f = request.files.get("image")
    if not f or f.filename == "":
        return jsonify({"ok": False, "error": "No file"}), 400
    if not _ext_ok(f.filename, ALLOWED_IMG):
        return jsonify({"ok": False, "error": "Invalid image type"}), 400

    filename = secure_filename(f.filename)
    unique = f"{uuid.uuid4().hex}_{filename}"
    save_path = os.path.join(UPLOAD_IMG_FOLDER, unique)
    f.save(save_path)

    rel = f"uploads/chat_images/{unique}"
    return jsonify({"ok": True, "media_path": rel})

@messaging_bp.route("/upload/audio", methods=["POST"])
def upload_audio():
    f = request.files.get("audio")
    if not f or f.filename == "":
        return jsonify({"ok": False, "error": "No file"}), 400
    if not _ext_ok(f.filename, ALLOWED_AUDIO):
        return jsonify({"ok": False, "error": "Invalid audio type"}), 400

    filename = secure_filename(f.filename)
    unique = f"{uuid.uuid4().hex}_{filename}"
    save_path = os.path.join(UPLOAD_AUDIO_FOLDER, unique)
    f.save(save_path)

    rel = f"uploads/chat_audio/{unique}"
    return jsonify({"ok": True, "audio_path": rel})

@messaging_bp.route("/group/<region>")
def group_chat(region):
    uid = _my_uid()
    me = _get_user_basic(uid)
    people = _get_people_for_sidebar(uid, me.get("role"))

    # ✅ Attach streaks (THIS WAS MISSING)
    try:
        streak_map = get_dm_streak_map(uid, [p["id"] for p in people])
    except Exception:
        streak_map = {}

    for p in people:
        p["streak"] = int(streak_map.get(p["id"], 0) or 0)

    groups = [
        {"region": "north", "name": "North Region Group"},
        {"region": "east", "name": "East Region Group"},
        {"region": "central", "name": "Central Region Group"},
        {"region": "west", "name": "West Region Group"},
    ]

    region = (region or "").strip().lower()

    try:
        conn = db_helper.get_connection()
        rows = conn.execute("""
            SELECT m.id, m.sender_id,
                   COALESCE(m.message_type, 'text') AS message_type,
                   m.content AS message_text,
                   m.media_path, m.audio_path, m.file_name,
                   m.timestamp,
                   CASE WHEN m.deleted_at IS NOT NULL THEN 1 ELSE 0 END AS is_deleted,
                   CASE WHEN m.edited_at  IS NOT NULL THEN 1 ELSE 0 END AS is_edited,
                   u.username AS sender_username,
                   COALESCE(p.profile_pic, 'profile_pic.png') AS sender_pfp
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            LEFT JOIN profiles p ON p.user_id = m.sender_id
            WHERE m.region_name = ?
            ORDER BY m.timestamp ASC
        """, (region,)).fetchall()
        messages = [dict(r) for r in rows]
    except Exception as e:
        print("❌ region history error:", e)
        messages = []
    finally:
        conn.close()

    return render_template(
        "messages/group_chat.html",
        me=me,
        people=people,
        groups=groups,
        region=region,
        active_id=None,
        active_group=region,
        messages=messages,
    )




@messaging_bp.route("/api/unread_count")
def api_unread_count():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"total": 0})
    try:
        counts = db_helper.get_unread_counts(int(uid))
        total = sum(counts.values()) if counts else 0
        return jsonify({"total": total})
    except Exception:
        return jsonify({"total": 0})


@messaging_bp.route("/api/report_user", methods=["POST"])
def api_report_user():
    me_id = session.get("user_id")
    if not me_id:
        return jsonify(ok=False, error="Not logged in"), 401

    data = request.get_json() or {}
    reported_user_id = int(data.get("reported_user_id") or 0)
    reason = (data.get("reason") or "").strip().lower()
    details = (data.get("details") or "").strip()

    if not reported_user_id or not reason:
        return jsonify(ok=False, error="Missing fields"), 400

    if reported_user_id == int(me_id):
        return jsonify(ok=False, error="You cannot report yourself"), 400

    conn = db_helper.get_connection()
    try:
        # ✅ Block only if still pending/open
        dup = conn.execute("""
            SELECT id FROM user_reports
            WHERE reporter_id=? AND reported_user_id=? AND reason=?
              AND LOWER(COALESCE(status,'open')) IN ('open','pending')
            LIMIT 1
        """, (me_id, reported_user_id, reason)).fetchone()

        if dup:
            return jsonify(ok=False, error="You already submitted a similar report (pending admin review)."), 409

        conn.execute("""
    INSERT INTO user_reports (reporter_id, reported_user_id, reason, details, created_at, status)
    VALUES (?, ?, ?, ?, datetime('now','localtime'), 'open')
""", (me_id, reported_user_id, reason, details))
        conn.commit()

        return jsonify(ok=True, message="Report submitted for admin approval.")

    except Exception as e:
        conn.rollback()
        print("❌ api_report_user error:", e)
        return jsonify(ok=False, error="Server error"), 500
    finally:
        conn.close()



