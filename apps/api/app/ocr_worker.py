"""Optional PaddleOCR 3.x worker. Raw OCR never bypasses user confirmation."""
import json
import sys
from pathlib import Path

def main():
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(ocr_version="PP-OCRv5", device="cpu", lang="ch",
        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)
    texts = []
    for result in ocr.predict(sys.argv[1]):
        texts.extend(result["rec_texts"])
    Path(sys.argv[2]).write_text(json.dumps({"text": "\n".join(texts)}, ensure_ascii=False), encoding="utf-8")

if __name__ == "__main__":
    main()
