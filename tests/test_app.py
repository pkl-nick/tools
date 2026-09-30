import io
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['LISTING_AI_MODE'] = 'mock'

import app as app_module   # noqa: E402
import listing_utils        # noqa: E402

# Seeded user ids (see db_builder.DEMO_USERS)
CONTRACTOR, DANA, MECHANIC, RENTER = 1, 2, 3, 4


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(
        TESTING=True,
        DB_PATH=str(tmp_path / 'test.db'),
        UPLOAD_FOLDER=str(tmp_path / 'uploads'),
    )
    with app_module.app.test_client() as c:
        yield c


def act_as(client, user_id):
    client.post(f'/switch-user/{user_id}')


def find_tool_id(client, name):
    with app_module.app.app_context():
        row = app_module.get_db().execute('SELECT id FROM tools WHERE name = ?', (name,)).fetchone()
        return row['id']


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
    assert d['daily_price'] == 40           # 10% of replacement value
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
    act_as(client, RENTER)   # renter is on Ryobi ONE+
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
    act_as(client, DANA)
    res = client.post('/api/analyze-photo', data={'photo': (fake_jpeg(), 'shelf.jpg')},
                      content_type='multipart/form-data')
    data = res.get_json()
    assert data['success'] and data['demo_mode']
    assert data['drafts_created'] >= 1

    page = client.get('/list/review')
    assert page.status_code == 200
    with app_module.app.app_context():
        drafts = app_module.get_db().execute(
            "SELECT id, name, photo_path FROM tools WHERE owner_id = ? AND status = 'draft'", (DANA,)
        ).fetchall()
    assert drafts and all(d['photo_path'] for d in drafts)

    form = {f'action_{d["id"]}': 'approve' for d in drafts}
    form[f'daily_price_{drafts[0]["id"]}'] = '12'
    client.post('/list/review', data=form)
    with app_module.app.app_context():
        row = app_module.get_db().execute('SELECT status, daily_price FROM tools WHERE id = ?',
                                          (drafts[0]['id'],)).fetchone()
    assert row['status'] == 'listed'
    assert row['daily_price'] == 12


def test_video_frames_in_one_batch_are_deduplicated(client):
    act_as(client, DANA)
    frame = fake_jpeg(b'same-frame').getvalue()
    first = client.post('/api/analyze-photo', data={'photo': (io.BytesIO(frame), 'f1.jpg'), 'batch_id': 'walk-1'},
                        content_type='multipart/form-data').get_json()
    second = client.post('/api/analyze-photo', data={'photo': (io.BytesIO(frame), 'f2.jpg'), 'batch_id': 'walk-1'},
                         content_type='multipart/form-data').get_json()
    assert first['drafts_created'] >= 1
    assert second['drafts_created'] == 0
    assert second['duplicates_skipped'] == first['drafts_created']


def test_analyze_photo_rejects_bad_files(client):
    res = client.post('/api/analyze-photo', data={'photo': (io.BytesIO(b'x'), 'notes.pdf')},
                      content_type='multipart/form-data')
    assert res.status_code == 400
    assert client.post('/api/analyze-photo').status_code == 400


def test_excluded_tools_never_go_live(client, monkeypatch):
    act_as(client, DANA)
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
    tool_id = find_tool_id(client, 'Drywall Panel Lift')   # owned by Dana, standard tier
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=2)

    act_as(client, RENTER)
    quote = client.get(f'/api/quote?tool_id={tool_id}&start={start}&end={end}&delivery=1&hour=10').get_json()
    assert quote['success'] and quote['days'] == 2 and quote['delivery_fee'] >= 10

    res = book(client, tool_id, start, end, delivery='1')
    assert res.status_code == 302
    with app_module.app.app_context():
        booking = app_module.get_db().execute('SELECT * FROM bookings').fetchone()
    assert booking['status'] == 'requested'
    assert booking['total_charge'] == quote['total_charge']

    # Renter can't accept their own request
    assert client.post(f'/booking/{booking["id"]}/accept').status_code == 403

    act_as(client, DANA)
    client.post(f'/booking/{booking["id"]}/accept')
    assert b'accepted' in client.get('/garage').data

    # Overlapping request from someone else is refused
    act_as(client, MECHANIC)
    book(client, tool_id, start, start)
    with app_module.app.app_context():
        assert app_module.get_db().execute('SELECT COUNT(*) FROM bookings').fetchone()[0] == 1

    act_as(client, DANA)
    client.post(f'/booking/{booking["id"]}/returned')
    with app_module.app.app_context():
        status = app_module.get_db().execute('SELECT status FROM bookings').fetchone()['status']
    assert status == 'returned'


def test_waiver_required_for_risky_tools(client):
    tool_id = find_tool_id(client, 'Coil Spring Compressor Kit')
    start = date.today() + timedelta(days=1)
    act_as(client, RENTER)
    book(client, tool_id, start, start)
    book(client, tool_id, start, start, waiver='on')
    with app_module.app.app_context():
        assert app_module.get_db().execute('SELECT COUNT(*) FROM bookings').fetchone()[0] == 1


def test_cannot_book_own_tool_or_past_dates(client):
    tool_id = find_tool_id(client, 'Drywall Panel Lift')
    act_as(client, DANA)
    book(client, tool_id, date.today(), date.today())
    act_as(client, RENTER)
    yesterday = date.today() - timedelta(days=1)
    book(client, tool_id, yesterday, yesterday)
    with app_module.app.app_context():
        assert app_module.get_db().execute('SELECT COUNT(*) FROM bookings').fetchone()[0] == 0


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
