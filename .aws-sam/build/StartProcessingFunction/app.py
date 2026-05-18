import os
import json
import boto3
import time
from datetime import datetime, timezone

sf = boto3.client("stepfunctions")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

def handler(event, context):
    upload_id = event["upload_id"]
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    response = sf.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=json.dumps(event),
    )

    return {
        "upload_id": upload_id,
        "execution_arn": response["executionArn"],
        "message": "processing started",
        "timing_start_processing": {
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int(round((time.perf_counter() - started_perf) * 1000)),
            "request_id": getattr(context, "aws_request_id", None),
        },
    }