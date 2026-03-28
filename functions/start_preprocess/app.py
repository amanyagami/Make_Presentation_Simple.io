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