#!/usr/bin/env python
"""The week-3 wrapper (see SCOPE.md): a dead-simple upload page, the
clarifying-question flow, and a result page — the thin UI layer around the
extraction/validation/playbook/letter-generator/reminder pieces built in
earlier weeks. Nothing here does any of the real work itself; every route
below is a few lines of glue calling into scripts/.

Run it:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...   # not needed for the /demo route
    python app.py
    # open http://127.0.0.1:5000

Storage (read this before treating this as more than a wrapper):
    Cases live in an in-memory dict (`CASES` below), swept for anything
    older than CASE_TTL. That means: state is lost on restart, and this
    does not work across multiple worker processes. That's deliberate for
    an MVP wrapper, not an oversight — a real deployment needs a session
    store (Redis, a database) behind the same CaseStore interface. Nothing
    here writes the uploaded document or the extraction to disk; the
    upload is deleted immediately after extraction (see upload()), success
    or failure, per schema/VALIDATION.md P2. The one thing that does
    persist to disk is a reminder case, and only if the user explicitly
    asks for one — see reminders/README.md for that retention policy.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from datetime import date

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

import letter_generator as lg  # noqa: E402
import reminder as rm  # noqa: E402
from extract import DEFAULT_MODEL, extract_document  # noqa: E402
from schema_utils import human_date, validate_instance  # noqa: E402
from validate_rules import run_validation  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
DEMO_EXTRACTION_PATH = os.path.join(ROOT, "eval", "fixtures", "sample_001.json")
UPLOAD_MAX_BYTES = 15 * 1024 * 1024  # 15MB
ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".txt", ".md"}
CASE_TTL_SECONDS = 2 * 60 * 60  # 2 hours

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or uuid.uuid4().hex
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_BYTES
app.jinja_env.filters["human_date"] = human_date  # {{ "2026-08-30" | human_date }} -> "August 30, 2026"


# ------------------------------------------------------------------ store --
# In-memory only — see the module docstring. Keyed by an opaque case id
# (never a claim number or member id) so there's no account system, per
# SCOPE.md's explicit MVP scope.

CASES: dict[str, dict] = {}


def _purge_expired() -> None:
    cutoff = time.time() - CASE_TTL_SECONDS
    for cid in [cid for cid, c in CASES.items() if c["_touched_at"] < cutoff]:
        del CASES[cid]


def new_case(extraction: dict) -> str:
    _purge_expired()
    case_id = uuid.uuid4().hex
    CASES[case_id] = {
        "extraction": extraction,
        "answers": {},
        "evidence_in_hand": set(),
        "letter_result": None,
        "reminder_case_id": None,
        "_touched_at": time.time(),
    }
    return case_id


def get_case(case_id: str) -> dict:
    _purge_expired()
    case = CASES.get(case_id)
    if case is None:
        abort(404)
    case["_touched_at"] = time.time()
    return case


# ------------------------------------------------------------------ helpers

def _playbook_for(case: dict):
    reason_category = (case["extraction"].get("denial", {}).get("reason_category") or {}).get("value")
    playbook, used_fallback = lg.load_playbook_entry(reason_category)
    return playbook, used_fallback


def _coerce_answer(question: dict, raw: str):
    if question["answer_type"] == "yes_no":
        return raw == "true"
    return raw  # free_text, date, choice are all plain strings from the form


# ------------------------------------------------------------------ routes

@app.route("/")
def index():
    return render_template("upload.html")


@app.route("/demo")
def demo():
    """Seeds a case straight from the checked-in sample fixture, skipping
    the upload + extraction call entirely. No API key needed — this is
    what lets the whole flow (questions, letter, checklist, reminder
    signup) be exercised and demoed without spending an API call.
    """
    import json

    with open(DEMO_EXTRACTION_PATH, encoding="utf-8") as f:
        extraction = json.load(f)
    case_id = new_case(extraction)
    flash("Loaded a sample denial letter — nothing was actually uploaded.", "info")
    return redirect(url_for("questions", case_id=case_id))


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("index"))

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        flash(f"'{ext}' isn't a supported file type. Upload a PDF, JPG, PNG, or a text file.", "error")
        return redirect(url_for("index"))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        flash(
            "This server doesn't have an API key configured, so it can't read uploads right now. "
            "Try the sample letter instead.", "error",
        )
        return redirect(url_for("index"))

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            extraction, _meta = extract_document(tmp_path, model=DEFAULT_MODEL)
        except Exception as e:  # noqa: BLE001 — surface a plain message, not a stack trace
            flash(f"We couldn't read that document: {e}", "error")
            return redirect(url_for("index"))
    finally:
        # The document never touches disk beyond this request, success or
        # failure — schema/VALIDATION.md P2.
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    schema_errors = validate_instance(extraction)
    if schema_errors:
        flash("We had trouble reading this document. Please try a clearer photo or a PDF.", "error")
        return redirect(url_for("index"))

    validation = run_validation(extraction)
    if validation.blocks:
        return render_template("blocked.html", blocks=validation.blocks)

    case_id = new_case(extraction)
    return redirect(url_for("questions", case_id=case_id))


@app.route("/questions/<case_id>", methods=["GET", "POST"])
def questions(case_id: str):
    case = get_case(case_id)
    playbook, used_fallback = _playbook_for(case)

    if request.method == "POST":
        pending_ids = {q["id"]: q for q in lg.pending_questions(playbook, case["answers"])}
        for qid, question in pending_ids.items():
            if qid in request.form:
                case["answers"][qid] = _coerce_answer(question, request.form[qid])
            elif question["answer_type"] == "yes_no":
                # An unanswered yes/no radio group just wasn't touched; leave pending.
                continue
            else:
                case["answers"][qid] = ""  # explicit "leave blank" for free_text/date/choice

    pending = lg.pending_questions(playbook, case["answers"])
    if request.method == "POST" and not pending:
        case["letter_result"] = lg.generate_letter(case["extraction"], case["answers"], evidence_in_hand=case["evidence_in_hand"])
        return redirect(url_for("result", case_id=case_id))

    validation = run_validation(case["extraction"])
    deadline = validation.computed_deadline
    days_left = (deadline - date.today()).days if deadline else None

    return render_template(
        "questions.html",
        case_id=case_id,
        playbook=playbook,
        used_fallback=used_fallback,
        questions=pending,
        answers=case["answers"],
        deadline=deadline,
        days_left=days_left,
        deadline_is_assumed=validation.computed_deadline_is_assumed,
        first_round=not case["answers"],
    )


@app.route("/result/<case_id>")
def result(case_id: str):
    case = get_case(case_id)
    if case["letter_result"] is None:
        return redirect(url_for("questions", case_id=case_id))
    return render_template("result.html", case_id=case_id, r=case["letter_result"])


@app.route("/result/<case_id>/checklist", methods=["POST"])
def update_checklist(case_id: str):
    """Re-runs the letter generator with whichever evidence items the user
    just checked off as "in hand" — updates the readiness verdict and the
    enclosures list without changing any answer.
    """
    case = get_case(case_id)
    case["evidence_in_hand"] = set(request.form.getlist("in_hand"))
    case["letter_result"] = lg.generate_letter(case["extraction"], case["answers"], evidence_in_hand=case["evidence_in_hand"])
    return redirect(url_for("result", case_id=case_id))


@app.route("/result/<case_id>/download")
def download(case_id: str):
    case = get_case(case_id)
    r = case["letter_result"]
    if not r or not r.get("letter_text"):
        abort(404)
    import io

    buf = io.BytesIO(r["letter_text"].encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="text/plain", as_attachment=True, download_name="appeal_letter.txt")


@app.route("/result/<case_id>/remind", methods=["POST"])
def remind(case_id: str):
    case = get_case(case_id)
    r = case["letter_result"]
    contact = {
        "name": request.form.get("name", "").strip(),
        "email": request.form.get("email", "").strip(),
        "phone": request.form.get("phone", "").strip(),
    }
    if not contact["email"] and not contact["phone"]:
        flash("Enter an email or a phone number so we have somewhere to send the reminder.", "error")
        return redirect(url_for("result", case_id=case_id))

    try:
        reminder_state = rm.create_case(case["extraction"], contact, letter_result=r)
    except rm.CaseBlockedError:
        flash("We can't set a reminder for this case.", "error")
        return redirect(url_for("result", case_id=case_id))

    state_path = os.path.join(ROOT, "reminders", "state", f"{reminder_state['case_id']}.json")
    rm.save_state(state_path, reminder_state)
    case["reminder_case_id"] = reminder_state["case_id"]
    flash(f"Reminder set — we'll check in as your deadline ({reminder_state['context']['deadline']}) gets close.", "success")
    return redirect(url_for("result", case_id=case_id))


@app.route("/case/<case_id>/delete", methods=["POST"])
def delete_case(case_id: str):
    case = CASES.pop(case_id, None)
    if case and case.get("reminder_case_id"):
        state_path = os.path.join(ROOT, "reminders", "state", f"{case['reminder_case_id']}.json")
        if os.path.exists(state_path):
            os.remove(state_path)
    flash("Your data has been deleted.", "success")
    return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="We couldn't find that. It may have expired — cases are cleared after a couple of hours."), 404


if __name__ == "__main__":
    app.run(debug=True)
