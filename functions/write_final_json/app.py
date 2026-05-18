import json
import os
import boto3
import re
from pathlib import Path

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

BUCKET = os.environ["BUCKET_NAME"] if "BUCKET_NAME" in os.environ else os.environ["BUCKET"]
TABLE = os.environ["TABLE_NAME"] if "TABLE_NAME" in os.environ else os.environ["TABLE"]
CDN_BASE = os.environ.get("CDN_BASE", "").rstrip("/")
table = dynamodb.Table(TABLE)


def _update(upload_id: str, state: str, step: str, progress: int, message: str, final_key: str, viewer_key: str, viewer_url: str) -> None:
    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression=(
            "SET #s=:s, step=:step, progress=:p, message=:m, "
            "final_key=:fk, viewer_key=:vk, viewer_url=:vu"
        ),
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s": state,
            ":step": step,
            ":p": progress,
            ":m": message,
            ":fk": final_key,
            ":vk": viewer_key,
            ":vu": viewer_url,
        },
    )


def _public_url(bucket: str, key: str) -> str:
    if CDN_BASE:
        return f"{CDN_BASE}/{key}"
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def _normalize_image_ref(upload_id: str, image_ref: str) -> str:
    if not image_ref:
        return image_ref

    ref = str(image_ref).strip()
    if not ref:
        return ref

    if ref.startswith("http://") or ref.startswith("https://"):
        return ref

    # Handle legacy refs produced by model prompts like /uploads/figures/figure1.png
    legacy_match = re.search(r"(?:^|/)figure(\d+)\.(png|jpg|jpeg|webp)$", ref, flags=re.IGNORECASE)
    if ref.startswith("/uploads/figures/") and legacy_match:
        key = f"uploads/{upload_id}/figure{legacy_match.group(1)}.{legacy_match.group(2).lower()}"
        return _public_url(BUCKET, key)

    # Handle bare refs like figure1.png
    bare_match = re.fullmatch(r"figure(\d+)\.(png|jpg|jpeg|webp)", ref, flags=re.IGNORECASE)
    if bare_match:
        key = f"uploads/{upload_id}/figure{bare_match.group(1)}.{bare_match.group(2).lower()}"
        return _public_url(BUCKET, key)

    # Handle S3-style keys or absolute upload paths.
    if ref.startswith("/"):
        ref = ref.lstrip("/")
    if ref.startswith("uploads/"):
        return _public_url(BUCKET, ref)

    return image_ref


def _normalize_slides(upload_id: str, slides):
    if not isinstance(slides, list):
        return []

    normalized = []
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        row = dict(slide)
        if row.get("image_ref"):
            row["image_ref"] = _normalize_image_ref(upload_id, row.get("image_ref"))
        normalized.append(row)
    return normalized


def handler(event, context):
    upload_id = event["upload_id"]
    slides = _normalize_slides(upload_id, event.get("slides", []))
    speaker_notes = event.get("speaker_notes", [])
    audio_map = event.get("audio_map", {})
    pdf_key = event.get("pdf_key")
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    base = f"uploads/{upload_id}/"
    final_key = base + "final.json"
    viewer_key = base + "index.html"

    # Attach audio refs back into speaker_notes for downstream UI.
    if isinstance(speaker_notes, list):
        for slide_item in speaker_notes:
            slide_no = slide_item.get("slide")
            for step_item in slide_item.get("steps", []):
                step_no = step_item.get("step")
                audio_id = f"slide{slide_no}_step{step_no}"
                audio_ref = audio_map.get(audio_id, {})
                if audio_ref:
                    step_item["audio_key"] = audio_ref.get("audio_key")
                    step_item["audio_url"] = audio_ref.get("audio_url")

    payload = {
        "upload_id": upload_id,
        "slides": slides,
        "speaker_notes": speaker_notes,
        "audio_map": audio_map,
    }

    s3.put_object(
        Bucket=BUCKET,
        Key=final_key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    # Keep your existing viewer artifact behavior.
    viewer_path = Path(__file__).with_name("viewer.html")
    if viewer_path.exists():
        html = viewer_path.read_text(encoding="utf-8")
        s3.put_object(
            Bucket=BUCKET,
            Key=viewer_key,
            Body=html.encode("utf-8"),
            ContentType="text/html",
        )

    viewer_url = _public_url(BUCKET, viewer_key)

    _update(
        upload_id,
        "done",
        "write_final_json",
        98,
        "Final slides and audio saved",
        final_key,
        viewer_key,
        viewer_url,
        timing_write_final_json={
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int(round((time.perf_counter() - started_perf) * 1000)),
            "request_id": getattr(context, "aws_request_id", None),
            "slides": len(slides),
            "audio_entries": len(audio_map) if isinstance(audio_map, dict) else 0,
        },
    )

    return {
        "upload_id": upload_id,
        "final_key": final_key,
        "viewer_key": viewer_key,
        "viewer_url": viewer_url,
        "slides": slides,
        "speaker_notes": speaker_notes,
        "audio_map": audio_map,
        "pdf_key": pdf_key,
    }

def lambda_handler(event, context):
    return handler(event, context)