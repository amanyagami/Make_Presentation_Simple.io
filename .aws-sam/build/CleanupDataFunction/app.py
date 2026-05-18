import json
import os
from typing import Dict, List, Optional

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

BUCKET = os.environ["BUCKET_NAME"] if "BUCKET_NAME" in os.environ else os.environ["BUCKET"]
TABLE = os.environ["TABLE_NAME"] if "TABLE_NAME" in os.environ else os.environ["TABLE"]
table = dynamodb.Table(TABLE)


def _update_job(upload_id: str, **fields) -> None:
    expr_names = {}
    expr_values = {}
    sets = []

    for i, (k, v) in enumerate(fields.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        expr_names[nk] = k
        expr_values[vk] = v
        sets.append(f"{nk} = {vk}")

    if not sets:
        return

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _list_keys(bucket: str, prefix: str) -> List[str]:
    keys: List[str] = []
    continuation_token: Optional[str] = None

    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/"):
                keys.append(key)

        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break

    return keys


def _delete_keys(bucket: str, keys: List[str]) -> int:
    if not keys:
        return 0

    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        resp = s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        deleted += len(batch) - len(resp.get("Errors", []))

    return deleted


def _safe_delete_key(bucket: str, key: Optional[str]) -> bool:
    if not key:
        return False
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _is_preserved_figure(key: str) -> bool:
    """
    Preserve figure artifacts.
    Example keys:
      uploads/<id>/figure1.png
      uploads/<id>/figure2.png
    """
    name = key.split("/")[-1].lower()
    return name.startswith("figure") and name.endswith((".png", ".jpg", ".jpeg", ".webp"))


def handler(event, context):
    """
    Cleanup stage:
    - deletes temporary/intermediate files
    - preserves figure assets
    - preserves final deliverables
    - updates DynamoDB job state to done
    """
    upload_id = event["upload_id"]
    pdf_key = event.get("pdf_key")
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    base = f"uploads/{upload_id}/"

    # Final outputs to keep.
    keep_exact = {
        base + "final.json",
        base + "final_slides.json",
        base + "index.html",
        base + "speaker_notes.json",
        base + "model_output.json",
        base + "narration.mp4",
        base + "narration.wav",
        base + "timeline.json",
        base + "audio/",
    }

    # Delete only temporary/intermediate prefixes.
    delete_prefixes = [
        base + "previews/",
        base + "audio_tmp/",
        base + "tmp/",
    ]

    deleted_summary: Dict[str, int] = {}

    for prefix in delete_prefixes:
        keys = _list_keys(BUCKET, prefix)
        deleted_summary[prefix] = _delete_keys(BUCKET, keys)

    # Delete common single-file intermediates.
    single_files = [
        base + "raw.txt",
        base + "vlm.raw.txt",
        base + "llm.raw.txt",
        base + "raw.json",
        base + "vlm.json",
        base + "llm.json",
        base + "figure_map.json",
    ]

    for key in single_files:
        if _safe_delete_key(BUCKET, key):
            deleted_summary[key] = 1
        else:
            deleted_summary[key] = 0

    # Delete the original uploaded PDF if it is provided.
    if pdf_key:
        deleted_summary[pdf_key] = 1 if _safe_delete_key(BUCKET, pdf_key) else 0

    # Keep figures, but delete any other leftover non-final, non-figure objects.
    all_keys = _list_keys(BUCKET, base)
    extra_keys = []

    for key in all_keys:
        if key in keep_exact:
            continue
        if key.endswith("/"):
            continue
        if _is_preserved_figure(key):
            continue
        if "/audio/" in key:
            continue
        if key.endswith("final.json"):
            continue
        if key.endswith("final_slides.json"):
            continue
        if key.endswith("index.html"):
            continue
        if key.endswith("speaker_notes.json"):
            continue
        if key.endswith("model_output.json"):
            continue
        if key.endswith("narration.mp4"):
            continue
        if key.endswith("narration.wav"):
            continue
        if key.endswith("timeline.json"):
            continue
        extra_keys.append(key)

    deleted_summary["extra_keys"] = _delete_keys(BUCKET, extra_keys)

    _update_job(
        upload_id,
        state="done",
        step="cleanup_data",
        progress=100,
        message="Processing complete",
        cleanup_report=json.dumps(deleted_summary),
        timing_cleanup_data={
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int(round((time.perf_counter() - started_perf) * 1000)),
            "request_id": getattr(context, "aws_request_id", None),
            "deleted_summary": deleted_summary,
        },
    )

    return {
        "upload_id": upload_id,
        "state": "done",
        "progress": 100,
        "message": "Processing complete",
        "cleanup_report": deleted_summary,
    }


def lambda_handler(event, context):
    return handler(event, context)