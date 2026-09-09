# Insurance denial appeal helper

Turns a health insurance denial letter into a drafted appeal letter, a
checklist of what to attach, and a deadline reminder. See [SCOPE.md](SCOPE.md)
for what this is and deliberately is not.

## Run it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # not needed for the sample-letter demo
python app.py
# open http://127.0.0.1:5000 and try "sample denial letter" — no API key needed for that path
```

## How the pieces fit together

| Piece | What it does | Where |
|---|---|---|
| Extraction schema | The structured shape every denial letter gets read into | [schema/](schema/denial_extraction.schema.json), [schema/VALIDATION.md](schema/VALIDATION.md) |
| Extraction prompt + runner | Turns a document into that structure via the model | [prompts/](prompts/extraction_system_prompt.md), [scripts/extract.py](scripts/extract.py) |
| Eval | Scores extraction accuracy against hand-labelled letters | [eval/](eval/README.md) |
| Playbook | Per-denial-reason arguments, questions, checklist, aliases | [playbook/](playbook/README.md) |
| Letter generator | Turns extraction + question answers into a letter | [scripts/letter_generator.py](scripts/letter_generator.py) |
| Reminders | Deadline follow-up email/text, escalating as it nears | [reminders/](reminders/README.md) |
| Web wrapper | The upload page, question flow, and result page tying the above together | [app.py](app.py), [templates/](templates/) |

Each has its own README with the detail; this file is just the map.

## Status

MVP, not production. See each README's own caveats — the web wrapper's
in particular (in-memory session state, no real email/SMS provider wired
in) is worth reading before treating this as more than a working
demonstration of the loop.
