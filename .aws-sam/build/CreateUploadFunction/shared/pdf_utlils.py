import subprocess
from pathlib import Path
from typing import Dict, List

def extract_text_from_pdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("pypdf is required for text extraction") from exc

    reader = PdfReader(str(pdf_path))
    parts: List[str] = []

    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text.strip())

    return "\n\n".join(parts).strip()


def render_pdf_previews(pdf_path: Path, out_dir: Path, dpi: int = 150) -> List[Path]:
    """
    Render pages to PNG files.
    Tries PyMuPDF first, then falls back to pdftoppm.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        generated: List[Path] = []
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)

        for idx, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out_path = out_dir / f"page{idx}.png"
            pix.save(str(out_path))
            generated.append(out_path)

        if generated:
            return generated
    except Exception:
        pass

    cmd = [
        "pdftoppm",
        "-png",
        "-rx",
        str(dpi),
        "-ry",
        str(dpi),
        str(pdf_path),
        str(out_dir / "page"),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    generated = sorted(out_dir.glob("page-*.png"))
    if not generated:
        generated = sorted(out_dir.glob("*.png"))
    return generated


def crop_bbox(image_path: Path, bbox: Dict[str, int], output_path: Path) -> None:
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required for cropping figures") from exc

    with Image.open(image_path) as im:
        left = int(bbox["x"])
        top = int(bbox["y"])
        right = left + int(bbox["w"])
        bottom = top + int(bbox["h"])
        cropped = im.crop((left, top, right, bottom))
        cropped.save(output_path)