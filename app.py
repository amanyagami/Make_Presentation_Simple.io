import subprocess

MODEL = "en_US-ljspeech-high.onnx"

def handler(event, context):
    text = event.get("text", "Hello from Lambda")

    out = "/tmp/out.wav"

    subprocess.run(
        [
            "python3", "-m", "piper",
            "-m", MODEL,
            "-f", out
        ],
        input=text.encode("utf-8"),
        check=True,
    )

    return {
        "statusCode": 200,
        "body": f"Audio saved to {out}"
    }