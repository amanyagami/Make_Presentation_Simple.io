import json
import os
from unittest.mock import MagicMock

from tests.module_utils import load_module

os.environ.setdefault("BUCKET", "test-bucket")
os.environ.setdefault("TABLE", "test-table")
os.environ.setdefault("HF_TOKEN", "test-token")

MODULE = load_module("call_model_app", "functions/call_model/app.py")


def test_normalize_json_text_handles_tuple_dict_none_and_strings():
    assert MODULE._normalize_json_text((None, '{"slides":[]}')) == '{"slides":[]}'
    assert MODULE._normalize_json_text({"slides": []}) == '{"slides": []}'
    assert MODULE._normalize_json_text(None) == ""
    assert MODULE._normalize_json_text("hello") == "hello"


def test_parse_payload_returns_fallback_for_invalid_json():
    fallback = {"slides": [1]}
    assert MODULE._parse_payload("not json", fallback) == fallback


def test_extract_step_notes_from_slides():
    slides = [
        {
            "id": "slide1",
            "order": 1,
            "image_ref": "/uploads/figures/figure1.png",
            "steps": [{"number": 1, "content": "text"}],
        }
    ]

    notes = MODULE._extract_step_notes(slides)
    assert notes == [
        {
            "slide": 1,
            "steps": [
                {
                    "step": 1,
                    "text": "text",
                    "image_refs": ["/uploads/figures/figure1.png"],
                }
            ],
        }
    ]


def test_lambda_handler_writes_final_json_and_speaker_notes(monkeypatch):
    raw_text_body = MagicMock(read=MagicMock(return_value=b"raw text"))
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": raw_text_body}
    mock_s3.put_object = MagicMock()

    monkeypatch.setattr(MODULE, "s3", mock_s3)
    monkeypatch.setattr(MODULE, "generate_multimodal_slides", lambda local_paths, token, raw_text: ("thinking", '{"slides":[{"id":"slide1","order":1,"type":"content","title":"Hello","subtitle":null,"image_ref":null,"notes":"","steps":[{"number":1,"heading":"Title","content":"Hello"}]}],"speaker_notes":[{"slide":1,"steps":[{"step":1,"text":"Hello"}]}]}'))
    monkeypatch.setattr(MODULE, "generate_response", lambda prompt, token: ("thinking2", '{"slides":[{"id":"slide1","order":1,"type":"content","title":"Hello","subtitle":null,"image_ref":null,"notes":"","steps":[{"number":1,"heading":"Title","content":"Hello"}]}],"speaker_notes":[{"slide":1,"steps":[{"step":1,"text":"Hello"}]}]}'))
    monkeypatch.setattr(MODULE, "_update", lambda upload_id, state, step, progress, message: None)

    result = MODULE.lambda_handler({"upload_id": "upload-1", "figures": []}, None)

    assert result["upload_id"] == "upload-1"
    assert result["slides"]
    assert result["speaker_notes"]
    assert mock_s3.put_object.call_count == 2
    keys = [call.kwargs["Key"] for call in mock_s3.put_object.call_args_list]
    assert "uploads/upload-1/speaker_notes.json" in keys
    assert "uploads/upload-1/final.json" in keys


def test_lambda_handler_uses_vlm_fallback_when_final_response_invalid(monkeypatch):
    raw_text_body = MagicMock(read=MagicMock(return_value=b"raw text"))
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": raw_text_body}
    mock_s3.put_object = MagicMock()

    monkeypatch.setattr(MODULE, "s3", mock_s3)
    monkeypatch.setattr(MODULE, "generate_multimodal_slides", lambda local_paths, token, raw_text: ("thinking", '{"slides":[{"id":"slide1","order":1,"type":"content","title":"Fallback","subtitle":null,"image_ref":null,"notes":"","steps":[{"number":1,"heading":"Title","content":"Fallback"}]}],"speaker_notes":[{"slide":1,"steps":[{"step":1,"text":"Fallback"}]}]}'))
    monkeypatch.setattr(MODULE, "generate_response", lambda prompt, token: ("thinking2", "not valid json"))
    monkeypatch.setattr(MODULE, "_update", lambda upload_id, state, step, progress, message: None)

    result = MODULE.lambda_handler({"upload_id": "upload-2", "figures": []}, None)

    assert result["slides"][0]["title"] == "Fallback"
    assert result["speaker_notes"][0]["steps"][0]["text"] == "Fallback"
