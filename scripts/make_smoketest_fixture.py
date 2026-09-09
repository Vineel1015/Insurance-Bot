#!/usr/bin/env python
"""Generate a mock raw extraction for eval/fixtures/sample_001, with a handful
of deliberate, documented errors, so the eval pipeline can be smoke-tested
end to end without an ANTHROPIC_API_KEY or a live model call.

Usage:
    python scripts/make_smoketest_fixture.py
    python scripts/run_eval.py --letters-dir eval/fixtures --labels-dir eval/fixtures \
        --raw-cache-dir eval/results/raw/smoketest --run-id smoketest

The output directory is under eval/results/, which is gitignored — this is a
generated artifact, not something to commit. Regenerate it any time with this
script; the mutations are deterministic.
"""
from __future__ import annotations

import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL_PATH = os.path.join(ROOT, "eval", "fixtures", "sample_001.json")
OUT_PATH = os.path.join(ROOT, "eval", "results", "raw", "smoketest", "sample_001.json")

# Each entry: (what it tests, the mutation). Kept in sync with the expected
# smoke-test output documented in eval/README.md.
MUTATIONS = """
- denial.reason_category confidence lowered to 0.65 (still correct)  -> exercises VALIDATION.md C2
- appeal.deadline_anchor changed to date_received (label: letter_date) -> exercises D2 (canonical deadline mismatch)
- service.procedure_codes gets a hallucinated extra code             -> exercises set_f1 miss
- appeal.submission_fax reformatted with dashes, no parens           -> should still normalize-match
- appeal.submission_phone has a typo'd last digit                    -> should NOT match
- insurer.plan_name and insurer.coverage_type hallucinated           -> label has both null; exercises null-handling FN
"""


def build_mock() -> dict:
    with open(LABEL_PATH, encoding="utf-8") as f:
        label = json.load(f)
    pred = copy.deepcopy(label)

    pred["denial"]["reason_category"]["confidence"] = 0.65

    pred["appeal"]["deadline_anchor"]["value"] = "date_received"
    pred["appeal"]["deadline_anchor"]["evidence"] = [
        {"quote": "within 180 days of the date of this letter", "page": 1}
    ]

    pred["service"]["procedure_codes"]["value"] = ["72148", "99213"]
    pred["service"]["procedure_codes"]["evidence"].append({"quote": "99213", "page": 1})

    pred["appeal"]["submission_fax"]["value"] = "555-201-9982"
    pred["appeal"]["submission_phone"]["value"] = "(555) 201-4401"

    pred["insurer"]["plan_name"] = {
        "value": "Example Health Plan Premier PPO", "confidence": 0.55,
        "evidence": [{"quote": "Example Health Plan", "page": 1}],
    }
    pred["insurer"]["coverage_type"] = {
        "value": "employer", "confidence": 0.6,
        "evidence": [{"quote": "Example Health Plan", "page": 1}],
    }

    pred["extraction_meta"]["model"] = "smoketest-mock"
    pred["extraction_meta"]["warnings"] = []
    return pred


def main() -> int:
    pred = build_mock()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"extraction": pred, "model": "smoketest-mock"}, f, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(MUTATIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
