from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from huggingface_hub import InferenceClient


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_IMAGE_REF_RE = re.compile(r"<\s*Image\s*(\d+)\s*>", flags=re.IGNORECASE)


def _image_to_b64(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url

    p = Path(path_or_url)
    b = p.read_bytes()
    suffix = p.suffix.lower()

    if suffix in [".jpg", ".jpeg"]:
        mime = "image/jpeg"
    elif suffix == ".png":
        mime = "image/png"
    elif suffix == ".webp":
        mime = "image/webp"
    else:
        mime = "application/octet-stream"

    b64 = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{b64}"


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

    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

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


def generate_multimodal_response(
    image_paths_or_urls: List[str],
    raw_text: str,
    hf_token: str,
    model: str = "Qwen/Qwen3-VL-8B-Instruct",
    provider: Optional[str] = "novita",
    max_tokens: int = 5000,
    temperature: float = 0.0,
) -> Tuple[str, str]:
    """
    Sends the fixed JSON-only instruction plus images to a multimodal model
    and returns: (thinking_text, final_json_string)

    - image_paths_or_urls: list of local paths or http(s) URLs
    - final_json_string: validated JSON (string) following the user's slide schema
    """
    client = InferenceClient(token=hf_token, provider=provider, timeout=120)

    images_payload = [_image_to_b64(p) for p in image_paths_or_urls]
    images_placeholders = [f"<Image {i+1}>" for i in range(len(images_payload))]

    # Original prompt preserved, then only the additional speaker_notes request is appended.
    user_instruction = (
        "How many slides are required to explain the full story of the text? "
        + "text = "
        + raw_text.strip()
        + "\n"
        "Understand the text fully and create two kinds of slides:\n"
        "type1: without image\n"
        "type2: with images\n"
        "Both slide types must contain content.\n"
        "If a slide uses an image, it must explain the image and its impact.\n"
        "All images must be used and explained in the slide flow.\n"
        "The slides should follow a flow that explains the full story.\n"
        "Each slide's content should be concise.\n"
        "Explain impact in 3-4 bullet points inside the slide object's steps as separate step entries.\n"
        "The slides should start with a title-only slide and end with a thank-you slide.\n"
        "Output must strictly follow the JSON slide structure below.\n\n"
        "Slide structure:\n"
        "{\n"
        ' \"slides\": [\n'
        " {\n"
        ' \"id\": \"slide1\",\n'
        ' \"order\": 1,\n'
        ' \"type\": \"content | image\",\n'
        ' \"title\": \"Slide title\",\n'
        ' \"subtitle\": null,\n'
        ' \"image_ref\": null,\n'
        ' \"notes\": \"Short explanation or speaker notes\",\n'
        ' \"steps\": [\n'
        " {\n"
        ' \"number\": 1,\n'
        ' \"heading\": \"Point heading\",\n'
        ' \"content\": \"Concise explanation\"\n'
        " }\n"
        " ]\n"
        " }\n"
        " ]\n"
        "}\n\n"
        "Rules:\n"
        "- 'type' must be either 'content' or 'image'\n"
        "- Title slide must have empty steps array\n"
        "- Thank you slide should have 1 step only\n"
        "- Each normal slide must have 3-4 steps\n"
        "- Keep content concise and meaningful\n\n"
        "Map images to placeholders in order: "
        + ", ".join(images_placeholders)
        + ".\n"
        'When referencing an image in a slide object, set "image_ref" to the placeholder string '
        '(for example, "<Image 1>").\n'
        "Return JSON only.\n"
        "\n"
        "Additional output field:\n"
        '- Also include a top-level "speaker_notes" field.\n'
        '- Keep the existing "slides" structure unchanged.\n'
        '- "speaker_notes" should be structured by slide and step for audio generation.\n'
        "\n"
        "speaker_notes expected structure:\n"
        "{\n"
        '  "speaker_notes": [\n'
        "    {\n"
        '      "slide": 1,\n'
        '      "steps": [\n'
        "        {\n"
        '          "step": 1,\n'
        '          "text": "Opening context...",\n'
        '          "refs": link_PlaceHolder \n'
        "        },\n"
        "        {\n"
        '          "step": 2,\n'
        '          "text": "Explain the figure...",\n'
        '          "refs": link_PlaceHolder \n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Additional rules for speaker_notes:\n"
        "- Preserve all existing image references, slide order, titles, subtitles, notes, and steps.\n"
        "- Do not rename or remove any existing slide fields.\n"
        "- Keep the narration as a clear story from start to finish.\n"
        "- Make speaker_notes consistent with the slide content and the image references.\n"
        "- Use slide/step granularity so each step can be converted into audio later.\n"
        "- Return strict JSON only.\n"
    )

    user_content_parts = [{"type": "text", "text": user_instruction}]
    for i, img in enumerate(images_payload):
        user_content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": img, "alt": f"Image {i+1}"},
            }
        )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful instruction-following assistant that can see and reason about images. "
                "STRICTLY RETURN JSON ONLY when requested."
            ),
        },
        {
            "role": "user",
            "content": user_content_parts,
        },
    ]

    resp = client.chat_completion(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    raw_output: Optional[str] = None
    try:
        raw_output = resp.choices[0].message.content
    except Exception:
        raw_output = getattr(resp, "output_text", None) or getattr(resp, "output", None)

    if isinstance(raw_output, list):
        raw_output = " ".join(
            [p.get("text", "") if isinstance(p, dict) else str(p) for p in raw_output]
        )

    if raw_output is None:
        raise RuntimeError("Unable to read text output from InferenceClient response object.")

    thinking = ""
    final_text = raw_output.strip()

    if "</think>" in raw_output:
        thinking, final_text = raw_output.rsplit("</think>", 1)
        thinking = thinking.replace("<think>", "").strip()
        final_text = final_text.strip()

    json_text = None
    try:
        parsed = json.loads(final_text)
        json_text = json.dumps(parsed, ensure_ascii=False)
    except Exception:
        candidate = clean_json_text(final_text)
        try:
            parsed = json.loads(candidate)
            json_text = json.dumps(parsed, ensure_ascii=False)
        except Exception:
            m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", candidate)
            if m:
                candidate = m.group(1)
                for end in range(len(candidate), 0, -1):
                    try:
                        parsed = json.loads(candidate[:end])
                        json_text = json.dumps(parsed, ensure_ascii=False)
                        break
                    except Exception:
                        continue

    if json_text is None:
        diagnostic = {
            "error": "model_output_not_valid_json",
            "raw_output": final_text,
        }
        json_text = json.dumps(diagnostic, ensure_ascii=False)

    return thinking, json_text


def generate_multimodal_slides(
    image_paths_or_urls: List[str],
    hf_token: str,
    raw_text: str,
    model: str = "Qwen/Qwen3-VL-8B-Instruct",
    provider: Optional[str] = "novita",
    max_tokens: int = 5000,
    temperature: float = 0.0,
) -> Tuple[str, str]:
    """
    Wrapper around generate_multimodal_response that:
    - calls the multimodal model,
    - parses the returned JSON,
    - replaces any image placeholders like <Image 1> in slide.image_ref with
      corresponding /uploads/figures/ web paths,
    - returns (thinking, final_json_string) where final_json_string is JSON text.
    """
    thinking, json_text = generate_multimodal_response(
        image_paths_or_urls=image_paths_or_urls,
        raw_text=raw_text,
        hf_token=hf_token,
        model=model,
        provider=provider,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    try:
        data = json.loads(json_text)
    except Exception:
        return thinking, json_text

    web_paths: List[str] = []
    for p in image_paths_or_urls:
        sp = str(p)

        idx = sp.find("/uploads/figures/")
        if idx != -1:
            web_paths.append(sp[idx:])
            continue

        if sp.startswith("/uploads/"):
            web_paths.append(sp)
            continue

        web_paths.append("/uploads/figures/" + Path(sp).name)

    slides = data.get("slides", [])
    for s in slides:
        img_ref = s.get("image_ref")
        if isinstance(img_ref, str):
            m = _IMAGE_REF_RE.search(img_ref)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(web_paths):
                    s["image_ref"] = web_paths[idx]
                else:
                    s["image_ref"] = None

    final_json = json.dumps(data, ensure_ascii=False)
    return thinking, final_json