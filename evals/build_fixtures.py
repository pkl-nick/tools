"""
Download, crop and save the evaluation images listed in evals/cases.json,
and write evals/ATTRIBUTION.md.

    python evals/build_fixtures.py            # only missing images
    python evals/build_fixtures.py --force    # re-download everything

To add a case: find an openly licensed photo (e.g. openverse.org with the
"use commercially" and "modify or adapt" filters), add an entry to cases.json
with its source and a crop box [left, top, right, bottom] as fractions, then run this.
"""

import io
import os
import sys
import json
import time
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = os.path.join(HERE, 'images')
MAX_EDGE = 1280     # similar to what the browser uploads after resizing
USER_AGENT = 'toolshare-eval/1.0 (https://github.com/pkl-nick/tools)'

LICENSE_NAMES = {'by': 'CC BY', 'by-sa': 'CC BY-SA', 'cc0': 'CC0', 'pdm': 'Public Domain Mark'}


def load_cases():
    with open(os.path.join(HERE, 'cases.json')) as f:
        return json.load(f)['cases']


def build_image(case):
    url = case['source']['image_url']
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    img = Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=60).read())).convert('RGB')
    left, top, right, bottom = case.get('crop') or [0, 0, 1, 1]
    w, h = img.size
    img = img.crop((round(left * w), round(top * h), round(right * w), round(bottom * h)))
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    img.save(os.path.join(IMAGES, case['file']), 'JPEG', quality=88)
    return img.size


def write_attribution(cases):
    lines = [
        '# Evaluation image credits',
        '',
        'Photos used to test snap-to-list. Each is cropped and resized from the original. '
        'Crops of CC BY-SA photos are shared under the same license.',
        '',
        '| File | Original | Author | License |',
        '|---|---|---|---|',
    ]
    for c in cases:
        s = c['source']
        lic = f"{LICENSE_NAMES.get(s['license'], s['license'].upper())} {s['license_version'] or ''}".strip()
        author = f"[{s['creator']}]({s['creator_url']})" if s.get('creator_url') else (s.get('creator') or 'unknown')
        lines.append(f"| `{c['file']}` | [{s['title']}]({s['landing_url']}) | {author} | [{lic}]({s['license_url']}) |")
    with open(os.path.join(HERE, 'ATTRIBUTION.md'), 'w') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    force = '--force' in sys.argv
    os.makedirs(IMAGES, exist_ok=True)
    cases = load_cases()
    for c in cases:
        path = os.path.join(IMAGES, c['file'])
        if os.path.exists(path) and not force:
            continue
        size = build_image(c)
        print(f"{c['file']}: {size[0]}x{size[1]}")
        time.sleep(0.5)
    write_attribution(cases)
    print(f'{len(cases)} cases, attribution written')


if __name__ == '__main__':
    main()
