#!/usr/bin/env bash
# Deploy toolshare to Azure App Service from Azure Cloud Shell (Bash).
#
#   bash deploy/azure-deploy.sh
#
# - Puts a new web app on nick-portfolio-plan, shared with the portfolio (no extra plan cost)
# - Wires it to the nraoai Azure OpenAI resource (key read straight from Azure, never printed)
# - Checks the GPT-5.6 deployments exist; photos go to Sol, video frames to Luna
# - Creates private blob containers in nradls and gives the app's managed identity access to just those
# - Creates an Azure Maps account (ZIP codes -> city/state) the app reads with its managed identity
# - Clones this repo and zip-deploys it; Azure installs requirements.txt during the deploy
#
# Safe to re-run: it updates settings and redeploys the latest code on BRANCH.

set -euo pipefail

# ------------------------------------------------------------------ settings
APP_NAME="${APP_NAME:-toolshare-app}"     # -> https://toolshare-app.azurewebsites.net ("toolshare" is taken)
PLAN_NAME="nick-portfolio-plan"           # existing Linux B1 plan, shared with the portfolio
PLAN_RG="nick-portfolio-rg"
AOAI_NAME="nraoai"                        # Azure OpenAI resource with the GPT-5.6 deployments
REPO_URL="https://github.com/pkl-nick/tools.git"
BRANCH="claude/tool-sharing-platform-research-xzdoea"
RUNTIME="PYTHON:3.11"
DEPLOYMENTS=(gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol)
STORAGE_ACCOUNT="nradls"                  # existing storage account (also used by the portfolio)
MEDIA_CONTAINER="toolshare-media"         # listing + profile photos (private; served through the app)
BACKUP_CONTAINER="toolshare-backups"      # database backups from the admin page
PHOTO_TIER="sol"                          # photos are read by Sol
VIDEO_TIER="luna"                         # video frames are read by Luna
MAPS_NAME="toolshare-maps"                # Azure Maps (Gen2): ZIP + address lookups; free tier covers a small app
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

step "Subscription"
az account show --query "{subscription:name, id:id}" -o table

step "Checking App Service plan $PLAN_NAME"
PLAN_SKU=$(az appservice plan show -g "$PLAN_RG" -n "$PLAN_NAME" --query sku.name -o tsv)
[ -n "$PLAN_SKU" ] || fail "Plan $PLAN_NAME not found in $PLAN_RG"
echo "plan: $PLAN_NAME  rg: $PLAN_RG  sku: $PLAN_SKU"
ALWAYS_ON=true
case "$PLAN_SKU" in
  F1|D1) ALWAYS_ON=false
         echo "Note: $PLAN_SKU is a free/shared tier: no Always On, and CPU minutes are capped per day." ;;
esac

step "Creating web app $APP_NAME (skipped if it already exists)"
if az webapp show -g "$PLAN_RG" -n "$APP_NAME" --query name -o tsv >/dev/null 2>&1; then
  echo "exists"
else
  az webapp create -g "$PLAN_RG" -p "$PLAN_NAME" -n "$APP_NAME" --runtime "$RUNTIME" --output none \
    || fail "Could not create $APP_NAME. If the name is taken, re-run with APP_NAME=<another-name> bash azure-deploy.sh"
  echo "created"
fi

step "Azure OpenAI resource $AOAI_NAME"
AOAI_RG=$(az cognitiveservices account list --query "[?name=='$AOAI_NAME'].resourceGroup | [0]" -o tsv)
[ -n "$AOAI_RG" ] || fail "Azure OpenAI resource $AOAI_NAME not found in this subscription"
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

step "Blob Storage: $STORAGE_ACCOUNT"
STORAGE_ID=$(az storage account list --query "[?name=='$STORAGE_ACCOUNT'].id | [0]" -o tsv)
[ -n "$STORAGE_ID" ] || fail "Storage account $STORAGE_ACCOUNT not found in this subscription"
STORAGE_RG=$(az storage account list --query "[?name=='$STORAGE_ACCOUNT'].resourceGroup | [0]" -o tsv)
for c in "$MEDIA_CONTAINER" "$BACKUP_CONTAINER"; do
  # container-rm goes through Azure Resource Manager, so no storage key or data-plane role is needed here
  if [ "$(az storage container-rm exists --storage-account "$STORAGE_ACCOUNT" -g "$STORAGE_RG" -n "$c" --query exists -o tsv)" = "true" ]; then
    echo "container $c exists"
  else
    az storage container-rm create --storage-account "$STORAGE_ACCOUNT" -g "$STORAGE_RG" -n "$c" --public-access off --output none
    echo "container $c created (private)"
  fi
done

step "Managed identity for $APP_NAME"
PRINCIPAL_ID=$(az webapp identity assign -g "$PLAN_RG" -n "$APP_NAME" --query principalId -o tsv)
echo "principal: $PRINCIPAL_ID"
for c in "$MEDIA_CONTAINER" "$BACKUP_CONTAINER"; do
  SCOPE="$STORAGE_ID/blobServices/default/containers/$c"
  # Scoped to each container, so the app can't touch the portfolio's blobs
  if [ "$(az role assignment list --assignee "$PRINCIPAL_ID" --scope "$SCOPE" --role "Storage Blob Data Contributor" --query "length(@)" -o tsv)" != "0" ]; then
    echo "role on $c already assigned"
  else
    az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
      --role "Storage Blob Data Contributor" --scope "$SCOPE" --output none
    echo "Storage Blob Data Contributor on $c (can take a few minutes to take effect)"
  fi
done

step "Azure Maps: $MAPS_NAME"
if az maps account show -g "$PLAN_RG" -n "$MAPS_NAME" --query name -o tsv >/dev/null 2>&1; then
  echo "exists"
else
  az maps account create -g "$PLAN_RG" -n "$MAPS_NAME" --sku G2 --kind Gen2 --accept-tos --output none 2>/dev/null \
    || az maps account create -g "$PLAN_RG" -n "$MAPS_NAME" --sku G2 --kind Gen2 --location eastus --accept-tos --output none \
    || fail "Could not create Azure Maps account $MAPS_NAME"
  echo "created"
fi
MAPS_ID=$(az maps account show -g "$PLAN_RG" -n "$MAPS_NAME" --query id -o tsv)
MAPS_CLIENT_ID=$(az maps account show -g "$PLAN_RG" -n "$MAPS_NAME" --query properties.uniqueId -o tsv)
if [ "$(az role assignment list --assignee "$PRINCIPAL_ID" --scope "$MAPS_ID" --role "Azure Maps Data Reader" --query "length(@)" -o tsv)" != "0" ]; then
  echo "Azure Maps Data Reader already assigned"
else
  az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
    --role "Azure Maps Data Reader" --scope "$MAPS_ID" --output none
  echo "Azure Maps Data Reader assigned (no keys: the app signs in with its managed identity)"
fi

step "App settings (values are not printed)"
EXISTING_SETTINGS=$(az webapp config appsettings list -g "$PLAN_RG" -n "$APP_NAME" --query "[].name" -o tsv)
SETTINGS=(
  "ENDPOINT_URL=$AOAI_ENDPOINT"
  "AZURE_OPENAI_API_KEY=$AOAI_KEY"
  "DEPLOYMENT_LUNA=${DEPLOYMENTS[0]}"
  "DEPLOYMENT_TERRA=${DEPLOYMENTS[1]}"
  "DEPLOYMENT_SOL=${DEPLOYMENTS[2]}"
  "PHOTO_TIER=$PHOTO_TIER"
  "VIDEO_TIER=$VIDEO_TIER"
  "TOOLS_DB_PATH=/home/data/tools.db"
  "UPLOAD_FOLDER=/home/data/uploads"
  "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
  "AZURE_STORAGE_ACCOUNT=$STORAGE_ACCOUNT"
  "MEDIA_CONTAINER=$MEDIA_CONTAINER"
  "BACKUP_CONTAINER=$BACKUP_CONTAINER"
  "AZURE_MAPS_CLIENT_ID=$MAPS_CLIENT_ID"
)
# Keep the secret key stable across re-runs so logins/sessions survive redeploys
if ! grep -qx "FLASK_SECRET_KEY" <<< "$EXISTING_SETTINGS"; then
  SETTINGS+=("FLASK_SECRET_KEY=$(openssl rand -hex 32)")
fi
# Key for the /eval test bench, so strangers can't run up model costs
if ! grep -qx "EVAL_KEY" <<< "$EXISTING_SETTINGS"; then
  SETTINGS+=("EVAL_KEY=$(openssl rand -hex 8)")
fi
# Password for /admin (metrics dashboard); generated once, then kept
if ! grep -qx "ADMIN_PASSWORD" <<< "$EXISTING_SETTINGS"; then
  SETTINGS+=("ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)")
fi
az webapp config appsettings set -g "$PLAN_RG" -n "$APP_NAME" --settings "${SETTINGS[@]}" --output none
unset AOAI_KEY SETTINGS
echo "set: ENDPOINT_URL, AZURE_OPENAI_API_KEY, DEPLOYMENT_*, PHOTO_TIER=$PHOTO_TIER, VIDEO_TIER=$VIDEO_TIER, TOOLS_DB_PATH, UPLOAD_FOLDER, SCM_DO_BUILD_DURING_DEPLOYMENT, AZURE_STORAGE_ACCOUNT, *_CONTAINER, AZURE_MAPS_CLIENT_ID, FLASK_SECRET_KEY, EVAL_KEY, ADMIN_PASSWORD"

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
(cd "$WORKDIR/src" && zip -qr "$WORKDIR/app.zip" . -x '.git/*' 'tests/*' 'docs/*' 'deploy/*' 'instance/*' 'evals/results/*')
az webapp deploy -g "$PLAN_RG" -n "$APP_NAME" --src-path "$WORKDIR/app.zip" --type zip --output none

URL="https://$(az webapp show -g "$PLAN_RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
step "Done"
EVAL_KEY_VALUE=$(az webapp config appsettings list -g "$PLAN_RG" -n "$APP_NAME" --query "[?name=='EVAL_KEY'].value | [0]" -o tsv)
echo "App: $URL"
ADMIN_PASSWORD_VALUE=$(az webapp config appsettings list -g "$PLAN_RG" -n "$APP_NAME" --query "[?name=='ADMIN_PASSWORD'].value | [0]" -o tsv)
echo "Test bench: $URL/eval?key=$EVAL_KEY_VALUE   (keep this link private; runs cost model credits)"
echo "Admin dashboard: $URL/admin   password: $ADMIN_PASSWORD_VALUE   (store it in a password manager)"
echo "Logs: az webapp log tail -g $PLAN_RG -n $APP_NAME"
