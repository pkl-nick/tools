#!/usr/bin/env bash
# Store Stripe API keys in the toolshare web app's settings (Azure Cloud Shell, Bash):
#
#   bash set-stripe-keys.sh
#
# Asks for the keys interactively; secrets are hidden while typing and never printed.
# Run once with test keys (pk_test / sk_test or rk_test), and again with live keys at launch.

set -euo pipefail

APP_NAME="${APP_NAME:-toolshare-app}"
APP_RG="nick-portfolio-rg"

fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

az webapp show -g "$APP_RG" -n "$APP_NAME" --query name -o tsv >/dev/null || fail "Web app $APP_NAME not found"

read -rp "Publishable key (pk_...): " PK
[[ "$PK" == pk_* ]] || fail "That doesn't look like a publishable key"
read -rsp "Secret or restricted key (sk_... or rk_..., hidden): " SK; echo
[[ "$SK" == sk_* || "$SK" == rk_* ]] || fail "That doesn't look like a secret or restricted key"
read -rsp "Webhook signing secret (whsec_..., hidden, blank to skip): " WH; echo
[ -z "$WH" ] || [[ "$WH" == whsec_* ]] || fail "That doesn't look like a webhook signing secret"

MODE=$([[ "$SK" == *_live_* ]] && echo live || echo test)
[[ ("$MODE" == live && "$PK" == pk_live_*) || ("$MODE" == test && "$PK" == pk_test_*) ]] \
  || fail "Publishable and secret keys are from different modes (test vs live)"

SETTINGS=("STRIPE_PUBLISHABLE_KEY=$PK" "STRIPE_SECRET_KEY=$SK")
[ -z "$WH" ] || SETTINGS+=("STRIPE_WEBHOOK_SECRET=$WH")
az webapp config appsettings set -g "$APP_RG" -n "$APP_NAME" --settings "${SETTINGS[@]}" --output none
STORED="$MODE-mode Stripe keys${WH:+ and webhook secret}"
unset SK WH SETTINGS
echo "Stored $STORED on $APP_NAME (the app restarts)."
