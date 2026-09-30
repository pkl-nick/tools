"""
Run the snap-to-list evaluation from the command line.

    python -m evals.run_eval                      # pipeline (what the app does), all cases
    python -m evals.run_eval --tier luna --tier sol
    python -m evals.run_eval --tier all --case dewalt-truck-bed

Needs ENDPOINT_URL and AZURE_OPENAI_API_KEY (and the DEPLOYMENT_* names if they
differ from the defaults). Without them it runs in demo mode, which only checks
the plumbing. Writes JSON and Markdown reports to evals/results/.
"""

import os
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import listing_utils                                     # noqa: E402
from evals.runner import load_cases, run_case, TIERS     # noqa: E402
from evals.scoring import summarize                      # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')


def fmt_row(r):
    if r.get('error'):
        return f"| {r['case_id']} | {r['tier']} | ERROR | | | | {r['error'][:60]} |"
    missing = ', '.join(r['missing']) or '—'
    wrong = ', '.join(f"{c['tool']} {c['attribute']}" for c in r['checks'] if not c['ok']) or '—'
    status = 'pass' if r['passed'] else 'FAIL'
    return (f"| {r['case_id']} | {r['tier']} | {r['score']} {status} | {r['found']}/{r['expected']} "
            f"({r['listings']} listed) | {r['seconds']}s | ${r['cost']:.4f} | missing: {missing}; wrong: {wrong} |")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tier', action='append', choices=TIERS + ['all'],
                        help='repeatable; default: pipeline')
    parser.add_argument('--case', action='append', help='case id; repeatable; default: all')
    args = parser.parse_args()

    tiers = TIERS if args.tier and 'all' in args.tier else (args.tier or ['pipeline'])
    cases = [c for c in load_cases() if not args.case or c['id'] in args.case]
    if not cases:
        sys.exit('No matching cases')
    if not listing_utils.ai_is_configured():
        print('Demo mode: no Azure credentials, results only check the plumbing.\n')

    header = '| case | tier | score | found | time | cost | notes |\n|---|---|---|---|---|---|---|'
    print(header)
    results = []
    for tier in tiers:
        for case in cases:
            r = run_case(case, tier)
            results.append(r)
            print(fmt_row(r), flush=True)

    summary = {t: summarize([r for r in results if r['tier'] == t]) for t in tiers}
    print('\n| tier | avg score | passed | errors | total cost | avg time |\n|---|---|---|---|---|---|')
    for t, s in summary.items():
        print(f"| {t} | {s['avg_score']} | {s['passed']}/{s['cases']} | {s['errors']} | ${s['total_cost']:.4f} | {s['avg_seconds']}s |")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    with open(os.path.join(RESULTS_DIR, f'{stamp}.json'), 'w') as f:
        json.dump({'when': stamp, 'tiers': tiers, 'summary': summary, 'results': results}, f, indent=2)
    print(f'\nSaved evals/results/{stamp}.json')


if __name__ == '__main__':
    main()
