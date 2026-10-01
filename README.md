# toolshare

Rent specialty tools from neighbors. Owners walk their garage with a phone, AI drafts the listings, and renters find tools nearby, including ones that fit batteries they already own.

Same stack as the portfolio app: Flask, SQLite, Jinja templates and plain JS, with Azure OpenAI GPT-5.6 (Luna, Terra, Sol) for vision via the v1 Responses API. The design is "Craigslist, evolved": text-first listings, blue link titles and the purple brand, with a modern layout (search in the header, category chips, list and gallery views, sticky booking card, dark mode, phone-first).

## What works
- **Accounts:** sign up or log in with **Google**, **Microsoft** or email and password (hashed). Social sign-ins link to an existing account only when the provider verified the email (Google); a Microsoft sign-in never takes over an account by email alone. Profiles cover name, neighborhood, location (rounded to about a block), battery systems, bio and photo, and each member has a public profile page with their listings. Browsing is open; posting, renting and the garage need an account. Includes CSRF protection, login lockout and a daily AI upload cap per member.
- **Location, with permission:** the browser asks before sharing location, on sign-up, account settings and each post. A post can use the owner's current location as its pickup spot (a second garage or shop) instead of the profile location. Coordinates are rounded to about a block and never shown.
- **Real places:** every member gives a ZIP code, which Azure Maps resolves to a city and state, so "Capitol Hill" in Denver and in Seattle stay apart. Listings read "Capitol Hill · Seattle, WA". Visitors can search near any ZIP.
- **Courier delivery (Uber Direct):** for tools that fit in a car, renters can choose an Uber courier both ways. It's priced from two live Uber quotes plus a markup, owners dispatch it from My garage, and status and tracking arrive by signed webhook. Trips run with Uber's test courier until `COURIER_LIVE=1`. See `docs/delivery.md`.
- **Snap-to-list:** upload photos or a walkthrough video. Video is split into frames in the browser, and duplicate frames and duplicate tools are merged. A vision model drafts the title, brand and model, category, battery system, price, deposit and safety tier for each tool.
- **Bulk review:** edit, approve or skip every draft on one page. Chainsaws and similar tools are blocked during the pilot.
- **Search that scales:** SQLite R\*Tree (location) and FTS5 (text) indexes kept current by triggers. Stemming, prefixes and AI-written alternate names ("weed eater" finds a string trimmer). Best-match or nearest-first ordering and 25 results per page. About 20 ms per search at 500k listings. See `docs/search.md`.
- **Browse:** nearest first, with search, category, distance and a "fits my batteries" filter.
- **Rental requests:** live quote with a weekly discount, 10% renter service fee, 15% owner commission, and a refundable deposit hold. Optional delivery is priced by distance and time of day (rush hour and late night cost more). Risky tools require a safety acknowledgment.
- **My garage:** owners accept, decline or mark returned; renters cancel. Overlapping bookings are blocked.
- **Cheap on purpose:** daily rates are about 1% of what the tool costs new (a $400 impact wrench is $4/day), so owners earn passively and renters can keep a tool for days or a week. Delivery and pickup carry the bigger fees.
- **Admin dashboard (`/admin`, password):** members, active users, visitors, posts, drafts, rental requests, rental volume, platform revenue, AI calls and spend, a sign-up-to-rental funnel, 30-day charts, AI usage by model, top pages, a member list with password reset, and recent activity. Backed by a privacy-light event log (no IPs).
- **Blob Storage:** listing and profile photos and database backups live in private Azure Blob containers, accessed with the app's managed identity (no keys). The admin page can back up the database and move older on-disk photos into Blob Storage.
- **Model test bench (`/eval`):** 17 openly licensed garage and tool photos with expected answers. It grades each model's drafts (found the tools, right brand/category/battery, didn't over-list) and keeps run history to compare Luna, Terra, Sol and the full pipeline.

Not built yet: payments (see `docs/payments-plan.md` and `docs/stripe-setup.md`), Apple sign-in, password-reset emails, messaging, ratings and a map view. Seeded sample listings are labeled and can't be rented.

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
| `places.py`, `delivery.py` | Azure Maps ZIP/address lookups; Uber Direct quotes, dispatch and webhook checks |
| `search.py` | Location + full-text search indexes and the one search function (`docs/search.md`) |
| `storage.py` | Blob Storage (managed identity) or local-disk storage for photos and backups |
| `telemetry.py`, `metrics.py` | Event log and the admin dashboard's numbers and chart geometry |
| `listing_utils.py` | Vision prompt, model tiering (Luna/Terra/Sol) and Structured Outputs, draft normalization, pricing, distance |
| `db_builder.py` | SQLite schema and demo seed data |
| `templates/`, `static/` | Pages, CSS, and the browser-side photo/video handling (`static/js/list.js`) |
| `docs/azure-setup.md` | GPT-5.6 deployments, v1 API, costs and App Service setup |
| `evals/` | Test photos, expected answers (`cases.json`), grader, runner, and credits (`ATTRIBUTION.md`) |
| `deploy/` | `azure-deploy.sh` (app + storage + identity), `setup-sign-in.sh` (Microsoft app registration + Google client), `set-stripe-keys.sh`, `setup-delivery.sh` (Uber Direct) |
| `docs/payments-plan.md`, `docs/stripe-setup.md` | Deposit recommendation, money flow, and your Stripe checklist |
| `docs/delivery.md` | ZIP-based locations and the Uber Direct courier flow, with your setup checklist |
| `docs/research.md` | Market research behind the idea |
