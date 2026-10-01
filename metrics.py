"""
Numbers for the admin dashboard. Everything is computed from the main tables
(users, tools, bookings) and the events table on each page load; at this
scale that's a few milliseconds.
"""

import json
from datetime import date, datetime, timedelta


def _since(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')


def _one(db, sql, params=()):
    row = db.execute(sql, params).fetchone()
    return (row[0] if row and row[0] is not None else 0)


def kpis(db):
    d7, d30 = _since(7), _since(30)
    real = 'is_demo = 0'
    live_bookings = "status IN ('accepted', 'returned')"
    return {
        'users': _one(db, f'SELECT COUNT(*) FROM users WHERE {real}'),
        'users_7d': _one(db, f'SELECT COUNT(*) FROM users WHERE {real} AND created_at >= ?', (d7,)),
        'active_users_7d': _one(db, 'SELECT COUNT(DISTINCT user_id) FROM events WHERE user_id IS NOT NULL AND ts >= ?', (d7,)),
        'visitors_7d': _one(db, "SELECT COUNT(DISTINCT visitor) FROM events WHERE event = 'page_view' AND ts >= ?", (d7,)),
        'page_views_7d': _one(db, "SELECT COUNT(*) FROM events WHERE event = 'page_view' AND ts >= ?", (d7,)),

        'listings_live': _one(db, f"SELECT COUNT(*) FROM tools t JOIN users u ON t.owner_id = u.id WHERE t.status = 'listed' AND u.{real}"),
        'listings_live_demo': _one(db, "SELECT COUNT(*) FROM tools t JOIN users u ON t.owner_id = u.id WHERE t.status = 'listed' AND u.is_demo = 1"),
        'posts_7d': _one(db, f"SELECT COUNT(*) FROM tools t JOIN users u ON t.owner_id = u.id WHERE u.{real} AND t.published_at >= ?", (d7,)),
        'posts_30d': _one(db, f"SELECT COUNT(*) FROM tools t JOIN users u ON t.owner_id = u.id WHERE u.{real} AND t.published_at >= ?", (d30,)),
        'drafts_pending': _one(db, "SELECT COUNT(*) FROM tools WHERE status = 'draft'"),
        'owners': _one(db, f"SELECT COUNT(DISTINCT t.owner_id) FROM tools t JOIN users u ON t.owner_id = u.id WHERE t.status = 'listed' AND u.{real}"),

        'bookings': _one(db, 'SELECT COUNT(*) FROM bookings'),
        'bookings_7d': _one(db, 'SELECT COUNT(*) FROM bookings WHERE created_at >= ?', (d7,)),
        'bookings_accepted': _one(db, f'SELECT COUNT(*) FROM bookings WHERE {live_bookings}'),
        'gmv': round(_one(db, f'SELECT SUM(total_charge) FROM bookings WHERE {live_bookings}'), 2),
        'platform_revenue': round(_one(db, f'SELECT SUM(total_charge - owner_payout) FROM bookings WHERE {live_bookings}'), 2),
        'delivery_share': _one(db, f'SELECT ROUND(100.0 * SUM(delivery) / COUNT(*)) FROM bookings WHERE {live_bookings}'),

        'ai_calls_7d': _one(db, "SELECT COUNT(*) FROM events WHERE event = 'photo_analyzed' AND ts >= ?", (d7,)),
        'ai_cost_7d': round(_one(db, "SELECT SUM(value) FROM events WHERE event IN ('photo_analyzed', 'eval_case') AND ts >= ?", (d7,)), 2),
        'ai_cost_30d': round(_one(db, "SELECT SUM(value) FROM events WHERE event IN ('photo_analyzed', 'eval_case') AND ts >= ?", (d30,)), 2),
        'ai_avg_seconds_7d': round(_one(db, "SELECT AVG(duration) FROM events WHERE event = 'photo_analyzed' AND ts >= ?", (d7,)), 1),
        'upload_limited_7d': _one(db, "SELECT COUNT(*) FROM events WHERE event = 'upload_limited' AND ts >= ?", (d7,)),

        'courier_trips_30d': _one(db, 'SELECT COUNT(*) FROM deliveries WHERE live = 1 AND created_at >= ?', (d30,)),
        'courier_test_trips_30d': _one(db, 'SELECT COUNT(*) FROM deliveries WHERE live = 0 AND created_at >= ?', (d30,)),
        'courier_cost_30d': round(_one(db, "SELECT SUM(fee_cents) FROM deliveries WHERE live = 1 AND status != 'canceled' "
                                           "AND created_at >= ?", (d30,)) / 100, 2),
        'courier_bookings': _one(db, f"SELECT COUNT(*) FROM bookings WHERE delivery_method = 'courier' AND {live_bookings}"),
    }


DAILY_SERIES = {
    'signups': ("SELECT substr(created_at, 1, 10) AS day, COUNT(*) FROM users WHERE is_demo = 0 AND created_at >= ? GROUP BY day", None),
    'posts': ("SELECT substr(t.published_at, 1, 10) AS day, COUNT(*) FROM tools t JOIN users u ON t.owner_id = u.id "
              "WHERE u.is_demo = 0 AND t.published_at >= ? GROUP BY day", None),
    'bookings': ("SELECT substr(created_at, 1, 10) AS day, COUNT(*) FROM bookings WHERE created_at >= ? GROUP BY day", None),
    'page_views': ("SELECT substr(ts, 1, 10) AS day, COUNT(*) FROM events WHERE event = 'page_view' AND ts >= ? GROUP BY day", None),
    'ai_cost': ("SELECT substr(ts, 1, 10) AS day, SUM(value) FROM events WHERE event IN ('photo_analyzed', 'eval_case') AND ts >= ? GROUP BY day", 2),
}


def daily(db, days=30):
    """{series: [(iso_day, value), ...]} with zero-filled days, oldest first"""
    start = date.today() - timedelta(days=days - 1)
    all_days = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    out = {}
    for name, (sql, digits) in DAILY_SERIES.items():
        rows = dict(db.execute(sql, (start.isoformat(),)).fetchall())
        out[name] = [(d, round(rows.get(d) or 0, digits) if digits else int(rows.get(d) or 0)) for d in all_days]
    return out


def ai_by_model(db, days=30):
    rows = db.execute(
        """SELECT props, value, duration FROM events WHERE event = 'photo_analyzed' AND ts >= ?""", (_since(days),)
    ).fetchall()
    stats = {}
    for r in rows:
        p = json.loads(r['props'] or '{}')
        key = (p.get('model') or 'unknown', p.get('source') or 'photo')
        s = stats.setdefault(key, {'model': key[0], 'source': key[1], 'calls': 0, 'cost': 0.0, 'seconds': 0.0, 'drafts': 0})
        s['calls'] += 1
        s['cost'] += r['value'] or 0
        s['seconds'] += r['duration'] or 0
        s['drafts'] += p.get('drafts') or 0
    out = []
    for s in stats.values():
        s['avg_seconds'] = round(s['seconds'] / s['calls'], 1)
        s['cost'] = round(s['cost'], 4)
        s['escalated'] = '>' in s['model']
        out.append(s)
    return sorted(out, key=lambda s: s['calls'], reverse=True)


def categories(db):
    return db.execute(
        """SELECT t.category, COUNT(*) AS n, ROUND(AVG(t.daily_price), 2) AS avg_price
           FROM tools t JOIN users u ON t.owner_id = u.id
           WHERE t.status = 'listed' AND u.is_demo = 0 GROUP BY t.category ORDER BY n DESC"""
    ).fetchall()


def funnel(db, days=30):
    """Visitors -> signed up -> uploaded a photo -> posted a listing -> got/made a booking"""
    since = _since(days)
    return [
        ('visited', _one(db, "SELECT COUNT(DISTINCT visitor) FROM events WHERE event = 'page_view' AND ts >= ?", (since,))),
        ('signed up', _one(db, "SELECT COUNT(*) FROM users WHERE is_demo = 0 AND created_at >= ?", (since,))),
        ('uploaded a photo', _one(db, "SELECT COUNT(DISTINCT user_id) FROM events WHERE event = 'photo_analyzed' AND ts >= ?", (since,))),
        ('posted a listing', _one(db, "SELECT COUNT(DISTINCT t.owner_id) FROM tools t JOIN users u ON t.owner_id = u.id "
                                      "WHERE u.is_demo = 0 AND t.published_at >= ?", (since,))),
        ('requested a rental', _one(db, "SELECT COUNT(DISTINCT renter_id) FROM bookings WHERE created_at >= ?", (since,))),
    ]


def top_pages(db, days=7, limit=10):
    return db.execute(
        """SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors FROM events
           WHERE event = 'page_view' AND ts >= ? GROUP BY path ORDER BY views DESC LIMIT ?""", (_since(days), limit)
    ).fetchall()


def recent_users(db, limit=25):
    return db.execute(
        """SELECT u.id, u.name, u.email, u.neighborhood, u.created_at, u.last_login_at,
                  (SELECT COUNT(*) FROM tools t WHERE t.owner_id = u.id AND t.status = 'listed') AS listings,
                  (SELECT COUNT(*) FROM bookings b WHERE b.renter_id = u.id) AS rentals,
                  TRIM((CASE WHEN u.password_hash IS NOT NULL THEN 'email ' ELSE '' END) ||
                       COALESCE((SELECT GROUP_CONCAT(provider, ' ') FROM identities i WHERE i.user_id = u.id), '')) AS methods
           FROM users u WHERE u.is_demo = 0 ORDER BY u.created_at DESC LIMIT ?""", (limit,)
    ).fetchall()


def recent_events(db, limit=40):
    return db.execute(
        """SELECT e.ts, e.event, e.path, e.value, e.duration, e.props, u.name AS user_name
           FROM events e LEFT JOIN users u ON e.user_id = u.id
           WHERE e.event != 'page_view' ORDER BY e.id DESC LIMIT ?""", (limit,)
    ).fetchall()


def collect(db):
    return {
        'kpis': kpis(db), 'daily': daily(db), 'ai_by_model': ai_by_model(db), 'categories': categories(db),
        'funnel': funnel(db), 'top_pages': top_pages(db), 'recent_users': recent_users(db),
        'recent_events': recent_events(db),
    }


# ---------------------------------------------------------------------------
# Chart geometry (server-side SVG, no chart library)
# ---------------------------------------------------------------------------

def nice_max(value):
    """Round an axis maximum up to 1, 2, 2.5 or 5 x 10^n"""
    import math
    if value <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(value))
    for m in (1, 2, 2.5, 5, 10):
        if m * exp >= value:
            return m * exp
    return 10 * exp


def column_chart(points, width=360, height=150, money=False):
    """
    Geometry for a single-series daily column chart.
    Columns are at most 24px wide with a 4px rounded top and a square baseline;
    each day also gets a full-height invisible hit area for the hover tooltip.
    """
    pad_left, pad_right, pad_top, pad_bottom = 40, 4, 10, 22
    plot_w, plot_h = width - pad_left - pad_right, height - pad_top - pad_bottom
    base_y = pad_top + plot_h
    top = nice_max(max((v for _, v in points), default=0))
    slot = plot_w / max(1, len(points))
    bar_w = min(24, max(2, slot - 2))       # 2px of surface between neighboring columns
    radius = min(4, bar_w / 2)

    def fmt(v):
        if money:
            return f'${v:,.2f}'
        return f'{v:,.0f}' if float(v).is_integer() else f'{v:,.1f}'

    bars = []
    for i, (day, v) in enumerate(points):
        slot_x = pad_left + i * slot
        x = slot_x + (slot - bar_w) / 2
        h = plot_h * v / top if top else 0
        y = base_y - h
        if h <= 0:
            path = ''
        elif h < radius:
            path = f'M{x:.1f},{base_y:.1f}V{y:.1f}H{x + bar_w:.1f}V{base_y:.1f}Z'
        else:
            path = (f'M{x:.1f},{base_y:.1f}V{y + radius:.1f}Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f}'
                    f'H{x + bar_w - radius:.1f}Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + radius:.1f}'
                    f'V{base_y:.1f}Z')
        bars.append({'path': path, 'hit_x': round(slot_x, 1), 'hit_w': round(slot, 1), 'day': day,
                     'label': f"{date.fromisoformat(day).strftime('%b %-d')}: {fmt(v)}"})

    ticks = [{'y': round(base_y - plot_h * f, 1), 'label': fmt(top * f)}
             for f in (0, 0.5, 1)]
    if not money and top < 2:
        ticks = [t for t in ticks if t['label'] in ('0', '1')]   # no "0.5 signups"
    label_idx = sorted({0, len(points) // 2, len(points) - 1})
    xlabels = [{'x': round(pad_left + i * slot + slot / 2, 1),
                'label': date.fromisoformat(points[i][0]).strftime('%b %-d')} for i in label_idx] if points else []
    return {'width': width, 'height': height, 'base_y': base_y, 'left': pad_left, 'right': width - pad_right,
            'bars': bars, 'ticks': ticks, 'xlabels': xlabels,
            'total': fmt(sum(v for _, v in points)), 'peak': fmt(max((v for _, v in points), default=0)),
            'rows': [(date.fromisoformat(d).strftime('%b %-d'), fmt(v)) for d, v in points]}
