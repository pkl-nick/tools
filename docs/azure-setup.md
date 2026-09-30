# Azure setup

Two pieces: a vision model deployment for snap-to-list, and an App Service web app to host the Flask app.

## 1. Model: video or image?

**Use an image (vision) deployment. Azure OpenAI chat models don't accept video files.**

- Azure's chat models, including the GPT-5.6 family, take images but not video files. Video has to be split into frames first, which is Microsoft's own guidance.
- Azure does have video services (Azure AI Content Understanding, Video Indexer), but they're built for transcripts, scene and shot detection, and generic labels. They won't give you "Milwaukee 2767-20, $20/day, $200 deposit", so you'd still need the vision model afterward.

**What the app does:** the browser pulls frames from the walkthrough video (at most one every 1.5 s, up to 24), skips frames where the camera hasn't moved, resizes them to 1600 px and sends them one at a time as images. Frames from one video share a batch id, so a tool seen in five frames becomes one draft. The server never touches video, so App Service needs no ffmpeg.

### Deployments: GPT-5.6 Luna, Terra and Sol

Create three deployments in the Azure OpenAI portal (Deployments → Deploy model). All three are version `2026-06-25`, need no access request, accept images, and support Structured Outputs. Your subscription may need a quota increase depending on its quota tier.

| Deployment name | Model | Used for | Reasoning effort |
|---|---|---|---|
| `gpt-5.6-luna` | GPT-5.6 Luna | Video frames (high volume) | `low` |
| `gpt-5.6-terra` | GPT-5.6 Terra | Photos (the default) | `low` |
| `gpt-5.6-sol` | GPT-5.6 Sol | Second opinion: an image is re-run on Sol if any draft comes back below 0.6 confidence | `medium` |

- **Deployment type:** Global Standard.
- **Other names:** if you name the deployments differently, set `DEPLOYMENT_LUNA`, `DEPLOYMENT_TERRA` and `DEPLOYMENT_SOL`.
- **Turning Sol off:** set `DEPLOYMENT_SOL` to empty.
- **Tuning:** effort per tier is adjustable with `LUNA_EFFORT`, `TERRA_EFFORT` and `SOL_EFFORT`.

### API: v1 + Responses

The app uses Azure's **v1 API** with the standard `OpenAI()` client (`openai` 3.22.1):
- The base URL is `https://<resource>.openai.azure.com/openai/v1/`.
- There's no `api-version` to maintain, and new features arrive without code changes.

Calls go through the **Responses API** with strict **Structured Outputs** (a JSON schema), and the listing JSON always parses. This matters for GPT-5.6:
- On Chat Completions it can't combine reasoning with tools, so the old forced-function-call approach fails.
- It rejects `temperature` and `max_tokens`. The app sends `reasoning.effort` and `max_output_tokens` instead.

### Rough cost

Each image is about 1,500 input tokens. Output is about 500 tokens of JSON plus reasoning tokens, which are billed as output. The estimates below use list prices at launch:

| Model | Price per 1M tokens (in / out) |
|---|---|
| Luna | $1 / $6 |
| Terra | $2.50 / $15 |
| Sol | $5 / $30 |

- **one photo (Terra):** about $0.01–0.03
- **a 24-frame walkthrough (Luna):** about $0.15–0.30
- **each Sol second opinion:** about $0.05–0.10

Azure's GPT-5.6 prices have moved since launch (OpenAI cut Luna and Terra prices, and Azure users reported a lag in matching them), so check the Azure OpenAI pricing page and update `COST_PER_M` in `listing_utils.py`. The app's per-call cost figure is an estimate.

### Settings

```
ENDPOINT_URL=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<key>
DEPLOYMENT_LUNA=gpt-5.6-luna
DEPLOYMENT_TERRA=gpt-5.6-terra
DEPLOYMENT_SOL=gpt-5.6-sol
```

With no key set, the app runs in demo mode and returns sample drafts.

## 2. App Service

1. **Web App:** Linux, Python 3.11. Put it on the portfolio's existing App Service Plan to avoid paying for a second plan (B1 is plenty for a POC).
2. **Startup command:** `gunicorn --bind=0.0.0.0 --timeout 120 --workers 2 app:app`
3. **Application settings:**
   - the model settings above
   - `FLASK_SECRET_KEY` (random string)
   - `TOOLS_DB_PATH=/home/data/tools.db`
   - `UPLOAD_FOLDER=/home/data/uploads`

   `/home` is the persistent share on Linux App Service, so the database and photos survive restarts and redeploys.
4. **Deployment:** Deployment Center → GitHub → `pkl-nick/tools`, pick the branch. Azure generates a GitHub Actions workflow.
5. **Upload limit:** photos are resized in the browser to about 300–600 KB, so App Service's default request limits are fine. The app caps each request at 15 MB.

## Later, before real users

- Move photos to Blob Storage (you already use `azure-storage-blob` in the portfolio) and the database to Azure SQL or PostgreSQL.
- Real accounts: Entra External ID or similar. The "acting as" switcher is demo-only.
- Payments and deposit holds: Stripe (authorize-only holds for deposits).
- Keep API keys in App Service settings or Key Vault, never as defaults in code.
