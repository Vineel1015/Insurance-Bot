"""Structural consistency check for playbook/*.yaml against the template and the
extraction schema. Run: python scripts/check_playbook.py  (exit 1 on problems)."""
import glob, json, os, sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_IDS = {"expedited_request", "lmn_needed"}
REQUIRED_TOP = [
    "id", "display_name", "version", "aliases", "plain_english", "insurer_obligations",
    "clarifying_questions", "arguments", "evidence_checklist", "standing_requests",
    "escalation", "pitfalls", "weak_case_signals",
]

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
        qids = {q["id"] for q in d["clarifying_questions"]}
        aids = {a["id"] for a in d["arguments"]}
        eids = {e["id"] for e in d["evidence_checklist"]}
        nq = len(d["clarifying_questions"])
        if not 3 <= nq <= 5:
            problems.append(f"{name}: {nq} clarifying questions (want 3-5)")
        for q in d["clarifying_questions"]:
            for u in q.get("unlocks", []):
                if u not in aids and u not in CONTROL_IDS:
                    problems.append(f"{name}: question {q['id']} unlocks unknown argument {u}")
            if q["answer_type"] == "choice" and not q.get("choices"):
                problems.append(f"{name}: choice question {q['id']} has no choices")
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
        for o in d["insurer_obligations"]:
            m = o.get("if_missing_argument")
            if m and m not in aids:
                problems.append(f"{name}: obligation {o['id']} -> unknown argument {m}")
        print(f"{name:45s} q={nq} args={len(aids)} evidence={len(eids)} aliases={len(d['aliases'])}")
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
