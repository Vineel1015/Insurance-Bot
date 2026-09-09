"""Per-field and aggregate scoring of an extraction against a hand-labelled
ground truth, per the metrics table in eval/README.md.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema_utils import get_field, walk_leaf_fields  # noqa: E402

# (dotted path, kind, target) — target is the per-kind threshold or None.
FIELD_SPECS: list[tuple[str, str, float | None]] = [
    ("document.document_type", "exact", 0.95),
    ("document.letter_date", "exact", 0.95),
    ("insurer.coverage_type", "exact", 0.95),
    ("insurer.name", "fuzzy", 0.90),
    ("insurer.is_self_funded", "exact", None),
    ("provider.provider_name", "fuzzy", 0.90),
    ("service.description", "fuzzy", 0.80),
    ("service.procedure_codes", "set_f1", 0.90),
    ("service.diagnosis_codes", "set_f1", 0.90),
    ("service.date_of_service_start", "exact", 0.95),
    ("service.date_of_service_end", "exact", 0.95),
    ("denial.timing", "exact", 0.95),
    ("denial.reason_category", "exact", 0.95),
    ("denial.reason_text", "fuzzy", 0.90),
    ("appeal.deadline_basis_text", "fuzzy", 0.90),
    ("appeal.submission_address", "normalized_exact", 0.90),
    ("appeal.submission_fax", "phone_exact", 0.90),
    ("appeal.submission_phone", "phone_exact", 0.90),
    ("appeal.submission_portal_url", "normalized_exact", 0.90),
    # Sensitive: scored pass/fail only, values never included in reports.
    ("member.member_id", "normalized_exact", 0.95),
]

SENSITIVE_FIELDS = {"member.member_id", "member.claim_number", "member.group_number", "member.member_name", "member.patient_name"}
FUZZY_MATCH_THRESHOLD = {"fuzzy": 0.85, "normalized_exact": 1.0}


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _norm_punct(s: str) -> str:
    return re.sub(r"[^\w\s]", "", _norm_text(s))


def _digits_only(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _fuzzy_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm_text(a), _norm_text(b)).ratio()


@dataclass
class FieldScore:
    path: str
    kind: str
    label_value: object
    pred_value: object
    pred_confidence: float
    label_is_null: bool
    pred_is_null: bool
    correct: bool | None  # None when label is null (nothing to score) and pred is also null
    similarity: float | None = None


def score_field(path: str, kind: str, label: dict, pred: dict) -> FieldScore:
    lf = get_field(label, path)
    pf = get_field(pred, path)
    lv = lf.get("value") if lf else None
    pv = pf.get("value") if pf else None
    pc = (pf.get("confidence") or 0) if pf else 0

    label_is_null = lv in (None, [], "")
    pred_is_null = pv in (None, [], "")

    if label_is_null:
        # Nothing to check the extracted value against; correctness here is
        # really a null-handling question, scored separately in aggregate.
        return FieldScore(path, kind, lv, pv, pc, True, pred_is_null, None)

    if pred_is_null:
        return FieldScore(path, kind, lv, pv, pc, False, True, False)

    if kind == "exact":
        correct = lv == pv
        sim = 1.0 if correct else 0.0
    elif kind == "fuzzy":
        sim = _fuzzy_ratio(str(lv), str(pv))
        correct = sim >= FUZZY_MATCH_THRESHOLD["fuzzy"]
    elif kind == "normalized_exact":
        correct = _norm_punct(str(lv)) == _norm_punct(str(pv))
        sim = 1.0 if correct else 0.0
    elif kind == "phone_exact":
        correct = _digits_only(str(lv)) == _digits_only(str(pv))
        sim = 1.0 if correct else 0.0
    elif kind == "set_f1":
        lset, pset = set(lv or []), set(pv or [])
        if not lset and not pset:
            correct, sim = True, 1.0
        else:
            tp = len(lset & pset)
            precision = tp / len(pset) if pset else 0.0
            recall = tp / len(lset) if lset else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            correct, sim = f1 >= 0.90, f1
    else:
        raise ValueError(f"unknown field kind {kind}")

    return FieldScore(path, kind, lv, pv, pc, False, False, correct, sim)


def _deadline_confidence(pred: dict) -> float:
    """The model's own confidence in whichever fields actually drove the
    computed deadline: the explicit date if it gave one, otherwise the
    average of deadline_days_stated and deadline_anchor confidence. Used for
    Brier scoring, so it must reflect what the model actually said, not a
    fixed number.
    """
    appeal = pred.get("appeal", {})
    date_field = appeal.get("deadline_date") or {}
    if date_field.get("value") is not None:
        return date_field.get("confidence") or 0
    days = (appeal.get("deadline_days_stated") or {}).get("confidence")
    anchor = (appeal.get("deadline_anchor") or {}).get("confidence")
    parts = [c for c in (days, anchor) if c is not None]
    return sum(parts) / len(parts) if parts else 0.0


def score_deadline(label: dict, pred: dict, label_computed_deadline, pred_computed_deadline) -> FieldScore:
    """Special-cased: score the *canonical* deadline (after validate_rules
    computes it the same way for both label and prediction), not the raw
    fields individually.
    """
    lv = label_computed_deadline.isoformat() if label_computed_deadline else None
    pv = pred_computed_deadline.isoformat() if pred_computed_deadline else None
    correct = lv == pv if lv else None
    confidence = _deadline_confidence(pred)
    return FieldScore("appeal.canonical_deadline", "exact", lv, pv, confidence, lv is None, pv is None, correct, 1.0 if correct else 0.0)


def score_null_handling(label: dict, pred: dict) -> dict:
    """Global precision/recall for 'this field is absent from the letter',
    across every leaf field in the schema. Hallucinating a value where the
    label says null is a false negative for null-detection (worse than a
    field the model simply left null when it shouldn't have, which is a
    false positive) — see eval/README.md.
    """
    tp = fp = fn = tn = 0
    for path, lfield in walk_leaf_fields(label):
        pfield = get_field(pred, path)
        label_null = lfield.get("value") in (None, [], "")
        pred_null = (pfield.get("value") in (None, [], "")) if pfield else True
        if label_null and pred_null:
            tp += 1
        elif label_null and not pred_null:
            fn += 1  # hallucination
        elif not label_null and pred_null:
            fp += 1  # missed field
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "true_null_true_null": tp, "hallucinated_fn": fn, "missed_fp": fp, "both_present_tn": tn,
        "precision": precision, "recall": recall,
    }


def brier_score(field_scores: list[FieldScore]) -> float | None:
    """Mean squared error between stated confidence and actual correctness,
    over fields where label was non-null (so correctness is defined).
    """
    scored = [f for f in field_scores if f.correct is not None]
    if not scored:
        return None
    return sum((f.pred_confidence - (1.0 if f.correct else 0.0)) ** 2 for f in scored) / len(scored)


def score_document(label: dict, pred: dict, label_computed_deadline=None, pred_computed_deadline=None) -> dict:
    field_scores = [score_field(path, kind, label, pred) for path, kind, *_ in FIELD_SPECS]
    field_scores.append(score_deadline(label, pred, label_computed_deadline, pred_computed_deadline))
    return {
        "field_scores": field_scores,
        "null_handling": score_null_handling(label, pred),
        "brier": brier_score(field_scores),
    }
