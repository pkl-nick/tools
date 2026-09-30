from flask import (Flask, render_template, request, jsonify, session, redirect, url_for,
                   flash, g, send_from_directory, abort)
import os
import uuid
from datetime import date, datetime
import db_builder
import listing_utils

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['DB_PATH'] = db_builder.get_db_path()
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', os.path.join('instance', 'uploads'))
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024   # per request; photos upload one at a time

ALLOWED_IMAGE_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'webp': 'image/webp', 'gif': 'image/gif',
}

# Databases that already have their schema, keyed by path
_initialized_dbs = set()


def get_db():
    """Per-request SQLite connection; creates and seeds the database on first use"""
    if 'db' not in g:
        path = app.config['DB_PATH']
        g.db = db_builder.get_connection(path)
        if path not in _initialized_dbs:
            db_builder.init_database(g.db)
            _initialized_dbs.add(path)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def get_current_user():
    """Demo auth: the session picks which seeded neighbor you are acting as"""
    db = get_db()
    user = None
    if 'user_id' in session:
        user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if user is None:
        # Default to the demo renter (last seeded user)
        user = db.execute('SELECT * FROM users ORDER BY id DESC LIMIT 1').fetchone()
        session['user_id'] = user['id']
    return user


def user_platforms(user):
    return [p for p in user['battery_platforms'].split(',') if p]


@app.context_processor
def inject_globals():
    user = get_current_user()
    db = get_db()
    return {
        'current_user': user,
        'all_users': db.execute('SELECT id, name FROM users ORDER BY id').fetchall(),
        'draft_count': db.execute(
            "SELECT COUNT(*) FROM tools WHERE owner_id = ? AND status = 'draft'", (user['id'],)
        ).fetchone()[0],
        'ai_live': listing_utils.ai_is_configured(),
    }


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def get_listed_tool(tool_id):
    tool = get_db().execute(
        '''SELECT t.*, u.name AS owner_name, u.neighborhood, u.lat, u.lng
           FROM tools t JOIN users u ON t.owner_id = u.id
           WHERE t.id = ? AND t.status = 'listed' ''', (tool_id,)
    ).fetchone()
    if tool is None:
        abort(404)
    return tool


def has_overlap(tool_id, start, end, exclude_booking_id=None):
    """True if an accepted booking already covers any day in [start, end]"""
    row = get_db().execute(
        '''SELECT COUNT(*) FROM bookings
           WHERE tool_id = ? AND status = 'accepted' AND id != ?
             AND start_date <= ? AND end_date >= ?''',
        (tool_id, exclude_booking_id or -1, end.isoformat(), start.isoformat())
    ).fetchone()
    return row[0] > 0


def build_quote(tool, renter, start, end, delivery, hour):
    miles = listing_utils.distance_miles(renter['lat'], renter['lng'], tool['lat'], tool['lng'])
    days = listing_utils.rental_days(start, end)
    result = listing_utils.quote(tool['daily_price'], tool['deposit'], days,
                                 delivery=delivery, miles=miles, hour=hour)
    result['miles'] = round(miles, 1)
    result['delivery_available'] = miles <= listing_utils.DELIVERY_MAX_MILES
    return result


# ---------------------------------------------------------------------------
# Browse and rent
# ---------------------------------------------------------------------------

@app.route('/')
def browse():
    """Browse listed tools near the current user"""
    user = get_current_user()
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '')
    platform = request.args.get('platform', '')
    view = 'gallery' if request.args.get('view') == 'gallery' else 'list'
    try:
        max_miles = float(request.args.get('max_miles', 10))
    except ValueError:
        max_miles = 10.0

    sql = '''SELECT t.*, u.name AS owner_name, u.neighborhood, u.lat, u.lng
             FROM tools t JOIN users u ON t.owner_id = u.id
             WHERE t.status = 'listed' '''
    params = []
    if q:
        sql += ' AND (t.name LIKE ? OR t.brand LIKE ? OR t.model LIKE ? OR t.description LIKE ?)'
        params += [f'%{q}%'] * 4
    if category in listing_utils.CATEGORIES:
        sql += ' AND t.category = ?'
        params.append(category)

    platforms = []
    if platform == 'mine':
        platforms = user_platforms(user)
    elif platform in listing_utils.BATTERY_PLATFORMS:
        platforms = [platform]
    if platform:
        # Filter to tools that run on these batteries (empty list matches nothing)
        sql += f" AND t.battery_platform IN ({','.join('?' for _ in platforms) or 'NULL'})"
        params += platforms

    tools = []
    for row in get_db().execute(sql, params).fetchall():
        miles = listing_utils.distance_miles(user['lat'], user['lng'], row['lat'], row['lng'])
        if miles <= max_miles:
            tools.append({**dict(row), 'miles': round(miles, 1)})
    tools.sort(key=lambda t: t['miles'])

    return render_template('browse.html', tools=tools, q=q, category=category, platform=platform,
                           max_miles=max_miles, view=view, categories=listing_utils.CATEGORIES,
                           platforms=listing_utils.BATTERY_PLATFORMS,
                           my_platforms=user_platforms(user))


@app.route('/tool/<int:tool_id>')
def tool_detail(tool_id):
    user = get_current_user()
    tool = get_listed_tool(tool_id)
    miles = listing_utils.distance_miles(user['lat'], user['lng'], tool['lat'], tool['lng'])
    booked = get_db().execute(
        '''SELECT start_date, end_date FROM bookings
           WHERE tool_id = ? AND status = 'accepted' AND end_date >= ? ORDER BY start_date''',
        (tool_id, date.today().isoformat())
    ).fetchall()
    # Battery add-ons: batteries from the same owner that fit this tool
    addons = []
    if tool['battery_platform'] and tool['category'] != 'Batteries & Chargers':
        addons = get_db().execute(
            '''SELECT id, name, daily_price FROM tools
               WHERE owner_id = ? AND status = 'listed' AND category = 'Batteries & Chargers'
                 AND battery_platform = ?''',
            (tool['owner_id'], tool['battery_platform'])
        ).fetchall()
    return render_template('tool.html', tool=tool, miles=round(miles, 1), booked=booked, addons=addons,
                           today=date.today().isoformat(),
                           fits_my_batteries=tool['battery_platform'] in user_platforms(user),
                           delivery_max=listing_utils.DELIVERY_MAX_MILES)


@app.route('/api/quote')
def api_quote():
    """Live price quote for the booking form"""
    user = get_current_user()
    tool = get_listed_tool(request.args.get('tool_id', type=int))
    start = parse_date(request.args.get('start'))
    end = parse_date(request.args.get('end'))
    if not start or not end or end < start:
        return jsonify({'success': False, 'error': 'Pick a valid start and return date'}), 400
    delivery = request.args.get('delivery') == '1'
    hour = request.args.get('hour', 10, type=int) % 24
    result = build_quote(tool, user, start, end, delivery, hour)
    return jsonify({'success': True, **result})


@app.route('/tool/<int:tool_id>/book', methods=['POST'])
def book_tool(tool_id):
    user = get_current_user()
    tool = get_listed_tool(tool_id)

    if tool['owner_id'] == user['id']:
        flash("That's your own tool. Switch users to test renting it.", 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))

    start = parse_date(request.form.get('start'))
    end = parse_date(request.form.get('end'))
    if not start or not end or end < start or start < date.today():
        flash('Pick a start date from today on and a return date on or after it.', 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))

    if tool['risk_tier'] == 'waiver' and request.form.get('waiver') != 'on':
        flash('Please read and accept the safety acknowledgment for this tool.', 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))

    if has_overlap(tool_id, start, end):
        flash('That tool is already booked for some of those days.', 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))

    delivery = request.form.get('delivery') == '1'
    hour = request.form.get('hour', 10, type=int) % 24
    q = build_quote(tool, user, start, end, delivery, hour)
    if delivery and not q['delivery_available']:
        flash(f"Delivery is only offered within {listing_utils.DELIVERY_MAX_MILES} miles.", 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))

    get_db().execute(
        '''INSERT INTO bookings (tool_id, renter_id, start_date, end_date, days, rental_total, service_fee,
                                 delivery, delivery_fee, deposit, total_charge, owner_payout, message,
                                 status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?)''',
        (tool_id, user['id'], start.isoformat(), end.isoformat(), q['days'], q['rental_total'],
         q['service_fee'], int(delivery), q['delivery_fee'], q['deposit_hold'], q['total_charge'],
         q['owner_payout'], request.form.get('message', '').strip()[:500],
         datetime.now().isoformat(timespec='seconds'))
    )
    get_db().commit()
    flash(f"Request sent to {tool['owner_name']}. You'll see it under My Garage.", 'success')
    return redirect(url_for('garage'))


# ---------------------------------------------------------------------------
# Snap-to-list
# ---------------------------------------------------------------------------

@app.route('/list')
def list_tools():
    """Upload garage photos to get AI-drafted listings"""
    return render_template('list.html')


@app.route('/api/analyze-photo', methods=['POST'])
def analyze_photo():
    """Analyze one photo and save a draft listing for each tool found"""
    user = get_current_user()
    photo = request.files.get('photo')
    if photo is None or not photo.filename:
        return jsonify({'success': False, 'error': 'No photo uploaded'}), 400

    ext = photo.filename.rsplit('.', 1)[-1].lower() if '.' in photo.filename else ''
    if ext not in ALLOWED_IMAGE_TYPES:
        return jsonify({'success': False,
                        'error': f'Unsupported file type ".{ext}". Use JPG, PNG, WEBP or GIF.'}), 400

    image_bytes = photo.read()
    if not image_bytes:
        return jsonify({'success': False, 'error': 'Photo is empty'}), 400

    try:
        source = 'video' if request.form.get('source') == 'video' else 'photo'
        drafts, cost, demo_mode, model_used = listing_utils.analyze_photo(
            image_bytes, ALLOWED_IMAGE_TYPES[ext], source=source)
    except Exception as e:
        print(f"Error analyzing photo: {e}")
        return jsonify({'success': False, 'error': f'Could not analyze photo: {e}'}), 502

    filename = f'{uuid.uuid4().hex}.{ext}'
    db = get_db()
    # Frames from one video walkthrough share a batch id; skip tools already drafted from an earlier frame
    batch_id = request.form.get('batch_id', '').strip()[:64] or None
    seen = set()
    if batch_id:
        seen = {listing_utils.draft_key(row) for row in db.execute(
            "SELECT name, brand, model FROM tools WHERE owner_id = ? AND batch_id = ? AND status = 'draft'",
            (user['id'], batch_id)
        ).fetchall()}

    created = []
    for draft in drafts:
        key = listing_utils.draft_key(draft)
        if key in seen:
            continue
        seen.add(key)
        db_builder.insert_tool(db, owner_id=user['id'], status='draft', photo_path=filename,
                               batch_id=batch_id, **draft)
        created.append(draft)

    # Keep the photo only if it produced a listing (it becomes the listing photo)
    if created:
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        with open(os.path.join(app.config['UPLOAD_FOLDER'], filename), 'wb') as f:
            f.write(image_bytes)
    db.commit()

    return jsonify({
        'success': True,
        'drafts_created': len(created),
        'duplicates_skipped': len(drafts) - len(created),
        'names': [d['name'] for d in created],
        'demo_mode': demo_mode,
        'model': model_used,
        'cost': cost,
    })


@app.route('/list/review', methods=['GET', 'POST'])
def review_drafts():
    """Bulk approve, edit or skip AI drafts"""
    user = get_current_user()
    db = get_db()
    drafts = db.execute(
        "SELECT * FROM tools WHERE owner_id = ? AND status = 'draft' ORDER BY id", (user['id'],)
    ).fetchall()

    if request.method == 'POST':
        listed = skipped = blocked = 0
        for draft in drafts:
            action = request.form.get(f'action_{draft["id"]}', 'keep')
            if action == 'skip':
                db.execute("UPDATE tools SET status = 'skipped' WHERE id = ?", (draft['id'],))
                skipped += 1
            elif action == 'approve':
                fields = {k: request.form.get(f'{k}_{draft["id"]}', draft[k]) for k in (
                    'name', 'brand', 'model', 'category', 'power_source', 'battery_platform',
                    'description', 'included_items', 'daily_price', 'deposit', 'replacement_value',
                    'risk_tier', 'safety_notes')}
                fields['confidence'] = draft['ai_confidence']
                clean = listing_utils.normalize_draft(fields)
                status = 'listed'
                if clean['risk_tier'] == 'excluded':
                    status = 'skipped'
                    blocked += 1
                else:
                    listed += 1
                sets = ', '.join(f'{k} = ?' for k in clean)
                db.execute(f'UPDATE tools SET {sets}, status = ? WHERE id = ?',
                           list(clean.values()) + [status, draft['id']])
        db.commit()
        msg = f'Listed {listed} tool{"s" if listed != 1 else ""}, skipped {skipped}.'
        if blocked:
            msg += f' {blocked} excluded for safety (chainsaws and similar) during the pilot.'
        flash(msg, 'success')
        return redirect(url_for('garage'))

    return render_template('review.html', drafts=drafts, categories=listing_utils.CATEGORIES,
                           power_sources=listing_utils.POWER_SOURCES,
                           platforms=listing_utils.BATTERY_PLATFORMS, risk_tiers=listing_utils.RISK_TIERS)


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(os.path.abspath(app.config['UPLOAD_FOLDER']), filename)


# ---------------------------------------------------------------------------
# My garage: listings, incoming requests, my rentals
# ---------------------------------------------------------------------------

@app.route('/garage')
def garage():
    user = get_current_user()
    db = get_db()
    listings = db.execute(
        "SELECT * FROM tools WHERE owner_id = ? AND status = 'listed' ORDER BY category, name", (user['id'],)
    ).fetchall()
    incoming = db.execute(
        '''SELECT b.*, t.name AS tool_name, u.name AS renter_name
           FROM bookings b JOIN tools t ON b.tool_id = t.id JOIN users u ON b.renter_id = u.id
           WHERE t.owner_id = ? ORDER BY b.created_at DESC''', (user['id'],)
    ).fetchall()
    rentals = db.execute(
        '''SELECT b.*, t.name AS tool_name, u.name AS owner_name
           FROM bookings b JOIN tools t ON b.tool_id = t.id JOIN users u ON t.owner_id = u.id
           WHERE b.renter_id = ? ORDER BY b.created_at DESC''', (user['id'],)
    ).fetchall()
    earnings = sum(b['owner_payout'] for b in incoming if b['status'] in ('accepted', 'returned'))
    return render_template('garage.html', listings=listings, incoming=incoming, rentals=rentals,
                           earnings=earnings)


# action -> (who may do it, statuses it applies to, new status)
BOOKING_ACTIONS = {
    'accept': ('owner', ('requested',), 'accepted'),
    'decline': ('owner', ('requested',), 'declined'),
    'returned': ('owner', ('accepted',), 'returned'),
    'cancel': ('renter', ('requested', 'accepted'), 'cancelled'),
}


@app.route('/booking/<int:booking_id>/<action>', methods=['POST'])
def update_booking(booking_id, action):
    if action not in BOOKING_ACTIONS:
        abort(404)
    user = get_current_user()
    db = get_db()
    booking = db.execute(
        '''SELECT b.*, t.owner_id FROM bookings b JOIN tools t ON b.tool_id = t.id WHERE b.id = ?''',
        (booking_id,)
    ).fetchone()
    if booking is None:
        abort(404)

    role, from_statuses, new_status = BOOKING_ACTIONS[action]
    actor_id = booking['owner_id'] if role == 'owner' else booking['renter_id']
    if actor_id != user['id']:
        abort(403)
    if booking['status'] not in from_statuses:
        flash(f"Can't {action} a booking that is {booking['status']}.", 'error')
        return redirect(url_for('garage'))
    if action == 'accept' and has_overlap(booking['tool_id'], date.fromisoformat(booking['start_date']),
                                          date.fromisoformat(booking['end_date']), booking_id):
        flash('You already accepted another rental for some of those days.', 'error')
        return redirect(url_for('garage'))

    db.execute('UPDATE bookings SET status = ? WHERE id = ?', (new_status, booking_id))
    db.commit()
    flash(f'Booking {new_status}.', 'success')
    return redirect(url_for('garage'))


@app.route('/tool/<int:tool_id>/unlist', methods=['POST'])
def unlist_tool(tool_id):
    user = get_current_user()
    db = get_db()
    cur = db.execute("UPDATE tools SET status = 'skipped' WHERE id = ? AND owner_id = ?", (tool_id, user['id']))
    db.commit()
    if cur.rowcount == 0:
        abort(404)
    flash('Listing removed.', 'success')
    return redirect(url_for('garage'))


@app.route('/switch-user/<int:user_id>', methods=['POST'])
def switch_user(user_id):
    if get_db().execute('SELECT id FROM users WHERE id = ?', (user_id,)).fetchone() is None:
        abort(404)
    session['user_id'] = user_id
    return redirect(request.referrer or url_for('browse'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8000, threaded=True)
