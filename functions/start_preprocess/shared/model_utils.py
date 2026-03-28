import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared.s3_utils import s3_uri

MAX_PROMPT_TEXT = 40000


def load_hf_token() -> str:
    token = os.getenv("HF_TOKEN", "").strip()
    if token:
        return token

    token_file = Path(os.getenv("HF_TOKEN_FILE", "/tmp/hf_token.txt"))
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token

    raise RuntimeError("HF token not found in HF_TOKEN or HF_TOKEN_FILE")


def truncate_text(text: str, limit: int = MAX_PROMPT_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[TRUNCATED]"


def call_vlm(image_paths: List[str], hf_token: str, raw_text: str):
    from vlm_query import generate_multimodal_slides

    return generate_multimodal_slides(image_paths, hf_token, raw_text)


def call_llm(prompt: str, hf_token: str):
    from llm_query import generate_response

    return generate_response(prompt, hf_token)


def build_prompt(raw_text: str, bucket: str, figures_index: List[Dict[str, Any]], vlm_json_text: str) -> str:
    figure_lines = "\n".join(
        [f"- {item['id']}: {s3_uri(bucket, item['key'])}" for item in figures_index]
    ) or "None"

    return f"""
INSTRUCTIONS:
You will be given:
1) RAW PDF TEXT
2) VLM candidate slides
3) A figure index

Task:
- Read RAW PDF TEXT to understand the document.
- Read VLM candidate slides.
- Improve, reorder, and complete the deck.
- Preserve image references when possible.
- Return EXACTLY one JSON object with a single top-level key "slides".
- Do not output markdown or extra commentary.

FIGURES:
{figure_lines}

VLM_SLIDES:
{vlm_json_text or "null"}

RAW_TEXT:
{truncate_text(raw_text)}

SCHEMA:
{{
  "slides": [
    {{
      "id": "slide1",
      "order": 1,
      "type": "content|image|mixed",
      "title": "...",
      "subtitle": null,
      "image_ref": null,
      "notes": "...",
      "steps": [
        {{
          "number": 1,
          "heading": "...",
          "content": "..."
        }}
      ]
    }}
  ]
}}

Return JSON only.
""".strip()


def extract_json_blob(text: str, fallback_text: Optional[str] = None) -> Optional[Any]:
    candidates = [text or ""]
    if fallback_text:
        candidates.append(fallback_text)

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue

        try:
            return json.loads(candidate)
        except Exception:
            pass

        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])\s*$", candidate)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass

    return None