# functions/generate_audio/app.py

import json
import os
import subprocess
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

import boto3

BUCKET = os.environ["BUCKET"]
TABLE = os.environ["TABLE"]
MODEL = os.environ.get("PIPER_MODEL", "en_US-ljspeech-high.onnx")
MODEL_CONFIG = os.environ.get("PIPER_MODEL_CONFIG", "en_US-ljspeech-high.onnx.json")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE)


def _update_job(upload_id: str, **fields: Any) -> None:
    expr_parts = []
    expr_values = {}
    expr_names = {}

    for i, (k, v) in enumerate(fields.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        expr_names[nk] = k
        expr_values[vk] = v
        expr_parts.append(f"{nk} = {vk}")

    if not expr_parts:
        return

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _normalize_notes(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    notes = payload.get("speaker_notes", [])
    if isinstance(notes, list):
        return notes
    return []


def _synthesize_with_piper(text: str, wav_out: str) -> None:
    """
    Minimal CLI-based Piper invocation.

    Assumes:
      python -m piper -m MODEL -f OUTPUT.wav
    reads text from stdin.
    """
    subprocess.run(
        [
            "python3",
            "-m",
            "piper",
            "-m",
            MODEL,
            "-f",
            wav_out,
        ],
        input=text.encode("utf-8"),
        check=True,
    )


def _wav_to_mp3(wav_path: str, mp3_path: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            wav_path,
            "-codec:a",
            "libmp3lame",
            mp3_path,
        ],
        check=True,
        capture_output=True,
    )


def _collect_audio_tasks(speaker_notes: List[Dict[str, Any]]) -> List[Tuple[int, int, str]]:
    tasks: List[Tuple[int, int, str]] = []
    for slide_item in speaker_notes:
        slide_no = slide_item["slide"]
        for step_item in slide_item.get("steps", []):
            tasks.append((slide_no, step_item["step"], step_item["text"]))
    return tasks


def _render_audio_task(
    upload_id: str,
    audio_dir: Path,
    slide_no: int,
    step_no: int,
    text: str,
) -> Tuple[str, Dict[str, Any]]:
    wav_path = audio_dir / f"slide{slide_no}_step{step_no}.wav"
    mp3_path = audio_dir / f"slide{slide_no}_step{step_no}.mp3"

    _synthesize_with_piper(text, str(wav_path))
    _wav_to_mp3(str(wav_path), str(mp3_path))

    audio_key = f"uploads/{upload_id}/audio/slide{slide_no}_step{step_no}.mp3"
    s3.upload_file(
        str(mp3_path),
        BUCKET,
        audio_key,
        ExtraArgs={"ContentType": "audio/mpeg"},
    )

    return (
        f"slide{slide_no}_step{step_no}",
        {
            "audio_key": audio_key,
            "audio_url": f"https://{BUCKET}.s3.amazonaws.com/{audio_key}",
        },
    )


def lambda_handler(event, context):
    upload_id = event["upload_id"]
    final_json_key = event.get("final_json_key") or f"uploads/{upload_id}/final.json"
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    _update_job(upload_id, state="audio_running", step="generate_audio", message="Generating narration audio")

    obj = s3.get_object(Bucket=BUCKET, Key=final_json_key)
    final_payload = json.loads(obj["Body"].read().decode("utf-8"))

    speaker_notes = _normalize_notes(final_payload)
    audio_map: Dict[str, Dict[str, Any]] = {}
    tasks = _collect_audio_tasks(speaker_notes)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        audio_dir = tmp / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        if tasks:
            _update_job(
                upload_id,
                state="audio_running",
                step="generate_audio",
                message=f"Generating narration audio ({len(tasks)} chunks in parallel)",
                audio_total=len(tasks),
            )

            max_workers = min(4, len(tasks))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_render_audio_task, upload_id, audio_dir, slide_no, step_no, text): (slide_no, step_no)
                    for slide_no, step_no, text in tasks
                }

                completed = 0
                for future in as_completed(futures):
                    slide_no, step_no = futures[future]
                    task_key, audio_entry = future.result()
                    audio_map[task_key] = audio_entry
                    completed += 1
                    _update_job(
                        upload_id,
                        state="audio_running",
                        step="generate_audio",
                        message=f"Generated narration for slide {slide_no}, step {step_no}",
                        audio_progress=completed,
                        audio_total=len(tasks),
                    )

    final_payload["audio_map"] = audio_map

    s3.put_object(
        Bucket=BUCKET,
        Key=final_json_key,
        Body=json.dumps(final_payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    _update_job(
        upload_id,
        state="audio_done",
        step="generate_audio",
        message="Narration audio generated",
        audio_map=json.dumps(audio_map),
        timing_generate_audio={
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int(round((time.perf_counter() - started_perf) * 1000)),
            "request_id": getattr(context, "aws_request_id", None),
            "chunks": len(tasks),
        },
    )

    return {
    "upload_id": upload_id,
    "final_json_key": final_json_key,
    "slides": final_payload.get("slides", []),
    "speaker_notes": final_payload.get("speaker_notes", []),
    "audio_map": audio_map,
    }