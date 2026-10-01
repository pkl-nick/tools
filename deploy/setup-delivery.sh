#!/usr/bin/env bash
# Connect toolshare to Uber Direct (courier delivery) from Azure Cloud Shell (Bash):
#
#   bash setup-delivery.sh
#
# Asks for the values from the Uber Direct dashboard (direct.uber.com -> Developer); secrets are
# hidden while typing and never printed. Couriers stay in TEST mode (Uber's "Robocourier", no real
# driver, no charge) until you re-run with COURIER_LIVE=1:
#
#   COURIER_LIVE=1 bash setup-delivery.sh
#
# Until payments are built, every live trip is paid by you, so only go live when you mean it.

set -euo pipefail

APP_NAME="${APP_NAME:-toolshare-app}"
APP_RG="nick-portfolio-rg"
COURIER_LIVE="${COURIER_LIVE:-0}"

fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

az webapp show -g "$APP_RG" -n "$APP_NAME" --query name -o tsv >/dev/null || fail "Web app $APP_NAME not found"
URL="https://$(az webapp show -g "$APP_RG" -n "$APP_NAME" --query defaultHostName -o tsv)"

if [ "$COURIER_LIVE" = "1" ]; then
  read -rp "Turn on REAL couriers billed to your Uber account? Type 'live' to confirm: " OK
  [ "$OK" = "live" ] || fail "Cancelled"
  az webapp config appsettings set -g "$APP_RG" -n "$APP_NAME" --settings "COURIER_LIVE=1" --output none
  echo "Live couriers are on for $APP_NAME. Turn them off with: az webapp config appsettings delete -g $APP_RG -n $APP_NAME --setting-names COURIER_LIVE"
  exit 0
fi

echo "From direct.uber.com -> Developer (use the test/sandbox values first):"
read -rp "Customer ID: " CUSTOMER_ID
[[ "$CUSTOMER_ID" =~ ^[A-Za-z0-9-]{8,}$ ]] || fail "That doesn't look like a customer ID"
read -rp "Client ID: " CLIENT_ID
[ -n "$CLIENT_ID" ] || fail "Client ID is required"
read -rsp "Client secret (hidden): " CLIENT_SECRET; echo
[ -n "$CLIENT_SECRET" ] || fail "Client secret is required"

echo
echo "Now add a webhook in the dashboard (Developer -> Webhooks -> Create):"
echo "  URL:    $URL/webhooks/uber-direct"
echo "  Events: delivery status (event.delivery_status) and courier update (event.courier_update)"
read -rsp "Webhook signing key (hidden, blank to add later): " WEBHOOK_KEY; echo

SETTINGS=("UBER_DIRECT_CUSTOMER_ID=$CUSTOMER_ID" "UBER_DIRECT_CLIENT_ID=$CLIENT_ID" "UBER_DIRECT_CLIENT_SECRET=$CLIENT_SECRET")
[ -z "$WEBHOOK_KEY" ] || SETTINGS+=("UBER_DIRECT_WEBHOOK_KEY=$WEBHOOK_KEY")
az webapp config appsettings set -g "$APP_RG" -n "$APP_NAME" --settings "${SETTINGS[@]}" --output none
STORED="Uber Direct credentials${WEBHOOK_KEY:+ and webhook key}"
unset CLIENT_SECRET WEBHOOK_KEY SETTINGS
echo "Stored $STORED on $APP_NAME (the app restarts). Couriers are in test mode."
echo
echo "To try it: as an owner, add a street address and mobile number under Account; as a renter, pick"
echo "\"courier (Uber) both ways\" on a tool that fits in a car, then accept and tap \"send with courier\"."
