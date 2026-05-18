import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import boto3

from llm_query import generate_response
from vlm_query import generate_multimodal_slides

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

BUCKET = os.environ["BUCKET_NAME"] if "BUCKET_NAME" in os.environ else os.environ["BUCKET"]
TABLE = os.environ["TABLE_NAME"] if "TABLE_NAME" in os.environ else os.environ["TABLE"]
HF_TOKEN = os.environ.get("HF_TOKEN", "")
table = dynamodb.Table(TABLE)


def _update(upload_id: str, state: str, step: str, progress: int, message: str) -> None:
    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s=:s, step=:step, progress=:p, message=:m",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s": state,
            ":step": step,
            ":p": progress,
            ":m": message,
        },
    )


def _read_text(key: str) -> str:
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8", errors="ignore")


def _normalize_json_text(value: Any) -> str:
    if isinstance(value, tuple):
        value = value[-1]
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _parse_payload(raw: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("slides", [])
            data.setdefault("speaker_notes", [])
            return data
    except Exception:
        pass
    return fallback


def _build_prompt(raw_text: str, vlm_text: str, figures: List[Dict[str, Any]]) -> str:
    figure_lines = "\n".join(
        f"- {f.get('id', 'figure')}: {f.get('s3_key', '')}" for f in figures
    ) or "None"

    original_prompt = f"""
You are given:
1) RAW_TEXT from a PDF
2) VLM_SLIDES (JSON from image understanding)
3) FIGURES (image references)

Task:
Improve the presentation by adding more slides if required and improving the content of each slide so the story is clearer and better explained. Keep image references unchanged.

Return EXACTLY one JSON object with keys "slides" and "speaker_notes".

Expected slide structure:
{{
  "slides": [
    {{
      "id": "slide1",
      "order": 1,
      "type": "content | image",
      "title": "",
      "subtitle": "",
      "image_ref": null,
      "notes": "",
      "steps": [
        {{
          "number": 1,
          "heading": "",
          "content": ""
        }},
        {{
          "number": 2,
          "heading": "",
          "content": ""
        }}
      ]
    }}
  ]
}}

Rules:
- Preserve existing image references from VLM_SLIDES and FIGURES.
- Add new slides only when needed to explain the full story better.
- Keep slide content concise, clear, and presentation-ready.
- Each slide should have 3-4 meaningful steps unless it is a title or thank-you slide.
- Start with a title slide and end with a thank-you slide.
- Ensure slide order is logical and the narrative flows well.
- If a slide has an image, its steps must explain the image and its impact.
""".strip()

    added_rules = """
Additional output field:
- Also include a top-level "speaker_notes" field.
- Keep the existing "slides" structure unchanged.
- "speaker_notes" should be structured by slide and step for audio generation.

speaker_notes expected structure:
{
  "speaker_notes": [
    {
      "slide": 1,
      "steps": [
        {
          "step": 1,
          "text": "Opening context...",
          "refs":  link_PlaceHolder
        },
        {
          "step": 2,
          "text": "Explain the figure...",
          "refs":  link_PlaceHolder
        }
      ]
    }
  ]
}

Additional rules for speaker_notes:
- Preserve all existing image references, slide order, titles, subtitles, notes, and steps.
- Do not rename or remove any existing slide fields.
- Keep the narration as a clear story from start to finish.
- Make speaker_notes consistent with the slide content and the image references.
- Use slide/step granularity so each step can be converted into audio later.
- Return strict JSON only.
""".strip()

    return f"""
{original_prompt}

{added_rules}

VLM_SLIDES:
{vlm_text}

FIGURES:
{figure_lines}

RAW_TEXT:
{raw_text}
""".strip()


def _extract_step_notes(slides: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Best-effort normalization for a speaker_notes field.
    If the model already returns structured notes, keep them.
    If it only returns slides, derive a compatible structure from slides.steps.
    """
    notes: List[Dict[str, Any]] = []

    for slide in slides or []:
        slide_id = slide.get("id")
        order = slide.get("order")
        steps = slide.get("steps", [])

        normalized_steps = []
        for idx, step in enumerate(steps or [], start=1):
            normalized_steps.append(
                {
                    "step": step.get("number", idx),
                    "text": step.get("content", "") or step.get("heading", ""),
                    "image_refs": [slide.get("image_ref")] if slide.get("image_ref") is not None else [],
                }
            )

        if slide_id is not None or order is not None:
            notes.append(
                {
                    "slide": order if order is not None else slide_id,
                    "steps": normalized_steps,
                }
            )

    return notes


def lambda_handler(event, context):
    upload_id = event["upload_id"]
    text_key = event.get("text_key") or f"uploads/{upload_id}/raw.txt"
    figures = event.get("figures", [])
    output_key = f"uploads/{upload_id}/final.json"
    speaker_notes_key = f"uploads/{upload_id}/speaker_notes.json"
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    raw_text = _read_text(text_key)

    _update(upload_id, "running", "call_model", 60, "Running visual analysis...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        local_paths: List[str] = []

        for f in figures:
            fig_id = f.get("id") or f.get("name") or "figure"
            s3_key = f["s3_key"]
            local_path = tmpdir / f"{fig_id}.png"
            s3.download_file(BUCKET, s3_key, str(local_path))
            local_paths.append(str(local_path))

        # Pass 1: multimodal draft
        try:
            vlm_result = generate_multimodal_slides(local_paths, HF_TOKEN, raw_text)
        except Exception as e:
            vlm_result = {
                "slides": [],
                "speaker_notes": [],
                "error": str(e),
            }

        vlm_text = _normalize_json_text(vlm_result)

        _update(upload_id, "running", "call_model", 75, "Refining slide content and speaker notes...")

        prompt = _build_prompt(raw_text, vlm_text, figures)

        try:
            _, final_response = generate_response(prompt, HF_TOKEN)
        except Exception:
            final_response = ""

        final_response = _normalize_json_text(final_response)

    fallback = _parse_payload(vlm_text, {"slides": [], "speaker_notes": []})
    data = _parse_payload(final_response, fallback)

    slides = data.get("slides", [])
    speaker_notes = data.get("speaker_notes", [])

    if not speaker_notes:
        speaker_notes = _extract_step_notes(slides)

    payload = {
        "upload_id": upload_id,
        "slides": slides,
        "speaker_notes": speaker_notes,
    }

    s3.put_object(
        Bucket=BUCKET,
        Key=speaker_notes_key,
        Body=json.dumps(
            {
                "upload_id": upload_id,
                "speaker_notes": speaker_notes,
            },
            indent=2,
        ).encode("utf-8"),
        ContentType="application/json",
    )

    s3.put_object(
        Bucket=BUCKET,
        Key=output_key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    _update(
        upload_id,
        "model_done",
        "call_model",
        88,
        f"Slides generated ({len(slides)} slides)",
        timing_call_model={
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int(round((time.perf_counter() - started_perf) * 1000)),
            "request_id": getattr(context, "aws_request_id", None),
            "slides": len(slides),
            "figures": len(figures),
        },
    )

    return {
        "upload_id": upload_id,
        "text_key": text_key,
        "final_key": output_key,
        "speaker_notes_key": speaker_notes_key,
        "slides": slides,
        "speaker_notes": speaker_notes,
        "figures": figures,
    }
 