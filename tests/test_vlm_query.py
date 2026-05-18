import json
from unittest.mock import MagicMock, patch

from tests.module_utils import load_module

MODULE = load_module("call_model_vlm_query", "functions/call_model/vlm_query.py")


def test_clean_json_text_strips_markdown_fences():
    raw = "Here is the response:\n```json\n{\"slides\":[{}]}\n```\nExtra"
    assert MODULE.clean_json_text(raw) == '{"slides":[{}]}'


def test_generate_multimodal_response_parses_json_from_model(monkeypatch):
    mock_client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content='{"slides":[{"id":"slide1"}]}'))]
    mock_client.chat_completion.return_value = response

    monkeypatch.setattr(MODULE, "InferenceClient", lambda token, provider=None, **kwargs: mock_client)
    thinking, json_text = MODULE.generate_multimodal_response([], "raw text", "fake-token")

    assert thinking == ""
    assert json.loads(json_text) == {"slides": [{"id": "slide1"}]}


def test_generate_multimodal_slides_rewrites_image_references(monkeypatch):
    slides_payload = {
        "slides": [
            {
                "id": "slide1",
                "order": 1,
                "type": "image",
                "title": "Image slide",
                "subtitle": "",
                "image_ref": "<Image 1>",
                "notes": "",
                "steps": [{"number": 1, "heading": "Caption", "content": "Text"}],
            }
        ]
    }
    monkeypatch.setattr(
        MODULE,
        "generate_multimodal_response",
        lambda *args, **kwargs: ("", json.dumps(slides_payload)),
    )

    _, json_text = MODULE.generate_multimodal_slides(["/tmp/figure1.png"], "fake-token", "raw text")
    result = json.loads(json_text)

    assert result["slides"][0]["image_ref"] == "/uploads/figures/figure1.png"
