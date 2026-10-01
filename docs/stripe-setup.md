# Stripe setup checklist (your side)

Do everything in **test mode** first. The integration is built and tested against test keys, and live keys come last.

## 1. Account and business
- [ ] Create a Stripe account at https://dashboard.stripe.com/register. You can start as a sole proprietor with your SSN and switch to an LLC/EIN later.
- [ ] Fill in the business profile:
  - **Description:** "online marketplace where neighbors rent tools to each other"
  - **Website:** `https://toolshare-app.azurewebsites.net` for now
  - **Support email and phone**
- [ ] Add the **bank account** where platform revenue gets paid out.
- [ ] Turn on **two-step authentication** for your Stripe login.

## 2. Turn on Connect (the marketplace part)
- [ ] Dashboard → **Connect** → Get started → choose **Marketplace**.
- [ ] Answer the **platform profile** questions:
  - Sellers are **individuals renting out their own tools**.
  - Customers pay the platform, and funds go to sellers (destination charges).
  - The platform takes a fee on each transaction.
  - **Who's responsible for losses** (refunds, chargebacks, negative balances): choose **the platform**. This is the usual setup for Express-style accounts, and it means toolshare covers a bad debt if an owner's balance goes negative.
- [ ] Connect → **Settings**:
  - **Branding:** name "toolshare", icon, and brand color `#7A1FA2`. These appear on owner onboarding.
  - **Onboarding:** Stripe-hosted, with the Express dashboard for owners.
  - **Capabilities:** request `card_payments` and `transfers` for connected accounts.
  - **Payout schedule:** weekly. Fewer payouts means lower per-payout fees on small rentals.
  - **Tax reporting (US 1099-K):** let Stripe file and deliver 1099s for connected accounts. Check the thresholds that apply for the current tax year.

## 3. Payment methods
- [ ] Settings → Payment methods: **Cards**, **Apple Pay**, **Google Pay** and **Link** on. Leave bank debits off; they can't be held or charged later the same way.
- [ ] Apple Pay needs your **domain verified**. Do it once a custom domain exists. The azurewebsites.net address works for testing.

## 4. Fraud and identity
- [ ] **Radar** is on by default. Leave the default rules.
- [ ] *(Recommended)* Turn on **Stripe Identity** for ID checks on a renter's first rental or on high-value tools (it charges per verification; check current pricing).

## 5. Webhook (how Stripe tells the app what happened)
- [ ] Developers → Webhooks → **Add endpoint**
  - URL: `https://toolshare-app.azurewebsites.net/stripe/webhook`
  - Tick **"Listen to events on Connected accounts"** as well, for owner-side events
  - Events:
    - `setup_intent.succeeded`
    - `payment_intent.succeeded`
    - `payment_intent.payment_failed`
    - `charge.refunded`
    - `charge.dispute.created`
    - `account.updated`
    - `payout.failed`
- [ ] Copy the endpoint's **signing secret** (`whsec_...`).

## 6. Keys into Azure (never into chat or the repo)
- [ ] Developers → API keys (test mode): copy the **Publishable key** (`pk_test_...`) and **Secret key** (`sk_test_...`). Even better, create a **restricted key** (`rk_test_...`) with write access to PaymentIntents, SetupIntents, Customers, Accounts, Account Links, Transfers, Refunds and Identity, plus read access to Balance and Events.
- [ ] In Cloud Shell:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/claude/tool-sharing-platform-research-xzdoea/deploy/set-stripe-keys.sh -o set-stripe-keys.sh
  bash set-stripe-keys.sh
  ```
  It asks for the three values, keeps the secrets hidden as you type, and stores them as app settings.

## 7. Legal pages (before live mode)
- [ ] Terms of service that **link to the [Stripe Connected Account Agreement](https://stripe.com/connect-account/legal)**, and that cover the protection fee, renter damage charges and late fees. Have a lawyer review these.
- [ ] Privacy policy.

## 8. Going live
- [ ] Activate live mode in the Dashboard (finish the identity and bank checks).
- [ ] Create the **live** webhook endpoint and keys, then run `set-stripe-keys.sh` again with the live values.
