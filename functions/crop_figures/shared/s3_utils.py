import json
from pathlib import Path
from typing import Any, Optional

import boto3

s3 = boto3.client("s3")


def put_json(bucket: str, key: str, payload: Any) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def get_json(bucket: str, key: str) -> Any:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def put_text(bucket: str, key: str, text: str, content_type: str = "text/plain") -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType=content_type,
    )


def get_text(bucket: str, key: str) -> str:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


def upload_file(bucket: str, key: str, local_path: str, content_type: Optional[str] = None) -> None:
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    s3.upload_file(local_path, bucket, key, ExtraArgs=extra_args or None)


def download_file(bucket: str, key: str, local_path: str) -> None:
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, local_path)


def presign_put_url(bucket: str, key: str, expires_in: int = 3600, content_type: str = "application/pdf") -> str:
    return s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )


def presign_get_url(bucket: str, key: str, expires_in: int = 3600) -> str:
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"