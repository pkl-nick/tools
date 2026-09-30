"""
Score snap-to-list drafts against a case's expectations.

A case expects some tools. A draft matches an expected tool when every word of
one of its 'match' phrases appears in the draft's name, brand or model. Each
draft can match only one expected tool. Matched drafts are then checked on the
attributes the case specifies (category, brand, battery platform, risk tier,
power source).

Score out of 100:
    60  found the expected tools (recall)
    30  got their attributes right
    10  didn't over-list (at most max_listings drafts)
Cases that expect nothing (negative / restraint) score 100 when they stay at
or under max_listings, minus 25 per extra listing.
A case passes at 70 or more.
"""

PASS_SCORE = 70


def draft_text(draft):
    return ' '.join(str(draft.get(k) or '') for k in ('name', 'brand', 'model')).lower()


def phrase_matches(phrase, text):
    return all(word in text for word in phrase.lower().split())


def match_tools(expected, drafts):
    """Greedy one-to-one matching; returns {expected index: draft index}"""
    used, pairs = set(), {}
    for ei, tool in enumerate(expected):
        for di, draft in enumerate(drafts):
            if di in used:
                continue
            if any(phrase_matches(p, draft_text(draft)) for p in tool['match']):
                pairs[ei] = di
                used.add(di)
                break
    return pairs


def _as_list(value):
    return value if isinstance(value, list) else [value]


def check_attributes(tool, draft):
    """List of (attribute, expected, got, ok) for the attributes the case specifies"""
    checks = []
    if 'category' in tool:
        ok = draft.get('category') in _as_list(tool['category'])
        checks.append(('category', tool['category'], draft.get('category'), ok))
    if 'brand' in tool:
        want = tool['brand'].lower()
        ok = want in (draft.get('brand') or '').lower() or want in (draft.get('name') or '').lower()
        checks.append(('brand', tool['brand'], draft.get('brand'), ok))
    if 'battery_platform' in tool:
        ok = draft.get('battery_platform') in _as_list(tool['battery_platform'])
        checks.append(('battery_platform', tool['battery_platform'], draft.get('battery_platform'), ok))
    if 'risk_tier' in tool:
        ok = draft.get('risk_tier') == tool['risk_tier']
        checks.append(('risk_tier', tool['risk_tier'], draft.get('risk_tier'), ok))
    if 'power_source' in tool:
        ok = draft.get('power_source') == tool['power_source']
        checks.append(('power_source', tool['power_source'], draft.get('power_source'), ok))
    return checks


def score_case(case, drafts):
    expected = case['expect']['tools']
    max_listings = case['expect']['max_listings']
    over = max(0, len(drafts) - max_listings)

    if not expected:
        score = max(0, 100 - 25 * over)
        return {
            'score': score, 'passed': score >= PASS_SCORE and over == 0,
            'found': 0, 'expected': 0, 'missing': [], 'checks': [],
            'listings': len(drafts), 'over_listed': over, 'extra_listings': len(drafts),
        }

    pairs = match_tools(expected, drafts)
    checks = []
    for ei, di in pairs.items():
        for attr, want, got, ok in check_attributes(expected[ei], drafts[di]):
            checks.append({'tool': expected[ei]['label'], 'attribute': attr, 'expected': want, 'got': got, 'ok': ok})

    recall = len(pairs) / len(expected)
    attr_score = (sum(c['ok'] for c in checks) / len(checks)) if checks else recall
    score = round(60 * recall + 30 * attr_score + (10 if over == 0 else 0))
    return {
        'score': score, 'passed': score >= PASS_SCORE,
        'found': len(pairs), 'expected': len(expected),
        'missing': [t['label'] for i, t in enumerate(expected) if i not in pairs],
        'checks': checks,
        'listings': len(drafts), 'over_listed': over, 'extra_listings': len(drafts) - len(pairs),
    }


def summarize(results):
    """Totals for a list of result dicts from runner.run_case"""
    ok = [r for r in results if not r.get('error')]
    n = len(ok)
    return {
        'cases': len(results),
        'errors': len(results) - n,
        'avg_score': round(sum(r['score'] for r in ok) / n, 1) if n else 0,
        'passed': sum(r['passed'] for r in ok),
        'total_cost': round(sum(r['cost'] for r in ok), 4),
        'avg_seconds': round(sum(r['seconds'] for r in ok) / n, 1) if n else 0,
    }
