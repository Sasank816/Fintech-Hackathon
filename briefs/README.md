# Build briefs: who builds what

There is one brief per person. Pick yours, open Claude Code in this repo, and paste the whole file in as the task.

| Brief | Builds | Starts |
|---|---|---|
| [AGENT_1_DESK.md](AGENT_1_DESK.md) | Integrator: keys, skeleton (`config`, `context`, `app`), voice/chat → priced bill, policy, approvals, "why?" | **First.** The others pull its skeleton at about 19:05 |
| [AGENT_2_STRIPE.md](AGENT_2_STRIPE.md) | Stripe test-mode invoices, webhooks, Test Clock late payments, chases, statutory interest, seed | Now, using `FakeGateway` |
| [AGENT_3_MAIL_MEMORY.md](AGENT_3_MAIL_MEMORY.md) | Gmail send/receive, inbound email → action, Cognee memory | Now. Prove Gmail by 19:20 and Cognee by 19:35 |
| [AGENT_4_UI.md](AGENT_4_UI.md) | Stripe-style UI (white × neon green), voice mic, README, demo script | Now, using `web/mock.js` |

## Rules
- **One branch: `main`.** Commit only the files your brief says you own. Run `git pull --rebase` before every push, and push every 15 minutes.
- **Don't change the shared contract** (the section at the end of every brief) without telling the team.
- **Never commit `.env`.** Copy `.env.example` (Agent 1 adds it) and fill in your own keys. Test mode only: `sk_test_…`.
- **Feature freeze 20:10 · rehearse 20:15 · record 20:30 · submit by 20:45.**

## Integration hooks (from Agent 1; the skeleton is live)
- `build_ctx` imports `clause.billing`, `clause.collections`, `clause.mail`, `clause.inbox` and `clause.desk` when they exist. If your module defines **`register(ctx)`**, it is called right away; register your approval actions there with `ctx.register_action("billing.interest", fn)`.
- If your module defines **`startup(ctx)`**, the app calls it once, in a thread, after the event loop exists. Start pollers there as daemon threads.
- If `clause.billing` or `clause.mail` defines **`router`**, the app mounts it. **Inside a route handler, get the context with `request.app.state.ctx`.** Don't use a module global, so tests can build their own app with `create_app(build_ctx(Settings(), fake=True))`.
- **`mail.send_invoice_email`** takes `invoice_id, decision_id=None, subject=None, body=None` (as Agent 3 built it). It looks up the customer from the invoice and always attaches the PDF. `billing.raise_invoice` takes exactly the keyword arguments of `raise_invoice`.
- Extra context helpers beyond the contract:
  - `ctx.improve_later(customer, feedback)` and `ctx.forget(customer)`;
  - `ctx.update_decision(id, **fields)`, which re-emits the decision;
  - `ctx.memory_log` holds the memory events the UI shows. Memory ops made through `ctx` are logged and emitted automatically.
- Smoke test: `python -m uvicorn clause.app:app --port 8000`, then open http://localhost:8000/api/state.

## Already in the repo (written, not yet run)
- `clause/law.py`: UK late-payment interest maths, verified against the demo numbers.
- `clause/policy.py` + `POLICY.md`: credit-control policy CC-1…CC-8.
- `clause/store.py`: SQLite doc store + SSE bus.
- `clause/memory.py`: Cognee memory, plus a local fallback.
- `clause/llm.py`: Claude structured calls, plus `FakeLLM` for tests.
- `clause/stripe_gateway.py`: Stripe test-mode gateway, plus `FakeGateway`.
