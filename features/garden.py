from flask import Blueprint, render_template, session, request, jsonify
from database import db_helper

garden_bp = Blueprint('garden', __name__, url_prefix='/garden')

def ensure_session():
    if 'user_id' not in session:
        session['user_id'] = 1
        session['username'] = 'Demo User'

@garden_bp.route('/')
def index():
    ensure_session()
    uid = session['user_id']

    user_row = db_helper.get_user_by_id(uid)
    user = dict(user_row) if user_row else {"points": 0}

    inv_row = db_helper.get_user_inventory(uid)
    inventory = dict(inv_row) if inv_row else {"seed_tree": 0, "seed_flower": 0, "water": 0}

    return render_template(
        'garden/garden_dashboard.html',
        user=user,
        inventory=inventory,
        plots=db_helper.get_user_plots(uid),
        my_rewards=db_helper.get_user_rewards(uid),
        all_rewards=db_helper.get_all_rewards()
    )


# --- API ROUTES ---

@garden_bp.route('/api/plant', methods=['POST'])
def api_plant():
    d = request.get_json() or {}
    uid = session['user_id']

    plot_id = d.get('plot_id')
    plant_type = d.get('plant_type')  # "tree" or "flower"

    # 1) Do the planting
    ok = db_helper.plant_seed(uid, plot_id, plant_type)

    # 2) If successful, add notice to community (region-only)
    if ok:
        region = db_helper.get_user_region(uid)   # make sure you added this helper
        username = session.get('username', 'Unknown')

        db_helper.log_garden_history(
            user_id=uid,
            category=plant_type,   # "tree" or "flower"
            title=f"Planted a {plant_type}",
            amount=-1
        )


        emoji = "🌳" if plant_type == "tree" else "🌸"
        db_helper.add_notice(
            username=username,
            region=region,
            emoji=emoji,
            message=f"<b>{username}</b> planted a <b>{plant_type}</b> in their garden!"
        )

    return jsonify({'success': ok})

@garden_bp.route('/api/water', methods=['POST'])
def api_water():
    d = request.get_json() or {}
    uid = session['user_id']

    ok = db_helper.water_plant(uid, d.get('plot_id'))

    if ok:
        region = db_helper.get_user_region(uid)
        username = session.get('username', 'Unknown')
        
        db_helper.log_garden_history(
            user_id=uid,
            category="water",
            title="Watered plant (-5 water)",
            amount=-5
        )

        db_helper.add_notice(
            username=username,
            region=region,
            emoji="💧",
            message=f"<b>{username}</b> watered their plant."
        )

    return jsonify({'success': ok})


@garden_bp.route('/api/harvest', methods=['POST'])
def api_harvest():
    d = request.get_json() or {}
    uid = session['user_id']
    plot_id = d.get('plot_id')

    # 1) Read plant_type BEFORE harvesting (harvest resets plant_type)
    conn = db_helper.get_connection()
    try:
        plot = conn.execute(
            "SELECT plant_type FROM plots WHERE id = ? AND user_id = ?",
            (plot_id, uid)
        ).fetchone()
    finally:
        conn.close()

    if not plot or not plot["plant_type"]:
        return jsonify({'success': False})

    plant_type = plot["plant_type"]  # "tree" or "flower"

    # 2) Harvest
    ok = db_helper.harvest_plant(uid, plot_id)

    # 3) If success, update community tree stats (SEPARATE from noticeboard)
    if ok:
        region = db_helper.get_user_region(uid)
        username = session.get('username', 'Unknown')
        action = "harvest_tree" if plant_type == "tree" else "harvest_flower"
        pts = 10 if plant_type == "tree" else 5
        emoji = "🌳" if plant_type == "tree" else "🌸"

        db_helper.log_garden_history(
            user_id=uid,
            category="points",
            title=f"Harvested {plant_type} (+{pts} pts)",
            amount=pts
        )

        # ✅ Community Tree Stats (NOT noticeboard)
        db_helper.add_tree_stat(
            user_id=uid,
            region=region,
            action=action,
            points=pts
        )

        # (Optional) Still show in noticeboard
        db_helper.add_notice(
            username=username,
            region=region,
            emoji="👨‍🌾",
            message=f"<b>{username}</b> harvested a {emoji} (+{pts} pts) for the community tree!"
        )

    return jsonify({'success': ok})



@garden_bp.route('/api/redeem', methods=['POST'])
def api_redeem():
    d = request.get_json()
    return jsonify({'success': db_helper.redeem_reward(session['user_id'], d.get('reward_id'))})

@garden_bp.route('/api/use_reward', methods=['POST'])
def api_use_reward():
    d = request.get_json()
    user_reward_id = d.get('user_reward_id')
    entered_pin = d.get('pin') # Get PIN from frontend

    # 1. Get Reward Details to check name
    # We need a helper to get details of a specific user_reward
    # For simplicity, we fetch all user rewards and find the match
    uid = session['user_id']
    my_rewards = db_helper.get_user_rewards(uid)
    
    target_reward = next((r for r in my_rewards if str(r['id']) == str(user_reward_id)), None)
    
    if not target_reward:
        return jsonify({'success': False, 'message': 'Reward not found'})

    reward_name = target_reward['name'].lower()
    
    # 2. PIN Validation Logic
    valid = False
    if 'shopee' in reward_name and entered_pin == '2354':
        valid = True
    elif 'popmart' in reward_name and entered_pin == '9156':
        valid = True
    elif 'fairprice' in reward_name and entered_pin == '3409':
        valid = True
    
    # 3. Process
    if valid:
        db_helper.use_reward(uid, user_reward_id)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'message': 'Invalid PIN Code'})
    
@garden_bp.route("/api/history")
def api_history():
    uid = session.get("user_id", 1)
    category = request.args.get("category")  # flower/tree/water/points
    items = db_helper.get_garden_history(uid, category=category, limit=30)
    return jsonify({"ok": True, "items": items})

@garden_bp.route("/history/<item_type>")
def garden_history(item_type):
    uid = session.get("user_id", 1)

    allowed = {"flower", "tree", "water", "points"}
    if item_type not in allowed:
        item_type = "points"

    logs = db_helper.get_garden_history(uid, item_type, limit=200)
    return render_template("garden/garden_history.html", logs=logs, item_type=item_type)


