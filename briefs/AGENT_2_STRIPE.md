# Agent 2: Stripe (billing, payment status, late-payment simulation, collections)

**Paste this whole file into your Claude Code session as the task.** Build against the SHARED CONTRACT at the bottom. Agent 1 pushes `clause/context.py` around 19:05. Until then, code against the contract's signatures and test with `FakeGateway`.

## Mission
Every penny that moves goes through you:
- invoices raised in Stripe test mode;
- invoice status kept true via webhooks, with a poll as backup;
- time moved with Test Clocks, so invoices go overdue on camera;
- the collections policy run: chases, statutory interest invoices, approvals for key accounts.

This is the 35-point "money moves in test mode in the video" criterion. It must never fail live.

## You own
- `clause/stripe_gateway.py` (exists)
- `clause/law.py` (exists; verified: Acme £93.18, Northwind £39.81)
- `clause/billing.py` (new)
- `clause/collections.py` (new)
- `scripts/seed.py`
- `tests/test_billing.py`, `tests/test_law.py`

## Build, in this order

### 1. Make the gateway real
Run it once against Stripe test mode (`STRIPE_SECRET_KEY`):
- create clock → customer → invoice (2 lines) → finalize → send;
- read `hosted_invoice_url` **and `invoice_pdf`** (add `invoice_pdf` to `InvoiceInfo`);
- pay with the test card → `status == "paid"`.

Add `list_events` if useful. Fix whatever the real API rejects. Keep idempotency keys on every write.

Gateway notes:
- `StripeGateway.create_invoice` passes `pending_invoice_items_behavior="exclude"` and then attaches items with `invoice=inv.id`. Check this works on stripe 16 / the current API version. If it doesn't, drop the param.
- `send_invoice` in test mode sends **no email**; that's expected. A3 emails.

### 2. `clause/billing.py`
- `ensure_customer(ctx, key, name, email, **profile)`:
  - Creates the Stripe customer on a clock that has fewer than 3 customers, creating a new clock if needed, and stores the clock id in `meta.clock_ids`.
  - Stores the customer doc.
  - Idempotent by key.
- `raise_invoice(ctx, *, customer_key, lines, kind, source, decision_id, description, parent_invoice=None)`:
  - `days_until_due = customer.terms_days`.
  - Metadata on the invoice and every item: `decision_id`, `kind`, `source_type`, `source_ref`, `policy_version`, `rate_card` and `parent_invoice`.
  - Stores the invoice doc (contract shape), emits `invoice`, and remembers `"Invoice {number} for £X raised for {lines} — source {type}: '{quote}'"`.
  - Registered as action `billing.raise_invoice`.
- `sync_invoice(ctx, invoice_id)`: re-reads Stripe, updates `status`, `remaining_pence`, `paid_at`, and `overdue` (computed against `ctx.clock_date()`), then emits `invoice`.
- `credit_note(...)`: wraps `gateway.credit_note`. Registered as `billing.credit_note`. Used by A3's inbox for CC-6.
- **Webhook** `POST /webhooks/stripe`:
  - Verify the signature with `stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)`. If the secret is unset, accept unsigned events but log a warning, so a local run can't break the demo.
  - Handle `invoice.finalized`, `invoice.sent`, `invoice.paid`, `invoice.payment_succeeded`, `invoice.payment_failed`, `invoice.voided`, `invoice.marked_uncollectible`, `credit_note.created` and `test_helpers.test_clock.ready` by calling `sync_invoice`.
  - On **`invoice.paid`**:
    - record a decision (`actor="stripe"`, `action="payment_received"`, outcome `none`, clauses `["CC-2"]`, reason "Payment received; chasing stops");
    - remember `"Paid {number} £X on {clock date}, {n} days after due, after {k} chases"`. This is the transaction history, as narrative, that makes "is Acme reliable?" answerable;
    - emit a `toast`.
  - Dedupe on `event.id` (store `meta` key `evt_<id>`).
- **Poll fallback.** A background thread re-syncs every *open* invoice every 5 s while the app runs, so a dead `stripe listen` never kills the demo. It's started from a `startup(ctx)` that A1 calls.
- **Router** (`router = APIRouter()`):
  - `POST /api/sim/advance {days}`:
    - advance every clock in `meta.clock_ids` to `clock_date + days` (`gateway.advance_clock` polls until ready);
    - `set_meta("clock_date")` and emit `clock`;
    - `sync_invoice` all open invoices;
    - run `collections.collections_tick(ctx)` and return its decisions.
  - `POST /api/sim/pay/{invoice_id}`: test-card payment (the backup for the hosted page).
  - `POST /api/sim/reset`: `store.reset()`, `memory.forget` for each customer, new clock at real now, `seed`.

### 3. `clause/collections.py`: `collections_tick(ctx)`
For each open `kind == "work"` invoice:
1. Build `policy.InvoiceStanding(today=ctx.clock_date(), due, remaining_pence, disputed, promise_date, chases_sent, interest_claimed, key_account)`.
2. **Chase.** Call `policy.next_chase(standing)`.
   - On AUTO: get the facts and the draft via A3's `llm.draft_email(facts)`; the facts carry `amount_due`, `due_date`, `days_overdue`, `invoice_number`, `pay_url` and an `interest_notice` when one applies.
   - Check the draft with `llm.check_draft(draft, allowed_pence={remaining, claim_total, interest, compensation}, must_include=[invoice number, pay url])`. On a failure, retry once, then fall back to `template_email`.
   - Then `record_decision(action="chase", clauses, reasons, evidence=[{type:"stripe", ref: invoice_id, quote: f"open, due {due}"}])` → `mail.send_customer_email(kind="chase")` → append to `invoice.chases`.
   - A HOLD outcome is recorded **once** per invoice per reason; don't spam the timeline every tick.
3. **Interest.**
   - Compute `claim = law.late_claim(remaining_pence, due, today, Decimal(rate) if contract rate else None)`, then `policy.interest(standing, claim.total_pence)`.
   - On AUTO: run `billing.interest` → `raise_invoice(kind="interest", parent_invoice=..., lines=[...])`.
     - Line 1: `Statutory interest, Late Payment of Commercial Debts (Interest) Act 1998: {claim.formula()}`. For a contractual claim, word it as contractual interest instead.
     - Line 2: `Fixed compensation (£70.00)`, only when statutory.
     - Then email it (`kind="interest"`), set `interest_claimed=True` on the parent, and record the decision with `calc` = all `LateClaim` fields, `evidence` = parent invoice + due date (+ the contract quote for a contractual rate), and clauses `["CC-5"]`.
   - On NEEDS_APPROVAL: record a decision with `proposal={"action": "billing.interest", "args": {"invoice_id": ..., "today": iso}}`. Northwind, a key account, lands here.
     - **The approved action must recompute and assert the same total** (CC-8); if it differs, fail loudly.
4. Make the tick idempotent per clock day: store `meta` key `tick_<date>`.

### 4. `scripts/seed.py` (also used by `/api/sim/reset`)
- Create the 3 demo customers from the contract table on one clock that starts now. Use emails from `DEMO_EMAIL_*`, falling back to `<key>@example.com`.
- Raise the seeded invoices through `raise_invoice(source={"type": "seed", "ref": "contract", "quote": ...})`:
  - Acme: **£4,800.00**.
  - Northwind: **£12,500.00**, with `contract_rate_over_base="4.0"`, `contract_quote="Interest on late payment accrues at 4% per annum above the Bank of England base rate."`, `key_account=True`.
- Bright starts with no invoices.
- Run it standalone with `python -m scripts.seed`.

### 5. Tests (offline, `FakeGateway`)
- `test_law.py`: £93.18 (Acme), £39.81 (Northwind), £41.74 (Bright on £361); the compensation bands are £40, £70 and £100; a contractual rate means no compensation.
- `test_billing.py`:
  - raise → open;
  - pay → paid → a payment decision;
  - webhook replay of the same `event.id` → processed once;
  - advance 45 → Acme gets a chase plus an AUTO interest invoice of 9318 pence; Northwind gets NEEDS_APPROVAL;
  - approving Northwind → an interest invoice of 3981 pence;
  - a second tick on the same day → no new decisions;
  - a disputed invoice → HOLD with no interest.

## Acceptance
- [ ] In the Stripe **test dashboard**, seeded invoices show the metadata `decision_id`, with the clock visible under Test Clocks.
- [ ] Paying the hosted invoice page with `4242…` flips the UI card to **Paid** within 3 s, via webhook or the poll.
- [ ] `POST /api/sim/advance {"days":45}` returns within about 30 s with the chase, interest and approval decisions, and the Acme interest invoice exists in Stripe at **£93.18**.
- [ ] `/api/sim/reset` gives a clean demo every time, so we can rehearse twice.
- [ ] `pytest -q` passes offline.

## Don't
- Touch live keys.
- Use Payment Links or Checkout. Invoices are the product.
- Rely on `invoice.overdue` events.
- Let the LLM compute interest.

---

## SHARED CONTRACT (identical in all four briefs; do not change it alone)

If you need to change anything in this section, tell the other three first. Agent 1 is the integrator and has the final say.

### The product in one paragraph
**Clause** is an accounts-receivable agent for a UK SME, demoed as *Hollis Electrical Ltd*. The owner sends a voice note or a chat message such as "finished at Bright Bakery, four hours plus two RCDs". A customer might instead email "please invoice the extra 3 hours".

Either way, the agent:
- prices the work from the rate card **in code** (the LLM never decides an amount);
- checks the written policy;
- raises a real **Stripe test-mode** invoice;
- emails it from **Gmail** with the Stripe pay link;
- tracks its status through **Stripe webhooks**.

When an invoice goes overdue, which we simulate with **Stripe Test Clocks**, the agent chases and raises a separate **UK statutory late-payment interest** invoice, using the GOV.UK maths in `clause/law.py`. It also handles promises and disputes from email replies.

Every decision is recorded with the clause, the evidence, the calculation and the Stripe IDs. It is also remembered in **Cognee**, so "why did you charge Acme £93.18?" is answered from memory.

**Track 02 rule we must satisfy: "No human raises the bill."** The owner never types an amount. The input is evidence, either work reported or a customer's email. The money comes from the rate card, and the policy decides what goes out automatically and what needs approval.

### Ground truth (verified; don't re-research)
- **Stripe test mode only.** `StripeGateway` refuses any key that isn't `sk_test_`/`rk_test_`.
- **Stripe sends no customer emails in test mode.** That is why we send them ourselves through Gmail.
- **Invoice statuses** are `draft`, `open`, `paid`, `void` and `uncollectible`. There is **no "overdue" status**: an overdue invoice stays `open` after its `due_date`, so we compute `overdue = status == "open" and clock_date > due_date`.
  - `invoice.overdue` exists, but only fires from Billing Automations. Don't rely on it.
- For past-due tracking, invoices use `collection_method=send_invoice` + `days_until_due`. Finalizing locks the amounts. The hosted page (`hosted_invoice_url`) accepts test card `4242 4242 4242 4242`, and `invoice.paid` fires when it is paid.
- **Test Clocks:**
  - A clock takes at most **3 customers**. A customer must be created *on* the clock.
  - Advancing is asynchronous: poll until `status == "ready"`. This is already done in `StripeGateway.advance_clock`.
- **Webhooks on localhost:** run `stripe listen --api-key <sk_test key> --forward-to localhost:8000/webhooks/stripe`. Stripe CLI 1.53.1 is on Lorenz's laptop. It prints a `whsec_...` value; put that in `STRIPE_WEBHOOK_SECRET`.
- **UK late-payment law** (GOV.UK) is already coded in `clause/law.py`:
  - interest is 8% plus the base rate, simple and daily; the base rate is 3.75%, so 11.75% for Jul–Dec 2026;
  - fixed compensation is £40, £70 or £100, by debt size;
  - a contractual rate *replaces* both statutory interest and compensation;
  - interest goes on a new, separate invoice;
  - it applies to B2B debts only.
- **Policy** is `POLICY.md` + `clause/policy.py`, clauses CC-1…CC-8, with outcomes `auto | needs_approval | deny | hold | none`.

### Existing code (on Lorenz's laptop; Agent 1 pushes it in the first 5 minutes)
These modules are written but **not yet run or tested**. Whoever owns a file makes it work.
- `clause/law.py`: interest maths. Owner: A2.
- `clause/policy.py` and `POLICY.md`: the policy engine, pure functions. Owner: A1.
- `clause/store.py`: SQLite doc store (`put/get/update/all/meta/set_meta/reset`) plus the SSE `Bus`. Owner: A1.
- `clause/memory.py`: `CogneeMemory` and `LocalMemory`, each with async `remember/recall/improve/forget`. Owner: A3.
- `clause/llm.py`: Claude structured-output calls (`classify_email`, `draft_email`, `answer_why`, `extract_contract`), `check_draft`, which rejects an email that mentions £ amounts the code didn't compute, and `FakeLLM`. Owner: A3.
- `clause/stripe_gateway.py`: `StripeGateway`, with clock, customer, invoice (create→items→finalize→send), credit note and test-card pay, idempotency keys on every write, and `FakeGateway`. Owner: A2.

### File ownership (only edit what you own; ask the owner otherwise)
| Agent | Owns |
|---|---|
| **A1 Desk** | `clause/config.py`, `clause/context.py`, `clause/app.py`, `clause/desk.py`, `clause/policy.py`, `POLICY.md`, `clause/store.py`, `data/ratecard.json`, `requirements.txt`, `.env.example`, `.gitignore`, `tests/test_desk.py`, `tests/test_e2e.py` |
| **A2 Stripe** | `clause/stripe_gateway.py`, `clause/law.py`, `clause/billing.py`, `clause/collections.py`, `scripts/seed.py`, `tests/test_billing.py`, `tests/test_law.py` |
| **A3 Mail+Memory** | `clause/mail.py`, `clause/inbox.py`, `clause/memory.py`, `clause/llm.py`, `tests/test_mail.py`, `tests/test_memory.py` |
| **A4 UI** | `web/index.html`, `web/app.js`, `web/styles.css`, `web/mock.js`, `README.md`, `DEMO.md` |

New dependency? Ask A1 to add it to `requirements.txt`. Don't edit that file yourself.

### Runtime shape
- One process: FastAPI on `localhost:8000`, started with `python -m uvicorn clause.app:app --port 8000`. On Windows use `.venv\Scripts\python`.
- Module functions are **plain synchronous `def`**. FastAPI routes are sync `def` too (they run in the threadpool), except the SSE route.
- **Never call `bus.publish` or memory coroutines directly from a sync function.** Always go through `ctx.emit(...)`, `ctx.remember_later(...)` and `ctx.recall(...)`; these are thread-safe and A1 provides them.

### `clause/context.py` (A1 provides this API exactly)
```python
@dataclass
class Ctx:
    settings: Settings          # from clause/config.py
    store: Store                # clause/store.py
    bus: Bus
    memory: Memory              # CogneeMemory | LocalMemory  (A3)
    gateway: Gateway            # StripeGateway | FakeGateway (A2)
    llm: LLM                    # ClaudeLLM | FakeLLM         (A3)
    mailer: Mailer              # GmailMailer | OutboxMailer  (A3)
    loop: asyncio.AbstractEventLoop | None   # captured at app startup; None in tests

    def emit(self, event: str, data: dict) -> None                       # thread-safe bus publish
    def remember_later(self, customer: str, kind: str, ref: str, text: str) -> None  # fire-and-forget; runs inline (asyncio.run) when loop is None
    def recall(self, customer: str, question: str, timeout: float = 25) -> list[MemoryHit]
    def clock_date(self) -> date                                          # store.meta("clock_date"); A2 keeps it current
    def register_action(self, name: str, fn: Callable[..., dict]) -> None # fn(ctx, **args) -> dict
    def run_action(self, name: str, args: dict) -> dict
    def record_decision(self, *, actor: str, action: str, customer: str, outcome: str,
                        clauses: list[str], reasons: list[str], summary: str,
                        invoice: str | None = None, evidence: list[dict] = (),
                        calc: dict | None = None, stripe_ids: list[str] = (),
                        proposal: dict | None = None) -> dict
        # writes kind "decision" (id "dec_xxx", status "executed" | "pending_approval" | "rejected" | "failed",
        # at = real UTC time, clock_date), emits "decision", remember_later(customer, "decision", id, summary + reasons)

def build_ctx(settings: Settings, *, fake: bool = False) -> Ctx
    # fake=True: Store(":memory:"), FakeGateway, FakeLLM, LocalMemory, OutboxMailer; used by every test
```
**Approvals (CC-8).** A `needs_approval` decision carries `proposal = {"action": "<registered name>", "args": {...}}`. Approving it runs `ctx.run_action(...)` with those args **unchanged**.

Registered action names:
- `billing.raise_invoice` (A2)
- `billing.interest` (A2)
- `billing.credit_note` (A2)
- `mail.send_invoice_email` (A3)
- `desk.bill_proposal` (A1)

### Documents in the store (`store.put(kind, id, data)`)
- **customer**, id = slug key (`acme`, `bright`, `northwind`):
  `{name, email, aliases: [..], stripe_customer_id, clock_id, terms_days, key_account: bool, contract_rate_over_base: "4.0" | None, contract_quote}`
- **invoice**, id = Stripe invoice id `in_...`:
  `{customer, number, kind: "work"|"interest", parent_invoice, status, overdue: bool, total_pence, remaining_pence, due_date: "YYYY-MM-DD", hosted_invoice_url, invoice_pdf, lines: [{code, description, quantity, unit_pence, amount_pence, evidence_quote}], source: {type: "voice"|"chat"|"email"|"seed"|"agent", ref, quote}, decision_id, chases: [{stage, sent_on, outbox_id}], interest_claimed: bool, disputed: bool, dispute_quote, promise_date, promise_quote, paid_at}`
- **email** (inbound), id = Message-ID:
  `{customer, from, to, subject, body, received_at, classification: {...}, invoice, decision_ids: [..]}`
- **outbox** (outbound), id = `out_xxx`:
  `{customer, to, subject, body, sent_at, mode: "gmail"|"outbox", message_id, invoice, decision_id, kind: "invoice"|"chase"|"interest"|"reply"}`
- **decision**: see `record_decision` above.
- **meta**: `clock_date`, `clock_ids`, `seeded`.

### SSE events (`ctx.emit(event, data)`; the UI listens on `GET /api/events`)
| Event | Data |
|---|---|
| `decision` | the decision doc |
| `invoice` | the invoice doc |
| `email` | the email or outbox doc, plus a `direction: "in"\|"out"` field |
| `memory` | `{op, customer, detail}` |
| `clock` | `{clock_date}` |
| `chat` | `{role: "assistant", text, decision_ids, invoice_ids}` |
| `toast` | `{level, text}` |

### HTTP API
| Route | Owner | Body → Result |
|---|---|---|
| `GET /api/state` | A1 | → `{business, clock_date, customers, invoices, decisions[-100:], emails[-50:], outbox[-50:], memory_events[-100:], approvals}` |
| `GET /api/events` | A1 | SSE stream |
| `POST /api/chat` | A1 | `{message, mode: "text"\|"voice"}` → `{reply, decision_ids, invoice_ids}` |
| `POST /api/approvals/{decision_id}` | A1 | `{approve: bool, note}` → decision doc |
| `POST /api/why` | A1 | `{question, customer?}` → `{answer, records, memory}` |
| `POST /webhooks/stripe` | A2 | Stripe-signed event → 200 |
| `POST /api/sim/advance` | A2 | `{days}` → `{clock_date, decisions}`: advance every clock, sync invoices, run collections |
| `POST /api/sim/pay/{invoice_id}` | A2 | → invoice doc (backup when the hosted page can't be used on camera) |
| `POST /api/sim/reset` | A2 | → `{ok}`: wipe store + memory, new clock, reseed |
| `POST /api/mail/poll` | A3 | → `{emails}`: check the Gmail inbox now |
| `POST /api/mail/inject` | A3 | `{from, subject, body}` → `{email, decisions}`: same path as a real inbound email (offline fallback) |
| `GET /api/memory/{customer}?q=` | A3 | → `{hits}` (Cognee recall) |
| `POST /api/memory/{customer}/forget` | A3 | → `{ok}` (erase demo) |

Each owner exposes an `APIRouter` named `router` in their module; A1 mounts them in `app.py`: `billing.router`, `mail.router`, plus a `startup(ctx)` hook if needed.

### Cross-module Python calls (signatures frozen)
```python
# A1  clause/desk.py
def bill_from_text(ctx, text: str, source: dict, customer_key: str | None = None) -> dict
    # -> {"decision": dict, "invoice": dict | None, "outbox": dict | None, "reply": str}
# A2  clause/billing.py
def ensure_customer(ctx, key: str, name: str, email: str, **profile) -> dict
def raise_invoice(ctx, *, customer_key: str, lines: list[dict], kind: str, source: dict,
                  decision_id: str, description: str, parent_invoice: str | None = None) -> dict
def sync_invoice(ctx, invoice_id: str) -> dict          # Stripe is the source of truth for status/amounts
def credit_note(ctx, *, invoice_id: str, amount_pence: int, memo: str, decision_id: str) -> dict
# A2  clause/collections.py
def collections_tick(ctx) -> list[dict]                 # decisions taken; idempotent per clock day
# A3  clause/mail.py
def send_customer_email(ctx, *, customer_key: str, subject: str, body: str, kind: str,
                        invoice_id: str | None = None, decision_id: str | None = None,
                        attach_invoice_pdf: bool = False) -> dict   # -> outbox doc
# A3  clause/inbox.py
def handle_inbound(ctx, email: dict) -> list[dict]      # classify + act; returns decisions
```

### Environment (`.env` in repo root, loaded with `load_dotenv(override=True)`; never commit it)
```
ANTHROPIC_API_KEY=            # the Machine-level key on Lorenz's laptop is DEAD (401); use a fresh one here
CLAUSE_MODEL=claude-opus-5-5  # switch to claude-sonnet-5-5 if chat replies take > 6 s
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
GMAIL_ADDRESS=                # the business inbox (one team member's Gmail, 2-Step Verification ON)
GMAIL_APP_PASSWORD=           # myaccount.google.com/apppasswords, 16 chars
MAIL_MODE=gmail               # gmail | outbox (outbox = record + show only, no SMTP)
MEMORY_BACKEND=cognee         # cognee | local
DEMO_EMAIL_ACME=              # real inboxes of teammates, so replies can be sent from phones on camera
DEMO_EMAIL_BRIGHT=
DEMO_EMAIL_NORTHWIND=
BUSINESS_NAME=Hollis Electrical Ltd
```
Only the integrator machine and A3 need `cognee` installed. Everyone else runs with `MEMORY_BACKEND=local`.

### Demo data (A2 seeds; A1 owns the rate card)
- **Rate card** `data/ratecard.json`, version `2026-10`:

  | Code | Item | Price |
  |---|---|---|
  | `LAB` | Labour | £60.00/hour |
  | `CALL` | Call-out | £45.00/visit |
  | `RCD` | RCD unit supplied and fitted | £38.00 each |
  | `EICR` | Condition report | £180.00 |
  | `LED` | LED panel supplied and fitted | £55.00 each |

- **Customers**, all on one Test Clock that starts at real "now":

  | Key | Name | Terms | Interest | Account | Seeded open invoice |
  |---|---|---|---|---|---|
  | `acme` | Acme Paper Ltd | net 30 | statutory | | £4,800.00 "Q3 maintenance contract" |
  | `bright` | Bright Bakery Co | net 30 | statutory | | none |
  | `northwind` | Northwind Logistics Ltd | net 30 | contract: 4% over base | **key account** | £12,500.00 "Warehouse rewire, phase 1" |

  A new customer created live goes on a *new* clock, because of the 3-per-clock limit. `/api/sim/advance` advances **every** clock in `meta.clock_ids`.
- **Golden numbers** after `advance 45 days`, i.e. 15 days overdue (the tests must assert these):
  - Acme: interest £23.18 + compensation £70.00 = **£93.18**, under £250, so **auto**.
  - Northwind: contract rate 7.75%, giving **£39.81**, no compensation; key account, so **needs approval**.
  - Voice note "four hours labour, two RCDs and the call-out" for Bright Bakery = 4×£60 + 2×£38 + £45 = **£361.00**.

### Timeline (BST; hard stops)
- **19:05:** keys in `.env`; A1 pushes the skeleton and the existing code. Everyone else starts at once against this contract with fakes; don't wait.
- **19:45:** first integration on A1's machine with real keys; the happy path runs end to end.
- **20:10:** **feature freeze.** Only fixes and polish after this.
- **20:15–20:25:** rehearse `DEMO.md` twice, using `/api/sim/reset` between runs.
- **20:30:** record (2:00 max). **20:45:** submit. Editable until 21:00.

**Git rules:**
- Commit only files you own.
- `git pull --rebase` before every push.
- Push every 15 minutes.
- Never commit `.env`, `data/*.db` or `data/cognee/`.
- **All tests run offline:** `python -m pytest -q` passes with no keys, using `build_ctx(settings, fake=True)`.
