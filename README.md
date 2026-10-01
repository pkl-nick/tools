# toolshare

Rent specialty tools from neighbors. Owners walk their garage with a phone, AI drafts the listings, and renters find tools nearby, including ones that fit batteries they already own.

Same stack as the portfolio app: Flask, SQLite, Jinja templates and plain JS, with Azure OpenAI GPT-5.6 (Luna, Terra, Sol) for vision via the v1 Responses API. The design is "Craigslist, evolved": text-first listings, blue link titles and the purple brand, with a modern layout (search in the header, category chips, list and gallery views, sticky booking card, dark mode, phone-first).

## What works
- **Accounts:** sign up, log in and log out with hashed passwords. Profiles cover name, neighborhood, location (rounded to about a block), battery systems, bio and photo, and each member has a public profile page with their listings. Browsing is open; posting, renting and the garage need an account. Includes CSRF protection, login lockout and a daily AI upload cap per member.
- **Snap-to-list:** upload photos or a walkthrough video. Video is split into frames in the browser, and duplicate frames and duplicate tools are merged. A vision model drafts the title, brand and model, category, battery system, price, deposit and safety tier for each tool.
- **Bulk review:** edit, approve or skip every draft on one page. Chainsaws and similar tools are blocked during the pilot.
- **Browse:** nearest first, with search, category, distance and a "fits my batteries" filter.
- **Rental requests:** live quote with a weekly discount, 10% renter service fee, 15% owner commission, and a refundable deposit hold. Optional delivery is priced by distance and time of day (rush hour and late night cost more). Risky tools require a safety acknowledgment.
- **My garage:** owners accept, decline or mark returned; renters cancel. Overlapping bookings are blocked.
- **Cheap on purpose:** daily rates are about 1% of what the tool costs new (a $400 impact wrench is $4/day), so owners earn passively and renters can keep a tool for days or a week. Delivery and pickup carry the bigger fees.
- **Admin dashboard (`/admin`, password):** members, active users, visitors, posts, drafts, rental requests, rental volume, platform revenue, AI calls and spend, a sign-up-to-rental funnel, 30-day charts, AI usage by model, top pages, a member list with password reset, and recent activity. Backed by a privacy-light event log (no IPs).
- **Blob Storage:** listing and profile photos and database backups live in private Azure Blob containers, accessed with the app's managed identity (no keys). The admin page can back up the database and move older on-disk photos into Blob Storage.
- **Model test bench (`/eval`):** 17 openly licensed garage and tool photos with expected answers. It grades each model's drafts (found the tools, right brand/category/battery, didn't over-list) and keeps run history to compare Luna, Terra, Sol and the full pipeline.

Not built yet: payments and deposit holds, sign-in with Google/Microsoft/Apple, password-reset emails, messaging, ratings and maps. Seeded sample listings are labeled and can't be rented.

## Run locally
```bash
pip install -r requirements-dev.txt
python app.py            # http://localhost:8000
python -m pytest         # tests
python -m evals.run_eval --tier all   # model evaluation (needs Azure credentials)
```
With no Azure credentials set, photo recognition runs in **demo mode** and returns sample drafts. To use the real models, set `ENDPOINT_URL` and `AZURE_OPENAI_API_KEY`, and create the three GPT-5.6 deployments (see `.env.example` and `docs/azure-setup.md`).

The database is created and seeded automatically at `instance/tools.db`. Delete that file to reset.

## Files
| File | Purpose |
|---|---|
| `app.py` | Flask routes |
| `storage.py` | Blob Storage (managed identity) or local-disk storage for photos and backups |
| `telemetry.py`, `metrics.py` | Event log and the admin dashboard's numbers and chart geometry |
| `listing_utils.py` | Vision prompt, model tiering (Luna/Terra/Sol) and Structured Outputs, draft normalization, pricing, distance |
| `db_builder.py` | SQLite schema and demo seed data |
| `templates/`, `static/` | Pages, CSS, and the browser-side photo/video handling (`static/js/list.js`) |
| `docs/azure-setup.md` | GPT-5.6 deployments, v1 API, costs and App Service setup |
| `evals/` | Test photos, expected answers (`cases.json`), grader, runner, and credits (`ATTRIBUTION.md`) |
| `docs/research.md` | Market research behind the idea |
