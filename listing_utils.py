"""
Listing Utils - snap-to-list AI, pricing and distance helpers.

analyze_photo() sends a garage photo to a vision model and gets back draft
listings for every tool it can see. With no API credentials configured it runs
in demo mode and returns sample drafts, so the whole flow works offline.
"""

import os
import json
import math
import time
import base64
import random
import hashlib
from datetime import date
from typing import List, Dict, Tuple, Optional

# Same env var names as the portfolio app, so an existing Azure OpenAI deployment carries over.
# The deployment must be a vision-capable model (gpt-4.1 and gpt-4o both are).
endpoint = os.getenv("ENDPOINT_URL", "")
deployment = os.getenv("DEPLOYMENT_NAME", "gpt-4.1")
subscription_key = os.getenv("AZURE_OPENAI_API_KEY", "")

# USD per 1K tokens [input, output]
COST_DICT = {
    "gpt-4.1": [0.002, 0.008],
    "gpt-4o": [0.0025, 0.01],
}

CATEGORIES = [
    'Power Tools',
    'Air Tools & Compressors',
    'Automotive',
    'Outdoor & Yard',
    'Masonry & Concrete',
    'Woodworking',
    'Plumbing & Electrical',
    'Ladders & Access',
    'Batteries & Chargers',
    'Specialty',
]

POWER_SOURCES = ['Battery', 'Corded (120V)', 'Corded (240V)', 'Gas', 'Pneumatic', 'Manual']

BATTERY_PLATFORMS = [
    'Milwaukee M18',
    'Milwaukee M12',
    'DeWalt 20V MAX',
    'DeWalt FLEXVOLT',
    'Ryobi ONE+',
    'Ryobi 40V',
    'Makita 18V LXT',
    'Makita 40V XGT',
    'Bosch 18V',
    'Ridgid 18V',
    'Craftsman V20',
    'EGO 56V',
    'Other',
]

# standard: normal rental. waiver: renter acknowledges safety notes at booking.
# excluded: not rentable during the POC (highest injury risk).
RISK_TIERS = ['standard', 'waiver', 'excluded']
EXCLUDED_KEYWORDS = ['chainsaw', 'chain saw', 'pole saw', 'log splitter', 'stump grinder', 'firearm']

# Marketplace economics
RENTER_SERVICE_FEE = 0.10     # added on top of the rental price
OWNER_COMMISSION = 0.15       # taken out of the owner's rental price
WEEKLY_DISCOUNT = 0.25        # applied to rentals of 7+ days

# Delivery pricing (owner or a courier drives it over and back)
DELIVERY_BASE_FEE = 5.00
DELIVERY_PER_MILE = 0.70      # fuel + wear, close to the IRS mileage rate
DELIVERY_MIN_FEE = 10.00
DELIVERY_PEAK_HOURS = set(range(7, 10)) | set(range(16, 19))   # rush hour traffic
DELIVERY_LATE_HOURS = set(range(21, 24)) | set(range(0, 6))
DELIVERY_PEAK_MULTIPLIER = 1.3
DELIVERY_LATE_MULTIPLIER = 1.5
DELIVERY_MAX_MILES = 25


def ai_is_configured() -> bool:
    """True when real vision calls will be made instead of demo drafts"""
    if os.getenv('LISTING_AI_MODE', '').lower() == 'mock':
        return False
    return bool(endpoint and subscription_key)


_client = None


def get_client():
    """Lazily build the Azure OpenAI client so demo mode never needs the SDK configured"""
    global _client
    if _client is None:
        from openai import AzureOpenAI
        _client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=subscription_key,
            api_version="2025-01-01-preview",
        )
    return _client


# ---------------------------------------------------------------------------
# Snap-to-list
# ---------------------------------------------------------------------------

def analyze_photo(image_bytes: bytes, mime_type: str = 'image/jpeg') -> Tuple[List[Dict], float, bool]:
    """
    Identify every rentable tool in a garage photo and draft a listing for each.

    Returns:
        - List of normalized draft dicts (see normalize_draft)
        - Cost of the API call (0 in demo mode)
        - Whether the result came from demo mode
    """
    if not ai_is_configured():
        return mock_analyze_photo(image_bytes), 0.0, True

    drafts, cost = vision_listing_call(image_bytes, mime_type)
    return drafts, cost, False


def vision_listing_call(image_bytes: bytes, mime_type: str) -> Tuple[List[Dict], float]:
    """Call the vision model with a forced function call that returns draft listings"""

    function_schema = {
        "name": "record_tool_listings",
        "description": "Record one draft rental listing for each distinct tool visible in the photo",
        "parameters": {
            "type": "object",
            "properties": {
                "tools": {
                    "type": "array",
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Short listing title, e.g. 'Gas Pressure Washer, 3100 PSI'"},
                            "brand": {"type": "string", "description": "Brand if visible or recognizable, else empty string"},
                            "model": {"type": "string", "description": "Model number if readable, else empty string"},
                            "category": {"type": "string", "enum": CATEGORIES},
                            "power_source": {"type": "string", "enum": POWER_SOURCES},
                            "battery_platform": {
                                "type": "string",
                                "enum": BATTERY_PLATFORMS + [''],
                                "description": "Battery system for cordless tools and batteries, empty string otherwise"
                            },
                            "description": {"type": "string", "description": "1-2 sentences a renter would find useful: what it does, capacity, condition"},
                            "included_items": {"type": "string", "description": "Accessories visible with it (hoses, blades, case, batteries), or 'Bare tool'"},
                            "replacement_value": {"type": "number", "description": "Estimated cost in USD to buy this tool new today"},
                            "daily_price": {"type": "number", "description": "Suggested daily rental price in USD"},
                            "deposit": {"type": "number", "description": "Suggested refundable deposit in USD"},
                            "risk_tier": {"type": "string", "enum": RISK_TIERS},
                            "safety_notes": {"type": "string", "description": "One short safety note for the renter"},
                            "confidence": {"type": "number", "description": "0-1 confidence in the identification"}
                        },
                        "required": ["name", "brand", "model", "category", "power_source", "battery_platform",
                                     "description", "included_items", "replacement_value", "daily_price",
                                     "deposit", "risk_tier", "safety_notes", "confidence"]
                    }
                }
            },
            "required": ["tools"]
        }
    }

    system_prompt = """You help tool owners list their garage gear on a neighbor-to-neighbor tool rental marketplace.

You will see one photo of a garage, shelf, workbench or single tool. Create a draft listing for each distinct, rentable tool you can see.

<listing_rules>
- Only list tools and equipment someone would rent: power tools, air tools, automotive tools, yard equipment, masonry and concrete gear, ladders, specialty tools, and batteries/chargers.
- Skip hand tools worth under about $30 (screwdrivers, hammers, loose sockets), consumables, and anything that is not a tool.
- Group obvious sets as one listing (e.g. a socket set in its case, two identical batteries).
- Read brand and model numbers from labels when legible. Never invent a model number; leave it empty if unreadable.
- Cordless tools: set battery_platform (e.g. Milwaukee M18 is red, DeWalt 20V is yellow/black, Ryobi ONE+ is green). If no battery is attached, say 'Bare tool' in included_items.
- Set confidence below 0.5 when the photo is blurry or the tool is partly hidden.
</listing_rules>

<pricing_guidance>
- replacement_value: typical new retail price in USD.
- daily_price: roughly 8-12% of replacement value for tools under $500, 5-8% above $500, never below $5. Round to whole dollars.
- deposit: roughly 50-100% of replacement value, rounded to the nearest $25.
</pricing_guidance>

<risk_tiers>
- excluded: chainsaws, pole saws, log splitters, stump grinders.
- waiver: tools that can cause serious injury in inexperienced hands: table/miter/circular/tile saws, nail guns, pressure washers, spring compressors, engine hoists, jacks and jack stands, ladders over 8 ft, demolition hammers.
- standard: everything else.
</risk_tiers>

If no rentable tool is visible, return an empty tools array."""

    image_b64 = base64.b64encode(image_bytes).decode('ascii')
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": "Draft listings for the tools in this photo."},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}", "detail": "high"}},
        ]},
    ]

    for attempt in range(3):
        try:
            response = get_client().chat.completions.create(
                model=deployment,
                messages=messages,
                tools=[{"type": "function", "function": function_schema}],
                tool_choice={"type": "function", "function": {"name": "record_tool_listings"}},
                temperature=0.1,
                max_tokens=3000
            )

            rates = COST_DICT.get(deployment, COST_DICT["gpt-4.1"])
            cost = (response.usage.prompt_tokens / 1000 * rates[0] +
                    response.usage.completion_tokens / 1000 * rates[1])

            tool_call = response.choices[0].message.tool_calls[0]
            result = json.loads(tool_call.function.arguments)

            if not isinstance(result.get('tools'), list):
                raise ValueError("Response is missing the tools array")

            return [normalize_draft(t) for t in result['tools'] if isinstance(t, dict)], cost

        except Exception as e:
            print(f"Vision listing API ERROR (attempt {attempt + 1}): {e}")
            if attempt < 2:
                time.sleep(random.randint(1, 3))
            else:
                raise e

    return [], 0


# Demo-mode drafts, picked deterministically from the photo bytes
MOCK_DRAFTS = [
    dict(name='Cordless Hammer Drill', brand='DeWalt', model='DCD996', category='Power Tools',
         power_source='Battery', battery_platform='DeWalt 20V MAX',
         description='3-speed brushless hammer drill, drills masonry and drives lag screws.',
         included_items='Bare tool', replacement_value=220, confidence=0.86,
         safety_notes='Use the side handle when drilling large holes.'),
    dict(name='Gas Pressure Washer, 3100 PSI', brand='Generac', model='', category='Outdoor & Yard',
         power_source='Gas', battery_platform='', description='Cleans driveways, decks and siding.',
         included_items='Hose, wand, 4 nozzle tips', replacement_value=430, risk_tier='waiver', confidence=0.78,
         safety_notes='Never point the wand at people or pets.'),
    dict(name='Pancake Air Compressor, 6 Gal', brand='Porter-Cable', model='C2002', category='Air Tools & Compressors',
         power_source='Corded (120V)', battery_platform='', description='Oil-free, 150 PSI. Good for trim nailers and tires.',
         included_items='25 ft hose', replacement_value=180, confidence=0.91,
         safety_notes='Drain the tank after each use.'),
    dict(name='Strut Spring Compressor', brand='', model='', category='Automotive', power_source='Manual',
         battery_platform='', description='Clamshell-style compressor for MacPherson strut springs.',
         included_items='Compressor, 2 jaw sets', replacement_value=200, risk_tier='waiver', confidence=0.64,
         safety_notes='Compressed springs store dangerous energy.'),
    dict(name='Circular Saw, 7-1/4"', brand='Milwaukee', model='2732-20 M18 FUEL', category='Power Tools',
         power_source='Battery', battery_platform='Milwaukee M18', description='Brushless circular saw, rips 2x lumber easily.',
         included_items='Bare tool, framing blade', replacement_value=280, risk_tier='waiver', confidence=0.88,
         safety_notes='Keep the blade guard working. Clamp the workpiece.'),
    dict(name='Extension Ladder, 24 ft', brand='Werner', model='D1224-2', category='Ladders & Access',
         power_source='Manual', battery_platform='', description='Type I fiberglass ladder, safe near power lines.',
         included_items='Ladder', replacement_value=330, risk_tier='waiver', confidence=0.8,
         safety_notes='Set at a 4:1 angle and have someone foot it.'),
    dict(name='Battery Pack & Charger', brand='Ryobi', model='P191 ONE+', category='Batteries & Chargers',
         power_source='Battery', battery_platform='Ryobi ONE+', description='Two 4Ah batteries with a dual-port charger.',
         included_items='2 batteries, charger', replacement_value=150, confidence=0.9,
         safety_notes='Do not use a swollen or cracked pack.'),
]


def mock_analyze_photo(image_bytes: bytes) -> List[Dict]:
    """Return 1-2 sample drafts chosen from a hash of the photo, so re-uploads are stable"""
    digest = hashlib.sha256(image_bytes).digest()
    count = 1 + digest[0] % 2
    start = digest[1] % len(MOCK_DRAFTS)
    picks = [MOCK_DRAFTS[(start + i) % len(MOCK_DRAFTS)] for i in range(count)]
    return [normalize_draft(dict(p)) for p in picks]


def suggest_daily_price(replacement_value: float) -> float:
    """Daily price heuristic used when the model's number is missing or out of range"""
    rate = 0.10 if replacement_value < 500 else 0.065
    return float(max(5, round(replacement_value * rate)))


def suggest_deposit(replacement_value: float) -> float:
    """Deposit heuristic: ~75% of replacement value, nearest $25, at least $50"""
    return float(max(50, round(replacement_value * 0.75 / 25) * 25))


def _to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_draft(raw: Dict) -> Dict:
    """Coerce a model or form draft into valid listing fields with sane prices"""
    name = str(raw.get('name', '')).strip()[:120] or 'Unidentified tool'
    category = raw.get('category') if raw.get('category') in CATEGORIES else 'Specialty'
    power_source = raw.get('power_source') if raw.get('power_source') in POWER_SOURCES else 'Manual'
    platform = raw.get('battery_platform') if raw.get('battery_platform') in BATTERY_PLATFORMS else ''
    if power_source != 'Battery' and category != 'Batteries & Chargers':
        platform = ''

    replacement = max(0.0, _to_float(raw.get('replacement_value')))
    daily = _to_float(raw.get('daily_price'))
    if daily <= 0 or (replacement and daily > replacement * 0.25):
        daily = suggest_daily_price(replacement)
    deposit = _to_float(raw.get('deposit'))
    if deposit <= 0 or (replacement and deposit > replacement * 1.5):
        deposit = suggest_deposit(replacement)

    risk = raw.get('risk_tier') if raw.get('risk_tier') in RISK_TIERS else 'standard'
    if any(k in name.lower() for k in EXCLUDED_KEYWORDS):
        risk = 'excluded'

    confidence = raw.get('confidence')
    confidence = None if confidence is None else min(1.0, max(0.0, _to_float(confidence)))

    return {
        'name': name,
        'brand': str(raw.get('brand', '')).strip()[:60],
        'model': str(raw.get('model', '')).strip()[:60],
        'category': category,
        'power_source': power_source,
        'battery_platform': platform,
        'description': str(raw.get('description', '')).strip()[:500],
        'included_items': str(raw.get('included_items', '')).strip()[:200],
        'replacement_value': round(replacement, 2),
        'daily_price': round(daily, 2),
        'deposit': round(deposit, 2),
        'risk_tier': risk,
        'safety_notes': str(raw.get('safety_notes', '')).strip()[:300],
        'ai_confidence': confidence,
    }


def draft_key(draft) -> str:
    """
    Identity used to merge duplicates when the same tool shows up in several
    video frames: brand + model when a model number was read, else brand + name.
    """
    brand = (draft['brand'] or '').strip().lower()
    model = (draft['model'] or '').strip().lower()
    if model:
        return f'{brand}|{model}'
    return f"{brand}|{(draft['name'] or '').strip().lower()}"


# ---------------------------------------------------------------------------
# Distance and pricing
# ---------------------------------------------------------------------------

def distance_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in miles"""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def rental_days(start: date, end: date) -> int:
    """Billable days: returning the next day counts as one day"""
    return max(1, (end - start).days)


def delivery_fee(miles: float, hour: int) -> float:
    """Round-trip delivery fee, adjusted for traffic (rush hour) and late-night requests"""
    fee = DELIVERY_BASE_FEE + miles * 2 * DELIVERY_PER_MILE
    if hour in DELIVERY_PEAK_HOURS:
        fee *= DELIVERY_PEAK_MULTIPLIER
    elif hour in DELIVERY_LATE_HOURS:
        fee *= DELIVERY_LATE_MULTIPLIER
    return round(max(DELIVERY_MIN_FEE, fee), 2)


def quote(daily_price: float, deposit: float, days: int,
          delivery: bool = False, miles: float = 0.0, hour: int = 12) -> Dict:
    """
    Price a rental. The deposit is a card hold released on return, so it is
    shown separately from what the renter actually pays.
    """
    rental = daily_price * days
    discount = rental * WEEKLY_DISCOUNT if days >= 7 else 0.0
    rental -= discount
    service_fee = rental * RENTER_SERVICE_FEE
    d_fee = delivery_fee(miles, hour) if delivery else 0.0
    return {
        'days': days,
        'daily_price': round(daily_price, 2),
        'weekly_discount': round(discount, 2),
        'rental_total': round(rental, 2),
        'service_fee': round(service_fee, 2),
        'delivery_fee': d_fee,
        'total_charge': round(rental + service_fee + d_fee, 2),
        'deposit_hold': round(deposit, 2),
        # Owner keeps the rental minus commission, plus the delivery fee for driving it over
        'owner_payout': round(rental * (1 - OWNER_COMMISSION) + d_fee, 2),
        'platform_revenue': round(rental * OWNER_COMMISSION + service_fee, 2),
    }
