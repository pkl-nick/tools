"""
Tool search: "what's near me" first, then "what matches".

Two indexes built into SQLite, kept current by triggers (no app code has to remember to update them):

    tools_geo   R*Tree of listed tools' pickup points. A search reads only the tools inside a box around
                the viewer, so cost depends on how many tools are nearby, not on how many exist nationwide.
    tools_fts   FTS5 full-text index of name, brand, model, keywords, battery system, category and
                description, with English stemming ("drills" finds "drill") and prefix matching.

Distances and ordering are done in SQL, so only one page of rows is ever loaded into Python.
If this SQLite build lacks R*Tree or FTS5, a plain table with a B-tree index and LIKE matching are
used instead: same results, slower at scale.
"""

import math
import re
import sqlite3

MILES_PER_DEG_LAT = 69.0
PER_PAGE = 25

# Relevance weights per FTS column, in the order the columns are declared below
FTS_COLUMNS = ['name', 'brand', 'model', 'search_keywords', 'battery_platform', 'category', 'description']
FTS_WEIGHTS = [10.0, 4.0, 4.0, 3.0, 2.0, 2.0, 1.0]
# MATERIALIZED (SQLite 3.35+) makes the match list a one-time table instead of a per-row subquery
_MATERIALIZED = 'MATERIALIZED ' if sqlite3.sqlite_version_info >= (3, 35) else ''
_TOKEN = re.compile(r'\w+', re.UNICODE)
FILLER_WORDS = {'a', 'an', 'and', 'the', 'for', 'to', 'of', 'with', 'my', 'me', 'no', 'need', 'tool', 'tools',
                'rent', 'rental', 'borrow', 'near', 'cheap'}


def has_module(conn, sql):
    try:
        conn.execute(sql)
        return True
    except Exception:
        return False


def install(conn):
    """Create the indexes and their triggers, and fill them from existing rows. Safe to re-run."""
    rtree = has_module(conn, 'CREATE VIRTUAL TABLE IF NOT EXISTS tools_geo '
                             'USING rtree(id, min_lat, max_lat, min_lng, max_lng)')
    if not rtree:
        # Same columns as the R*Tree, so queries don't change; only the index differs
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS tools_geo (id INTEGER PRIMARY KEY, min_lat REAL, max_lat REAL,
                                                  min_lng REAL, max_lng REAL);
            CREATE INDEX IF NOT EXISTS idx_tools_geo_lat ON tools_geo(min_lat, min_lng);
        ''')
    fts = has_module(conn, f'''CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
        {', '.join(FTS_COLUMNS)}, content='tools', content_rowid='id', tokenize='porter unicode61',
        prefix='2 3')''')

    # Pickup point = the listing's own spot, else the owner's location. Only listed tools are indexed.
    point = 'COALESCE(t.pickup_lat, u.lat), COALESCE(t.pickup_lat, u.lat), COALESCE(t.pickup_lng, u.lng), COALESCE(t.pickup_lng, u.lng)'
    conn.executescript(f'''
        CREATE TRIGGER IF NOT EXISTS tools_geo_insert AFTER INSERT ON tools WHEN new.status = 'listed' BEGIN
            INSERT INTO tools_geo (id, min_lat, max_lat, min_lng, max_lng)
            SELECT t.id, {point} FROM tools t JOIN users u ON u.id = t.owner_id WHERE t.id = new.id;
        END;
        CREATE TRIGGER IF NOT EXISTS tools_geo_update
        AFTER UPDATE OF status, pickup_lat, pickup_lng, owner_id ON tools BEGIN
            DELETE FROM tools_geo WHERE id = old.id;
            INSERT INTO tools_geo (id, min_lat, max_lat, min_lng, max_lng)
            SELECT t.id, {point} FROM tools t JOIN users u ON u.id = t.owner_id
            WHERE t.id = new.id AND t.status = 'listed';
        END;
        CREATE TRIGGER IF NOT EXISTS tools_geo_delete AFTER DELETE ON tools BEGIN
            DELETE FROM tools_geo WHERE id = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS users_geo_update AFTER UPDATE OF lat, lng ON users BEGIN
            DELETE FROM tools_geo WHERE id IN (SELECT id FROM tools WHERE owner_id = new.id AND pickup_lat IS NULL);
            INSERT INTO tools_geo (id, min_lat, max_lat, min_lng, max_lng)
            SELECT id, new.lat, new.lat, new.lng, new.lng FROM tools
            WHERE owner_id = new.id AND pickup_lat IS NULL AND status = 'listed';
        END;
    ''')
    conn.execute('DELETE FROM tools_geo')
    conn.execute(f'''INSERT INTO tools_geo (id, min_lat, max_lat, min_lng, max_lng)
                     SELECT t.id, {point} FROM tools t JOIN users u ON u.id = t.owner_id
                     WHERE t.status = 'listed' ''')

    if fts:
        # External-content FTS: the text lives once, in tools; the index stores only tokens.
        # The 'delete' command must be given the old values, which triggers have.
        cols = ', '.join(FTS_COLUMNS)
        new_vals = ', '.join(f"COALESCE(new.{c}, '')" for c in FTS_COLUMNS)
        old_vals = ', '.join(f"COALESCE(old.{c}, '')" for c in FTS_COLUMNS)
        conn.executescript(f'''
            CREATE TRIGGER IF NOT EXISTS tools_fts_insert AFTER INSERT ON tools BEGIN
                INSERT INTO tools_fts (rowid, {cols}) VALUES (new.id, {new_vals});
            END;
            CREATE TRIGGER IF NOT EXISTS tools_fts_delete AFTER DELETE ON tools BEGIN
                INSERT INTO tools_fts (tools_fts, rowid, {cols}) VALUES ('delete', old.id, {old_vals});
            END;
            CREATE TRIGGER IF NOT EXISTS tools_fts_update AFTER UPDATE OF {cols} ON tools BEGIN
                INSERT INTO tools_fts (tools_fts, rowid, {cols}) VALUES ('delete', old.id, {old_vals});
                INSERT INTO tools_fts (rowid, {cols}) VALUES (new.id, {new_vals});
            END;
        ''')
        # Re-index every row from the tools table (NULL and '' produce the same, empty, token list)
        conn.execute("INSERT INTO tools_fts (tools_fts) VALUES ('rebuild')")
    return {'rtree': rtree, 'fts': fts}


def has_fts(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tools_fts'").fetchone() is not None


def fts_query(text, any_word=False):
    """User text -> safe FTS5 query: each word quoted (no operators get through), prefix-matched"""
    words = _TOKEN.findall(text.lower())[:8]
    if any_word:
        # On its own, "tool" or "for" would match half the catalog
        words = [w for w in words if w not in FILLER_WORDS and len(w) > 1]
    if not words:
        return None
    return (' OR ' if any_word else ' ').join(f'"{w}"*' for w in words)


def box(lat, lng, miles):
    """Lat/lng bounds of a square around a point; the circle is cut out of it in SQL"""
    dlat = miles / MILES_PER_DEG_LAT
    dlng = miles / (MILES_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def search_tools(conn, lat, lng, miles=10.0, q='', category=None, platforms=None, include_demo=True,
                 sort='closest', page=1, per_page=PER_PAGE):
    """
    Listed tools within `miles` of (lat, lng). Returns {'tools', 'total', 'page', 'pages', 'matched_any'}.
    sort: 'closest' (demo samples after real listings, then distance) or 'best' (text relevance first).
    platforms: None = any; a list = only tools on those battery systems ([] matches nothing).
    """
    lat_lo, lat_hi, lng_lo, lng_hi = box(lat, lng, miles)
    # Equirectangular distance: exact enough within a metro, and plain arithmetic SQLite can do
    kx = MILES_PER_DEG_LAT * math.cos(math.radians(lat))
    dist2 = f'((g.min_lat - {lat!r}) * {MILES_PER_DEG_LAT} * (g.min_lat - {lat!r}) * {MILES_PER_DEG_LAT} + ' \
            f'(g.min_lng - {lng!r}) * {kx!r} * (g.min_lng - {lng!r}) * {kx!r})'

    def candidates(match):
        """(id, distance², is_demo) of every nearby tool that passes the filters. Small: one metro's worth."""
        # Join order is forced with CROSS JOIN: the R*Tree box goes first, so a search touches only the
        # tools near the viewer. Left to itself, SQLite starts from every listed tool in the country.
        cte, params, joins = '', [], 'FROM tools_geo g'
        where = ['g.min_lat >= ? AND g.max_lat <= ? AND g.min_lng >= ? AND g.max_lng <= ?', f'{dist2} <= ?']
        if match is not None and fts:
            # Matching ids are read once from the index (~10 ms for 28k nationwide hits at 500k listings)
            # and joined to the nearby set through an automatic index
            cte = f'WITH m AS {_MATERIALIZED}(SELECT rowid AS id FROM tools_fts WHERE tools_fts MATCH ?) '
            params.append(match)
            joins += ' CROSS JOIN m'
            where.append('m.id = g.id')
        params += [lat_lo, lat_hi, lng_lo, lng_hi, miles * miles]
        joins += ' CROSS JOIN tools t CROSS JOIN users u'
        where += ['t.id = g.id', 'u.id = t.owner_id', "t.status = 'listed'"]
        if match is not None and not fts:
            any_word = ' OR ' in match
            words = [w for w in _TOKEN.findall(q.lower())[:8] if not any_word or w not in FILLER_WORDS]
            where.append('(' + (' OR ' if any_word else ' AND ').join(
                "(t.name || ' ' || t.brand || ' ' || t.model || ' ' || COALESCE(t.search_keywords, '') || ' ' "
                "|| COALESCE(t.battery_platform, '') || ' ' || t.category || ' ' || t.description) LIKE ?"
                for _ in words) + ')')
            params += [f'%{w}%' for w in words]
        if not include_demo:
            where.append('u.is_demo = 0')
        if category:
            where.append('t.category = ?')
            params.append(category)
        if platforms is not None:
            where.append(f"t.battery_platform IN ({','.join('?' * len(platforms)) or 'NULL'})")
            params += list(platforms)
        return conn.execute(f"{cte}SELECT g.id, {dist2}, u.is_demo {joins} WHERE {' AND '.join(where)}",
                            params).fetchall()

    def relevance(ids):
        """
        Score just the nearby matches (a few hundred rows), in Python. The FTS index already decided
        *what* matches; this only orders it. Asking FTS5 for bm25 would mean scoring every nationwide
        match (4x the cost of the search) or re-running the match once per row (far worse).
        Score = sum over query words of column weight x occurrences, words weighted by rarity.
        """
        words = [w for w in _TOKEN.findall(q.lower())[:8] if w not in FILLER_WORDS] or _TOKEN.findall(q.lower())[:8]
        if not words or not ids:
            return {}
        cols = ', '.join(f"COALESCE({c}, '')" for c in FTS_COLUMNS)
        docs = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for row in conn.execute(f"SELECT id, {cols} FROM tools WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                docs[row[0]] = [_TOKEN.findall(str(v).lower()) for v in row[1:]]

        def hits(token_list, w):
            # prefix either way covers plurals and stems: "nailers" ~ "nailer", "compr" ~ "compressor"
            return sum(1 for t in token_list if t.startswith(w) or (len(t) >= 4 and w.startswith(t)))

        df = {w: sum(1 for d in docs.values() if any(hits(col, w) for col in d)) for w in words}
        idf = {w: math.log(1 + len(docs) / (1 + df[w])) for w in words}
        return {i: -sum(idf[w] * weight * min(hits(col, w), 3)          # negative: lower sorts first
                        for w in words for weight, col in zip(FTS_WEIGHTS, d))
                for i, d in docs.items()}

    fts = has_fts(conn)
    page = max(1, int(page))
    matched_any = False
    match = fts_query(q) if q else None
    found = candidates(match)
    if q and not found and match and ' ' in match:
        # Nothing has every word ("milwaukee cordless impact wrench"): show tools with any of them
        loose = fts_query(q, any_word=True)
        if loose:
            match = loose
            found = candidates(match)
            matched_any = bool(found)

    if sort == 'best' and match:
        score = relevance([r[0] for r in found])
        found.sort(key=lambda r: (score.get(r[0], 0), r[2], r[1]))
    else:
        found.sort(key=lambda r: (r[2], r[1]))           # real listings before samples, then nearest
    total = len(found)
    page_ids = [r[0] for r in found[(page - 1) * per_page:page * per_page]]
    by_id = {}
    if page_ids:
        for r in conn.execute(
                f'''SELECT t.*, u.name AS owner_name, u.neighborhood, u.city, u.region, u.is_demo AS owner_is_demo,
                          g.min_lat AS lat, g.min_lng AS lng, sqrt({dist2}) AS miles
                   FROM tools t JOIN users u ON u.id = t.owner_id JOIN tools_geo g ON g.id = t.id
                   WHERE t.id IN ({','.join('?' * len(page_ids))})''', page_ids):
            by_id[r['id']] = r
    rows = [by_id[i] for i in page_ids if i in by_id]
    tools = [{**dict(r), 'miles': round(r['miles'], 1)} for r in rows]
    return {'tools': tools, 'total': total, 'page': page,
            'pages': max(1, math.ceil(total / per_page)), 'matched_any': matched_any}


def describe(conn):
    """For the admin page: which indexes this database is running on"""
    rtree = 'rtree' in (conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tools_geo'").fetchone() or [''])[0].lower()
    return f"search: {'R*Tree' if rtree else 'B-tree (no R*Tree)'} + {'FTS5' if has_fts(conn) else 'LIKE (no FTS5)'}, " \
           f"SQLite {sqlite3.sqlite_version}"
