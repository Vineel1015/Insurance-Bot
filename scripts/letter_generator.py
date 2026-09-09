"""Assembles a ready-to-send (or needs-review) appeal letter from a validated
extraction, the matching playbook entry, and the user's answers to its
clarifying questions.

This module is deliberately deterministic and offline — no model call. Every
word that ends up in the letter comes from one of three places:

  1. the extraction (facts the letter itself stated)
  2. the user's answers to clarifying questions (facts the user stated)
  3. static playbook boilerplate (arguments, standing requests, checklist)

A playbook template can reference a narrative detail that isn't available
from any of those three sources (e.g. "{harm_of_delay}", "{diagnosis_plain}").
Rather than inventing that text, the generator leaves a clearly bracketed
`[[ FILL IN — ... ]]` marker and reports it as an unresolved gap, so nothing
resembling a fabricated fact reaches a letter a user might send. Closing
those gaps (more targeted questions, or a separately-guarded LLM narrative
pass) is future work — see playbook/README.md.

CLI:
    python scripts/letter_generator.py extraction.json answers.json
    python scripts/letter_generator.py extraction.json answers.json \
        --evidence-in-hand letter_of_medical_necessity,relevant_medical_records \
        --patient-contact contact.json --out result.json --letter-out letter.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema_utils import get_field, human_date, validate_instance  # noqa: E402
from validate_rules import run_validation  # noqa: E402

import yaml  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYBOOK_DIR = os.path.join(ROOT, "playbook")
FALLBACK_CATEGORY = "other"

STRENGTH_RANK = {"strong": 0, "medium": 1, "weak": 2}

# Argument ids that ask the insurer to disclose something. Included only when
# the extraction shows the letter didn't already disclose it — see
# schema/VALIDATION.md rule C4. An explicit allowlist, not a name pattern:
# some argument ids (e.g. `criteria_actually_met`) contain "criteria" but mean
# the opposite thing, so substring matching would be wrong here.
CRITERIA_GATED_ARGUMENTS = {"request_criteria", "request_policy"}
REVIEWER_GATED_ARGUMENTS = {"request_reviewer_credentials", "request_specialist_review", "request_reviewer"}

# The inverse: arguments that quote or respond to the insurer's own cited
# criteria (via {denial.criteria_cited.value}) only make sense when the
# letter actually cited some. Without this gate, an unconditional argument
# like criteria_actually_met would render "The denial cites ." when nothing
# was cited — exactly the case CRITERIA_GATED_ARGUMENTS's request_criteria
# exists to handle instead.
CRITERIA_REQUIRED_ARGUMENTS = {"criteria_actually_met"}

TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------- playbook ----

def load_playbook_entry(reason_category: str | None) -> tuple[dict, bool]:
    """Returns (playbook_dict, used_fallback)."""
    category = reason_category or FALLBACK_CATEGORY
    path = os.path.join(PLAYBOOK_DIR, f"{category}.yaml")
    if not os.path.exists(path):
        path = os.path.join(PLAYBOOK_DIR, f"{FALLBACK_CATEGORY}.yaml")
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f), True
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f), False


# --------------------------------------------------------- requirements ----

def requirement_met(expected, answer) -> bool:
    """Evaluates one {question_id: expected} predicate against an answer.
    Shared grammar for argument.requires.questions and a question's own
    depends_on (see playbook/_template.yaml) — the web app's question flow
    uses this same function to decide which follow-up questions to show.
    """
    if answer is None:
        return False
    if expected == "nonempty":
        return bool(str(answer).strip())
    if isinstance(expected, list):
        return answer in expected
    return answer == expected


def argument_questions_met(arg: dict, answers: dict) -> bool:
    required_questions = (arg.get("requires") or {}).get("questions") or {}
    for qid, expected in required_questions.items():
        if qid not in answers or not requirement_met(expected, answers[qid]):
            return False
    return True


def pending_questions(playbook: dict, answers: dict) -> list[dict]:
    """Clarifying questions not yet answered whose depends_on (if any) is
    satisfied by the answers so far — top-level questions are always
    included until answered; a follow-up appears only once its trigger
    question has been answered the right way. Used by the web app to drive
    a multi-round question flow without needing to know in advance how many
    rounds a category has (see playbook/README.md's "Closing placeholder
    gaps" section — depends_on chains are never more than one level deep).
    """
    pending = []
    for q in playbook["clarifying_questions"]:
        if q["id"] in answers:
            continue
        dep = q.get("depends_on") or {}
        if dep and not all(requirement_met(v, answers.get(k)) for k, v in dep.items()):
            continue
        pending.append(q)
    return pending


def select_arguments(playbook: dict, extraction: dict, answers: dict) -> list[dict]:
    """Arguments whose question requirements are satisfied by the answers
    given, minus the disclosure-request arguments that don't apply because
    the letter already disclosed the thing they'd ask for.
    """
    criteria_cited = get_field(extraction, "denial.criteria_cited")
    criteria_already_disclosed = bool((criteria_cited or {}).get("value"))

    reviewer = get_field(extraction, "denial.reviewer_credentials")
    reviewer_already_disclosed = bool((reviewer or {}).get("value"))

    selected = []
    for arg in playbook["arguments"]:
        if not arg.get("template", "").strip():
            continue  # control-only entries (e.g. other.yaml's route_to_category)
        if not argument_questions_met(arg, answers):
            continue
        if arg["id"] in CRITERIA_GATED_ARGUMENTS and criteria_already_disclosed:
            continue
        if arg["id"] in REVIEWER_GATED_ARGUMENTS and reviewer_already_disclosed:
            continue
        if arg["id"] in CRITERIA_REQUIRED_ARGUMENTS and not criteria_already_disclosed:
            continue
        selected.append(arg)

    selected.sort(key=lambda a: STRENGTH_RANK.get(a["strength"], 9))
    return selected


# ------------------------------------------------------------ resolution ----

def _flatten(node, prefix, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    else:
        out[prefix] = node


def _stringify(value) -> str | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    if isinstance(value, str) and ISO_DATE_RE.match(value):
        return human_date(value)
    if isinstance(value, str):
        return value
    return str(value)


def build_context(extraction: dict, answers: dict, computed_deadline: date | None) -> dict:
    """Combines extraction fields, user answers, and small deterministic
    derived values into one placeholder-resolution context. Never guesses —
    every value here is traceable to the extraction, an answer, or simple
    string arithmetic on one of those (e.g. taking a surname from a full
    name).
    """
    context: dict[str, str] = {}

    flat: dict = {}
    _flatten(extraction, "", flat)
    for path, value in flat.items():
        s = _stringify(value)
        if s is not None:
            context[path] = s

    for qid, value in (answers or {}).items():
        s = _stringify(value)
        if s is not None:
            context[qid] = s

    if computed_deadline:
        context["deadline"] = human_date(computed_deadline)

    return context


def humanize_token(token: str) -> str:
    return token.replace(".", " ").replace("_", " ")


_SENTENCE_START_RE = re.compile(r"(^|[.!?])\s*$")


def render_template(template: str, context: dict, unresolved: set[str]) -> str:
    """Substitutes {token} with its resolved value. When a token falls at the
    start of a sentence (string start, or right after ". "/"! "/"? "), the
    resolved value's first letter is capitalized — free-text answers are
    typically typed lowercase, and several templates insert them as their
    own sentence (e.g. "{cob_rule_applied}." after a period), so without this
    the letter would read "...secondary. it already processed...".
    """
    def repl(m: re.Match) -> str:
        token = m.group(1)
        at_sentence_start = bool(_SENTENCE_START_RE.search(template[: m.start()]))
        if token in context:
            val = context[token]
            if at_sentence_start and val:
                val = val[0].upper() + val[1:]
            return val
        unresolved.add(token)
        return f"[[ FILL IN — {humanize_token(token)} ]]"

    return TOKEN_RE.sub(repl, template)


# --------------------------------------------------------------- letter ----

_REPEATED_TERMINAL_PUNCT_RE = re.compile(r"([.!?])(\s*[.!?])+")


def _wrap_paragraph(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text.strip())
    # A free-text answer inserted as its own sentence may or may not already
    # end with terminal punctuation, and several templates add a "." after
    # the placeholder to cover the case where it doesn't. When the answer
    # *did* include one, that produces "...worsen.." or "...worsen. .": collapse
    # any run of terminal punctuation (with optional whitespace between) down
    # to the first mark, so the letter never carries a doubled period.
    return _REPEATED_TERMINAL_PUNCT_RE.sub(r"\1", text)


def build_evidence_checklist(
    playbook: dict,
    selected_arguments: list[dict],
    evidence_in_hand: set[str],
    context: dict,
    unresolved: set[str],
) -> list[dict]:
    """`how_to_get` text can itself carry {placeholders} (see
    not_medically_necessary.yaml's letter_of_medical_necessity item, which
    quotes a suggested message to the doctor's office including the
    deadline) — render it through the same resolver as argument templates,
    on the same terms: anything unresolved becomes an honest [[ FILL IN ]]
    and is added to `unresolved`, not a silently broken sentence.
    """
    referenced = set()
    for arg in selected_arguments:
        referenced |= set((arg.get("requires") or {}).get("evidence") or [])

    checklist = []
    for item in playbook["evidence_checklist"]:
        eid = item["id"]
        effective_required = bool(item.get("required")) or eid in referenced
        checklist.append({
            **item,
            "how_to_get": render_template(item.get("how_to_get", ""), context, unresolved),
            "required": effective_required,
            "referenced_by_letter": eid in referenced,
            "in_hand": eid in evidence_in_hand,
        })
    checklist.sort(key=lambda i: (not i["required"], i["id"]))
    return checklist


def _wants_expedited(pred_validation) -> bool:
    return any(f.rule in ("D7", "C5") for f in pred_validation.flags)


def _build_header(extraction: dict, playbook: dict, patient_contact: dict | None,
                   computed_deadline: date | None, unresolved: set[str]) -> str:
    def val(path: str) -> str | None:
        f = get_field(extraction, path)
        return _stringify(f.get("value")) if f else None

    patient_contact = patient_contact or {}
    lines = []

    name = patient_contact.get("name") or val("member.member_name") or None
    if not name:
        unresolved.add("patient_name")
        name = "[[ FILL IN — your name ]]"
    lines.append(name)
    for f in ("address", "phone", "email"):
        v = patient_contact.get(f)
        if v:
            lines.append(v)
        else:
            unresolved.add(f"patient_{f}")
            lines.append(f"[[ FILL IN — your {f} ]]")

    lines.append("")
    lines.append(human_date(date.today()))
    lines.append("")

    insurer_name = val("insurer.name") or "[[ FILL IN — insurer name ]]"
    address = val("appeal.submission_address")
    lines.append(insurer_name)
    if address:
        lines.append(address)
    else:
        lines.append("[[ FILL IN — appeal mailing address (check the last page of your denial letter) ]]")
    lines.append("")

    member_id = val("member.member_id") or "[[ FILL IN — member ID ]]"
    claim_number = val("member.claim_number")
    subject = f"Re: Appeal of Adverse Benefit Determination — Member ID {member_id}"
    if claim_number:
        subject += f", Claim #{claim_number}"
    lines.append(subject)
    if computed_deadline:
        lines.append(f"Appeal deadline: {human_date(computed_deadline)}")
    lines.append("")
    lines.append("To Whom It May Concern:")
    return "\n".join(lines)


def _build_opening(extraction: dict, context: dict, unresolved: set[str]) -> str:
    template = (
        "I am writing to appeal the denial of coverage for {service.description.value}, "
        "provided by {provider.provider_name.value} on {service.date_of_service_start.value}, "
        "as stated in your letter dated {document.letter_date.value}. I disagree with this "
        "decision for the reasons below."
    )
    return _wrap_paragraph(render_template(template, context, unresolved))


def generate_letter(
    extraction: dict,
    answers: dict,
    evidence_in_hand: set[str] | None = None,
    patient_contact: dict | None = None,
) -> dict:
    evidence_in_hand = evidence_in_hand or set()
    answers = answers or {}

    schema_errors = validate_instance(extraction)
    pred_validation = run_validation(extraction)

    if pred_validation.blocks:
        return {
            "status": "blocked",
            "blocks": [b.__dict__ for b in pred_validation.blocks],
            "letter_text": None,
        }

    reason_category = get_field(extraction, "denial.reason_category")
    playbook, used_fallback = load_playbook_entry((reason_category or {}).get("value"))

    selected_arguments = select_arguments(playbook, extraction, answers)
    context = build_context(extraction, answers, pred_validation.computed_deadline)

    unresolved: set[str] = set()

    header = _build_header(extraction, playbook, patient_contact, pred_validation.computed_deadline, unresolved)
    opening = _build_opening(extraction, context, unresolved)

    argument_details = []
    body_paragraphs = [opening]
    for arg in selected_arguments:
        rendered = _wrap_paragraph(render_template(arg["template"], context, unresolved))
        body_paragraphs.append(rendered)
        argument_details.append({
            "id": arg["id"], "strength": arg["strength"], "claim": arg["claim"], "rendered_text": rendered,
        })

    if _wants_expedited(pred_validation):
        body_paragraphs.append(
            "Given the urgency described above, I respectfully request that this appeal be "
            "handled on an expedited basis, with a decision as soon as possible."
        )

    standing_requests = playbook.get("standing_requests") or []
    if standing_requests:
        body_paragraphs.append("I additionally request the following:")
        body_paragraphs.append("\n".join(f"  {i + 1}. {r}" for i, r in enumerate(standing_requests)))

    checklist = build_evidence_checklist(playbook, selected_arguments, evidence_in_hand, context, unresolved)
    enclosures = [i["item"] for i in checklist if i["required"]]
    if enclosures:
        body_paragraphs.append("Enclosed with this letter:")
        body_paragraphs.append("\n".join(f"  - {e}" for e in enclosures))

    body_paragraphs.append(
        "Please contact me if you need any further information to process this appeal. "
        "I look forward to your response."
    )
    body_paragraphs.append("Sincerely,")
    signer = patient_contact.get("name") if patient_contact else None
    signer = signer or ((get_field(extraction, "member.member_name") or {}).get("value"))
    body_paragraphs.append(signer or "[[ FILL IN — your name ]]")

    letter_text = header + "\n\n" + "\n\n".join(body_paragraphs) + "\n"

    missing_required_evidence = [i["item"] for i in checklist if i["required"] and not i["in_hand"]]
    readiness_reasons = []
    if unresolved:
        readiness_reasons.append(f"{len(unresolved)} detail(s) in the letter still need your input.")
    if missing_required_evidence:
        readiness_reasons.append(f"{len(missing_required_evidence)} required attachment(s) not yet in hand.")
    if pred_validation.flags:
        readiness_reasons.append(f"{len(pred_validation.flags)} item(s) from the denial letter need your review.")
    if used_fallback:
        readiness_reasons.append("We couldn't confidently classify this denial's reason; answers weren't matched to a specific playbook.")

    return {
        "status": "generated",
        "readiness": "ready_to_send" if not readiness_reasons else "needs_review",
        "readiness_reasons": readiness_reasons,
        "reason_category": (reason_category or {}).get("value"),
        "used_fallback_playbook": used_fallback,
        "playbook_display_name": playbook["display_name"],
        "argument_details": argument_details,
        "unresolved_placeholders": sorted(unresolved),
        "evidence_checklist": checklist,
        "missing_required_evidence": missing_required_evidence,
        "submission": {
            "address": (get_field(extraction, "appeal.submission_address") or {}).get("value"),
            "fax": (get_field(extraction, "appeal.submission_fax") or {}).get("value"),
            "phone": (get_field(extraction, "appeal.submission_phone") or {}).get("value"),
            "portal_url": (get_field(extraction, "appeal.submission_portal_url") or {}).get("value"),
        },
        "deadline": pred_validation.computed_deadline.isoformat() if pred_validation.computed_deadline else None,
        "deadline_is_assumed": pred_validation.computed_deadline_is_assumed,
        "escalation": playbook.get("escalation"),
        "pitfalls": playbook.get("pitfalls"),
        "validation_flags": [f.__dict__ for f in pred_validation.flags],
        "schema_errors": schema_errors,
        "letter_text": letter_text,
    }


# ------------------------------------------------------------------ cli ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extraction", help="Path to an extraction JSON file (schema/denial_extraction.schema.json shape)")
    ap.add_argument("answers", help="Path to a JSON file of {question_id: answer}")
    ap.add_argument("--evidence-in-hand", default="", help="Comma-separated evidence_checklist ids the user already has")
    ap.add_argument("--patient-contact", help="Path to a JSON file with {name, address, phone, email}")
    ap.add_argument("--out", help="Write the full structured result JSON here")
    ap.add_argument("--letter-out", help="Write just the letter text here")
    args = ap.parse_args()

    with open(args.extraction, encoding="utf-8") as f:
        extraction = json.load(f)
    with open(args.answers, encoding="utf-8") as f:
        answers = json.load(f)

    patient_contact = None
    if args.patient_contact:
        with open(args.patient_contact, encoding="utf-8") as f:
            patient_contact = json.load(f)

    evidence_in_hand = {e.strip() for e in args.evidence_in_hand.split(",") if e.strip()}

    result = generate_letter(extraction, answers, evidence_in_hand, patient_contact)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    else:
        print(json.dumps({k: v for k, v in result.items() if k != "letter_text"}, indent=2))

    if result.get("letter_text"):
        if args.letter_out:
            with open(args.letter_out, "w", encoding="utf-8") as f:
                f.write(result["letter_text"])
        else:
            print("\n--- LETTER TEXT ---\n")
            print(result["letter_text"])

    return 0 if result["status"] == "generated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
