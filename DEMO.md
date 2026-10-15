# 2-minute demo script

**Setup:**
- Browser full screen at http://localhost:8000.
- Click **Start demo**, which gives a clean case study (Bright Bakery only).
- Have the Stripe test dashboard in a second tab.
- Have **receivevoiz@gmail.com** open on a phone or in a third tab.

Press **→** for each step.

| Time | Step | On screen | Say |
|---|---|---|---|
| 0:00–0:12 | — | voiz, empty board | "UK small firms are owed billions in late payments. Most never chase, and almost none claim the interest the law gives them. voiz does both, from your voice." |
| 0:12–0:40 | **1 · Voice note → invoice** | £361.00 card in *Open*. Cut to the Stripe test invoice, then the email in receivevoiz@gmail.com. | "I just say what I did. voiz prices it from my rate card; the model never sets an amount. My policy allows it, so it raises a real Stripe invoice and emails it with a pay link. No human raised that bill." |
| 0:40–1:05 | **2 · Skip 45 days → overdue** | Card slides to *Overdue*. A reminder and a £41.74 interest invoice appear in *Mail*. (Cut the ~25 s Stripe test-clock wait.) | "Forty-five days later on Stripe's test clock it's overdue. voiz sends a reminder and bills statutory interest under the Late Payment Act: 11.75% plus £40 compensation, on a separate invoice, because the policy says so." |
| 1:05–1:20 | **3 · Customer replies** | Decision "On hold · CC-4" | "Bright replies with a date. voiz pauses reminders and remembers the promise." |
| 1:20–1:35 | **4 · Customer pays** | Cards move to *Paid*, with a toast | "They pay by card in Stripe test mode, and the webhook moves it to paid." |
| 1:35–1:55 | **5 · Why this interest?** | Side panel: formula, CC-5, Stripe ID, memory | "And every pound is explained: the clause, the law, the maths and the evidence." |
| 1:55–2:00 | — | voiz wordmark | "voiz. Invoices from your voice. Collected by the law." |

**Backups:**
- If an email is slow, the *Mail* tab shows every email sent.
- If the webhook is down, the 5-second poll still moves the card to Paid.
- **Exit demo** returns to the full view: three customers, approvals and the key-account flow.
