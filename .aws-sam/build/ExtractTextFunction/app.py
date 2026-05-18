import os, tempfile, boto3, time
from datetime import datetime, timezone
from pathlib import Path
import fitz

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
table    = dynamodb.Table(TABLE)

def handler(event, context):
    upload_id = event["upload_id"]
    pdf_key   = event["pdf_key"]
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = Path(tmpdir) / "input.pdf"
        s3.download_file(BUCKET, pdf_key, str(pdf_path))

        doc  = fitz.open(str(pdf_path))
        text = "\\n".join(p.get_text("text") for p in doc)

        text_key = f"uploads/{upload_id}/raw.txt"
        s3.put_object(Bucket=BUCKET, Key=text_key,
                      Body=text.encode("utf-8"), ContentType="text/plain")

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s = :s, step = :step, progress = :p, message = :m, text_key = :tk, timing_extract_text = :tim",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "text_ready",
            ":step": "extract_text",
            ":p":    40,
            ":m":    "Text extraction complete",
            ":tk":   text_key,
            ":tim": {
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int(round((time.perf_counter() - started_perf) * 1000)),
                "request_id": getattr(context, "aws_request_id", None),
            },
        },
    )

    return {
    "upload_id": upload_id,
    "pdf_key": pdf_key,
    "text_key": text_key,
}

def lambda_handler(event, context):
    return handler(event, context)