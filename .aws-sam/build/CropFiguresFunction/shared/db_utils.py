from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3

ddb = boto3.resource("dynamodb")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


def table(table_name: str):
    return ddb.Table(table_name)


def get_job(table_name: str, upload_id: str) -> Optional[Dict[str, Any]]:
    resp = table(table_name).get_item(Key={"upload_id": upload_id})
    item = resp.get("Item")
    return _normalize(item) if item else None


def put_job(table_name: str, item: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = _normalize(item)
    table(table_name).put_item(Item=cleaned)
    return cleaned


def update_job(table_name: str, upload_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    current = get_job(table_name, upload_id) or {"upload_id": upload_id}
    current.update(_normalize(updates))
    current["upload_id"] = upload_id
    current["updated_at"] = utc_now()
    put_job(table_name, current)
    return current