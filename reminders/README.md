# Deadline reminders

Step 5 of the MVP loop (see [SCOPE.md](../SCOPE.md)): one reminder before
the appeal deadline, escalating as it nears, plus a "did you send it?"
confirmation and a later "how did it go?" check-in. Implemented in
[scripts/reminder.py](../scripts/reminder.py) — deterministic templating and
scheduling, no model call, no vendor account required to run or test it.

## Quick start

```bash
python scripts/reminder.py create \
    --extraction eval/fixtures/sample_001.json \
    --contact my_contact.json \
    --state reminders/state/case1.json

python scripts/reminder.py tick --state reminders/state/case1.json
python scripts/reminder.py mark-sent --state reminders/state/case1.json --sent true
python scripts/reminder.py report-outcome --state reminders/state/case1.json --outcome won
```

`my_contact.json`: `{"name": "...", "email": "...", "phone": "..."}` — either
or both of email/phone; only the channels present get used.

`tick` is meant to be run once a day per case (see `tick-all` below for many
cases at once) by whatever scheduler the app ends up using — nothing here
sets one up. Pass `--today YYYY-MM-DD` to any command to test a specific
date instead of the real one.

## Cadence

Recomputed fresh every tick from the stored deadline and today's date —
not fixed "exactly 10 days out" checkpoints — so it self-heals if a case
starts late or a tick gets skipped:

| Days until deadline | Reminder frequency |
|---|---|
| more than 21 | none yet |
| 15–21 | weekly |
| 0–14, or the denial is for `concurrent` care | daily ([`VALIDATION.md`](../schema/VALIDATION.md) rule D7) |
| deadline day | one "today's the day" message with fast-submission options |
| after the deadline, unconfirmed | one "it may have passed, here's what to do" message, then nothing further |
| confirmed sent | one outcome check-in 45 days later, one more 30 days after that if no reply, then nothing further |

At most one message goes out per case per day, regardless of which rule
fired.

## Delivery

`Sender` in `reminder.py` is the seam for a real provider: pass
`send_email`/`send_sms` callables into `Sender(...)` to wire up SMTP,
Twilio, etc. The default writes every message to `reminders/outbox.jsonl`
instead of sending anything — inspect it to see exactly what would have
gone out. That file is gitignored (see Privacy below); delete it freely.

## Privacy

Reminder state necessarily persists for weeks, which is in tension with
[`VALIDATION.md`](../schema/VALIDATION.md)'s default of deleting the source
document and extraction at session end (P2) — the whole point of this
feature is remembering something after the session ends. The design keeps
that tension as small as possible:

- `create_case()` copies out only what a reminder message needs — insurer
  name, service description, the computed deadline, the submission route,
  and which required evidence is still missing — plus the contact info the
  user gave specifically for this feature. It never stores the full
  extraction, the source document, or `member.*` identifiers (member ID,
  claim number, patient name). The raw extraction is read once, at
  creation, and never touched again.
- State files live under `reminders/state/` and are gitignored, same as
  `eval/results/` and `uploads/`.
- `python scripts/reminder.py delete --state <path>` permanently removes a
  case's state file. Offer this to users explicitly, not just on request.

## Testing without a database or a scheduler

There's no persistent service here, just files: `create` writes one JSON
state file per case, `tick`/`tick-all` read and rewrite it. That's enough
to unit test and to run by hand, and it's a deliberately small piece to
swap for a real datastore later — the scheduling and content logic
(`decide()`, `render_email()`, `render_sms()`) don't know or care where the
state came from.
