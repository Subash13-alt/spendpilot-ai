"""Receipt upload validation, hashing and text extraction (OCR for images, text for PDF/TXT)."""
import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from typing import Optional

ALLOWED = {"png", "jpg", "jpeg", "pdf", "txt"}
# Magic bytes: the file content must match its extension, not just the filename.
SIGNATURES = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "pdf": b"%PDF-"}


class ReceiptError(Exception):
    pass


@dataclass
class StoredReceipt:
    path: str
    sha256: str
    ext: str
    size: int


def validate_and_store(file_storage, upload_folder: str, max_bytes: int = 5 * 1024 * 1024,
                       allowed=ALLOWED) -> StoredReceipt:
    """Validate type/size/content, then save under a random name (the client filename is never used)."""
    if file_storage is None or not getattr(file_storage, "filename", ""):
        raise ReceiptError("No receipt file provided")
    name = file_storage.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in allowed:
        raise ReceiptError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(allowed))}")
    data = file_storage.read(max_bytes + 1)
    if not data:
        raise ReceiptError("The uploaded file is empty")
    if len(data) > max_bytes:
        raise ReceiptError(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit")
    sig = SIGNATURES.get(ext)
    if sig and not data.startswith(sig):
        raise ReceiptError("File content does not match its extension")
    if ext == "txt":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ReceiptError("Text receipts must be UTF-8")
    os.makedirs(upload_folder, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(upload_folder, stored)
    with open(path, "wb") as fh:
        fh.write(data)
    return StoredReceipt(path=path, sha256=hashlib.sha256(data).hexdigest(), ext=ext, size=len(data))


def extract_text(path: str, ext: str, tesseract_cmd: Optional[str] = None) -> dict:
    """Return {text, method, warning}. Never raises: corrupted input yields empty text plus a warning."""
    try:
        if ext == "txt":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return {"text": fh.read(), "method": "plain-text", "warning": None}
        if ext == "pdf":
            return _pdf_text(path, tesseract_cmd)
        return _ocr_image_file(path, tesseract_cmd)
    except Exception as exc:  # corrupted/unreadable receipt must not crash the workflow
        return {"text": "", "method": "failed", "warning": f"Could not read receipt: {exc}"}


def _ocr_image(img, tesseract_cmd):
    import pytesseract
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    return pytesseract.image_to_string(img)


def _ocr_image_file(path, tesseract_cmd):
    from PIL import Image
    try:
        with Image.open(path) as img:
            img.load()
            text = _ocr_image(img.convert("L"), tesseract_cmd)
    except ImportError:
        return {"text": "", "method": "unavailable", "warning": "OCR libraries not installed"}
    except Exception as exc:
        name = type(exc).__name__
        if name == "TesseractNotFoundError":
            return {"text": "", "method": "unavailable",
                    "warning": "Tesseract OCR is not installed or not on PATH - enter details manually"}
        raise
    warning = None if text.strip() else "OCR found no readable text - enter details manually"
    return {"text": text, "method": "tesseract-ocr", "warning": warning}


def _pdf_text(path, tesseract_cmd):
    from pypdf import PdfReader
    reader = PdfReader(path)
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:5])
    if text.strip():
        return {"text": text, "method": "pdf-text", "warning": None}
    # scanned PDF: try embedded images through OCR
    chunks = []
    from PIL import Image
    for page in reader.pages[:3]:
        for im in getattr(page, "images", []):
            try:
                chunks.append(_ocr_image(Image.open(io.BytesIO(im.data)).convert("L"), tesseract_cmd))
            except Exception:
                continue
    text = "\n".join(chunks)
    warning = None if text.strip() else "PDF has no extractable text - enter details manually"
    return {"text": text, "method": "pdf-ocr" if text.strip() else "failed", "warning": warning}
