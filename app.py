from flask import (Flask, render_template, request, jsonify, session, redirect, url_for,
                   flash, g, send_from_directory, abort, Response)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
import os
import re
import hmac
import json
import time
import uuid
import sqlite3
import secrets
import hashlib
import mimetypes
import functools
import tempfile
from datetime import date, datetime, timedelta
import db_builder
import listing_utils
import metrics
import places
import search
import delivery
import storage
from telemetry import track
from evals import runner as eval_runner
from evals.scoring import summarize as summarize_eval

DEV_SECRET = 'dev-secret-key-change-in-production'
ON_AZURE = bool(os.getenv('WEBSITE_SITE_NAME'))   # set by App Service

app = Flask(__name__)
# App Service terminates HTTPS in front of the app; trust its forwarded scheme and client address
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.getenv('FLASK_SECRET_KEY', DEV_SECRET)
app.config.update(
    DB_PATH=db_builder.get_db_path(),
    UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER', os.path.join('instance', 'uploads')),
    BACKUP_FOLDER=os.getenv('BACKUP_FOLDER', os.path.join('instance', 'backups')),
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,     # per request; photos upload one at a time
    SESSION_COOKIE_SECURE=ON_AZURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    CSRF_ENABLED=True,
    UPLOAD_DAILY_LIMIT=int(os.getenv('UPLOAD_DAILY_LIMIT', '150')),   # photos + video frames per user per 24h
    SHOW_DEMO_LISTINGS=os.getenv('SHOW_DEMO_LISTINGS', '1') == '1',
)
if ON_AZURE and app.secret_key == DEV_SECRET:
    raise RuntimeError('Set FLASK_SECRET_KEY in the App Service settings')

# Google / Microsoft sign-in. A provider is offered only when its client id and secret are set.
oauth = OAuth(app)
OAUTH_PROVIDERS = {
    'google': {
        'label': 'Google',
        'metadata': 'https://accounts.google.com/.well-known/openid-configuration',
    },
    'microsoft': {
        'label': 'Microsoft',
        # "common" accepts personal Microsoft accounts and work/school accounts
        'metadata': 'https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration',
    },
}


def provider_configured(name):
    prefix = name.upper()
    return bool(os.getenv(f'{prefix}_CLIENT_ID') and os.getenv(f'{prefix}_CLIENT_SECRET'))


def enabled_providers():
    return {k: v['label'] for k, v in OAUTH_PROVIDERS.items() if provider_configured(k)}


def oauth_client(name):
    client = oauth.create_client(name)
    if client is None:
        prefix = name.upper()
        oauth.register(name, client_id=os.getenv(f'{prefix}_CLIENT_ID'),
                       client_secret=os.getenv(f'{prefix}_CLIENT_SECRET'),
                       server_metadata_url=OAUTH_PROVIDERS[name]['metadata'],
                       client_kwargs={'scope': 'openid email profile'})
        client = oauth.create_client(name)
    return client


ALLOWED_IMAGE_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'webp': 'image/webp', 'gif': 'image/gif',
}
AVATAR_MAX_BYTES = 5 * 1024 * 1024
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MIN_PASSWORD = 8
ADMIN_SESSION_SECONDS = 12 * 3600

# Databases that already have their schema, keyed by path
_initialized_dbs = set()


def get_db():
    """Per-request SQLite connection; creates, seeds and migrates the database on first use"""
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


def media_store():
    return storage.get_store(storage.MEDIA_CONTAINER, app.config['UPLOAD_FOLDER'])


def backup_store():
    return storage.get_store(storage.BACKUP_CONTAINER, app.config['BACKUP_FOLDER'])


# ---------------------------------------------------------------------------
# Sessions, CSRF, rate limiting, telemetry hooks
# ---------------------------------------------------------------------------

def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def csrf_field():
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


# In-memory failed-attempt counter. The app runs one worker process, so this is shared by all requests.
_failures = {}


def too_many_failures(key, limit, window=900):
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < window]
    _failures[key] = recent
    return len(recent) >= limit


def record_failure(key):
    _failures.setdefault(key, []).append(time.time())


def client_ip():
    return request.remote_addr or 'unknown'


def visitor_id():
    return session.get('vid')


CSRF_EXEMPT = {'uber_direct_webhook'}     # verified by signature instead


@app.before_request
def before_request():
    if request.endpoint == 'static':
        return
    g.user = None
    uid = session.get('user_id')
    if uid:
        g.user = get_db().execute(
            'SELECT * FROM users WHERE id = ? AND is_demo = 0', (uid,)
        ).fetchone()
        if g.user is None:
            session.pop('user_id', None)
    if 'vid' not in session:
        session['vid'] = uuid.uuid4().hex

    # Accounts created through Google/Microsoft finish their profile before anything else
    if (g.user is not None and not (g.user['neighborhood'] and g.user['postal_code']) and request.method == 'GET'
            and request.endpoint not in ('welcome', 'logout', 'uploaded_file', 'oauth_start', 'oauth_callback')):
        return redirect(url_for('welcome', next=request.full_path if request.query_string else request.path))

    if request.method == 'POST' and app.config['CSRF_ENABLED'] and request.endpoint not in CSRF_EXEMPT:
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token') or ''
        if not session.get('csrf') or not hmac.compare_digest(sent, session['csrf']):
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Your session expired. Reload the page and try again.'}), 400
            flash('Your session expired. Please try again.', 'error')
            return redirect(request.referrer or url_for('browse'))


@app.after_request
def after_request(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    if ON_AZURE:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    if (request.method == 'GET' and response.status_code == 200 and response.mimetype == 'text/html'
            and request.endpoint not in (None, 'static')):
        db = get_db()
        track(db, 'page_view', user_id=g.user['id'] if g.get('user') else None,
              visitor=visitor_id(), path=request.endpoint)
        db.commit()
    return response


def safe_next(target):
    """Only allow redirects back into this site"""
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return url_for('browse')


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Please log in first'}), 401
            flash('Log in or create an account to continue.', 'error')
            return redirect(url_for('login', next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)
    return wrapped


def is_admin():
    return session.get('admin_until', 0) > time.time()


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return redirect(url_for('admin_login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def log_in(user):
    session.clear()
    session.permanent = True
    session['user_id'] = user['id']
    session['vid'] = uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def user_platforms(user):
    return [p for p in (user['battery_platforms'] if user else '').split(',') if p]


def viewer_location():
    """Logged-in users measure distance from their own location; visitors from the service area center"""
    if g.get('user'):
        return g.user['lat'], g.user['lng']
    area = session.get('area')
    if area:
        return area['lat'], area['lng']
    return db_builder.DEMO_CENTER


def visitor_area_name():
    area = session.get('area')
    if area:
        return area['city'] or area['postal_code']
    return os.getenv('SERVICE_AREA_NAME', 'denver')


@app.template_filter('money')
def money_filter(amount):
    """$4 for whole dollars, $4.50 otherwise"""
    amount = float(amount or 0)
    return f'${amount:.0f}' if amount == int(amount) else f'${amount:.2f}'


@app.template_filter('initials')
def initials_filter(name):
    parts = [p for p in re.split(r'\s+', (name or '').strip()) if p and p[0].isalnum()]
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


@app.context_processor
def inject_globals():
    user = g.get('user')
    draft_count = 0
    if user:
        draft_count = get_db().execute(
            "SELECT COUNT(*) FROM tools WHERE owner_id = ? AND status = 'draft'", (user['id'],)
        ).fetchone()[0]
    return {
        'current_user': user,
        'draft_count': draft_count,
        'ai_live': listing_utils.ai_is_configured(),
        'csrf_token': csrf_token,
        'csrf_field': csrf_field,
        'is_admin': is_admin(),
        'area_name': visitor_area_name(),
        'visitor_area': session.get('area'),
        'sign_in_providers': enabled_providers(),
        'place_label': place_label,
    }


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def get_listed_tool(tool_id):
    tool = get_db().execute(
        '''SELECT t.*, u.name AS owner_name, u.neighborhood, u.city, u.region,
                  COALESCE(t.pickup_lat, u.lat) AS lat, COALESCE(t.pickup_lng, u.lng) AS lng, u.avatar_path AS owner_avatar,
                  u.is_demo AS owner_is_demo, u.created_at AS owner_since, u.offers_delivery AS owner_offers_delivery,
                  u.street_address AS owner_street, u.phone AS owner_phone, u.postal_code AS owner_postal
           FROM tools t JOIN users u ON t.owner_id = u.id
           WHERE t.id = ? AND t.status = 'listed' ''', (tool_id,)
    ).fetchone()
    if tool is None or (tool['owner_is_demo'] and not app.config['SHOW_DEMO_LISTINGS']):
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


def owner_delivers(tool, miles):
    return bool(tool['owner_offers_delivery']) and miles <= listing_utils.DELIVERY_MAX_MILES


def courier_available(tool):
    """Uber Direct is set up, the tool fits in a car, and the owner gave a pickup address and phone"""
    return (delivery.configured() and not tool['owner_is_demo']
            and delivery.courier_eligible(tool['courier_size'], tool['weight_lbs'])
            and bool(tool['owner_street'] and tool['owner_phone'] and tool['owner_postal']))


def build_quote(tool, start, end, method, hour, courier_fee=None):
    """method: pickup | owner (owner drives it over) | courier (Uber Direct, round trip)"""
    lat, lng = viewer_location()
    miles = listing_utils.distance_miles(lat, lng, tool['lat'], tool['lng'])
    days = listing_utils.rental_days(start, end)
    result = listing_utils.quote(tool['daily_price'], tool['deposit'], days,
                                 delivery=method != 'pickup', miles=miles, hour=hour,
                                 delivery_fee_override=courier_fee if method == 'courier' else None,
                                 delivery_to_owner=method == 'owner')
    result['miles'] = round(miles, 1)
    result['method'] = method
    result['delivery_available'] = owner_delivers(tool, miles)
    return result


def owner_place(tool):
    """Where a courier picks the tool up"""
    return {'name': tool['owner_name'], 'phone': tool['owner_phone'], 'street': tool['owner_street'],
            'city': tool['city'] or '', 'region': tool['region'] or '', 'postal_code': tool['owner_postal'],
            'lat': tool['lat'], 'lng': tool['lng']}


def dropoff_from_form(form, user):
    """Renter's delivery address -> (place, error). Geocoded so the courier gets an exact pin."""
    street = form.get('dropoff_street', '').strip()[:120]
    city = form.get('dropoff_city', '').strip()[:60]
    region = form.get('dropoff_region', '').strip().upper()[:2]
    postal = form.get('dropoff_postal', '').strip()[:5]
    phone = normalize_phone(form.get('renter_phone', '') or user['phone'] or '')
    if not street or not city or len(region) != 2 or not places.ZIP_RE.match(postal):
        return None, 'Enter the full delivery address: street, city, state and ZIP.'
    if not phone:
        return None, 'Enter a mobile number for the courier to text.'
    found = places.lookup_address(street, city, region, postal)
    if found is None and places.configured():
        return None, "We couldn't find that address. Check it and try again."
    return {'name': user['name'], 'phone': phone, 'street': street, 'city': city, 'region': region,
            'postal_code': postal, 'lat': found['lat'] if found else None,
            'lng': found['lng'] if found else None}, None


def courier_round_trip(tool, dropoff):
    """Uber's price for delivery and return, plus our markup, in dollars"""
    pickup = owner_place(tool)
    out = delivery.quote(pickup, dropoff)
    back = delivery.quote(dropoff, pickup)
    return delivery.renter_price_cents(out['fee_cents'] + back['fee_cents']) / 100, out


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def parse_location(form):
    """Browser geolocation fills lat/lng; rounded to ~100 m so exact addresses aren't stored"""
    try:
        lat, lng = float(form.get('lat', '')), float(form.get('lng', ''))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return round(lat, 3), round(lng, 3)
    except ValueError:
        pass
    return None


def selected_platforms(form):
    return ','.join(p for p in form.getlist('battery_platforms') if p in listing_utils.BATTERY_PLATFORMS)


def resolve_home(form, user=None):
    """
    ZIP code -> city/state and a default location (the ZIP's center). Browser location, when the
    person shares it, refines the coordinates. Returns (fields, error).
    """
    postal = form.get('postal_code', '').strip()[:5]
    if not places.ZIP_RE.match(postal):
        return None, 'Enter your 5-digit ZIP code.'
    place = places.lookup_zip(postal)
    if place is None and places.configured():
        return None, "We couldn't find that ZIP code. Check it and try again."
    located = parse_location(form)
    zip_changed = not user or user['postal_code'] != postal
    if located:
        lat, lng = located
    elif place and zip_changed:
        lat, lng = place['lat'], place['lng']
    elif user:
        lat, lng = user['lat'], user['lng']
    else:
        lat, lng = (place['lat'], place['lng']) if place else db_builder.DEMO_CENTER
    city = place['city'] if place else (user['city'] if user and not zip_changed else '')
    region = place['region'] if place else (user['region'] if user and not zip_changed else '')
    return {'postal_code': postal, 'city': city or '', 'region': region or '',
            'lat': round(lat, 3), 'lng': round(lng, 3)}, None


PHONE_DIGITS = re.compile(r'\D')


def normalize_phone(raw):
    """US phone -> E.164 (+15551234567), which couriers need. None when it isn't a US number."""
    digits = PHONE_DIGITS.sub('', raw or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return f'+1{digits}' if len(digits) == 10 and digits[0] not in '01' else None


def courier_details(form):
    """Private details only shared with a courier: street address and phone. Returns (fields, error)."""
    street = form.get('street_address', '').strip()[:120]
    raw_phone = form.get('phone', '').strip()
    phone = normalize_phone(raw_phone) if raw_phone else ''
    if phone is None:
        return None, 'Enter a 10-digit US phone number.'
    return {'street_address': street, 'phone': phone,
            'offers_delivery': 1 if form.get('offers_delivery') else 0}, None


def place_label(person):
    """"Capitol Hill · Seattle, WA" """
    return places.describe(person['neighborhood'], person['city'], person['region'])


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if g.user:
        return redirect(url_for('browse'))
    form = request.form
    if request.method == 'POST':
        name = form.get('name', '').strip()[:60]
        email = form.get('email', '').strip().lower()[:254]
        password = form.get('password', '')
        neighborhood = form.get('neighborhood', '').strip()[:60]
        errors = []
        if not name:
            errors.append('Enter your name.')
        if not EMAIL_RE.match(email):
            errors.append('Enter a valid email address.')
        if len(password) < MIN_PASSWORD:
            errors.append(f'Use a password of at least {MIN_PASSWORD} characters.')
        if not neighborhood:
            errors.append('Enter your neighborhood so renters know roughly where tools are.')
        home, home_error = resolve_home(form)
        if home_error:
            errors.append(home_error)
        db = get_db()
        if not errors and db.execute('SELECT 1 FROM users WHERE email = ? COLLATE NOCASE', (email,)).fetchone():
            errors.append('An account with that email already exists. Try logging in.')
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('signup.html', form=form, platforms=listing_utils.BATTERY_PLATFORMS), 400

        now = datetime.now().isoformat(timespec='seconds')
        try:
            cur = db.execute(
                '''INSERT INTO users (name, email, password_hash, neighborhood, lat, lng, postal_code, city, region,
                                      battery_platforms, is_demo, created_at, last_login_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)''',
                (name, email, generate_password_hash(password), neighborhood, home['lat'], home['lng'],
                 home['postal_code'], home['city'], home['region'], selected_platforms(form), now, now))
        except sqlite3.IntegrityError:
            flash('An account with that email already exists. Try logging in.', 'error')
            return render_template('signup.html', form=form, platforms=listing_utils.BATTERY_PLATFORMS), 400
        user = db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()
        track(db, 'signup', user_id=user['id'], visitor=visitor_id(), located=parse_location(form) is not None)
        db.commit()
        log_in(user)
        flash(f'Welcome, {name}! Post your tools or browse what neighbors have.', 'success')
        return redirect(safe_next(request.args.get('next')))
    return render_template('signup.html', form=form, platforms=listing_utils.BATTERY_PLATFORMS)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('browse'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        ip_key, email_key = f'login-ip:{client_ip()}', f'login-email:{email}'
        if too_many_failures(ip_key, 20) or too_many_failures(email_key, 8):
            flash('Too many attempts. Wait 15 minutes and try again.', 'error')
            return render_template('login.html', email=email), 429
        db = get_db()
        user = db.execute(
            'SELECT * FROM users WHERE email = ? COLLATE NOCASE AND is_demo = 0', (email,)
        ).fetchone()
        if user is None or not user['password_hash'] or not check_password_hash(user['password_hash'],
                                                                                request.form.get('password', '')):
            record_failure(ip_key)
            record_failure(email_key)
            track(db, 'login_failed', visitor=visitor_id())
            db.commit()
            flash('That email and password don\'t match.', 'error')
            return render_template('login.html', email=email), 401
        db.execute('UPDATE users SET last_login_at = ? WHERE id = ?',
                   (datetime.now().isoformat(timespec='seconds'), user['id']))
        track(db, 'login', user_id=user['id'], visitor=visitor_id())
        db.commit()
        log_in(user)
        return redirect(safe_next(request.args.get('next')))
    return render_template('login.html', email='')


@app.route('/logout', methods=['POST'])
def logout():
    if g.user:
        track(get_db(), 'logout', user_id=g.user['id'], visitor=visitor_id())
        get_db().commit()
    session.clear()
    flash('You\'re logged out.', 'success')
    return redirect(url_for('browse'))


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    db = get_db()
    if request.method == 'POST':
        form = request.form
        name = form.get('name', '').strip()[:60]
        email = form.get('email', '').strip().lower()[:254]
        neighborhood = form.get('neighborhood', '').strip()[:60]
        bio = form.get('bio', '').strip()[:500]
        errors = []
        if not name:
            errors.append('Enter your name.')
        if not neighborhood:
            errors.append('Enter your neighborhood.')
        if not EMAIL_RE.match(email):
            errors.append('Enter a valid email address.')
        elif db.execute('SELECT 1 FROM users WHERE email = ? COLLATE NOCASE AND id != ?', (email, g.user['id'])).fetchone():
            errors.append('Another account already uses that email.')
        if errors:
            for e in errors:
                flash(e, 'error')
            return redirect(url_for('account'))
        home, home_error = resolve_home(form, g.user)
        courier, courier_error = courier_details(form)
        for e in (home_error, courier_error):
            if e:
                flash(e, 'error')
        if home_error or courier_error:
            return redirect(url_for('account'))
        db.execute('''UPDATE users SET name = ?, email = ?, neighborhood = ?, bio = ?, battery_platforms = ?,
                                       lat = ?, lng = ?, postal_code = ?, city = ?, region = ?,
                                       street_address = ?, phone = ?, offers_delivery = ? WHERE id = ?''',
                   (name, email, neighborhood, bio, selected_platforms(form), home['lat'], home['lng'],
                    home['postal_code'], home['city'], home['region'], courier['street_address'], courier['phone'],
                    courier['offers_delivery'], g.user['id']))
        track(db, 'profile_updated', user_id=g.user['id'], located=parse_location(form) is not None)
        db.commit()
        flash('Profile saved.', 'success')
        return redirect(url_for('account'))
    listings = db.execute("SELECT COUNT(*) FROM tools WHERE owner_id = ? AND status = 'listed'", (g.user['id'],)).fetchone()[0]
    linked = {r['provider']: r for r in db.execute('SELECT * FROM identities WHERE user_id = ?', (g.user['id'],))}
    return render_template('account.html', platforms=listing_utils.BATTERY_PLATFORMS,
                           my_platforms=user_platforms(g.user), listings=listings, min_password=MIN_PASSWORD,
                           linked=linked, has_password=bool(g.user['password_hash']))


@app.route('/account/avatar', methods=['POST'])
@login_required
def account_avatar():
    photo = request.files.get('avatar')
    ext = (photo.filename.rsplit('.', 1)[-1].lower() if photo and '.' in photo.filename else '')
    if ext not in ALLOWED_IMAGE_TYPES:
        flash('Choose a JPG, PNG, WEBP or GIF image.', 'error')
        return redirect(url_for('account'))
    data = photo.read()
    if not data or len(data) > AVATAR_MAX_BYTES:
        flash('Profile photos must be under 5 MB.', 'error')
        return redirect(url_for('account'))
    db = get_db()
    old = g.user['avatar_path']
    try:
        key = media_store().put(storage.new_key('avatars', ext), data, ALLOWED_IMAGE_TYPES[ext])
    except Exception as e:
        print(f'Avatar upload failed: {e}')
        flash('Couldn\'t save that photo. Please try again.', 'error')
        return redirect(url_for('account'))
    db.execute('UPDATE users SET avatar_path = ? WHERE id = ?', (key, g.user['id']))
    track(db, 'avatar_uploaded', user_id=g.user['id'], value=len(data))
    db.commit()
    if old and '/' in old:
        media_store().delete(old)
    flash('Profile photo updated.', 'success')
    return redirect(url_for('account'))


@app.route('/account/password', methods=['POST'])
@login_required
def account_password():
    current, new = request.form.get('current_password', ''), request.form.get('new_password', '')
    if g.user['password_hash'] and not check_password_hash(g.user['password_hash'], current):
        flash('Your current password isn\'t right.', 'error')
    elif len(new) < MIN_PASSWORD:
        flash(f'Use a new password of at least {MIN_PASSWORD} characters.', 'error')
    else:
        db = get_db()
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(new), g.user['id']))
        track(db, 'password_changed', user_id=g.user['id'])
        db.commit()
        flash('Password changed.', 'success')
    return redirect(url_for('account'))


@app.route('/u/<int:user_id>')
def profile(user_id):
    db = get_db()
    person = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if person is None or (person['is_demo'] and not app.config['SHOW_DEMO_LISTINGS']):
        abort(404)
    tools = db.execute(
        "SELECT * FROM tools WHERE owner_id = ? AND status = 'listed' ORDER BY published_at DESC, id DESC", (user_id,)
    ).fetchall()
    lat, lng = viewer_location()
    miles = round(listing_utils.distance_miles(lat, lng, person['lat'], person['lng']), 1)
    completed = db.execute(
        """SELECT COUNT(*) FROM bookings b JOIN tools t ON b.tool_id = t.id
           WHERE t.owner_id = ? AND b.status = 'returned'""", (user_id,)
    ).fetchone()[0]
    return render_template('profile.html', person=person, tools=tools, miles=miles, completed=completed,
                           platforms=user_platforms(person))


@app.route('/welcome', methods=['GET', 'POST'])
@login_required
def welcome():
    """Finish a profile that was started with Google or Microsoft"""
    if request.method == 'POST':
        neighborhood = request.form.get('neighborhood', '').strip()[:60]
        if not neighborhood:
            flash('Enter your neighborhood so renters know roughly where tools are.', 'error')
            return redirect(url_for('welcome', next=request.args.get('next')))
        home, home_error = resolve_home(request.form, g.user)
        if home_error:
            flash(home_error, 'error')
            return redirect(url_for('welcome', next=request.args.get('next')))
        db = get_db()
        db.execute('''UPDATE users SET neighborhood = ?, lat = ?, lng = ?, postal_code = ?, city = ?, region = ?,
                                       battery_platforms = ? WHERE id = ?''',
                   (neighborhood, home['lat'], home['lng'], home['postal_code'], home['city'], home['region'],
                    selected_platforms(request.form), g.user['id']))
        track(db, 'profile_completed', user_id=g.user['id'], located=parse_location(request.form) is not None)
        db.commit()
        flash(f"You're all set, {g.user['name']}.", 'success')
        return redirect(safe_next(request.args.get('next')))
    return render_template('welcome.html', platforms=listing_utils.BATTERY_PLATFORMS)


# ---------------------------------------------------------------------------
# Google / Microsoft sign-in (OpenID Connect)
# ---------------------------------------------------------------------------

@app.route('/auth/<provider>')
def oauth_start(provider):
    if provider not in enabled_providers():
        abort(404)
    session['oauth_next'] = safe_next(request.args.get('next'))
    session['oauth_link'] = bool(g.user) and request.args.get('link') == '1'
    redirect_uri = url_for('oauth_callback', provider=provider, _external=True)
    return oauth_client(provider).authorize_redirect(redirect_uri, prompt='select_account')


def microsoft_issuer_ok(claims):
    """The "common" endpoint serves every tenant, so the issuer must match the token's own tenant id"""
    tid = claims.get('tid', '')
    return bool(tid) and claims.get('iss') == f'https://login.microsoftonline.com/{tid}/v2.0'


@app.route('/auth/<provider>/callback')
def oauth_callback(provider):
    if provider not in enabled_providers():
        abort(404)
    db = get_db()
    next_url = session.pop('oauth_next', url_for('browse'))
    linking = session.pop('oauth_link', False)
    label = OAUTH_PROVIDERS[provider]['label']
    try:
        options = {'iss': {'essential': True}} if provider == 'microsoft' else None
        token = oauth_client(provider).authorize_access_token(claims_options=options)
        info = token.get('userinfo') or {}
        if provider == 'microsoft' and not microsoft_issuer_ok(info):
            raise ValueError('unexpected token issuer')
        subject = info['sub']
    except Exception as e:
        print(f'{provider} sign-in failed: {e}')
        track(db, 'oauth_failed', visitor=visitor_id(), provider=provider)
        db.commit()
        flash(f"{label} sign-in didn't complete. Please try again.", 'error')
        return redirect(url_for('login'))

    email = (info.get('email') or (info.get('preferred_username') if provider == 'microsoft' else '') or '').strip().lower()
    if not EMAIL_RE.match(email):
        email = ''
    # Only Google asserts it verified the address. Microsoft tenants can set arbitrary emails,
    # so a Microsoft sign-in never takes over an existing account by email alone.
    email_verified = provider == 'google' and info.get('email_verified') is True
    now = datetime.now().isoformat(timespec='seconds')

    def link(user_id):
        db.execute('INSERT OR IGNORE INTO identities (user_id, provider, subject, email, created_at) VALUES (?, ?, ?, ?, ?)',
                   (user_id, provider, subject, email, now))

    known = db.execute('SELECT user_id FROM identities WHERE provider = ? AND subject = ?', (provider, subject)).fetchone()
    if linking and g.user:
        if known and known['user_id'] != g.user['id']:
            flash(f'That {label} account is already connected to a different toolshare account.', 'error')
        else:
            link(g.user['id'])
            track(db, 'oauth_linked', user_id=g.user['id'], provider=provider)
            db.commit()
            flash(f'{label} sign-in connected.', 'success')
        return redirect(url_for('account'))

    if known:
        user = db.execute('SELECT * FROM users WHERE id = ? AND is_demo = 0', (known['user_id'],)).fetchone()
    else:
        user = None
        if email:
            user = db.execute('SELECT * FROM users WHERE email = ? COLLATE NOCASE AND is_demo = 0', (email,)).fetchone()
        if user and not email_verified:
            flash(f'An account with {email} already exists. Log in with your password, then connect {label} '
                  'from account settings.', 'error')
            return redirect(url_for('login'))
        if user:
            link(user['id'])
        else:
            if not email:
                flash(f"Your {label} account didn't share an email address, so we can't create an account with it.", 'error')
                return redirect(url_for('signup'))
            name = (info.get('name') or email.split('@')[0]).strip()[:60]
            lat, lng = db_builder.DEMO_CENTER
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash, neighborhood, lat, lng, battery_platforms, "
                "is_demo, created_at, last_login_at) VALUES (?, ?, NULL, '', ?, ?, '', 0, ?, ?)",
                (name, email, lat, lng, now, now))
            user = db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()
            link(user['id'])
            track(db, 'signup', user_id=user['id'], visitor=visitor_id(), method=provider)
    if user is None:
        abort(404)
    db.execute('UPDATE users SET last_login_at = ? WHERE id = ?', (now, user['id']))
    track(db, 'login', user_id=user['id'], visitor=visitor_id(), method=provider)
    db.commit()
    log_in(user)
    return redirect(next_url)


@app.route('/auth/<provider>/disconnect', methods=['POST'])
@login_required
def oauth_disconnect(provider):
    db = get_db()
    others = db.execute('SELECT COUNT(*) FROM identities WHERE user_id = ? AND provider != ?',
                        (g.user['id'], provider)).fetchone()[0]
    if not g.user['password_hash'] and not others:
        flash('Set a password first, so you can still log in after disconnecting.', 'error')
        return redirect(url_for('account'))
    db.execute('DELETE FROM identities WHERE user_id = ? AND provider = ?', (g.user['id'], provider))
    track(db, 'oauth_unlinked', user_id=g.user['id'], provider=provider)
    db.commit()
    flash(f"{OAUTH_PROVIDERS.get(provider, {}).get('label', provider)} sign-in disconnected.", 'success')
    return redirect(url_for('account'))


# ---------------------------------------------------------------------------
# Browse and rent
# ---------------------------------------------------------------------------

@app.route('/')
def browse():
    """Browse listed tools near the viewer"""
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '')
    platform = request.args.get('platform', '')
    view = 'gallery' if request.args.get('view') == 'gallery' else 'list'
    try:
        max_miles = float(request.args.get('max_miles', 10))
    except ValueError:
        max_miles = 10.0
    max_miles = min(50.0, max(1.0, max_miles)) if max_miles == max_miles else 10.0    # also rejects NaN

    sort = 'best' if q and request.args.get('sort') != 'closest' else 'closest'
    page = max(1, request.args.get('page', 1, type=int))

    my_platforms = user_platforms(g.user)
    platforms = None
    if platform == 'mine':
        platforms = my_platforms
    elif platform in listing_utils.BATTERY_PLATFORMS:
        platforms = [platform]

    lat, lng = viewer_location()
    result = search.search_tools(get_db(), float(lat), float(lng), miles=max_miles, q=q,
                                 category=category if category in listing_utils.CATEGORIES else None,
                                 platforms=platforms, include_demo=app.config['SHOW_DEMO_LISTINGS'],
                                 sort=sort, page=page)
    tools = result['tools']
    if q and page == 1:
        track(get_db(), 'search', user_id=g.user['id'] if g.user else None, visitor=visitor_id(),
              value=result['total'], q=q[:60], any_word=result['matched_any'])
        get_db().commit()

    return render_template('browse.html', tools=tools, q=q, category=category, platform=platform,
                           max_miles=max_miles, view=view, sort=sort, result=result, categories=listing_utils.CATEGORIES,
                           platforms=listing_utils.BATTERY_PLATFORMS, my_platforms=my_platforms)


@app.route('/area', methods=['POST'])
def set_area():
    """Visitors without an account pick where to search, by ZIP or by sharing their location"""
    located = parse_location(request.form)
    postal = request.form.get('postal_code', '').strip()[:5]
    if located:
        session['area'] = {'postal_code': '', 'city': 'near you', 'region': '', 'lat': located[0], 'lng': located[1]}
    elif places.ZIP_RE.match(postal):
        place = places.lookup_zip(postal)
        if place is None and places.configured():
            flash("We couldn't find that ZIP code.", 'error')
            return redirect(url_for('browse'))
        lat, lng = (place['lat'], place['lng']) if place else db_builder.DEMO_CENTER
        session['area'] = {'postal_code': postal, 'city': place['city'] if place else '',
                           'region': place['region'] if place else '', 'lat': lat, 'lng': lng}
    else:
        flash('Enter a 5-digit ZIP code.', 'error')
    return redirect(url_for('browse'))


@app.route('/tool/<int:tool_id>')
def tool_detail(tool_id):
    tool = get_listed_tool(tool_id)
    lat, lng = viewer_location()
    miles = listing_utils.distance_miles(lat, lng, tool['lat'], tool['lng'])
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
                           fits_my_batteries=tool['battery_platform'] in user_platforms(g.user),
                           delivery_max=listing_utils.DELIVERY_MAX_MILES,
                           owner_delivers=owner_delivers(tool, miles), courier_ok=courier_available(tool),
                           courier_test_mode=not delivery.live_enabled())


@app.route('/api/quote')
def api_quote():
    """Live price quote for the booking form"""
    tool = get_listed_tool(request.args.get('tool_id', type=int))
    start = parse_date(request.args.get('start'))
    end = parse_date(request.args.get('end'))
    if not start or not end or end < start:
        return jsonify({'success': False, 'error': 'Pick a valid start and return date'}), 400
    method = request.args.get('method') or ('owner' if request.args.get('delivery') == '1' else 'pickup')
    if method not in ('pickup', 'owner'):
        method = 'pickup'     # courier prices need an address: /api/courier-quote
    hour = request.args.get('hour', 10, type=int) % 24
    return jsonify({'success': True, **build_quote(tool, start, end, method, hour)})


@app.route('/api/courier-quote', methods=['POST'])
@login_required
def api_courier_quote():
    """Price a round-trip Uber Direct courier to the renter's address"""
    tool = get_listed_tool(request.form.get('tool_id', type=int))
    start, end = parse_date(request.form.get('start')), parse_date(request.form.get('end'))
    if not start or not end or end < start:
        return jsonify({'success': False, 'error': 'Pick a valid start and return date'}), 400
    if not courier_available(tool):
        return jsonify({'success': False, 'error': 'Courier delivery isn\'t available for this tool.'}), 400
    key = f'courier-quote:{g.user["id"]}'
    if too_many_failures(key, 30):
        return jsonify({'success': False, 'error': 'Too many quotes. Try again in a few minutes.'}), 429
    record_failure(key)       # counts every quote, not just failures
    dropoff, error = dropoff_from_form(request.form, g.user)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    try:
        fee, out = courier_round_trip(tool, dropoff)
    except delivery.DeliveryError as e:
        return jsonify({'success': False, 'error': f'Uber couldn\'t quote this trip: {e}'}), 400
    except Exception as e:
        print(f'Courier quote failed: {e}')
        return jsonify({'success': False, 'error': 'Couldn\'t reach the courier service. Try again.'}), 502
    hour = request.form.get('hour', 10, type=int) % 24
    q = build_quote(tool, start, end, 'courier', hour, courier_fee=fee)
    return jsonify({'success': True, **q, 'courier_minutes': out['duration_min']})


@app.route('/tool/<int:tool_id>/book', methods=['POST'])
@login_required
def book_tool(tool_id):
    user = g.user
    tool = get_listed_tool(tool_id)

    if tool['owner_id'] == user['id']:
        flash("That's your own tool.", 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))
    if tool['owner_is_demo']:
        flash('This is a sample listing, so it can\'t be rented.', 'error')
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

    method = request.form.get('delivery_method') or ('owner' if request.form.get('delivery') == '1' else 'pickup')
    if method not in ('pickup', 'owner', 'courier'):
        method = 'pickup'
    hour = request.form.get('hour', 10, type=int) % 24
    dropoff, courier_fee = None, None
    if method == 'courier':
        if not courier_available(tool):
            flash('Courier delivery isn\'t available for this tool.', 'error')
            return redirect(url_for('tool_detail', tool_id=tool_id))
        dropoff, error = dropoff_from_form(request.form, user)
        if error:
            flash(error, 'error')
            return redirect(url_for('tool_detail', tool_id=tool_id))
        try:
            courier_fee, _ = courier_round_trip(tool, dropoff)
        except Exception as e:
            print(f'Courier quote at booking failed: {e}')
            flash('Couldn\'t get a courier price for that address. Try again, or choose pickup.', 'error')
            return redirect(url_for('tool_detail', tool_id=tool_id))
    q = build_quote(tool, start, end, method, hour, courier_fee=courier_fee)
    if method == 'owner' and not q['delivery_available']:
        flash(f"The owner only delivers within {listing_utils.DELIVERY_MAX_MILES} miles.", 'error')
        return redirect(url_for('tool_detail', tool_id=tool_id))

    db = get_db()
    d = dropoff or {}
    db.execute(
        '''INSERT INTO bookings (tool_id, renter_id, start_date, end_date, days, rental_total, service_fee,
                                 delivery, delivery_fee, deposit, total_charge, owner_payout, message,
                                 status, created_at, delivery_method, dropoff_street, dropoff_city,
                                 dropoff_region, dropoff_postal, dropoff_lat, dropoff_lng, renter_phone)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (tool_id, user['id'], start.isoformat(), end.isoformat(), q['days'], q['rental_total'],
         q['service_fee'], int(method != 'pickup'), q['delivery_fee'], q['deposit_hold'], q['total_charge'],
         q['owner_payout'], request.form.get('message', '').strip()[:500],
         datetime.now().isoformat(timespec='seconds'), method, d.get('street'), d.get('city'), d.get('region'),
         d.get('postal_code'), d.get('lat'), d.get('lng'), d.get('phone'))
    )
    track(db, 'booking_requested', user_id=user['id'], value=q['total_charge'], tool_id=tool_id,
          days=q['days'], delivery=method != 'pickup', method=method)
    db.commit()
    flash(f"Request sent to {tool['owner_name']}. You'll see it under My Garage.", 'success')
    return redirect(url_for('garage'))


# ---------------------------------------------------------------------------
# Snap-to-list
# ---------------------------------------------------------------------------

@app.route('/list')
@login_required
def list_tools():
    """Upload garage photos to get AI-drafted listings"""
    located = (g.user['lat'], g.user['lng']) != tuple(db_builder.DEMO_CENTER)
    return render_template('list.html', profile_located=located)


def uploads_today(user_id):
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec='seconds')
    return get_db().execute(
        "SELECT COUNT(*) FROM events WHERE event = 'photo_analyzed' AND user_id = ? AND ts >= ?", (user_id, since)
    ).fetchone()[0]


@app.route('/api/analyze-photo', methods=['POST'])
@login_required
def analyze_photo():
    """Analyze one photo and save a draft listing for each tool found"""
    user = g.user
    db = get_db()
    if uploads_today(user['id']) >= app.config['UPLOAD_DAILY_LIMIT']:
        track(db, 'upload_limited', user_id=user['id'])
        db.commit()
        return jsonify({'success': False, 'error': 'You\'ve hit today\'s photo limit. Try again tomorrow.'}), 429

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

    source = 'video' if request.form.get('source') == 'video' else 'photo'
    started = time.monotonic()
    try:
        drafts, cost, demo_mode, model_used = listing_utils.analyze_photo(
            image_bytes, ALLOWED_IMAGE_TYPES[ext], source=source)
    except Exception as e:
        print(f"Error analyzing photo: {e}")
        return jsonify({'success': False, 'error': f'Could not analyze photo: {e}'}), 502
    seconds = round(time.monotonic() - started, 2)

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
        if key not in seen:
            seen.add(key)
            created.append(draft)

    # Keep the photo only if it produced a listing (it becomes the listing photo)
    photo_key = None
    if created:
        try:
            photo_key = media_store().put(storage.new_key('listings', ext), image_bytes, ALLOWED_IMAGE_TYPES[ext])
        except Exception as e:
            print(f'Photo storage failed: {e}')
            return jsonify({'success': False, 'error': 'Couldn\'t save the photo. Please try again.'}), 502
    # Pickup spot chosen on the post page (current location, with permission); blank = profile location
    pickup = parse_location({'lat': request.form.get('pickup_lat', ''), 'lng': request.form.get('pickup_lng', '')})
    for draft in created:
        db_builder.insert_tool(db, owner_id=user['id'], status='draft', photo_path=photo_key, batch_id=batch_id,
                               pickup_lat=pickup[0] if pickup else None, pickup_lng=pickup[1] if pickup else None,
                               **draft)
    track(db, 'photo_analyzed', user_id=user['id'], value=round(cost, 5), duration=seconds,
          source=source, model=model_used, drafts=len(created), duplicates=len(drafts) - len(created), demo=demo_mode,
          pickup='current' if pickup else 'profile')
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
@login_required
def review_drafts():
    """Bulk approve, edit or skip AI drafts"""
    user = g.user
    db = get_db()
    drafts = db.execute(
        "SELECT * FROM tools WHERE owner_id = ? AND status = 'draft' ORDER BY id", (user['id'],)
    ).fetchall()

    if request.method == 'POST':
        listed = skipped = blocked = 0
        now = datetime.now().isoformat(timespec='seconds')
        for draft in drafts:
            action = request.form.get(f'action_{draft["id"]}', 'keep')
            if action == 'skip':
                db.execute("UPDATE tools SET status = 'skipped' WHERE id = ?", (draft['id'],))
                skipped += 1
            elif action == 'approve':
                fields = {k: request.form.get(f'{k}_{draft["id"]}', draft[k]) for k in (
                    'name', 'brand', 'model', 'category', 'power_source', 'battery_platform',
                    'description', 'included_items', 'daily_price', 'deposit', 'replacement_value',
                    'risk_tier', 'safety_notes', 'courier_size', 'weight_lbs', 'search_keywords')}
                fields['confidence'] = draft['ai_confidence']
                clean = listing_utils.normalize_draft(fields, owner_priced=True)
                status = 'listed'
                if clean['risk_tier'] == 'excluded':
                    status = 'skipped'
                    blocked += 1
                else:
                    listed += 1
                sets = ', '.join(f'{k} = ?' for k in clean)
                db.execute(f'UPDATE tools SET {sets}, status = ?, published_at = ? WHERE id = ?',
                           list(clean.values()) + [status, now if status == 'listed' else None, draft['id']])
        track(db, 'drafts_reviewed', user_id=user['id'], listed=listed, skipped=skipped, blocked=blocked)
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
    """Serve listing and profile photos from storage. Keys are unique, so browsers can cache them for good."""
    if '..' in filename:
        abort(404)
    found = media_store().get(filename)
    if found is None:
        # Photos uploaded before Blob Storage was set up still live on local disk
        legacy = os.path.abspath(app.config['UPLOAD_FOLDER'])
        if os.path.isfile(os.path.join(legacy, filename)):
            return send_from_directory(legacy, filename, max_age=31536000)
        abort(404)
    data, content_type = found
    content_type = content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    return Response(data, mimetype=content_type, headers={'Cache-Control': 'public, max-age=31536000, immutable'})


# ---------------------------------------------------------------------------
# My garage: listings, incoming requests, my rentals
# ---------------------------------------------------------------------------

@app.route('/garage')
@login_required
def garage():
    user = g.user
    db = get_db()
    listings = db.execute(
        "SELECT * FROM tools WHERE owner_id = ? AND status = 'listed' ORDER BY category, name", (user['id'],)
    ).fetchall()
    incoming = db.execute(
        '''SELECT b.*, t.name AS tool_name, u.name AS renter_name, u.id AS renter_user_id
           FROM bookings b JOIN tools t ON b.tool_id = t.id JOIN users u ON b.renter_id = u.id
           WHERE t.owner_id = ? ORDER BY b.created_at DESC''', (user['id'],)
    ).fetchall()
    rentals = db.execute(
        '''SELECT b.*, t.name AS tool_name, u.name AS owner_name, u.id AS owner_user_id
           FROM bookings b JOIN tools t ON b.tool_id = t.id JOIN users u ON t.owner_id = u.id
           WHERE b.renter_id = ? ORDER BY b.created_at DESC''', (user['id'],)
    ).fetchall()
    earnings = sum(b['owner_payout'] for b in incoming if b['status'] in ('accepted', 'returned'))
    ids = [b['id'] for b in list(incoming) + list(rentals) if b['delivery_method'] == 'courier']
    trips = {}
    if ids:
        for d in db.execute(f"SELECT * FROM deliveries WHERE booking_id IN ({','.join('?' * len(ids))})", ids):
            trips.setdefault(d['booking_id'], {})[d['leg']] = d
    return render_template('garage.html', listings=listings, incoming=incoming, rentals=rentals,
                           earnings=earnings, trips=trips, done_statuses=delivery.DONE_STATUSES,
                           courier_test_mode=not delivery.live_enabled())


# action -> (who may do it, statuses it applies to, new status)
BOOKING_ACTIONS = {
    'accept': ('owner', ('requested',), 'accepted'),
    'decline': ('owner', ('requested',), 'declined'),
    'returned': ('owner', ('accepted',), 'returned'),
    'cancel': ('renter', ('requested', 'accepted'), 'cancelled'),
}


@app.route('/booking/<int:booking_id>/<action>', methods=['POST'])
@login_required
def update_booking(booking_id, action):
    if action not in BOOKING_ACTIONS:
        abort(404)
    user = g.user
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

    if new_status in ('declined', 'cancelled'):
        cancel_courier_trips(db, booking_id)
    db.execute('UPDATE bookings SET status = ? WHERE id = ?', (new_status, booking_id))
    track(db, f'booking_{new_status}', user_id=user['id'], value=booking['total_charge'], booking_id=booking_id)
    db.commit()
    flash(f'Booking {new_status}.', 'success')
    return redirect(url_for('garage'))



# ---------------------------------------------------------------------------
# Courier delivery (Uber Direct)
# ---------------------------------------------------------------------------

def cancel_courier_trips(db, booking_id):
    """Best effort: call off couriers that haven't finished when a booking is called off"""
    for trip in db.execute('SELECT * FROM deliveries WHERE booking_id = ?', (booking_id,)).fetchall():
        if trip['status'] in delivery.DONE_STATUSES:
            continue
        try:
            delivery.cancel(trip['external_id'])
            db.execute("UPDATE deliveries SET status = 'canceled', updated_at = ? WHERE id = ?",
                       (datetime.now().isoformat(timespec='seconds'), trip['id']))
        except Exception as e:
            print(f"Couldn't cancel courier {trip['external_id']}: {e}")


@app.route('/booking/<int:booking_id>/courier/<leg>', methods=['POST'])
@login_required
def dispatch_courier(booking_id, leg):
    """The owner sends the tool out; the renter sends it back. Each is one Uber Direct trip."""
    if leg not in ('out', 'return'):
        abort(404)
    db = get_db()
    b = db.execute(
        '''SELECT b.*, t.name AS tool_name, t.owner_id, t.courier_size, t.weight_lbs,
                  COALESCE(t.pickup_lat, o.lat) AS owner_lat, COALESCE(t.pickup_lng, o.lng) AS owner_lng,
                  o.name AS owner_name, o.phone AS owner_phone, o.street_address AS owner_street,
                  o.city AS owner_city, o.region AS owner_region, o.postal_code AS owner_postal,
                  r.name AS renter_name
           FROM bookings b JOIN tools t ON b.tool_id = t.id JOIN users o ON t.owner_id = o.id
                JOIN users r ON b.renter_id = r.id
           WHERE b.id = ?''', (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if g.user['id'] != (b['owner_id'] if leg == 'out' else b['renter_id']):
        abort(403)
    if b['delivery_method'] != 'courier' or b['status'] != 'accepted':
        flash('Couriers can only be sent for accepted courier bookings.', 'error')
        return redirect(url_for('garage'))
    existing = db.execute('SELECT * FROM deliveries WHERE booking_id = ? AND leg = ?', (booking_id, leg)).fetchone()
    if existing and existing['status'] not in ('canceled', 'returned'):
        flash('A courier is already booked for that trip.', 'error')
        return redirect(url_for('garage'))
    if not (b['owner_street'] and b['owner_phone'] and b['dropoff_street'] and b['renter_phone']):
        flash('Both addresses and phone numbers are needed for a courier. Check account settings.', 'error')
        return redirect(url_for('garage'))

    owner = {'name': b['owner_name'], 'phone': b['owner_phone'], 'street': b['owner_street'],
             'city': b['owner_city'] or '', 'region': b['owner_region'] or '', 'postal_code': b['owner_postal'],
             'lat': b['owner_lat'], 'lng': b['owner_lng']}
    renter = {'name': b['renter_name'], 'phone': b['renter_phone'], 'street': b['dropoff_street'],
              'city': b['dropoff_city'], 'region': b['dropoff_region'], 'postal_code': b['dropoff_postal'],
              'lat': b['dropoff_lat'], 'lng': b['dropoff_lng']}
    pickup, dropoff = (owner, renter) if leg == 'out' else (renter, owner)
    item = {'name': b['tool_name'], 'courier_size': b['courier_size'] or 'medium', 'weight_lbs': b['weight_lbs']}
    try:
        trip = delivery.create(pickup, dropoff, item, external_id=f'toolshare-{booking_id}-{leg}-{int(time.time())}',
                               notes=f'toolshare rental #{booking_id}: {b["tool_name"]}')
    except delivery.DeliveryError as e:
        flash(f'Uber couldn\'t book a courier: {e}', 'error')
        return redirect(url_for('garage'))
    except Exception as e:
        print(f'Courier dispatch failed: {e}')
        flash('Couldn\'t reach the courier service. Try again in a minute.', 'error')
        return redirect(url_for('garage'))
    now = datetime.now().isoformat(timespec='seconds')
    db.execute('''INSERT OR REPLACE INTO deliveries (booking_id, leg, provider, external_id, fee_cents, status,
                                                      tracking_url, live, created_at, updated_at)
                  VALUES (?, ?, 'uber_direct', ?, ?, ?, ?, ?, ?, ?)''',
               (booking_id, leg, trip['id'], trip['fee_cents'], trip['status'], trip['tracking_url'],
                int(trip['live']), now, now))
    track(db, 'courier_dispatched', user_id=g.user['id'], value=trip['fee_cents'] / 100, booking_id=booking_id,
          leg=leg, live=trip['live'])
    db.commit()
    flash('Courier requested. Follow it with the tracking link.' if trip['live'] else
          'Test courier requested (Uber\'s Robocourier, no real driver).', 'success')
    return redirect(url_for('garage'))


@app.route('/webhooks/uber-direct', methods=['POST'])
def uber_direct_webhook():
    """Delivery status updates from Uber, signed with the webhook's signing key"""
    raw = request.get_data()
    if not delivery.verify_webhook(raw, request.headers.get('X-Uber-Signature', '')):
        abort(401)
    payload = request.get_json(silent=True) or {}
    data = payload.get('data') or {}
    delivery_id = payload.get('delivery_id') or data.get('id')
    status = payload.get('status') or data.get('status')
    if not delivery_id or not status:
        return jsonify({'ok': True})
    db = get_db()
    cur = db.execute('''UPDATE deliveries SET status = ?, tracking_url = COALESCE(?, tracking_url), updated_at = ?
                         WHERE external_id = ?''',
                     (str(status)[:40], data.get('tracking_url'), datetime.now().isoformat(timespec='seconds'),
                      delivery_id))
    if cur.rowcount:
        track(db, 'courier_status', status=str(status)[:40])
    db.commit()
    return jsonify({'ok': True})


@app.route('/tool/<int:tool_id>/unlist', methods=['POST'])
@login_required
def unlist_tool(tool_id):
    db = get_db()
    cur = db.execute("UPDATE tools SET status = 'skipped' WHERE id = ? AND owner_id = ?", (tool_id, g.user['id']))
    if cur.rowcount == 0:
        abort(404)
    track(db, 'listing_removed', user_id=g.user['id'], tool_id=tool_id)
    db.commit()
    flash('Listing removed.', 'success')
    return redirect(url_for('garage'))


# ---------------------------------------------------------------------------
# Admin: password-protected metrics, backups and account help
# ---------------------------------------------------------------------------

def admin_password_ok(candidate):
    expected = os.getenv('ADMIN_PASSWORD', '')
    if not expected:
        return False
    # Compare fixed-length digests so timing doesn't reveal the password length
    return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(expected.encode()).digest())


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    enabled = bool(os.getenv('ADMIN_PASSWORD'))
    if request.method == 'POST' and enabled:
        key = f'admin-ip:{client_ip()}'
        db = get_db()
        if too_many_failures(key, 5):
            flash('Too many attempts. Wait 15 minutes and try again.', 'error')
            return render_template('admin_login.html', enabled=enabled), 429
        if admin_password_ok(request.form.get('password', '')):
            session['admin_until'] = time.time() + ADMIN_SESSION_SECONDS
            track(db, 'admin_login', user_id=g.user['id'] if g.user else None)
            db.commit()
            return redirect(safe_next(request.args.get('next')) if request.args.get('next') else url_for('admin'))
        record_failure(key)
        track(db, 'admin_login_failed')
        db.commit()
        flash('Wrong admin password.', 'error')
        return render_template('admin_login.html', enabled=enabled), 401
    return render_template('admin_login.html', enabled=enabled)


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_until', None)
    flash('Signed out of admin.', 'success')
    return redirect(url_for('browse'))


@app.route('/admin')
@admin_required
def admin():
    db = get_db()
    data = metrics.collect(db)
    try:
        backups = backup_store().list('db/')[:10]
        backup_error = None
    except Exception as e:
        backups, backup_error = [], str(e)[:200]
    legacy_photos = db.execute(
        """SELECT COUNT(*) FROM (SELECT photo_path AS p FROM tools WHERE photo_path IS NOT NULL AND photo_path NOT LIKE '%/%'
                                 UNION SELECT avatar_path FROM users WHERE avatar_path IS NOT NULL AND avatar_path NOT LIKE '%/%')"""
    ).fetchone()[0]
    charts = [(title, metrics.column_chart(data['daily'][key], money=money), note) for title, key, money, note in (
        ('posts', 'posts', False, 'listings published per day'),
        ('signups', 'signups', False, 'new accounts per day'),
        ('rental requests', 'bookings', False, 'booking requests per day'),
        ('page views', 'page_views', False, 'pages viewed per day'),
        ('AI spend', 'ai_cost', True, 'estimated model cost per day (uploads + test bench)'),
    )]
    return render_template('admin.html', m=data, charts=charts, backups=backups, backup_error=backup_error,
                           search_index=search.describe(get_db()),
                           media=media_store().describe(), blob=storage.blob_configured(),
                           legacy_photos=legacy_photos, eval_key=os.getenv('EVAL_KEY', ''),
                           models={'photo': listing_utils.PHOTO_TIER, 'video': listing_utils.VIDEO_TIER,
                                   'luna': listing_utils.DEPLOYMENT_LUNA, 'terra': listing_utils.DEPLOYMENT_TERRA,
                                   'sol': listing_utils.DEPLOYMENT_SOL},
                           upload_limit=app.config['UPLOAD_DAILY_LIMIT'])


@app.route('/admin/backup', methods=['POST'])
@admin_required
def admin_backup():
    """Copy the live SQLite database (consistent snapshot) to the backup container"""
    db = get_db()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'snapshot.db')
        dest = sqlite3.connect(path)
        db.backup(dest)
        dest.close()
        with open(path, 'rb') as f:
            data = f.read()
    key = f"db/tools-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    try:
        backup_store().put(key, data, 'application/vnd.sqlite3')
    except Exception as e:
        flash(f'Backup failed: {e}', 'error')
        return redirect(url_for('admin'))
    track(db, 'backup_created', value=len(data), key=key)
    db.commit()
    flash(f'Backed up the database ({len(data) // 1024} KB) to {key}.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/backups/<path:key>')
@admin_required
def admin_backup_download(key):
    if '..' in key or not key.startswith('db/'):
        abort(404)
    found = backup_store().get(key)
    if found is None:
        abort(404)
    return Response(found[0], mimetype='application/vnd.sqlite3',
                    headers={'Content-Disposition': f'attachment; filename="{key.split("/")[-1]}"'})


@app.route('/admin/migrate-media', methods=['POST'])
@admin_required
def admin_migrate_media():
    """Move photos saved on local disk before Blob Storage was set up into the media container"""
    db = get_db()
    if not storage.blob_configured():
        flash('Blob Storage isn\'t configured, so there is nothing to migrate to.', 'error')
        return redirect(url_for('admin'))
    legacy = os.path.abspath(app.config['UPLOAD_FOLDER'])
    moved = missing = 0
    refs = [('tools', 'photo_path', r[0]) for r in db.execute(
        "SELECT DISTINCT photo_path FROM tools WHERE photo_path IS NOT NULL AND photo_path NOT LIKE '%/%'")]
    refs += [('users', 'avatar_path', r[0]) for r in db.execute(
        "SELECT DISTINCT avatar_path FROM users WHERE avatar_path IS NOT NULL AND avatar_path NOT LIKE '%/%'")]
    for table, column, name in refs:
        path = os.path.join(legacy, name)
        if not os.path.isfile(path):
            missing += 1
            continue
        with open(path, 'rb') as f:
            data = f.read()
        new_key = media_store().put(f'listings/{name}' if table == 'tools' else f'avatars/{name}', data,
                                    mimetypes.guess_type(name)[0] or 'application/octet-stream')
        db.execute(f'UPDATE {table} SET {column} = ? WHERE {column} = ?', (new_key, name))
        moved += 1
    track(db, 'media_migrated', value=moved, missing=missing)
    db.commit()
    flash(f'Moved {moved} photo{"s" if moved != 1 else ""} to Blob Storage. {missing} missing on disk.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def admin_reset_password(user_id):
    """There's no email service yet, so an admin can issue a temporary password to pass on"""
    db = get_db()
    user = db.execute('SELECT id, name FROM users WHERE id = ? AND is_demo = 0', (user_id,)).fetchone()
    if user is None:
        abort(404)
    temp = secrets.token_urlsafe(9)
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(temp), user_id))
    track(db, 'password_reset_by_admin', user_id=user_id)
    db.commit()
    flash(f'Temporary password for {user["name"]}: {temp}  (shown once; ask them to change it under Account)', 'success')
    return redirect(url_for('admin'))


# ---------------------------------------------------------------------------
# Evaluation: run the test photos through the models and score the drafts
# ---------------------------------------------------------------------------

def eval_allowed(key):
    """Demo mode costs nothing, so it's open. With real models: admins, or the EVAL_KEY link."""
    if not listing_utils.ai_is_configured() or is_admin():
        return True
    expected = os.getenv('EVAL_KEY', '')
    return bool(expected) and hmac.compare_digest(key or '', expected)


@app.route('/eval')
def eval_page():
    key = request.args.get('key', '')
    runs = get_db().execute(
        """SELECT run_id, tier, COUNT(*) AS cases, ROUND(AVG(score), 1) AS avg_score, SUM(passed) AS passed,
                  SUM(error IS NOT NULL) AS errors, ROUND(SUM(cost), 4) AS cost, ROUND(AVG(seconds), 1) AS seconds,
                  MIN(created_at) AS started
           FROM eval_results GROUP BY run_id, tier ORDER BY started DESC LIMIT 30"""
    ).fetchall()
    return render_template('eval.html', cases=eval_runner.load_cases(), tiers=eval_runner.TIERS,
                           allowed=eval_allowed(key), key=key, runs=runs,
                           needs_key=listing_utils.ai_is_configured() and not os.getenv('EVAL_KEY') and not is_admin(),
                           photo_tier=listing_utils.PHOTO_TIER, video_tier=listing_utils.VIDEO_TIER)


@app.route('/api/eval/run', methods=['POST'])
def eval_run():
    data = request.get_json(silent=True) or {}
    if not eval_allowed(data.get('key', '')):
        return jsonify({'success': False, 'error': 'Missing or wrong eval key'}), 403
    case = eval_runner.get_case(data.get('case_id', ''))
    tier = data.get('tier', '')
    if case is None or tier not in eval_runner.TIERS:
        return jsonify({'success': False, 'error': 'Unknown case or tier'}), 400
    run_id = (data.get('run_id') or uuid.uuid4().hex)[:40]

    result = eval_runner.run_case(case, tier)
    db = get_db()
    db.execute(
        """INSERT INTO eval_results (run_id, case_id, tier, model, score, passed, seconds, cost, error, result_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, case['id'], tier, result.get('model'), result.get('score'), int(bool(result.get('passed'))),
         result.get('seconds'), result.get('cost', 0), result.get('error'), json.dumps(result),
         datetime.now().isoformat(timespec='seconds'))
    )
    track(db, 'eval_case', value=result.get('cost', 0), duration=result.get('seconds'), tier=tier, model=result.get('model'))
    db.commit()
    return jsonify({'success': True, 'run_id': run_id, 'result': result})


@app.route('/api/eval/runs/<run_id>')
def eval_run_detail(run_id):
    if not eval_allowed(request.args.get('key', '')):
        return jsonify({'success': False, 'error': 'Missing or wrong eval key'}), 403
    rows = get_db().execute('SELECT result_json FROM eval_results WHERE run_id = ? ORDER BY id', (run_id,)).fetchall()
    results = [json.loads(r['result_json']) for r in rows]
    return jsonify({'success': True, 'results': results,
                    'summary': {t: summarize_eval([r for r in results if r['tier'] == t])
                                for t in dict.fromkeys(r['tier'] for r in results)}})


@app.route('/eval/images/<path:filename>')
def eval_image(filename):
    return send_from_directory(eval_runner.IMAGES_DIR, filename)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8000, threaded=True)
