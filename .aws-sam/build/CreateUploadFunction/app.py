import os, json, uuid, boto3
from datetime import datetime, timezone

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
table    = dynamodb.Table(TABLE)

CORS = {
    "Content-Type":                "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers":"Content-Type,Authorization",
    "Access-Control-Allow-Methods":"OPTIONS,POST,GET",
}

def _resp(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body)}

def lambda_handler(event, context):
    method = (event.get("httpMethod") or
              event.get("requestContext",{}).get("http",{}).get("method","")).upper()

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS, "body": ""}

    body = event.get("body", event)
    if isinstance(body, str):
        body = json.loads(body) if body else {}
    if not isinstance(body, dict):
        body = {}

    filename = body.get("filename")
    if not filename:
        return _resp(400, {"message": "filename is required"})

    content_type = body.get("content_type", "application/pdf")
    upload_id    = uuid.uuid4().hex
    safe_name    = os.path.basename(filename).replace(" ", "_")
    pdf_key      = f"uploads/{upload_id}/{safe_name}"

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": pdf_key, "ContentType": content_type},
        ExpiresIn=3600,
    )

    table.put_item(Item={
        "upload_id":   upload_id,
        "state":       "waiting_for_upload",
        "step":        "created",
        "progress":    0,
        "message":     "Waiting for PDF upload",
        "pdf_filename": safe_name,
        "pdf_key":     pdf_key,
        "content_type": content_type,
        "created_at":  datetime.now(timezone.utc).isoformat(),
    })

    return _resp(200, {
        "upload_id":  upload_id,
        "pdf_key":    pdf_key,
        "upload_url": upload_url,
    })