# Clause — 1:50–2:00 demo video script

**Prerequisite:** Use real Stripe test-mode backend, seeded accounts, working webhooks and Gmail before claiming a live integration. If unavailable, label mock footage clearly. Record at 1440×900 or a scaled 1080p display. Never show keys, tokens or private inbox messages.

| Time | Screen / action | Narration |
|---|---|---|
| 0:00–0:12 | Clause hero, policy banner and invoice board. | “Small businesses do the work, then spend hours getting paid. Clause turns work, conversations and payment terms into auditable collections.” |
| 0:12–0:35 | Press mic. Say: **“Just finished at Bright Bakery: four hours labour, two RCDs and the call-out.”** Invoice £361 appears. Show rate-card quote and Stripe test invoice. | “I never enter a price. Clause applies our rate card: £240 labour, £76 for two RCDs and £45 call-out. £361, invoiced under policy CC-1.” |
| 0:35–0:55 | Teammate sends Acme email: **“Please go ahead and invoice the extra 3 hours and the EICR.”** Click Check inbox; £360 card appears. | “Clause understands a customer's email as approved billable work and raises £360 without anyone creating an invoice.” |
| 0:55–1:20 | Advance **+45 days**. Overdue cards turn red. Show chase outbound and Acme £93.18 interest invoice; Northwind £39.81 Needs approval. Click Approve. | “It tracks payment deadlines. Acme's £4,800 debt accrued £23.18 in interest plus £70 fixed compensation. Northwind's £39.81 follows its contract, but key-account policy requires approval.” |
| 1:20–1:35 | Open real Acme hosted invoice payment page. Use the test card **4242 4242 4242 4242** (and valid test expiry/CVC). Watch Paid column transition. | “Stripe confirms the payment; the webhook immediately updates receivables and our audit history.” |
| 1:35–1:50 | Click Why? on Acme interest. Show CC-5 evidence, calculation and memory. | “And every charge is explainable: source invoice, policy clause, formula, Stripe ID and collection history.” |
| 1:50–2:00 | End card with repository, team names, technology. | “Clause. Invoices from the work. Collections with a reason. Stripe test mode, Claude and Cognee.” |

## Rehearsal checklist

- [ ] Test `POST /api/sim/reset` and run once before recording.
- [ ] Check voice permission in Chrome on localhost; if denied, type the identical text without pretending voice worked.
- [ ] Verify the money: **£361.00**, **£360.00**, **£93.18**, **£39.81**.
- [ ] Verify Stripe-hosted invoice links and real test-mode objects exist.
- [ ] Verify webhook event moves an invoice to Paid without refreshing.
- [ ] Confirm customer emails actually arrived before showing a phone.
- [ ] Explain that time and test payments are simulated.
- [ ] Rehearse twice and trim to no more than two minutes.

## Contingencies

If Gmail fails, use backend `/api/mail/inject` but narrate it as a simulated inbound email. If Stripe hosted checkout fails, use backend `/api/sim/pay/{invoice_id}` only if it really updates a test-mode invoice, and disclose the simulation. If the LLM key fails, use the backend's FakeLLM mode and clearly identify it. `?mock=1` is safe for rehearsal, never proof of working Stripe/Gmail/Cognee integration.
