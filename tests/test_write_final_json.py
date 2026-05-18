import json
import os
from unittest.mock import MagicMock

from tests.module_utils import load_module

os.environ.setdefault("BUCKET", "test-bucket")
os.environ.setdefault("TABLE", "test-table")

MODULE = load_module("write_final_json_app", "functions/write_final_json/app.py")


def test_handler_writes_final_json_and_viewer_url(monkeypatch):
    mock_s3 = MagicMock()
    mock_s3.put_object = MagicMock()

    monkeypatch.setattr(MODULE, "s3", mock_s3)
    recorded_updates = []
    monkeypatch.setattr(
        MODULE,
        "_update",
        lambda upload_id, state, step, progress, message, final_key, viewer_key, viewer_url: recorded_updates.append(
            (upload_id, state, step, progress, message, final_key, viewer_key, viewer_url)
        ),
    )

    event = {
        "upload_id": "upload-1",
        "slides": [{"id": "slide1", "order": 1}],
        "speaker_notes": [{"slide": 1, "steps": [{"step": 1, "text": "Hi"}]}],
        "audio_map": {"slide1_step1": {"audio_url": "https://example.com/audio.mp3", "audio_key": "uploads/upload-1/audio/slide1_step1.mp3"}},
    }

    result = MODULE.handler(event, None)

    assert result["viewer_url"] == "https://test-bucket.s3.amazonaws.com/uploads/upload-1/index.html"
    assert mock_s3.put_object.call_count == 2

    final_call = next(call for call in mock_s3.put_object.call_args_list if call.kwargs["Key"].endswith("final.json"))
    final_payload = json.loads(final_call.kwargs["Body"].decode("utf-8"))
    assert final_payload["audio_map"]["slide1_step1"]["audio_url"] == "https://example.com/audio.mp3"
    assert recorded_updates
