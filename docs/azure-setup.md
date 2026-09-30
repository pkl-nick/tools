# Azure setup

Two pieces: a vision model deployment for snap-to-list, and an App Service web app to host the Flask app.

## 1. Model: video or image?

**Use an image (vision) deployment. Azure OpenAI chat models don't accept video files.**

- The vision-capable chat models on Azure (GPT-4.1, GPT-4o, GPT-5 series, o-series) take images through Chat Completions. Video has to be split into frames first. Microsoft's own guidance is to extract frames and send them as images.
- Azure does have video services (Azure AI Content Understanding, Video Indexer), but they're built for transcripts, scene and shot detection, and generic labels. They won't give you "Milwaukee 2767-20, $20/day, $200 deposit", so you'd still need the vision model afterward.

**What the app does:** the browser pulls frames from the walkthrough video (at most one every 1.5 s, up to 24), skips frames where the camera hasn't moved, resizes them to 1600 px and sends them one at a time as images. Frames from one video share a batch id, so a tool seen in five frames becomes one draft. The server never touches video, so App Service needs no ffmpeg.

### Recommended deployment

| | |
|---|---|
| Model | `gpt-4.1` (your portfolio app already uses it, so you may be able to reuse that deployment) |
| Deployment type | Global Standard |
| API | Chat Completions with function calling (what `listing_utils.py` uses) |
| Cheaper option to test | `gpt-4.1-mini`: set `DEPLOYMENT_NAME=gpt-4.1-mini` and compare on the same photos |

Some newer GPT-5 deployments only support the Responses API, and this code uses Chat Completions, so stick with 4.1 for the POC.

### Rough cost

At about 1,500 input tokens (image plus prompt) and 500 output tokens per image, and gpt-4.1 list pricing of about $2 per 1M input tokens and $8 per 1M output tokens:

- **one photo:** about $0.007
- **a 24-frame garage walkthrough:** about $0.15–0.20

Check current prices on the Azure OpenAI pricing page for your region.

### Settings

Same names as the portfolio app:

```
ENDPOINT_URL=https://<resource>.openai.azure.com/
DEPLOYMENT_NAME=gpt-4.1
AZURE_OPENAI_API_KEY=<key>
```

With no key set, the app runs in demo mode and returns sample drafts.

## 2. App Service

1. **Web App:** Linux, Python 3.11. Put it on the portfolio's existing App Service Plan to avoid paying for a second plan (B1 is plenty for a POC).
2. **Startup command:** `gunicorn --bind=0.0.0.0 --timeout 120 --workers 2 app:app`
3. **Application settings:**
   - the three model settings above
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
