import os
import json
import boto3

sf = boto3.client("stepfunctions")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

def handler(event, context):
    upload_id = event["upload_id"]

    response = sf.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=json.dumps(event),
    )

    return {
        "upload_id": upload_id,
        "execution_arn": response["executionArn"],
        "message": "processing started",
    }