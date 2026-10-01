# Payments plan: do we need deposits?

## The problem with deposits as built
Today every listing has a refundable deposit of about **75% of the tool's new price**. The AI suggests 50–100%. That clashes with the whole idea of the product:

| Tool | Rent | Deposit hold today |
|---|---|---|
| Coil spring compressor ($150 new) | $1.50/day | $120 |
| Impact wrench ($400 new) | $4/day | $300 |
| 60-gal compressor ($900 new) | $4.50/day | $675 |

A renter paying $4 is asked to tie up $300. Three more problems:
- **Holds expire.** A card hold lasts at most about **7 days**: Visa merchant-initiated holds last 4 days and 18 hours, and most others 7. ([Stripe docs](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)) So a hold can't cover the week-long rentals we want to encourage, unless you re-authorize, which can fail, or charge and refund the money.
- **Debit cards lose real money for days.** A hold reduces the renter's available balance until it's released.
- **It scares off exactly the renters we want:** people doing a one-off project for a few dollars.

## Why deposits exist at all
Deposits aren't really for renters. They make **owners** comfortable lending a $400 tool to a stranger. That comfort can come from other places that cost renters little or nothing.

Peer-to-peer rental sites mostly don't take large deposits. They use **ID checks plus a platform guarantee**. Yoodlize, for example, covers listings up to $2,000 at no extra cost. ([SideHusl](https://sidehusl.com/yoodlize/))

## Recommendation: "card on file + toolshare protection"
1. **No deposit by default.** At booking the renter saves a card, and nothing is held. Stripe calls this a SetupIntent: the card is saved for later charges without a payment.
2. **Rent is charged when the owner accepts** (or at pickup). It's small, so it's charged in full.
3. **Toolshare protection fee.** About **$1 per rental**, or 10% with a $0.50 minimum, added to every booking. It funds a protection reserve that pays owners for damage or theft up to a cap, say $500 per incident, at first.
4. **Damage and late returns go on the saved card.** If the return photos show damage, or the tool comes back late, the renter's saved card is charged the late days or the damage, up to a renter deductible of $50–100. The protection reserve pays the owner anything beyond that. This needs clear terms at booking. The pickup and return photos (planned) are the evidence.
5. **Trust gates instead of money:**
   - **ID check** (Stripe Identity) on a renter's first rental, or for any tool worth more than about $300.
   - **New renters start small:** tools under a set value until they've finished a few rentals.
   - **Ratings** once both sides have finished a rental.
6. **Optional deposits only where they make sense:**
   - **Who:** tools over about $500, or owners who opt in.
   - **How much:** capped at 20–25% of the tool's value, with a $150 maximum.
   - **How:** for rentals longer than 5 days, charged and refunded instead of held.

### Does $1 per rental cover the risk? Rough math (assumptions, not data)
Say 1 rental in 200 ends in a claim averaging $200, with the renter's deductible recovering $50 of it. That's an expected cost of about **$0.75 per rental**, so a $1 fee covers it with a margin.

Track claims from day one and adjust. The admin dashboard is the place to watch this.

## Money flow with Stripe Connect
- **Owners** become Stripe "connected accounts". They sign up through Stripe-hosted onboarding, Stripe handles ID checks and tax forms, and payouts go to their bank.
- **Renters** pay toolshare. Each charge is a **destination charge**: the money goes to the owner's connected account, and toolshare keeps an **application fee**, which is the commission plus the service and protection fees. ([Stripe: build a marketplace](https://docs.stripe.com/connect/end-to-end-marketplace))
- **New platforms should use Stripe's newer "Accounts v2" API**, which Stripe now recommends for new marketplaces. ([Stripe Connect 2026 overview](https://no7software.co.uk/blog/stripe-connect-marketplace-payments-engineering))
- **Delivery fees** go to whoever drives. If toolshare should also take a cut of delivery, that's one number to decide.

### The small-ticket problem
Card fees are roughly 2.9% + 30¢ in the US; check Stripe's pricing page for current rates. On a $4.95 rental that's about **$0.44, or about 9%**, before Connect's per-payout and active-account fees. Three ways to soften it:
- **Bundle one charge per booking:** rent, delivery and protection together, never separate small charges.
- **Pay owners out weekly, not daily**, to cut per-payout fees.
- **Treat delivery and protection as the main revenue**, as you suggested, rather than the rental itself.

## Decisions needed before I build payments
1. Deposits: no default deposit, with an optional capped deposit for expensive tools (recommended), or something else?
2. Protection fee: a flat $1, or 10% with a $0.50 minimum?
3. Owner protection cap per incident: $500 to start?
4. Renter damage deductible: $50, $100, or nothing?
5. Should toolshare take a cut of delivery fees, and how much?
