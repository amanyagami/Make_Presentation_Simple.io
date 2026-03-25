# main.py (updated)
import os
import re
import json
import uuid
import shutil
import subprocess
import logging
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional
import aiofiles

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
 
# Image manipulation
from PIL import Image  # pillow

# Local helpers (expected to exist in same project)
from pdf_to_text import extract_pdf_to_text
from llm_query import generate_response
from vlm_query import generate_multimodal_slides
 

# ----------------------------
# Configuration & Directories
# ----------------------------
APP_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = APP_DIR / "uploads"
PREVIEWS_DIR = UPLOAD_DIR / "previews"
FIGURES_DIR = UPLOAD_DIR / "figures"

# Ensure directories exist
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
FIGURES_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 80 * 1024 * 1024  # 80 MB limit for uploads
HF_TOKEN_FILE = APP_DIR / "hf_token.txt"  # expected HF token location

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf-deck")

# ----------------------------
# App initialization
# ----------------------------
app = FastAPI(title="PDF → Deck Service", docs_url=None, redoc_url=None)

 
# Serve static files (app root) and uploaded files
app.mount("/static", StaticFiles(directory=str(APP_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# CORS policy (permissive for development convenience)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ----------------------------
# Utilities: file IO & helpers
# ----------------------------
async def save_upload_to_disk(target_path: Path, upload_file: UploadFile) -> int:
    """
    Stream an UploadFile to disk in chunks, enforcing MAX_FILE_SIZE.
    Returns the total bytes written.
    """
    total = 0
    async with aiofiles.open(target_path, "wb") as out_f:
        while True:
            chunk = await upload_file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="File too large")
            await out_f.write(chunk)
    # Best-effort close
    try:
        await upload_file.close()
    except Exception:
        pass
    return total


async def write_status(upload_id: str, data: dict):
    """
    Atomically write a JSON status file for this upload under uploads/<upload_id>.status.json
    Uses aiofiles to avoid blocking.
    """
    status_path = UPLOAD_DIR / f"{upload_id}.status.json"
    async with aiofiles.open(status_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False))


def load_hf_token(token_path: Path = HF_TOKEN_FILE) -> str:
    """
    Read the local HuggingFace token file. Raise RuntimeError on missing/empty token.
    """
    if not token_path.exists():
        raise RuntimeError(f"HF token file not found: {token_path.resolve()}")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"HF token file is empty: {token_path.resolve()}")
    return token


# ----------------------------
# PDF preview rendering helpers
# ----------------------------
def pdftoppm_render(pdf_path: Path, outdir: Path) -> List[Path]:
    """
    Use `pdftoppm` (poppler) to render PDF pages to PNG files.
    If pdftoppm is not present or fails, return an empty list.
    Render resolution: 150 DPI.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    base = outdir / f"{pdf_path.stem}"
    cmd = ["pdftoppm", "-png", "-rx", "150", "-ry", "150", str(pdf_path), str(base)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        logger.info("pdftoppm not found; skipping preview render. Install poppler to enable previews.")
        return []
    except subprocess.CalledProcessError as e:
        logger.exception("pdftoppm failed: %s", e)
        return []

    # Expect pdftoppm outputs: <base>-1.png, <base>-2.png, ...
    generated = sorted(outdir.glob(f"{pdf_path.stem}-*.png"))
    return generated


def collect_and_rename_previews(preview_paths: List[Path], upload_id: str) -> List[Dict[str, Any]]:
    """
    Move/copy preview images into uploads/previews/<upload_id>/ and build an index:
      [{"page":1,"path":"/uploads/previews/<upload_id>/page1.png","filename":"page1.png"}, ...]
    If move fails, attempt copy. Skip items that fail.
    """
    dest_dir = PREVIEWS_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, p in enumerate(sorted(preview_paths), start=1):
        ext = p.suffix or ".png"
        dest = dest_dir / f"page{i}{ext}"
        try:
            shutil.move(str(p), str(dest))
        except Exception:
            # fallback to copy
            try:
                shutil.copy(str(p), str(dest))
            except Exception:
                logger.exception("Failed moving/copying preview %s", p)
                continue
        out.append({"page": i, "path": f"/uploads/previews/{upload_id}/page{i}{ext}", "filename": dest.name})
    return out


# ----------------------------
# Image extraction helpers (external extractor integration)
# ----------------------------
def parse_image_paths_from_stdout(stdout: str) -> List[Path]:
    """
    Parse stdout for tokens that look like image file paths (common extensions).
    For relative paths that don't exist as-is, try APP_DIR/<token> and cwd/<token>.
    Return Path objects (may be absolute or candidate relative paths).
    """
    if not stdout:
        return []
    matches = re.findall(r"\S+\.(?:png|jpg|jpeg|gif|tif|tiff|webp)", stdout, flags=re.IGNORECASE)
    paths = []
    for token in matches:
        p = Path(token)
        if not p.is_absolute():
            candidate = APP_DIR / token
            if candidate.exists():
                paths.append(candidate)
                continue
            candidate2 = Path.cwd() / token
            if candidate2.exists():
                paths.append(candidate2)
                continue
            # append candidate2 even if it doesn't exist (keeps ordering)
            paths.append(candidate2)
        else:
            paths.append(p)

    # Deduplicate preserving order, canonicalize if file exists
    seen = set()
    unique = []
    for p in paths:
        rp = str(p.resolve()) if p.exists() else str(p)
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def collect_and_rename_extracted_images_from_paths(paths: List[Path]) -> List[Dict[str, Any]]:
    """
    Move detected image files into uploads/figures as figure1..figureN and return index:
      [{"id":"figure1","path":"/uploads/figures/figure1.png"}, ...]
    Skip missing/non-file paths.
    """
    valid_files = [p for p in paths if p.exists() and p.is_file()]
    valid_files = sorted(valid_files)
    out = []
    for i, p in enumerate(valid_files, start=1):
        ext = p.suffix or ".png"
        dest = FIGURES_DIR / f"figure{i}{ext}"
        try:
            shutil.move(str(p), str(dest))
        except Exception:
            logger.exception("Failed moving extracted image %s", p)
            continue
        out.append({"id": f"figure{i}", "path": f"/uploads/figures/figure{i}{ext}"})
    return out


# ----------------------------
# Image cropping helper
# ----------------------------
def crop_and_save_selection(preview_abs_path: Path, bbox: Dict[str, int], out_path: Path) -> bool:
    """
    Crop a preview image (pixel coordinates) using bbox keys: x,y,w,h and save to out_path.
    Returns True on success, False on failure (and logs the exception).
    """
    try:
        with Image.open(preview_abs_path) as im:
            left = int(bbox["x"])
            top = int(bbox["y"])
            right = left + int(bbox["w"])
            bottom = top + int(bbox["h"])
            cropped = im.crop((left, top, right, bottom))
            cropped.save(out_path)
        return True
    except Exception:
        logger.exception("Failed cropping %s with bbox %s", preview_abs_path, bbox)
        return False


# ----------------------------
# LLM prompt building & normalization
# ----------------------------
def build_single_prompt(raw_text: str, figures_index: List[Dict[str, Any]]) -> str:
    """
    Construct the single prompt sent to the LLM. The prompt requests exactly one JSON object
    with a top-level 'slides' key following a specific schema.
    """
    fig_lines = "\n".join([f"- {f['id']}: {f['path']}" for f in figures_index]) or "None"
    prompt = f"""
INSTRUCTIONS: You will be given the RAW PDF TEXT and a figure index (images produced by user selections).
Design an optimal slide deck based on the content. You DO NOT need to extract table JSON. If any slide benefits from an image, reference it by figure id (e.g. "figure1"). Return EXACTLY one JSON object with a single top-level key "slides" (list). No additional text.

FIGURES:
{fig_lines}

RAW_TEXT:
{raw_text}

SCHEMA (must follow):
slides -> [{{"id":"slide1","order":1,"type":"content|image|mixed","title":"...","subtitle":null,"image_ref":null,"notes":"...","steps":[{{"number":1,"heading":"...","content":"..."}}]}}]

Return JSON only.
""".strip()
    return prompt


# ----------------------------
# Background processing pipeline
# ----------------------------
async def process_upload_background(upload_id: str, pdf_path: Path, basename: str, figures_index: List[Dict[str, Any]]):
    """
    Background pipeline (VLM -> LLM merge):
      1. Extract text
      2. Call VLM to produce image-aware slide candidates (JSON)
      3. Call LLM with VLM JSON + RAW_TEXT + FIGURES to produce final merged slides JSON
      4. Validate/normalize image refs, persist outputs, update status
      5. Cleanup previews and intermediate text files once final manifest is created
    Errors write an 'error' status file for the upload.
    """
    # Keep references to intermediate files so cleanup can target them
    vlm_raw_out_path = None
    raw_out_path = None
    text_path = ""

    try:
        await write_status(upload_id, {"upload_id": upload_id, "state": "running", "step": "extract_text", "message": "Extracting text", "progress": 10})

        # Extract text (helper may return text or (path, text))
        try:
            text_result = extract_pdf_to_text(pdf_path)
            if isinstance(text_result, (tuple, list)):
                text_path, raw_text = text_result[0], text_result[1]
            else:
                raw_text = text_result
                text_path = ""
        except Exception as e:
            logger.exception("Text extraction failed")
            raise RuntimeError(f"Text extraction failed: {e}")

        # Load HF token (raises on missing)
        try:
            hf_token = load_hf_token()
        except Exception as e:
            raise RuntimeError(f"HF token load failed: {e}")

        # Build absolute image paths to pass to VLM (multimodal)
        abs_image_paths = []
        for f in figures_index:
            rel = f["path"].lstrip("/")
            abs_image_paths.append(str(APP_DIR / rel))

        # --- 1) Call VLM to generate candidate slides from images ---
        await write_status(upload_id, {"upload_id": upload_id, "state": "running", "step": "vlm_call", "message": "Calling multimodal model for image summaries", "progress": 40})
        try:
            thinking_vlm, vlm_json_text = generate_multimodal_slides(abs_image_paths, hf_token, raw_text)
        except Exception as e:
            logger.exception("VLM call failed")
            thinking_vlm = ""
            vlm_json_text = json.dumps({"error": "vlm_call_failed", "detail": str(e)}, ensure_ascii=False)

        # Persist VLM raw output for debugging
        try:
            vlm_raw_out_path = UPLOAD_DIR / f"{basename}.vlm.raw.txt"
            async with aiofiles.open(vlm_raw_out_path, "w", encoding="utf-8") as vf:
                await vf.write((thinking_vlm or "") + "\n\n" + (vlm_json_text or ""))
        except Exception:
            logger.exception("Failed writing VLM raw output")

        # --- 2) Build LLM prompt that includes the VLM JSON + RAW_TEXT + FIGURES ---
        await write_status(upload_id, {"upload_id": upload_id, "state": "running", "step": "build_prompt", "message": "Preparing LLM prompt", "progress": 50})

        # Prevent hitting token limits: conservatively truncate raw_text if very large (adjust threshold as needed)
        raw_text_for_prompt = raw_text  
        fig_lines = "\n".join([f"- {f['id']}: {f['path']}" for f in figures_index]) or "None"
        vlm_block = vlm_json_text if vlm_json_text else "null"

        prompt = (
    "You are given:\n"
    "  1) RAW PDF TEXT (label: RAW_TEXT).\n"
    "  2) VLM candidate slides (label: VLM_SLIDES) produced from the document images (JSON).\n"
    "  3) A figure index listing uploaded image ids and web paths (label: FIGURES).\n\n"
    "Task:\n"
    "  - First read RAW_TEXT to understand the full story.\n"
    "  - Then read VLM_SLIDES and use improve the slide content using the RAW_TEXT. to .\n"
    "  - Decide  how many more slides are required to explain the full story better and create those slides.\n"
    "  - Refine and   reorder the VLM_SLIDES to make it consistent with the below layout .\n"
    "  - Preserve image references that point to /uploads/figures/* when present. If an image reference uses a placeholder like \"<Image 1>\" that should already be resolved by the VLM step; keep it or replace it with the web path /uploads/figures/<filename>.\n"
    "  - Return EXACTLY one JSON object with a single top-level key \"slides\" (list) following this schema:\n"
    "    slides -> [{\"id\":\"slide1\",\"order\":1,\"type\":\"content|image|mixed\",\"title\":\"...\",\"subtitle\":null,\"image_ref\":null,\"notes\":\"...\",\"steps\":[{\"number\":1,\"heading\":\"...\",\"content\":\"...\"}]}]\n"
    "  - Do NOT return any text outside the JSON object. No commentary, no markdown.\n\n"
    "VLM_SLIDES:\n"
    + (vlm_block if vlm_block else "null")
    + "\n\nFIGURES:\n"
    + (fig_lines or "None")
    + "\n\nRAW_TEXT:\n"
    + (raw_text_for_prompt or "")
).strip()

        # --- 3) Call text LLM to produce final merged slides ---
        await write_status(upload_id, {"upload_id": upload_id, "state": "running", "step": "llm_call", "message": "Calling LLM to merge VLM slides with text", "progress": 65})
        try:
            thinking_llm, final_response = generate_response(prompt, hf_token)
        except Exception as e:
            logger.exception("LLM call failed")
            thinking_llm = ""
            final_response = ""

        # Persist LLM raw output for debugging
        try:
            raw_out_path = UPLOAD_DIR / f"{basename}.llm.txt"
            async with aiofiles.open(raw_out_path, "w", encoding="utf-8") as rf:
                await rf.write((thinking_llm or "") + "\n\n" + (final_response or ""))
        except Exception:
            logger.exception("Failed writing LLM raw output")

        await write_status(upload_id, {"upload_id": upload_id, "state": "running", "step": "parse_json", "message": "Parsing LLM output", "progress": 80})

        # --- 4) Robust JSON extraction/parsing ---
        data = None
        # Try to parse entire response as JSON first
        try:
            data = json.loads(final_response)
        except Exception:
            # Try to extract last JSON object or array from final_response
            m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])\s*$", final_response or "")
            if m:
                try:
                    data = json.loads(m.group(1))
                except Exception:
                    data = None

        # Fallback: use VLM JSON if valid
        if data is None:
            try:
                data = json.loads(vlm_json_text)
                logger.info("Falling back to VLM JSON for final manifest")
            except Exception:
                # As a last resort, produce a diagnostic error object
                logger.exception("Both LLM and VLM outputs failed to parse as JSON")
                await write_status(upload_id, {
                    "upload_id": upload_id,
                    "state": "error",
                    "step": "parse_failed",
                    "message": "Both LLM and VLM outputs not valid JSON",
                    "progress": 0
                })
                return

        # Ensure top-level shape contains slides list
        if not isinstance(data, dict) or "slides" not in data or not isinstance(data["slides"], list):
            # Attempt to wrap if we received an array of slides
            if isinstance(data, list):
                data = {"slides": data}
            else:
                # invalid shape -> error
                logger.exception("Parsed JSON does not contain slides list")
                await write_status(upload_id, {
                    "upload_id": upload_id,
                    "state": "error",
                    "step": "invalid_schema",
                    "message": "Parsed JSON does not follow expected schema",
                    "progress": 0
                })
                return

        # --- 5) Normalize/resolve image_ref to concrete /uploads/figures paths ---
        # Build mapping from figures_index: id -> path
        id_to_path = {f["id"]: f["path"] for f in figures_index}
        # Also create ordered web_paths in case VLM used placeholders like "<Image 1>"
        ordered_web_paths = []
        for f in figures_index:
            ordered_web_paths.append(f["path"])

        placeholder_re = re.compile(r"<\s*Image\s*(\d+)\s*>", flags=re.IGNORECASE)
        for s in data.get("slides", []):
            img = s.get("image_ref")
            if isinstance(img, str):
                # If image_ref is a figure id like "figure1" -> map via id_to_path
                if img in id_to_path:
                    s["image_ref"] = id_to_path[img]
                    continue
                # If it's a web path already, keep it (but ensure it starts with /uploads/)
                if img.startswith("/uploads/"):
                    # leave as-is
                    continue
                # If it's a placeholder <Image N>, map by order
                m = placeholder_re.search(img)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(ordered_web_paths):
                        s["image_ref"] = ordered_web_paths[idx]
                    else:
                        s["image_ref"] = None
                    continue
                # If it looks like a filename, try to map by basename
                basename_match = Path(img).name
                candidate = next((p for p in ordered_web_paths if Path(p).name == basename_match), None)
                if candidate:
                    s["image_ref"] = candidate
                else:
                    # could not resolve -> set to None
                    s["image_ref"] = None

        # --- 6) Persist final parsed JSON and write final status ---
        parsed_out_path = APP_DIR / f"final.json"
        try:
            async with aiofiles.open(parsed_out_path, "w", encoding="utf-8") as jf:
                await jf.write(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            logger.exception("Failed writing parsed JSON")

        # --- 7) Cleanup intermediate previews and text artifacts (safe to delete now) ---
        try:
            # 7a) Remove previews directory for this upload_id
            previews_subdir = PREVIEWS_DIR / upload_id
            if previews_subdir.exists() and previews_subdir.is_dir():
                try:
                    shutil.rmtree(previews_subdir)
                    logger.info("Removed previews directory %s", previews_subdir)
                except Exception:
                    logger.exception("Failed removing previews directory %s", previews_subdir)

            # 7b) Remove extracted text file if extract_pdf_to_text returned a path
            if text_path:
                try:
                    tp = Path(text_path)
                    if tp.exists() and tp.is_file():
                        tp.unlink()
                        logger.info("Removed extracted text file %s", tp)
                except Exception:
                    logger.exception("Failed removing extracted text file %s", text_path)

            # 7c) Remove intermediate raw VLM and LLM text outputs but KEEP final manifest
            if vlm_raw_out_path and vlm_raw_out_path.exists():
                try:
                    vlm_raw_out_path.unlink()
                    logger.info("Removed VLM raw output %s", vlm_raw_out_path)
                except Exception:
                    logger.exception("Failed removing VLM raw output %s", vlm_raw_out_path)
            if raw_out_path and raw_out_path.exists():
                try:
                    raw_out_path.unlink()
                    logger.info("Removed LLM raw output %s", raw_out_path)
                except Exception:
                    logger.exception("Failed removing LLM raw output %s", raw_out_path)
        except Exception:
            logger.exception("Cleanup phase encountered an error")

        # Write final status (manifest remains available)
        await write_status(upload_id, {
            "upload_id": upload_id,
            "state": "done",
            "step": "finished",
            "message": "Processing complete",
            "progress": 100,
            "manifest_url": f"/uploads/{parsed_out_path.name}"
        })

    except Exception as err:
        logger.exception("Processing failed for %s: %s", upload_id, err)
        await write_status(upload_id, {
            "upload_id": upload_id,
            "state": "error",
            "step": "failed",
            "message": str(err),
            "progress": 0
        })


# ----------------------------
# HTTP endpoints
# ----------------------------
FRONTEND_FILE = APP_DIR / "deck.html"  # <-- your file name

@app.get("/", response_class=HTMLResponse)
async def index():
    if not FRONTEND_FILE.exists():
        return HTMLResponse("<h1>Frontend file not found</h1>", status_code=404)
    return FileResponse(str(FRONTEND_FILE))


@app.get("/viewer", response_class=HTMLResponse)
async def viewer(request: Request):
    """Serve the viewer HTML (viewer.html)."""
    v = APP_DIR / "viewer.html"
    if not v.exists():
        return HTMLResponse("<h1>viewer.html not found</h1>", status_code=404)
    return FileResponse(str(v))


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """
    Accept PDF uploads:
      - reject non-PDFs,
      - save file to uploads/<uuid>_<filename>,
      - attempt to render previews via pdftoppm into a temp dir,
      - move previews into uploads/previews/<upload_id>/,
      - return upload_id, status url and previews index.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    upload_id = uuid.uuid4().hex
    basename = Path(file.filename).stem
    save_name = f"{upload_id}_{file.filename}"
    save_path = UPLOAD_DIR / save_name

    # Save upload to disk (streaming)
    try:
        await save_upload_to_disk(save_path, file)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed saving upload: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save upload")

    # Initial status file (uploaded)
    await write_status(upload_id, {
        "upload_id": upload_id,
        "state": "uploaded",
        "step": "uploaded",
        "message": "File received. Create selections to start processing.",
        "progress": 5,
        "pdf_filename": save_name
    })

    # Render previews with pdftoppm into a temporary directory, then collect them
    tmpdir = Path(tempfile.mkdtemp(prefix="pdfpreview_"))
    try:
        preview_paths = pdftoppm_render(save_path, tmpdir)
        previews_index = collect_and_rename_previews(preview_paths, upload_id) if preview_paths else []
    finally:
        # Clean up temporary directory
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    app.mount("/", StaticFiles(directory=str(APP_DIR), html=True), name="root")
    return JSONResponse(status_code=201, content={
        "upload_id": upload_id,
        "status_url": f"/status/{upload_id}",
        "pdf_filename": save_name,
        "previews": previews_index,
        "message": "Upload success. Use preview images to select bounding boxes and POST them to /process/{upload_id}"
    })


@app.post("/process/{upload_id}")
async def start_processing(upload_id: str, payload: Dict[str, Any]):
    """
    Kick off processing for an uploaded PDF.

    Expected payload:
    {
      "pdf_filename": "<stored filename in uploads>",
      "selections": [
         {"page": 1, "x":..., "y":..., "w":..., "h":..., "type":"figure|table", "label": "optional"},
         ...
      ],
      "use_extractor": false  # optional
    }

    Behavior:
      - Create cropped figures from the supplied selections (saved to uploads/figures/),
      - Optionally run external extractor and merge results (renaming to avoid id collisions),
      - Write a queued status file,
      - Start the background processing task (asyncio.create_task) using the collected figures index.
    """
    try:
        pdf_filename = payload.get("pdf_filename")
        if not pdf_filename:
            raise HTTPException(status_code=400, detail="Missing pdf_filename")

        save_path = UPLOAD_DIR / pdf_filename
        if not save_path.exists():
            raise HTTPException(status_code=404, detail="Uploaded PDF not found")

        selections = payload.get("selections", [])
        if not isinstance(selections, list):
            raise HTTPException(status_code=400, detail="Invalid selections format")

        # Build figures_index from selections by cropping preview images
        figures_index: List[Dict[str, Any]] = []
        previews_dir = PREVIEWS_DIR / upload_id
        fig_counter = 1
        for sel in selections:
            page = int(sel.get("page", 0))
            if page <= 0:
                continue
            preview_candidate = previews_dir / f"page{page}.png"
            if not preview_candidate.exists():
                logger.warning("Preview for page %s not found (%s)", page, preview_candidate)
                continue
            out_name = f"{upload_id}_figure{fig_counter}.png"
            out_path = FIGURES_DIR / out_name
            ok = crop_and_save_selection(preview_candidate, sel, out_path)
            if not ok:
                continue
            figures_index.append({"id": f"figure{fig_counter}", "path": f"/uploads/figures/{out_name}"})
            fig_counter += 1

        # Optionally run external extractor and merge its images into figures_index,
        # renaming to avoid ID collisions and copying/moving files into uploads/figures/
         
        # Write queued status and then start background processing
        await write_status(upload_id, {
            "upload_id": upload_id,
            "state": "queued",
            "step": "processing",
            "message": "Processing started",
            "progress": 10,
            "figures": figures_index
        })

        basename = Path(pdf_filename).stem
        # Launch background task (non-blocking)
        asyncio.create_task(process_upload_background(upload_id, save_path, basename, figures_index))

        return JSONResponse(status_code=202, content={"upload_id": upload_id, "status_url": f"/status/{upload_id}"})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to start processing: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{upload_id}")
async def status(upload_id: str):
    """
    Return the status JSON for an upload. If the file content cannot be parsed as JSON,
    return it under {"raw": "<text>"} so the frontend can still inspect it.
    """
    status_path = UPLOAD_DIR / f"{upload_id}.status.json"
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Status not found")
    async with aiofiles.open(status_path, "r", encoding="utf-8") as f:
        txt = await f.read()
    try:
        return JSONResponse(status_code=200, content=json.loads(txt))
    except Exception:
        # Fallback: deliver the raw text when parsing fails
        return JSONResponse(status_code=200, content={"raw": txt})


@app.get("/uploads/list/{upload_id}")
async def list_upload_artifacts(upload_id: str):
    """
    Convenience endpoint to list files related to an upload for debugging.
    Returns: {"files": [...], "previews": [...], "figures": [...]}
    """
    files = [p.name for p in UPLOAD_DIR.glob(f"{upload_id}*")]
    previews_subdir = PREVIEWS_DIR / upload_id
    previews = [p.name for p in previews_subdir.glob("*")] if previews_subdir.exists() else []
    figures = [p.name for p in FIGURES_DIR.glob(f"{upload_id}*")]
    return JSONResponse(status_code=200, content={"files": files, "previews": previews, "figures": figures})