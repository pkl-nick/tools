# Locations and delivery

## Where things are: ZIP codes, not just neighborhood names
A neighborhood name alone is ambiguous: there's a Capitol Hill in Denver, in Seattle and in Washington, D.C. Every member now gives a **ZIP code**, and the app resolves it with **Azure Maps** to a city, a state and a center point.

| Stored | Where it comes from | Who sees it |
|---|---|---|
| neighborhood | typed by the member | everyone ("Capitol Hill · Seattle, WA") |
| ZIP, city, state | ZIP → Azure Maps | city and state: everyone; ZIP: only the member |
| lat/lng (≈ a block) | browser location if shared, else the ZIP's center | nobody (used for distances) |
| street address, mobile | Account → delivery (optional) | only the courier on a trip the member booked or accepted |

- **Distances:** a member who skips browser location is placed at the center of their ZIP, not in Denver.
- **Visitors:** someone without an account can type a ZIP, or tap "use my location", to search near them.
- **Existing accounts** are asked once for a ZIP the next time they open a page.
- **Typos:** a ZIP that Azure Maps can't find is rejected.
- **Cost:** ZIP lookups are cached in memory (there are only ~42,000 US ZIPs). Typical use stays inside Azure Maps' free monthly allowance; check current pricing.
- **No keys:** `deploy/azure-deploy.sh` creates the Maps account (`toolshare-maps`, Gen2) and gives the web app's managed identity *Azure Maps Data Reader*. The app only needs `AZURE_MAPS_CLIENT_ID`.

## Three ways to get a tool
| Option | Who moves it | Price | Money goes to |
|---|---|---|---|
| Pickup | renter | free | — |
| Owner drop-off | owner, within 10 mi; owners can turn this off under Account | by distance and time of day | owner |
| **Courier (Uber Direct)**, both ways | Uber courier | Uber's two quotes + 15% + $1 | toolshare, which pays Uber |

### How a courier rental works
1. **Renter picks the courier option** on a tool that fits in a car and enters a delivery address. The app asks Uber for two quotes (out and back) and shows the total with the markup.
2. **Booking request.** The request saves the method, address and phone. The price is re-quoted when the request is sent.
3. **Owner accepts**, then taps **send with courier** in My garage when the tool is ready. This books the outbound trip.
4. **Renter taps courier it back** when finished. This books the return trip.
5. **Live status.** Each trip shows its status and Uber's tracking link in My garage. Uber's webhook keeps the status current.
6. **Cancellations.** Declining or cancelling the booking calls off any courier that hasn't finished.

### Limits
- **Fits in a car:** Uber Direct couriers drive cars; items should be under about 20 kg / 44 lb. Uber also prohibits some items, such as vehicle batteries.
- **AI sizing:** the listing AI estimates a **courier size** (small/medium/large/xlarge, or "too big") and weight for every tool.
- **Tools that don't qualify** (oversized, too heavy, or unsized like the old listings) only offer pickup or owner drop-off.
- **Owner details:** the owner needs a street address and mobile number in Account before the courier option appears.
- **Quotes expire:** Uber quotes last only a few minutes, and the trip is booked days later. Toolshare absorbs any difference between the quoted and final price.

### Test mode by default
- **Robocourier:** until `COURIER_LIVE=1`, every trip is created with Uber's automated test courier ("Robocourier"). No real driver, no charge, and it moves through the statuses on its own.
- **Live-credential guard:** if live credentials are used while live mode is off, the app cancels the trip immediately and shows an error. That way nobody gets billed by accident.
- **Who pays:** payments aren't built yet, so every live trip would be paid by you. Turn live mode on only after Stripe is in place, or for a supervised test.

## Your setup checklist
- [ ] Sign up for Uber Direct at https://direct.uber.com, using the business you'll bill under.
- [ ] Dashboard → **Developer**: copy the **Customer ID**, **Client ID** and **Client secret** for the test environment.
- [ ] Run in Cloud Shell:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pkl-nick/tools/claude/tool-sharing-platform-research-xzdoea/deploy/setup-delivery.sh -o setup-delivery.sh
  bash setup-delivery.sh
  ```
  It prints the webhook URL to paste into Uber's dashboard (Developer → Webhooks). It then asks for the webhook's signing key; all secrets stay hidden as you type.
- [ ] Try a test trip: an owner adds an address and phone, a renter books with the courier option, the owner accepts and taps **send with courier**, and the status advances in My garage.
- [ ] Later, once payments exist: get Uber's approval for production, store the production credentials by re-running the script, then run `COURIER_LIVE=1 bash setup-delivery.sh`.

## Settings
| Setting | Purpose |
|---|---|
| `AZURE_MAPS_CLIENT_ID` | Maps account id; set by `azure-deploy.sh` (or `AZURE_MAPS_KEY` for local testing) |
| `UBER_DIRECT_CUSTOMER_ID`, `UBER_DIRECT_CLIENT_ID`, `UBER_DIRECT_CLIENT_SECRET` | Uber Direct API |
| `UBER_DIRECT_WEBHOOK_KEY` | verifies `X-Uber-Signature` on status updates |
| `COURIER_LIVE` | `1` = real couriers; anything else = Robocourier test trips |
| `DELIVERY_MARKUP_PCT`, `DELIVERY_MARKUP_FLAT` | renter price = Uber fee × (1 + pct) + flat; defaults 15 and 1.00 |

## Other providers
`delivery.py` is the only file that knows about Uber. DoorDash Drive has a similar quote → create → webhook API. It could be added as a second provider, and the cheaper of the two used for each trip.
