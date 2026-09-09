"""Code-level validation on top of LLM extraction output.

Implements the checkable rules from schema/VALIDATION.md as pure functions
over an extraction dict that already passed JSON-schema validation. Rules
that require product/legal judgement calls (e.g. C3, C5, D5, K5, E3, E4) are
left as TODOs referencing their rule id in VALIDATION.md rather than guessed
at here.

Usage:
    from validate_rules import run_validation
    result = run_validation(extraction_dict)
    result.blocks   # list[Finding] — do not proceed
    result.flags    # list[Finding] — proceed, but show the user
    result.fixed    # extraction dict with normalization fixes applied
    result.computed_deadline   # date | None, the canonical deadline
"""
from __future__ import annotations

import copy
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema_utils import walk_leaf_fields  # noqa: E402

CPT_RE = re.compile(r"^\d{5}$")
HCPCS_RE = re.compile(r"^[A-Z]\d{4}$")
ICD10_RE = re.compile(r"^[A-TV-Z]\d[0-9A-Z](\.[0-9A-Z]{1,4})?$")
DIGITS_RE = re.compile(r"\D")

OUT_OF_SCOPE_COVERAGE = {"medicare", "medicare_advantage", "medicaid", "tricare", "va"}
COMMON_WINDOWS = {30, 45, 60, 90, 120, 180, 365}
CONSERVATIVE_MAIL_DAYS = 5
CONSERVATIVE_FALLBACK_DAYS = 60


@dataclass
class Finding:
    rule: str
    message: str
    field: str | None = None


@dataclass
class ValidationResult:
    blocks: list[Finding] = field(default_factory=list)
    flags: list[Finding] = field(default_factory=list)
    fixed: dict = field(default_factory=dict)
    computed_deadline: date | None = None
    computed_deadline_is_assumed: bool = False
    reminder_deadline: date | None = None

    def to_dict(self) -> dict:
        return {
            "blocks": [f.__dict__ for f in self.blocks],
            "flags": [f.__dict__ for f in self.flags],
            "computed_deadline": self.computed_deadline.isoformat() if self.computed_deadline else None,
            "computed_deadline_is_assumed": self.computed_deadline_is_assumed,
            "reminder_deadline": self.reminder_deadline.isoformat() if self.reminder_deadline else None,
        }


def _v(field_obj: dict | None):
    """Value of a {value, confidence, evidence} field, or None if the field itself is missing."""
    return field_obj.get("value") if field_obj else None


def _c(field_obj: dict | None) -> float:
    return field_obj.get("confidence") or 0 if field_obj else 0


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def run_validation(extraction: dict, today: date | None = None) -> ValidationResult:
    today = today or date.today()
    fixed = copy.deepcopy(extraction)
    result = ValidationResult(fixed=fixed)

    _run_gates(fixed, result)
    _run_deadline_rules(fixed, result, today)
    _run_category_rules(fixed, result)
    _run_code_normalization(fixed, result)
    _run_submission_rules(fixed, result)
    _run_evidence_rules(fixed, result)

    return result


# ---------------------------------------------------------------- gates ----

def _run_gates(fx: dict, r: ValidationResult) -> None:
    doc_type = _v(fx.get("document", {}).get("document_type"))
    if doc_type not in ("denial_letter", "prior_auth_denial"):
        messages = {
            "eob_with_denial": "This looks like an Explanation of Benefits. Your insurer also sent a denial letter — that's the one we need.",
            "appeal_decision": "This is a response to an appeal you already filed. Level-2 or external review isn't supported yet.",
            "not_a_denial": "This document doesn't look like a denial letter.",
            "unknown": "We couldn't tell what kind of document this is.",
            None: "We couldn't tell what kind of document this is.",
        }
        r.blocks.append(Finding("R1", messages.get(doc_type, messages["unknown"]), "document.document_type"))

    coverage = _v(fx.get("insurer", {}).get("coverage_type"))
    if coverage in OUT_OF_SCOPE_COVERAGE:
        r.blocks.append(Finding("R2", "Medicare and Medicaid appeals follow a different process we don't support yet.", "insurer.coverage_type"))

    ocr = fx.get("extraction_meta", {}).get("ocr_quality")
    if ocr == "poor":
        r.blocks.append(Finding("R3", "The document is too hard to read. Please upload a clearer photo or a PDF.", "extraction_meta.ocr_quality"))

    warnings = fx.get("extraction_meta", {}).get("warnings") or []
    if any("page" in w.lower() and ("missing" in w.lower() or "jump" in w.lower()) for w in warnings):
        r.flags.append(Finding("R4", "This letter may be missing a page. Appeal instructions are usually on the last page.", "document.page_count"))


# ------------------------------------------------------------- deadline ----

def _resolve_anchor_date(fx: dict, anchor: str | None) -> tuple[date | None, bool]:
    """Returns (anchor_date, is_assumed)."""
    if anchor == "letter_date":
        return _parse_date(_v(fx.get("document", {}).get("letter_date"))), False
    if anchor == "date_received":
        letter_date = _parse_date(_v(fx.get("document", {}).get("letter_date")))
        if letter_date:
            return letter_date + timedelta(days=CONSERVATIVE_MAIL_DAYS), True
        return None, True
    if anchor == "date_of_service":
        svc = fx.get("service", {})
        d = _v(svc.get("date_of_service_end")) or _v(svc.get("date_of_service_start"))
        return _parse_date(d), False
    if anchor == "date_of_eob":
        # Not separately captured; fall back to letter_date as an approximation.
        return _parse_date(_v(fx.get("document", {}).get("letter_date"))), True
    return None, True


def _run_deadline_rules(fx: dict, r: ValidationResult, today: date) -> None:
    appeal = fx.get("appeal", {})
    document = fx.get("document", {})
    service = fx.get("service", {})
    denial = fx.get("denial", {})

    deadline_date = _parse_date(_v(appeal.get("deadline_date")))
    days_stated = _v(appeal.get("deadline_days_stated"))
    anchor = _v(appeal.get("deadline_anchor"))

    computed = None
    computed_is_assumed = False
    if days_stated and anchor:
        anchor_date, assumed = _resolve_anchor_date(fx, anchor)
        if anchor_date:
            computed = anchor_date + timedelta(days=int(days_stated))
            computed_is_assumed = assumed

    # D2: cross-check explicit date against computed date
    if deadline_date and computed and abs((deadline_date - computed).days) > 3:
        r.flags.append(Finding(
            "D2",
            f"The letter states a deadline of {deadline_date.isoformat()}, but the stated day "
            f"count implies {computed.isoformat()}. Showing both; using the earlier one for "
            f"reminders.",
            "appeal.deadline_date",
        ))
        canonical = min(deadline_date, computed)
    else:
        canonical = deadline_date or computed

    # D3: nothing extractable
    if canonical is None:
        letter_date = _parse_date(_v(document.get("letter_date")))
        base = letter_date or today
        canonical = base + timedelta(days=CONSERVATIVE_FALLBACK_DAYS)
        computed_is_assumed = True
        r.flags.append(Finding(
            "D3",
            "We couldn't find your appeal deadline in this letter. Most plans allow at least "
            "180 days from the letter date, but please check the letter or call the number on "
            "your insurance card. We've set a placeholder reminder for 60 days as a safety net.",
            "appeal.deadline_date",
        ))

    r.computed_deadline = canonical
    r.computed_deadline_is_assumed = computed_is_assumed
    r.reminder_deadline = canonical

    # D4: unusual window
    if days_stated and int(days_stated) not in COMMON_WINDOWS:
        r.flags.append(Finding("D4", f"{days_stated} days is an unusual appeal window. Please confirm this against the letter.", "appeal.deadline_days_stated"))

    # D6: deadline in the past
    if canonical and canonical < today:
        r.flags.append(Finding(
            "D6",
            "This deadline may have already passed. Late appeals are sometimes still accepted, "
            "and external review may still be available. Act today.",
            "appeal.deadline_date",
        ))

    # D7: deadline very close
    if canonical and today <= canonical < today + timedelta(days=14):
        r.flags.append(Finding("D7", "Your appeal deadline is less than two weeks away. Switching to daily reminders.", "appeal.deadline_date"))

    # D8: letter date in the future
    letter_date = _parse_date(_v(document.get("letter_date")))
    if letter_date and letter_date > today:
        r.flags.append(Finding("D8", "The letter date is in the future, which usually means a misread year.", "document.letter_date"))

    # D9: letter predates date of service for a post-service denial
    dos_start = _parse_date(_v(service.get("date_of_service_start")))
    timing = _v(denial.get("timing"))
    if timing == "post_service" and letter_date and dos_start and letter_date < dos_start:
        r.flags.append(Finding("D9", "The letter date is before the date of service, which is inconsistent for a post-service denial.", "document.letter_date"))

    # D10: low confidence on deadline fields
    for f in ("deadline_date", "deadline_days_stated", "deadline_anchor", "deadline_basis_text"):
        obj = appeal.get(f)
        if obj and obj.get("value") is not None and _c(obj) < 0.8:
            quote = (obj.get("evidence") or [{}])[0].get("quote", "")
            r.flags.append(Finding("D10", f"Low confidence on {f}. Please verify: “{quote}”", f"appeal.{f}"))


# --------------------------------------------------------------- reason ----

def _run_category_rules(fx: dict, r: ValidationResult) -> None:
    denial = fx.get("denial", {})
    category_obj = denial.get("reason_category") or {}
    category = category_obj.get("value")
    reason_text = _v(denial.get("reason_text"))

    if category == "other" and not reason_text:
        r.blocks.append(Finding("C1", "The letter doesn't state a clear reason for the denial. Please upload the page that explains why the claim was denied.", "denial.reason_text"))

    if category is not None and _c(category_obj) < 0.7:
        r.flags.append(Finding("C2", f"We're not fully sure this is a “{category}” denial. Please confirm the reason: “{reason_text or ''}”", "denial.reason_category"))

    criteria = denial.get("criteria_cited") or {}
    if not (criteria.get("value") or []):
        # Not an error — this activates the "request the criteria" argument
        # in the matching playbook entry (see insurer_obligations.criteria_disclosed).
        pass

    timing = _v(denial.get("timing"))
    if timing == "concurrent":
        r.flags.append(Finding("C5", "This denial cuts off care that's already in progress. Surfacing the expedited appeal option.", "denial.timing"))


# ----------------------------------------------------------------- codes ----

def _run_code_normalization(fx: dict, r: ValidationResult) -> None:
    service = fx.get("service", {})

    proc = service.get("procedure_codes") or {}
    cleaned = []
    for code in proc.get("value") or []:
        c = code.strip().upper().replace(" ", "")
        if CPT_RE.match(c) or HCPCS_RE.match(c):
            cleaned.append(c)
        else:
            r.flags.append(Finding("K1", f"Procedure code “{code}” doesn't look like a valid CPT/HCPCS code and was dropped from the letter.", "service.procedure_codes"))
    proc["value"] = cleaned

    diag = service.get("diagnosis_codes") or {}
    cleaned = []
    for code in diag.get("value") or []:
        c = code.strip().upper().replace(" ", "")
        if ICD10_RE.match(c):
            cleaned.append(c)
        else:
            r.flags.append(Finding("K2", f"Diagnosis code “{code}” doesn't look like a valid ICD-10 code and was dropped from the letter.", "service.diagnosis_codes"))
    diag["value"] = cleaned

    appeal = fx.get("appeal", {})
    for f in ("submission_fax", "submission_phone"):
        obj = appeal.get(f) or {}
        v = obj.get("value")
        if v:
            digits = DIGITS_RE.sub("", v)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 10:
                obj["value"] = f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
            else:
                r.flags.append(Finding("K4", f"{f} “{v}” doesn't look like a complete 10-digit US phone number.", f"appeal.{f}"))


# ------------------------------------------------------------ submission ----

def _run_submission_rules(fx: dict, r: ValidationResult) -> None:
    appeal = fx.get("appeal", {})
    has_route = any(
        _v(appeal.get(f))
        for f in ("submission_address", "submission_fax", "submission_portal_url")
    )
    if not has_route:
        r.flags.append(Finding(
            "S1",
            "We couldn't find where to send your appeal. Check the last page of the letter, "
            "the back of your insurance card, or your plan's member portal.",
            "appeal.submission_address",
        ))


# -------------------------------------------------------------- evidence ----

def _run_evidence_rules(fx: dict, r: ValidationResult) -> None:
    for path, obj in walk_leaf_fields(fx):
        value = obj.get("value")
        evidence = obj.get("evidence") or []
        if value not in (None, [], "") and not evidence:
            r.flags.append(Finding("E1", "This value has no supporting quote from the document and is unverified.", path))
        if (value in (None, [])) and obj.get("confidence"):
            obj["confidence"] = 0  # E2: fix
