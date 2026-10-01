"""
Product telemetry: one row per event in the events table.

Events are written in the request's own database connection and committed
with it. Tracking never raises, so a telemetry problem can't break a page.
No IP addresses or user agents are stored. Anonymous visitors are counted
with a random id kept in their session cookie.

Events: page_view, signup, login, login_failed, logout, profile_updated,
avatar_uploaded, password_changed, photo_analyzed, upload_limited,
drafts_reviewed, listing_removed, booking_requested, booking_accepted,
booking_declined, booking_returned, booking_cancelled, eval_case,
backup_created, media_migrated, admin_login, admin_login_failed
"""

import json
from datetime import datetime


def track(db, event, user_id=None, visitor=None, path=None, value=None, duration=None, **props):
    try:
        db.execute(
            '''INSERT INTO events (ts, event, user_id, visitor, path, value, duration, props)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (datetime.now().isoformat(timespec='seconds'), event, user_id, visitor, path,
             value, duration, json.dumps(props, default=str) if props else None)
        )
    except Exception as e:   # never let telemetry break a request
        print(f'telemetry error ({event}): {e}')
