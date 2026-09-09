# Insurance denial appeal helper

Turns a health insurance denial letter into a drafted appeal letter, a
checklist of what to attach, and a deadline reminder. See [SCOPE.md](SCOPE.md)
for what this is and deliberately is not — it's an MVP for one wedge
(denial letters, commercial plans), not a general insurance tool.

## Requirements

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/) to read real
  documents — not needed to try the app with the built-in sample letter
- Optional, for real deadline-reminder delivery: SMTP credentials (any
  provider — SendGrid, Postmark, AWS SES, Mailgun, Gmail, etc.) and/or a
  Twilio account

## Setup

```bash
git clone https://github.com/Vineel1015/Insurance-Bot.git
cd Insurance-Bot
pip install -r requirements.txt
```

## Configuration

Set these as real environment variables in your shell — there's no `.env`
file support, so a `.env` on disk won't be picked up.

| Variable | Required for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Reading real uploaded documents | Get one from the Anthropic console. Not needed for the sample-letter demo. |
| `FLASK_SECRET_KEY` | Nothing strictly — has a working default | Set it if you want flash messages to survive an app restart; otherwise a random one is generated each time the app starts. |
| `SMTP_HOST`, `SMTP_FROM` | Real email reminders | See [reminders/README.md](reminders/README.md#delivery) for the full list (port, credentials, TLS mode). |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Real SMS reminders | Same doc. |

**bash:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```
**PowerShell:**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```
Environment variables set this way only last for the current shell session
— set them again next time you open a new terminal, or add them to your
shell profile.

## Using the web app

### Start it

```bash
python app.py
```
Then open **http://127.0.0.1:5000**. The server runs in debug mode (auto-reloads
on code changes) and is a development server only — see Status below before
running it anywhere but your own machine.

### Walk through it

1. **Upload page.** Either upload a real denial letter (PDF, JPG, PNG, or a
   text file, up to 15MB) and click "Upload and read my letter", or click
   **"Try it with a sample denial letter"** to skip straight to the
   questions with a built-in fictional example — no API key needed for
   that path, since nothing is actually uploaded or sent to the model.
2. **Reading the letter.** On upload, the app extracts the denial reason,
   deadline, and other facts, then checks them against a few routing
   rules. If the letter is out of scope (Medicare/Medicaid) or unreadable,
   you'll see why instead of moving on.
3. **Questions.** You're shown a plain-English explanation of the denial
   and a handful of questions tailored to the reason category. Answering
   some questions reveals a few more follow-up questions specific to your
   answers (for example, naming your treating physician only if you said
   a doctor recommended the treatment) — this can take one or two rounds
   depending on the category.
4. **Result page.** Shows the drafted appeal letter, a checklist of what to
   attach (check items off as you gather them — the checklist and the
   letter's readiness status update live), where to send it, your
   deadline, and any warnings worth double-checking before you send it.
   Anything the app couldn't determine from your letter or answers is
   marked `[[ FILL IN — ... ]]` rather than guessed — replace those before
   sending. Download the letter as a plain text file from this page.
5. **Reminder (optional).** Enter a name and an email and/or phone number
   to get reminded as your deadline approaches — see
   [reminders/README.md](reminders/README.md) for the schedule. This only
   sends anything for real if SMTP/Twilio are configured (see
   Configuration above); otherwise the reminder is scheduled but logged
   locally instead of delivered — nothing breaks, it just won't reach an
   actual inbox or phone.
6. **Delete my data.** Removes your case (and any reminder you set up)
   immediately. The uploaded document itself was already deleted right
   after it was read, regardless of whether you click this.

Case data lives in memory only and doesn't survive an app restart — see
Status below.

## Using the pieces directly, without the web app

Every script below can be run on its own — useful for testing, batch
processing, or if you'd rather script the loop than click through it.

**Extract structured data from one letter:**
```bash
python scripts/extract.py path/to/letter.pdf --out extraction.json
```
Add `--model claude-opus-5` to use a different model, or `--raw-cache
some.json` to reuse (and cache) a previous extraction instead of calling
the API again.

**Generate the appeal letter from an extraction + answers:**
```bash
python scripts/letter_generator.py extraction.json answers.json \
    --evidence-in-hand letter_of_medical_necessity,relevant_medical_records \
    --patient-contact contact.json --out result.json --letter-out letter.txt
```
`answers.json` is `{"question_id": answer, ...}` — see
[eval/fixtures/sample_001.answers.json](eval/fixtures/sample_001.answers.json)
for a worked example, and [playbook/](playbook/README.md) for what
questions exist per denial category. `--patient-contact` is a JSON file
with `{name, address, phone, email}`.

**Manage a deadline reminder case:**
```bash
python scripts/reminder.py create --extraction extraction.json --letter result.json \
    --contact contact.json --state reminders/state/case1.json
python scripts/reminder.py tick --state reminders/state/case1.json
python scripts/reminder.py tick-all --state-dir reminders/state/
python scripts/reminder.py mark-sent --state reminders/state/case1.json --sent true
python scripts/reminder.py report-outcome --state reminders/state/case1.json --outcome won
python scripts/reminder.py send-test --email you@example.com --phone +15551234567
python scripts/reminder.py delete --state reminders/state/case1.json
```
`tick` is meant to run once a day per case (`tick-all` for many at once) —
nothing here sets up that schedule; wire it into cron, a cloud scheduler,
or similar. Full detail in [reminders/README.md](reminders/README.md).

**Score extraction accuracy against hand-labelled letters:**
```bash
python scripts/run_eval.py --letters-dir eval/fixtures --labels-dir eval/fixtures
```
By default (no `--letters-dir`/`--labels-dir`) it looks in `eval/letters/`
and `eval/labels/`, which start out empty — that's where real (redacted)
labelled letters go once you have some; the command above runs it against
[eval/fixtures/](eval/fixtures/), the one checked-in synthetic example, in
the meantime. See [eval/README.md](eval/README.md), including how to run
this offline without an API key using a cached raw extraction.

**Check the playbook is internally consistent** (every template placeholder
has a matching question, ids line up, etc.) after editing anything in
`playbook/`:
```bash
python scripts/check_playbook.py
```

## How the pieces fit together

| Piece | What it does | Where |
|---|---|---|
| Extraction schema | The structured shape every denial letter gets read into | [schema/](schema/denial_extraction.schema.json), [schema/VALIDATION.md](schema/VALIDATION.md) |
| Extraction prompt + runner | Turns a document into that structure via the model | [prompts/](prompts/extraction_system_prompt.md), [scripts/extract.py](scripts/extract.py) |
| Eval | Scores extraction accuracy against hand-labelled letters | [eval/](eval/README.md) |
| Playbook | Per-denial-reason arguments, questions, checklist, aliases | [playbook/](playbook/README.md) |
| Letter generator | Turns extraction + question answers into a letter | [scripts/letter_generator.py](scripts/letter_generator.py) |
| Reminders | Deadline follow-up email/text, escalating as it nears | [reminders/](reminders/README.md) |
| Web wrapper | The upload page, question flow, and result page tying the above together | [app.py](app.py), [templates/](templates/) |

Each has its own README with the detail; this file is the map and the
quick-start.

## Status

MVP, not production. Before treating this as more than a working
demonstration of the loop:

- **Web app state is in memory only** — lost on restart, and doesn't work
  across multiple worker processes. A real deployment needs a session
  store (Redis, a database) behind the same interface `app.py` uses today.
- **The Flask dev server is not for production use** — it says so itself
  on startup. Put a real WSGI server in front of it before deploying.
- **Reminders can deliver for real** via SMTP and Twilio once configured
  (see [reminders/README.md#delivery](reminders/README.md#delivery)), but
  nothing schedules the daily check that actually sends them — that's a
  cron job or similar you'd add.
- **None of this has been tested against real denial letters yet.** A real
  upload does go through real extraction, not a fixture — but the eval
  suite and every worked example so far use one synthetic sample letter,
  so extraction accuracy on actual letters is still unmeasured. Collecting
  real (redacted) letters and labelling them is the next real step; see
  [eval/README.md](eval/README.md).
