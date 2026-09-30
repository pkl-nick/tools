#!/usr/bin/env bash
# Deploy toolshare to Azure App Service from Azure Cloud Shell (Bash).
#
#   bash deploy/azure-deploy.sh
#
# - Puts a new web app on the same App Service plan as the portfolio (no extra plan cost)
# - Wires it to your Azure OpenAI resource (key read straight from Azure, never printed)
# - Checks the three GPT-5.6 deployments exist
# - Clones this repo and zip-deploys it; Azure installs requirements.txt during the deploy
#
# Safe to re-run: it updates settings and redeploys the latest code on BRANCH.

set -euo pipefail

# ------------------------------------------------------------------ settings
APP_NAME="toolshare-nick"                 # must be globally unique -> https://<APP_NAME>.azurewebsites.net
SOURCE_APP="nick-ryan-portfolio"          # new app shares this app's App Service plan
SOURCE_RG="nick-portfolio-rg"
AOAI_NAME=""                              # Azure OpenAI resource name; blank = auto-detect if you have exactly one
REPO_URL="https://github.com/pkl-nick/tools.git"
BRANCH="claude/tool-sharing-platform-research-xzdoea"
RUNTIME="PYTHON:3.11"
DEPLOYMENTS=(gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol)
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

step "Subscription"
az account show --query "{subscription:name, id:id}" -o table

step "Finding the App Service plan used by $SOURCE_APP"
PLAN_ID=$(az webapp show -g "$SOURCE_RG" -n "$SOURCE_APP" --query serverFarmId -o tsv)
[ -n "$PLAN_ID" ] || fail "Could not find web app $SOURCE_APP in $SOURCE_RG"
IFS=$'\t' read -r PLAN_NAME PLAN_RG PLAN_SKU PLAN_LINUX PLAN_LOCATION < <(az appservice plan show --ids "$PLAN_ID" \
  --query "[name, resourceGroup, sku.name, reserved, location]" -o tsv)
echo "plan: $PLAN_NAME  rg: $PLAN_RG  sku: $PLAN_SKU  linux: $PLAN_LINUX  region: $PLAN_LOCATION"
[ "$PLAN_LINUX" = "true" ] || fail "Plan $PLAN_NAME is not a Linux plan; Python apps need Linux"
ALWAYS_ON=true
case "$PLAN_SKU" in
  F1|D1) ALWAYS_ON=false
         echo "Note: $PLAN_SKU is a free/shared tier: no Always On, and CPU minutes are capped per day." ;;
esac

step "Creating web app $APP_NAME (skipped if it already exists)"
if az webapp show -g "$PLAN_RG" -n "$APP_NAME" --query name -o tsv >/dev/null 2>&1; then
  echo "exists"
else
  az webapp create -g "$PLAN_RG" -p "$PLAN_ID" -n "$APP_NAME" --runtime "$RUNTIME" --output none
  echo "created"
fi

step "Locating the Azure OpenAI resource"
if [ -z "$AOAI_NAME" ]; then
  mapfile -t AOAI_ROWS < <(az cognitiveservices account list \
    --query "[?kind=='OpenAI' || kind=='AIServices'].[name, resourceGroup]" -o tsv)
  if [ "${#AOAI_ROWS[@]}" -ne 1 ]; then
    printf '%s\n' "${AOAI_ROWS[@]:-(none found)}"
    fail "Found ${#AOAI_ROWS[@]} Azure OpenAI resources. Set AOAI_NAME at the top of this script."
  fi
  IFS=$'\t' read -r AOAI_NAME AOAI_RG <<< "${AOAI_ROWS[0]}"
else
  AOAI_RG=$(az cognitiveservices account list --query "[?name=='$AOAI_NAME'].resourceGroup | [0]" -o tsv)
  [ -n "$AOAI_RG" ] || fail "Azure OpenAI resource $AOAI_NAME not found"
fi
# AI Services resources list several endpoints; prefer the openai.azure.com one the v1 API uses
AOAI_ENDPOINT=$(az cognitiveservices account show -n "$AOAI_NAME" -g "$AOAI_RG" \
  --query "properties.endpoints.\"OpenAI Language Model Instance API\" || properties.endpoint" -o tsv)
AOAI_KEY=$(az cognitiveservices account keys list -n "$AOAI_NAME" -g "$AOAI_RG" --query key1 -o tsv)
echo "resource: $AOAI_NAME  rg: $AOAI_RG  endpoint: $AOAI_ENDPOINT"

step "Checking GPT-5.6 deployments"
DEPLOYED=$(az cognitiveservices account deployment list -n "$AOAI_NAME" -g "$AOAI_RG" --query "[].name" -o tsv)
MISSING=0
for d in "${DEPLOYMENTS[@]}"; do
  if grep -qx "$d" <<< "$DEPLOYED"; then echo "ok       $d"; else echo "MISSING  $d"; MISSING=1; fi
done
[ "$MISSING" -eq 0 ] || echo "Some deployments are missing. The app still deploys; create them in the portal before using snap-to-list."

step "App settings (values are not printed)"
EXISTING_SETTINGS=$(az webapp config appsettings list -g "$PLAN_RG" -n "$APP_NAME" --query "[].name" -o tsv)
SETTINGS=(
  "ENDPOINT_URL=$AOAI_ENDPOINT"
  "AZURE_OPENAI_API_KEY=$AOAI_KEY"
  "DEPLOYMENT_LUNA=${DEPLOYMENTS[0]}"
  "DEPLOYMENT_TERRA=${DEPLOYMENTS[1]}"
  "DEPLOYMENT_SOL=${DEPLOYMENTS[2]}"
  "TOOLS_DB_PATH=/home/data/tools.db"
  "UPLOAD_FOLDER=/home/data/uploads"
  "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
)
# Keep the secret key stable across re-runs so logins/sessions survive redeploys
if ! grep -qx "FLASK_SECRET_KEY" <<< "$EXISTING_SETTINGS"; then
  SETTINGS+=("FLASK_SECRET_KEY=$(openssl rand -hex 32)")
fi
az webapp config appsettings set -g "$PLAN_RG" -n "$APP_NAME" --settings "${SETTINGS[@]}" --output none
unset AOAI_KEY SETTINGS
echo "set: ENDPOINT_URL, AZURE_OPENAI_API_KEY, DEPLOYMENT_*, TOOLS_DB_PATH, UPLOAD_FOLDER, SCM_DO_BUILD_DURING_DEPLOYMENT, FLASK_SECRET_KEY"

step "Runtime configuration"
az webapp config set -g "$PLAN_RG" -n "$APP_NAME" --output none \
  --startup-file "gunicorn --bind=0.0.0.0 --timeout 120 --workers 1 --threads 4 app:app" \
  --always-on "$ALWAYS_ON" \
  --ftps-state FtpsOnly --min-tls-version 1.2 --http20-enabled true
az webapp update -g "$PLAN_RG" -n "$APP_NAME" --https-only true --output none
# One worker process: SQLite lives on the /home network share, where multi-process locking is unreliable
echo "gunicorn (1 worker, 4 threads), HTTPS only, TLS 1.2+, FTPS only"

step "Deploying $BRANCH"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$WORKDIR/src"
echo "commit: $(git -C "$WORKDIR/src" log -1 --format='%h %s')"
(cd "$WORKDIR/src" && zip -qr "$WORKDIR/app.zip" . -x '.git/*' 'tests/*' 'docs/*' 'deploy/*' 'instance/*')
az webapp deploy -g "$PLAN_RG" -n "$APP_NAME" --src-path "$WORKDIR/app.zip" --type zip --output none

URL="https://$(az webapp show -g "$PLAN_RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
step "Done"
echo "App: $URL"
echo "Logs: az webapp log tail -g $PLAN_RG -n $APP_NAME"
