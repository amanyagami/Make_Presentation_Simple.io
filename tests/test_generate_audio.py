import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from tests.module_utils import load_module

os.environ.setdefault("BUCKET", "test-bucket")
os.environ.setdefault("TABLE", "test-table")

MODULE = load_module("generate_audio_app", "functions/generate_audio/app.py")


def test_normalize_notes_returns_list_for_list_input():
    payload = {"speaker_notes": [{"slide": 1, "steps": []}]}

    assert MODULE._normalize_notes(payload) == [{"slide": 1, "steps": []}]


def test_normalize_notes_returns_empty_list_for_invalid_payload():
    assert MODULE._normalize_notes({"speaker_notes": {"not": "list"}}) == []


def test_lambda_handler_generates_audio_for_each_step(monkeypatch, tmp_path):
    speaker_notes = [
        {"slide": 1, "steps": [{"step": 1, "text": "Hello world"}]}
    ]
    body = json.dumps({"speaker_notes": speaker_notes}).encode("utf-8")
    mock_body = MagicMock(read=MagicMock(return_value=body))
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": mock_body}
    mock_s3.upload_file = MagicMock()
    mock_s3.put_object = MagicMock()

    uploads = []
    monkeypatch.setattr(MODULE, "s3", mock_s3)
    monkeypatch.setattr(MODULE, "_update_job", lambda upload_id, **fields: uploads.append(fields))

    synth_calls = []
    monkeypatch.setattr(MODULE, "_synthesize_with_piper", lambda text, wav_out: synth_calls.append((text, wav_out)))
    ffmpeg_calls = []
    monkeypatch.setattr(MODULE, "_wav_to_mp3", lambda wav_path, mp3_path: ffmpeg_calls.append((wav_path, mp3_path)))

    result = MODULE.lambda_handler({"upload_id": "upload-1"}, None)

    assert result["upload_id"] == "upload-1"
    assert result["audio_map"]
    assert "slide1_step1" in result["audio_map"]
    assert mock_s3.upload_file.call_count == 1
    assert mock_s3.put_object.call_count == 1
    assert len(synth_calls) == 1
    assert len(ffmpeg_calls) == 1
    assert result["audio_map"]["slide1_step1"]["audio_key"] == "uploads/upload-1/audio/slide1_step1.mp3"


def test_lambda_handler_generates_audio_for_multiple_steps(monkeypatch):
    speaker_notes = [
        {
            "slide": 1,
            "steps": [
                {"step": 1, "text": "First"},
                {"step": 2, "text": "Second"},
            ],
        }
    ]
    body = json.dumps({"speaker_notes": speaker_notes}).encode("utf-8")
    mock_body = MagicMock(read=MagicMock(return_value=body))
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": mock_body}
    mock_s3.upload_file = MagicMock()
    mock_s3.put_object = MagicMock()

    monkeypatch.setattr(MODULE, "s3", mock_s3)
    monkeypatch.setattr(MODULE, "_update_job", lambda upload_id, **fields: None)
    monkeypatch.setattr(MODULE, "_synthesize_with_piper", lambda text, wav_out: None)
    monkeypatch.setattr(MODULE, "_wav_to_mp3", lambda wav_path, mp3_path: None)

    result = MODULE.lambda_handler({"upload_id": "upload-3"}, None)

    assert set(result["audio_map"].keys()) == {"slide1_step1", "slide1_step2"}
    assert mock_s3.upload_file.call_count == 2
    assert mock_s3.put_object.call_count == 1


def test_lambda_handler_skips_audio_when_no_speaker_notes(monkeypatch):
    body = json.dumps({"speaker_notes": []}).encode("utf-8")
    mock_body = MagicMock(read=MagicMock(return_value=body))
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": mock_body}
    mock_s3.upload_file = MagicMock()
    mock_s3.put_object = MagicMock()

    monkeypatch.setattr(MODULE, "s3", mock_s3)
    monkeypatch.setattr(MODULE, "_update_job", lambda upload_id, **fields: None)
    monkeypatch.setattr(MODULE, "_synthesize_with_piper", lambda text, wav_out: None)
    monkeypatch.setattr(MODULE, "_wav_to_mp3", lambda wav_path, mp3_path: None)

    result = MODULE.lambda_handler({"upload_id": "upload-2"}, None)

    assert result["audio_map"] == {}
    assert mock_s3.upload_file.call_count == 0
    assert mock_s3.put_object.call_count == 1
