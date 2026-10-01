"""
SQLite database for the tool-sharing POC.

Creates the schema on first run and seeds a handful of demo neighbors and
tools so the browse page has something to show before anyone lists gear.
"""

import os
import sqlite3

import search
from datetime import datetime

DEFAULT_DB_PATH = os.path.join('instance', 'tools.db')

# Demo neighborhood center (Denver, CO). Override with DEMO_CENTER_LAT / DEMO_CENTER_LNG.
DEMO_CENTER = (
    float(os.getenv('DEMO_CENTER_LAT', '39.7392')),
    float(os.getenv('DEMO_CENTER_LNG', '-104.9903')),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    battery_platforms TEXT NOT NULL DEFAULT ''   -- comma-separated, e.g. "Milwaukee M18,Ryobi ONE+"
);

CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    power_source TEXT NOT NULL,
    battery_platform TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    included_items TEXT NOT NULL DEFAULT '',
    daily_price REAL NOT NULL,
    deposit REAL NOT NULL,
    replacement_value REAL NOT NULL DEFAULT 0,
    risk_tier TEXT NOT NULL DEFAULT 'standard',   -- standard | waiver | excluded
    safety_notes TEXT NOT NULL DEFAULT '',
    photo_path TEXT,
    ai_confidence REAL,
    batch_id TEXT,                                -- groups drafts from one upload session (video frames)
    status TEXT NOT NULL DEFAULT 'draft',         -- draft | listed | skipped
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    tool_id INTEGER NOT NULL REFERENCES tools(id),
    renter_id INTEGER NOT NULL REFERENCES users(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    rental_total REAL NOT NULL,
    service_fee REAL NOT NULL,
    delivery INTEGER NOT NULL DEFAULT 0,
    delivery_fee REAL NOT NULL DEFAULT 0,
    deposit REAL NOT NULL,
    total_charge REAL NOT NULL,
    owner_payout REAL NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'requested',     -- requested | accepted | declined | cancelled | returned
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tools_status ON tools(status);
CREATE INDEX IF NOT EXISTS idx_bookings_tool ON bookings(tool_id);

-- Results from the /eval page, one row per case per model tier
CREATE TABLE IF NOT EXISTS eval_results (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    model TEXT,
    score INTEGER,
    passed INTEGER,
    seconds REAL,
    cost REAL,
    error TEXT,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_run ON eval_results(run_id);

-- Google / Microsoft sign-ins linked to an account
CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,          -- google | microsoft
    subject TEXT NOT NULL,           -- the provider's stable user id ("sub")
    email TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);

-- Courier trips for a booking: out (owner -> renter) and return (renter -> owner)
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    leg TEXT NOT NULL,                 -- out | return
    provider TEXT NOT NULL,            -- uber_direct
    external_id TEXT,                  -- provider's delivery id
    fee_cents INTEGER,                 -- what the provider charges us
    status TEXT NOT NULL,              -- provider status: pending, pickup, pickup_complete, dropoff, delivered, canceled, returned
    tracking_url TEXT,
    live INTEGER NOT NULL DEFAULT 0,   -- 0 = provider test mode
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (booking_id, leg)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_external ON deliveries(external_id);

-- Product telemetry (see telemetry.py)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    user_id INTEGER,
    visitor TEXT,          -- random id from the session cookie, for counting anonymous visitors
    path TEXT,             -- Flask endpoint name, not the raw URL
    value REAL,            -- e.g. AI cost or booking total
    duration REAL,         -- seconds
    props TEXT             -- JSON
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_event_ts ON events(event, ts);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, event, ts);
"""

# (name, neighborhood, lat offset, lng offset, battery platforms)
DEMO_USERS = [
    ('Mike (contractor)', 'Highland', 0.012, -0.018, 'Milwaukee M18,DeWalt 20V MAX'),
    ('Dana', 'Baker', -0.022, 0.004, 'Ryobi ONE+'),
    ('Luis (mechanic)', 'Five Points', 0.009, 0.021, 'Milwaukee M18'),
    ('You (demo renter)', 'Capitol Hill', 0.0, 0.0, 'Ryobi ONE+'),
]

# owner index into DEMO_USERS, then tool fields
DEMO_TOOLS = [
    (0, dict(courier_size='too_big', weight_lbs=160, name='60-Gallon Upright Air Compressor', brand='Campbell Hausfeld', model='DC060500',
             category='Air Tools & Compressors', power_source='Corded (240V)',
             description='Shop compressor, 3.7 HP. Runs framing nailers, impact guns and sprayers all day.',
             included_items='25 ft hose, quick-connect coupler', daily_price=4.5, deposit=300,
             replacement_value=900, risk_tier='standard',
             safety_notes='Needs a 240V outlet. Drain tank after use.')),
    (0, dict(courier_size='medium', weight_lbs=9, name='Framing Nailer', brand='Milwaukee', model='2744-20 M18 FUEL',
             category='Power Tools', power_source='Battery', battery_platform='Milwaukee M18',
             description='Cordless 21-degree framing nailer. Bare tool, bring your own M18 battery or add one.',
             included_items='Bare tool only', daily_price=3, deposit=250, replacement_value=450,
             risk_tier='waiver', safety_notes='Eye protection required. Never disable the contact trip.')),
    (0, dict(courier_size='xlarge', weight_lbs=70, name='Wet Tile Saw, 10"', brand='DeWalt', model='D36000',
             category='Masonry & Concrete', power_source='Corded (120V)',
             description='Sliding table tile saw, cuts up to 24" tile.', included_items='Stand, blade, water tray',
             daily_price=4, deposit=300, replacement_value=1000, risk_tier='waiver',
             safety_notes='Blade guard must stay on. GFCI outlet only.')),
    (0, dict(courier_size='small', weight_lbs=3, name='M18 5.0Ah Battery (x2)', brand='Milwaukee', model='48-11-1850',
             category='Batteries & Chargers', power_source='Battery', battery_platform='Milwaukee M18',
             description='Two charged M18 XC5.0 packs. Add-on for bare M18 tools.', included_items='2 batteries',
             daily_price=1, deposit=150, replacement_value=250, risk_tier='standard',
             safety_notes='Do not use a pack that is swollen or cracked.')),
    (1, dict(courier_size='too_big', weight_lbs=70, name='Gas Pressure Washer, 3300 PSI', brand='Simpson', model='MegaShot MSH3125',
             category='Outdoor & Yard', power_source='Gas',
             description='Cleans driveways, decks and siding. Honda engine, starts first pull.',
             included_items='25 ft hose, 5 nozzle tips, soap tip', daily_price=3.5, deposit=200,
             replacement_value=450, risk_tier='waiver',
             safety_notes='Never point at people or pets. 0-degree tip can cut skin.')),
    (1, dict(courier_size='medium', weight_lbs=8, name='String Trimmer', brand='Ryobi', model='P20100 ONE+ HP',
             category='Outdoor & Yard', power_source='Battery', battery_platform='Ryobi ONE+',
             description='18V brushless trimmer. Bare tool, fits any Ryobi ONE+ battery.',
             included_items='Bare tool, spare spool', daily_price=1, deposit=75, replacement_value=140,
             risk_tier='standard', safety_notes='Wear eye protection.')),
    (1, dict(courier_size='too_big', weight_lbs=90, name='Drywall Panel Lift', brand='Pentagon Tool', model='Professional 11ft',
             category='Specialty', power_source='Manual',
             description='Holds 4x12 sheets on ceilings up to 11 ft. One person can hang ceilings.',
             included_items='Extension, cradle', daily_price=2.5, deposit=150, replacement_value=260,
             risk_tier='standard', safety_notes='Lock the wheels before cranking up.')),
    (2, dict(courier_size='medium', weight_lbs=18, name='Coil Spring Compressor Kit', brand='OTC', model='6494',
             category='Automotive', power_source='Manual',
             description='Strut spring compressor for MacPherson struts. Heavy duty, clamshell style.',
             included_items='Compressor, jaws for 3 spring sizes', daily_price=1.5, deposit=120,
             replacement_value=250, risk_tier='waiver',
             safety_notes='Compressed springs store dangerous energy. Follow the included steps exactly.')),
    (2, dict(courier_size='too_big', weight_lbs=120, name='2-Ton Folding Engine Hoist', brand='Pittsburgh', model='69514',
             category='Automotive', power_source='Manual',
             description='Cherry picker for engine and transmission pulls. Folds for transport.',
             included_items='Hoist, load leveler', daily_price=3, deposit=200, replacement_value=320,
             risk_tier='waiver', safety_notes='Never get under a suspended load.')),
    (2, dict(courier_size='small', weight_lbs=7, name='1/2" High-Torque Impact Wrench', brand='Milwaukee', model='2767-20 M18 FUEL',
             category='Power Tools', power_source='Battery', battery_platform='Milwaukee M18',
             description='1,400 ft-lb breakaway torque. Takes off seized lug nuts and axle nuts.',
             included_items='Bare tool, impact socket set', daily_price=2, deposit=200,
             replacement_value=400, risk_tier='standard', safety_notes='Use impact-rated sockets only.')),
    (3, dict(courier_size='too_big', weight_lbs=95, name='Electric Concrete Mixer, 4 cu ft', brand='Kushlan', model='450DD',
             category='Masonry & Concrete', power_source='Corded (120V)',
             description='Direct-drive mixer, mixes two 80 lb bags at a time.', included_items='Mixer',
             daily_price=3.5, deposit=200, replacement_value=600, risk_tier='standard',
             safety_notes='Keep hands out of the drum while running.')),
]


def get_db_path():
    return os.getenv('TOOLS_DB_PATH', DEFAULT_DB_PATH)


def get_connection(db_path=None):
    """Open a connection with dict-like rows and foreign keys on"""
    path = db_path or get_db_path()
    if path != ':memory:':
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


# Stored in SQLite's PRAGMA user_version; each migration below brings a database up one step
SCHEMA_VERSION = 5


def init_database(conn, seed=None):
    """Create tables, seed demo data if the database is empty, and run pending migrations"""
    if seed is None:
        seed = os.getenv('SEED_DEMO', '1') == '1'
    conn.executescript(SCHEMA)
    if seed and conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        seed_demo_data(conn)
        conn.execute('PRAGMA user_version = 1')   # seed prices are already cut; later migrations still run
    migrate(conn)
    conn.commit()


def migrate(conn):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version < 1:
        # v1: daily prices cut ~90% (now ~1% of replacement value), nearest $0.50, $1 minimum
        conn.execute('UPDATE tools SET daily_price = MAX(1.0, ROUND(daily_price * 0.1 * 2) / 2.0)')
        conn.execute('PRAGMA user_version = 1')
        version = 1
    if version < 2:
        # v2: real accounts. Everyone who existed before accounts is a demo user (no password).
        for column, decl in [('email', 'TEXT'), ('password_hash', 'TEXT'), ('bio', "TEXT NOT NULL DEFAULT ''"),
                             ('avatar_path', 'TEXT'), ('is_demo', 'INTEGER NOT NULL DEFAULT 0'),
                             ('created_at', 'TEXT'), ('last_login_at', 'TEXT')]:
            conn.execute(f'ALTER TABLE users ADD COLUMN {column} {decl}')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE)')
        conn.execute("UPDATE users SET is_demo = 1, created_at = COALESCE(created_at, datetime('now'))")
        conn.execute('ALTER TABLE tools ADD COLUMN published_at TEXT')
        conn.execute("UPDATE tools SET published_at = created_at WHERE status = 'listed'")
        conn.execute('PRAGMA user_version = 2')
        version = 2
    if version < 3:
        # v3: each listing can have its own pickup spot (else the owner's profile location is used)
        conn.execute('ALTER TABLE tools ADD COLUMN pickup_lat REAL')
        conn.execute('ALTER TABLE tools ADD COLUMN pickup_lng REAL')
        conn.execute('PRAGMA user_version = 3')
        version = 3
    if version < 4:
        # v4: real places (ZIP -> city/state), private courier details, courier sizing, delivery legs
        for table, column, decl in [
            ('users', 'postal_code', 'TEXT'), ('users', 'city', 'TEXT'), ('users', 'region', 'TEXT'),
            ('users', 'street_address', 'TEXT'), ('users', 'phone', 'TEXT'),           # private, couriers only
            ('users', 'offers_delivery', 'INTEGER NOT NULL DEFAULT 1'),
            ('tools', 'courier_size', 'TEXT'), ('tools', 'weight_lbs', 'REAL'),
            ('bookings', 'delivery_method', "TEXT NOT NULL DEFAULT 'pickup'"),       # pickup | owner | courier
            ('bookings', 'dropoff_street', 'TEXT'), ('bookings', 'dropoff_city', 'TEXT'),
            ('bookings', 'dropoff_region', 'TEXT'), ('bookings', 'dropoff_postal', 'TEXT'),
            ('bookings', 'dropoff_lat', 'REAL'), ('bookings', 'dropoff_lng', 'REAL'),
            ('bookings', 'renter_phone', 'TEXT'),
        ]:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
        conn.execute("UPDATE bookings SET delivery_method = CASE WHEN delivery = 1 THEN 'owner' ELSE 'pickup' END")
        for tid, sizing in _deferred_sizing:
            conn.execute('UPDATE tools SET courier_size = ?, weight_lbs = ? WHERE id = ?',
                         (sizing['courier_size'], sizing['weight_lbs'], tid))
        _deferred_sizing.clear()
        conn.execute('PRAGMA user_version = 4')
        version = 4
    if version < 5:
        # v5: search indexes (R*Tree for location, FTS5 for text) and AI-written search keywords
        conn.execute("ALTER TABLE tools ADD COLUMN search_keywords TEXT NOT NULL DEFAULT ''")
        for name, keywords in SEED_KEYWORDS.items():
            conn.execute('UPDATE tools SET search_keywords = ? WHERE name = ? AND owner_id IN '
                         '(SELECT id FROM users WHERE is_demo = 1)', (keywords, name))
        search.install(conn)
        conn.execute('PRAGMA user_version = 5')


# Alternate names and uses for the seeded tools (new listings get these from the AI)
SEED_KEYWORDS = {
    '60-Gallon Upright Air Compressor': 'shop compressor, air tank, inflate, spray painting, nail gun air',
    'Framing Nailer': 'nail gun, framer, deck, fence, stud wall',
    'Wet Tile Saw, 10"': 'tile cutter, porcelain, ceramic, backsplash, bathroom remodel',
    'M18 5.0Ah Battery (x2)': 'battery pack, red lithium, spare battery',
    'Gas Pressure Washer, 3300 PSI': 'power washer, jet wash, driveway, deck cleaning, siding',
    'String Trimmer': 'weed eater, weed whacker, weedwacker, edger, line trimmer',
    'Drywall Panel Lift': 'sheetrock lift, drywall hoist, ceiling panel, plasterboard',
    'Coil Spring Compressor Kit': 'strut spring compressor, macpherson strut, suspension, shocks',
    '2-Ton Folding Engine Hoist': 'cherry picker, engine crane, shop crane, engine lift, motor hoist',
    '1/2" High-Torque Impact Wrench': 'impact gun, lug nuts, breaker, stuck bolts, tire change',
    'Electric Concrete Mixer, 4 cu ft': 'cement mixer, mortar mixer, fence posts, concrete pad',
}

_deferred_sizing = []   # courier sizes for seeded tools, applied once migration v4 adds the columns


def seed_demo_data(conn):
    """Insert demo neighbors and their listed tools around DEMO_CENTER"""
    now = datetime.now().isoformat(timespec='seconds')
    center_lat, center_lng = DEMO_CENTER
    user_ids = []
    for name, hood, dlat, dlng, platforms in DEMO_USERS:
        cur = conn.execute(
            'INSERT INTO users (name, neighborhood, lat, lng, battery_platforms) VALUES (?, ?, ?, ?, ?)',
            (name, hood, center_lat + dlat, center_lng + dlng, platforms)
        )
        user_ids.append(cur.lastrowid)

    for owner_idx, tool in DEMO_TOOLS:
        tool = dict(tool)
        sizing = {k: tool.pop(k) for k in ('courier_size', 'weight_lbs')}
        tid = insert_tool(conn, owner_id=user_ids[owner_idx], status='listed', created_at=now, **tool)
        _deferred_sizing.append((tid, sizing))


TOOL_COLUMNS = [
    'name', 'brand', 'model', 'category', 'power_source', 'battery_platform', 'description',
    'included_items', 'daily_price', 'deposit', 'replacement_value', 'risk_tier', 'safety_notes',
    'photo_path', 'ai_confidence', 'batch_id', 'status', 'created_at', 'published_at',
    'pickup_lat', 'pickup_lng', 'courier_size', 'weight_lbs', 'search_keywords',
]


def insert_tool(conn, owner_id, **fields):
    """Insert a tool row; unknown keys are ignored, missing ones use column defaults"""
    fields.setdefault('created_at', datetime.now().isoformat(timespec='seconds'))
    cols = [c for c in TOOL_COLUMNS if c in fields]
    placeholders = ', '.join('?' for _ in cols)
    cur = conn.execute(
        f"INSERT INTO tools (owner_id, {', '.join(cols)}) VALUES (?, {placeholders})",
        [owner_id] + [fields[c] for c in cols]
    )
    return cur.lastrowid


if __name__ == '__main__':
    connection = get_connection()
    init_database(connection)
    counts = {t: connection.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('users', 'tools', 'bookings')}
    print(f'Database ready at {get_db_path()}: {counts}')
