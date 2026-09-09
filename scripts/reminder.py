"""Step 5 of the MVP loop (see SCOPE.md): one deadline reminder before the
appeal window closes, escalating to daily as the deadline nears, plus a
"did you send it?" confirmation and a later outcome check-in.

The scheduling and content logic is deliberately deterministic — no model
call. It answers two questions given a case and today's date: "should a
reminder go out today, and what should it say?" Actually sending the
message is behind a small `Sender` seam. Sender.from_env() delivers for
real via SMTP (smtp_send_email — SendGrid, Postmark, SES, Mailgun, Gmail,
or any other SMTP relay) and Twilio (twilio_send_sms) the moment their env
vars are set — see each function's docstring — and falls back to logging
to a local outbox file, per channel, otherwise. Run
`python scripts/reminder.py send-test --email you@example.com` to check a
provider is actually configured correctly before relying on it.

Privacy (see schema/VALIDATION.md P1-P3): a case's reminder state stores
only what the reminders themselves need — contact info the user gave for
this purpose, and a handful of case facts (insurer name, service
description, deadline, submission route, missing evidence) — never the
full extraction, the source document, or member/claim identifiers. That
lets the original document and extraction be deleted per the default
retention policy while the reminder still works, because create_case()
copies out what it needs once and the raw extraction is never touched
again.

CLI:
    python scripts/reminder.py create --extraction eval/fixtures/sample_001.json \
        --letter result.json --contact contact.json --state cases/abc123.json
    python scripts/reminder.py tick --state cases/abc123.json --today 2026-08-20
    python scripts/reminder.py tick-all --state-dir cases/
    python scripts/reminder.py mark-sent --state cases/abc123.json --sent true
    python scripts/reminder.py report-outcome --state cases/abc123.json --outcome won
    python scripts/reminder.py delete --state cases/abc123.json
    python scripts/reminder.py send-test --email you@example.com --phone +15551234567
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema_utils import get_field, human_date  # noqa: E402
from validate_rules import run_validation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTBOX = os.path.join(ROOT, "reminders", "outbox.jsonl")

# --- cadence policy --------------------------------------------------------
# Minimum days between reminders, keyed by how many days remain. Recomputed
# every tick from the stored deadline and today's date, so it self-heals
# regardless of when the case was created or whether a tick was missed —
# no fragile "exactly 10 days out" checkpoint matching.
DAILY_THRESHOLD_DAYS = 14      # schema/VALIDATION.md D7: under two weeks -> daily
WEEKLY_START_DAYS = 21         # start reminding at three weeks out
OUTCOME_CHECK_DELAY_DAYS = 45  # first "how did it go?" after a confirmed send
OUTCOME_CHECK_REPEAT_DAYS = 30
MAX_OUTCOME_CHECKS = 2

PRE_DEADLINE = "pre_deadline"
DEADLINE_DAY = "deadline_day"
POST_DEADLINE = "post_deadline"
SENT_ACK = "sent_ack"
OUTCOME_CHECK = "outcome_check"


# ------------------------------------------------------------- case state --

def _today(value: str | None = None) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return date.today()


def new_case_id() -> str:
    return uuid.uuid4().hex[:12]


class CaseBlockedError(Exception):
    """Raised by create_case() when the extraction fails a schema/VALIDATION.md
    routing gate (out of scope, unreadable, etc.) — mirrors the `blocked`
    status letter_generator.py returns, so a reminder case is never created
    for a document the rest of the pipeline already refused to act on.
    """


def build_context(extraction: dict, letter_result: dict | None = None) -> dict:
    """Extracts the minimal, non-identifying set of facts a reminder needs,
    from an extraction (already schema-validated and run through
    validate_rules elsewhere) and an optional letter_generator result.
    """
    v = run_validation(extraction)

    def val(path: str):
        f = get_field(extraction, path)
        return f.get("value") if f else None

    context = {
        "insurer_name": val("insurer.name"),
        "service_description": val("service.description"),
        "deadline": v.computed_deadline.isoformat() if v.computed_deadline else None,
        "deadline_is_assumed": v.computed_deadline_is_assumed,
        "concurrent_care": val("denial.timing") == "concurrent",
        "submission": {
            "address": val("appeal.submission_address"),
            "fax": val("appeal.submission_fax"),
            "phone": val("appeal.submission_phone"),
            "portal_url": val("appeal.submission_portal_url"),
        },
        "missing_required_evidence": [],
    }
    if letter_result:
        context["missing_required_evidence"] = letter_result.get("missing_required_evidence") or []
        if letter_result.get("deadline"):
            context["deadline"] = letter_result["deadline"]
            context["deadline_is_assumed"] = letter_result.get("deadline_is_assumed", context["deadline_is_assumed"])
        if letter_result.get("submission"):
            context["submission"] = letter_result["submission"]
    return context


def create_case(
    extraction: dict,
    contact: dict,
    letter_result: dict | None = None,
    case_id: str | None = None,
    created_at: date | None = None,
) -> dict:
    """Builds a fresh reminder-state dict. Caller is responsible for saving
    it (save_state) — kept separate so callers can inspect/adjust before
    persisting. Raises CaseBlockedError if the extraction fails a routing
    gate (see schema/VALIDATION.md R1-R3) — e.g. out of scope or unreadable.
    """
    blocks = run_validation(extraction).blocks
    if blocks:
        raise CaseBlockedError("; ".join(f"{b.rule}: {b.message}" for b in blocks))

    created_at = created_at or date.today()
    return {
        "case_id": case_id or new_case_id(),
        "contact": {
            "name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
        },
        "created_at": created_at.isoformat(),
        "context": build_context(extraction, letter_result),
        "sent_confirmed": None,       # None = unknown, True/False = user told us
        "sent_confirmed_at": None,
        "outcome": None,              # "won" | "denied" | "waiting" | None
        "outcome_reported_at": None,
        "reminders_sent": [],         # [{"date": "...", "kind": "..."}]
    }


def load_state(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------- cadence --

def _last_reminder_date(state: dict) -> date | None:
    if not state["reminders_sent"]:
        return None
    return _today(state["reminders_sent"][-1]["date"])


def _has_kind(state: dict, kind: str) -> date | None:
    for r in reversed(state["reminders_sent"]):
        if r["kind"] == kind:
            return _today(r["date"])
    return None


def decide(state: dict, today: date | None = None) -> str | None:
    """Returns the message kind to send today, or None if nothing is due.
    Pure function of state + today — safe to call repeatedly (e.g. from a
    dry-run) without side effects.
    """
    today = today or date.today()

    if _last_reminder_date(state) == today:
        return None  # never more than one message a day, regardless of branch

    ctx = state["context"]
    deadline = _today(ctx["deadline"]) if ctx.get("deadline") else None

    if state.get("sent_confirmed") is True:
        if state.get("outcome"):
            return None  # case closed
        checks_done = sum(1 for r in state["reminders_sent"] if r["kind"] == OUTCOME_CHECK)
        if checks_done >= MAX_OUTCOME_CHECKS:
            return None
        sent_at = _today(state["sent_confirmed_at"])
        last_check = _has_kind(state, OUTCOME_CHECK)
        due_at = last_check + timedelta(days=OUTCOME_CHECK_REPEAT_DAYS) if last_check else sent_at + timedelta(days=OUTCOME_CHECK_DELAY_DAYS)
        return OUTCOME_CHECK if today >= due_at else None

    if deadline is None:
        return None  # nothing to schedule against; VALIDATION.md D3 should have flagged this upstream

    days_left = (deadline - today).days

    if days_left < 0:
        return None if _has_kind(state, POST_DEADLINE) else POST_DEADLINE

    if days_left == 0:
        return DEADLINE_DAY

    urgent = days_left < DAILY_THRESHOLD_DAYS or ctx.get("concurrent_care")
    if urgent:
        min_gap = 1
    elif days_left <= WEEKLY_START_DAYS:
        min_gap = 7
    else:
        return None  # too early to start

    last = _last_reminder_date(state)
    if last is not None and (today - last).days < min_gap:
        return None
    return PRE_DEADLINE


# ---------------------------------------------------------------- content --


def _submission_line(ctx: dict) -> str:
    s = ctx.get("submission") or {}
    parts = []
    if s.get("portal_url"):
        parts.append(f"online at {s['portal_url']}")
    if s.get("fax"):
        parts.append(f"by fax to {s['fax']}")
    if s.get("address"):
        parts.append(f"by mail to {s['address']}")
    if not parts:
        return "Check your denial letter or your insurance card for where to send it."
    return "You can send it " + "; or ".join(parts) + "."


def _missing_line(ctx: dict) -> str:
    items = ctx.get("missing_required_evidence") or []
    if not items:
        return ""
    return "Still needed before you send it: " + "; ".join(items) + "."


def _assumed_note(ctx: dict) -> str:
    if not ctx.get("deadline_is_assumed"):
        return ""
    return " (We couldn't find an exact deadline in your letter, so this date is an estimate — please double-check it against the letter.)"


def render_email(kind: str, state: dict, today: date) -> dict:
    ctx = state["context"]
    name = state["contact"].get("name") or "there"
    insurer = ctx.get("insurer_name") or "your insurer"
    service = ctx.get("service_description") or "your claim"
    deadline_str = human_date(ctx["deadline"]) if ctx.get("deadline") else "your deadline"
    days_left = (_today(ctx["deadline"]) - today).days if ctx.get("deadline") else None

    if kind == PRE_DEADLINE:
        subject = f"Your appeal deadline is in {days_left} day{'s' if days_left != 1 else ''} — did you send it?"
        body = (
            f"Hi {name},\n\n"
            f"Just checking in about your appeal to {insurer} for {service}.\n\n"
            f"Your appeal deadline is {deadline_str} — that's {days_left} day"
            f"{'s' if days_left != 1 else ''} away.{_assumed_note(ctx)}\n\n"
            "Have you sent your appeal letter yet?\n"
            "  - If yes, reply SENT and we'll stop reminding you about the deadline. "
            "We'll check back later to see how it went.\n"
            "  - If not yet, here's what you need:\n"
            f"    {_missing_line(ctx) or 'You have everything you need — send it when ready.'}\n"
            f"    {_submission_line(ctx)}\n"
        )
    elif kind == DEADLINE_DAY:
        subject = f"Today is your appeal deadline for {insurer}"
        body = (
            f"Hi {name},\n\n"
            f"Today, {deadline_str}, is the deadline to appeal the denial for {service} "
            f"from {insurer}.{_assumed_note(ctx)}\n\n"
            "If you haven't sent it yet, the fastest options are usually a fax or an "
            "online portal, if your insurer offers one — mail may not arrive in time.\n"
            f"    {_submission_line(ctx)}\n\n"
            "Have you sent it? Reply SENT once you have, or NOT YET if you need more time — "
            "a late appeal is sometimes still accepted, so don't give up if you miss today.\n"
        )
    elif kind == POST_DEADLINE:
        subject = "Your appeal deadline may have passed — here's what to do"
        body = (
            f"Hi {name},\n\n"
            f"Your appeal deadline for {insurer} ({deadline_str}) has passed, and we haven't "
            "heard back from you.\n\n"
            "If you haven't sent your appeal:\n"
            "  - Send it anyway. Insurers sometimes accept a late appeal, especially with a "
            "brief explanation of the delay.\n"
            "  - Ask about external review — depending on the type of denial, you may still "
            "be able to request an independent review even after the internal appeal window.\n\n"
            "Reply SENT if you've sent it, or let us know if you need help with next steps.\n"
        )
    elif kind == SENT_ACK:
        body = (
            f"Hi {name},\n\n"
            f"Got it — thanks for letting us know you sent your appeal to {insurer}. "
            "We'll check back in a few weeks to see how it went. In the meantime, hang onto "
            "any confirmation you have that it was sent (a fax confirmation, mailing receipt, "
            "or portal submission number).\n"
        )
        subject = "Got it — we'll check back on your appeal"
    elif kind == OUTCOME_CHECK:
        subject = f"Any update on your appeal to {insurer}?"
        body = (
            f"Hi {name},\n\n"
            f"A little while ago you sent an appeal to {insurer} for {service}. "
            "Have you heard back?\n\n"
            "Reply WON if it was approved, DENIED if it was denied again, or WAITING if "
            "you haven't heard yet.\n"
        )
    else:
        raise ValueError(f"unknown message kind {kind!r}")

    return {"subject": subject, "body": body}


def render_sms(kind: str, state: dict, today: date) -> str:
    ctx = state["context"]
    insurer = ctx.get("insurer_name") or "your insurer"
    deadline_str = human_date(ctx["deadline"]) if ctx.get("deadline") else "soon"
    days_left = (_today(ctx["deadline"]) - today).days if ctx.get("deadline") else None

    if kind == PRE_DEADLINE:
        return f"Reminder: your appeal to {insurer} is due {deadline_str} ({days_left}d left). Sent it? Reply SENT or NOT YET."
    if kind == DEADLINE_DAY:
        return f"TODAY ({deadline_str}) is your appeal deadline for {insurer}. Sent it? Reply SENT or NOT YET."
    if kind == POST_DEADLINE:
        return f"Your appeal deadline for {insurer} may have passed. Late appeals + external review may still be options. Reply SENT if you've sent it."
    if kind == SENT_ACK:
        return f"Got it — you sent your appeal to {insurer}. We'll check back in a few weeks to see how it went."
    if kind == OUTCOME_CHECK:
        return f"Any word back on your {insurer} appeal? Reply WON, DENIED, or WAITING."
    raise ValueError(f"unknown message kind {kind!r}")


# --------------------------------------------------------------- delivery --
# Real email/SMS providers plug in here. Both concrete providers below are
# stdlib-only (smtplib / urllib) — no vendor SDK dependency for what is,
# underneath, one SMTP conversation and one HTTP POST. Sender.from_env()
# picks them up automatically when configured, per-channel, and otherwise
# falls back to logging to a local outbox file (gitignored — it contains
# contact info) so the scheduling and content logic can be exercised and
# inspected without any vendor account, and so a deployment with only one
# channel configured doesn't break the other.

def smtp_send_email(to: str, subject: str, body: str) -> None:
    """Sends one plain-text email over SMTP. Works with SendGrid, Postmark,
    AWS SES, Mailgun, Gmail, or any other provider's SMTP relay — every
    major transactional-email provider offers one alongside its REST API,
    so pointing these env vars at it is enough; no provider-specific code.

    Env vars:
        SMTP_HOST        required
        SMTP_FROM        required (falls back to SMTP_USERNAME if unset)
        SMTP_PORT        default 587
        SMTP_USERNAME    optional — omit only for an unauthenticated relay
        SMTP_PASSWORD    optional, paired with SMTP_USERNAME
        SMTP_USE_SSL     default false (STARTTLS on SMTP_PORT). Set true
                          for implicit TLS (typically port 465) instead.
    """
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM") or username
    if not from_addr:
        raise RuntimeError("SMTP_FROM (or SMTP_USERNAME) must be set to send email")
    use_ssl = os.environ.get("SMTP_USE_SSL", "false").strip().lower() in ("1", "true", "yes")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.set_content(body)

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=15) as smtp:
        if not use_ssl:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)


def twilio_send_sms(to: str, text: str) -> None:
    """Sends one SMS via Twilio's REST API.

    Env vars (all required): TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER.
    """
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({"To": to, "From": from_number, "Body": text}).encode()
    creds = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Twilio API returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Twilio API error {e.code}: {detail}") from e


@dataclass
class Sender:
    send_email: Callable[[str, str, str], None] = field(default=None)
    send_sms: Callable[[str, str], None] = field(default=None)

    def __post_init__(self):
        self.send_email = self.send_email or self._console_email
        self.send_sms = self.send_sms or self._console_sms

    def _console_email(self, to: str, subject: str, body: str) -> None:
        _append_outbox({"channel": "email", "to": to, "subject": subject, "body": body})

    def _console_sms(self, to: str, text: str) -> None:
        _append_outbox({"channel": "sms", "to": to, "text": text})

    @classmethod
    def from_env(cls) -> "Sender":
        """A Sender using real providers wherever configured, falling back
        to the outbox logger per channel otherwise — an environment with
        only SMTP_HOST set still logs SMS to the outbox instead of raising,
        and vice versa. This is what tick()/tick-all/mark-sent use by
        default; pass an explicit Sender only to override or in tests.
        """
        return cls(
            send_email=smtp_send_email if os.environ.get("SMTP_HOST") else None,
            send_sms=twilio_send_sms if os.environ.get("TWILIO_ACCOUNT_SID") else None,
        )


def _append_outbox(record: dict, path: str = DEFAULT_OUTBOX) -> None:
    record = {"sent_at": datetime.now().isoformat(timespec="seconds"), **record}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # newline="" so Python doesn't translate \n to \r\n on Windows — this is
    # a JSONL log, one bare \n per line, portable to any reader.
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(record) + "\n")


def _send_channel(send_fn: Callable, *args, channel: str, to: str) -> bool:
    """Runs one send, catching and logging any failure rather than raising
    — a bad phone number or a provider outage must never crash a tick,
    especially tick-all working through many cases in one run. Returns
    whether it succeeded, so the caller can decide whether to retry.
    """
    try:
        send_fn(*args)
        return True
    except Exception as e:  # noqa: BLE001 — deliberately broad: any provider failure lands here
        print(f"[reminder] failed to send {channel} to {to}: {e}", file=sys.stderr)
        _append_outbox({"channel": channel, "to": to, "status": "failed", "error": str(e)})
        return False


# --------------------------------------------------------------- tick/run --

def _dispatch(state: dict, kind: str, today: date, sender: Sender) -> bool:
    """Renders and sends one message kind to every configured contact
    channel. Returns whether at least one channel delivered — a total
    failure across every attempted channel means nothing reached the user,
    so the caller should not mark this as sent.
    """
    email = render_email(kind, state, today)
    sms = render_sms(kind, state, today)
    contact = state["contact"]
    attempted = False
    delivered = False
    if contact.get("email"):
        attempted = True
        delivered |= _send_channel(sender.send_email, contact["email"], email["subject"], email["body"], channel="email", to=contact["email"])
    if contact.get("phone"):
        attempted = True
        delivered |= _send_channel(sender.send_sms, contact["phone"], sms, channel="sms", to=contact["phone"])
    return delivered or not attempted


def tick(state: dict, today: date | None = None, sender: Sender | None = None) -> dict:
    """Decides whether a reminder is due and, if so, renders and sends it,
    then updates and returns the state. Does not save to disk — callers
    (the CLI, or a scheduled job) do that so this stays a pure decision +
    side-effect step, easy to test. Defaults to Sender.from_env(), so this
    delivers for real the moment SMTP_HOST / TWILIO_ACCOUNT_SID are set —
    no code change needed to go from logging-only to live.
    """
    today = today or date.today()
    sender = sender or Sender.from_env()

    kind = decide(state, today)
    if kind is None:
        return state

    if not _dispatch(state, kind, today, sender):
        return state  # every channel failed — leave unmarked so the next tick retries

    state["reminders_sent"].append({"date": today.isoformat(), "kind": kind})
    return state


def mark_sent(state: dict, sent: bool, today: date | None = None, sender: Sender | None = None) -> dict:
    today = today or date.today()
    state["sent_confirmed"] = sent
    if sent:
        state["sent_confirmed_at"] = today.isoformat()
        sender = sender or Sender.from_env()
        _dispatch(state, SENT_ACK, today, sender)
        state["reminders_sent"].append({"date": today.isoformat(), "kind": SENT_ACK})
    return state


def report_outcome(state: dict, outcome: str, today: date | None = None) -> dict:
    if outcome not in ("won", "denied", "waiting"):
        raise ValueError("outcome must be one of: won, denied, waiting")
    today = today or date.today()
    state["outcome"] = outcome
    state["outcome_reported_at"] = today.isoformat()
    return state


# ------------------------------------------------------------------- cli --

def _cmd_create(args) -> int:
    with open(args.extraction, encoding="utf-8") as f:
        extraction = json.load(f)
    letter_result = None
    if args.letter:
        with open(args.letter, encoding="utf-8") as f:
            letter_result = json.load(f)
    with open(args.contact, encoding="utf-8") as f:
        contact = json.load(f)
    try:
        state = create_case(extraction, contact, letter_result)
    except CaseBlockedError as e:
        print(f"Not creating a reminder case — this document didn't pass the pipeline's routing checks: {e}", file=sys.stderr)
        return 1
    save_state(args.state, state)
    print(f"Created case {state['case_id']} -> {args.state}")
    print(f"  deadline: {state['context']['deadline']} (assumed={state['context']['deadline_is_assumed']})")
    return 0


def _cmd_tick(args) -> int:
    state = load_state(args.state)
    today = _today(args.today)
    before = len(state["reminders_sent"])
    state = tick(state, today)
    if len(state["reminders_sent"]) > before:
        kind = state["reminders_sent"][-1]["kind"]
        print(f"Sent {kind} reminder for case {state['case_id']} on {today.isoformat()}")
    else:
        print(f"Nothing due for case {state['case_id']} on {today.isoformat()}")
    save_state(args.state, state)
    return 0


def _cmd_tick_all(args) -> int:
    today = _today(args.today)
    n_sent = 0
    n_errored = 0
    for name in sorted(os.listdir(args.state_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(args.state_dir, name)
        try:
            state = load_state(path)
            before = len(state["reminders_sent"])
            state = tick(state, today)
            if len(state["reminders_sent"]) > before:
                n_sent += 1
                print(f"Sent {state['reminders_sent'][-1]['kind']} for {state['case_id']}")
            save_state(path, state)
        except Exception as e:  # noqa: BLE001 — one bad case must not abort the whole run
            n_errored += 1
            print(f"[reminder] error processing {name}: {e}", file=sys.stderr)
    print(f"{n_sent} reminder(s) sent across {args.state_dir} for {today.isoformat()}"
          + (f" ({n_errored} case(s) errored, see stderr)" if n_errored else ""))
    return 0


def _cmd_mark_sent(args) -> int:
    state = load_state(args.state)
    sent = args.sent.lower() in ("true", "yes", "1")
    state = mark_sent(state, sent, _today(args.today))
    save_state(args.state, state)
    print(f"case {state['case_id']}: sent_confirmed={sent}")
    return 0


def _cmd_report_outcome(args) -> int:
    state = load_state(args.state)
    state = report_outcome(state, args.outcome, _today(args.today))
    save_state(args.state, state)
    print(f"case {state['case_id']}: outcome={args.outcome}")
    return 0


def _cmd_delete(args) -> int:
    if os.path.exists(args.state):
        os.remove(args.state)
        print(f"Deleted {args.state}")
    else:
        print(f"{args.state} does not exist")
    return 0


def _cmd_send_test(args) -> int:
    """Fires one real message through whatever's configured in the
    environment, without needing a case — for checking SMTP/Twilio
    credentials actually work before relying on the daily cadence to
    surface a problem.
    """
    if not args.email and not args.phone:
        print("Pass --email and/or --phone to send a test message to.", file=sys.stderr)
        return 1
    sender = Sender.from_env()
    ok = True
    if args.email:
        configured = os.environ.get("SMTP_HOST") is not None
        print(f"Sending test email to {args.email} via {'SMTP' if configured else 'the outbox (SMTP_HOST not set)'}...")
        ok &= _send_channel(sender.send_email, args.email, "Appeal Helper test email",
                             "This is a test message from `python scripts/reminder.py send-test`. "
                             "If you received this, SMTP is configured correctly.",
                             channel="email", to=args.email)
    if args.phone:
        configured = os.environ.get("TWILIO_ACCOUNT_SID") is not None
        print(f"Sending test SMS to {args.phone} via {'Twilio' if configured else 'the outbox (TWILIO_ACCOUNT_SID not set)'}...")
        ok &= _send_channel(sender.send_sms, args.phone,
                             "Appeal Helper test SMS. If you received this, Twilio is configured correctly.",
                             channel="sms", to=args.phone)
    if not ok:
        print("At least one send failed — see the error above.", file=sys.stderr)
        return 1
    print("Done. If a provider wasn't configured, check reminders/outbox.jsonl instead of an inbox.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="Seed a new reminder case from an extraction (+ optional letter result)")
    p.add_argument("--extraction", required=True)
    p.add_argument("--letter", help="Optional letter_generator.py result JSON, for richer message content")
    p.add_argument("--contact", required=True, help="JSON file with {name, email, phone}")
    p.add_argument("--state", required=True, help="Where to write the new case's state file")
    p.set_defaults(func=_cmd_create)

    p = sub.add_parser("tick", help="Check one case and send a reminder if due")
    p.add_argument("--state", required=True)
    p.add_argument("--today", help="YYYY-MM-DD, default today (for testing)")
    p.set_defaults(func=_cmd_tick)

    p = sub.add_parser("tick-all", help="Check every case in a directory and send reminders that are due")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--today", help="YYYY-MM-DD, default today (for testing)")
    p.set_defaults(func=_cmd_tick_all)

    p = sub.add_parser("mark-sent", help="Record the user's yes/no answer to 'did you send it?'")
    p.add_argument("--state", required=True)
    p.add_argument("--sent", required=True, choices=["true", "false"])
    p.add_argument("--today", help="YYYY-MM-DD, default today (for testing)")
    p.set_defaults(func=_cmd_mark_sent)

    p = sub.add_parser("report-outcome", help="Record the appeal's outcome")
    p.add_argument("--state", required=True)
    p.add_argument("--outcome", required=True, choices=["won", "denied", "waiting"])
    p.add_argument("--today", help="YYYY-MM-DD, default today (for testing)")
    p.set_defaults(func=_cmd_report_outcome)

    p = sub.add_parser("delete", help="Permanently delete a case's reminder state")
    p.add_argument("--state", required=True)
    p.set_defaults(func=_cmd_delete)

    p = sub.add_parser("send-test", help="Send one real test message via whatever's configured in the environment, no case needed")
    p.add_argument("--email", help="Send a test email here")
    p.add_argument("--phone", help="Send a test SMS here")
    p.set_defaults(func=_cmd_send_test)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
