#!/usr/bin/env bash
# Set up "Sign in with Microsoft" and "Sign in with Google" for toolshare. Run in Azure Cloud Shell (Bash):
#
#   bash setup-sign-in.sh
#
# Microsoft: fully automatic. Registers an app in your Microsoft Entra directory that accepts
#   personal Microsoft accounts (Outlook, Hotmail, Xbox) and work/school accounts, then stores
#   its client id and a new client secret in the web app's settings.
# Google: Google has no command line for this, so the script prints the exact values to enter
#   in Google Cloud Console, then asks you to paste the client id and secret it gives you.
#
# Safe to re-run. Secrets are never printed. Changing app settings restarts the web app.

set -euo pipefail

APP_NAME="${APP_NAME:-toolshare-app}"
APP_RG="nick-portfolio-rg"
ENTRA_APP_NAME="toolshare sign-in"
SECRET_YEARS=2

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

HOST=$(az webapp show -g "$APP_RG" -n "$APP_NAME" --query defaultHostName -o tsv) \
  || fail "Web app $APP_NAME not found in $APP_RG"
URL="https://$HOST"
EXISTING=$(az webapp config appsettings list -g "$APP_RG" -n "$APP_NAME" --query "[].name" -o tsv)
echo "web app: $URL"

# ----------------------------------------------------------------------------- Microsoft
step "Microsoft sign-in"
MS_REDIRECT="$URL/auth/microsoft/callback"
OBJ_ID=$(az ad app list --display-name "$ENTRA_APP_NAME" --query "[0].id" -o tsv)
BODY=$(cat <<JSON
{
  "displayName": "$ENTRA_APP_NAME",
  "signInAudience": "AzureADandPersonalMicrosoftAccount",
  "api": { "requestedAccessTokenVersion": 2 },
  "web": { "redirectUris": ["$MS_REDIRECT"], "homePageUrl": "$URL" },
  "optionalClaims": { "idToken": [ { "name": "email", "essential": false } ] }
}
JSON
)
if [ -z "$OBJ_ID" ]; then
  OBJ_ID=$(az rest --method POST --uri "https://graph.microsoft.com/v1.0/applications" \
    --headers "Content-Type=application/json" --body "$BODY" --query id -o tsv)
  echo "registered app '$ENTRA_APP_NAME'"
else
  az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJ_ID" \
    --headers "Content-Type=application/json" --body "$BODY" --output none
  echo "app '$ENTRA_APP_NAME' exists; redirect URI and settings refreshed"
fi
MS_CLIENT_ID=$(az ad app show --id "$OBJ_ID" --query appId -o tsv)
az ad sp show --id "$MS_CLIENT_ID" --output none 2>/dev/null || az ad sp create --id "$MS_CLIENT_ID" --output none
echo "client id: $MS_CLIENT_ID"
echo "redirect:  $MS_REDIRECT"

if grep -qx "MICROSOFT_CLIENT_SECRET" <<< "$EXISTING" && [ "${RESET_MICROSOFT_SECRET:-0}" != "1" ]; then
  echo "client secret already stored (set RESET_MICROSOFT_SECRET=1 to issue a new one)"
  az webapp config appsettings set -g "$APP_RG" -n "$APP_NAME" --settings "MICROSOFT_CLIENT_ID=$MS_CLIENT_ID" --output none
else
  END=$(date -u -d "+$SECRET_YEARS years" +%Y-%m-%dT%H:%M:%SZ)
  MS_SECRET=$(az rest --method POST --uri "https://graph.microsoft.com/v1.0/applications/$OBJ_ID/addPassword" \
    --headers "Content-Type=application/json" \
    --body "{\"passwordCredential\": {\"displayName\": \"toolshare web app\", \"endDateTime\": \"$END\"}}" \
    --query secretText -o tsv)
  az webapp config appsettings set -g "$APP_RG" -n "$APP_NAME" --output none \
    --settings "MICROSOFT_CLIENT_ID=$MS_CLIENT_ID" "MICROSOFT_CLIENT_SECRET=$MS_SECRET"
  unset MS_SECRET
  echo "new client secret stored (expires $END; re-run with RESET_MICROSOFT_SECRET=1 before then)"
fi

# ----------------------------------------------------------------------------- Google
step "Google sign-in"
GOOGLE_REDIRECT="$URL/auth/google/callback"
if grep -qx "GOOGLE_CLIENT_SECRET" <<< "$EXISTING" && [ "${RESET_GOOGLE:-0}" != "1" ]; then
  echo "Google is already configured (set RESET_GOOGLE=1 to replace it)"
else
  cat <<EOF
Google has no command line for creating sign-in clients, so do this in the browser (about 5 minutes):

  1. Open https://console.cloud.google.com/ and create a project, e.g. "toolshare".
  2. Go to "APIs & Services" > "OAuth consent screen" (also called "Google Auth Platform"):
       - User type / audience: External
       - App name: toolshare, support email: yours
       - Scopes: openid, email, profile (the defaults; no Google review is needed for these)
       - Publish the app ("In production") so anyone can sign in, not only test users
  3. Go to "Credentials" > "Create credentials" > "OAuth client ID":
       - Application type: Web application
       - Authorized JavaScript origins:  $URL
       - Authorized redirect URIs:       $GOOGLE_REDIRECT
  4. Copy the Client ID and Client secret it shows you, then paste them below.

EOF
  read -rp "Google client ID (blank to skip for now): " GOOGLE_ID
  if [ -n "$GOOGLE_ID" ]; then
    read -rsp "Google client secret (hidden): " GOOGLE_SECRET; echo
    [ -n "$GOOGLE_SECRET" ] || fail "No secret entered"
    az webapp config appsettings set -g "$APP_RG" -n "$APP_NAME" --output none \
      --settings "GOOGLE_CLIENT_ID=$GOOGLE_ID" "GOOGLE_CLIENT_SECRET=$GOOGLE_SECRET"
    unset GOOGLE_SECRET
    echo "Google client stored"
  else
    echo "skipped; re-run this script when you have the Google client"
  fi
fi

step "Done"
echo "The web app restarts with the new settings. Open $URL/login to see the sign-in buttons."
echo "If you add a custom domain later, re-run this script so both redirect URIs follow it."
