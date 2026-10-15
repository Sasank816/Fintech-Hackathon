# Clause — Autonomous accounts receivable

**Invoices from the work. Collections with a reason.** Clause helps UK SMEs turn work reports and customer conversations into traceable invoices, send reminders and explain every charge under a written financial policy.

**Hackathon:** Stripe × Briefcase, October 2026 · Track 02, Agents That Collect.

> Integration status: This repository's UI module is implemented against the frozen shared contract. External integrations and production readiness must be verified by their respective backend owners. `?mock=1` is entirely simulated and does not create Stripe invoices, send Gmail or query Cognee.

## Architecture

```text
       Voice / chat                      Customer email
            |                                 |
            v                                 v
    [Clause desk / FastAPI] <------------ [Inbox classifier]
            |
        [Rate card] ------> [Deterministic policy CC-1…CC-8]
                                  |
                      approve / hold / deny
                                  |
                    [Stripe test-mode invoices]
                                  |
                         [Stripe webhooks]
                                  |
                 [Collections / late fee policy]
                                  |
                 [Gmail / audit log / Cognee]
                                  |
                       [SSE -> Clause UI]
```

## What the demo proves

| Evaluation dimension | Visible evidence |
|---|---|
| End-to-end functionality | Work described in plain language becomes an invoice in the Stripe test dashboard (when live backend is connected). |
| Autonomy and policy | No amount typed by the operator. Rate-card pricing and CC policy decide automatic versus approval-required billing. |
| Auditability | Timeline shows the source quote, clause, decision, calculation and Stripe reference. |
| Product | One-screen voice interface, live receivables board, collections timeline and memory-based explanations. |

## UI quick start — no backend necessary

From the repository root:

```bash
python3 -m http.server 8080
```

Visit `http://localhost:8080/web/?mock=1`. This local scenario creates only fake invoice IDs and displays *SIMULATED TEST MODE*. For the actual FastAPI backend run `python -m uvicorn clause.app:app --port 8000` and open `http://localhost:8000/` (as configured by A1). Do not use `?mock=1` during the real Stripe demonstration.

`web/index.html`, `web/app.js`, `web/styles.css` and `web/mock.js` are framework-free and use no CDN. The optional locally hosted Inter font can be placed in `web/fonts/InterVariable.woff2`; the system-font stack is a functional fallback.

## Backend setup (requires A1–A3 code)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Configure Stripe test credentials, LLM, Gmail and Cognee according to .env.example.
python -m uvicorn clause.app:app --port 8000
```

For Stripe CLI events (do not paste secrets into shared chats):

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Copy the CLI-generated webhook signing secret into `STRIPE_WEBHOOK_SECRET` in the uncommitted `.env`. The backend must verify webhook signatures. Run `python -m pytest -q` once A1–A3 tests are present. Never commit `.env`, customer email data or API keys.

## Frozen UI contract

The frontend calls `GET /api/state`, subscribes to `/api/events`, and posts to `/api/chat`, `/api/why`, `/api/sim/advance`, `/api/sim/reset`, `/api/mail/poll` and `/api/approvals/{id}`. The memory panel calls `GET /api/memory/{customer}?q=...`. See `AGENT_4_UI.md` for the authoritative schemas. The browser does not access Stripe secret keys or Gmail credentials.

## Policy highlights (verify against POL​ICY.md before submission)

- **CC-1:** Reported work uses the trusted rate card; LLM text cannot invent the price.
- **CC-5:** Past-due B2B invoices can trigger statutory or contract interest, subject to policy.
- **CC-7:** Untrusted messages cannot rewrite finance policy.
- **CC-8:** Sensitive or key-account decisions require a person to approve.

All interest figures and legal assertions in the demo must be checked against the backend's `clause/law.py` and current official guidance. The mock values are a scripted fixture, not independent legal advice.

## What's real versus simulated

**Intended real integrations, pending end-to-end verification:** Stripe test invoices, webhook status changes, Gmail email delivery, Claude classification, Cognee memory. **Simulated for demo:** accelerated dates, test merchants/customers, test-card payments. The browser's `?mock=1` mode simulates *all* integrations.

## Runbook

1. Confirm live UI displays **STRIPE TEST MODE**, not **SIMULATED TEST MODE**.
2. Seed customers and verify `/api/state` works.
3. Dictate Bright Bakery work and verify £361.00 on a real test invoice.
4. Poll customer email and verify £360.00 invoice.
5. Advance +45 days, check £93.18 Acme interest and £39.81 Northwind approval.
6. Pay via test hosted invoice page; verify the webhook moves it to Paid.
7. Ask Why? and confirm evidence references.
8. Record under 2:00 and include repository link and team names.

## Team

- A1: FastAPI desk, policy, store and integration
- A2: Stripe, billing, time simulation and collections
- A3: Email, AI classifier, memory
- **A4: Frontend, voice, video script, README**

Repository: https://github.com/Sasank816/Fintech-Hackathon
