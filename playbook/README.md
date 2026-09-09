# Denial-reason playbook

One YAML file per `denial.reason_category` value in the extraction schema. The
appeal-letter generator loads the entry matching the extracted category and
uses it to (a) explain the denial, (b) pick clarifying questions, (c) choose and
order arguments, (d) build the attachment checklist.

This is not one generic prompt. Each reason has different counter-arguments,
different evidence, and different insurer obligations. The file structure is in
`_template.yaml`; `not_medically_necessary.yaml` is the fully worked example.

## Categories

| id | Fully worked? | How insurers phrase it | Core counter |
|----|---------------|------------------------|--------------|
| `not_medically_necessary` | yes | "does not meet medical necessity criteria", "not clinically indicated", "does not meet clinical guidelines" | Physician letter of medical necessity + demand the specific criteria used + show the criteria are met or don't fit the patient |
| `experimental_investigational` | template only | "experimental", "investigational", "unproven", "not standard of care" | Peer-reviewed evidence, FDA status, specialty-society guidelines, other insurers' coverage policies |
| `out_of_network` | template only | "non-participating provider", "out of network", "not contracted" | Network adequacy (no in-network specialist within reasonable distance), continuity of care, emergency exception, No Surprises Act if applicable, provider was listed in-network in directory |
| `no_prior_authorization` | template only | "authorization not obtained", "no precertification on file", "notification required" | Retro-authorization request; emergency exception; provider's responsibility under contract (in-network); auth was obtained (provide number); auth not required per plan documents |
| `not_a_covered_benefit` | template only | "excluded under your plan", "not a covered service", "benefit exclusion" | Read the actual plan document (SPD / EOC); exclusion misapplied; service falls under a different covered category; state mandate requires coverage |
| `step_therapy_or_formulary` | template only | "must first try", "non-formulary", "not on preferred drug list", "quantity limit" | Step-therapy exception: already tried/failed, contraindicated, stable on current drug; many states have step-therapy override laws |
| `coding_or_billing_error` | template only | "invalid code", "bundled", "duplicate", "diagnosis does not support procedure" | Usually a provider-side fix; ask provider to rebill; not a true appeal — route the user to the billing office first |
| `missing_information` | template only | "additional information required", "records not received" | Not a true denial; supply the records; confirm receipt; the deadline clock matters here |
| `timely_filing` | template only | "claim not submitted within filing limit" | Provider's responsibility for in-network; patient not liable; proof of timely submission |
| `eligibility_or_coordination_of_benefits` | template only | "not eligible on date of service", "other insurance primary", "COB information needed" | Proof of coverage; update COB with insurer; often clerical |
| `other` | generic fallback | — | Generic: request criteria, request file, physician support, restate facts |

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
5. Add 3+ labelled letters in `eval/` for the category.
