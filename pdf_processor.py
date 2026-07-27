"""
PDF text extraction for the content vault.

Text-based PDFs only in v1 — scanned/image PDFs return a clear error
(no OCR). Output matches the transcription JSON shape used by VideoJob
so search/Ask can reuse the same indexing path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def extract_pdf_content(file_path: str) -> dict[str, Any]:
    """
    Extract per-page text from a PDF.

    Returns a dict with ``metadata``, ``transcription``, and
    ``processing_errors`` (same top-level shape as media summaries).
    """
    path = Path(file_path)
    if not path.exists():
        return {
            "metadata": None,
            "transcription": None,
            "processing_errors": [f"PDF file does not exist: {file_path}"],
        }

    try:
        from pypdf import PdfReader
    except ImportError:
        return {
            "metadata": None,
            "transcription": None,
            "processing_errors": [
                "PDF support requires the pypdf package. Install it and retry."
            ],
        }

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        logger.exception("Failed to open PDF %s", path)
        return {
            "metadata": None,
            "transcription": None,
            "processing_errors": [f"Could not open PDF: {exc}"],
        }

    page_count = len(reader.pages)
    segments: list[dict[str, Any]] = []
    page_texts: list[str] = []

    for index, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as exc:
            logger.warning("Failed to extract text from page %s of %s: %s", index, path, exc)
            text = ""
        if not text:
            continue
        page_texts.append(text)
        segments.append(
            {
                "page": index,
                "text": text,
                "start": 0,
                "end": 0,
            }
        )

    full_text = "\n\n".join(page_texts).strip()
    metadata = {
        "page_count": page_count,
        "file_size_bytes": path.stat().st_size,
        "format_extension": "pdf",
        "duration_seconds": 0,
        "width_pixels": 0,
        "height_pixels": 0,
    }

    if not full_text:
        return {
            "metadata": metadata,
            "transcription": None,
            "processing_errors": [
                "No extractable text found. Scanned or image-only PDFs are not "
                "supported without OCR."
            ],
        }

    return {
        "metadata": metadata,
        "transcription": {
            "text": full_text,
            "text_segments": segments,
            "page_count": page_count,
            "word_count": len(full_text.split()),
            "source": "pdf",
            "language": "unknown",
        },
        "processing_errors": [],
    }
