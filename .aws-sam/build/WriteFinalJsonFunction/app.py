import os, json, boto3
from pathlib import Path

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
# CDN_BASE: CloudFront URL or S3 website URL, e.g. https://d1234.cloudfront.net
CDN_BASE = os.environ.get("CDN_BASE","").rstrip("/")
table    = dynamodb.Table(TABLE)

def handler(event, context):
    upload_id  = event["upload_id"]
    slides     = event.get("slides", [])
    pdf_key    = event.get("pdf_key")

    base      = f"uploads/{upload_id}/"
    final_key = base + "final.json"
    index_key = base + "index.html"

    # Save final.json
    s3.put_object(Bucket=BUCKET, Key=final_key,
                  Body=json.dumps({"slides": slides}, indent=2).encode(),
                  ContentType="application/json")

    # Copy viewer.html → index.html in the upload folder
    viewer_path = Path(__file__).with_name("viewer.html")
    if viewer_path.exists():
        html = viewer_path.read_text(encoding="utf-8")
        s3.put_object(Bucket=BUCKET, Key=index_key,
                      Body=html.encode("utf-8"), ContentType="text/html")

    # Build public viewer URL
    viewer_url = (f"{CDN_BASE}/{index_key}" if CDN_BASE
                  else f"https://{BUCKET}.s3.amazonaws.com/{index_key}")

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression=(
            "SET #s=:s, step=:step, progress=:p, message=:m, "
            "final_key=:fk, viewer_key=:vk, viewer_url=:vu"
        ),
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "done",
            ":step": "write_final_json",
            ":p":    95,
            ":m":    "Slides saved — ready to view",
            ":fk":   final_key,
            ":vk":   index_key,
            ":vu":   viewer_url,   # <-- frontend polls for this
        },
    )

    return {
        "upload_id":  upload_id,
        "final_key":  final_key,
        "viewer_key": index_key,
        "viewer_url": viewer_url,
        "pdf_key":    pdf_key,
    }

def lambda_handler(event, context):
    return handler(event, context)