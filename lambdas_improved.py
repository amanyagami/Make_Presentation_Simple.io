"""
=============================================================================
IMPROVED LAMBDA FUNCTIONS — DECK BUILDER
=============================================================================

SUMMARY OF IMPROVEMENTS
─────────────────────────────────────────────────────────────────────────────
1. STATUS LAMBDA: Returns `previews` list (with CloudFront URLs), `viewer_url`,
   and `message` so the frontend can drive entirely off polling.

2. RENDER_PREVIEWS: Stores preview URLs in DynamoDB so status endpoint can
   return them — no separate "previews ready" endpoint needed.

3. WRITE_FINAL_JSON: Returns the public viewer URL (CloudFront/S3 Website)
   and writes it to DynamoDB so status polling surfaces it.

4. START_PREPROCESS (the HTTP-facing lambda called by the frontend on upload
   AND on "Generate Slides"): Distinguishes the two calls by payload shape.
   • On first call (just pdf_key, no selections): starts render_previews +
     extract_text in parallel via Step Functions.
   • On second call (selections present): starts crop_figures → call_model →
     write_final_json → cleanup_data via Step Functions.

5. CLEANUP: Runs automatically at the end of the processing workflow — the
   frontend doesn't need to call it.

6. All lambdas set a `message` field in DynamoDB updates so the frontend
   status bar shows human-readable progress text.

=============================================================================
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. create_upload/app.py   (unchanged, but add CORS_HEADERS helper)
# ─────────────────────────────────────────────────────────────────────────────

CREATE_UPLOAD = '''
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
'''

# ─────────────────────────────────────────────────────────────────────────────
# 2. status/app.py   (IMPROVED — returns previews list & viewer_url)
# ─────────────────────────────────────────────────────────────────────────────

STATUS = '''
import os, json, boto3
from boto3.dynamodb.types import TypeDeserializer

dynamodb = boto3.resource("dynamodb")
TABLE    = os.environ["TABLE_NAME"]
table    = dynamodb.Table(TABLE)

CORS = {
    "Content-Type":                "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers":"Content-Type,Authorization",
    "Access-Control-Allow-Methods":"OPTIONS,GET",
}

def lambda_handler(event, context):
    method = (event.get("httpMethod") or
              event.get("requestContext",{}).get("http",{}).get("method","")).upper()

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS, "body": ""}

    upload_id = (event.get("pathParameters") or {}).get("upload_id")
    if not upload_id:
        return {"statusCode": 400, "headers": CORS,
                "body": json.dumps({"error": "upload_id required"})}

    resp = table.get_item(Key={"upload_id": upload_id})
    item = resp.get("Item")
    if not item:
        return {"statusCode": 404, "headers": CORS,
                "body": json.dumps({"error": "not found"})}

    # Convert Decimal → int/float for JSON serialisation
    import decimal
    def default(o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o == int(o) else float(o)
        raise TypeError

    return {
        "statusCode": 200,
        "headers": CORS,
        "body": json.dumps(item, default=default),
    }
'''

# ─────────────────────────────────────────────────────────────────────────────
# 3. render_previews/app.py   (IMPROVED — stores preview URL list in DynamoDB)
# ─────────────────────────────────────────────────────────────────────────────

RENDER_PREVIEWS = '''
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
'''

# ─────────────────────────────────────────────────────────────────────────────
# 4. extract_text/app.py   (minimal change — update message in DynamoDB)
# ─────────────────────────────────────────────────────────────────────────────

EXTRACT_TEXT = '''
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

    return {"upload_id": upload_id, "text_key": text_key}
'''

# ─────────────────────────────────────────────────────────────────────────────
# 5. crop_figures/app.py   (unchanged in logic, add message field)
# ─────────────────────────────────────────────────────────────────────────────

CROP_FIGURES = '''
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

    return {"upload_id": upload_id, "figures": figures}
'''

# ─────────────────────────────────────────────────────────────────────────────
# 6. call_model/app.py   (add message updates, minor cleanup)
# ─────────────────────────────────────────────────────────────────────────────

CALL_MODEL = '''
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
            p = tmpdir / f"{f[\'id\']}.png"
            s3.download_file(BUCKET, f["s3_key"], str(p))
            local_paths.append(str(p))

        try:
            _, vlm_json_text = generate_multimodal_slides(
                local_paths, os.environ.get("HF_TOKEN",""), raw_text)
        except Exception as e:
            vlm_json_text = json.dumps({"error": str(e)})

    _update(upload_id, "running", "call_model", 75, "Generating slide content with LLM…")

    fig_lines = "\\n".join(f"- {f[\'id\']}: {f[\'s3_key\']}" for f in figures) or "None"
    prompt = f"""
You are given:
1) RAW_TEXT from a PDF
2) VLM_SLIDES (JSON from image understanding)
3) FIGURES (image references)

Task: Improve and expand slides using RAW_TEXT. Keep image references.
Return EXACTLY one JSON object with key "slides".

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
            ":m":    f"Slides generated ({len(data.get(\'slides\',[]))} slides)",
            ":fk":   final_key,
        },
    )

    return {"upload_id": upload_id, "final_key": final_key, "slides": data.get("slides",[])}
'''

# ─────────────────────────────────────────────────────────────────────────────
# 7. write_final_json/app.py   (IMPROVED — stores public viewer_url in DynamoDB)
# ─────────────────────────────────────────────────────────────────────────────

WRITE_FINAL_JSON = '''
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
'''

# ─────────────────────────────────────────────────────────────────────────────
# 8. cleanup_data/app.py   (unchanged logic, add final state = "done")
# ─────────────────────────────────────────────────────────────────────────────

CLEANUP_DATA = '''
import os, boto3
from botocore.exceptions import ClientError

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET   = os.environ["BUCKET_NAME"]
TABLE    = os.environ["TABLE_NAME"]
table    = dynamodb.Table(TABLE)

def delete_prefix(bucket, prefix):
    deleted, token = 0, None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token: kwargs["ContinuationToken"] = token
        resp     = s3.list_objects_v2(**kwargs)
        contents = resp.get("Contents", [])
        if not contents: break
        objs = [{"Key": o["Key"]} for o in contents]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket,
                              Delete={"Objects": objs[i:i+1000], "Quiet": True})
            deleted += len(objs[i:i+1000])
        if resp.get("IsTruncated"): token = resp.get("NextContinuationToken")
        else: break
    return deleted

def handler(event, context):
    upload_id = event["upload_id"]
    pdf_key   = event.get("pdf_key")
    base      = f"uploads/{upload_id}/"
    deleted   = []

    n = delete_prefix(BUCKET, base + "previews/")
    deleted.append({"prefix": base + "previews/", "count": n})

    for key in [base+"raw.txt", base+"vlm.raw.txt", base+"llm.raw.txt"]:
        try:
            s3.delete_object(Bucket=BUCKET, Key=key)
            deleted.append({"key": key, "deleted": True})
        except ClientError:
            deleted.append({"key": key, "deleted": False})

    if pdf_key:
        try:
            s3.delete_object(Bucket=BUCKET, Key=pdf_key)
            deleted.append({"key": pdf_key, "deleted": True})
        except ClientError:
            deleted.append({"key": pdf_key, "deleted": False})

    # Final state = "done" (not "cleaned") so frontend polling stops correctly
    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s=:s, step=:step, progress=:p, message=:m, cleanup_done=:c",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":    "done",
            ":step": "cleanup_data",
            ":p":    100,
            ":m":    "Processing complete",
            ":c":    True,
        },
    )

    return {"upload_id": upload_id, "deleted": deleted}
'''

# ─────────────────────────────────────────────────────────────────────────────
# 9. start_preprocess/app.py   (IMPROVED — single entry point for BOTH calls)
#    The frontend calls POST /process/:upload_id
#    • Without "selections" key → starts preprocessing workflow
#    • With "selections" key    → starts processing workflow
# ─────────────────────────────────────────────────────────────────────────────

START_PREPROCESS = '''
import os, json, uuid, boto3

dynamodb       = boto3.resource("dynamodb")
stepfunctions  = boto3.client("stepfunctions")
TABLE          = os.environ["TABLE"]
PREPROCESS_ARN = os.environ["PREPROCESS_STATE_MACHINE_ARN"]
PROCESS_ARN    = os.environ["PROCESS_STATE_MACHINE_ARN"]
table          = dynamodb.Table(TABLE)

CORS = {
    "Content-Type":                "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers":"Content-Type,Authorization",
    "Access-Control-Allow-Methods":"OPTIONS,POST",
}

def _resp(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body)}

def lambda_handler(event, context):
    method = (event.get("httpMethod") or
              event.get("requestContext",{}).get("http",{}).get("method","")).upper()

    if method == "OPTIONS":
        return {"statusCode":200,"headers":CORS,"body":""}

    upload_id = (event.get("pathParameters") or {}).get("upload_id")
    if not upload_id:
        body = event.get("body","")
        if isinstance(body, str):
            body = json.loads(body) if body else {}
        upload_id = (body if isinstance(body, dict) else {}).get("upload_id")

    if not upload_id:
        return _resp(400, {"error": "upload_id required"})

    # Look up existing record
    item = table.get_item(Key={"upload_id": upload_id}).get("Item")
    if not item:
        return _resp(404, {"error": "upload not found"})

    pdf_key  = item.get("pdf_key")
    pdf_name = item.get("pdf_filename")

    # Parse body
    raw_body = event.get("body","")
    if isinstance(raw_body, str):
        raw_body = json.loads(raw_body) if raw_body else {}
    body = raw_body if isinstance(raw_body, dict) else {}

    selections = body.get("selections")   # None = preprocessing call

    if selections is None:
        # ── FIRST CALL: preprocess (render previews + extract text) ──────────
        state_machine_arn = PREPROCESS_ARN
        name              = f"preprocess-{upload_id}-{uuid.uuid4().hex[:8]}"
        workflow_input    = {"upload_id": upload_id, "pdf_key": pdf_key,
                             "pdf_filename": pdf_name}
        new_state         = "running"
        new_step          = "start_preprocess"
        progress          = 10
        message           = "Preprocessing started"
    else:
        # ── SECOND CALL: full processing (crop → model → write → cleanup) ────
        state_machine_arn = PROCESS_ARN
        name              = f"process-{upload_id}-{uuid.uuid4().hex[:8]}"
        workflow_input    = {
            "upload_id":  upload_id,
            "pdf_key":    pdf_key,
            "text_key":   item.get("text_key", f"uploads/{upload_id}/raw.txt"),
            "selections": selections,
        }
        new_state   = "running"
        new_step    = "start_processing"
        progress    = 50
        message     = f"Processing started ({len(selections)} selection(s))"

    table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s=:s, step=:step, progress=:p, message=:m",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":s":new_state, ":step":new_step, ":p":progress, ":m":message},
    )

    resp = stepfunctions.start_execution(
        stateMachineArn=state_machine_arn,
        name=name,
        input=json.dumps(workflow_input),
    )

    return _resp(202, {
        "upload_id":     upload_id,
        "execution_arn": resp["executionArn"],
        "message":       message,
    })
'''

print("All lambda source strings defined.")
print("See the docstring at the top of this file for the full architecture description.")
