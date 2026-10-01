"""
Courier delivery through Uber Direct (Uber's delivery-as-a-service API).

    UBER_DIRECT_CUSTOMER_ID / UBER_DIRECT_CLIENT_ID / UBER_DIRECT_CLIENT_SECRET   from the Direct dashboard
    UBER_DIRECT_WEBHOOK_KEY      signing key of the webhook (verifies x-uber-signature)
    COURIER_LIVE=1               allow real couriers. Until payments are built, every live trip is paid
                                 by the platform, so by default only test deliveries are created, and they
                                 are completed by Uber's automated test courier ("Robocourier").
    DELIVERY_MARKUP_PCT / DELIVERY_MARKUP_FLAT    what renters pay on top of Uber's fee

Uber's rules for Direct: items go by car, up to about 20 kg (44 lb) each, and some items are
prohibited (for example vehicle batteries). Tools that don't fit fall back to owner delivery or pickup.
"""

import os
import json
import hmac
import time
import hashlib

import requests

AUTH_URL = 'https://auth.uber.com/oauth/v2/token'
API_URL = 'https://api.uber.com/v1/customers'
MAX_WEIGHT_LBS = 44
COURIER_SIZES = ['small', 'medium', 'large', 'xlarge']      # Uber's manifest sizes
TOO_BIG = 'too_big'                                         # our extra value: needs a truck

# Final states where nothing else will happen to a trip
DONE_STATUSES = {'delivered', 'canceled', 'returned'}

_token = {'value': None, 'expires': 0}


class DeliveryError(Exception):
    pass


def configured() -> bool:
    return all(os.getenv(k) for k in ('UBER_DIRECT_CUSTOMER_ID', 'UBER_DIRECT_CLIENT_ID', 'UBER_DIRECT_CLIENT_SECRET'))


def live_enabled() -> bool:
    return os.getenv('COURIER_LIVE') == '1'


def courier_eligible(courier_size, weight_lbs) -> bool:
    return courier_size in COURIER_SIZES and (weight_lbs is None or weight_lbs <= MAX_WEIGHT_LBS)


def renter_price_cents(uber_fee_cents: int) -> int:
    """Uber's fee plus our markup (delivery is meant to be a real revenue line)"""
    pct = float(os.getenv('DELIVERY_MARKUP_PCT', '15'))
    flat = float(os.getenv('DELIVERY_MARKUP_FLAT', '1.00'))
    return int(round(uber_fee_cents * (1 + pct / 100) + flat * 100))


def _access_token():
    if _token['value'] and _token['expires'] > time.time() + 300:
        return _token['value']
    res = requests.post(AUTH_URL, data={
        'client_id': os.getenv('UBER_DIRECT_CLIENT_ID'), 'client_secret': os.getenv('UBER_DIRECT_CLIENT_SECRET'),
        'grant_type': 'client_credentials', 'scope': 'eats.deliveries',
    }, timeout=15)
    if res.status_code != 200:
        raise DeliveryError(f'Uber auth failed ({res.status_code})')
    data = res.json()
    _token.update(value=data['access_token'], expires=time.time() + int(data.get('expires_in', 3600)))
    return _token['value']


def _post(path, body):
    url = f"{API_URL}/{os.getenv('UBER_DIRECT_CUSTOMER_ID')}/{path}"
    res = requests.post(url, json=body, headers={'Authorization': f'Bearer {_access_token()}'}, timeout=20)
    data = res.json() if res.content else {}
    if res.status_code >= 300:
        raise DeliveryError(data.get('message') or data.get('code') or f'Uber API error {res.status_code}')
    return data


def address_json(street, city, region, postal_code):
    """Uber takes addresses as a JSON-encoded string"""
    return json.dumps({'street_address': [street], 'city': city, 'state': region,
                       'zip_code': postal_code, 'country': 'US'})


def _coords(prefix, place):
    """Exact pins help the courier; Uber geocodes the address itself when they're missing"""
    if place.get('lat') is None or place.get('lng') is None:
        return {}
    return {f'{prefix}_latitude': place['lat'], f'{prefix}_longitude': place['lng']}


def quote(pickup, dropoff):
    """
    pickup / dropoff: dicts with street, city, region, postal_code, lat, lng.
    Returns {quote_id, fee_cents, expires, dropoff_eta, duration_min}.
    """
    data = _post('delivery_quotes', {
        'pickup_address': address_json(pickup['street'], pickup['city'], pickup['region'], pickup['postal_code']),
        'dropoff_address': address_json(dropoff['street'], dropoff['city'], dropoff['region'], dropoff['postal_code']),
        **_coords('pickup', pickup), **_coords('dropoff', dropoff),
    })
    return {'quote_id': data['id'], 'fee_cents': int(data['fee']), 'expires': data.get('expires'),
            'dropoff_eta': data.get('dropoff_eta'), 'duration_min': data.get('duration')}


def create(pickup, dropoff, item, external_id, notes=''):
    """
    Dispatch a courier now. pickup / dropoff also need name and phone (E.164).
    item: {name, courier_size, weight_lbs}. Returns {id, status, tracking_url, fee_cents, live}.
    """
    q = quote(pickup, dropoff)
    body = {
        'quote_id': q['quote_id'],
        'external_id': external_id,
        'pickup_name': pickup['name'], 'pickup_phone_number': pickup['phone'],
        'pickup_address': address_json(pickup['street'], pickup['city'], pickup['region'], pickup['postal_code']),
        **_coords('pickup', pickup),
        'pickup_notes': notes[:280],
        'dropoff_name': dropoff['name'], 'dropoff_phone_number': dropoff['phone'],
        'dropoff_address': address_json(dropoff['street'], dropoff['city'], dropoff['region'], dropoff['postal_code']),
        **_coords('dropoff', dropoff),
        'manifest_items': [{
            'name': item['name'][:100], 'quantity': 1, 'size': item['courier_size'],
            **({'weight': int(item['weight_lbs'] * 453.6)} if item.get('weight_lbs') else {}),   # grams
        }],
    }
    if not live_enabled():
        # Uber's automated test courier completes the trip by itself
        body['test_specifications'] = {'robo_courier_specification': {'mode': 'auto'}}
    data = _post('deliveries', body)
    if data.get('live_mode') and not live_enabled():
        # Production credentials ignore the test courier, so this would be a real, billed trip. Undo it.
        try:
            cancel(data['id'])
        finally:
            raise DeliveryError('These Uber Direct credentials are live. Set COURIER_LIVE=1 to dispatch real couriers.')
    return {'id': data['id'], 'status': data.get('status', 'pending'), 'tracking_url': data.get('tracking_url'),
            'fee_cents': int(data.get('fee', q['fee_cents'])), 'live': bool(data.get('live_mode', False))}


def cancel(delivery_id):
    return _post(f'deliveries/{delivery_id}/cancel', {})


def verify_webhook(raw_body: bytes, signature: str) -> bool:
    """x-uber-signature is the hex HMAC-SHA256 of the raw body with the webhook's signing key"""
    key = os.getenv('UBER_DIRECT_WEBHOOK_KEY', '')
    if not key or not signature:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())
