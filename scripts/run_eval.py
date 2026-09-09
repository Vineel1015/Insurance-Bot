#!/usr/bin/env python
"""Run the extraction eval described in eval/README.md.

For every letter in eval/letters/ with a matching label in eval/labels/
(same filename stem), this: extracts, runs the code-level validation rules
from schema/VALIDATION.md, scores the result against the label, writes a
per-letter result file, and prints a summary table.

Usage:
    python scripts/run_eval.py
    python scripts/run_eval.py --model claude-opus-5 --limit 5
    python scripts/run_eval.py --letters-dir eval/letters --labels-dir eval/labels

Offline / no API key:
    Pre-populate eval/results/raw/<run_id>/<id>.json with cached extraction
    output (see extract.py's --raw-cache), or pass --raw-cache-dir pointing
    at an existing cache directory to reuse it without calling the API.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract import DEFAULT_MODEL, extract_document  # noqa: E402
from schema_utils import validate_instance  # noqa: E402
from score import FIELD_SPECS, SENSITIVE_FIELDS, score_document  # noqa: E402
from validate_rules import run_validation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LETTERS_DIR = os.path.join(ROOT, "eval", "letters")
DEFAULT_LABELS_DIR = os.path.join(ROOT, "eval", "labels")
DEFAULT_RESULTS_DIR = os.path.join(ROOT, "eval", "results")

LETTER_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".txt", ".md")


def _find_cases(letters_dir: str, labels_dir: str) -> list[tuple[str, str, str]]:
    """Returns [(case_id, letter_path, label_path), ...] for every letter that
    has a matching label file.
    """
    cases = []
    skipped = []
    for path in sorted(glob.glob(os.path.join(letters_dir, "*"))):
        if not path.lower().endswith(LETTER_EXTS):
            continue
        case_id = os.path.splitext(os.path.basename(path))[0]
        label_path = os.path.join(labels_dir, f"{case_id}.json")
        if os.path.exists(label_path):
            cases.append((case_id, path, label_path))
        else:
            skipped.append(case_id)
    if skipped:
        print(f"[run_eval] {len(skipped)} letter(s) with no matching label, skipped: {', '.join(skipped)}", file=sys.stderr)
    return cases


def _run_case(case_id: str, letter_path: str, label_path: str, model: str, raw_cache_dir: str | None) -> dict:
    with open(label_path, encoding="utf-8") as f:
        label = json.load(f)

    raw_cache_path = os.path.join(raw_cache_dir, f"{case_id}.json") if raw_cache_dir else None
    extraction, extract_meta = extract_document(letter_path, model=model, raw_cache_path=raw_cache_path)

    schema_errors = validate_instance(extraction)
    pred_validation = run_validation(extraction)
    label_validation = run_validation(label)  # gives us the label's canonical deadline, computed the same way

    scored = score_document(
        label, extraction,
        label_computed_deadline=label_validation.computed_deadline,
        pred_computed_deadline=pred_validation.computed_deadline,
    )

    return {
        "case_id": case_id,
        "letter_path": letter_path,
        "extract_meta": extract_meta,
        "schema_errors": schema_errors,
        "validation_blocks": [b.__dict__ for b in pred_validation.blocks],
        "validation_flags": [fl.__dict__ for fl in pred_validation.flags],
        "field_scores": [
            {
                "path": fs.path, "kind": fs.kind, "correct": fs.correct,
                "similarity": fs.similarity, "label_is_null": fs.label_is_null,
                "pred_is_null": fs.pred_is_null, "pred_confidence": fs.pred_confidence,
                **({} if fs.path in SENSITIVE_FIELDS else {"label_value": fs.label_value, "pred_value": fs.pred_value}),
            }
            for fs in scored["field_scores"]
        ],
        "null_handling": scored["null_handling"],
        "brier": scored["brier"],
        "insurer_bucket": (label.get("insurer", {}).get("name", {}) or {}).get("value") or "unknown",
        "ocr_quality_bucket": extraction.get("extraction_meta", {}).get("ocr_quality") or "unknown",
    }


def _aggregate(results: list[dict]) -> dict:
    by_field: dict[str, list] = {}
    for r in results:
        for fs in r["field_scores"]:
            by_field.setdefault(fs["path"], []).append(fs)

    field_summary = {}
    for path, scores in by_field.items():
        scored = [s for s in scores if s["correct"] is not None]
        n_null_label = sum(1 for s in scores if s["label_is_null"])
        acc = sum(1 for s in scored if s["correct"]) / len(scored) if scored else None
        field_summary[path] = {"n": len(scores), "n_scored": len(scored), "n_label_null": n_null_label, "accuracy": acc}

    null_agg = {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    briers = []
    for r in results:
        nh = r["null_handling"]
        null_agg["tp"] += nh["true_null_true_null"]
        null_agg["fn"] += nh["hallucinated_fn"]
        null_agg["fp"] += nh["missed_fp"]
        null_agg["tn"] += nh["both_present_tn"]
        if r["brier"] is not None:
            briers.append(r["brier"])

    precision = null_agg["tp"] / (null_agg["tp"] + null_agg["fp"]) if (null_agg["tp"] + null_agg["fp"]) else None
    recall = null_agg["tp"] / (null_agg["tp"] + null_agg["fn"]) if (null_agg["tp"] + null_agg["fn"]) else None

    by_bucket: dict[str, dict[str, list]] = {"insurer": {}, "ocr_quality": {}}
    for r in results:
        by_bucket["insurer"].setdefault(r["insurer_bucket"], []).append(r)
        by_bucket["ocr_quality"].setdefault(r["ocr_quality_bucket"], []).append(r)

    return {
        "n_documents": len(results),
        "field_summary": field_summary,
        "null_handling": {**null_agg, "precision": precision, "recall": recall},
        "brier_mean": sum(briers) / len(briers) if briers else None,
        "bucket_counts": {
            "insurer": {k: len(v) for k, v in by_bucket["insurer"].items()},
            "ocr_quality": {k: len(v) for k, v in by_bucket["ocr_quality"].items()},
        },
    }


def _print_report(agg: dict, results: list[dict]) -> None:
    targets = {spec[0]: spec[2] for spec in FIELD_SPECS}
    print(f"\n=== {agg['n_documents']} document(s) ===\n")
    print(f"{'field':45s} {'n':>4s} {'scored':>7s} {'accuracy':>9s} {'target':>7s}")
    for path, s in sorted(agg["field_summary"].items()):
        acc = f"{s['accuracy']:.2f}" if s["accuracy"] is not None else "  n/a"
        target = targets.get(path)
        target_s = f"{target:.2f}" if target else "  -"
        flag = " *" if (s["accuracy"] is not None and target and s["accuracy"] < target) else ""
        print(f"{path:45s} {s['n']:>4d} {s['n_scored']:>7d} {acc:>9s} {target_s:>7s}{flag}")

    nh = agg["null_handling"]
    print("\nnull-handling (is-the-field-absent detection):")
    print(f"  precision={nh['precision']:.2f} recall={nh['recall']:.2f}" if nh["precision"] is not None else "  n/a (no scored fields)")
    print(f"  true-null/true-null={nh['tp']}  hallucinated(FN)={nh['fn']}  missed(FP)={nh['fp']}  both-present={nh['tn']}")

    if agg["brier_mean"] is not None:
        print(f"\nconfidence calibration (Brier score, lower is better): {agg['brier_mean']:.3f}")

    print("\nby insurer:", agg["bucket_counts"]["insurer"])
    print("by ocr_quality:", agg["bucket_counts"]["ocr_quality"])

    blocked = [r["case_id"] for r in results if r["validation_blocks"]]
    if blocked:
        print(f"\n{len(blocked)} document(s) hit a validation block: {', '.join(blocked)}")
    schema_bad = [r["case_id"] for r in results if r["schema_errors"]]
    if schema_bad:
        print(f"{len(schema_bad)} document(s) failed schema validation: {', '.join(schema_bad)}")

    print("\n(* below target)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--letters-dir", default=DEFAULT_LETTERS_DIR)
    ap.add_argument("--labels-dir", default=DEFAULT_LABELS_DIR)
    ap.add_argument("--out-dir", default=None, help="default: eval/results/<run_id>")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--run-id", default=None, help="default: UTC timestamp")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--raw-cache-dir", default=None, help="reuse/cache raw model output here (default: eval/results/raw/<run_id>)")
    ap.add_argument("--no-raw-cache", action="store_true", help="always call the API fresh, don't cache")
    args = ap.parse_args()

    run_id = args.run_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or os.path.join(DEFAULT_RESULTS_DIR, run_id)
    raw_cache_dir = None if args.no_raw_cache else (args.raw_cache_dir or os.path.join(DEFAULT_RESULTS_DIR, "raw", run_id))
    os.makedirs(out_dir, exist_ok=True)

    cases = _find_cases(args.letters_dir, args.labels_dir)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"No letter/label pairs found under {args.letters_dir} / {args.labels_dir}.", file=sys.stderr)
        print("See eval/README.md for how to source and label sample letters.", file=sys.stderr)
        return 1

    results = []
    for case_id, letter_path, label_path in cases:
        print(f"[run_eval] {case_id} ...", file=sys.stderr)
        try:
            result = _run_case(case_id, letter_path, label_path, args.model, raw_cache_dir)
        except Exception as e:  # noqa: BLE001 - report and keep going
            print(f"[run_eval]   FAILED: {e}", file=sys.stderr)
            result = {"case_id": case_id, "error": str(e)}
        results.append(result)
        with open(os.path.join(out_dir, f"{case_id}.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)

    ok_results = [r for r in results if "error" not in r]
    agg = _aggregate(ok_results)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(agg, f, indent=2)

    _print_report(agg, ok_results)
    failed = [r["case_id"] for r in results if "error" in r]
    if failed:
        print(f"\n{len(failed)} document(s) errored out and were excluded from scoring: {', '.join(failed)}", file=sys.stderr)

    print(f"\nWrote per-document results and summary.json to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
