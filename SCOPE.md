# Insurance-bot — MVP scope

One loop, one document type, one kind of person.

**The person:** someone who just received a health insurance denial letter for a
commercial plan (employer, marketplace, or individual), is angry and confused,
and is facing an appeal deadline.

**The document:** the denial letter (a.k.a. "adverse benefit determination" or
"notice of adverse determination"). Nothing else.

## The v0 loop

1. User uploads a photo or PDF of the denial letter.
2. Agent extracts structured fields (see `schema/denial_extraction.schema.json`).
3. Agent explains the denial in plain English and asks 3–5 clarifying questions
   drawn from the matching playbook entry (see `playbook/`).
4. Agent generates a ready-to-send appeal letter plus a checklist:
   what to attach, where to send it, deadline on a calendar.
5. One reminder email/text before the deadline: "Your appeal window closes in
   10 days — did you send it?" (see `reminders/`)

## Explicitly out of scope for MVP

Every one of these is a real feature for later. Every one would double the
timeline now. Refuse yourself.

- Calling insurers by phone
- Submitting appeals on the user's behalf (user sends everything themselves)
- Medicare / Medicare Advantage / Medicaid appeals (different process; detect
  and route to a "not yet supported" message)
- EOBs, prior-auth submissions, claim forms, or any document that is not a
  denial letter
- Multi-document case files
- Tracking claims over time
- Mobile app (web only)
- User accounts before they are needed
- Dental / vision / pharmacy-benefit-manager-only plans (v1 candidate)

## Guardrails from day one

**Legal framing.** The product explains documents and helps users draft their
own letters. It is not legal advice and not medical advice. The user sends
everything themselves. Say this in the UI, not just in fine print.

**Privacy.** Denial letters are health data. Behave as if HIPAA applies even if
we are not a covered entity (verify this; do not rely on an assumption):

- Do not retain uploaded documents by default. Delete after processing unless
  the user opts in.
- No third-party analytics on any page that shows document content.
- Never log member IDs, claim numbers, or names.
- Say all of this plainly in the UI.

## Build order

| Week | Deliverable | Done when |
|------|-------------|-----------|
| 1 | Extraction only | Per-field accuracy on 30–50 real letters; deadline extraction near-perfect |
| 2 | Appeal letter generator | Reason-specific playbook entries produce letters an advocate would send |
| 3 | Wrapper | Upload page, clarifying-question flow, deadline reminder email |

## Metrics

- Extraction accuracy per field (deadline is the one that must be ~100%)
- % of users who actually send the appeal (activation — this is *the* number)
- % who report an outcome back
- Appeal success rate once there is volume

## Before writing any code

Collect 30–50 sample denial letters and manually do the full loop for 3 people
by hand. If you can't get 3 people to hand you a denial letter, dig into
distribution before building.
