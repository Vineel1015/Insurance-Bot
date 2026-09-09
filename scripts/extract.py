#!/usr/bin/env python
"""Run extraction on a single denial letter.

Usage:
    python scripts/extract.py path/to/letter.pdf
    python scripts/extract.py path/to/letter.jpg --out result.json
    python scripts/extract.py path/to/letter.txt --model claude-opus-5

Supported inputs:
    .pdf                 sent as a native document content block
    .jpg / .jpeg / .png  sent as an image content block
    .txt / .md           sent as plain text (for OCR'd letters or test fixtures)

Requires ANTHROPIC_API_KEY in the environment, unless --raw-cache points at a
previously saved raw tool-call JSON (useful for offline testing of validation
and scoring without spending an API call).
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompt_builder import build_system_prompt  # noqa: E402
from schema_utils import build_tool_definition, load_schema, validate_instance  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000

TEXT_EXTS = {".txt", ".md"}
IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PDF_EXTS = {".pdf": "application/pdf"}


def _content_block_for_file(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        raw = f.read()

    if ext in TEXT_EXTS:
        return {"type": "text", "text": raw.decode("utf-8", errors="replace")}

    if ext in PDF_EXTS:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": PDF_EXTS[ext], "data": base64.b64encode(raw).decode()},
        }

    if ext in IMAGE_EXTS:
        media_type = IMAGE_EXTS[ext]
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(raw).decode()},
        }

    mime, _ = mimetypes.guess_type(path)
    raise ValueError(f"Unsupported file type for {path} (guessed mime: {mime}). Supported: pdf, jpg/jpeg/png/webp, txt/md.")


def extract_document(
    path: str,
    model: str = DEFAULT_MODEL,
    client=None,
    raw_cache_path: str | None = None,
) -> tuple[dict, dict]:
    """Returns (extraction_dict, meta) where meta includes raw usage/model info.

    If raw_cache_path exists, the cached tool-call input is used instead of
    calling the API. If it doesn't exist and a live call is made, the result
    is written there for next time (pass raw_cache_path=None to disable).
    """
    if raw_cache_path and os.path.exists(raw_cache_path):
        with open(raw_cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        return cached["extraction"], {"model": cached.get("model", model), "cached": True}

    import anthropic  # imported lazily so offline/cached runs don't need the package configured

    client = client or anthropic.Anthropic()
    schema = load_schema()
    tool = build_tool_definition(schema)
    system_prompt = build_system_prompt()
    doc_block = _content_block_for_file(path)

    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[
            {
                "role": "user",
                "content": [
                    doc_block,
                    {"type": "text", "text": "Extract this denial letter into the record_denial_extraction tool."},
                ],
            }
        ],
    )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(f"Model did not call the extraction tool. Response: {response.content}")

    extraction = tool_use.input
    extraction.setdefault("extraction_meta", {})
    extraction["extraction_meta"]["model"] = model
    extraction["extraction_meta"].setdefault("prompt_version", "0.1.0")
    extraction["extraction_meta"].setdefault("schema_version", "0.1.0")

    meta = {
        "model": model,
        "cached": False,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }

    if raw_cache_path:
        os.makedirs(os.path.dirname(raw_cache_path), exist_ok=True)
        with open(raw_cache_path, "w", encoding="utf-8") as f:
            json.dump({"extraction": extraction, "model": model}, f, indent=2)

    return extraction, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("document", help="Path to a .pdf, .jpg/.jpeg/.png, or .txt/.md denial letter")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", help="Write the extraction JSON here (default: print to stdout)")
    ap.add_argument("--raw-cache", help="Path to cache/reuse the raw model output (skips the API call if present)")
    ap.add_argument("--no-validate-schema", action="store_true", help="Skip jsonschema validation of the result")
    args = ap.parse_args()

    extraction, meta = extract_document(args.document, model=args.model, raw_cache_path=args.raw_cache)

    if not args.no_validate_schema:
        errors = validate_instance(extraction)
        if errors:
            print("SCHEMA VALIDATION ERRORS:", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)

    out = json.dumps(extraction, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"Wrote {args.out} (model={meta['model']}, cached={meta['cached']})", file=sys.stderr)
    else:
        print(out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
