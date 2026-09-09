# Extraction eval

Week 1 lives or dies here. Target: 30–50 real denial letters, redacted, with
hand-labelled ground truth.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# Run every letter/label pair under eval/letters + eval/labels:
python scripts/run_eval.py

# Just one letter, no eval bookkeeping:
python scripts/extract.py eval/fixtures/sample_001.txt
```

Real runs write per-letter results and a `summary.json` to
`eval/results/<run_id>/` (gitignored — regenerate, don't commit). Raw model
output is cached under `eval/results/raw/<run_id>/<id>.json` by default, so
re-running the same `--run-id` after a validation-rule or scoring change
re-scores without spending another API call; pass `--no-raw-cache` to force a
fresh extraction, or `--raw-cache-dir` to point at a specific cache.

### Smoke-testing without an API key

`eval/fixtures/sample_001.{txt,json}` is a synthetic (fictional insurer,
fictional patient) letter and hand-written label, checked into git, used to
exercise the whole pipeline — extraction shape, `VALIDATION.md` rules,
scoring, null-handling, calibration — without calling the model or touching
real PHI:

```bash
python scripts/make_smoketest_fixture.py   # writes a mock extraction with known, documented errors
python scripts/run_eval.py --letters-dir eval/fixtures --labels-dir eval/fixtures \
    --raw-cache-dir eval/results/raw/smoketest --run-id smoketest
```

The mutations `make_smoketest_fixture.py` introduces (a low-confidence but
correct category, a mismatched deadline anchor, a hallucinated procedure
code, a hallucinated plan name, a typo'd phone number) are listed in the
script itself, so the printed report's failures are all expected — use it to
confirm a change to `validate_rules.py` or `score.py` didn't silently break
something, not as a real accuracy number.

## Layout

```
eval/
  fixtures/  <id>.txt + <id>.json      synthetic, checked-in smoke-test case (see above)
  letters/   <id>.pdf | <id>.jpg | <id>.txt   redacted real source documents (never commit unredacted)
  labels/    <id>.json                 ground truth, same shape as denial_extraction.schema.json
  results/   <run_id>/<id>.json        model outputs + scores (gitignored)
  results/raw/<run_id>/<id>.json       cached raw model output (gitignored)
```

Ground-truth files use the extraction schema exactly, so the same JSON schema
validates both. In labels, set every `confidence` to 1.0 and fill `evidence`
with the actual quote — the evidence quote is itself evaluated (E3 in
VALIDATION.md).

## Sourcing letters

- Patient forums and subreddits for chronic conditions (redacted samples are
  posted regularly; ask permission before saving)
- State insurance department / DMHC sites publish example adverse determination
  letters and IRO decisions that quote them
- Insurers publish template denial language in provider manuals
- The first 20 users, with consent, after redaction

Redact before saving: names, member/group/claim numbers, addresses, DOB,
phone numbers. Keep: insurer name, dates, codes, reason text, deadline text,
appeal address (it's the insurer's, not the patient's).

## Metrics (per field)

| Field group | Metric | Target |
|-------------|--------|--------|
| `appeal.deadline_date` / `deadline_days_stated` / `deadline_anchor` | exact match after computing canonical deadline | ≥ 0.98 |
| `document.document_type`, `insurer.coverage_type`, `denial.reason_category`, `denial.timing` | accuracy (exact enum match) | ≥ 0.95 |
| `document.letter_date`, `service.date_of_service_*` | exact match | ≥ 0.95 |
| `insurer.name`, `provider.provider_name` | normalised fuzzy match (≥ 0.9 token-sort ratio) | ≥ 0.95 |
| `service.procedure_codes`, `diagnosis_codes` | set F1 | ≥ 0.90 |
| `denial.reason_text`, `appeal.deadline_basis_text` | fuzzy match against label quote (≥ 0.85) | ≥ 0.90 |
| `appeal.submission_*` | exact after whitespace/punct normalisation | ≥ 0.90 |
| `member.*` | exact | ≥ 0.95 (but never logged in reports — report pass/fail only) |
| null-handling | precision/recall on "field is absent from letter" | both ≥ 0.90 (hallucinated values are worse than misses) |
| calibration | Brier score of `confidence` vs correctness | report; use to tune D10/N3 thresholds |

Report per-field, per-insurer, and per-`ocr_quality` bucket. A field that is
fine on clean PDFs and bad on phone photos is a UX problem (ask for a better
photo), not a prompt problem.

## Runner

`scripts/run_eval.py`: for each letter with a matching label, runs
`scripts/extract.py`, applies the `VALIDATION.md` rules
(`scripts/validate_rules.py`), scores the result against the label
(`scripts/score.py`), writes a per-letter result file, and prints a summary
table plus `summary.json`. `extraction_meta.prompt_version` is stamped on
every extraction so runs stay comparable across prompt changes.

A few scoring notes worth knowing before reading a report:

- The deadline row (`appeal.canonical_deadline`) doesn't compare the raw
  `deadline_date`/`deadline_days_stated`/`deadline_anchor` fields directly —
  it runs the same `VALIDATION.md` deadline computation used in production on
  both the label and the prediction, then compares the two computed dates.
  That's the number that actually matters to a user.
- `member.*` fields are scored pass/fail only; the report never prints their
  label or predicted values (see `SENSITIVE_FIELDS` in `score.py` and P1 in
  `VALIDATION.md`).
- Null-handling precision/recall is computed across every leaf field in the
  schema, not just the ones with an explicit accuracy target — a model that
  hallucinates values for fields the label leaves blank shows up here even if
  no other row catches it.
- The confidence-calibration Brier score only includes fields where the label
  has a non-null value (there's nothing to calibrate against when the correct
  answer is "nothing was there").
