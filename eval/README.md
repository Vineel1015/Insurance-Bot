# Extraction eval

Week 1 lives or dies here. Target: 30–50 real denial letters, redacted, with
hand-labelled ground truth.

## Layout

```
eval/
  letters/   <id>.pdf | <id>.jpg      redacted source documents (never commit unredacted)
  labels/    <id>.json                 ground truth, same shape as denial_extraction.schema.json
  results/   <run_id>/<id>.json        model outputs (gitignored)
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

`scripts/run_eval.py` (to be written): for each letter, run extraction, run
`VALIDATION.md` rules, write result, then score against the label. Print a
table and a JSON summary. Keep prompt version in `extraction_meta` so runs are
comparable.
