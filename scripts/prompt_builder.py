"""Assembles the extraction system prompt by combining the static prompt text
with a category list generated live from playbook/*.yaml, so the prompt and
the playbook never drift apart.
"""
from __future__ import annotations

import glob
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_PATH = os.path.join(ROOT, "prompts", "extraction_system_prompt.md")
PLAYBOOK_DIR = os.path.join(ROOT, "playbook")


def _load_categories() -> list[dict]:
    categories = []
    for f in sorted(glob.glob(os.path.join(PLAYBOOK_DIR, "*.yaml"))):
        if os.path.basename(f) == "_template.yaml":
            continue
        with open(f, encoding="utf-8") as fh:
            d = yaml.safe_load(fh)
        categories.append(d)
    return categories


def _render_categories_block(categories: list[dict]) -> str:
    lines = []
    for c in categories:
        aliases = ", ".join(f'"{a}"' for a in c.get("aliases", [])[:6])
        lines.append(f"- `{c['id']}` ({c['display_name']}): phrases like {aliases}")
    return "\n".join(lines)


def build_system_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        template = f.read()
    block = _render_categories_block(_load_categories())
    return template.replace("{{DENIAL_REASON_CATEGORIES}}", block)


if __name__ == "__main__":
    print(build_system_prompt())
