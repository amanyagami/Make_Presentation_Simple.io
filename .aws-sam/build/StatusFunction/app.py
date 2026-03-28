
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