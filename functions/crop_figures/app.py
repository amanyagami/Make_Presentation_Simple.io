import os, tempfile, boto3
from pathlib import Path
from PIL import Image

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
table    = dynamodb.Table(TABLE)

def handler(event, context):
    upload_id  = event["upload_id"]
    selections = event["selections"]  # [{page, x, y, w, h, type}]

    figures = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        for i, sel in enumerate(selections, 1):
            page        = int(sel["page"])
            preview_key = f"uploads/{upload_id}/previews/page{page}.png"
            local_prev  = tmpdir / f"page{page}.png"

            s3.download_file(BUCKET, preview_key, str(local_prev))

            x, y, w, h = int(sel["x"]), int(sel["y"]), int(sel["w"]), int(sel["h"])
            with Image.open(local_prev) as im:
                cropped = im.crop((x, y, x + w, y + h))
                name    = f"figure{i}.png"
                path    = tmpdir / name
                cropped.save(path)

            s3_key = f"uploads/{upload_id}/{name}"
            s3.upload_file(str(path), BUCKET, s3_key,
                           ExtraArgs={"ContentType": "image/png"})
            figures.append({"id": f"figure{i}", "s3_key": s3_key,
                             "type": sel.get("type", "figure")})

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s = :s, step = :step, progress = :p, message = :m",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "figures_ready",
            ":step": "crop_figures",
            ":p":    55,
            ":m":    f"Cropped {len(figures)} figure(s)",
        },
    )

    return {
    "upload_id": upload_id,
    "pdf_key": event.get("pdf_key"),
    "text_key": event.get("text_key"),
    "figures": figures,
}
def lambda_handler(event, context):
    return handler(event, context)