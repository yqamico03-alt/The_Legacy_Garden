import os
import re
import requests
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, flash
from flask import request 
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from database import db_helper

load_dotenv()

# ============================================================
# 0) CONFIG
# ============================================================
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
SIGHTENGINE_USER = os.getenv("SIGHTENGINE_API_USER")
SIGHTENGINE_SECRET = os.getenv("SIGHTENGINE_API_SECRET")

story_bp = Blueprint("story", __name__, url_prefix="/story")
UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ============================================================
# 1) VALIDATION (SMARTER & SAFER)
# ============================================================
# 1. BANNED WORDS (Strict list)
STRICT_BANNED = {
    "fk", "fuk", "fck",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy", "cunt",
    "nigger", "faggot",
    "cb", "knn", "ccb", "kanina",
    "ass", "sex", "porn", "bastard", "ccb", "knnn","kns", "ccbknn",
    "stupid", "dumb"
}

#banned words
BANNED_WORDS_FILE = os.getenv("BANNED_WORDS_FILE", "banned_words.txt")

def load_banned_words_file(path: str):
    """
    Loads extra banned words from a txt file.
    - Ignores empty lines and lines starting with #.
    - Returns a set of lowercase words.
    """
    out = set()
    try:
        if not path or not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                w = (line or "").strip().lower()
                if not w or w.startswith("#"):
                    continue
                out.add(w)
    except Exception as e:
        print("⚠️ banned words file load error:", e)
    return out

# merge words into STRICT_BANNED
STRICT_BANNED |= load_banned_words_file(BANNED_WORDS_FILE)

# 2. KEYBOARD SMASH PATTERNS (The "Random String" Logic)
SMASH_PATTERNS = ["qwerty", "asdfgh", "zxcvbn", "poiuy", "mnbvc", "123456", "hjkl"]

# --- EXTRA PROFANITY / HARASSMENT DETECTION (handles obfuscation like fking, f*ck, fucckk) ---
LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s"
})

BAD_PHRASE_PATTERNS = [
    r"\bgo\s+and\s+die\b",
    r"\bkill\s+yourself\b",
    r"\bkys\b",
    r"\bgo\s+die\b",
]

BAD_ROOTS = {
    "fuck", "shit", "bitch", "asshole", "dick", "pussy", "cunt",
    "nigger", "faggot",
    "kanina", "knn", "ccb", "cb",
    "porn", "sex" , "fucking", "fking", "fk" , 
}

def _tokenize_normalized(text: str):
    """Lowercase, leetspeak normalize, remove non-alnum, return tokens."""
    if not text:
        return []
    lowered = text.lower().translate(LEET_MAP)
    tokens = re.split(r"[^a-z0-9]+", lowered)
    return [t for t in tokens if t]

def contains_bad_content(text: str) -> bool:
    """Catches profanities/harassment even when obfuscated."""
    if not text:
        return False

    low = text.lower()

    # 1) Harmful phrases
    for pat in BAD_PHRASE_PATTERNS:
        if re.search(pat, low):
            return True

    # 2) Token-based detection
    toks = _tokenize_normalized(text)

    for t in toks:
        # allow 'ass' only as standalone
        if t == "ass":
            return True

        if t in BAD_ROOTS:
            return True

        # common obfuscations: fking/fkng/fck/fuk/fucckk/fuuuck
        if re.fullmatch(r"f+u*c+k+(i+n+g+)?", t):
            return True
        if re.fullmatch(r"f+k+(i+n+g+)?", t):
            return True
        if re.fullmatch(r"sh+i+t+", t):
            return True
        if re.fullmatch(r"bi+t+ch+", t):
            return True

        # containment (e.g., 'fuuuckyou')
        for root in BAD_ROOTS:
            if root in t and len(t) <= 30:
                return True

    return False

def looks_like_gibberish(text: str) -> bool:
    """
    Detects keyboard smashing without flagging real words like 'difficulty'.
    """
    if not text: return False
    
    words = text.split()
    for w in words:
        lower_w = w.lower()
        
   
        if re.search(r'(.)\1{4,}', lower_w):
            return True


        if re.search(r'[bcdfghjklmnpqrstvwxyz]{7,}', lower_w):
            return True
            

        for pattern in SMASH_PATTERNS:
            if pattern in lower_w:
                return True
   
        if len(w) > 35 and not w.startswith('http'):
            return True

    return False

def contains_bad_word(text: str) -> bool:
    """
    Checks for whole words only. 
    'Assignment' -> Safe. 'You are an ass' -> Caught.
    """
    
    tokens = re.split(r'[^a-zA-Z0-9]+', text.lower())
    
    for t in tokens:
        if t in STRICT_BANNED:
            return True
    return False

def check_local_validation(text: str, min_len: int):
    if not text or not text.strip(): return "Required."
    if len(text.strip()) < min_len: return f"Too short (min {min_len} chars)."
    
    if contains_bad_word(text) or contains_bad_content(text):
        return "Explicit content detected, please change."
        
    if looks_like_gibberish(text):
        return "Warning: random words or gibberish/keyboard detected."
        
    return None

# ============================================================
# 2) SIGHTENGINE API (FAIL-OPEN FOR DEMO)
# ============================================================
def sightengine_text_check(text: str) -> str:
    # If keys missing → pending (NOT approved)
    if not SIGHTENGINE_USER or not SIGHTENGINE_SECRET:
        print("⚠️ No API Keys -> Pending Admin Review")
        return "pending"

    try:
        r = requests.post(
            "https://api.sightengine.com/1.0/text/check.json",
            data={
                "text": text,
                "lang": "en",
                "mode": "standard",
                "api_user": SIGHTENGINE_USER,
                "api_secret": SIGHTENGINE_SECRET
            },
            timeout=3
        )
        data = r.json()

        # If API returns failure/quota/etc → pending (NOT approved)
        if data.get("status") == "failure":
            print(f"⚠️ API failure ({data.get('error')}) -> Pending Admin Review")
            return "pending"

        # Reject if clearly bad
        if data.get("sexual", 0) > 0.8 or data.get("hate", 0) > 0.8 or data.get("profanity", 0) > 0.9:
            return "rejected"

        return "approved"

    except Exception as e:
        # Timeout/connection error → pending (NOT approved)
        print(f"⚠️ Text API exception -> Pending Admin Review: {e}")
        return "pending"

def sightengine_image_check(path: str) -> str:
    if not SIGHTENGINE_USER or not SIGHTENGINE_SECRET:
        return "approved" 
    try:
        with open(path, "rb") as f:
            r = requests.post(
                "https://api.sightengine.com/1.0/check.json",
                files={"media": f},
                data={ "models": "nudity,wad", "api_user": SIGHTENGINE_USER, "api_secret": SIGHTENGINE_SECRET },
                timeout=5
            )
        data = r.json()
        
        if data.get("status") == "failure": return "approved"
        
        nudity = data.get("nudity", {})
        if (float(nudity.get("raw", 0)) > 0.6 or 
            float(nudity.get("partial", 0)) > 0.7 or 
            float(nudity.get("sexual_activity", 0)) > 0.6):
            return "rejected"
            
        return "approved"
    except:
        return "approved"

# sightengine api
@story_bp.route("/api/moderate-text", methods=["POST"])
def api_moderate_text():
    """
    Frontend live validation uses this.
    It runs:
    - check_local_validation()
    - sightengine_text_check()
    Returns:
      { ok: True, status: "approved" }
      { ok: True, status: "warning", msg: "Warning: ..." }
      { ok: False, status: "rejected", msg: "Explicit content detected..." }
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    min_len = int(data.get("min_len") or 2)

   
    err = check_local_validation(text, min_len=min_len)
    if err:
       
        if "Warning:" in err:
            return jsonify({"ok": False, "status": "warning", "msg": err})
        return jsonify({"ok": False, "status": "rejected", "msg": err})

  
    if sightengine_text_check(text) == "rejected":
        return jsonify({"ok": False, "status": "rejected", "msg": "Explicit content detected, please change."})

    return jsonify({"ok": True, "status": "approved"})

# ============================================================
# 3) ROUTES
# ============================================================
@story_bp.route("/")
def index():
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("login"))

    success = request.args.get("success")
    water = request.args.get("water", 0)

    me = db_helper.get_user_by_id(uid)
    if me is None:
        return redirect(url_for("login"))

    my_role = ((me.get("role") if isinstance(me, dict) else me["role"]) or "youth").lower().strip()
    # normalize directly
    if my_role.startswith("senior"):
        my_role = "senior"
    else:
        my_role = "youth"

    # ✅ IMPORTANT: pass MY role as POV (not other_role)
    stories = db_helper.get_stories_for_pov(my_role, uid)

    filtered = []
    for s in stories:
        s_uid = int(s.get("user_id") or 0)

        if s_uid == int(uid):
            filtered.append(s)
            continue

        author_role = (s.get("role") or "").strip().lower()   # ✅ from users u.role
        if author_role == my_role:
            continue  # block same generation

        filtered.append(s)

    stories = filtered

    stories = sorted(filtered, key=lambda s: (s.get("id") if hasattr(s, "get") else s["id"]) or 0, reverse=True)

    # ---- likes (unchanged) ----
    conn = db_helper.get_connection()
    try:
        liked_rows = conn.execute(
            "SELECT story_id FROM story_likes WHERE user_id = ?",
            (uid,)
        ).fetchall()
        liked_ids = {row["story_id"] if hasattr(row, "keys") else row[0] for row in liked_rows}

        count_rows = conn.execute(
            "SELECT story_id, COUNT(*) AS c FROM story_likes GROUP BY story_id"
        ).fetchall()
        like_map = {}
        for r in count_rows:
            story_id = r["story_id"] if hasattr(r, "keys") else r[0]
            c = r["c"] if hasattr(r, "keys") else r[1]
            like_map[story_id] = c
    finally:
        conn.close()

    for s in stories:
        sid = s.get("id") if hasattr(s, "get") else s["id"]
        if hasattr(s, "get"):
            s["user_liked"] = sid in liked_ids
            s["like_count"] = int(like_map.get(sid, 0))
            if s.get("comment_count") is None:
                s["comment_count"] = 0

    return render_template("story/index.html", stories=stories, success=success, water=water)

@story_bp.route("/manage")
def manage():
    uid = session.get("user_id", 1)
    active_tab = request.args.get("tab", "stories")

    stories = db_helper.get_user_stories(uid)
    drafts = db_helper.get_user_drafts(uid)

    return render_template(
        "story/manage.html",
        stories=stories,
        drafts=drafts,
        active_tab=active_tab
    )

@story_bp.route("/api/check-image", methods=["POST"])
def api_check_image():
    file = request.files.get("photo")
    if not file: return jsonify({"safe": False})
    
    tmp = os.path.join(UPLOAD_FOLDER, f"__tmp_{os.urandom(4).hex()}")
    file.save(tmp)
    status = sightengine_image_check(tmp)
    try: os.remove(tmp)
    except: pass
    
    return jsonify({"safe": status != "rejected"})

@story_bp.route("/create", methods=["GET", "POST"])
def create():
    if request.method == "POST":
        title = request.form.get("title", "")
        content = request.form.get("content", "")
        topic = request.form.get("topic", "")
        save_as = request.form.get("save_as")

        uid = session.get("user_id", 1)

# =====================================
# 📝 DRAFT MODE (light validation)
# =====================================
        if save_as == "draft":
            uid = session.get("user_id", 1)
            draft_id = request.form.get("draft_id")
            remove_image = (request.form.get("remove_image") == "1")

            # fetch existing draft (if editing)
            existing = None
            if draft_id:
                existing = db_helper.get_draft_by_id(int(draft_id))
                if not existing or int(existing["user_id"]) != int(uid):
                    return redirect(url_for("story.manage", tab="drafts"))

            # light validation for drafts (only if user typed something)
            if title.strip() and (contains_bad_word(title) or looks_like_gibberish(title) or contains_bad_content(title)):
                return render_template(
                    "story/create.html",
                    errors={"title": "Content not allowed."},
                    form_data=request.form,
                    draft_id=draft_id
                )

            if content.strip() and (contains_bad_word(content) or looks_like_gibberish(content) or contains_bad_content(content)):
                return render_template(
                    "story/create.html",
                    errors={"content": "Content not allowed."},
                    form_data=request.form,
                    draft_id=draft_id
                )

            # keep existing image unless removed
            img_name = existing["image_path"] if existing else None

            # remove existing image
            if remove_image and img_name:
                try:
                    os.remove(os.path.join(UPLOAD_FOLDER, img_name))
                except:
                    pass
                img_name = None

            # upload new image (only if no existing image)
            photo = request.files.get("photo")
            if photo and photo.filename:
                if img_name:
                    return render_template(
                        "story/create.html",
                        errors={"photo": "Remove the current image first before uploading a new one."},
                        form_data=request.form,
                        draft_id=draft_id
                    )

                tmp = os.path.join(UPLOAD_FOLDER, f"__tmp_{os.urandom(4).hex()}")
                photo.save(tmp)

                img_status = sightengine_image_check(tmp)
                if img_status == "approved":
                    img_name = f"{os.urandom(4).hex()}_{secure_filename(photo.filename)}"
                    os.rename(tmp, os.path.join(UPLOAD_FOLDER, img_name))
                else:
                    try:
                        os.remove(tmp)
                    except:
                        pass
                    flash("⚠️ Photo was not approved, so it was not saved with your draft.", "warning")

            # save draft
            if draft_id:
                db_helper.update_draft(
                    draft_id=int(draft_id),
                    user_id=uid,
                    title=title,
                    content=content,
                    topic=topic,
                    image_path=img_name
                )
            else:
                db_helper.create_draft(uid, title, content, topic, img_name)

            return redirect(url_for("story.manage", tab="drafts"))

                # =====================================
        # 🌱 PUBLISH MODE (strict validation)
        # =====================================

        errors = {}

        e = check_local_validation(title, 5)
        if e: errors["title"] = e

        e = check_local_validation(content, 20)
        if e: errors["content"] = e

        if not topic:
            errors["topic"] = "Choose a topic."

        if errors:
            return render_template("story/create.html", errors=errors, form_data=request.form)

        # 2. Text AI Check
        t_status = sightengine_text_check(title)
        c_status = sightengine_text_check(content)

        if "rejected" in (t_status, c_status):
            errors["content"] = "Explicit content detected, please change."
            return render_template("story/create.html", errors=errors, form_data=request.form)
        final_status = "approved"

        # 3. Image Logic
        img_name = None
        water = 3 
        photo = request.files.get("photo")
        remove_image = request.form.get("remove_image") == "1"

        tmp = None

        if photo and photo.filename:
            tmp = os.path.join(UPLOAD_FOLDER, f"__tmp_{os.urandom(4).hex()}")
            photo.save(tmp)
            img_status = sightengine_image_check(tmp)

            if img_status == "rejected":
                try: os.remove(tmp)
                except: pass
                img_name = None
                water = 3 # Text only
            else:
                img_name = f"{os.urandom(4).hex()}_{secure_filename(photo.filename)}"
                os.rename(tmp, os.path.join(UPLOAD_FOLDER, img_name))
                water = 5 

        # 4. Save
        me = db_helper.get_user_by_id(session.get("user_id", 1))
        poster_role = (me["role"] or "Youth").lower().strip()
        role_visibility = "senior" if "senior" in poster_role else "youth"

        db_helper.create_story(
            session.get("user_id", 1), title, content, topic, role_visibility, img_name, final_status
        )

        # 5. Success
        db_helper.add_water_reward(session.get("user_id", 1), water)
        
        draft_id = request.form.get("draft_id")
        if draft_id:
            try:
                db_helper.delete_draft(int(draft_id), session.get("user_id", 1))
            except Exception as e:
                print("Draft delete failed:", e)

        return redirect(url_for("story.index", success="True", water=water))


    return render_template("story/create.html", errors={}, form_data={})

@story_bp.route("/fix-roles")
def fix_roles():
    conn = db_helper.get_connection()
    conn.execute("UPDATE stories SET role_visibility = 'senior' WHERE user_id IN (SELECT id FROM users WHERE LOWER(role) = 'senior')")
    conn.execute("UPDATE stories SET role_visibility = 'youth' WHERE user_id IN (SELECT id FROM users WHERE LOWER(role) = 'youth')")
    conn.commit()
    conn.close()
    return "Fixed!"

@story_bp.route("/view/<int:story_id>")
def view_story(story_id):
    uid = session.get("user_id", 1)

    story = db_helper.get_story_by_id(story_id)
    if not story:
        return redirect(url_for("story.index"))

    story = dict(story)

    me = db_helper.get_user_by_id(uid)
    my_role = (me["role"] or "youth").lower().strip()
    other_role = "senior" if my_role == "youth" else "youth"

    author_role = (story.get("role_visibility") or "").lower().strip()
    author_id = int(story.get("user_id") or 0)

    # ✅ admins can view any story regardless of generation
    is_admin = my_role == "admin"

    # ✅ allow owner to view their own story
    # ✅ otherwise must be opposite generation
    if not is_admin and author_id != int(uid) and author_role != other_role:
        flash("You can only view stories from the other generation.", "danger")
        return redirect(url_for("story.index"))

    comments = db_helper.get_story_comments(story_id)

    conn = db_helper.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM story_likes WHERE story_id = ?",
            (story_id,)
        ).fetchone()
        like_count = row["c"] if hasattr(row, "keys") else row[0]

        existing = conn.execute(
            "SELECT 1 FROM story_likes WHERE story_id = ? AND user_id = ?",
            (story_id, uid)
        ).fetchone()

        story["like_count"] = int(like_count)
        story["user_liked"] = bool(existing)
    finally:
        conn.close()

    return render_template("story/viewmore.html", story=story, comments=comments)

# Drafts
@story_bp.route("/draft/save", methods=["POST"])
def save_draft():
    uid = session.get("user_id", 1)
    title = request.form.get("title","")
    content = request.form.get("content","")
    topic = request.form.get("topic","")
    db_helper.create_draft(uid, title=title, content=content, topic=topic, image_path=None)
    return jsonify({"ok": True})

@story_bp.route("/draft/<int:draft_id>/edit")
def edit_draft(draft_id):
    uid = session.get("user_id", 1)
    d = db_helper.get_draft_by_id(draft_id)
    if d and d["user_id"] == uid:
        return render_template("story/create.html", errors={}, form_data=d, draft_id=draft_id)
    return redirect(url_for("story.manage"))

@story_bp.route("/draft/<int:draft_id>/update", methods=["POST"])
def update_draft(draft_id):
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "msg": "Not logged in"}), 401

    title = request.form.get("title", "")
    content = request.form.get("content", "")
    topic = request.form.get("topic", "")

    d = db_helper.get_draft_by_id(draft_id)
    if not d or d["user_id"] != uid:
        return jsonify({"ok": False, "msg": "Draft not found"}), 404

    db_helper.update_draft(
        draft_id=draft_id,
        user_id=uid,
        title=title,
        content=content,
        topic=topic,
        image_path=d.get("image_path")
    )
    return jsonify({"ok": True})


@story_bp.route("/draft/<int:draft_id>/delete", methods=["POST"])
def delete_draft(draft_id):
    uid = session.get("user_id", 1)

 
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "msg": "Reason is required."})

    ok = db_helper.delete_draft(draft_id, uid)
    return jsonify({"ok": bool(ok)})


@story_bp.route("/api/banned-words")
def api_banned_words():
    return jsonify({"words": sorted(list(STRICT_BANNED))})

# ============================================================
# DELETE ROUTES
# ============================================================

@story_bp.route("/delete/<int:story_id>", methods=["POST"])
def delete_story(story_id):
 
    story = db_helper.get_story_by_id(story_id)
    uid = session.get("user_id", 1)


    if story and story['user_id'] == uid:
        db_helper.delete_story(story_id)
        flash("Story deleted successfully.", "success")
    else:
        flash("You cannot delete this story.", "danger")


    return redirect(url_for('story.manage'))

@story_bp.route("/report/<int:story_id>", methods=["POST"])
def report_story(story_id):
    uid = session.get("user_id", 1)
    reason = request.form.get("reason", "")
    
  
    if not reason:
        return jsonify({"ok": False, "msg": "Reason is required."})

 
    local_error = check_local_validation(reason, min_len=5)
    if local_error:
        return jsonify({"ok": False, "msg": local_error})

   
    ai_status = sightengine_text_check(reason)
    if ai_status == "rejected":
        return jsonify({"ok": False, "msg": "Please keep the report reason professional."})

   
    success = db_helper.report_story(story_id, uid, reason)
    
    return jsonify({"ok": success})

@story_bp.route("/like/<int:story_id>", methods=["POST"])
def like_story(story_id):
    uid = session.get("user_id", 1)
    conn = db_helper.get_connection()
    try:
     
        existing = conn.execute("SELECT id FROM story_likes WHERE story_id = ? AND user_id = ?", (story_id, uid)).fetchone()
        
        if existing:
            
            conn.execute("DELETE FROM story_likes WHERE story_id = ? AND user_id = ?", (story_id, uid))
        else:
            
            conn.execute("INSERT INTO story_likes (story_id, user_id) VALUES (?, ?)", (story_id, uid))
            
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('story.index'))

@story_bp.route("/<int:story_id>/like-toggle", methods=["POST"])
def like_toggle(story_id):
    uid = session.get("user_id", 1)
    conn = db_helper.get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM story_likes WHERE story_id = ? AND user_id = ?",
            (story_id, uid)
        ).fetchone()

        if existing:
            conn.execute(
                "DELETE FROM story_likes WHERE story_id = ? AND user_id = ?",
                (story_id, uid)
            )
            liked_now = False
        else:
            conn.execute(
                "INSERT INTO story_likes (story_id, user_id) VALUES (?, ?)",
                (story_id, uid)
            )
            liked_now = True

        conn.commit()

        
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM story_likes WHERE story_id = ?",
            (story_id,)
        ).fetchone()

        like_count = row["c"] if isinstance(row, dict) else row[0]

        return jsonify({
            "ok": True,
            "liked": liked_now,
            "like_count": like_count
        })
    except Exception as e:
        print("like-toggle error:", e)
        return jsonify({"ok": False}), 500
    finally:
        conn.close()


@story_bp.route("/profile/<int:user_id>")
def view_profile(user_id):
    uid = session.get("user_id", 1)

    user = db_helper.get_user_by_id(user_id)
    if not user:
        return redirect(url_for("story.index"))

    stories = db_helper.get_user_stories(user_id)

    return render_template(
        "story/profile.html",
        user=user,
        stories=stories,
        current_user_id=uid
    )

@story_bp.route("/comment/<int:story_id>", methods=["POST"])
def add_comment(story_id):
    uid = session.get("user_id", 1)
    content = (request.form.get("content") or "").strip()

    err = check_local_validation(content, min_len=2)
    if err:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "msg": err})
        flash(err, "danger")
        return redirect(url_for("story.view_story", story_id=story_id))

    # 2) Sightengine AI check
    if sightengine_text_check(content) == "rejected":
        msg = "Explicit content detected, please change."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "danger")
        return redirect(url_for("story.view_story", story_id=story_id))

    # 3) Anti-double-submit guard (optional but good)
    conn = db_helper.get_connection()
    try:
        last = conn.execute("""
            SELECT id, content, created_at
            FROM story_comments
            WHERE story_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (story_id, uid)).fetchone()

        if last and (last["content"] or "").strip() == content:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"ok": True, "deduped": True})
            return redirect(url_for("story.view_story", story_id=story_id))

    finally:
        conn.close()

    db_helper.add_comment(story_id, uid, content)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})

    return redirect(url_for("story.view_story", story_id=story_id))


@story_bp.route("/comment/report/<int:comment_id>", methods=["POST"])
def report_comment(comment_id):
    uid = session.get("user_id", 1)
    reason = (request.form.get("reason") or "").strip()

    # must exist
    conn = db_helper.get_connection()
    try:
        row = conn.execute("SELECT user_id FROM story_comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "msg": "Comment not found."}), 404

        # cannot report own comment
        if int(row["user_id"]) == int(uid):
            return jsonify({"ok": False, "msg": "You cannot report your own comment."}), 403
    finally:
        conn.close()

    # validate reason
    err = check_local_validation(reason, min_len=5)
    if err:
        return jsonify({"ok": False, "msg": err})

    if sightengine_text_check(reason) == "rejected":
        return jsonify({"ok": False, "msg": "Please keep the report reason professional."})

    result = db_helper.report_comment(comment_id, uid, reason)

    if isinstance(result, tuple):
        ok, msg = result
    else:
        ok, msg = bool(result), ("Reported." if result else "Failed to report.")

    return jsonify({"ok": ok, "msg": msg})


@story_bp.route("/comment/delete/<int:comment_id>", methods=["POST"])
def delete_comment(comment_id):
    uid = session.get("user_id", 1)

    result = db_helper.delete_comment(comment_id, uid)

    if isinstance(result, tuple):
        ok, msg = result
    else:
        ok, msg = bool(result), ("Deleted." if result else "Not allowed / not found.")

    return jsonify({"ok": ok, "msg": msg})


# editing feature
from flask import abort, render_template, request, redirect, url_for, session, flash
import os
from werkzeug.utils import secure_filename

@story_bp.route("/<int:story_id>/edit", methods=["GET", "POST"])
def edit_story(story_id):
    uid = session.get("user_id", 1)

    story = db_helper.get_story_by_id(story_id)
    if not story:
        abort(404)

    story = dict(story)

    # only allow owner
    if int(story.get("user_id", 0)) != int(uid):
        abort(403)

    if request.method == "POST":
        title = request.form.get("title", "")
        content = request.form.get("content", "")
        topic = request.form.get("topic", story.get("topic", ""))

        errors = {}
        e = check_local_validation(title, 5)
        if e: errors["title"] = e
        e = check_local_validation(content, 20)
        if e: errors["content"] = e
        if not topic:
            errors["topic"] = "Choose a topic."

        if errors:
            return render_template("story/edit_story.html", errors=errors, story=story)

        # (optional) re-moderate
        if "rejected" in (sightengine_text_check(title), sightengine_text_check(content)):
            errors["content"] = "Explicit content detected, please change."
            return render_template("story/edit_story.html", errors=errors, story=story)

        # image handling (keep old by default)
        img_name = story.get("image_path")
        remove_image = (request.form.get("remove_image") == "1")
        photo = request.files.get("photo")

        if remove_image and img_name:
            try:
                os.remove(os.path.join(UPLOAD_FOLDER, img_name))
            except:
                pass
            img_name = None

        if photo and photo.filename:
            tmp = os.path.join(UPLOAD_FOLDER, f"__tmp_{os.urandom(4).hex()}")
            photo.save(tmp)

            if sightengine_image_check(tmp) == "rejected":
                try: os.remove(tmp)
                except: pass
                flash("⚠️ Photo not approved, not saved.", "warning")
            else:
                new_name = f"{os.urandom(4).hex()}_{secure_filename(photo.filename)}"
                os.rename(tmp, os.path.join(UPLOAD_FOLDER, new_name))
                img_name = new_name

        # IMPORTANT: you must add this db_helper method
        db_helper.update_story(
            story_id=story_id,
            user_id=uid,
            title=title,
            content=content,
            topic=topic,
            image_path=img_name,
            status="approved"
        )

        flash("✅ Story updated.", "success")
        return redirect(url_for("story.manage"))

    return render_template("story/edit_story.html", errors={}, story=story)