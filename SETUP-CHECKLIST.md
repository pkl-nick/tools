# toolshare setup checklist

Everything that needs you: accounts, consoles, secrets and decisions I can't do from here. Work top to bottom; later parts depend on earlier ones.

**Before you start**
- **Time:** about 2½–3½ hours of hands-on work, plus waiting on Uber and Stripe account review.
- **Where commands run:** Azure Cloud Shell (Bash), at https://shell.azure.com.
- **Script links:** every link is pinned to commit `c708605`, so you always get the version described here.
- **Secrets:** every script asks for secrets with hidden typing and stores them as App Service settings. Never paste a secret into chat, email or the repo.

| Part | What | Time | Blocks |
|---|---|---|---|
| 1 | Redeploy to Azure (Azure Maps, search, delivery code) | 20 min | everything else |
| 2 | Microsoft sign-in | 5 min | — |
| 3 | Google sign-in | 15 min | — |
| 4 | Uber Direct courier delivery | 45 min + approval wait | — |
| 5 | Stripe account and Connect (prep only) | 45 min + verification wait | payments build |
| 6 | End-to-end smoke test | 30 min | — |
| 7 | Decisions I need from you | 15 min | payments build |
| 8 | Housekeeping (budget alert, reminders, backups) | 15 min | — |
| 9 | Later, before real users | — | launch |

**Values you'll use all day**

| What | Value |
|---|---|
| App | https://toolshare-app.azurewebsites.net |
| Resource group / plan | `nick-portfolio-rg` / `nick-portfolio-plan` |
| Web app name | `toolshare-app` |
| Azure OpenAI | `nraoai` (deployments `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`) |
| Storage | `nradls` (containers `toolshare-media`, `toolshare-backups`) |
| Azure Maps | `toolshare-maps` (created in Part 1) |
| Google redirect URI | `https://toolshare-app.azurewebsites.net/auth/google/callback` |
| Microsoft redirect URI | `https://toolshare-app.azurewebsites.net/auth/microsoft/callback` (set by script) |
| Uber webhook URL | `https://toolshare-app.azurewebsites.net/webhooks/uber-direct` |
| Stripe webhook URL (later) | `https://toolshare-app.azurewebsites.net/stripe/webhook` |

---

## Part 1 — Redeploy to Azure (do this first)

This puts the last three rounds of work live: accounts, sign-in, ZIP codes via Azure Maps, Uber delivery and indexed search. The script also creates the Azure Maps account and gives the app access to it.

- [ ] **1.1** Open Cloud Shell (Bash) and check you're in the right subscription:
  ```bash
  az account show --query "{name:name, id:id}" -o table
  ```
  If it's wrong: `az account set --subscription "<name or id>"`.

- [ ] **1.2** *(First time only; harmless to repeat)* Register the Azure Maps resource provider, since new subscriptions often don't have it:
  ```bash
  az provider register --namespace Microsoft.Maps --wait
  ```

- [ ] **1.3** Run the deploy:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/azure-deploy.sh -o azure-deploy.sh && bash azure-deploy.sh
  ```
  Expect about 5–10 minutes. In the output, look for:
  - `ok` next to all three GPT-5.6 deployments. A `MISSING` line means it needs creating in the Azure OpenAI portal.
  - `Azure Maps: toolshare-maps`, then `created` (or `exists`), then `Azure Maps Data Reader assigned`.
  - At the end: the **Test bench** link and the **Admin dashboard password**.

- [ ] **1.4** Save these to your password manager:
  - **Admin password.** The admin page is at `/admin/login`.
  - **Test-bench link.** It looks like `/eval?key=…`. Keep it private, because each run spends model credits.

- [ ] **1.5** Wait about 5 minutes for the role assignments to take effect, then verify:
  - [ ] **Site loads:** https://toolshare-app.azurewebsites.net shows listings.
  - [ ] **Search indexes:** log in at `/admin/login`. The "top searches" panel should say **`search: R*Tree + FTS5`**.
  - [ ] **Search works:** search **weed eater**. "String Trimmer" should come up, via its alternate-name keywords.

  You'll check ZIP lookup in Part 6, when you create an account.

**If something fails**
| Symptom | Fix |
|---|---|
| `Could not create Azure Maps account` | Run step 1.2, then re-run the deploy. Check that `az maps account list -o table` works. |
| Photos fail to upload right after deploy | Role assignment still propagating. Wait 5–10 minutes. |
| ZIP "couldn't find" for a real ZIP | Maps role not active yet. Wait 10 minutes. Check the app setting `AZURE_MAPS_CLIENT_ID` is set (command below). |
| App shows an error page | `az webapp log tail -g nick-portfolio-rg -n toolshare-app` and send me the error lines. |

Check which settings exist (names only; no values are printed):
```bash
az webapp config appsettings list -g nick-portfolio-rg -n toolshare-app --query "[].name" -o tsv | sort
```

---

## Part 2 — Microsoft sign-in (automatic)

The script registers an app in your Microsoft Entra directory, open to both personal Microsoft accounts (Outlook, Hotmail, Xbox) and work/school accounts. It then stores the app's id and a 2-year secret in App Service.

- [ ] **2.1** In Cloud Shell, run the script below. The Google questions come up in Part 3, so you can do both in one run; if you don't have the Google values yet, press Enter at the Google prompt to skip it.
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/setup-sign-in.sh -o setup-sign-in.sh && bash setup-sign-in.sh
  ```
  - It prints the Microsoft **client id** and redirect URI, then `new client secret stored (expires …)`.
  - If you get `Insufficient privileges`: your account can't create app registrations in this directory. Ask the directory admin, or use the Entra admin center → **App registrations** → *New registration* with the same settings (I can walk you through it).

- [ ] **2.2** Put the **secret expiry date** in your calendar, about 2 years out, minus a month. To rotate:
  ```bash
  RESET_MICROSOFT_SECRET=1 bash setup-sign-in.sh
  ```

- [ ] **2.3** Verify: open `/login` in a private window and check there's a **Sign in with Microsoft** button. Sign in with a personal Outlook/Hotmail account. You should land on the "one more step" page asking for neighborhood and ZIP.

---

## Part 3 — Google sign-in (Google Cloud Console)

Google has no command line for this, so it's done in the browser. Only the basic scopes are used (`openid`, `email`, `profile`), which **don't need Google's app review**.

- [ ] **3.1** Go to https://console.cloud.google.com and sign in with the Google account that should own this. Top bar → project picker → **New project** → name it `toolshare` → **Create** → select it.

- [ ] **3.2** Left menu → **APIs & Services** → **OAuth consent screen**. Newer consoles call this **Google Auth Platform**, with Branding / Audience / Clients pages. Click **Get started** and fill in:
  - **App name:** `toolshare`
  - **User support email:** your email
  - **Audience:** **External**
  - **Contact email:** your email → agree → **Create**

- [ ] **3.3** **Branding** (optional for now):
  - Leave the logo empty. Uploading one triggers Google's brand verification.
  - Leave homepage, privacy policy and terms empty until you have those pages (Part 9).
  - If you're asked for an **authorized domain** and `azurewebsites.net` is rejected, leave it out. Shared hosting domains often can't be claimed, and basic sign-in works without one.

- [ ] **3.4** **Data access / Scopes:** add `openid`, `.../auth/userinfo.email` and `.../auth/userinfo.profile`, or just leave the defaults. Nothing sensitive is requested.

- [ ] **3.5** **Audience** → **Publish app**, so the status changes from *Testing* to **In production**. This is important: in Testing, only test users you list can sign in. For these basic scopes, publishing doesn't require verification.

- [ ] **3.6** **Clients** (or **Credentials** → *Create credentials* → **OAuth client ID**):
  - **Application type:** Web application
  - **Name:** `toolshare web`
  - **Authorized JavaScript origins:** `https://toolshare-app.azurewebsites.net`
  - **Authorized redirect URIs:** `https://toolshare-app.azurewebsites.net/auth/google/callback`. It must match exactly: https, no trailing slash.
  - Click **Create**, then copy the **Client ID** and **Client secret**. The secret may only be shown once; you can download the JSON as a backup, but keep it out of the repo.

- [ ] **3.7** Store them by running the script again. It skips the Microsoft part since that's already stored, and asks for Google:
  ```bash
  bash setup-sign-in.sh
  ```
  Paste the client ID, then the secret (hidden as you type). If Google was already stored and you need to replace it:
  ```bash
  RESET_GOOGLE=1 bash setup-sign-in.sh
  ```

- [ ] **3.8** Verify in a private window:
  - `/login` shows **continue with Google**.
  - Signing in lands on the "one more step" page.
  - If you already have an email/password account with the same Gmail address, signing in with Google links to it, because Google verifies emails.

**If something fails**
| Error | Fix |
|---|---|
| `redirect_uri_mismatch` | The redirect URI in 3.6 doesn't exactly match. Check https, the `/auth/google/callback` path and no trailing slash. |
| "Access blocked: app is in testing" | Step 3.5 wasn't done. |
| No Google button on `/login` | Settings not stored. Check that `GOOGLE_CLIENT_ID` appears in the settings list from Part 1. |

---

## Part 4 — Uber Direct (courier delivery)

Trips stay in **test mode** (Uber's automated "Robocourier": no driver, no charge) until you deliberately turn live mode on. The full flow is described in `docs/delivery.md`.

### 4A. Account
- [ ] **4.1** Go to https://direct.uber.com → **Get started / Sign up**. Use your business details: name, address, a phone number Uber can verify, and the business email you want billing at. Sole proprietor is fine to start, same as Stripe.
- [ ] **4.2** If Uber asks what you're delivering or about your use case, describe it as "neighbor-to-neighbor tool rental marketplace, on-demand delivery and return of hand-carried power tools (typically 5–40 lb) within a metro area."
- [ ] **4.3** Note whether your account gets **self-serve API access** right away, or needs a sales/onboarding call for production. Test credentials are usually available right away. Production ("live") access may need Uber's approval and a billing method on file. Neither blocks today's test setup.

### 4B. API credentials (test environment)
- [ ] **4.4** Dashboard → **Developer** (sometimes under *Settings → Developer*). Make sure the environment toggle is on **Test** / sandbox, then copy:
  - **Customer ID** (a UUID-like id)
  - **Client ID**
  - **Client secret** (hidden from here on)
- [ ] **4.5** Check that the API scope includes **`eats.deliveries`**, which is what the app requests. It's the default for Direct API keys.

### 4C. Store credentials and set up the webhook
- [ ] **4.6** In Cloud Shell:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/setup-delivery.sh -o setup-delivery.sh && bash setup-delivery.sh
  ```
  It asks for the Customer ID, Client ID and secret, then **pauses and prints the webhook URL**.
- [ ] **4.7** While it waits, open the Uber dashboard → **Developer → Webhooks → Create webhook**, still in the test environment:
  - **URL:** `https://toolshare-app.azurewebsites.net/webhooks/uber-direct`
  - **Events:** **delivery status** (`event.delivery_status`) and **courier update** (`event.courier_update`)
  - Save, then copy the webhook's **signing key / secret**.
- [ ] **4.8** Back in Cloud Shell, paste the signing key (hidden). The script stores everything and says *"Couriers are in test mode."*
  - If you press Enter without a key, deliveries still work, but status won't update in My garage (the app rejects unsigned updates). Re-run the script later to add it.

### 4D. Prepare a test, then run it in Part 6
- [ ] **4.9** Pick **two real addresses** in a city Uber Direct serves, such as your home and a friend's or a nearby store. Quotes fail for addresses outside Uber's area, even in test mode.
- [ ] **4.10** Have **two mobile numbers** ready (yours twice is fine, in any US format; the app converts them).

### 4E. Later: going live (not tomorrow)
- [ ] Only after Stripe payments are built. Until then, every live trip is paid by you with nothing collected from the renter.
- [ ] Get Uber's production approval. Then re-run `setup-delivery.sh` with the **production** credentials and production webhook key, and create the webhook again in the production environment.
- [ ] Turn on live couriers (it asks you to type `live`):
  ```bash
  COURIER_LIVE=1 bash setup-delivery.sh
  ```
  To turn it off:
  ```bash
  az webapp config appsettings delete -g nick-portfolio-rg -n toolshare-app --setting-names COURIER_LIVE
  ```
- **Safety net:** if production credentials are stored but live mode is off, the app cancels any trip Uber reports as live and shows an error. Nobody gets billed by accident.

**If something fails**
| Message in the app | Meaning / fix |
|---|---|
| "Courier delivery isn't available for this tool" | One of: Uber settings missing; tool too big or heavy (the AI sizes each tool; the sample listings with "too big" sizes never qualify); or the **owner** hasn't saved a street address and phone under Account. |
| "Uber couldn't quote this trip: …" | Usually an address outside Uber's service area or a typo. Uber's message is shown after the colon. |
| "Uber auth failed (401)" | Wrong client ID or secret, or test vs production mixed up. Re-run 4.6. |
| Status stuck on "pending" | Webhook not set, or wrong signing key. Recheck 4.7–4.8. |

---

## Part 5 — Stripe (account prep only; payments aren't built yet)

**What this part is for:** getting the slow parts (identity checks, Connect approval, bank account) moving now, so they're done by the time payments are built. Nothing in the app uses Stripe yet. The full reference is `docs/stripe-setup.md`; this is the do-it-tomorrow version.

### 5A. Account
- [ ] **5.1** Sign up at https://dashboard.stripe.com/register. Stay in **Test mode**: the toggle is at the top right.
- [ ] **5.2** Turn on **two-step authentication** (Settings → Personal details / Security).
- [ ] **5.3** Settings → **Business details**:
  - **Type:** sole proprietor (SSN) is fine. You can switch to an LLC/EIN later.
  - **Description:** "Online marketplace where neighbors rent tools to each other"
  - **Website:** `https://toolshare-app.azurewebsites.net`
  - **Support email and phone**
- [ ] **5.4** Settings → **Bank accounts and scheduling**: add the bank account where platform revenue is paid out.

### 5B. Connect (the marketplace part)
- [ ] **5.5** Dashboard → **Connect** → **Get started** → choose **Marketplace**.
- [ ] **5.6** Platform profile answers:
  - **Who sells:** individuals renting out their own tools
  - **Payment flow:** customers pay the platform, and funds go to sellers (**destination charges**)
  - **Fees:** the platform takes a fee on each transaction
  - **Losses** (refunds, chargebacks, negative balances): **the platform** is responsible
- [ ] **5.7** Connect → **Settings**:
  - **Branding:** name `toolshare`, brand color `#7A1FA2`, and an icon if you have one
  - **Onboarding:** Stripe-hosted, with the **Express** dashboard for owners
  - **Capabilities:** `card_payments` and `transfers`
  - **Payouts:** **weekly**, which cuts per-payout fees on small rentals
  - **Tax forms:** let Stripe file and deliver **1099-K**s. Check this year's thresholds on the page.

### 5C. Payment methods and fraud
- [ ] **5.8** Settings → **Payment methods**:
  - **On:** Cards, Apple Pay, Google Pay, Link
  - **Off:** bank debits (ACH)
- [ ] **5.9** Radar: leave the defaults on.
- [ ] **5.10** *(Optional; can decide later)* Look at **Stripe Identity** pricing for ID checks on a renter's first rental. It feeds decision 7.1 below.

### 5D. Keys into Azure (test mode)
- [ ] **5.11** Developers → **API keys** (test). Recommended: **Create restricted key** (`rk_test_…`) with **write** access to PaymentIntents, SetupIntents, Customers, Accounts, Account Links, Transfers, Refunds and Identity, and **read** access to Balance and Events. Otherwise, use the standard secret key `sk_test_…`.
- [ ] **5.12** In Cloud Shell. Have the publishable key (`pk_test_…`) and the secret or restricted key ready. **Leave the webhook secret blank** (press Enter).
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/set-stripe-keys.sh -o set-stripe-keys.sh && bash set-stripe-keys.sh
  ```

### 5E. Deferred until I build payments: don't do these tomorrow
- [ ] ⏸ **Webhook endpoint** (`/stripe/webhook`). That route doesn't exist yet, so Stripe would keep getting "not found" and email you about failures. I'll tell you when to add it, with the event list from `docs/stripe-setup.md` §5.
- [ ] ⏸ **Apple Pay domain verification.** Needs the custom domain (Part 9).
- [ ] ⏸ **Live mode keys.** After payments are built and tested.

---

## Part 6 — End-to-end smoke test (after Parts 1–4)

Use a normal browser window for the **owner** and a private window for the **renter**. You can use two email addresses, or a Gmail alias like `you+renter@gmail.com` (Gmail delivers plus-addresses to the same inbox).

**Owner**
- [ ] **6.1** Sign up with email, neighborhood and **your real ZIP**. Allow location when asked, or decline; both should work. Then check:
  - [ ] Your profile shows "**Neighborhood · City, ST**". This proves Azure Maps works.
  - [ ] A bad ZIP like `00000` is rejected.
- [ ] **6.2** Account → **delivery**: tick "I can drop tools off myself" (optional), and add your **street address and mobile**. Save.
- [ ] **6.3** **Post** → take or upload a photo of a hand-carried tool, such as a drill. Then check the review page:
  - [ ] The draft has sensible name, price and safety fields.
  - [ ] It has a filled-in **"also found by searching"** field.
  - Approve it.
- [ ] **6.4** Upload a short walkthrough **video** and check that it produces drafts from frames.

**Renter**
- [ ] **6.5** In the private window, check search before signing up:
  - [ ] Typing a ZIP from another city in the "searching near" box changes the results.
  - [ ] Using the **owner's ZIP**, search for the tool by an alternate name from its keywords. It appears.
- [ ] **6.6** Sign up as the renter, using the **other Uber address's ZIP**. Try Google or Microsoft sign-in here to test both.
- [ ] **6.7** Open the owner's tool and choose **courier (Uber) both ways**:
  - Enter the second test address and phone, then **get courier price**. You should see a price with "Courier, both ways".
  - Click **request to rent**.

**Courier round trip**
- [ ] **6.8** Owner window → **My garage**:
  - **accept** the request, then **send with courier**.
  - It should say *"Test courier requested (Uber's Robocourier, no real driver)."*
- [ ] **6.9** Watch the status. Refresh **My garage** every minute or two: it should advance (pickup → dropoff → delivered), and the **track** link should open Uber's tracking page. The trip should also appear in the Uber dashboard's test deliveries.
- [ ] **6.10** Renter window → **My garage** → **courier it back**. Check that the return leg appears and advances.

**Admin**
- [ ] **6.11** Open `/admin` and check:
  - signups and posts are counted
  - the courier tile shows **test** trips
  - your searches are listed, and a search for something nobody has (try "snowblower") shows under **searches with no results**
- [ ] **6.12** Click **Back up now** and confirm a backup appears in the list.

Write down anything odd and send it to me. Screenshots help.

---

## Part 7 — Decisions I need from you

These unblock the payments build. My recommendations are in `docs/payments-plan.md`.

- [ ] **7.1 Deposits.** No default deposit, with an optional capped deposit (20–25% of value, max $150) for tools over about $500 (recommended)? Or something else?
- [ ] **7.2 Protection fee.** Flat **$1 per rental**, or **10% with a $0.50 minimum**?
- [ ] **7.3 Owner protection cap** per incident: **$500** to start?
- [ ] **7.4 Renter damage deductible:** $50, $100, or none?
- [ ] **7.5 Delivery cut.** Courier trips are built as Uber's fee **+ 15% + $1**. Keep that? Should toolshare also take a cut of **owner** drop-off fees (currently 0%)?
- [ ] **7.6 ID checks.** Use Stripe Identity on a renter's first rental, or only for tools over about $300?
- [ ] **7.7 Sample listings.** When should the 11 seeded sample listings disappear? It's one setting: `SHOW_DEMO_LISTINGS=0`. Suggestion: once about 20 real listings exist near you.
- [ ] **7.8 Launch area.** Is Denver still the first city? It sets the default map center and the "searching near" label for visitors.

---

## Part 8 — Housekeeping (15 minutes, worth it)

- [ ] **8.1 Budget alert.** Azure portal → **Cost Management + Billing** → **Budgets** → **Add**:
  - **Scope:** the subscription, or `nick-portfolio-rg` plus the resource group holding `nraoai`
  - **Amount:** e.g. $50/month
  - **Alerts:** at 50%, 80% and 100%, to your email

  This catches runaway AI or Maps costs early.
- [ ] **8.2 Calendar reminders:**
  - Microsoft sign-in secret expiry (from 2.2)
  - Check Azure OpenAI prices in a month and update `COST_PER_M` if they changed. The admin page's AI spend is an estimate.
- [ ] **8.3 Password manager entries:**
  - admin password and test-bench link (Part 1)
  - Google client secret (Part 3)
  - Uber client secret and webhook key (Part 4)
  - Stripe keys (Part 5)
- [ ] **8.4 Backups.** After the smoke test, click **Back up now** on `/admin` whenever you've made meaningful changes. Ask me if you want this automated (a daily timer is a small add).

---

## Part 9 — Later, before real users (not tomorrow)

- [ ] **Custom domain** (e.g. `toolshare.app` or similar). Buy it, add it to App Service with a free managed certificate, then:
  - re-run `setup-sign-in.sh` so the Microsoft redirect follows the new domain
  - add the new origin and redirect URI to the Google client
  - update the Uber webhook URL
  - verify the domain for Apple Pay in Stripe
- [ ] **Terms of service and privacy policy pages.** Stripe and Google both expect them. The terms should link to the Stripe Connected Account Agreement and cover the protection fee, damage charges and late fees. Have a lawyer review them; I can draft them for you.
- [ ] **Payments build.** I build this once Part 7 is answered, then you add the Stripe webhook (5E) and test with Stripe's test cards.
- [ ] **Uber live mode** (4E), after payments.
- [ ] **Email.** Password-reset and booking-notification emails need an email service (e.g. Azure Communication Services). That will be one more small setup script.
- [ ] **Scale trigger.** When you need a second web server, move from SQLite to Azure Database for PostgreSQL (see `docs/search.md`).

---

### Quick reference: the four scripts
```bash
# Deploy / redeploy (safe to re-run)
curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/azure-deploy.sh -o azure-deploy.sh && bash azure-deploy.sh
# Microsoft + Google sign-in
curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/setup-sign-in.sh -o setup-sign-in.sh && bash setup-sign-in.sh
# Uber Direct
curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/setup-delivery.sh -o setup-delivery.sh && bash setup-delivery.sh
# Stripe keys
curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/c7086057836d77b979e79f4deac8be2a4edc6592/deploy/set-stripe-keys.sh -o set-stripe-keys.sh && bash set-stripe-keys.sh
```
Live logs: `az webapp log tail -g nick-portfolio-rg -n toolshare-app`
