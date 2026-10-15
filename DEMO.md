# 2-minute demo script

**Setup:**
- Browser full screen at http://localhost:8000.
- Use **Reset demo** from the ••• menu.
- Have the Stripe test dashboard open in a second tab.
- Have the Bright Bakery teammate's phone ready to show the email arriving.

Press **→** for each step.

| Time | Step (button) | On screen | Say |
|---|---|---|---|
| 0:00–0:12 | — | voiz home: Outstanding £17,300 | "UK small firms are owed billions in late payments, and most never chase or claim the interest the law gives them. voiz does both, from your voice." |
| 0:12–0:32 | **1 · Voice note** | £361.00 invoice appears. Cut to the Stripe test invoice, then the phone inbox. | "I just say what I did. voiz prices it from my rate card. The model never sets an amount. It raises a real Stripe test invoice and emails it with a pay link." |
| 0:32–0:47 | **2 · Customer asks for a bill** | £360.00 invoice raised from Acme's email | "Acme emails 'please invoice the extra three hours'. Nobody touches it. **No human raises the bill.**" |
| 0:47–1:07 | **3 · Skip 45 days** | Cards slide to Overdue; Interest claimed jumps; Northwind waits for approval | "Forty-five days later on Stripe's test clock: reminders go out, and statutory interest is billed under the Late Payment Act, £93.18 for Acme. Northwind is a key account, so policy says I approve it." |
| 1:07–1:15 | **4 · Approve key account** | Northwind interest £39.81 raised | "One click. It executes exactly what was proposed." |
| 1:15–1:25 | **5 · Customer promises to pay** | Bright "On hold · CC-4" | "Bright promises a date, so voiz pauses chasing and remembers the promise." |
| 1:25–1:35 | **6 · Customer pays** | Acme card moves to Paid, with a toast | "Acme pays with a test card. The webhook updates it live." |
| 1:35–1:55 | **7 · Why £93.18?** | Side panel: formula, CC-5, Stripe ID, memory | "And every pound is explained: the clause, the law, the maths, the evidence, from the decision ledger and customer memory." |
| 1:55–2:00 | — | voiz wordmark | "voiz. Invoices from your voice. Collected by the law." |

**Backups:**
- If Gmail is slow, the *Mail* tab shows every email sent.
- If the webhook is down, the 5-second poll still moves the card to Paid.
