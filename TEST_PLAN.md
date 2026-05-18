# Test Plan for PPT Maker Lambdas

## Goals

1. Verify whether audio generation is performed correctly for a given generated slide manifest.
2. Validate each important lambda handler path with unit tests and mocked dependencies.
3. Catch regressions in model parsing, final JSON generation, and S3 persistence.

## Test Strategy

- Use `pytest` for lightweight test discovery and assertions.
- Use `unittest.mock` for mocking AWS clients, subprocess calls, and external model responses.
- Keep tests isolated from real AWS, real model inference, and real audio tooling.

## Scope

### Audio generation tests

Verify `functions/generate_audio/app.py`:

- `_normalize_notes()` should return a list for valid speaker notes and `[]` for invalid payloads.
- `lambda_handler()` should:
  - read `final.json` from S3
  - generate audio files for each speaker note step when data is present
  - upload MP3s with the expected S3 key pattern
  - write back a `final.json` with `audio_map`
  - skip audio generation when `speaker_notes` is empty
- Ensure `piper` and `ffmpeg` execution is invoked for speaker note steps.

### Lambda validation tests

Create tests for each lambda module focused on:

- `functions/call_model/app.py`
  - JSON normalization and fallback behavior
  - speaker notes extraction from slide steps
  - handler behavior with mocked VLM and LLM output
- `functions/call_model/vlm_query.py`
  - JSON cleaning and fence removal
  - placeholder replacement for image references
  - safe handling of invalid JSON from the model
- `functions/write_final_json/app.py`
  - correct `final.json` and `index.html` writes to S3
  - computed `viewer_url`
  - audio refs attached into speaker notes

### Additional lambda test ideas

The same pattern can be extended to other lambdas:

- `functions/cleanup_data/app.py`
  - preserve final artifacts and delete intermediate files only
- `functions/extract_text/app.py`
  - validate text extraction from S3 bytes and storage behavior
- `functions/render_previews/app.py`
  - verify preview generation path and upload logic with mocked image APIs
- `functions/crop_figures/app.py`
  - ensure crop metadata and figure uploads happen correctly

These can be added later as the repository matures.

## Test structure

- `tests/` — all pytest test modules
- `tests/module_utils.py` — shared module loader helper for direct file imports
- `tests/test_generate_audio.py` — audio generation tests
- `tests/test_call_model.py` — call model lambda tests
- `tests/test_vlm_query.py` — VLM helper tests
- `tests/test_write_final_json.py` — final JSON writer tests

## Running tests

Install pytest if not already installed:

```bash
pip install pytest
```

Run all tests:

```bash
cd /home/yagami/bigssd/ppt_maker
python -m pytest tests/ -q
```

## Notes

- These tests are designed to be unit-level and do not require a live AWS environment.
- If you want a second layer of verification later, add integration tests with `moto` or local SAM emulation.
