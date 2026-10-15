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

## Already in the repo (written, not yet run)
- `clause/law.py`: UK late-payment interest maths, verified against the demo numbers.
- `clause/policy.py` + `POLICY.md`: credit-control policy CC-1…CC-8.
- `clause/store.py`: SQLite doc store + SSE bus.
- `clause/memory.py`: Cognee memory, plus a local fallback.
- `clause/llm.py`: Claude structured calls, plus `FakeLLM` for tests.
- `clause/stripe_gateway.py`: Stripe test-mode gateway, plus `FakeGateway`.
