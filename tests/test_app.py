import io
import re
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['LISTING_AI_MODE'] = 'mock'

import app as app_module   # noqa: E402
import listing_utils        # noqa: E402

import db_builder           # noqa: E402

CENTER = db_builder.DEMO_CENTER


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    app_module.app.config.update(
        TESTING=True,
        CSRF_ENABLED=False,          # one test below turns it back on
        DB_PATH=str(tmp_path / 'test.db'),
        UPLOAD_FOLDER=str(tmp_path / 'uploads'),
        BACKUP_FOLDER=str(tmp_path / 'backups'),
    )
    app_module._failures.clear()
    with app_module.app.test_client() as c:
        yield c


def db_query(sql, params=()):
    with app_module.app.app_context():
        return app_module.get_db().execute(sql, params).fetchall()


def db_exec(sql, params=()):
    with app_module.app.app_context():
        db = app_module.get_db()
        cur = db.execute(sql, params)
        db.commit()
        return cur.lastrowid


def make_user(client, name='Pat', email=None, password='hunter2hunter2', platforms=(), lat=None, lng=None):
    """Sign up through the real form; leaves the client logged in as this user"""
    client.post('/logout')
    email = email or f'{name.lower().replace(" ", ".")}@example.com'
    data = {'name': name, 'email': email, 'password': password, 'neighborhood': 'Highland',
            'lat': '' if lat is None else str(lat), 'lng': '' if lng is None else str(lng),
            'battery_platforms': list(platforms)}
    res = client.post('/signup', data=data)
    assert res.status_code == 302, res.data[:500]
    return db_query('SELECT id FROM users WHERE email = ?', (email,))[0]['id']


def login_as(client, user_id):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id


def logout(client):
    with client.session_transaction() as sess:
        sess.pop('user_id', None)


def add_tool(owner_id, name='Drywall Panel Lift', risk='standard', price=2.5, status='listed', **extra):
    with app_module.app.app_context():
        db = app_module.get_db()
        tid = db_builder.insert_tool(db, owner_id=owner_id, name=name, category='Specialty', power_source='Manual',
                                     daily_price=price, deposit=150, replacement_value=260, risk_tier=risk,
                                     status=status, published_at='2026-09-30T10:00:00', **extra)
        db.commit()
        return tid


def find_tool_id(client, name):
    return db_query('SELECT id FROM tools WHERE name = ?', (name,))[0]['id']


def fake_jpeg(seed=b'1'):
    return io.BytesIO(b'\xff\xd8\xff\xe0' + seed * 64)


# --- pricing and helpers ---------------------------------------------------

def test_quote_math():
    q = listing_utils.quote(daily_price=20, deposit=200, days=2)
    assert q['rental_total'] == 40
    assert q['service_fee'] == 4
    assert q['total_charge'] == 44
    assert q['owner_payout'] == 34
    assert q['platform_revenue'] == 10
    assert q['deposit_hold'] == 200


def test_weekly_discount():
    q = listing_utils.quote(daily_price=10, deposit=0, days=7)
    assert q['weekly_discount'] == 17.5
    assert q['rental_total'] == 52.5


def test_delivery_fee_peak_and_minimum():
    assert listing_utils.delivery_fee(miles=1, hour=12) == listing_utils.DELIVERY_MIN_FEE
    midday = listing_utils.delivery_fee(miles=10, hour=12)
    rush = listing_utils.delivery_fee(miles=10, hour=17)
    late = listing_utils.delivery_fee(miles=10, hour=22)
    assert midday == 19.0
    assert rush > midday and late > rush


def test_rental_days():
    d = date(2026, 10, 1)
    assert listing_utils.rental_days(d, d) == 1
    assert listing_utils.rental_days(d, d + timedelta(days=1)) == 1
    assert listing_utils.rental_days(d, d + timedelta(days=3)) == 3


def test_normalize_draft_fixes_bad_values():
    d = listing_utils.normalize_draft({
        'name': 'Stihl Chainsaw', 'category': 'Nonsense', 'power_source': 'Gas',
        'battery_platform': 'Ryobi ONE+', 'replacement_value': 400, 'daily_price': 999, 'deposit': -5,
        'confidence': 3,
    })
    assert d['category'] == 'Specialty'
    assert d['battery_platform'] == ''      # gas tool has no battery system
    assert d['risk_tier'] == 'excluded'
    assert d['daily_price'] == 4            # ~1% of replacement value
    assert d['deposit'] == 300
    assert d['ai_confidence'] == 1.0


def test_draft_key_prefers_model_number():
    a = {'brand': 'Milwaukee', 'model': '2767-20', 'name': 'Impact wrench'}
    b = {'brand': 'milwaukee', 'model': '2767-20 ', 'name': '1/2" impact'}
    c = {'brand': 'Milwaukee', 'model': '', 'name': 'Impact wrench'}
    assert listing_utils.draft_key(a) == listing_utils.draft_key(b)
    assert listing_utils.draft_key(a) != listing_utils.draft_key(c)


# --- browse ----------------------------------------------------------------

def test_browse_lists_seeded_tools(client):
    res = client.get('/')
    assert res.status_code == 200
    assert b'Coil Spring Compressor Kit' in res.data
    assert b'demo mode' in res.data


def test_browse_fits_my_batteries(client):
    make_user(client, 'Ryan', platforms=['Ryobi ONE+'], lat=CENTER[0], lng=CENTER[1])
    res = client.get('/?platform=mine')
    assert b'String Trimmer' in res.data
    assert b'Framing Nailer' not in res.data   # Milwaukee M18


def test_browse_search_and_distance(client):
    res = client.get('/?q=hoist&max_miles=25')
    assert b'Engine Hoist' in res.data
    assert b'Pressure Washer' not in res.data
    assert b'Engine Hoist' not in client.get('/?q=hoist&max_miles=0.5').data
    assert b'No tools match' in client.get('/?q=zzz-no-such-tool').data


# --- snap-to-list ----------------------------------------------------------

def test_analyze_photo_creates_drafts_and_review_lists_them(client):
    member = make_user(client, 'Dana')
    res = client.post('/api/analyze-photo', data={'photo': (fake_jpeg(), 'shelf.jpg')},
                      content_type='multipart/form-data')
    data = res.get_json()
    assert data['success'] and data['demo_mode']
    assert data['drafts_created'] >= 1

    page = client.get('/list/review')
    assert page.status_code == 200
    with app_module.app.app_context():
        drafts = app_module.get_db().execute(
            "SELECT id, name, photo_path FROM tools WHERE owner_id = ? AND status = 'draft'", (member,)
        ).fetchall()
    assert drafts and all(d['photo_path'] for d in drafts)

    form = {f'action_{d["id"]}': 'approve' for d in drafts}
    form[f'daily_price_{drafts[0]["id"]}'] = '12'
    client.post('/list/review', data=form)
    with app_module.app.app_context():
        row = app_module.get_db().execute('SELECT status, daily_price, published_at, photo_path FROM tools WHERE id = ?',
                                          (drafts[0]['id'],)).fetchone()
    assert row['status'] == 'listed'
    assert row['daily_price'] == 12
    assert row['published_at']
    assert row['photo_path'].startswith('listings/')
    assert client.get(f"/uploads/{row['photo_path']}").status_code == 200   # served from storage


def test_video_frames_in_one_batch_are_deduplicated(client):
    make_user(client, 'Dana')
    frame = fake_jpeg(b'same-frame').getvalue()
    first = client.post('/api/analyze-photo', data={'photo': (io.BytesIO(frame), 'f1.jpg'), 'batch_id': 'walk-1'},
                        content_type='multipart/form-data').get_json()
    second = client.post('/api/analyze-photo', data={'photo': (io.BytesIO(frame), 'f2.jpg'), 'batch_id': 'walk-1'},
                         content_type='multipart/form-data').get_json()
    assert first['drafts_created'] >= 1
    assert second['drafts_created'] == 0
    assert second['duplicates_skipped'] == first['drafts_created']


def test_analyze_photo_rejects_bad_files(client):
    make_user(client, 'Dana')
    res = client.post('/api/analyze-photo', data={'photo': (io.BytesIO(b'x'), 'notes.pdf')},
                      content_type='multipart/form-data')
    assert res.status_code == 400
    assert client.post('/api/analyze-photo').status_code == 400


def test_excluded_tools_never_go_live(client, monkeypatch):
    make_user(client, 'Dana')
    monkeypatch.setattr(listing_utils, 'analyze_photo', lambda b, m, source='photo': (
        [listing_utils.normalize_draft({'name': 'Gas Chainsaw', 'replacement_value': 300})], 0.0, True, 'demo'))
    client.post('/api/analyze-photo', data={'photo': (fake_jpeg(), 'saw.jpg')}, content_type='multipart/form-data')
    tool_id = find_tool_id(client, 'Gas Chainsaw')
    client.post('/list/review', data={f'action_{tool_id}': 'approve', f'name_{tool_id}': 'Gas Chainsaw',
                                      f'risk_tier_{tool_id}': 'standard'})
    assert client.get(f'/tool/{tool_id}').status_code == 404


# --- booking flow ----------------------------------------------------------

def book(client, tool_id, start, end, **extra):
    return client.post(f'/tool/{tool_id}/book', data={
        'start': start.isoformat(), 'end': end.isoformat(), 'hour': '10', **extra})


def test_full_booking_flow(client):
    owner = make_user(client, 'Owner Olive', lat=CENTER[0] + 0.01, lng=CENTER[1])
    tool_id = add_tool(owner)
    renter = make_user(client, 'Renter Rae', lat=CENTER[0], lng=CENTER[1])
    other = make_user(client, 'Other Otto', lat=CENTER[0], lng=CENTER[1])
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=2)

    login_as(client, renter)
    quote = client.get(f'/api/quote?tool_id={tool_id}&start={start}&end={end}&delivery=1&hour=10').get_json()
    assert quote['success'] and quote['days'] == 2 and quote['delivery_fee'] >= 10

    res = book(client, tool_id, start, end, delivery='1')
    assert res.status_code == 302
    booking = db_query('SELECT * FROM bookings')[0]
    assert booking['status'] == 'requested'
    assert booking['total_charge'] == quote['total_charge']

    # Renter can't accept their own request
    assert client.post(f'/booking/{booking["id"]}/accept').status_code == 403

    login_as(client, owner)
    client.post(f'/booking/{booking["id"]}/accept')
    assert b'accepted' in client.get('/garage').data

    # Overlapping request from someone else is refused
    login_as(client, other)
    book(client, tool_id, start, start)
    assert len(db_query('SELECT id FROM bookings')) == 1

    login_as(client, owner)
    client.post(f'/booking/{booking["id"]}/returned')
    assert db_query('SELECT status FROM bookings')[0]['status'] == 'returned'
    events = [r['event'] for r in db_query('SELECT event FROM events')]
    assert {'booking_requested', 'booking_accepted', 'booking_returned'} <= set(events)


def test_waiver_required_for_risky_tools(client):
    owner = make_user(client, 'Owner Olive')
    tool_id = add_tool(owner, name='Coil Spring Compressor', risk='waiver')
    make_user(client, 'Renter Rae')
    start = date.today() + timedelta(days=1)
    book(client, tool_id, start, start)
    book(client, tool_id, start, start, waiver='on')
    assert len(db_query('SELECT id FROM bookings')) == 1


def test_cannot_book_own_tool_or_past_dates(client):
    owner = make_user(client, 'Owner Olive')
    tool_id = add_tool(owner)
    book(client, tool_id, date.today(), date.today())
    make_user(client, 'Renter Rae')
    yesterday = date.today() - timedelta(days=1)
    book(client, tool_id, yesterday, yesterday)
    assert len(db_query('SELECT id FROM bookings')) == 0


def test_sample_listings_cannot_be_booked(client):
    make_user(client, 'Renter Rae')
    tool_id = find_tool_id(client, 'Drywall Panel Lift')   # seeded demo owner
    assert b'sample listing' in client.get(f'/tool/{tool_id}').data
    book(client, tool_id, date.today() + timedelta(days=1), date.today() + timedelta(days=1))
    assert len(db_query('SELECT id FROM bookings')) == 0


def fake_responses_client(tools_by_model, calls):
    """Fake OpenAI client whose responses.create returns canned structured output per deployment"""
    import json
    from types import SimpleNamespace as NS

    def create(**kwargs):
        calls.append(kwargs)
        return NS(status='completed', output_text=json.dumps({'tools': tools_by_model[kwargs['model']]}),
                  usage=NS(input_tokens=1500, output_tokens=500))
    return NS(responses=NS(create=create))


def tool_json(name, confidence):
    return {'name': name, 'brand': 'Milwaukee', 'model': '2767-20', 'category': 'Power Tools',
            'power_source': 'Battery', 'battery_platform': 'Milwaukee M18', 'description': 'd',
            'included_items': 'Bare tool', 'replacement_value': 400, 'daily_price': 20, 'deposit': 200,
            'risk_tier': 'standard', 'safety_notes': 's', 'confidence': confidence}


@pytest.fixture
def live_ai(monkeypatch):
    """Pretend Azure is configured, with default GPT-5.6 deployment names"""
    monkeypatch.delenv('LISTING_AI_MODE', raising=False)
    monkeypatch.setattr(listing_utils, 'endpoint', 'https://res.openai.azure.com/')
    monkeypatch.setattr(listing_utils, 'subscription_key', 'k')


def test_v1_base_url():
    assert listing_utils.v1_base_url('https://res.openai.azure.com/') == 'https://res.openai.azure.com/openai/v1/'
    assert listing_utils.v1_base_url('https://res.openai.azure.com/openai/v1') == 'https://res.openai.azure.com/openai/v1/'


def test_responses_request_shape_and_parsing(live_ai, monkeypatch):
    calls = []
    monkeypatch.setattr(listing_utils, 'get_client',
                        lambda: fake_responses_client({'gpt-5.6-terra': [tool_json('Impact Wrench', 0.9)]}, calls))
    drafts, cost, demo, used = listing_utils.analyze_photo(b'\xff\xd8img', 'image/jpeg', source='photo')

    assert not demo and used == 'gpt-5.6-terra'
    assert drafts[0]['battery_platform'] == 'Milwaukee M18'
    assert cost == pytest.approx(0.01125)   # 1500 in @ $2.50/M + 500 out @ $15/M
    req = calls[0]
    assert req['text']['format']['strict'] is True
    assert req['reasoning'] == {'effort': 'low'}
    assert 'temperature' not in req and 'max_tokens' not in req   # rejected by GPT-5.6
    image = req['input'][0]['content'][1]
    assert image['type'] == 'input_image' and image['image_url'].startswith('data:image/jpeg;base64,')


def test_video_frames_use_luna(live_ai, monkeypatch):
    calls = []
    monkeypatch.setattr(listing_utils, 'get_client',
                        lambda: fake_responses_client({'gpt-5.6-luna': [tool_json('Drill', 0.95)]}, calls))
    _, _, _, used = listing_utils.analyze_photo(b'img', source='video')
    assert used == 'gpt-5.6-luna' and len(calls) == 1


def test_low_confidence_escalates_to_sol(live_ai, monkeypatch):
    calls = []
    monkeypatch.setattr(listing_utils, 'get_client', lambda: fake_responses_client({
        'gpt-5.6-luna': [tool_json('Blurry thing', 0.3)],
        'gpt-5.6-sol': [tool_json('Engine hoist', 0.85)],
    }, calls))
    drafts, _, _, used = listing_utils.analyze_photo(b'img', source='video')
    assert used == 'gpt-5.6-luna > gpt-5.6-sol'
    assert drafts[0]['name'] == 'Engine hoist'
    assert calls[1]['reasoning'] == {'effort': 'medium'}


def test_escalation_can_be_disabled(live_ai, monkeypatch):
    calls = []
    monkeypatch.setattr(listing_utils, 'DEPLOYMENT_SOL', '')
    monkeypatch.setattr(listing_utils, 'get_client',
                        lambda: fake_responses_client({'gpt-5.6-terra': [tool_json('Blurry', 0.2)]}, calls))
    drafts, _, _, used = listing_utils.analyze_photo(b'img')
    assert used == 'gpt-5.6-terra' and len(calls) == 1 and drafts[0]['name'] == 'Blurry'


def test_photos_on_sol_do_not_escalate_to_sol_again(live_ai, monkeypatch):
    calls = []
    monkeypatch.setattr(listing_utils, 'PHOTO_TIER', 'sol')
    monkeypatch.setattr(listing_utils, 'get_client',
                        lambda: fake_responses_client({'gpt-5.6-sol': [tool_json('Blurry', 0.2)]}, calls))
    drafts, cost, _, used = listing_utils.analyze_photo(b'img', source='photo')
    assert used == 'gpt-5.6-sol' and len(calls) == 1
    assert calls[0]['reasoning'] == {'effort': 'medium'}
    assert cost == pytest.approx(1500 / 1e6 * 5 + 500 / 1e6 * 30)   # Sol pricing


# --- cheap pricing --------------------------------------------------------

def test_daily_price_is_about_one_percent():
    assert listing_utils.suggest_daily_price(400) == 4
    assert listing_utils.suggest_daily_price(180) == 2       # 1.80 -> nearest $0.50
    assert listing_utils.suggest_daily_price(1000) == 7      # 0.7% above $500
    assert listing_utils.suggest_daily_price(40) == 1        # $1 minimum


def test_ai_price_too_high_is_replaced_but_owner_price_is_kept():
    ai = listing_utils.normalize_draft({'name': 'Saw', 'replacement_value': 400, 'daily_price': 40})
    assert ai['daily_price'] == 4
    owner = listing_utils.normalize_draft({'name': 'Saw', 'replacement_value': 400, 'daily_price': 12}, owner_priced=True)
    assert owner['daily_price'] == 12
    assert listing_utils.normalize_draft({'name': 'x', 'daily_price': 0.2}, owner_priced=True)['daily_price'] == 1


def test_money_filter(client):
    f = app_module.app.jinja_env.filters['money']
    assert f(4) == '$4' and f(4.5) == '$4.50' and f(None) == '$0'


def test_seeded_prices_are_cheap(client):
    assert b'$1.50' in client.get('/').data   # coil spring compressor


# --- evaluation harness ---------------------------------------------------

from evals import scoring, runner   # noqa: E402


def eval_case(tools, max_listings=3):
    return {'id': 'c', 'expect': {'tools': tools, 'max_listings': max_listings}}


def test_scoring_matches_phrases_and_checks_attributes():
    case = eval_case([
        {'label': 'miter saw', 'match': ['miter saw', 'chop saw'], 'brand': 'DeWalt', 'category': ['Woodworking', 'Power Tools']},
        {'label': 'drill', 'match': ['drill'], 'battery_platform': ['DeWalt 20V MAX']},
    ])
    drafts = [
        {'name': '12" Sliding Chop Saw', 'brand': 'DeWalt', 'category': 'Woodworking'},
        {'name': 'Cordless Drill', 'brand': 'DeWalt', 'battery_platform': 'Other'},
    ]
    r = scoring.score_case(case, drafts)
    assert r['found'] == 2 and r['missing'] == []
    assert [c['ok'] for c in r['checks']] == [True, True, False]   # brand, category ok; battery wrong
    assert r['score'] == 60 + 20 + 10 and r['passed']


def test_scoring_each_draft_matches_one_tool_and_penalizes_over_listing():
    case = eval_case([{'label': 'drill', 'match': ['drill']}, {'label': 'drill 2', 'match': ['drill']}], max_listings=1)
    r = scoring.score_case(case, [{'name': 'Drill'}, {'name': 'Sander'}])
    assert r['found'] == 1 and r['missing'] == ['drill 2'] and r['over_listed'] == 1
    assert r['score'] == 30 + 15 and not r['passed']


def test_scoring_negative_case():
    assert scoring.score_case(eval_case([], 0), [])['passed']
    r = scoring.score_case(eval_case([], 0), [{'name': 'Lamp'}])
    assert r['score'] == 75 and not r['passed']


def test_eval_cases_are_valid_and_images_exist():
    cases = runner.load_cases()
    assert len(cases) >= 15 and len({c['id'] for c in cases}) == len(cases)
    for c in cases:
        assert os.path.exists(os.path.join(runner.IMAGES_DIR, c['file'])), c['file']
        assert c['source']['license'] in ('by', 'by-sa', 'cc0', 'pdm')
        for t in c['expect']['tools']:
            assert t['match'] and t['label']
            if 'category' in t:
                assert set(t['category'] if isinstance(t['category'], list) else [t['category']]) <= set(listing_utils.CATEGORIES)
            if 'battery_platform' in t:
                assert set(t['battery_platform']) <= set(listing_utils.BATTERY_PLATFORMS)


def test_eval_runner_with_fake_model(live_ai, monkeypatch):
    calls = []
    monkeypatch.setattr(listing_utils, 'get_client', lambda: fake_responses_client({
        'gpt-5.6-sol': [dict(tool_json('Folding Engine Hoist', 0.9), category='Automotive', risk_tier='waiver'),
                        dict(tool_json('Floor Jack', 0.9), category='Automotive', risk_tier='standard')],
    }, calls))
    r = runner.run_case(runner.get_case('garage-engine-hoist'), 'sol')
    assert r['found'] == 2 and r['passed'] and r['model'] == 'gpt-5.6-sol'


def test_eval_page_and_api_in_demo_mode(client):
    assert b'model test bench' in client.get('/eval').data
    res = client.post('/api/eval/run', json={'case_id': 'living-room-no-tools', 'tier': 'pipeline', 'run_id': 'r1'})
    data = res.get_json()
    assert data['success'] and data['result']['model'] == 'demo'
    detail = client.get('/api/eval/runs/r1').get_json()
    assert detail['results'][0]['case_id'] == 'living-room-no-tools'
    assert b'pipeline' in client.get('/eval').data           # shows in previous runs
    assert client.get('/eval/images/living-room-no-tools.jpg').status_code == 200


def test_eval_api_requires_key_when_models_are_live(client, live_ai, monkeypatch):
    monkeypatch.setenv('EVAL_KEY', 'sekrit')
    body = {'case_id': 'living-room-no-tools', 'tier': 'luna'}
    assert client.post('/api/eval/run', json=body).status_code == 403
    assert client.post('/api/eval/run', json=dict(body, key='wrong')).status_code == 403
    monkeypatch.delenv('EVAL_KEY')
    assert client.post('/api/eval/run', json=dict(body, key='')).status_code == 403   # no key configured = closed


# --- accounts ---------------------------------------------------------------

def test_visitors_can_browse_but_must_log_in_to_post_or_rent(client):
    assert client.get('/').status_code == 200
    owner = make_user(client, 'Owner Olive')
    tool_id = add_tool(owner)
    client.post('/logout')
    page = client.get(f'/tool/{tool_id}').data
    assert b'log in to rent' in page
    res = client.get('/list')
    assert res.status_code == 302 and '/login?next=/list' in res.headers['Location']
    assert client.get('/garage').status_code == 302
    assert client.post('/api/analyze-photo').status_code == 401


def test_signup_validates_and_rejects_duplicate_email(client):
    bad = client.post('/signup', data={'name': '', 'email': 'nope', 'password': 'short', 'neighborhood': ''})
    assert bad.status_code == 400 and b'valid email' in bad.data and b'at least 8' in bad.data
    make_user(client, 'Pat', email='pat@example.com')
    client.post('/logout')
    dup = client.post('/signup', data={'name': 'Pat 2', 'email': 'PAT@example.com', 'password': 'longenough1',
                                       'neighborhood': 'Baker'})
    assert dup.status_code == 400 and b'already exists' in dup.data


def test_signup_stores_hashed_password_and_rounded_location(client):
    uid = make_user(client, 'Pat', lat=39.7392123, lng=-104.9903456, platforms=['Milwaukee M18'])
    u = db_query('SELECT * FROM users WHERE id = ?', (uid,))[0]
    assert u['password_hash'] != 'hunter2hunter2' and u['password_hash'].startswith(('scrypt:', 'pbkdf2:'))
    assert (u['lat'], u['lng']) == (39.739, -104.99)
    assert u['battery_platforms'] == 'Milwaukee M18' and u['is_demo'] == 0
    assert b'Pat' in client.get('/garage').data


def test_login_logout_and_lockout(client):
    make_user(client, 'Pat', email='pat@example.com')
    client.post('/logout')
    assert client.get('/garage').status_code == 302
    assert client.post('/login', data={'email': 'pat@example.com', 'password': 'wrong-password'}).status_code == 401
    ok = client.post('/login?next=/garage', data={'email': 'PAT@example.com', 'password': 'hunter2hunter2'})
    assert ok.status_code == 302 and ok.headers['Location'].endswith('/garage')
    assert client.get('/garage').status_code == 200
    # next must stay on this site
    client.post('/logout')
    evil = client.post('/login?next=//evil.example', data={'email': 'pat@example.com', 'password': 'hunter2hunter2'})
    assert evil.headers['Location'] == '/'
    client.post('/logout')
    for _ in range(8):
        client.post('/login', data={'email': 'pat@example.com', 'password': 'nope-nope'})
    locked = client.post('/login', data={'email': 'pat@example.com', 'password': 'hunter2hunter2'})
    assert locked.status_code == 429
    events = [r['event'] for r in db_query('SELECT event FROM events')]
    assert events.count('login_failed') == 8 and 'login' in events and 'signup' in events


def test_demo_users_cannot_log_in(client):
    assert client.post('/login', data={'email': '', 'password': ''}).status_code == 401
    login_as(client, 1)    # seeded demo user id in the session is ignored
    assert client.get('/garage').status_code == 302


def test_account_update_avatar_and_password(client):
    uid = make_user(client, 'Pat', email='pat@example.com')
    other = make_user(client, 'Sam', email='sam@example.com')
    login_as(client, uid)
    taken = client.post('/account', data={'name': 'Pat', 'email': 'sam@example.com', 'neighborhood': 'Baker'})
    assert taken.status_code == 302 and db_query('SELECT email FROM users WHERE id = ?', (uid,))[0]['email'] == 'pat@example.com'
    client.post('/account', data={'name': 'Pat Q', 'email': 'patq@example.com', 'neighborhood': 'Baker',
                                  'bio': 'I fix old trucks.', 'battery_platforms': ['DeWalt 20V MAX', 'bogus'],
                                  'lat': '39.75', 'lng': '-105.0'})
    u = db_query('SELECT * FROM users WHERE id = ?', (uid,))[0]
    assert (u['name'], u['email'], u['bio'], u['battery_platforms'], u['lat']) == \
        ('Pat Q', 'patq@example.com', 'I fix old trucks.', 'DeWalt 20V MAX', 39.75)

    client.post('/account/avatar', data={'avatar': (fake_jpeg(), 'me.jpg')}, content_type='multipart/form-data')
    key = db_query('SELECT avatar_path FROM users WHERE id = ?', (uid,))[0]['avatar_path']
    assert key.startswith('avatars/')
    served = client.get(f'/uploads/{key}')
    assert served.status_code == 200 and served.mimetype == 'image/jpeg' and 'immutable' in served.headers['Cache-Control']
    assert key.encode() in client.get(f'/u/{uid}').data

    client.post('/account/password', data={'current_password': 'wrong', 'new_password': 'newpassword1'})
    client.post('/account/password', data={'current_password': 'hunter2hunter2', 'new_password': 'newpassword1'})
    client.post('/logout')
    assert client.post('/login', data={'email': 'patq@example.com', 'password': 'newpassword1'}).status_code == 302
    assert other


def test_public_profile_shows_listings(client):
    uid = make_user(client, 'Pat')
    add_tool(uid, name='Engine Hoist')
    client.post('/logout')
    page = client.get(f'/u/{uid}').data
    assert b'Engine Hoist' in page and b'Pat' in page and b'pat@example.com' not in page


def test_csrf_blocks_forged_posts(client):
    uid = make_user(client, 'Pat')
    tool_id = add_tool(uid)
    app_module.app.config['CSRF_ENABLED'] = True
    try:
        forged = client.post(f'/tool/{tool_id}/unlist')
        assert forged.status_code == 302
        assert db_query('SELECT status FROM tools WHERE id = ?', (tool_id,))[0]['status'] == 'listed'
        assert client.post('/api/analyze-photo').status_code == 400
        client.get('/')                       # rendering a page issues the token
        with client.session_transaction() as sess:
            token = sess['csrf']
        client.post(f'/tool/{tool_id}/unlist', data={'_csrf': token})
        assert db_query('SELECT status FROM tools WHERE id = ?', (tool_id,))[0]['status'] == 'skipped'
    finally:
        app_module.app.config['CSRF_ENABLED'] = False


def test_daily_upload_cap(client, monkeypatch):
    make_user(client, 'Pat')
    monkeypatch.setitem(app_module.app.config, 'UPLOAD_DAILY_LIMIT', 2)
    codes = [client.post('/api/analyze-photo', data={'photo': (fake_jpeg(bytes([i])), f'{i}.jpg')},
                         content_type='multipart/form-data').status_code for i in range(3)]
    assert codes == [200, 200, 429]


# --- telemetry, admin, storage ---------------------------------------------

def test_page_views_and_ai_calls_are_tracked(client):
    make_user(client, 'Pat')
    client.get('/')
    client.post('/api/analyze-photo', data={'photo': (fake_jpeg(), 'a.jpg')}, content_type='multipart/form-data')
    rows = db_query("SELECT event, path, visitor, props FROM events WHERE event IN ('page_view', 'photo_analyzed')")
    assert any(r['event'] == 'page_view' and r['path'] == 'browse' and r['visitor'] for r in rows)
    ai = [r for r in rows if r['event'] == 'photo_analyzed'][0]
    assert '"source": "photo"' in ai['props'] and '"model": "demo"' in ai['props']


def test_admin_requires_password(client, monkeypatch):
    assert client.get('/admin').status_code == 302
    assert b'turned off' in client.get('/admin/login').data
    monkeypatch.setenv('ADMIN_PASSWORD', 'correct horse')
    assert client.post('/admin/login', data={'password': 'nope'}).status_code == 401
    assert client.post('/admin/login', data={'password': 'correct horse'}).status_code == 302
    assert client.get('/admin').status_code == 200
    client.post('/admin/logout')
    assert client.get('/admin').status_code == 302
    for _ in range(5):
        client.post('/admin/login', data={'password': 'nope'})
    assert client.post('/admin/login', data={'password': 'correct horse'}).status_code == 429


def admin_session(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD', 'pw')
    client.post('/admin/login', data={'password': 'pw'})


def test_admin_dashboard_shows_metrics(client, monkeypatch):
    owner = make_user(client, 'Owner Olive')
    add_tool(owner, name='Engine Hoist')
    renter = make_user(client, 'Renter Rae')
    book(client, find_tool_id(client, 'Engine Hoist'), date.today() + timedelta(days=1), date.today() + timedelta(days=2))
    client.post('/api/analyze-photo', data={'photo': (fake_jpeg(), 'a.jpg')}, content_type='multipart/form-data')
    admin_session(client, monkeypatch)
    page = client.get('/admin').data.decode()
    assert 'admin dashboard' in page and 'Owner Olive' in page and 'renter.rae@example.com' in page
    assert 'class="column-chart"' in page and 'class="funnel"' in page
    with app_module.app.app_context():
        k = app_module.metrics.kpis(app_module.get_db())
    assert (k['users'], k['listings_live'], k['posts_7d'], k['bookings'], k['ai_calls_7d']) == (2, 1, 1, 1, 1)
    assert k['listings_live_demo'] == 11
    assert renter


def test_admin_backup_and_download(client, monkeypatch):
    admin_session(client, monkeypatch)
    client.post('/admin/backup')
    page = client.get('/admin').data.decode()
    key = re.search(r'(db/tools-[0-9-]+\.db)', page).group(1)
    res = client.get(f'/admin/backups/{key}')
    assert res.status_code == 200 and res.data[:16] == b'SQLite format 3\x00'
    assert client.get('/admin/backups/../secrets').status_code == 404


def test_admin_reset_password(client, monkeypatch):
    uid = make_user(client, 'Pat', email='pat@example.com')
    client.post('/logout')
    admin_session(client, monkeypatch)
    res = client.post(f'/admin/users/{uid}/reset-password', follow_redirects=True)
    temp = re.search(r'Temporary password for Pat: (\S+)', res.data.decode()).group(1)
    assert client.post('/login', data={'email': 'pat@example.com', 'password': temp}).status_code == 302


def test_media_migration_moves_legacy_photos(client, monkeypatch, tmp_path):
    uid = make_user(client, 'Pat')
    legacy_dir = tmp_path / 'uploads'
    legacy_dir.mkdir(exist_ok=True)
    (legacy_dir / 'old.jpg').write_bytes(b'\xff\xd8legacy')
    tool_id = add_tool(uid, photo_path='old.jpg')
    assert client.get('/uploads/old.jpg').data == b'\xff\xd8legacy'      # still served from disk

    blob = app_module.storage.LocalStorage(str(tmp_path / 'fake-blob'))   # stands in for the blob container
    monkeypatch.setattr(app_module.storage, 'blob_configured', lambda: True)
    monkeypatch.setattr(app_module, 'media_store', lambda: blob)
    admin_session(client, monkeypatch)
    client.post('/admin/migrate-media')
    assert db_query('SELECT photo_path FROM tools WHERE id = ?', (tool_id,))[0]['photo_path'] == 'listings/old.jpg'
    assert client.get('/uploads/listings/old.jpg').data == b'\xff\xd8legacy'


def test_local_storage_blocks_path_traversal(tmp_path):
    store = app_module.storage.LocalStorage(str(tmp_path / 'root'))
    store.put('listings/a.jpg', b'x', 'image/jpeg')
    assert store.get('listings/a.jpg') == (b'x', None)
    assert store.get('../outside.txt') is None
    with pytest.raises(ValueError):
        store.put('../escape.jpg', b'x', 'image/jpeg')


def test_blob_storage_uses_container_api(monkeypatch):
    """BlobStorage against a fake SDK client: right container, content type and not-found handling"""
    from azure.core.exceptions import ResourceNotFoundError
    from types import SimpleNamespace as NS
    saved = {}

    class FakeContainer:
        def upload_blob(self, key, data, overwrite, content_settings):
            saved[key] = (data, content_settings.content_type)

        def download_blob(self, key):
            if key not in saved:
                raise ResourceNotFoundError('nope')
            data, ctype = saved[key]
            return NS(readall=lambda: data, properties=NS(content_settings=NS(content_type=ctype)))

    fake_service = NS(account_name='nradls', get_container_client=lambda name: FakeContainer())
    import azure.storage.blob as blob_sdk
    monkeypatch.setattr(blob_sdk.BlobServiceClient, 'from_connection_string', staticmethod(lambda cs: fake_service))
    store = app_module.storage.BlobStorage('toolshare-media', connection_string='UseDevelopmentStorage=true')
    store.put('listings/x.jpg', b'img', 'image/jpeg')
    assert store.get('listings/x.jpg') == (b'img', 'image/jpeg')
    assert store.get('listings/missing.jpg') is None
    assert 'nradls/toolshare-media' in store.describe()


def test_migration_from_v1_database(tmp_path):
    """A database from before accounts gets the new columns, and its users become demo users"""
    path = str(tmp_path / 'v1.db')
    conn = db_builder.get_connection(path)
    conn.executescript(db_builder.SCHEMA.split('-- Results from the /eval page')[0])
    conn.execute("INSERT INTO users (name, neighborhood, lat, lng) VALUES ('Old', 'X', 0, 0)")
    db_builder.insert_tool(conn, owner_id=1, name='Old saw', category='Specialty', power_source='Manual',
                           daily_price=3, deposit=50, status='listed')
    conn.execute('PRAGMA user_version = 1')
    conn.commit()
    db_builder.init_database(conn)
    u = conn.execute('SELECT * FROM users').fetchone()
    t = conn.execute('SELECT * FROM tools').fetchone()
    assert u['is_demo'] == 1 and u['email'] is None and t['published_at'] and t['daily_price'] == 3
    assert conn.execute('PRAGMA user_version').fetchone()[0] == db_builder.SCHEMA_VERSION


def test_security_headers(client):
    h = client.get('/').headers
    assert h['X-Content-Type-Options'] == 'nosniff' and h['X-Frame-Options'] == 'DENY'
