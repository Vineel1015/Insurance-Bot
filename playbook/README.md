# Denial-reason playbook

One YAML file per `denial.reason_category` value in the extraction schema.
`scripts/letter_generator.py` loads the entry matching the extracted
category and uses it to select arguments, assemble the letter, build the
attachment checklist, and report readiness — see that file's docstring and
`eval/fixtures/sample_001.*` for a worked example, or run:

```bash
python scripts/letter_generator.py eval/fixtures/sample_001.json eval/fixtures/sample_001.answers.json
```

This is not one generic prompt. Each reason has different counter-arguments,
different evidence, and different insurer obligations. The file structure is in
`_template.yaml`. All eleven categories are drafted; none has yet been tested
against real letters, so expect the `aliases` and `clarifying_questions` to
change once the eval set exists.

## Categories

| id | Kind of denial | How insurers phrase it | Core counter |
|----|----------------|------------------------|--------------|
| `not_medically_necessary` | clinical | "does not meet medical necessity criteria", "not clinically indicated", "lower level of care" | Physician letter of medical necessity; demand the specific criteria; show criteria are met; conservative treatment already failed |
| `experimental_investigational` | clinical | "experimental", "investigational", "unproven", "not standard of care" | FDA status; specialty-society guidelines and literature; specialist letter; off-label is routine; demand the policy and its review date |
| `out_of_network` | contractual | "non-participating provider", "out of network", "not contracted" | Emergency; No Surprises Act at in-network facility; network inadequacy; directory error; continuity of care; retroactive gap exception |
| `no_prior_authorization` | administrative | "authorization not obtained", "no precertification on file" | Emergency exemption; auth exists but mismatched; in-network provider's responsibility; retroactive authorization; relied on misinformation |
| `not_a_covered_benefit` | contractual | "excluded under your plan", "not a covered service", "cosmetic", "limit exceeded" | Read the plan document; exclusion misapplied; reconstructive not cosmetic; federal/state mandated benefit; mental health parity; limit not permitted |
| `step_therapy_or_formulary` | clinical | "must first try", "non-formulary", "quantity limit" | Already tried and failed; contraindicated; stable on current drug; state step-therapy law; quantity justified; treat as formal exception request |
| `coding_or_billing_error` | administrative | "invalid code", "bundled", "duplicate", "diagnosis does not support" | Provider corrects and resubmits; not a duplicate; member not liable for in-network coding errors; request remark codes |
| `missing_information` | administrative | "additional information required", "records not received" | Supply exactly what was asked, with a cover letter; prove it was already sent; respond inside the window |
| `timely_filing` | administrative | "claim not submitted within filing limit" | In-network provider is liable, not member; proof of timely submission; filed with another insurer first; good cause |
| `eligibility_or_coordination_of_benefits` | administrative | "not eligible on date of service", "other insurance primary", "COB questionnaire" | Proof of coverage; no other coverage; COB order rules; old plan terminated; retroactive enrolment; grace period; identifier correction |
| `other` | unknown | vague or unusual wording | Route to a category via extra questions; otherwise demand the specific reason, request criteria and file, physician support, restate facts |

## Control ids in `unlocks`

Two ids can appear in a question's `unlocks` list without being arguments:

- `expedited_request` — tells the checklist generator to surface the expedited
  appeal route and switch reminder cadence.
- `lmn_needed` — flags that a letter-of-medical-necessity request task should
  be surfaced. Currently informational only: `letter_generator.py` doesn't
  read `unlocks` directly for this (see below), so treat this tag as a
  documented intent for a future "action items" list, not live behavior yet.

In `other.yaml`, the `route_to_category` argument is also a control step: it
re-runs classification with the user's answers and switches playbooks. Its
`template` is an empty string, and `letter_generator.py` explicitly skips
any argument with an empty template rather than emitting a blank paragraph.

## How `letter_generator.py` actually decides things

A few choices are made in code rather than by reading `unlocks` tags, worth
knowing before editing a playbook file:

- **Expedited handling** in the letter is triggered by `schema/VALIDATION.md`
  rules D7 (deadline under two weeks away) and C5 (care is `concurrent`),
  computed from the extraction — not by the `expedited_request` tag on a
  question. This is more robust than tracking which question triggered it,
  since it only needs the extraction to be right, not a specific question to
  have been asked and answered a specific way.
- **Disclosure-request arguments are gated by the extraction, not just by
  questions.** An argument that asks the insurer to disclose something they
  already disclosed is worse than useless — it makes the appeal look like it
  wasn't read. So `request_criteria` / `request_policy` are only included
  when `denial.criteria_cited` is empty, and `request_reviewer_credentials` /
  `request_specialist_review` / `request_reviewer` are only included when
  `denial.reviewer_credentials` is empty. These five ids are hardcoded in
  `letter_generator.py` (`CRITERIA_GATED_ARGUMENTS` /
  `REVIEWER_GATED_ARGUMENTS`) — reuse them exactly if you add another
  argument with the same purpose, or add its id to those sets.
- **`{provider.provider_name.value}` is the provider who performed or billed
  the denied service** per the extraction schema — which can be a facility,
  not a person. Several early drafts of these templates used it to mean "my
  treating physician" (who can write a supportive letter), which is a
  different thing and was silently wrong when the two didn't match (e.g. an
  imaging center standing in as "my treating physician, Example Imaging
  Center, has determined..."). Where a template specifically means the
  prescribing/treating/referring physician, use `{treating_physician_name}`
  instead — the extraction schema doesn't capture that separately, so this is
  always answered by a clarifying question, never guessed from `provider`.

## Closing placeholder gaps

Every bare `{token}` in an argument's `template` must be answerable by some
question's `id` in the same file, or be one of the two values the generator
derives in code (`deadline`, `provider_last_name` — see `build_context()` in
`letter_generator.py`). `scripts/check_playbook.py` enforces this: it fails
if any template references a token with no matching question, so a template
can no longer silently ship with an unclosed `[[ FILL IN ]]` gap the author
didn't notice.

Two patterns keep the up-front questionnaire short while still closing every
gap:

- **Reuse an existing question's answer.** `tried_alternatives` and
  `harm_of_delay` both gate whether an argument is included *and* supply the
  text that goes in it — no separate question needed. Prefer this whenever a
  question you'd ask anyway already contains the value.
- **Add a follow-up with `depends_on`.** Most gaps only matter for one narrow
  scenario (the other insurer's name only matters if the user said they had
  other coverage). Give the fill-in question its own `id`, tie it to its
  trigger with `depends_on`, and it won't count against the 3–6 top-level cap
  — see `_template.yaml` for the exact grammar. It's fine, and expected, for
  a genuinely branchy category like `eligibility_or_coordination_of_benefits`
  to have five or six top-level questions and another dozen follow-ups that
  most users never see.

A few gaps are legitimately optional — the user may not know the answer
(e.g. `mismatch_reason_if_known` when a denial has no obvious explanation, or
`plan_page_ref` when the user hasn't located the exact page). For those,
still add the question so an answer *can* close the gap, but say plainly in
`why_it_matters` or the question text that leaving it blank is fine; the
generator will render a visible `[[ FILL IN ]]` rather than block on it.

## Rights that apply across categories (commercial plans)

Verify against current regulations before relying on any of these in a
template; they are starting points, not legal conclusions.

- **Right to the claim file and the criteria.** ERISA plans (most employer
  plans) and ACA-compliant plans must, on request and free of charge, provide
  the documents, records, and the specific internal rule or guideline relied
  on. Every appeal letter should request this.
- **Internal appeal window.** ERISA: at least 180 days from receipt of the
  denial. Many fully insured plans mirror this. Shorter windows are a flag.
- **Expedited / urgent appeal.** Decision within 72 hours when delay could
  seriously jeopardize life, health, or ability to regain maximum function.
- **External review.** After internal appeals are exhausted (or deemed
  exhausted), an independent review organization or state regulator. Typically
  4 months from the final internal denial to request it.
- **Reviewer qualifications.** Medical-necessity denials should be reviewed by
  a clinician with appropriate expertise; appeals should not be decided by the
  same person who made the original denial.

## Adding a category

1. Copy `_template.yaml` to `<category_id>.yaml`.
2. Fill `aliases` from real letters — this is what makes classification work.
3. Write `clarifying_questions` before `arguments`; each argument must name
   which question or evidence item it depends on.
4. Every `argument.template` paragraph must be sendable as-is with the
   placeholders filled. No "[insert compelling reason here]".
5. Run `python scripts/check_playbook.py` — it fails if any template
   placeholder has no matching question (see "Closing placeholder gaps"
   above) or if the question counts, `unlocks`, `requires`, or `depends_on`
   references don't line up.
6. Add 3+ labelled letters in `eval/` for the category.
