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