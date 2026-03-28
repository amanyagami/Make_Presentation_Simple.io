import os, tempfile, boto3
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
        UpdateExpression="SET #s = :s, step = :step, progress = :p, message = :m, text_key = :tk",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "text_ready",
            ":step": "extract_text",
            ":p":    40,
            ":m":    "Text extraction complete",
            ":tk":   text_key,
        },
    )

    return {
    "upload_id": upload_id,
    "pdf_key": pdf_key,
    "text_key": text_key,
}

def lambda_handler(event, context):
    return handler(event, context)