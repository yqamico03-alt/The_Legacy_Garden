from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort
from database import db_helper
import sqlite3
import os
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash # <--- jiawen added this
from features.story import story_bp
from features.garden import garden_bp 
import re
import random
from flask_socketio import SocketIO, join_room, emit
from functools import wraps
from flask import Blueprint
import threading #yq added
from features.messaging import messaging_bp, init_messaging
from flask_babel import Babel, gettext as _
from flask_babel import format_timedelta
from flask_babel import force_locale
from dotenv import load_dotenv #pip install python-dotenv
from twilio.rest import Client #pip install twilio
from flask_dance.contrib.google import make_google_blueprint, google #Jiawen pip install flask-dance
from flask_mail import Mail, Message

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'features', '.env'), override=False) #Jiawen

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID") #Jiawen
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN") #Jiawen
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER") #Jiawen

client = Client(TWILIO_SID, TWILIO_AUTH) # Jiawen

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
# Initialize the Flask application
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# --- MAIL ---
# ===== MAIL CONFIG =====
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False

# Use environment variables (IMPORTANT)
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')

mail = Mail(app)

# ── Google OAuth ── Jiawen
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
google_bp = make_google_blueprint(
    client_id="164290718917-v0ps723j4mp1jlj08auug87q24pk5p2f.apps.googleusercontent.com",
    client_secret="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",
    scope=["openid", "https://www.googleapis.com/auth/userinfo.email",
           "https://www.googleapis.com/auth/userinfo.profile"],
    redirect_to="google_login_callback"
)
app.register_blueprint(google_bp, url_prefix="/login")

# Secret key is required to use 'session' (it encrypts the cookie)
app.secret_key = 'winx_club_secret'

def get_locale():
    lang = session.get("lang", "en")

    # accept multiple forms just in case
    if lang in ("zh_Hans", "zh-hans", "zh_hans", "zh"):
        return "zh_Hans"

    return "en"

babel = Babel(app, locale_selector=get_locale)

@app.route("/set_language", methods=["POST"])
def set_language():
    data = request.get_json(silent=True) or {}
    lang = data.get("lang", "en")

    if lang.lower().startswith("zh"):
        session["lang"] = "zh-CN"
    else:
        session["lang"] = "en"

    session.permanent = True
    return jsonify(ok=True)

app.register_blueprint(story_bp)
app.register_blueprint(garden_bp)
app.register_blueprint(messaging_bp)
init_messaging(socketio)  # ← registers all DM socket event handlers

# Setup upload folder
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


# ADMIN
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect(url_for("login"))

        if (session.get("role") or "").strip().lower() != "admin":
            flash("You do not have permission to access the admin page.")
            return redirect(url_for("home"))  # or url_for("story.index")
        return f(*args, **kwargs)
    return decorated_function

@app.route("/")
def landing():
    # Optional: if already logged in, send them to app
    if session.get("user_id"):
        return redirect(url_for("story.index"))   # change to your logged-in home
    return render_template("landing.html")

# --- HOME ROUTE ---
@app.route('/')
def home():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('story.index'))

@app.route("/google-callback") #Jiawen
def google_login_callback():
    if not google.authorized:
        flash("Google login failed.")
        return redirect(url_for("login"))  # changed from signup to login

    resp = google.get("/oauth2/v2/userinfo")
    info = resp.json()

    google_email = info.get("email", "")
    google_name  = info.get("name", "")

    conn = db_helper.get_connection()
    user = conn.execute(
        "SELECT u.id, u.username FROM users u JOIN profiles p ON u.id = p.user_id WHERE p.email = ?",
        (google_email,)
    ).fetchone()
    conn.close()

    if user:
        session["username"] = user["username"]
        session["user_id"] = user["id"]
        return redirect(url_for("home"))
    else:
        flash("No account found with this Google email. Please sign up first.")
        return redirect(url_for("signup"))
    
# --- SIGNUP ROUTE ---  (UPDATED)
def is_valid_password(password):
    return (
        len(password) >= 8 and
        re.search(r"[A-Z]", password) and
        re.search(r"[a-z]", password) and
        re.search(r"\d", password)
    )

@app.route('/signup', methods=['GET', 'POST']) #Jiawen
def signup():
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role     = request.form.get('role', '').strip()
        region   = request.form.get('region', '').strip()
        email    = request.form.get('email', '').strip()
        phone    = request.form.get('phone', '').strip()
        bio      = request.form.get('bio', '').strip()
        no_email = request.form.get('no_email', '0').strip()
        otp_input       = request.form.get('otp', '').strip()
        email_otp_input = request.form.get('email_otp', '').strip()
        action          = request.form.get('action', '').strip()

        # Save email OTP input to session so it persists after phone OTP submission
        if email_otp_input:
            session['saved_email_otp'] = email_otp_input
            session.modified = True

        # ── VERIFY EMAIL BUTTON CLICKED → just send email OTP ────────────────
        if action == 'verify_email':
            if not email:
                flash("Please enter an email address.")
                return render_template('profile/signup.html')

            if not name or not username or not password or not role or not region:
                flash("Please fill in all required fields before verifying email.")
                return render_template('profile/signup.html')

            if not is_valid_password(password):
                flash(
                    "Password must be at least 8 characters long and include "
                    "an uppercase letter, a lowercase letter, and a number."
                )
                return render_template('profile/signup.html')

            session['signup_data'] = {
                'name':           name,
                'username':       username,
                'password':       generate_password_hash(password),
                'role':           role,
                'region':         region,
                'email':          email,
                'no_email':       False,
                'phone':          phone,
                'bio':            bio,
                'email_verified': False,
            }

            try:
                send_email_otp(email, purpose="signup")
                session['signup_data']['email_otp'] = True
                session.modified = True
                flash("A verification code has been sent to your email.")
                print(f"Email OTP sent to: {email}")
            except Exception as e:
                flash(f"Failed to send email OTP: {str(e)}")
                print(f"Email OTP error: {str(e)}")

            return render_template('profile/signup.html',
                                   show_email_otp=True,
                                   form_data=session.get('signup_data', {}),
                                   saved_email_otp=session.get('saved_email_otp', ''))

        # ── STEP 3: Email verified → verify phone OTP ────────────────────────
        if ('signup_data' in session
                and (session['signup_data'].get('email_verified') or session['signup_data'].get('no_email'))
                and otp_input):

            data = session['signup_data']
            if otp_input == data.get('otp'):
                session.pop('saved_email_otp', None)
                return _create_account(data)
            else:
                flash("Invalid phone OTP. Please try again.")
                return render_template('profile/signup.html',
                                       show_otp=True,
                                       show_email_otp=True,
                                       form_data=session.get('signup_data', {}),
                                       saved_email_otp=session.get('saved_email_otp', ''))

        # ── STEP 2: Verify email OTP → then send phone OTP ───────────────────
        if ('signup_data' in session
                and 'email_otp' in session['signup_data']
                and email_otp_input):

            data = session['signup_data']
            stored_code = session.get('email_otp_code')
            expiry      = session.get('email_otp_expiry', '2000-01-01')

            if (email_otp_input == stored_code and
                    datetime.now() < datetime.fromisoformat(expiry)):

                data['email_verified'] = True
                session.modified = True

                # Send phone OTP
                otp = str(random.randint(100000, 999999))
                data['otp'] = otp
                session.modified = True

                try:
                    client.messages.create(
                        body=f"Your Legacy Garden OTP is {otp}",
                        from_=TWILIO_PHONE,
                        to="+65" + data['phone']
                    )
                    flash("Email verified! An OTP has been sent to your phone.")
                except Exception as e:
                    flash(f"Email verified but failed to send phone OTP: {str(e)}")

                return render_template('profile/signup.html',
                                       show_otp=True,
                                       show_email_otp=True,
                                       form_data=session.get('signup_data', {}),
                                       saved_email_otp=session.get('saved_email_otp', ''))
            else:
                flash("Invalid or expired email OTP. Please try again.")
                return render_template('profile/signup.html',
                                       show_email_otp=True,
                                       form_data=session.get('signup_data', {}),
                                       saved_email_otp=session.get('saved_email_otp', ''))

        # ── Re-submission while waiting for email OTP (edge case) ────────────
        if 'signup_data' in session and 'email_otp' in session['signup_data']:
            return render_template('profile/signup.html',
                                   show_email_otp=True,
                                   form_data=session.get('signup_data', {}),
                                   saved_email_otp=session.get('saved_email_otp', ''))

        # ── STEP 1: First submission → validate + send OTP ───────────────────
        if not name or not username or not password or not role or not region or not phone:
            flash("Please fill in all required fields.")
            return render_template('profile/signup.html')

        if no_email != '1' and not email:
            flash("Please enter an email or tick 'I don't have an email address'.")
            return render_template('profile/signup.html')

        # Use stored password if placeholder (means already hashed in session)
        if password == 'placeholder' and 'signup_data' in session:
            hashed_password = session['signup_data']['password']
        else:
            if not is_valid_password(password):
                flash(
                    "Password must be at least 8 characters long and include "
                    "an uppercase letter, a lowercase letter, and a number."
                )
                return render_template('profile/signup.html')
            hashed_password = generate_password_hash(password)

        session['signup_data'] = {
            'name':           name,
            'username':       username,
            'password':       hashed_password,
            'role':           role,
            'region':         region,
            'email':          email if no_email != '1' else '',
            'no_email':       no_email == '1',
            'phone':          phone,
            'bio':            bio,
            'email_verified': False,
        }

        # No email → skip email OTP, send phone OTP directly
        if no_email == '1':
            otp = str(random.randint(100000, 999999))
            session['signup_data']['otp'] = otp
            try:
                client.messages.create(
                    body=f"Your Legacy Garden OTP is {otp}",
                    from_=TWILIO_PHONE,
                    to="+65" + phone
                )
                flash("OTP sent to your phone. Please enter it below.")
            except Exception as e:
                flash(f"Failed to send OTP: {str(e)}")
            return render_template('profile/signup.html',
                                   show_otp=True,
                                   form_data=session.get('signup_data', {}),
                                   saved_email_otp=session.get('saved_email_otp', ''))

        # Has email but didn't click verify email button — show email OTP
        try:
            send_email_otp(email, purpose="signup")
            session['signup_data']['email_otp'] = True
            session.modified = True
            flash("A verification code has been sent to your email.")
        except Exception as e:
            flash(f"Failed to send email OTP: {str(e)}")

        return render_template('profile/signup.html',
                               show_email_otp=True,
                               form_data=session.get('signup_data', {}),
                               saved_email_otp=session.get('saved_email_otp', ''))

    return render_template('profile/signup.html')


def _create_account(data):
    """Helper: insert user + profile into DB then redirect to login."""
    conn = db_helper.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            (data['username'], data['password'], data['role'])
        )
        user_id = cur.lastrowid
        conn.execute(
            "INSERT INTO profiles (user_id, name, region, email, phone, bio) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, data['name'], data['region'], data['email'], data['phone'], data['bio'])
        )
        conn.commit()
        db_helper.add_notice(
            username=data['username'],
            region=data['region'],
            emoji="🧑‍🤝‍🧑",
            message=_(
                "New Member: <b>%(user)s</b> just joined the %(region)s community today!",
                user=data['username'],
                region=_(data['region'])
            )
        )
        flash("Account created successfully! Please log in.")
        session.pop('signup_data', None)
        session.pop('saved_email_otp', None)
        return redirect(url_for('login'))

    except sqlite3.IntegrityError:
        flash("Username already exists. Please choose another.")
        return render_template('profile/signup.html',
                               form_data=session.get('signup_data', {}),
                               saved_email_otp=session.get('saved_email_otp', ''))
    finally:
        conn.close()


@app.route('/resend_otp', methods=['POST']) #Jiawen
def resend_otp():
    data = session.get('signup_data')
    if not data:
        return {'error': 'Session expired. Please sign up again.'}, 400

    new_otp = str(random.randint(100000, 999999))
    session['signup_data']['otp'] = new_otp
    session.modified = True

    try:
        client.messages.create(
            body=f"Your Legacy Garden OTP is {new_otp}",
            from_=TWILIO_PHONE,
            to="+65" + data['phone']
        )
        return {'message': 'OTP resent to your phone.'}, 200
    except Exception as e:
        return {'error': f'Failed to resend OTP: {str(e)}'}, 500


@app.route('/resend_email_otp', methods=['POST']) #Jiawen
def resend_email_otp():
    data = session.get('signup_data')
    if not data or not data.get('email'):
        return {'error': 'Session expired or no email on file.'}, 400

    try:
        send_email_otp(data['email'], purpose="signup")
        return {'message': 'OTP resent to your email.'}, 200
    except Exception as e:
        return {'error': f'Failed to resend OTP: {str(e)}'}, 500

# --- LOGIN ROUTE ---   (UPDATED)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_input = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        # 1. Try to find the user in the database
        user = db_helper.get_user_by_login(login_input)

        # fel added
        user = dict(user) if user else None
        # to here

        if not user:
            flash("Username or email not found.")
        else:
            # 2. Use check_password_hash because the DB now has scrambled text
            if check_password_hash(user['password'], password):

                # fel added
                # ✅ BAN CHECK HERE (before setting session)
                if (user.get("status") or "").lower() == "banned":
                    flash("Your account is temporarily suspended. Don’t worry — our support team can help you. Please contact the administrator for assistance.", "danger")
                    return redirect(url_for("login"))

                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']

                return redirect(url_for('story.index')) 

            else:
                flash("Incorrect password.")
                
    return render_template('profile/login.html')


# --- PROFILE ROUTE ---   (UPDATED)
@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    uid = session['user_id']
    conn = db_helper.get_connection()
    try:
        # 1) Get user (always exists)
        u = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (uid,)).fetchone()
        if not u:
            session.clear()
            return redirect(url_for('login'))

        # 2) Get profile (might not exist)
        p = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (uid,)).fetchone()

        # 3) If missing profile row, create a default one (so admin won’t get kicked)
        if not p:
            conn.execute("""
                INSERT INTO profiles (user_id, name, region, email, bio, profile_pic)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                uid,
                u["username"],     # default name
                "Unknown",         # default region
                "",                # default email (or put "admin@example.com" if you want)
                "",                # default bio
                "profile_pic.png"  # default pic
            ))
            conn.commit()
            p = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (uid,)).fetchone()

        user_data = {**dict(p), "username": u["username"], "role": u["role"]}

        stories = conn.execute("""
            SELECT s.*
            FROM stories s
            WHERE s.user_id = ?
            ORDER BY s.created_at DESC
        """, (uid,)).fetchall()

        return render_template('profile/profile.html', profile=user_data, stories=stories, is_own_profile=True)

    finally:
        conn.close()

# --- EDIT PROFILE ROUTE --- (MERGED: role update + region change notices)
@app.route('/edit_profile', methods=['GET', 'POST']) # Jiawen
def edit_profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = db_helper.get_connection()

    try:
        if request.method == 'POST':
            new_name = request.form.get('name', '').strip()
            new_bio = request.form.get('bio', '').strip()
            new_region = request.form.get('region', '').strip()
            new_role = request.form.get('role', '').strip()

            file = request.files.get('profile_image')

            # Get old region BEFORE updating
            old_row = conn.execute(
                "SELECT region FROM profiles WHERE user_id = ?",
                (session['user_id'],)
            ).fetchone()
            old_region = (old_row["region"] if old_row else None) or "Unknown"

            # 1) Image Update Logic
            if file and file.filename:
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                conn.execute(
                    "UPDATE profiles SET profile_pic = ? WHERE user_id = ?",
                    (filename, session['user_id'])
                )

            # 2) Update Profiles Table
            conn.execute(
                "UPDATE profiles SET name = ?, bio = ?, region = ? WHERE user_id = ?",
                (new_name, new_bio, new_region, session['user_id'])
            )

            # 3) Update Users Table (Role)
            conn.execute(
                "UPDATE users SET role = ? WHERE id = ?",
                (new_role, session['user_id'])
            )

            conn.commit()

            if new_region and old_region != new_region:
                username = session.get("username", "Someone")

                db_helper.add_notice(
                    username=username,
                    region=old_region,
                    emoji="🚪",
                    message=_("<b>%(user)s</b> has left the %(region)s community.",
                            user=username,
                            region=_(old_region))
                )

                db_helper.add_notice(
                    username=username,
                    region=new_region,
                    emoji="🧑‍🤝‍🧑",
                    message=_("<b>%(user)s</b> just joined the %(region)s community today!",
                            user=username,
                            region=_(new_region))
                )

            # Keep session synced
            session['role'] = new_role

            flash("Profile updated!")
            return redirect(url_for('profile'))

        # -------- GET: load existing data --------
        user_data = conn.execute("""
            SELECT u.role, p.name, p.bio, p.region, p.profile_pic
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            WHERE u.id = ?
        """, (session['user_id'],)).fetchone()

        return render_template('profile/edit_profile.html', profile=user_data)

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}")
        return redirect(url_for('edit_profile'))

    finally:
        conn.close()



# --- REMOVE PHOTO ---
@app.route('/remove_photo', methods=['POST'])
def remove_photo():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = db_helper.get_connection()
    try:
        # Reset to default
        conn.execute("UPDATE profiles SET profile_pic = 'profile_pic.png' WHERE user_id = ?", 
                     (session['user_id'],))
        conn.commit()
        flash("Photo removed successfully!")
    except Exception as e:
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    # Redirect back to edit page
    return redirect(url_for('edit_profile'))

# --- SETTINGS ---
@app.route('/settings', methods=['GET', 'POST']) #Jiawen
def settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = db_helper.get_connection()
    
    if request.method == 'POST':
        allowed_fields = {
            'show_email', 'show_region', 'show_phone', 'community_visible',
            'notify_messages', 'notify_updates', 'notify_events',
            'notify_comm', 'notify_inapp'
        }

        # update ONLY fields that came in this request
        for field in request.form:
            if field in allowed_fields:
                value = int(request.form.get(field, 0))  # expects "0" or "1"
                conn.execute(
                    f"UPDATE profiles SET {field} = ? WHERE user_id = ?",
                    (value, session['user_id'])
                )

        conn.commit()
        return ("", 204)
        
    user_data = conn.execute("SELECT * FROM profiles WHERE user_id = ?", 
                             (session['user_id'],)).fetchone()
    conn.close()
    return render_template('profile/settings.html', settings=user_data)

# --- DELETE ACCOUNT ---
@app.route('/delete_account', methods=['POST'])
def delete_account():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    uid = session['user_id']
    conn = db_helper.get_connection()
    try:
        # 1) Get username + region BEFORE deleting
        row = conn.execute("""
            SELECT u.username, p.region
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            WHERE u.id = ?
        """, (uid,)).fetchone()

        if row:
            username = row["username"]
            region = row["region"] or "Unknown"

            # 2) Add "left" notice (this stays in DB)
            db_helper.add_notice(
                username=username,
                region=region,
                emoji="🚪",
                message=_("<b>%(user)s</b> has left the %(region)s community.",
                        user=username,
                        region=_(region))
            )

        # Delete from all tables to avoid errors for your team
        conn.execute("DELETE FROM profiles WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()

        session.clear()
        flash("Account deleted.")
    finally:
        conn.close()
    return redirect(url_for('signup'))

# --- MAIL ---
from flask_mail import Mail, Message
# ===== MAIL CONFIG =====
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False

# Use environment variables (IMPORTANT)
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')

mail = Mail(app)

# --- SEND VERIFICATION CODE (EMAIL OTP) ---
def send_verification_code(email):
    code = str(random.randint(100000, 999999))  # 6-digit code

    session['reset_code'] = code
    session['reset_code_expiry'] = (
        datetime.now() + timedelta(minutes=10)
    ).isoformat()
    session['reset_email'] = email

    msg = Message(
        subject="Your Password Reset Code – The Legacy Garden 🌱",
        recipients=[email]
    )

    msg.body = f"""
Hi,

Your password reset verification code is:

{code}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.
"""

    mail.send(msg)

# --- SEND VERIFICATION CODE (FOR SIGNUP) ---
def send_email_otp(email, purpose="verification"): #Jiawen
    """Generic email OTP sender — reuses existing mail setup."""
    code = str(random.randint(100000, 999999))

    session['email_otp_code'] = code
    session['email_otp_expiry'] = (
        datetime.now() + timedelta(minutes=10)
    ).isoformat()
    session['email_otp_address'] = email

    subjects = {
        "verification": "Your Email Verification Code – The Legacy Garden 🌱",
        "change_email": "Confirm Your New Email – The Legacy Garden 🌱",
        "change_password": "Your Password Change Code – The Legacy Garden 🌱",
        "signup": "Your Sign Up Verification Code – The Legacy Garden 🌱",
    }

    msg = Message(
        subject=subjects.get(purpose, "Your Verification Code – The Legacy Garden 🌱"),
        recipients=[email]
    )

    msg.body = f"""
Hi,

Your verification code is:

{code}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.
"""
    mail.send(msg)
    return code
    
# --- RESET PASSWORD ---   (UPDATED)
@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    # NO login check here — this is for logged-out users who forgot their password

    # Clear stale reset session data on fresh GET
    if request.method == 'GET':
        session.pop('reset_otp', None)
        session.pop('reset_user_id', None)
        session.pop('reset_username', None)
        session.pop('reset_otp_expiry', None)

    error = None

    if request.method == 'POST':
        action = request.form.get('action')

        # ===============================
        # STEP 1: SEND OTP TO PHONE
        # ===============================
        if action == 'send_code':
            login_input = request.form.get('login_input', '').strip()

            conn = db_helper.get_connection()
            try:
                user = conn.execute(
                    """
                    SELECT u.id, p.phone 
                    FROM users u 
                    JOIN profiles p ON u.id = p.user_id 
                    WHERE u.username = ?
                    """,
                    (login_input,)
                ).fetchone()
            finally:
                conn.close()

            if user and user['phone']:
                otp = str(random.randint(100000, 999999))
                session['reset_otp'] = otp
                session['reset_user_id'] = user['id']
                session['reset_username'] = login_input
                session['reset_otp_expiry'] = (datetime.now() + timedelta(minutes=10)).isoformat()

                try:
                    client.messages.create(
                        body=f"Your Legacy Garden OTP is {otp}",
                        from_=TWILIO_PHONE,
                        to="+65" + user['phone']
                    )
                    flash("OTP sent to your registered phone number.")
                except Exception as e:
                    flash(f"OTP (demo mode): {otp}")

                return render_template('profile/reset_password.html',  # ← fixed
                                       show_otp=True,
                                       username=login_input)
            else:
                error = "Username not found."

        # ===============================
        # STEP 2: VERIFY OTP + RESET
        # ===============================
        elif action == 'reset_password':
            entered_code = request.form.get('verification_code', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()

            if not session.get('reset_otp'):
                error = "Please request an OTP first."
            elif entered_code != session.get('reset_otp'):
                error = "Invalid OTP. Please try again."
            elif datetime.now() > datetime.fromisoformat(session.get('reset_otp_expiry')):
                error = "OTP has expired. Please request a new one."
            elif new_password != confirm_password:
                error = "Passwords do not match."
            elif not is_valid_password(new_password):
                error = "Password must be at least 8 characters long and include an uppercase letter, a lowercase letter, and a number."
            else:
                hashed_pw = generate_password_hash(new_password)
                conn = db_helper.get_connection()
                try:
                    conn.execute(
                        "UPDATE users SET password = ? WHERE id = ?",
                        (hashed_pw, session.get('reset_user_id'))
                    )
                    conn.commit()
                finally:
                    conn.close()

                session.pop('reset_otp', None)
                session.pop('reset_user_id', None)
                session.pop('reset_username', None)
                session.pop('reset_otp_expiry', None)
                flash("Password reset successfully! Please log in.")
                return redirect(url_for('login'))
    
    return render_template('profile/reset_password.html',  # ← fixed
                           error=error,
                           show_otp='reset_otp' in session and request.method == 'POST',
                           username=session.get('reset_username', ''))

# --- RESET PASSWORD FOR SETTINGS ---   (UPDATED)
@app.route('/reset_password_settings', methods=['GET', 'POST'])
def reset_password_settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    # Clear stale reset session data on fresh GET
    if request.method == 'GET':
        session.pop('reset_otp', None)
        session.pop('reset_user_id', None)
        session.pop('reset_username', None)
        session.pop('reset_otp_expiry', None)

    error = None

    if request.method == 'POST':
        action = request.form.get('action')

        # ===============================
        # STEP 1: SEND OTP TO PHONE
        # ===============================
        if action == 'send_code':
            login_input = request.form.get('login_input', '').strip()

            conn = db_helper.get_connection()
            try:
                user = conn.execute(
                    """
                    SELECT u.id, p.phone 
                    FROM users u 
                    JOIN profiles p ON u.id = p.user_id 
                    WHERE u.username = ?
                    """,
                    (login_input,)
                ).fetchone()
            finally:
                conn.close()

            if user and user['phone']:
                otp = str(random.randint(100000, 999999))
                session['reset_otp'] = otp
                session['reset_user_id'] = user['id']
                session['reset_username'] = login_input  # save username
                session['reset_otp_expiry'] = (datetime.now() + timedelta(minutes=10)).isoformat()

                try:
                    client.messages.create(
                        body=f"Your Legacy Garden OTP is {otp}",
                        from_=TWILIO_PHONE,
                        to="+65" + user['phone']
                    )
                    flash("OTP sent to your registered phone number.")
                except Exception as e:
                    flash(f"OTP (demo mode): {otp}")

                return render_template('profile/reset_password_settings.html',
                                    show_otp=True,
                                    username=login_input)  # pass username to lock the field
            else:
                error = "Username not found."

        # ===============================
        # STEP 2: VERIFY OTP + RESET
        # ===============================
        elif action == 'reset_password':
            entered_code = request.form.get('verification_code', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()

            if not session.get('reset_otp'):
                error = "Please request an OTP first."
            elif entered_code != session.get('reset_otp'):
                error = "Invalid OTP. Please try again."
            elif datetime.now() > datetime.fromisoformat(session.get('reset_otp_expiry')):
                error = "OTP has expired. Please request a new one."
            elif new_password != confirm_password:
                error = "Passwords do not match."
            elif not is_valid_password(new_password):
                error = "Password must be at least 8 characters long and include an uppercase letter, a lowercase letter, and a number."
            else:
                hashed_pw = generate_password_hash(new_password)
                conn = db_helper.get_connection()
                try:
                    conn.execute(
                        "UPDATE users SET password = ? WHERE id = ?",
                        (hashed_pw, session.get('reset_user_id'))
                    )
                    conn.commit()
                finally:
                    conn.close()

                session.pop('reset_otp', None)
                session.pop('reset_user_id', None)
                session.pop('reset_otp_expiry', None)
                flash("Password updated successfully!")
                return redirect(url_for('login'))
        
        return render_template('profile/reset_password_settings.html',
                        error=error,
                        show_otp='reset_otp' in session and request.method == 'POST',
                        username=session.get('reset_username', ''))

    # GET request — show the blank form
    return render_template('profile/reset_password_settings.html',
                           show_otp=False,
                           username=session.get('username', ''))

# --- CHANGE EMAIL --- recent jiawen
@app.route('/change_email', methods=['GET', 'POST'])
def change_email():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    # Clear stale session on fresh GET
    if request.method == 'GET':
        session.pop('change_email_otp', None)
        session.pop('change_email_new', None)
        session.pop('change_email_expiry', None)
        session.pop('change_email_phone_verified', None)
        session.pop('change_email_code', None)
        session.pop('change_email_code_expiry', None)

    if request.method == 'POST':
        action = request.form.get('action', '').strip()
        user_id = session['user_id']

        if not action:
            return render_template('profile/change_email.html')

        # ===============================
        # STEP 1: SEND PHONE OTP
        # ===============================
        if action == 'send_phone_otp':
            conn = db_helper.get_connection()
            try:
                user = conn.execute(
                    "SELECT p.phone FROM users u JOIN profiles p ON u.id = p.user_id WHERE u.id = ?",
                    (user_id,)
                ).fetchone()
            finally:
                conn.close()

            if not user or not user['phone']:
                flash("No phone number found on your account.")
                return render_template('profile/change_email.html')

            otp = str(random.randint(100000, 999999))
            session['change_email_otp'] = otp
            session['change_email_expiry'] = (datetime.now() + timedelta(minutes=10)).isoformat()

            try:
                client.messages.create(
                    body=f"Your Legacy Garden OTP is {otp}",
                    from_=TWILIO_PHONE,
                    to="+65" + user['phone']
                )
                flash("OTP sent to your registered phone number.")
            except Exception as e:
                flash(f"OTP (demo mode): {otp}")

            return render_template('profile/change_email.html',
                                   step='verify_phone')

        # ===============================
        # STEP 2: VERIFY PHONE OTP
        # ===============================
        elif action == 'verify_phone_otp':
            otp_input = request.form.get('otp', '').strip()
            stored_otp = session.get('change_email_otp')
            expiry = session.get('change_email_expiry')

            if not stored_otp:
                flash("Session expired. Please start over.")
                return render_template('profile/change_email.html')

            if otp_input != stored_otp:
                flash("Invalid OTP. Please try again.")
                return render_template('profile/change_email.html',
                                       step='verify_phone')

            if datetime.now() > datetime.fromisoformat(expiry):
                flash("OTP has expired. Please start over.")
                session.pop('change_email_otp', None)
                session.pop('change_email_expiry', None)
                return render_template('profile/change_email.html')

            session['change_email_phone_verified'] = True
            session.pop('change_email_otp', None)
            session.pop('change_email_expiry', None)

            return render_template('profile/change_email.html',
                                   step='enter_email')

        # ===============================
        # STEP 3: SEND EMAIL OTP
        # ===============================
        elif action == 'send_email_otp':
            if not session.get('change_email_phone_verified'):
                flash("Please verify your phone first.")
                return render_template('profile/change_email.html')

            new_email = request.form.get('new_email', '').strip().lower()

            if not new_email:
                flash("Please enter a new email address.")
                return render_template('profile/change_email.html', step='enter_email')

            conn = db_helper.get_connection()
            try:
                user = conn.execute(
                    "SELECT p.email FROM users u JOIN profiles p ON u.id = p.user_id WHERE u.id = ?",
                    (user_id,)
                ).fetchone()

                if new_email == (user['email'] or '').lower():
                    flash("You are already using this email address.")
                    return render_template('profile/change_email.html', step='enter_email')

                email_exists = conn.execute(
                    "SELECT id FROM profiles WHERE email = ?", (new_email,)
                ).fetchone()

                if email_exists:
                    flash("This email is already in use by another account.")
                    return render_template('profile/change_email.html', step='enter_email')
            finally:
                conn.close()

            # Send email OTP
            try:
                send_email_otp(new_email, purpose="change_email")
                session['change_email_new'] = new_email
                flash("A verification code has been sent to your new email.")
            except Exception as e:
                flash(f"Failed to send email OTP: {str(e)}")
                return render_template('profile/change_email.html', step='enter_email')

            return render_template('profile/change_email.html',
                                   step='verify_email',
                                   new_email=new_email)

        # ===============================
        # STEP 4: VERIFY EMAIL OTP + SAVE
        # ===============================
        elif action == 'verify_email_otp':
            if not session.get('change_email_phone_verified'):
                flash("Please verify your phone first.")
                return render_template('profile/change_email.html')

            email_otp_input = request.form.get('email_otp', '').strip()
            new_email = session.get('change_email_new')
            stored_code = session.get('email_otp_code')
            expiry = session.get('email_otp_expiry', '2000-01-01')

            if not new_email:
                flash("Session expired. Please start over.")
                return render_template('profile/change_email.html')

            if email_otp_input != stored_code:
                flash("Invalid email OTP. Please try again.")
                return render_template('profile/change_email.html',
                                       step='verify_email',
                                       new_email=new_email)

            if datetime.now() > datetime.fromisoformat(expiry):
                flash("Email OTP has expired. Please try again.")
                return render_template('profile/change_email.html',
                                       step='verify_email',
                                       new_email=new_email)

            # Save email
            conn = db_helper.get_connection()
            try:
                conn.execute(
                    "UPDATE profiles SET email = ? WHERE user_id = ?",
                    (new_email, user_id)
                )
                conn.commit()
            except Exception as e:
                flash("Error updating email. Please try again.")
                return render_template('profile/change_email.html',
                                       step='verify_email',
                                       new_email=new_email)
            finally:
                conn.close()

            session.pop('change_email_otp', None)
            session.pop('change_email_new', None)
            session.pop('change_email_expiry', None)
            session.pop('change_email_phone_verified', None)
            flash("Email updated successfully!")
            return redirect(url_for('settings'))

    return render_template('profile/change_email.html', step='send_phone_otp')

# --- CHANGE EMAIL PHONE (resend) --- recent jiawen
@app.route('/resend_change_email_phone_otp', methods=['POST'])
def resend_change_email_phone_otp():
    if 'user_id' not in session:
        return jsonify({'error': 'Session expired.'}), 400

    user_id = session['user_id']
    conn = db_helper.get_connection()
    try:
        user = conn.execute(
            "SELECT p.phone FROM users u JOIN profiles p ON u.id = p.user_id WHERE u.id = ?",
            (user_id,)
        ).fetchone()
    finally:
        conn.close()

    otp = str(random.randint(100000, 999999))
    session['change_email_otp'] = otp
    session['change_email_expiry'] = (datetime.now() + timedelta(minutes=10)).isoformat()
    session.modified = True

    try:
        client.messages.create(
            body=f"Your Legacy Garden OTP is {otp}",
            from_=TWILIO_PHONE,
            to="+65" + user['phone']
        )
        return jsonify({'message': 'OTP resent successfully.'})
    except Exception as e:
        return jsonify({'message': f'OTP (demo mode): {otp}'})

# --- CHANGE EMAIL (resend) --- recent jiawen
@app.route('/resend_change_email_otp', methods=['POST'])
def resend_change_email_otp():
    if 'user_id' not in session or not session.get('change_email_new'):
        return jsonify({'error': 'Session expired.'}), 400

    new_email = session.get('change_email_new')
    try:
        send_email_otp(new_email, purpose="change_email")
        return jsonify({'message': 'Verification code resent successfully.'})
    except Exception as e:
        return jsonify({'error': f'Failed to resend: {str(e)}'})
    

# --- CHANGE PHONE NUMBER ---
@app.route('/change_phone', methods=['GET', 'POST'])
def change_phone():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    if request.method == 'POST':
        new_phone = request.form.get('new_phone', '').strip()
        otp_input = request.form.get('otp', '').strip()

        # Step 1: Send OTP to new phone number
        if new_phone and not otp_input:
            if not re.match(r'^\d{8}$', new_phone):
                flash("Please enter a valid 8-digit Singapore phone number.")
                return render_template('profile/change_phone.html', show_otp=False)

            # Check not already in use
            conn = db_helper.get_connection()
            try:
                existing = conn.execute(
                    "SELECT user_id FROM profiles WHERE phone = ? AND user_id != ?",
                    (new_phone, user_id)
                ).fetchone()
            finally:
                conn.close()

            if existing:
                flash("This phone number is already linked to another account.")
                return render_template('profile/change_phone.html', show_otp=False)

            otp = str(random.randint(100000, 999999))
            session['change_phone_otp'] = otp
            session['change_phone_new'] = new_phone
            session['change_phone_expiry'] = (datetime.now() + timedelta(minutes=10)).isoformat()
            session.modified = True

            try:
                client.messages.create(
                    body=f"Your Legacy Garden OTP is {otp}",
                    from_=TWILIO_PHONE,
                    to="+65" + new_phone
                )
                flash("OTP sent to your new phone number.")
            except Exception as e:
                flash(f"OTP (demo mode): {otp}")

            return render_template('profile/change_phone.html',
                                   show_otp=True,
                                   form_data={'new_phone': new_phone})

        # Step 2: Verify OTP and save new phone
        if otp_input:
            stored_otp = session.get('change_phone_otp')
            expiry = session.get('change_phone_expiry')
            new_phone = session.get('change_phone_new')

            if not stored_otp or not expiry or not new_phone:
                flash("Session expired. Please try again.")
                return render_template('profile/change_phone.html', show_otp=False)

            if datetime.now() > datetime.fromisoformat(expiry):
                flash("OTP has expired. Please try again.")
                return render_template('profile/change_phone.html', show_otp=False)

            if otp_input != stored_otp:
                flash("Invalid OTP. Please try again.")
                return render_template('profile/change_phone.html',
                                       show_otp=True,
                                       form_data={'new_phone': new_phone})

            # Save new phone
            conn = db_helper.get_connection()
            try:
                conn.execute(
                    "UPDATE profiles SET phone = ? WHERE user_id = ?",
                    (new_phone, user_id)
                )
                conn.commit()
            except Exception as e:
                flash("Error updating phone number. Please try again.")
                return render_template('profile/change_phone.html', show_otp=True,
                                       form_data={'new_phone': new_phone})
            finally:
                conn.close()

            session.pop('change_phone_otp', None)
            session.pop('change_phone_new', None)
            session.pop('change_phone_expiry', None)
            flash("Phone number updated successfully!")
            return redirect(url_for('settings'))

    return render_template('profile/change_phone.html', show_otp=False)


# --- RESEND CHANGE PHONE OTP ---
@app.route('/resend_change_phone_otp', methods=['POST'])
def resend_change_phone_otp():
    if 'user_id' not in session or not session.get('change_phone_new'):
        return jsonify({'error': 'Session expired.'}), 400

    new_phone = session.get('change_phone_new')
    otp = str(random.randint(100000, 999999))
    session['change_phone_otp'] = otp
    session['change_phone_expiry'] = (datetime.now() + timedelta(minutes=10)).isoformat()
    session.modified = True

    try:
        client.messages.create(
            body=f"Your Legacy Garden OTP is {otp}",
            from_=TWILIO_PHONE,
            to="+65" + new_phone
        )
        return jsonify({'message': 'OTP resent successfully.'})
    except Exception as e:
        return jsonify({'message': f'OTP (demo mode): {otp}'})


# --- TERMS AND PRIVACY --- (JIAWEN - NEWLY ADDED)
@app.route('/terms')
def terms():
    return render_template('profile/terms.html')

# --- COMMUNITY GUIDELINES --- (JIAWEN - NEWLY ADDED)
@app.route("/community_guidelines")
def community_guidelines():
    return render_template("profile/community_guidelines.html")

# --- 6. LOGOUT ROUTE ---
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('landing'))


def get_tree_stage(points):
    if points >= 300:
        return "Flourishing Stage"
    elif points >= 150:
        return "Growing Stage"
    elif points >= 50:
        return "Budding Stage"
    else:
        return "Seedling Stage"


def get_tree_image(points):
    if points >= 300:
        return "tree_flourishing.png"
    elif points >= 150:
        return "tree_growing.png"
    elif points >= 50:
        return "tree_budding.png"
    else:
        return "tree_seedling.png"

# =========================
# ADMIN ACCESS DECORATOR
# =========================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role", "").lower() != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = db_helper.get_connection()

    # ----- STORIES LIST -----
    stories = conn.execute("""
        SELECT 
            s.id,
            s.title,
            s.content,
            s.image_path,
            s.status,
            u.username,
            r.reason AS report_reason
        FROM stories s
        JOIN users u ON s.user_id = u.id
        LEFT JOIN reports r ON s.id = r.story_id
        WHERE s.status != 'approved'
        ORDER BY s.created_at DESC
    """).fetchall()

    # ----- STORIES COUNT -----
    story_count = conn.execute("""
        SELECT COUNT(*) 
        FROM stories 
        WHERE status != 'approved'
    """).fetchone()[0]

    # =========================
    # REPORTED COMMENTS
    # =========================
    reported_comments_raw = conn.execute("""
        SELECT 
            cr.comment_id AS id,
            sc.content,
            u.username,
            s.title AS story_title,
            cr.reason AS report_reason
        FROM comment_reports cr
        LEFT JOIN story_comments sc ON cr.comment_id = sc.id
        LEFT JOIN users u ON sc.user_id = u.id
        LEFT JOIN stories s ON sc.story_id = s.id
        ORDER BY cr.created_at DESC
    """).fetchall()

    reported_comments = [dict(row) for row in reported_comments_raw]

    # ----- EVENTS COUNT (SAFE) -----
    try:
        event_count = conn.execute("""
            SELECT COUNT(*) 
            FROM events 
            WHERE status IN ('pending', 'reported')
        """).fetchone()[0]
    except sqlite3.OperationalError:
        event_count = 0

    conn.close()

    return render_template(
        "admin/admin_dashboard.html",
        stories=stories,
        story_count=story_count,
        event_count=event_count,
        reported_comments=reported_comments,  
    )


# =========================
# ADMIN – APPROVE STORY
# =========================
@app.route("/admin/approve/<int:story_id>", methods=["POST"])
@admin_required
def approve_story(story_id):
    conn = db_helper.get_connection()

    conn.execute("""
        UPDATE stories
        SET status = 'approved'
        WHERE id = ?
    """, (story_id,))

    conn.execute("""
        DELETE FROM reports
        WHERE story_id = ?
    """, (story_id,))

    conn.commit()
    conn.close()

    flash("Story approved successfully")
    return redirect(url_for("admin_dashboard"))

# =========================
# ADMIN – DELETE STORY
# =========================
@app.route("/admin/delete/<int:story_id>", methods=["POST"])
@admin_required
def delete_story(story_id):
    conn = db_helper.get_connection()

    conn.execute("DELETE FROM reports WHERE story_id = ?", (story_id,))
    conn.execute("DELETE FROM story_likes WHERE story_id = ?", (story_id,))
    conn.execute("DELETE FROM story_comments WHERE story_id = ?", (story_id,))
    conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))

    conn.commit()
    conn.close()

    flash("Story deleted successfully")
    return redirect(url_for("admin_dashboard"))

# =========================
# ADMIN – APPROVE COMMENT
# =========================
@app.route("/admin/approve_comment/<int:comment_id>", methods=["POST"])
@admin_required
def approve_comment(comment_id):
    conn = db_helper.get_connection()
    conn.execute("DELETE FROM comment_reports WHERE comment_id = ?", (comment_id,))
    conn.commit()
    conn.close()
    flash("Comment approved (report dismissed)")
    return redirect(url_for("admin_dashboard"))

# =========================
# ADMIN – DELETE COMMENT
# =========================
@app.route("/admin/delete_comment/<int:comment_id>", methods=["POST"])
@admin_required
def delete_comment_admin(comment_id):
    conn = db_helper.get_connection()
    conn.execute("DELETE FROM comment_reports WHERE comment_id = ?", (comment_id,))
    conn.execute("DELETE FROM story_comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    flash("Comment deleted successfully")
    return redirect(url_for("admin_dashboard"))

# =========================
# ADMIN – EVENTS
# =========================
@app.route("/admin/events")
@admin_required
def admin_events():
    conn = db_helper.get_connection()

    events = conn.execute("""
        SELECT 
            e.*,
            u.username
        FROM events e
        LEFT JOIN users u ON e.created_by = u.id
        ORDER BY e.created_at DESC
    """).fetchall()

    conn.close()

    return render_template("admin/admin_events.html", events=events)


# =========================
# ADMIN – ADD EVENT
# =========================
@app.route("/admin/events/add", methods=["POST"])
@admin_required
def add_event():
    conn = db_helper.get_connection()

    title = request.form.get("title")
    event_date = request.form.get("event_date")
    short_desc = request.form.get("short_description")
    full_desc = request.form.get("full_description")
    rewards = request.form.get("rewards")
    status = request.form.get("status")

    image_file = request.files.get("image")
    filename = None

    if image_file and image_file.filename:
        filename = secure_filename(image_file.filename)
        image_path = os.path.join("static", "uploads", filename)
        image_file.save(image_path)

    conn.execute("""
        INSERT INTO events (
            title,
            short_description,
            full_description,
            rewards,
            event_date,
            image_filename,
            status,
            created_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        title,
        short_desc,
        full_desc,
        rewards,
        event_date,
        filename,
        status,
        session["user_id"]
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("admin_events"))

# =========================
# ADMIN – EDIT EVENT
# =========================
@app.route("/admin/events/edit/<int:event_id>", methods=["GET", "POST"])
@admin_required
def edit_event(event_id):
    conn = db_helper.get_connection()

    event = conn.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return redirect(url_for("admin_events"))

    if request.method == "POST":

        title = request.form.get("title")
        event_date = request.form.get("event_date")
        short_description = request.form.get("short_description")
        full_description = request.form.get("full_description")
        rewards = request.form.get("rewards")
        status = request.form.get("status")

        image_file = request.files.get("image")

        if image_file and image_file.filename != "":
            filename = secure_filename(image_file.filename)
            image_path = os.path.join("static/uploads", filename)
            image_file.save(image_path)

            conn.execute("""
                UPDATE events
                SET title=?, event_date=?, short_description=?,
                    full_description=?, rewards=?, status=?, image_filename=?
                WHERE id=?
            """, (title, event_date, short_description,
                  full_description, rewards, status, filename, event_id))
        else:
            conn.execute("""
                UPDATE events
                SET title=?, event_date=?, short_description=?,
                    full_description=?, rewards=?, status=?
                WHERE id=?
            """, (title, event_date, short_description,
                  full_description, rewards, status, event_id))

        conn.commit()
        conn.close()
        return redirect(url_for("admin_events"))

    conn.close()
    return render_template("admin/edit_event.html", event=event)


# =========================
# ADMIN – DELETE
# =========================
@app.route("/admin/events/delete/<int:event_id>", methods=["POST"])
@admin_required
def delete_event_admin(event_id):
    conn = db_helper.get_connection()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()

    return redirect(url_for("admin_events"))

# =========================
# ADMIN – user
# =========================
#fel added for admin user
# =========================
# ADMIN – USER REPORTS
# =========================
@app.route("/admin_user")
@admin_required
def admin_user():
    reports = db_helper.get_all_user_reports()
    return render_template("admin/reports.html", reports=reports)

@app.route("/admin/ban_user/<int:user_id>", methods=["POST"])
@admin_required
def admin_ban_user(user_id):
    db_helper.set_user_status(user_id, "banned")
    flash("User banned.", "success")
    return redirect(url_for("admin_user"))

@app.route("/admin/unban_user/<int:user_id>", methods=["POST"])
@admin_required
def admin_unban_user(user_id):
    db_helper.set_user_status(user_id, "active")
    flash("User unbanned.", "success")
    return redirect(url_for("admin_user"))
# to here

# fel readded
@app.route("/admin/user_reports/resolve/<int:report_id>", methods=["POST"])
@admin_required
def admin_resolve_user_report(report_id):
    # admin session id — if your admin_required already ensures admin, this is fine
    admin_id = session.get("admin_user") or session.get("user_id")

    ok = db_helper.resolve_user_report(report_id, admin_id or 0)
    flash("Report resolved." if ok else "Failed to resolve report.", "success" if ok else "danger")
    return redirect(url_for("admin_user"))  # your reports page endpoint is admin_user
# to here

# =========================
# VIEW PROFILE
# =========================
@app.route('/view_profile/<username>')
def view_profile(username):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    print(f"\n🔍 DEBUG view_profile:")
    print(f"  Requested username: {username}")
    print(f"  Session user_id: {session.get('user_id')}")
    print(f"  Session username: {session.get('username')}")

    conn = db_helper.get_connection()
    try:
        # ✅ Get the VIEWED user's profile data
        user_data = conn.execute("""
            SELECT u.id, u.username, u.role, u.points,
                   p.user_id, p.name, p.bio, p.region, p.profile_pic, p.email,
                   p.community_visible, p.show_email, p.show_region
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            WHERE u.username = ?
        """, (username,)).fetchone()

        print(f"  Query result: {user_data is not None}")

        if not user_data:
            print(f"  ❌ User '{username}' not found in database!")
            flash(f"User '{username}' not found.")
            return redirect(url_for('story.index'))

        user_data = dict(user_data)
        print(f"  ✅ Found user: id={user_data['id']}, username={user_data['username']}")

        # ✅ Check if viewing own profile
        is_own_profile = (user_data['id'] == session['user_id'])
        print(f"  is_own_profile: {is_own_profile}")

        # ✅ Privacy check
        if not user_data.get('community_visible', 1) and not is_own_profile:
            print("  ❌ Profile is private")
            flash("This profile is private.")
            return redirect(url_for('story.index'))

        # ✅ Fetch stories WITH like & comment counts + user_liked
        viewer_id = session.get("user_id")

        stories = conn.execute("""
            SELECT s.*,
                   COALESCE(lc.like_count, 0) AS like_count,
                   COALESCE(cc.comment_count, 0) AS comment_count,
                   CASE WHEN ul.user_id IS NULL THEN 0 ELSE 1 END AS user_liked
            FROM stories s
            LEFT JOIN (
                SELECT story_id, COUNT(*) AS like_count
                FROM story_likes
                GROUP BY story_id
            ) lc ON lc.story_id = s.id
            LEFT JOIN (
                SELECT story_id, COUNT(*) AS comment_count
                FROM story_comments
                GROUP BY story_id
            ) cc ON cc.story_id = s.id
            LEFT JOIN story_likes ul
                ON ul.story_id = s.id AND ul.user_id = ?
            WHERE s.user_id = ? AND s.status = 'approved'
            ORDER BY s.created_at DESC
        """, (viewer_id, user_data['id'])).fetchall()

        stories = [dict(s) for s in stories]
        print(f"  ✅ Found {len(stories)} stories")

        # ✅ Apply privacy masking (for non-own profile)
        if not is_own_profile:
            if not user_data.get('show_email', 1):
                user_data['email'] = None
            if not user_data.get('show_region', 1):
                user_data['region'] = None

        print("  ✅ Rendering view_profile.html")

        return render_template(
            'profile/view_profile.html',
            profile=user_data,
            user=user_data,          # ✅ from your 2nd route (prevents template crash)
            stories=stories,
            is_own_profile=is_own_profile
        )

    except Exception as e:
        print(f"  ❌ EXCEPTION: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error loading profile: {e}")
        return redirect(url_for('story.index'))
    finally:
        conn.close()



# Fel added
# --- 3. COMMUNITY DASHBOARD ---
@app.route('/community')
def community():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    current_user_id = session['user_id']
    profile = db_helper.get_profile_by_user_id(current_user_id) or {"region": "Unknown"}
    region_name = profile.get("region", "Unknown")

    youth_members, senior_members, total_members = db_helper.get_region_member_counts(region_name)

    last_notice_update = db_helper.get_latest_notice_timestamp(region_name)
    notices = db_helper.get_region_notices(region_name, limit=3)
    latest_notice = notices[0] if notices else None   # keep as dict, no comma

    achievement_last_update = get_last_monday()
    week_start_dt = achievement_last_update
    week_end_dt = week_start_dt + timedelta(days=7)
    week_start_str = week_start_dt.strftime("%Y-%m-%d")

    weekly = db_helper.get_weekly_achievements(week_start_str)
    if not weekly:
        weekly = {
            "most_active_region": "—",
            "best_harvest_region": "—",
            "participation_region": "—",
            "generated_at": None
        }

    # latest notice this week (any region)
    latest_notice_this_week = db_helper.get_latest_notice_timestamp_in_range(week_start_dt, week_end_dt)

    trees_harvested, flowers_harvested, community_points = db_helper.get_region_tree_totals(region_name)

    tree_stage = get_tree_stage(community_points)   
    tree_image = get_tree_image(community_points)


    should_recompute = False

    if not weekly:
        should_recompute = True
    else:
        # weekly["generated_at"] is SQLite CURRENT_TIMESTAMP (UTC)
        gen = weekly.get("generated_at")
        if gen and latest_notice_this_week:
            generated_at_dt = datetime.strptime(gen, "%Y-%m-%d %H:%M:%S")
            # if your notices/generate_at are UTC, compare directly
            if latest_notice_this_week > generated_at_dt:
                should_recompute = True

    if should_recompute:
        most_active, participation, best_harvest = db_helper.compute_weekly_winners(
                week_start_dt, week_end_dt
            )

        db_helper.save_weekly_achievements(
            week_start_str, most_active, participation, best_harvest
        )
        weekly = db_helper.get_weekly_achievements(week_start_str)


    # IMPORTANT: pass acceptance flag
    guidelines_accepted = int(profile.get("guidelines_accepted") or 0)

    my_username = session.get("username", "")
    my_region = (profile.get("region") or "Unknown")

    with force_locale("en"):
        return render_template(
            'community/community.html',
            profile=profile,
            youth_members=youth_members,
            senior_members=senior_members,
            total_members=total_members,

            last_notice_update=last_notice_update,
            notices=notices,
            latest_notice=latest_notice,

            achievement_last_update=achievement_last_update,
            most_active_region=weekly["most_active_region"],
            best_harvest_region=weekly["best_harvest_region"],
            participation_region=weekly["participation_region"],
                
            guidelines_accepted=guidelines_accepted,

            trees_harvested=trees_harvested,
            flowers_harvested=flowers_harvested,
            community_points=community_points,

            stage=tree_stage,
            tree_image=tree_image,

            # ✅ ADD THESE TWO
            my_username=my_username,
            my_region=my_region,
    )



@app.route("/community/accept_guidelines", methods=["POST"])
def accept_guidelines():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = db_helper.get_connection()
    try:
        conn.execute("""
            UPDATE profiles
            SET guidelines_accepted = 1,
                guidelines_accepted_at = ?
            WHERE user_id = ?
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session["user_id"]))
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for("community"))


@app.template_filter("time_ago")
def time_ago(dt):
    if not dt:
        return _("No recent activity")

    # SQLite may return string timestamps
    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return _("No recent activity")

    # Convert UTC → Singapore time (only if your DB timestamps are UTC)
    dt = dt + timedelta(hours=8)

    delta = datetime.now() - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return _("Just now")

    if seconds < 3600:
        mins = seconds // 60
        if mins == 1:
            return _("1 minute ago")
        else:
            return _("%(mins)d minutes ago", mins=mins)

    if seconds < 86400:
        hrs = seconds // 3600
        if hrs == 1:
            return _("1 hour ago")
        else:
            return _("%(hrs)d hours ago", hrs=hrs)

    days = seconds // 86400
    if days == 1:
        return _("1 day ago")
    else:
        return _("%(days)d days ago", days=days)


def get_last_monday():
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())  # Monday of this week
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)





@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


# Events main page (change yq)
@app.route('/events')
def events_home():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    today_str = get_events_demo_date_str()
    was_reset = db_helper.ensure_weekly_reset(user_id, today_str)

    streaks = db_helper.get_or_create_user_streaks(user_id)

    # 🔥 ADD THIS LINE
    events = db_helper.get_all_events()

    # #Yq fix: check DB directly for any unseen unlocks — no session cookie needed
    # is_seen=0 means the card was unlocked since the user last visited the album
    conn = db_helper.get_connection()
    try:
        unseen_count = conn.execute(
            "SELECT COUNT(*) FROM user_memory_unlocks WHERE user_id = ? AND is_seen = 0",
            (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    has_new_unlock = unseen_count > 0

    return render_template(
        'events/events.html',
        daily_streak=int(streaks.get("daily_game_streak", 0) or 0),
        win_streak=int(streaks.get("winning_streak", 0) or 0),
        seed_claimed=int(streaks.get("seed_claimed", 0) or 0),
        was_reset=was_reset,
        events=events,   # 🔥 VERY IMPORTANT
        has_new_unlock=has_new_unlock  # #Yq fix: drives the red dot on Memory Album
    )

# Memory Match game
@app.route('/events/memory-match')
def memory_match():
    return render_template('events/memorymatch.html')

@app.route('/events/hangman')
def hangman():
    return render_template('events/hangman.html')

# yq added: unlock a Singapore nostalgia memory for the logged-in user
@app.route("/unlock_memory", methods=["POST"])
def unlock_memory():
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    memory_key = (data.get("memory_key") or "").strip()
    if not memory_key:
        return jsonify({"status": "error", "message": "No memory_key provided"}), 400

    user_id = session["user_id"]
    conn = db_helper.get_connection()
    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO user_memory_unlocks (user_id, memory_key, is_seen)
            VALUES (?, ?, 0)
        """, (user_id, memory_key))
        conn.commit()
        # #Yq fix: rowcount==1 means genuinely new unlock; 0 means already existed
        newly_unlocked = cursor.rowcount == 1
        return jsonify({"status": "success", "memory_key": memory_key, "newly_unlocked": newly_unlocked})
    finally:
        conn.close()

# yq added: Memory Album page — shows all memories unlocked by the logged-in user
# #Yq added: now passes ALL cards with locked/unlocked state so locked cards are shown as silhouettes
@app.route("/memory_album")
def memory_album():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = db_helper.get_connection()
    try:
        # #Yq added: LEFT JOIN so ALL cards appear; unlocked_at is NULL for locked cards
        rows = conn.execute("""
            SELECT mc.memory_key, mc.title, mc.category, mc.description, mc.emoji,
                   mc.image_path,
                   umu.unlocked_at,
                   CASE WHEN umu.user_id IS NULL THEN 1 ELSE 0 END AS locked
            FROM memory_cards mc
            LEFT JOIN user_memory_unlocks umu
                ON mc.memory_key = umu.memory_key AND umu.user_id = ?
            ORDER BY locked ASC, umu.unlocked_at DESC, mc.category, mc.title
        """, (user_id,)).fetchall()

        total_cards = conn.execute("SELECT COUNT(*) FROM memory_cards").fetchone()[0]
        unlocked_count = conn.execute(
            "SELECT COUNT(*) FROM user_memory_unlocks WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        # #Yq added: split into unlocked and locked lists for the template
        # Sort: unlocked first (newest unlock at top), then locked cards after
        all_rows = [dict(r) for r in rows]
        unlocked = [c for c in all_rows if not c["locked"]]
        locked   = [c for c in all_rows if c["locked"]]
        unlocked.sort(key=lambda c: c["unlocked_at"] or "", reverse=True)
        all_cards = unlocked + locked
    finally:
        conn.close()

    # #yq fix: mark all unseen unlocks as seen in DB — this clears the red dot reliably
    # Using DB instead of session cookie so it works across tabs and page reloads
    conn2 = db_helper.get_connection()
    try:
        conn2.execute(
            "UPDATE user_memory_unlocks SET is_seen = 1 WHERE user_id = ? AND is_seen = 0",
            (user_id,)
        )
        conn2.commit()
    finally:
        conn2.close()

    return render_template(
        "events/memory_album.html",
        all_cards=all_cards,
        unlocked_count=unlocked_count,
        total_cards=total_cards
    )

@app.route("/api/streaks/hangman_end", methods=["POST"])
def hangman_end():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    did_win = bool(data.get("did_win", False))

    updated = db_helper.update_streaks_on_game_end(session["user_id"], did_win)
    return jsonify(updated)

#Changed
@app.route("/api/streaks/quit_game", methods=["POST"])
def quit_game():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session["user_id"]
    
    # ✅ Apply both penalties in a single transaction
    conn = db_helper.get_connection()
    try:
        # Ensure user_streaks record exists
        conn.execute("""
            INSERT OR IGNORE INTO user_streaks (user_id, daily_game_streak, winning_streak)
            VALUES (?, 0, 0)
        """, (user_id,))
        
        # Apply penalties: daily_game_streak - 1 (min 0), winning_streak = 0
        conn.execute("""
            UPDATE user_streaks
            SET daily_game_streak = MAX(0, daily_game_streak - 1),
                winning_streak = 0
            WHERE user_id = ?

        """, (user_id,))
        
        conn.commit()
        
        # Fetch and return updated values
        row = conn.execute("""
            SELECT daily_game_streak, winning_streak, last_game_date
            FROM user_streaks
            WHERE user_id = ?
        """, (user_id,)).fetchone()
        
        updated = dict(row) if row else {"daily_game_streak": 0, "winning_streak": 0, "last_game_date": None}
        return jsonify(updated)
    except Exception as e:
        conn.rollback()
        print(f"❌ Error applying quit penalty: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/rewards/claim_seed", methods=["POST"])
def claim_seed():
    uid = session.get("user_id", 1)
    result = db_helper.claim_seed_reward(uid)

    # if claim successful, try log history and show errors
    if isinstance(result, dict) and result.get("ok"):
        seed_type = (result.get("seed_type") or "tree").strip().lower()  # default tree

        try:
            db_helper.log_garden_history(
                user_id=uid,
                category=seed_type,   # MUST be "tree" or "flower"
                title=f"Claimed {seed_type} seed (+1)",
                amount=1
            )
            result["history_logged"] = True
        except Exception as e:
            # ✅ expose exact reason
            result["history_logged"] = False
            result["history_error"] = str(e)

    return jsonify(result)

# vivion
@app.route('/mygarden')
def mygarden():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    uid = session['user_id']

    user_row = db_helper.get_user_by_id(uid)
    user = dict(user_row) if user_row else {}
    user["points"] = int(user.get("points") or 0)

    inventory_row = db_helper.get_user_inventory(uid)
    inventory = dict(inventory_row) if inventory_row else {"seed_tree": 0, "seed_flower": 0, "water": 0}

    plots = db_helper.get_user_plots(uid)
    all_rewards = db_helper.get_all_rewards()
    my_rewards = db_helper.get_user_rewards(uid)

    return render_template(
        'garden/garden_dashboard.html',
        user=user,
        inventory=inventory,
        plots=plots,
        all_rewards=all_rewards,
        my_rewards=my_rewards
    )

# --- zn ---
def friendly_day_label(msg_dt: datetime, now_dt: datetime) -> str:
    msg_date = msg_dt.date()
    now_date = now_dt.date()
    diff_days = (now_date - msg_date).days

    if diff_days == 0:
        return "Today"
    if diff_days == 1:
        return "Yesterday"
    if 2 <= diff_days <= 6:
        return msg_dt.strftime("%A")  # Monday, Tuesday, etc
    return msg_dt.strftime("%d %b %Y")  # fallback e.g. 06 Feb 2026

def get_demo_date_str():
    # if you set a demo date, use it; else use real date
    return session.get("demo_date") or datetime.now().strftime("%Y-%m-%d")

def get_demo_now():
    """
    Returns a datetime.
    If session['demo_date'] exists, return that date with current time.
    Else return real datetime.now().
    """
    demo = session.get("demo_date")
    if not demo:
        return datetime.now()

    # keep time real, only fake the DATE
    now = datetime.now()
    y, m, d = map(int, demo.split("-"))
    return now.replace(year=y, month=m, day=d)
dm_streaks = {}      # { "dm:userA:userB": int }
dm_sent_today = {}   # { "dm:userA:userB": { "userA": bool, "userB": bool } }
dm_last_day = {}     # { "dm:userA:userB": "YYYY-MM-DD" }


# --- Yq --- 
# =========================================================
# EVENTS DEMO HELPERS (ISOLATED FROM DM DEMO)
# =========================================================

def get_events_demo_date_str():
    """
    Returns YYYY-MM-DD.
    Uses events demo date if set, else real date.
    """
    return session.get("events_demo_date") or datetime.now().strftime("%Y-%m-%d")


def get_events_demo_now():
    """
    Returns datetime.
    Fakes DATE only, keeps real time.
    """
    demo = session.get("events_demo_date")
    if not demo:
        return datetime.now()

    now = datetime.now()
    y, m, d = map(int, demo.split("-"))
    return now.replace(year=y, month=m, day=d)
# --- end --- 


# =========================
# DM STREAK PERSISTENCE (SQLite)
# =========================

def ensure_dm_streak_table():
    """
    Create a table to persist DM streak state so it survives reloads/restarts.
    """
    conn = db_helper.get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_streak_state (
                room TEXT PRIMARY KEY,
                streak INTEGER NOT NULL DEFAULT 0,
                last_day TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()
    finally:
        conn.close()


def db_get_dm_state(room: str):
    """
    Returns (streak:int, last_day:str). Defaults to (0, '') if missing.
    """
    conn = db_helper.get_connection()
    try:
        row = conn.execute(
            "SELECT streak, last_day FROM dm_streak_state WHERE room = ?",
            (room,)
        ).fetchone()
        if not row:
            return 0, ""
        return int(row["streak"] or 0), (row["last_day"] or "")
    finally:
        conn.close()


def db_set_dm_state(room: str, streak: int, last_day: str):
    """
    Upsert DM state.
    """
    conn = db_helper.get_connection()
    try:
        conn.execute("""
            INSERT INTO dm_streak_state (room, streak, last_day)
            VALUES (?, ?, ?)
            ON CONFLICT(room) DO UPDATE SET
                streak = excluded.streak,
                last_day = excluded.last_day
        """, (room, int(streak), str(last_day or "")))
        conn.commit()
    finally:
        conn.close()


# ✅ run once on startup
ensure_dm_streak_table()


@app.route("/messages/home")
def messaging_home():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("messaging/messaging_home.html")


# Inbox (DMs)
@app.route("/messages/inbox")
def messaging_inbox():
    if "user_id" not in session:
        return redirect(url_for("login"))

    uid = session["user_id"]
    my_username = session.get("username")
    if not my_username:
        return redirect(url_for("login"))

    selected_recipient = (request.args.get("to") or "").strip() or None

    conn = db_helper.get_connection()
    try:
        # LEFT PANEL: list all other users
        rows = conn.execute("""
            SELECT u.id, u.username,
                   COALESCE(p.name, u.username) AS display_name,
                   COALESCE(p.profile_pic, 'profile_pic.png') AS profile_pic
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE u.id != ?
            ORDER BY COALESCE(p.name, u.username) ASC
        """, (uid,)).fetchall()

        chats = []
        for r in rows:
            d = dict(r)
            pic = d.get("profile_pic") or "profile_pic.png"

            if isinstance(pic, str):
                if pic.startswith("static/uploads/"):
                    pic = pic.replace("static/uploads/", "", 1)
                if pic.startswith("uploads/"):
                    pic = pic.replace("uploads/", "", 1)

            d["profile_pic_url"] = url_for("static", filename=f"uploads/{pic}")

            room = room_name(my_username, d["username"])

            # ✅ PULL STREAK FROM DB (PERSISTENT)
            streak_val, last_day_val = db_get_dm_state(room)

            # keep in-memory dicts synced
            dm_streaks[room] = streak_val
            dm_last_day[room] = last_day_val

            d["streak"] = streak_val
            d["completed_today"] = (last_day_val == get_demo_date_str())

            chats.append(d)

        # RIGHT PANEL defaults
        history = []
        recipient_display_name = None
        recipient_profile = None
        recipient_profile_pic_url = url_for("static", filename="uploads/profile_pic.png")
        current_streak = 0
        completed_today = False

        if selected_recipient:
            rec = conn.execute("""
                SELECT u.id, u.username,
                       COALESCE(p.name, u.username) AS display_name,
                       COALESCE(p.profile_pic, 'profile_pic.png') AS profile_pic,
                       p.region, p.email, p.bio
                FROM users u
                LEFT JOIN profiles p ON p.user_id = u.id
                WHERE u.username = ?
            """, (selected_recipient,)).fetchone()

            if rec:
                rec = dict(rec)
                recipient_display_name = rec["display_name"]

                pic = rec.get("profile_pic") or "profile_pic.png"
                if isinstance(pic, str):
                    if pic.startswith("static/uploads/"):
                        pic = pic.replace("static/uploads/", "", 1)
                    if pic.startswith("uploads/"):
                        pic = pic.replace("uploads/", "", 1)

                recipient_profile_pic_url = url_for("static", filename=f"uploads/{pic}")

                recipient_profile = {
                    "username": rec["username"],
                    "name": rec["display_name"],
                    "region": rec.get("region"),
                    "email": rec.get("email"),
                    "bio": rec.get("bio"),
                }

                # Load DM history
                msg_rows = conn.execute("""
                    SELECT m.message_text, m.timestamp,
                           u.username AS sender_username
                    FROM messages m
                    JOIN users u ON u.id = m.sender_id
                    WHERE (
                        (m.sender_id = ? AND m.receiver_id = ?)
                        OR
                        (m.sender_id = ? AND m.receiver_id = ?)
                    )
                    ORDER BY m.timestamp ASC
                """, (uid, rec["id"], rec["id"], uid)).fetchall()

                now_dt = get_demo_now()

                history = []
                for x in msg_rows:
                    d = dict(x)
                    ts_str = d.get("timestamp") or ""

                    try:
                        msg_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        msg_dt = now_dt

                    d["day_label"] = friendly_day_label(msg_dt, now_dt)
                    history.append(d)

                room = room_name(my_username, selected_recipient)

                # ✅ STREAK FOR RIGHT PANEL ALSO FROM DB
                streak_val, last_day_val = db_get_dm_state(room)
                dm_streaks[room] = streak_val
                dm_last_day[room] = last_day_val

                current_streak = streak_val
                completed_today = (last_day_val == get_demo_date_str())

    finally:
        conn.close()

    my_profile = db_helper.get_profile_by_user_id(uid) or {}

    return render_template(
        "messaging/messaging_inbox.html",
        chats=chats,
        selected_recipient=selected_recipient,
        recipient_display_name=recipient_display_name,
        recipient_profile=recipient_profile,
        recipient_profile_pic_url=recipient_profile_pic_url,
        history=history,
        my_username=my_username,
        profile=my_profile,
        current_streak=current_streak,
        completed_today=completed_today,
    )


# Communities (region group chat)
@app.route("/messages/communities")
def messaging_communities():
    if "user_id" not in session:
        return redirect(url_for("login"))

    uid = session["user_id"]
    my_username = session.get("username", "")

    my_profile = db_helper.get_profile_by_user_id(uid) or {}
    my_region = (my_profile.get("region") or "Unknown").strip()

    # banner numbers
    youth_members, senior_members, total_members = db_helper.get_region_member_counts(my_region)

    # load region messages
    conn = db_helper.get_connection()
    try:
        rows = conn.execute("""
            SELECT m.id, m.message_text, m.timestamp,
                   u.username AS sender_username,
                   COALESCE(p.name, u.username) AS sender_display_name
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE m.region_name = ? AND m.receiver_id IS NULL
            ORDER BY m.id ASC
        """, (my_region,)).fetchall()

        history = [dict(r) for r in rows]
    finally:
        conn.close()

    return render_template(
        "messaging/messaging_communities.html",
        my_username=my_username,
        region_name=my_region,
        history=history,
        youth_members=youth_members,
        senior_members=senior_members,
        total_members=total_members,
    )


# =========================
# SOCKET.IO: REGION CHAT
# =========================

@socketio.on("join_region")
def join_region(data):
    region = ((data or {}).get("region") or "").strip()
    if not region:
        return
    join_room(f"region:{region}")


@socketio.on("send_region_message")
def send_region_message(data):
    if "user_id" not in session or "username" not in session:
        return

    uid = session["user_id"]
    my_username = session["username"]

    region = ((data or {}).get("region") or "").strip()
    msg = ((data or {}).get("message") or "").strip()
    if not region or not msg:
        return

    ts = get_demo_now().strftime("%Y-%m-%d %H:%M:%S")

    prof = db_helper.get_profile_by_user_id(uid) or {}
    display_name = prof.get("name") or my_username

    conn = db_helper.get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO messages (sender_id, receiver_id, region_name, message_text, timestamp)
            VALUES (?, NULL, ?, ?, ?)
        """, (uid, region, msg, ts))
        conn.commit()
        message_id = cur.lastrowid
    finally:
        conn.close()

    payload = {
        "id": message_id,
        "sender": my_username,
        "sender_display_name": display_name,
        "message": msg,
        "timestamp": ts,
        "region": region
    }

    emit("new_region_message", payload, room=f"region:{region}")


@socketio.on("typing")
def on_typing(_data=None):
    username = request.args.get("username")
    recipient = request.args.get("recipient")
    if not username or not recipient:
        return

    emit("typing", {"user": username}, room=room_name(username, recipient), include_self=False)


@socketio.on("send_message")
def on_send_message(data):
    sender_username = request.args.get("username")
    recipient_username = (data or {}).get("recipient")
    message_text = ((data or {}).get("message") or "").strip()

    if not sender_username or not recipient_username or not message_text:
        return

    # timestamp + day label (based on demo date)
    now_dt = get_demo_now()
    ts = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    day_label = friendly_day_label(now_dt, now_dt)

    # save to DB
    conn = db_helper.get_connection()
    try:
        sender_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (sender_username,)
        ).fetchone()

        rec_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (recipient_username,)
        ).fetchone()

        if not sender_row or not rec_row:
            return

        sender_id = sender_row["id"]
        receiver_id = rec_row["id"]

        db_helper.save_message(sender_id, receiver_id, message_text, timestamp=ts)
    finally:
        conn.close()

    # streak logic (✅ persistent)
    room = room_name(sender_username, recipient_username)
    today = get_demo_date_str()

    # ✅ load persisted state FIRST
    saved_streak, saved_last_day = db_get_dm_state(room)
    dm_streaks[room] = saved_streak
    dm_last_day[room] = saved_last_day

    if room not in dm_sent_today:
        dm_sent_today[room] = {sender_username: False, recipient_username: False}

    dm_sent_today[room][sender_username] = True

    both_sent = (
        dm_sent_today[room].get(sender_username, False)
        and dm_sent_today[room].get(recipient_username, False)
    )

    lit_up = False
    if both_sent and dm_last_day[room] != today:
        dm_streaks[room] += 1
        dm_last_day[room] = today
        lit_up = True

        dm_sent_today[room][sender_username] = False
        dm_sent_today[room][recipient_username] = False

    # ✅ persist current streak state EVERY message
    db_set_dm_state(room, dm_streaks[room], dm_last_day[room])

    # emit message + streak update
    payload = {
        "sender": sender_username,
        "recipient": recipient_username,
        "message": message_text,
        "timestamp": ts,
        "day_label": day_label,
    }

    completed_today = (dm_last_day.get(room) == today)

    emit("new_message", payload, room=room)
    emit(
        "streak_update",
        {
            "streak": dm_streaks[room],
            "completed_today": completed_today,
            "lit_up": lit_up,
        },
        room=room
    )



# =========================
# SOCKET.IO: DM CHAT (ONLINE + STREAKS)
# =========================

online_users = {}  # { username: sid }

def room_name(a: str, b: str) -> str:
    x, y = sorted([a, b])
    return f"dm:{x}:{y}"


def did_complete_today(room: str) -> bool:
    today = get_demo_date_str()
    return dm_last_day.get(room) == today


@socketio.on("connect")
def on_connect():
    username = request.args.get("username")
    recipient = request.args.get("recipient")

    if username:
        online_users[username] = request.sid

    if username and recipient:
        join_room(room_name(username, recipient))

    socketio.emit("online_list", {u: True for u in online_users.keys()})


@socketio.on("disconnect")
def on_disconnect():
    # ── DM online list cleanup ───────────────────────────────────
    for u, sid in list(online_users.items()):
        if sid == request.sid:
            del online_users[u]
            break
    socketio.emit("online_list", {u: True for u in online_users.keys()})

    # ── Game forfeit on disconnect ───────────────────────────────
    game_info = sid_to_game.pop(request.sid, None)
    if not game_info:
        return

    room      = game_info.get("room", "")
    role      = game_info.get("role", "")
    game_type = game_info.get("game_type", "")
    user_id   = game_info.get("user_id")

    if not room or not role or not game_type:
        return

    # Only forfeit if the game is still in progress (not already cleaned up)
    game_alive = (
        (game_type == "memory"  and room in memory_states  and not memory_states[room].get("game_over")) or
        (game_type == "hangman" and room in hangman_states and not hangman_states[room].get("game_over"))
    )
    if not game_alive:
        return

    # KEY FIX: With polling transport, on_disconnect for the OLD sid can fire
    # AFTER join_game for the NEW sid (the poll timeout is ~25s).
    # If join_game was called recently for this (room, role), this disconnect belongs
    # to a stale/old SID — the player already reconnected, so skip forfeit entirely.
    key_rj = (room, role)
    with _recent_joins_lock:
        last_join = _recent_joins.get(key_rj, 0)
    if _time.time() - last_join < _RECENT_JOIN_WINDOW:
        print(f"✅ on_disconnect ignored for room={room} role={role} — player rejoined {_time.time()-last_join:.1f}s ago (reload)")
        return

    print(f"⚡ on_disconnect: SID={request.sid} room={room} role={role} game={game_type} — starting grace period")

    # Snapshot winner SID NOW — by the time the timer fires the reloader's new SID
    # may be registered in sid_to_game under the winner role, causing the wrong player
    # to receive opponent_forfeit.
    _snap_winner_role = "Youth" if role == "Elderly" else "Elderly"
    _snap_winner_sid  = next(
        (s for s, i in sid_to_game.items()
         if i.get("room") == room and i.get("role") == _snap_winner_role),
        None
    )

    # Don't forfeit immediately — wait 30s grace for reconnect.
    def _commit_disconnect_forfeit():
        key = (room, role)
        with _pending_disconnects_lock:
            if key not in _pending_disconnects:
                return  # #yq added: was cancelled by reconnect (join_game)
            del _pending_disconnects[key]

        # #yq added: Remove from disconnecting set
        with _room_disconnecting_lock:
            if room in _room_disconnecting:
                _room_disconnecting[room].discard(role)
                if not _room_disconnecting[room]:
                    del _room_disconnecting[room]

        # Re-check game still alive after grace period
        still_alive = (
            (game_type == "memory"  and room in memory_states  and not memory_states[room].get("game_over")) or
            (game_type == "hangman" and room in hangman_states and not hangman_states[room].get("game_over"))
        )
        if not still_alive:
            return

        # #yq added: If the opponent also recently reconnected (was in _room_disconnecting but
        # their timer was cancelled), it means BOTH players reloaded — don't forfeit either
        opponent_role = "Youth" if role == "Elderly" else "Elderly"
        opponent_key = (room, opponent_role)
        with _pending_disconnects_lock:
            opponent_still_pending = opponent_key in _pending_disconnects
        # If opponent's timer is also still pending, both are mid-reload — skip forfeit
        if opponent_still_pending:
            print(f"⏳ Both players reloading room={room} — skipping forfeit for role={role}")
            return

        print(f"⚡ on_disconnect forfeit committed: room={room} role={role} game={game_type}")

        winner_role = "Youth" if role == "Elderly" else "Elderly"

        # Mark game over server-side
        if game_type == "memory" and room in memory_states:
            memory_states[room]["game_over"] = True
        elif game_type == "hangman" and room in hangman_states:
            hangman_states[room]["game_over"] = True

        # Apply streak penalty to the disconnecting player
        if user_id:
            try:
                conn = db_helper.get_connection()
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO user_streaks (user_id, daily_game_streak, winning_streak)
                        VALUES (?, 0, 0)
                    """, (user_id,))
                    conn.execute("""
                        UPDATE user_streaks
                        SET daily_game_streak = MAX(0, daily_game_streak - 1),
                            winning_streak = 0
                        WHERE user_id = ?
                    """, (user_id,))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                print(f"❌ disconnect streak penalty error: {e}")

        # Use snapshot SID; validate it's still live and still maps to winner_role
        winner_sid = _snap_winner_sid
        if winner_sid:
            _si = sid_to_game.get(winner_sid, {})
            if _si.get("room") != room or _si.get("role") != winner_role:
                winner_sid = None
        socketio.emit("opponent_forfeit", {
            "game_type":   game_type,
            "winner_role": winner_role,
            "leaver_role": role,
        }, to=winner_sid if winner_sid else room)

        cleanup_room(room, game_type)

    # #yq added: Schedule forfeit after 6-second grace period
    import threading as _threading
    key = (room, role)
    timer = _threading.Timer(30.0, _commit_disconnect_forfeit)  # 30s covers any reload
    with _pending_disconnects_lock:
        old_timer = _pending_disconnects.get(key)
        if old_timer:
            old_timer.cancel()
        _pending_disconnects[key] = timer
    # #yq added: Track this role as mid-disconnect for this room
    with _room_disconnecting_lock:
        if room not in _room_disconnecting:
            _room_disconnecting[room] = set()
        _room_disconnecting[room].add(role)
    timer.start()





# =========================
# DEMO HELPERS (DM DATE / STREAK CONTROL)
# =========================

@app.route("/demo/reset_all")
def demo_reset_all():
    """
    Demo-only: wipes ALL messages (DM + community) and resets DM streak memory.
    Does NOT delete users/profiles.
    """
    conn = db_helper.get_connection()
    try:
        conn.execute("DELETE FROM messages")

        # ✅ ALSO clear persisted DM streak state
        conn.execute("DELETE FROM dm_streak_state")

        # safe reset of auto-increment
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
        except Exception:
            pass

        conn.commit()
    finally:
        conn.close()

    dm_streaks.clear()
    dm_sent_today.clear()
    dm_last_day.clear()

    session.pop("demo_date", None)

    return "✅ Reset done: messages cleared + DM streaks cleared + demo date cleared."


@app.route("/demo/set_date")
def demo_set_date():
    d = (request.args.get("date") or "").strip()
    if not d:
        return "Give ?date=YYYY-MM-DD", 400

    try:
        datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        return "Invalid format. Use YYYY-MM-DD", 400

    session["demo_date"] = d
    return f"✅ Demo date set to {d}"


@app.route("/demo/set_streak")
def demo_set_streak():
    if 'user_id' not in session:
        return "Not logged in", 403
    conn = db_helper.get_connection()
    conn.execute("""
        UPDATE user_streaks
        SET daily_game_streak = 5
        WHERE user_id = ?
    """, (session['user_id'],))
    conn.commit()
    conn.close()
    return "Daily streak set to 5!"


@app.route("/demo/day")
def demo_day():
    current = session.get("demo_date")

    if current:
        d = datetime.strptime(current, "%Y-%m-%d")
        d = d + timedelta(days=1)
    else:
        d = datetime.now() + timedelta(days=1)

    session["demo_date"] = d.strftime("%Y-%m-%d")
    return f"Demo date set to {session['demo_date']}"


# --- Yq ---
@app.route("/events_demo/set_date")
def events_demo_set_date():
    d = (request.args.get("date") or "").strip()
    if not d:
        return "Give ?date=YYYY-MM-DD", 400

    try:
        datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        return "Invalid format. Use YYYY-MM-DD", 400

    session["events_demo_date"] = d
    return f"✅ Events demo date set to {d}"

@app.route("/events_demo/set_streak")
def events_demo_set_streak():
    if "user_id" not in session:
        return "Not logged in", 401

    s = (request.args.get("streak") or "").strip()

    try:
        streak_val = int(s)
    except Exception:
        return "streak must be integer", 400

    user_id = session["user_id"]

    # Use EVENTS demo date, not real date
    demo_today = get_events_demo_date_str()
    yesterday = (
        datetime.strptime(demo_today, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    conn = db_helper.get_connection()
    try:
        # Ensure row exists (safe columns only)
        conn.execute("""
            INSERT OR IGNORE INTO user_streaks
            (user_id, daily_game_streak, winning_streak)
            VALUES (?, 0, 0)
        """, (user_id,))

        # ✅ USE last_play_date (matches DB)
        conn.execute("""
            UPDATE user_streaks
            SET daily_game_streak = ?,
                winning_streak = 0,
                last_play_date = ?,
                seed_claimed = 0
            WHERE user_id = ?
        """, (max(0, streak_val), yesterday, user_id))

        conn.commit()
    finally:
        conn.close()

    return f"✅ Events daily streak set to {streak_val}"


# --- Yq ---
queues = {"Elderly": [], "Youth": []}



# LEGACY_DUPLICATE_HANDLER (disabled to prevent double-processing)
# @socketio.on("flip_card")
# def flip_card(data):
#     room = data.get("room")
#     card_index = data.get("cardIndex")
#     role_in = (data.get("role") or "").strip().lower()
# 
#     role_map = {
#         "senior": "Elderly",
#         "elder": "Elderly",
#         "elderly": "Elderly",
#         "youth": "Youth",
#         "young": "Youth",
#     }
#     role = role_map.get(role_in, data.get("role"))
# 
#     if not room or card_index is None:
#         return
# 
#     emit("card_flipped", {
#         "cardIndex": card_index,
#         "role": role
#     }, room=room)
# 
# 
# 
# # Create queues for matching
# # Create queues for matching
waiting = {"Elderly": [], "Youth": []}
# 
# 
# 
# ===== ✅ Game state (reload / resync support) =====
# Server is the source of truth. Clients can request the latest state after reload.
memory_states = {}   # room_id -> state dict
hangman_states = {}  # room_id -> state dict

# ✅ NEW: Track players in each room for opponent name persistence
# room_id -> {"Elderly": {"user_id": int, "username": str}, "Youth": {...}}
room_players = {}

# sid -> {"room": str, "role": str, "game_type": str, "user_id": str}
sid_to_game = {}




def cleanup_room(room, game_type):
    if game_type == "memory":
        memory_states.pop(room, None)
    elif game_type == "hangman":
        hangman_states.pop(room, None)
    # ✅ Clean up room players tracking
    room_players.pop(room, None)
    # ✅ Clean up SID tracking for this room
    to_remove = [sid for sid, info in sid_to_game.items() if info.get("room") == room]
    for sid in to_remove:
        sid_to_game.pop(sid, None)

def hash_room(s: str) -> int:
    h = 0
    for ch in (s or ""):
        h = ((h << 5) - h) + ord(ch)
        h &= 0xFFFFFFFF
    return abs(int(h))

# yq added: Singapore-themed symbols for Memory Match — each maps to a memory_key
SG_MEMORY_SYMBOLS = [
    "🍜",   # laksa
    "🍞",   # kaya_toast
    "🥘",   # char_kway_teow
    "🍚",   # chicken_rice
    "🫓",   # roti_prata
    "🏠",   # kampong
    "🏪",   # hawker_centre
    "⛵",   # bumboat
    "🏘️",  # old_shophouse
    "🏮",   # lantern
    "🧧",   # cny
    "🪔",   # deepavali
]

# yq added: Lookup table — symbol → memory_key (used in pair_result emit)
SG_SYMBOL_TO_MEMORY_KEY = {
    "🍜":  "laksa",
    "🍞":  "kaya_toast",
    "🥘":  "char_kway_teow",
    "🍚":  "chicken_rice",
    "🫓":  "roti_prata",
    "🏠":  "kampong",
    "🏪":  "hawker_centre",
    "⛵":  "bumboat",
    "🏘️": "old_shophouse",
    "🏮":  "lantern",
    "🧧":  "cny",
    "🪔":  "deepavali",
}

# yq added: use Singapore-themed symbols instead of generic ones
def memory_default_state(room_id: str):
    deck = SG_MEMORY_SYMBOLS + SG_MEMORY_SYMBOLS
    rng = random.Random(hash_room(room_id))
    rng.shuffle(deck)
    start_turn = "Elderly" if (hash_room(room_id) % 2 == 0) else "Youth"
    return {
        "deck": deck,
        "matched": [],
        "scores": {"Elderly": 0, "Youth": 0},
        "current_turn": start_turn,
        "pairs_total": 12,
        "last_flip_role": None,
        "flipped": [],
        "game_over": False,

        # YQ added ADD TIMER STATE
        "turn_started_at": datetime.now(),
        "turn_seconds": 60   # 1 minute
    }

# yq added: Singapore-themed word categories for Hangman
HANGMAN_WORD_CATEGORIES = {
    "Singapore Food": [
        "LAKSA", "PRATA", "SATAY", "ROJAK", "RENDANG",
        "OTAH", "CHENDOL", "NASI", "POPIAH", "KUEH",
        "KAYA", "DURIAN", "MURTABAK", "HOKKIEN", "CARROT"
    ],
    "Old Singapore": [
        "KAMPONG", "BUMBOAT", "SHOPHOUSE", "HAWKER", "TRISHAW",
        "ATTAP", "JOSS", "LONGKANG", "STANDPIPE", "BULLOCK",
        "TONGKANG", "COOLIE", "SAMSUI", "RICKSHAW", "KETUPAT"
    ],
    "Singapore Festivals": [
        "LANTERN", "DEEPAVALI", "DIWALI", "MOONCAKE", "CHINGAY",
        "THAIPUSAM", "VESAK", "HARI", "RAYA", "HANTU",
        "QINGMING", "PONGAL", "NAVARATHRI", "SONGKRAN", "DRAGON"
    ],
    # "Nature": [
    #     "RIVER", "MOUNTAIN", "THUNDER", "FOUNTAIN", "SUNLIGHT",
    #     "VOLCANO", "RAINDROP", "GLACIER", "RAINBOW", "BLOSSOM",
    #     "TORNADO", "WATERFALL", "MEADOW", "PEBBLE", "SEASHELL"
    # ]
}

# yq added: Hangman word → memory_key (all SG hangman words unlock a memory)
HANGMAN_WORD_TO_MEMORY_KEY = {
    # Singapore Food
    "LAKSA":     "laksa",
    "PRATA":     "roti_prata",
    "SATAY":     "satay",
    "ROJAK":     "rojak",
    "RENDANG":   "rendang",
    "OTAH":      "otah",
    "CHENDOL":   "chendol",
    "NASI":      "nasi_lemak",
    "POPIAH":    "popiah",
    "KUEH":      "kueh",
    "KAYA":      "kaya_toast",
    "DURIAN":    "durian",
    "MURTABAK":  "murtabak",
    "HOKKIEN":   "hokkien_mee",
    "CARROT":    "carrot_cake",
    # Old Singapore
    "KAMPONG":   "kampong",
    "BUMBOAT":   "bumboat",
    "SHOPHOUSE": "old_shophouse",
    "HAWKER":    "hawker_centre",
    "TRISHAW":   "trishaw",
    "ATTAP":     "attap_house",
    "JOSS":      "joss_sticks",
    "LONGKANG":  "longkang",
    "STANDPIPE": "standpipe",
    "BULLOCK":   "bullock_cart",
    "TONGKANG":  "tongkang",
    "COOLIE":    "coolie",
    "SAMSUI":    "samsui_women",
    "RICKSHAW":  "rickshaw",
    "KETUPAT":   "ketupat",
    # Singapore Festivals
    "LANTERN":   "lantern",
    "DEEPAVALI": "deepavali",
    "DIWALI":    "deepavali",
    "MOONCAKE":  "mooncake",
    "CHINGAY":   "chingay",
    "THAIPUSAM": "thaipusam",
    "VESAK":     "vesak",
    "HARI":      "hari_raya",
    "RAYA":      "hari_raya",
    "HANTU":     "hungry_ghost",
    "QINGMING":  "qingming",
    "PONGAL":    "pongal",
    "NAVARATHRI":"navarathri",
    "SONGKRAN":  "songkran",
    "DRAGON":    "dragon_boat",
    # Nature words → no memory unlock (None implicitly)
}

# yq added: updated to use SG word categories and include memory_key in state
def hangman_default_state(room_id: str):
    """
    ✅ FIXED: Uses truly random word selection instead of deterministic hash.
    Now each game will have a different word, even for the same two players.
    """
    # yq added: pick from SG categories, fall back to Nature for non-SG words
    category = random.choice(list(HANGMAN_WORD_CATEGORIES.keys()))
    word = random.choice(HANGMAN_WORD_CATEGORIES[category])

    # yq added: look up memory_key for this word (None if not a SG memory word)
    memory_key = HANGMAN_WORD_TO_MEMORY_KEY.get(word)

    # Randomly choose starting player (50/50 chance)
    start_turn = random.choice(["Elderly", "Youth"])

    print(f"🎲 HANGMAN: New game in room {room_id}")
    print(f"   Selected word: {word}")
    print(f"   Starting player: {start_turn}")

    return {
        "word": word,
        "category": category,
        "guessed": [],
        "current_turn": start_turn,
        "game_over": False,
        "memory_key": memory_key,  # yq added: used by client to unlock memory on win

        # YQ ADDED ADD TIMER
        "turn_started_at": datetime.now(),
        "turn_seconds": 60   # 1 minute
    }

def serialize_memory_state(state):
    # yq added: compute time_remaining from server-side turn_started_at
    turn_seconds = state.get("turn_seconds", 60)
    started = state.get("turn_started_at")
    if started:
        elapsed = (datetime.now() - started).total_seconds()
        time_remaining = max(0, int(turn_seconds - elapsed))
    else:
        time_remaining = turn_seconds
    return {
        "game_type": "memory",
        "deck": state.get("deck", []),
        "matched": list(state.get("matched", [])),
        "scores": state.get("scores", {"Elderly": 0, "Youth": 0}),
        "current_turn": state.get("current_turn", "Elderly"),
        "pairs_total": int(state.get("pairs_total", 12)),
        "flipped": list(state.get("flipped", [])),
        "game_over": bool(state.get("game_over", False)),
        "time_remaining": time_remaining,  # yq added
        "turn_started_at_ts": started.timestamp() if started else None,  # yq added
    }

def serialize_hangman_state(state):
    word = state.get("word", "")
    guessed = list(state.get("guessed", []))
    
    # Create display string showing guessed letters
    display = " ".join([ch if ch in guessed else "_" for ch in word])

    # yq added: compute time_remaining from server-side turn_started_at
    turn_seconds = state.get("turn_seconds", 60)
    started = state.get("turn_started_at")
    if started:
        elapsed = (datetime.now() - started).total_seconds()
        time_remaining = max(0, int(turn_seconds - elapsed))
    else:
        time_remaining = turn_seconds
    
    return {
        "game_type": "hangman",
        "guessed": guessed,
        "current_turn": state.get("current_turn", "Elderly"),
        "game_over": bool(state.get("game_over", False)),
        "word_display": display,  # ✅ Add this
        "word_length": len(word),  # ✅ Add this
        "category": state.get("category", ""),  # ✅ Add category hint
        "time_remaining": time_remaining,  # yq added
        "turn_started_at_ts": started.timestamp() if started else None,  # yq added
        "memory_key": state.get("memory_key"),  # yq added: sent to client so winner can unlock memory
    }

# ✅ FIXED: Fetch usernames from database during matchmaking
@socketio.on("join_waiting_room")
def handle_waiting_room(data):
    user_id = str(data.get("user_id"))
    role_in = (data.get("role") or "").strip().lower()
    game_in = (data.get("game_type") or "").strip().lower()

    role_map = {
        "senior": "Elderly",
        "elder": "Elderly",
        "elderly": "Elderly",
        "youth": "Youth",
        "young": "Youth",
    }
    role = role_map.get(role_in)

    game_map = {
        "memory": "memory",
        "memory-match": "memory",
        "memory_match": "memory",
        "hangman": "hangman",
    }
    game_type = game_map.get(game_in)

    # ✅ FIXED: Fetch username from database
    username = db_helper.get_username_by_id(user_id)
    if not username:
        username = "Player"  # Fallback

    print("\n=== JOIN_WAITING_ROOM ===")
    print("RAW:", data)
    print("SID:", request.sid)
    print("NORMALIZED:", user_id, username, role, game_type)

    if role not in waiting:
        emit("queue_error", {"message": f"Bad role: {role_in}"}, to=request.sid)
        return

    if game_type not in ("memory", "hangman"):
        emit("queue_error", {"message": f"Bad game: {game_in}"}, to=request.sid)
        return

    opponent_role = "Youth" if role == "Elderly" else "Elderly"

    # Find opponent in opposite queue with same game
    opponent_index = None
    for i, p in enumerate(waiting[opponent_role]):
        if p.get("game_type") == game_type:
            opponent_index = i
            break

    if opponent_index is not None:
        opponent = waiting[opponent_role].pop(opponent_index)
        # ✅ FIXED: Add timestamp to room ID to ensure unique rooms (and thus unique words)
        import time
        room_id = f"room_{opponent['user_id']}_{user_id}_{game_type}_{int(time.time())}"

        # ✅ FIXED: Get usernames (opponent already has username in queue)
        opponent_username = opponent.get("username", "Unknown")

        print("✅ MATCHED:", username, "("+role+")", "vs", opponent_username, "("+opponent_role+")", "ROOM:", room_id)
        
        room_players[room_id] = {
            role: {"user_id": user_id, "username": username},
            opponent_role: {"user_id": opponent["user_id"], "username": opponent_username}
        }

        
        join_room(room_id, sid=request.sid)
        join_room(room_id, sid=opponent["sid"])

        # ✅ FIXED: Send opponent usernames to both players
        emit("match_found", {
            "room": room_id,
            "your_role": role,
            "opponent_role": opponent_role,
            "opponent_username": opponent_username  # ← Critical fix
        }, to=request.sid)

        emit("match_found", {
            "room": room_id,
            "your_role": opponent_role,
            "opponent_role": role,
            "opponent_username": username  # ← Critical fix
        }, to=opponent["sid"])

    else:
        # ✅ FIXED: Store username in queue for when match is found
        waiting[role].append({
            "sid": request.sid, 
            "user_id": user_id, 
            "username": username,  # ← Store username
            "game_type": game_type
        })
        emit("queued", {"message": "Waiting for opponent..."}, to=request.sid)
        print("⏳ QUEUED:", username, role, game_type, "QUEUE SIZES:", {k: len(v) for k, v in waiting.items()})

def name_with_region(player: dict) -> str:
    if not player:
        return "Someone"
    uid = player.get("user_id")
    uname = player.get("username", "Someone")
    region = db_helper.get_user_region(uid) if uid else "Unknown"
    return f"<b>{uname}</b> ({region})"


@socketio.on("cancel_queue")
def cancel_queue(data):
    user_id = data.get("user_id")
    role = data.get("role")

    role_map = {"Senior": "Elderly", "Elder": "Elderly"}
    role = role_map.get(role, role)

    if role not in waiting:
        return

    waiting[role] = [p for p in waiting[role] if p.get("user_id") != user_id and p.get("sid") != request.sid]
    emit("queue_cancelled", {"message": "Left queue"}, to=request.sid)

@app.route("/events/waitingroom")
def waiting_room():
    if "user_id" not in session:
        return redirect(url_for("login"))

    game = request.args.get("game", "hangman")  # hangman or memory
    return render_template(
        "events/waitingroom.html",
        game_type=game,
        user_id=session["user_id"],
        role=session.get("role", "Youth")
    )


@socketio.on("flip_card")
def flip_card(data):
    """Memory Match: server-authoritative flips.
    - Player may flip only when it's their turn.
    - Turn only changes AFTER 2nd flip and ONLY on mismatch.
    - On match, same player keeps turn.
    """
    room = (data.get("room") or "").strip()
    idx = data.get("index")
    if idx is None:
        idx = data.get("cardIndex")
    if idx is None:
        idx = data.get("card_index")
    role_in = (data.get("role") or "").strip().lower()

    role_map = {
        "senior": "Elderly",
        "elder": "Elderly",
        "elderly": "Elderly",
        "youth": "Youth",
        "young": "Youth",
    }
    role = role_map.get(role_in, data.get("role")) or ""

    if not room:
        return
    if room not in memory_states:
        memory_states[room] = memory_default_state(room)

    state = memory_states[room]

    # If game already over, just resync
    if state.get("game_over"):
        emit("sync_state", serialize_memory_state(state), room=room)
        return

    # Validate idx
    try:
        idx = int(idx)
    except Exception:
        return

    deck = state.get("deck") or []
    if idx < 0 or idx >= len(deck):
        return

    # Enforce turn strictly
    if role and state.get("current_turn") and role != state.get("current_turn"):
        # Not your turn -> just resync you (and room) so UI stays correct
        emit("sync_state", serialize_memory_state(state), to=request.sid)
        return

    matched = set(state.get("matched") or [])
    flipped = list(state.get("flipped") or [])

    # Can't flip matched cards
    if idx in matched:
        return

    # Prevent flipping same card twice in a pair
    if idx in flipped:
        return

    # Only allow up to 2 flips before resolution
    if len(flipped) >= 2:
        return

    flipped.append(idx)
    state["flipped"] = flipped
    state["last_flip_role"] = role or state.get("current_turn")

    # After 1st flip: broadcast lightweight flip to BOTH players instantly (no round-trip delay)
    if len(flipped) == 1:
        emit("card_flipped", {"index": idx, "symbol": deck[idx]}, room=room)
        return
    
    # After 2nd flip, show both cards first here
    # emit("sync_state", serialize_memory_state(state), room=room)

    # Broadcast 2nd flip immediately so both players see it before pair resolution
    emit("card_flipped", {"index": idx, "symbol": deck[idx]}, room=room)

    # After 2nd flip: resolve pair
    a, b = flipped[0], flipped[1]
    sym_a = deck[a]
    sym_b = deck[b]
    owner = state.get("last_flip_role") or role or state.get("current_turn")
    is_match = (sym_a == sym_b)
    #NEW: Emit sync_state BEFORE processing so both cards are visible
    # emit("sync_state", serialize_memory_state(state), room=room)

    if is_match:
        matched.update([a, b])
        state["matched"] = list(matched)
        scores = state.get("scores") or {}
        scores[owner] = int(scores.get(owner, 0)) + 1
        state["scores"] = scores
        # #yq added: Keep turn on match — explicitly persist so sync_state always has correct turn
        next_turn = state.get("current_turn") or owner
        state["current_turn"] = next_turn  # persist (unchanged, but explicit)
        state["turn_started_at"] = datetime.now() # yq added: reset timer on each move (match keeps same player)
    else:
        # Switch turn on mismatch
        curr = state.get("current_turn") or owner
        next_turn = "Youth" if curr == "Elderly" else "Elderly"
        state["current_turn"] = next_turn
        state["turn_started_at"] = datetime.now() # yq added: reset timer on each move

    # Clear flipped after telling clients; clients will handle flip-back animation
    state["flipped"] = []
    state["last_flip_role"] = None

    # Game over?
    pairs_total = int(state.get("pairs_total", 0) or 0)
    total_scored = int((state.get("scores") or {}).get("Elderly", 0)) + int((state.get("scores") or {}).get("Youth", 0))
    if pairs_total and total_scored >= pairs_total:
        state["game_over"] = True

        scores = state.get("scores", {})
        e = int(scores.get("Elderly", 0))
        y = int(scores.get("Youth", 0))

        winner_role = None
        if e > y:
            winner_role = "Elderly"
        elif y > e:
            winner_role = "Youth"

        if winner_role:
            loser_role = "Youth" if winner_role == "Elderly" else "Elderly"

            winner = (room_players.get(room, {}).get(winner_role) or {})
            loser  = (room_players.get(room, {}).get(loser_role) or {})

            winner_region = db_helper.get_user_region(winner.get("user_id"))
            loser_region  = db_helper.get_user_region(loser.get("user_id"))

            winner_label = name_with_region(winner)
            loser_label  = name_with_region(loser)

            db_helper.add_notice(
                username=winner.get("username", "Someone"),
                region=winner_region,
                emoji="🏆",
                message=_("%(winner)s won Memory Match against %(loser)s!",
          winner=winner_label,
          loser=loser_label)
            )

            db_helper.add_notice(
                username=loser.get("username", "Someone"),
                region=loser_region,
                emoji="💔",
                message=_("%(loser)s lost Memory Match to %(winner)s.",
          loser=loser_label,
          winner=winner_label)
            )
        else:
            # draw notice (optional)
            p1 = room_players.get(room, {}).get("Elderly") or {}
            p2 = room_players.get(room, {}).get("Youth") or {}

            p1_region = db_helper.get_user_region(p1.get("user_id"))
            p2_region = db_helper.get_user_region(p2.get("user_id"))

            p1_label = name_with_region(p1)
            p2_label = name_with_region(p2)

            db_helper.add_notice(
                username=p1.get("username","Someone"),
                region=p1_region,
                emoji="🤝",
               message=_("%(p1)s drew Memory Match with %(p2)s.",
          p1=p1_label,
          p2=p2_label)
            )
            db_helper.add_notice(
                username=p2.get("username","Someone"),
                region=p2_region,
                emoji="🤝",
                message=_("%(p2)s drew Memory Match with %(p1)s.",
          p2=p2_label,
          p1=p1_label)
            )


        cleanup_room(room, "memory")


    emit("pair_result", {
        "a": a,
        "b": b,
        "is_match": is_match,
        "next_turn": next_turn,
        "scores": state.get("scores", {}),
        "game_over": state.get("game_over", False),
        "memory_key": SG_SYMBOL_TO_MEMORY_KEY.get(sym_a) if is_match else None,  # yq added
    }, room=room)
    state["flipped"] = []
    # emit("sync_state", serialize_memory_state(state), room=room)


# ✅ FIXED: Send opponent name on join_game (for page reload)
@socketio.on("join_game")
def handle_join_game(data):
    room = (data.get("room") or "").strip()
    role = (data.get("role") or "").strip()
    game_type = (data.get("game_type") or "").strip().lower()

    if not room:
        return

    join_room(room)
    print("✅ join_game:", request.sid, "joined", room, "role:", role, "game_type:", game_type)

    # Record join time — used in on_disconnect to detect if this is a reload
    # (with polling transport, on_disconnect for the old SID can fire AFTER join_game for the new SID)
    key = (room, role)
    with _recent_joins_lock:
        _recent_joins[key] = _time.time()

    # Cancel any pending disconnect forfeit for this (room, role) — player reconnected
    with _pending_disconnects_lock:
        timer = _pending_disconnects.pop(key, None)
    if timer:
        timer.cancel()
        print(f"✅ Reload detected — cancelled pending forfeit for room={room} role={role}")
    # #yq added: Remove from disconnecting set (player is back)
    with _room_disconnecting_lock:
        if room in _room_disconnecting:
            _room_disconnecting[room].discard(role)
            if not _room_disconnecting[room]:
                del _room_disconnecting[room]

    # ✅ Track SID → game info so on_disconnect can forfeit correctly
    sid_to_game[request.sid] = {
        "room": room,
        "role": role,
        "game_type": game_type,
        "user_id": session.get("user_id"),
    }

    # ✅ FIXED: Send opponent name when player joins/rejoins
    if room in room_players:
        opponent_role = "Youth" if role == "Elderly" else "Elderly"
        opponent_username = (room_players[room].get(opponent_role) or {}).get("username")


        if opponent_username:
            print(f"📤 Sending opponent name to {request.sid}: {opponent_username}")
            emit("opponent_info", {
                "opponent_username": opponent_username
            }, to=request.sid)
        else:
            print(f"⚠️ No opponent username found for role {opponent_role} in room {room}")

    # Ensure state exists for reload/resync
    if game_type == "memory":
        if room not in memory_states:
            memory_states[room] = memory_default_state(room)

    elif game_type == "hangman":
        if room not in hangman_states:
            hangman_states[room] = hangman_default_state(room)

    # #yq added: Only emit player_joined if the game hasn't started yet (initial join)
    # On reconnect (reload), the game is already in progress — don't broadcast player_joined
    # as it confuses the opponent's client mid-game
    game_in_progress = (
        (game_type == "memory"  and room in memory_states  and
         (memory_states[room].get("matched") or memory_states[room].get("scores", {}).get("Elderly", 0) > 0 or
          memory_states[room].get("scores", {}).get("Youth", 0) > 0)) or
        (game_type == "hangman" and room in hangman_states and
         hangman_states[room].get("guessed"))
    )
    if not game_in_progress:
        emit("player_joined", {"role": role}, room=room)

    # Send current state to the joining client only
    if game_type == "memory" and room in memory_states:
        emit("sync_state", serialize_memory_state(memory_states[room]), to=request.sid)

    if game_type == "hangman" and room in hangman_states:
        emit("sync_state", serialize_hangman_state(hangman_states[room]), to=request.sid)


@socketio.on("request_state")
def handle_request_state(data):
    room = (data.get("room") or "").strip()
    game_type = (data.get("game_type") or "").strip().lower()
    if not room:
        return

    if game_type == "memory":
        if room not in memory_states:
            memory_states[room] = memory_default_state(room)
        emit("sync_state", serialize_memory_state(memory_states[room]), to=request.sid)

    elif game_type == "hangman":
        if room not in hangman_states:
            hangman_states[room] = hangman_default_state(room)
        emit("sync_state", serialize_hangman_state(hangman_states[room]), to=request.sid)


@socketio.on("submit_guess")
def handle_submit_guess(data):
    # Hangman guess (server-authoritative)
    room = (data.get("room") or "").strip()
    letter = (data.get("letter") or "").strip()
    role_in = (data.get("role") or "").strip().lower()

    role_map = {
        "senior": "Elderly",
        "elder": "Elderly",
        "elderly": "Elderly",
        "youth": "Youth",
        "young": "Youth",
    }
    role = role_map.get(role_in, data.get("role"))

    if not room or not letter or not role:
        return

    letter = letter[0].upper()

    if room not in hangman_states:
        hangman_states[room] = hangman_default_state(room)
    state = hangman_states[room]

    print(f"BEFORE - Current turn: {state.get('current_turn')}, Guesser: {role}, Letter: {letter}")

    if state.get("game_over"):
        return

    if role != state.get("current_turn"):
        print(f"REJECTED - Not {role}'s turn (current: {state.get('current_turn')})")
        return

    if letter in state.get("guessed", []):
        return

    state["guessed"].append(letter)

    correct = letter in state.get("word", "")
    print(f"Letter {letter} is {'CORRECT' if correct else 'WRONG'} (word: {state.get('word')})")
    
    # ✅ Switch turn ONLY if guess was wrong
    if not correct:
        old_turn = state["current_turn"]
        state["current_turn"] = "Youth" if state["current_turn"] == "Elderly" else "Elderly"
        print(f"TURN SWITCHED: {old_turn} -> {state['current_turn']}")
        state["turn_started_at"] = datetime.now() # yq added: reset timer on turn switch
    else:
        print(f"CORRECT GUESS - Turn stays: {state['current_turn']}")
        state["turn_started_at"] = datetime.now() # yq added: reset timer on correct guess too

    # ✅ Check if game is won
    if all(ch in state["guessed"] for ch in state["word"]):
        state["game_over"] = True

        # ✅ winner/loser info from room_players
        winner_role = role
        loser_role = "Youth" if winner_role == "Elderly" else "Elderly"

        winner = (room_players.get(room, {}).get(winner_role) or {})
        loser  = (room_players.get(room, {}).get(loser_role) or {})

        winner_region = db_helper.get_user_region(winner.get("user_id"))
        loser_region  = db_helper.get_user_region(loser.get("user_id"))

        winner_label = name_with_region(winner)
        loser_label  = name_with_region(loser)

        # ✅ Winner region board
        db_helper.add_notice(
            username=winner.get("username", "Someone"),
            region=winner_region,
            emoji="🏆",
            message=f"{winner_label} won Hangman against {loser_label}!"
        )

        # ✅ Loser region board
        db_helper.add_notice(
            username=loser.get("username", "Someone"),
            region=loser_region,
            emoji="💔",
           message=_("%(loser)s lost Hangman to %(winner)s.",
          loser=loser_label,
          winner=winner_label)
        )


        cleanup_room(room, "hangman")


    print(f"AFTER - Current turn: {state.get('current_turn')}")

    # ✅ Send complete game state to both clients
    # emit("game_update", {
    #     "letter": letter,
    #     "guesser_role": role,
    #     "correct": correct,
    #     "current_turn": state.get("current_turn"),
    #     "guessed": state.get("guessed", []),
    #     "game_over": state.get("game_over", False)
    # }, room=room)
    
    # print(f"EMITTED game_update with current_turn: {state.get('current_turn')}")
    emit("game_update", {
    "letter": letter,
    "guesser_role": role,
    "correct": correct,
    "current_turn": state.get("current_turn"),
    "guessed": state.get("guessed", []),
    "word_display": " ".join(
        ch if ch in state["guessed"] else "_" 
        for ch in state["word"]
    ),
    "game_over": state.get("game_over", False)
    }, room=room)

#Changed
@socketio.on("forfeit_game")
def handle_forfeit(data):
    """If a player leaves mid-game, the opponent instantly wins."""
    room = (data.get("room") or "").strip()
    game_type = (data.get("game_type") or "").strip().lower()
    role_in = (data.get("role") or "").strip().lower()

    role_map = {
        "senior": "Elderly",
        "elder": "Elderly",
        "elderly": "Elderly",
        "youth": "Youth",
        "young": "Youth",
    }
    leaver_role = role_map.get(role_in, data.get("role")) or ""

    if not room:
        return


    winner_role = "Youth" if leaver_role == "Elderly" else "Elderly"

    # Mark server state as game over so reload doesn't revive the match
    if game_type == "memory":
        if room in memory_states:
            memory_states[room]["game_over"] = True
    elif game_type == "hangman":
        if room in hangman_states:
            hangman_states[room]["game_over"] = True

    # #yq added: Emit ONLY to the winner's socket, not the whole room
    # (emitting to room sends it to the leaver too, causing false win popup on reconnect)
    winner_sid = None
    for sid, info in sid_to_game.items():
        if info.get("room") == room and info.get("role") == winner_role:
            winner_sid = sid
            break

    if winner_sid:
        socketio.emit("opponent_forfeit", {
            "game_type": game_type,
            "winner_role": winner_role,
            "leaver_role": leaver_role
        }, to=winner_sid)
    else:
        # Fallback: emit to room if winner SID not found (e.g. winner also disconnected)
        emit("opponent_forfeit", {
            "game_type": game_type,
            "winner_role": winner_role,
            "leaver_role": leaver_role
        }, room=room)

    cleanup_room(room, game_type)


def cleanup_room(room, game_type):
    if game_type == "memory":
        memory_states.pop(room, None)
    elif game_type == "hangman":
        hangman_states.pop(room, None)

# yq added 
# ── In-memory store for pending forfeits ──────────────────────
# { room_id: { "leaver_role": str, "game_type": str, "ts": float } }
_pending_forfeits = {}
_pending_forfeits_lock = threading.Lock()

# #yq added: Grace-period disconnect store — prevents reload from triggering forfeit
# Key: (room, role)  Value: threading.Timer
_pending_disconnects = {}
_pending_disconnects_lock = threading.Lock()

# #yq added: Track how many players in a room are currently mid-disconnect (reconnecting)
# Key: room  Value: set of roles that are mid-disconnect
_room_disconnecting = {}
_room_disconnecting_lock = threading.Lock()

# Track recent join_game calls so on_disconnect can skip forfeit for reloaders.
# Key: (room, role)  Value: timestamp of last join_game
# With polling transport, on_disconnect for the OLD sid fires AFTER join_game for the new sid.
# By checking this dict in on_disconnect we skip starting a new grace timer for reloaders.
import time as _time
_recent_joins = {}
_recent_joins_lock = threading.Lock()
_RECENT_JOIN_WINDOW = 60  # seconds — any join within this window = treat as reload


@app.route("/api/forfeit_beacon", methods=["POST"])
def forfeit_beacon():
    """Beacon endpoint kept alive but intentionally ignored.
    Fires on reload AND tab-close — indistinguishable. The 30s disconnect
    grace period handles true closes without needing the beacon."""
    return ("", 204)


@app.route("/api/check_forfeit", methods=["GET"])
def check_forfeit():
    """
    Polled by the opponent every few seconds to detect if the other player
    closed their browser (beacon forfeit).

    Query params:
      room      – the game room ID
      role      – the polling player's own role (so we skip forfeits they caused)
    """
    import time

    room = (request.args.get("room") or "").strip()
    role = (request.args.get("role") or "").strip()

    if not room:
        return jsonify({"forfeit": False})

    with _pending_forfeits_lock:
        entry = _pending_forfeits.get(room)

    if not entry:
        return jsonify({"forfeit": False})

    # Only return if we are the WINNER (i.e. the other player left)
    if entry["winner_role"] != role:
        return jsonify({"forfeit": False})

    # Clean up entries older than 30 seconds to avoid memory leak
    with _pending_forfeits_lock:
        now = time.time()
        stale = [r for r, v in _pending_forfeits.items() if now - v["ts"] > 30]
        for r in stale:
            del _pending_forfeits[r]
        # remove this one now that it's been consumed
        _pending_forfeits.pop(room, None)

    return jsonify({
        "forfeit":     True,
        "winner_role": entry["winner_role"],
        "leaver_role": entry["leaver_role"],
        "game_type":   entry["game_type"],
    })

# Yq added
def check_turn_timeout(room, state, game_type):
    if state.get("game_over"):
        return

    started = state.get("turn_started_at")
    if not started:
        return

    elapsed = (datetime.now() - started).total_seconds()
    remaining = state.get("turn_seconds", 60) - elapsed

    if remaining <= 0:
        state["game_over"] = True

        loser_role = state.get("current_turn")
        winner_role = "Youth" if loser_role == "Elderly" else "Elderly"

        socketio.emit("opponent_forfeit", {
            "game_type": game_type,
            "winner_role": winner_role,
            "leaver_role": loser_role,
            "timeout": True
        }, room=room)

        cleanup_room(room, game_type)




# Don't delete this part 
if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5000, debug=True, use_reloader=False)
