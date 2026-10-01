"""
Turn ZIP codes and street addresses into coordinates and a city/state, with Azure Maps.

On Azure the app signs in with its managed identity ("Azure Maps Data Reader" role) and
names the Maps account with AZURE_MAPS_CLIENT_ID. AZURE_MAPS_KEY also works, for local
testing. Without either, lookups return None and callers fall back gracefully.

Results for ZIP codes are cached in memory: there are only ~42,000 US ZIPs and they don't move.
"""

import os
import re
import time

import requests

MAPS_URL = 'https://atlas.microsoft.com'
API_VERSION = '2026-01-01'
ZIP_RE = re.compile(r'^\d{5}$')

_zip_cache = {}
_token = {'value': None, 'expires': 0}


def configured() -> bool:
    return bool(os.getenv('AZURE_MAPS_KEY') or os.getenv('AZURE_MAPS_CLIENT_ID'))


def _headers():
    key = os.getenv('AZURE_MAPS_KEY')
    if key:
        return {'subscription-key': key}
    if _token['value'] is None or _token['expires'] < time.time() + 120:
        from azure.identity import DefaultAzureCredential
        tok = DefaultAzureCredential(exclude_interactive_browser_credential=True).get_token(
            'https://atlas.microsoft.com/.default')
        _token.update(value=tok.token, expires=tok.expires_on)
    return {'Authorization': f"Bearer {_token['value']}", 'x-ms-client-id': os.getenv('AZURE_MAPS_CLIENT_ID', '')}


def _first_place(params):
    """Call the geocoder; return {lat, lng, city, region, postal_code, formatted, confidence} or None"""
    res = requests.get(f'{MAPS_URL}/geocode', params={'api-version': API_VERSION, 'top': 1, **params},
                       headers=_headers(), timeout=10)
    res.raise_for_status()
    features = res.json().get('features') or []
    if not features:
        return None
    f = features[0]
    lng, lat = f['geometry']['coordinates'][:2]
    addr = f['properties'].get('address', {})
    districts = addr.get('adminDistricts') or [{}]
    return {
        'lat': round(lat, 5), 'lng': round(lng, 5),
        'city': addr.get('locality') or '',
        'region': districts[0].get('shortName') or districts[0].get('name') or '',
        'postal_code': addr.get('postalCode') or '',
        'formatted': addr.get('formattedAddress') or '',
        'confidence': f['properties'].get('confidence'),
    }


def lookup_zip(postal_code: str):
    """US ZIP -> place (center of the ZIP). None when not found or Maps isn't configured."""
    postal_code = (postal_code or '').strip()[:5]
    if not ZIP_RE.match(postal_code) or not configured():
        return None
    if postal_code not in _zip_cache:
        try:
            place = _first_place({'postalCode': postal_code, 'countryRegion': 'US'})
        except Exception as e:
            print(f'ZIP lookup failed for {postal_code}: {e}')
            return None
        if place and place['postal_code'] and place['postal_code'] != postal_code:
            place = None       # the geocoder fell back to something else; treat as not found
        _zip_cache[postal_code] = place
    return _zip_cache[postal_code]


def lookup_address(street: str, city: str, region: str, postal_code: str):
    """Street address -> place, for courier drop-offs. None when not found or Maps isn't configured."""
    if not configured() or not street:
        return None
    try:
        return _first_place({'addressLine': street, 'locality': city, 'adminDistrict': region,
                             'postalCode': postal_code, 'countryRegion': 'US'})
    except Exception as e:
        print(f'Address lookup failed: {e}')
        return None


def describe(neighborhood: str, city: str, region: str) -> str:
    """"Capitol Hill · Seattle, WA" (or whatever parts exist)"""
    place = ', '.join(p for p in (city, region) if p)
    return ' · '.join(p for p in (neighborhood, place) if p)
