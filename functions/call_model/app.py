import os, json, tempfile, boto3
from pathlib import Path
from llm_query import generate_response
from vlm_query import generate_multimodal_slides

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
table    = dynamodb.Table(TABLE)

def _update(upload_id, state, step, progress, message):
    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s=:s, step=:step, progress=:p, message=:m",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":s":state,":step":step,":p":progress,":m":message},
    )

def handler(event, context):
    upload_id = event["upload_id"]
    text_key  = event["text_key"]
    figures   = event.get("figures", [])

    # Load text
    raw_text = s3.get_object(Bucket=BUCKET, Key=text_key)["Body"].read().decode("utf-8")

    _update(upload_id, "running", "call_model", 60, "Running visual analysis…")

    # Download figures for VLM
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        local_paths = []
        for f in figures:
            p =  tmpdir / f"{f['id']}.png"
            s3.download_file(BUCKET, f["s3_key"], str(p))
            local_paths.append(str(p))

        try:
            _, vlm_json_text = generate_multimodal_slides(
                local_paths, os.environ.get("HF_TOKEN",""), raw_text)
        except Exception as e:
            vlm_json_text = json.dumps({"error": str(e)})

    _update(upload_id, "running", "call_model", 75, "Generating slide content with LLM…")

    fig_lines = "\n".join(f"- {f['id']}: {f['s3_key']}" for f in figures) or "None"
    prompt = f"""
You are given:
1) RAW_TEXT from a PDF
2) VLM_SLIDES (JSON from image understanding)
3) FIGURES (image references)

Task:
Improve the presentation by adding more slides if required and improving the content of each slide so the story is clearer and better explained. Keep image references unchanged.

Return EXACTLY one JSON object with key "slides" only.

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

VLM_SLIDES:
{vlm_json_text}

FIGURES:
{fig_lines}

RAW_TEXT:
{raw_text}
""".strip()

    try:
        _, final_response = generate_response(prompt, os.environ.get("HF_TOKEN",""))
    except Exception:
        final_response = ""

    # Parse JSON safely
    data = None
    try:
        data = json.loads(final_response)
    except Exception:
        pass

    if data is None:
        try:
            data = json.loads(vlm_json_text)
        except Exception:
            data = {"slides": []}

    if isinstance(data, list):
        data = {"slides": data}

    final_key = f"uploads/{upload_id}/final.json"
    s3.put_object(Bucket=BUCKET, Key=final_key,
                  Body=json.dumps(data, indent=2).encode("utf-8"),
                  ContentType="application/json")

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s=:s, step=:step, progress=:p, message=:m, final_key=:fk",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "model_done",
            ":step": "call_model",
            ":p":    88,
            ":m":    f"Slides generated {len(data.get('slides', []))} slides",
            ":fk":   final_key,
        },
    )

    return {"upload_id": upload_id, "final_key": final_key, "slides": data.get("slides",[])}


def lambda_handler(event, context):
    return handler(event, context)