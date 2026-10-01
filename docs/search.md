# Search

Tool search is local: someone in Denver never needs a Seattle listing. So a search first narrows to **what's near**, then to **what matches**. Both steps use indexes built into SQLite, with no extra service and no extra cost.

## How it works
| Piece | What it does |
|---|---|
| `tools_geo` (SQLite **R\*Tree**) | One point per listed tool: its own pickup spot, else the owner's location. A search reads only the tools inside a box around the viewer, then cuts the circle out of the box in SQL. |
| `tools_fts` (SQLite **FTS5**) | Full-text index of name, brand, model, search keywords, battery system, category and description. Uses English stemming ("nailers" finds "nailer") and prefix matching ("compr" finds "compressor"). |
| `search_keywords` | Written by the vision model when a tool is listed: other names and jobs, e.g. "weed eater" for a string trimmer or "cherry picker" for an engine hoist. Owners can edit them on the review page. They're never shown on the listing. |
| Triggers | Keep both indexes current when a tool is listed, edited, moved or removed, and when an owner moves. No route has to remember to update them. |
| `search.search_tools()` | The one function every search goes through. Moving to PostgreSQL or Azure AI Search later means changing this function, not the pages. |

**Ordering:**
- **Browsing:** nearest first. Real listings come before samples.
- **With search text:** best match first, with a link to sort closest first instead. Best match weights the name highest, then brand and model, then keywords, battery system and category, then the description. Rarer words count more.
- **No exact match:** if no tool has every word, the search shows tools with any of the words and says so. Filler words like "tool" and "for" are ignored in that fallback.

**Paging:** results come 25 at a time, and only that page's rows are loaded.

**Safety:** search text is split into words and quoted, so FTS operators and SQL can't get through.

## Measured
Synthetic data: 500,000 listings from 50,000 owners spread over 60 US metros, searched from Denver. Times are on this dev container.

| Search | Radius | Before (full scan) | After |
|---|---|---|---|
| browse, no text | 10 mi | 3,913 ms | 19 ms |
| browse, no text | 25 mi | 3,763 ms | 40 ms |
| "impact wrench" | 10 mi | 334 ms | 22 ms |
| "drill" | 25 mi | 369 ms | 28 ms |

**Scaling:** cost now grows with the number of tools **near the viewer**, not the national total. The text step reads the nationwide matches from the index once (about 10 ms for 28,000 hits), and only the nearby matches are scored.

**Two things the benchmark caught:**
- **Join order:** SQLite's planner preferred to start from every listed tool, so the join order is forced to start at the location index.
- **Scoring:** FTS5's built-in relevance score (bm25) is computed for every nationwide match, which cost four times the search itself. Only the few hundred nearby matches are scored instead.

## Admin
The admin page shows **top searches** and **searches with no results** for the last 30 days. The no-results list is tools people want that nobody near them has listed, so it doubles as a list of owners to recruit. The page also names the search engine in use: "R\*Tree + FTS5" on a normal SQLite build.

## Fallbacks
If an SQLite build lacks R\*Tree or FTS5, `search.install()` falls back to a plain table with a B-tree index and to `LIKE` matching. Results are the same, just slower at scale. Azure App Service's Python image includes both extensions; the admin page shows which is active.

## When to move on
SQLite search handles millions of listings. The thing that will force a change is SQLite's single writer: it means one web server. At that point:
1. **PostgreSQL + PostGIS** (Azure Database for PostgreSQL Flexible Server). `ST_DWithin` on a GiST index replaces the R\*Tree, `tsvector` + GIN replaces FTS5, `pg_trgm` adds typo tolerance, and `pgvector` adds search by meaning ("get a stuck bolt off" → breaker bar).
2. **Azure AI Search**, only if relevance tuning outgrows Postgres.

Either way, it's a rewrite of `search_tools()` plus a sync step.
