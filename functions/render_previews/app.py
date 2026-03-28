import os, json, tempfile, boto3
from pathlib import Path
import fitz  # PyMuPDF

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
# CDN_BASE is the CloudFront (or S3 website) base URL, e.g. https://d1234.cloudfront.net
CDN_BASE = os.environ.get("CDN_BASE", "").rstrip("/")
table    = dynamodb.Table(TABLE)

def handler(event, context):
    upload_id = event["upload_id"]
    pdf_key   = event["pdf_key"]

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = Path(tmpdir) / "input.pdf"
        s3.download_file(BUCKET, pdf_key, str(pdf_path))

        doc      = fitz.open(str(pdf_path))
        previews = []

        for i, page in enumerate(doc):
            pix        = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            name       = f"page{i+1}.png"
            local_path = Path(tmpdir) / name
            pix.save(str(local_path))

            s3_key = f"uploads/{upload_id}/previews/{name}"
            s3.upload_file(str(local_path), BUCKET, s3_key,
                           ExtraArgs={"ContentType": "image/png"})

            # Build the public URL the frontend can use directly
            public_url = (f"{CDN_BASE}/{s3_key}" if CDN_BASE
                          else f"https://{BUCKET}.s3.amazonaws.com/{s3_key}")

            previews.append({
                "page": i + 1,
                "s3_key": s3_key,
                "url": public_url,   # <-- frontend needs this
            })

    # KEY IMPROVEMENT: store previews in DynamoDB so status endpoint returns them
    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s = :s, step = :step, progress = :p, message = :m, previews = :v",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "previews_ready",
            ":step": "render_previews",
            ":p":    25,
            ":m":    f"Page previews ready ({len(previews)} pages)",
            ":v":    previews,
        },
    )

    return {"upload_id": upload_id, "previews": previews}

def lambda_handler(event, context):
    return handler(event, context)