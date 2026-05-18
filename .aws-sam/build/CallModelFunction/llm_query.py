from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional, Tuple

from huggingface_hub import InferenceClient


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.IGNORECASE | re.DOTALL,
)


def clean_json_text(text: str) -> str:
    """
    Best-effort cleanup for model output.

    Keeps the content intact, but removes:
    - markdown code fences
    - leading/trailing whitespace
    - obvious pre/post text around the first JSON object/array

    This does NOT remove any JSON keys.
    """
    if not text:
        return ""

    text = text.strip()

    # Remove fenced JSON blocks if present.
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # If the model wrapped JSON in extra commentary, try to isolate the first
    # balanced JSON object or array.
    start_candidates = [i for i in [text.find("{"), text.find("[")] if i != -1]
    if not start_candidates:
        return text

    start = min(start_candidates)
    opener = text[start]
    closer = "}" if opener == "{" else "]"

    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(text)):
        ch = text[idx]

        if escape:
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1].strip()

    return text[start:].strip()


def safe_json_loads(text: str, default: Optional[Any] = None) -> Any:
    """
    Parse JSON after cleaning. Returns `default` if parsing fails.
    """
    if default is None:
        default = {}

    cleaned = clean_json_text(text)
    if not cleaned:
        return default

    try:
        return json.loads(cleaned)
    except Exception:
        return default


def generate_response(prompt: str, hf_token: str) -> Tuple[str, str]:
    """
    Sends a prompt to the Qwen Thinking model via Hugging Face InferenceClient
    and returns separated thinking and final response.

    Returns:
        (thinking_text, final_text)
    """
    client = InferenceClient(token=hf_token, provider="nscale", timeout=120)

    resp = client.chat_completion(
        model="Qwen/Qwen3-4B-Instruct-2507",
        messages=[
            {"role": "user", "content": prompt}
        ],
        max_tokens=5000,
        temperature=0.7,
    )

    raw_output = resp.choices[0].message.content

    # Split thinking and final answer using </think>
    if "</think>" in raw_output:
        thinking, final = raw_output.rsplit("</think>", 1)
        thinking = thinking.replace("<think>", "").strip()
        final = final.strip()
    else:
        thinking = ""
        final = raw_output.strip()

    # Clean up common formatting issues without changing JSON keys.
    final = clean_json_text(final)

    return thinking, final


def generate_vlm_response(
    prompt: str,
    hf_token: str,
    image_path: Optional[str] = None,
) -> str:
    """
    Sends a prompt + optional image to the Qwen VL model and returns raw content.
    """
    client = InferenceClient(token=hf_token, provider="nscale", timeout=120)

    content_blocks = []

    if image_path:
        with open(image_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("utf-8")

        content_blocks.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_base64}"
                },
            }
        )

    content_blocks.append(
        {
            "type": "text",
            "text": prompt,
        }
    )

    resp = client.chat_completion(
        model="Qwen/Qwen3-VL-8B-Instruct",
        messages=[
            {
                "role": "user",
                "content": content_blocks,
            }
        ],
        max_tokens=2000,
        temperature=0.7,
    )

    return clean_json_text(resp.choices[0].message.content)