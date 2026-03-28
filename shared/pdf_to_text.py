from pathlib import Path
from PyPDF2 import PdfReader
from typing import Tuple

def extract_pdf_to_text(pdf_path: str) -> Tuple[Path, str]:
    """
    Extract text from a PDF file and save it into a 'texts' directory,
    including page number information as one single string.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Invalid PDF path provided.")

    output_dir = pdf_path.parent / "texts"
    output_dir.mkdir(exist_ok=True)

    output_file = output_dir / f"{pdf_path.stem}.txt"

    reader = PdfReader(pdf_path)

    full_text = ""

    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        full_text += (
            f"\n{'='*40}\n"
            f"Page {page_number}\n"
            f"{'='*40}\n"
            f"{text}\n"
        )

    output_file.write_text(full_text, encoding="utf-8")

    return output_file, full_text