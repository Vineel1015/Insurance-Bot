"""Structural consistency check for playbook/*.yaml against the template and the
extraction schema. Run: python scripts/check_playbook.py  (exit 1 on problems)."""
import glob
import json
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_IDS = {"expedited_request", "lmn_needed"}
REQUIRED_TOP = [
    "id", "display_name", "version", "aliases", "plain_english", "insurer_obligations",
    "clarifying_questions", "arguments", "evidence_checklist", "standing_requests",
    "escalation", "pitfalls", "weak_case_signals",
]
# Bare placeholder names the letter generator resolves in code, not from a
# question — see build_context() in scripts/letter_generator.py. Keep this
# list short and be wary of adding to it: a derived value can be wrong
# (resolves to *something*, just not the right thing) in a way this checker
# can't catch, unlike a genuinely missing question — see the removed
# provider_last_name for exactly that failure mode (it took the last word of
# provider.provider_name, which is who performed/billed the service and can
# be a facility, not a person — it silently produced "Dr. Center" from
# "Riverside Imaging Center"). Prefer a real question over a new derived
# token unless the value is unambiguously computable from the extraction.
DERIVED_TOKENS = {"deadline"}
TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def main() -> int:
    schema = json.load(open(os.path.join(ROOT, "schema", "denial_extraction.schema.json")))
    enum = {x for x in schema["properties"]["denial"]["properties"]["reason_category"]["properties"]["value"]["enum"] if x}
    problems, found = [], set()
    for f in sorted(glob.glob(os.path.join(ROOT, "playbook", "*.yaml"))):
        name = os.path.basename(f)
        if name == "_template.yaml":
            continue
        d = yaml.safe_load(open(f, encoding="utf-8"))
        found.add(d["id"])
        if d["id"] != name[:-5]:
            problems.append(f"{name}: id {d['id']} != filename")
        if d["id"] not in enum:
            problems.append(f"{name}: id not in schema enum")
        for k in REQUIRED_TOP:
            if k not in d:
                problems.append(f"{name}: missing top-level {k}")

        questions = d["clarifying_questions"]
        qids = {q["id"] for q in questions}
        aids = {a["id"] for a in d["arguments"]}
        eids = {e["id"] for e in d["evidence_checklist"]}

        top_level = [q for q in questions if not q.get("depends_on")]
        followups = [q for q in questions if q.get("depends_on")]
        n_top = len(top_level)
        if not 3 <= n_top <= 6:
            problems.append(f"{name}: {n_top} top-level clarifying questions (want 3-6; follow-ups with depends_on don't count)")
        if len(questions) > 18:
            problems.append(f"{name}: {len(questions)} clarifying questions total looks runaway (top-level + follow-ups)")

        for q in questions:
            for u in q.get("unlocks", []):
                if u not in aids and u not in CONTROL_IDS:
                    problems.append(f"{name}: question {q['id']} unlocks unknown argument {u}")
            if q["answer_type"] == "choice" and not q.get("choices"):
                problems.append(f"{name}: choice question {q['id']} has no choices")
            for dep_qid in (q.get("depends_on") or {}):
                if dep_qid not in qids:
                    problems.append(f"{name}: question {q['id']} depends_on unknown question {dep_qid}")
                if dep_qid == q["id"]:
                    problems.append(f"{name}: question {q['id']} depends_on itself")

        for a in d["arguments"]:
            if a["strength"] not in ("strong", "medium", "weak"):
                problems.append(f"{name}: arg {a['id']} bad strength")
            for qid in (a["requires"].get("questions") or {}):
                if qid not in qids:
                    problems.append(f"{name}: arg {a['id']} requires unknown question {qid}")
            for e in (a["requires"].get("evidence") or []):
                if e not in eids:
                    problems.append(f"{name}: arg {a['id']} requires unknown evidence {e}")
            if a["id"] != "route_to_category" and "insert" in a["template"].lower():
                problems.append(f"{name}: arg {a['id']} has an 'insert' placeholder")

            # Every bare {token} in a template must resolve: either it's a
            # question id (any answer supplies it) or a value the generator
            # derives in code. A gap here means a real letter would carry a
            # silent [[ FILL IN ]] the author didn't know about.
            bare_tokens = {t for t in TOKEN_RE.findall(a.get("template", "")) if "." not in t}
            unresolved = bare_tokens - qids - DERIVED_TOKENS
            if unresolved:
                problems.append(f"{name}: arg {a['id']} has placeholder(s) with no matching question: {sorted(unresolved)}")

        for o in d["insurer_obligations"]:
            m = o.get("if_missing_argument")
            if m and m not in aids:
                problems.append(f"{name}: obligation {o['id']} -> unknown argument {m}")

        print(f"{name:45s} q={n_top}+{len(followups)} args={len(aids)} evidence={len(eids)} aliases={len(d['aliases'])}")
    missing = enum - found
    if missing:
        problems.append(f"enum values with no playbook: {sorted(missing)}")
    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(" -", p)
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
