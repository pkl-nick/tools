"""
Run evaluation cases through the snap-to-list models. Shared by the CLI
(evals/run_eval.py) and the web page (/eval).

Tiers:
    luna | terra | sol  - one call to that model, no escalation (compare models head to head)
    pipeline            - exactly what the app does for a photo upload (PHOTO_TIER + Sol escalation)
"""

import os
import json
import time

import listing_utils
from evals.scoring import score_case

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(HERE, 'images')
TIERS = ['luna', 'terra', 'sol', 'pipeline']


def load_cases():
    with open(os.path.join(HERE, 'cases.json')) as f:
        return json.load(f)['cases']


def get_case(case_id):
    for case in load_cases():
        if case['id'] == case_id:
            return case
    return None


def run_case(case, tier):
    """Run one case on one tier; always returns a result dict (errors included, never raised)"""
    if tier not in TIERS:
        raise ValueError(f'Unknown tier {tier!r}; use one of {TIERS}')
    with open(os.path.join(IMAGES_DIR, case['file']), 'rb') as f:
        image = f.read()

    started = time.monotonic()
    try:
        if not listing_utils.ai_is_configured():
            drafts, cost, model = listing_utils.mock_analyze_photo(image), 0.0, 'demo'
        elif tier == 'pipeline':
            drafts, cost, _, model = listing_utils.analyze_photo(image, 'image/jpeg', source='photo')
        else:
            drafts, cost = listing_utils.vision_listing_call(image, 'image/jpeg', tier)
            model = listing_utils.deployment_for(tier)
    except Exception as e:
        return {'case_id': case['id'], 'tier': tier, 'error': str(e)[:500],
                'seconds': round(time.monotonic() - started, 1)}

    result = {
        'case_id': case['id'], 'tier': tier, 'model': model,
        'seconds': round(time.monotonic() - started, 1), 'cost': round(cost, 5),
        'drafts': [{k: d.get(k) for k in ('name', 'brand', 'model', 'category', 'battery_platform', 'power_source',
                                         'risk_tier', 'daily_price', 'deposit', 'replacement_value', 'ai_confidence')}
                   for d in drafts],
    }
    result.update(score_case(case, drafts))
    return result
