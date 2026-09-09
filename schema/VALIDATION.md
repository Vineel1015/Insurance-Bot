# Code-level validation on top of extraction

The LLM fills `denial_extraction.schema.json`. Code then runs these rules.
Each rule produces one of:

- **block** — do not proceed; ask the user for a better upload or route out of scope
- **flag** — proceed, but show the user a specific warning and ask them to confirm the value
- **fix** — silently normalise (whitespace, date formats, code formats)

Rules are numbered so eval reports and UI copy can reference them.

## Routing gates (run first)

| # | Rule | Outcome |
|---|------|---------|
| R1 | `document.document_type` not in {`denial_letter`, `prior_auth_denial`} | **block** with a type-specific message. `eob_with_denial`: "This looks like an Explanation of Benefits. Your insurer also sent a denial letter — that's the one we need." `appeal_decision`: "This is a response to an appeal you already filed. Level-2 / external review isn't supported yet." |
| R2 | `insurer.coverage_type` in {`medicare`, `medicare_advantage`, `medicaid`, `tricare`, `va`} | **block**: "Medicare and Medicaid appeals follow a different process we don't support yet." Link to the official appeals page for that program. |
| R3 | `extraction_meta.ocr_quality == "poor"` | **block**: ask for a clearer photo or a PDF. |
| R4 | `document.page_count > 1` and any warning mentions a missing page | **flag**: "This looks like page N of M. Appeal instructions are usually on the last page." |

## Deadline (the field that must be right)

Canonical deadline is computed in code, never trusted from the model alone.

| # | Rule | Outcome |
|---|------|---------|
| D1 | Compute `computed_deadline = anchor_date + deadline_days_stated` where anchor_date is resolved from `deadline_anchor` (`letter_date` → `document.letter_date`; `date_received` → `letter_date + 5 days` as a conservative mailing assumption, flagged; `date_of_service` → `service.date_of_service_end or start`). | value |
| D2 | If `deadline_date.value` present AND `computed_deadline` present AND they differ by > 3 days | **flag** both to the user; use the **earlier** date for reminders. |
| D3 | If neither `deadline_date` nor (`deadline_days_stated` + resolvable anchor) is present | **flag**: "We couldn't find your appeal deadline in this letter. Most plans allow 180 days from the letter date, but check the letter or call the number on your card." Set reminder from a conservative 60-day assumption, clearly labelled as assumed. |
| D4 | `deadline_days_stated` not in {30, 45, 60, 90, 120, 180, 365} | **flag**: unusual window, ask user to confirm against the letter. |
| D5 | `insurer.coverage_type == "employer"` and `deadline_days_stated < 180` | **flag**: ERISA plans generally must allow at least 180 days; either the extraction is wrong or the letter is non-compliant. Verify against current regs before hardcoding this. |
| D6 | Canonical deadline is in the past | **flag** prominently: "This deadline may have passed. Late appeals are sometimes accepted; external review may still be available. Don't give up, but act today." Do not block. |
| D7 | Canonical deadline < 14 days from today | **flag**: switch reminder cadence to daily; surface `expedited_*` fields. |
| D8 | `document.letter_date` > today, or > today by any amount | **flag**: likely OCR misread of the year. |
| D9 | `document.letter_date` < `service.date_of_service_start` when `denial.timing == post_service` | **flag**: dates inconsistent. |
| D10 | Any deadline-related field has `confidence < 0.8` | **flag** and show the `deadline_basis_text` quote so the user can verify. |

## Enum and category integrity

| # | Rule | Outcome |
|---|------|---------|
| C1 | `denial.reason_category == "other"` and `reason_text.value` is null | **block**: the letter must state a reason; ask for the page that contains "reason for denial". |
| C2 | `denial.reason_category` confidence < 0.7 | **flag**: show the top category and `reason_text`, ask the user to confirm or pick from the list. Misclassification here sends the user down the wrong playbook. |
| C3 | `reason_text` mentions two or more category aliases (from playbook `aliases`) | **flag**: multi-reason denial. MVP picks the primary and notes the secondary in the letter. |
| C4 | `denial.criteria_cited` is empty | not an error. Sets `playbook.insurer_obligations.criteria_disclosed = false`, which activates the "request the criteria" argument. |
| C5 | `denial.timing == "concurrent"` | **flag**: care is being cut off; surface expedited appeal immediately. |

## Codes and identifiers

| # | Rule | Outcome |
|---|------|---------|
| K1 | Each `procedure_codes[]` matches `^\d{5}$` (CPT) or `^[A-Z]\d{4}$` (HCPCS) after stripping whitespace | **fix**; otherwise **flag** that code and drop it from the letter. |
| K2 | Each `diagnosis_codes[]` matches `^[A-TV-Z]\d[0-9A-Z](\.[0-9A-Z]{1,4})?$` (ICD-10-CM) | **fix**; otherwise **flag**. |
| K3 | `member_id`, `claim_number`, `group_number`: strip whitespace and punctuation; no pattern check (formats vary by insurer). | **fix** |
| K4 | `submission_fax`, `submission_phone`: normalise to `(NNN) NNN-NNNN`; must have 10 digits. | **fix**; otherwise **flag**. |
| K5 | `submission_portal_url`: must parse as a URL with a host; no auto-correction. | **flag** if not. |

## Submission route

| # | Rule | Outcome |
|---|------|---------|
| S1 | None of `submission_address`, `submission_fax`, `submission_portal_url` present | **flag**: "We couldn't find where to send your appeal. It's usually on the last page of the letter, on the back of your insurance card, or in your plan's member portal." Checklist item becomes a user task. |
| S2 | `submission_address` present but has no ZIP-like token | **flag**: likely truncated; show the quote. |

## Evidence and null-handling

| # | Rule | Outcome |
|---|------|---------|
| E1 | Any field with non-null `value` and empty `evidence` | **flag** as unverified; in eval, count as a hallucination unless the label agrees. |
| E2 | Any field with `value == null` and `confidence > 0` | **fix** confidence to 0. |
| E3 | `evidence[].quote` not found in the OCR text (fuzzy, ≥ 0.85 similarity) | **flag** in eval; in prod, treat as E1. |
| E4 | Same quote used as evidence for `letter_date` and `deadline_date` | **flag**: the model may have confused the two dates. |

## Privacy

| # | Rule | Outcome |
|---|------|---------|
| P1 | Never write `member.*` values to logs, error reports, analytics, or eval result tables. Log presence/absence only. | hard rule |
| P2 | Source document and extraction JSON are deleted at session end unless the user opted in. Retention opt-in is per document. | hard rule |
| P3 | Eval fixtures must be redacted before they enter `eval/letters/`. | hard rule |

## Confidence thresholds (initial, tune against eval)

| Field group | Auto-accept ≥ | Ask user to confirm | Treat as missing < |
|-------------|---------------|---------------------|--------------------|
| deadline fields | 0.90 | 0.60–0.90 | 0.60 |
| reason_category | 0.85 | 0.60–0.85 | 0.60 |
| submission route | 0.85 | 0.60–0.85 | 0.60 |
| everything else | 0.75 | 0.50–0.75 | 0.50 |
