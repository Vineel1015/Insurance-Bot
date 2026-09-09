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

`tick`, `tick-all`, and `mark-sent` all use `Sender.from_env()` by
default, which delivers for real the moment the relevant environment
variables are set — no code change needed to go from logging-only to
live. Whichever channel isn't configured keeps writing to
`reminders/outbox.jsonl` instead of raising, so a deployment with only
email set up (say) doesn't break SMS.

**Email — SMTP.** Works with SendGrid, Postmark, AWS SES, Mailgun, Gmail,
or any other provider's SMTP relay; every major transactional-email
provider offers one, so there's no vendor SDK dependency for this, just
the stdlib `smtplib`.

| Variable | Required | Notes |
|---|---|---|
| `SMTP_HOST` | yes | also the "is SMTP configured" switch |
| `SMTP_FROM` | yes (or set `SMTP_USERNAME`) | the From address |
| `SMTP_PORT` | no | default `587` |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | no | omit only for an unauthenticated relay |
| `SMTP_USE_SSL` | no | `true` for implicit TLS (typically port 465); default is STARTTLS on `SMTP_PORT` |

**SMS — Twilio.** One HTTP POST to Twilio's REST API via stdlib
`urllib`, again no SDK dependency.

| Variable | Required |
|---|---|
| `TWILIO_ACCOUNT_SID` | yes — also the "is Twilio configured" switch |
| `TWILIO_AUTH_TOKEN` | yes |
| `TWILIO_FROM_NUMBER` | yes |

**Check it's actually working** before relying on the daily cadence to
surface a misconfiguration:

```bash
python scripts/reminder.py send-test --email you@example.com --phone +15551234567
```

**Failure handling.** A provider error (bad credentials, an outage, an
invalid number) is caught per channel — it's logged to stderr and to the
outbox as a `"status": "failed"` record, never raised. `tick` only marks
a reminder as sent if at least one configured channel actually
delivered; if every channel failed, nothing is recorded and the next
tick retries automatically. `tick-all` additionally catches anything
unexpected per case (a corrupted state file, say) so one bad case can't
abort a whole batch run — it prints the error and moves on.

**Once a provider is configured, the user's email and/or phone number are
sent to it** (your SMTP relay's operator, or Twilio) to actually deliver
the message — this is unavoidable for real delivery, but it's a real
third-party disclosure worth stating plainly to the user, consistent with
the rest of this project's privacy framing, not something to leave
implicit.

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
