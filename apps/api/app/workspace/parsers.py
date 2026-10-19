"""Text-first parsing. Preserve locations; do not pretend scanned PDFs were read."""
from io import BytesIO
from zipfile import ZipFile, BadZipFile
import re

MAX_TEXT = 200000
ALLOWED = {".txt", ".md", ".docx", ".pdf"}


class ParseError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_file(data: bytes, suffix: str) -> list[dict]:
    if suffix not in ALLOWED:
        raise ParseError("unsupported_type")
    try:
        if suffix in {".txt", ".md"}:
            text = data.decode("utf-8-sig")
            pieces = [{"location": f"段落 {i}", "text": p.strip()}
                      for i, p in enumerate(re.split(r"\n\s*\n", text), 1) if p.strip()]
        elif suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise ParseError("encrypted_pdf")
            if len(reader.pages) > 200:
                raise ParseError("too_many_pages")
            pieces = []
            for i, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if not text:
                    # Partial text-only extraction is not a complete parse.
                    raise ParseError("ocr_required")
                pieces.append({"location": f"第 {i} 页", "text": text})
        else:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            with ZipFile(BytesIO(data)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 40 * 1024 * 1024:
                    raise ParseError("expanded_file_too_large")
            doc = Document(BytesIO(data))
            pieces = []
            # Walk body in original order, retaining table cells as a row.
            for i, child in enumerate(doc.element.body.iterchildren(), 1):
                if child.tag.endswith("}p"):
                    text = Paragraph(child, doc).text.strip()
                    if text:
                        pieces.append({"location": f"正文段落 {i}", "text": text})
                elif child.tag.endswith("}tbl"):
                    for j, row in enumerate(Table(child, doc).rows, 1):
                        text = " | ".join(c.text.strip().replace("\n", " ") for c in row.cells)
                        if text.strip(" |"):
                            pieces.append({"location": f"表格 {i} 第 {j} 行", "text": text})
    except ParseError:
        raise
    except UnicodeDecodeError:
        raise ParseError("utf8_required") from None
    except (BadZipFile, Exception):
        raise ParseError("parse_failed") from None
    if not pieces:
        raise ParseError("empty_document")
    if len(pieces) > 200 or sum(len(p["text"]) for p in pieces) > MAX_TEXT:
        raise ParseError("document_too_large")
    if any(len(p["text"]) > 60000 for p in pieces):
        raise ParseError("piece_too_large")
    return pieces


def split_pieces(pieces, size=1100, overlap=120):
    """Bound chunks without crossing page/table-row locations."""
    chunks = []
    for piece in pieces:
        text = piece["text"].strip()
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                # Prefer sentence/line boundaries in the second half.
                cut = max(text.rfind(s, start + size // 2, end) for s in ("\n", "。", "；"))
                if cut > start:
                    end = cut + 1
            chunks.append({"text": text[start:end], "location": piece["location"], "offset": start})
            if end == len(text):
                break
            start = max(start + 1, end - overlap)
    if len(chunks) > 300:
        raise ParseError("too_many_chunks")
    return chunks
