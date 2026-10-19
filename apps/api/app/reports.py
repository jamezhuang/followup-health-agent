import asyncio
import csv
import io
import json
import re
import sys
import tempfile
from pathlib import Path
from uuid import uuid4
from pypdf import PdfReader
from PIL import Image
from pydantic import ValidationError
from .schemas import ReportView, ReportRow
from .storage import now
from .providers import ProviderError

NOTICE = "请对照原报告核对文字、项目、数值、单位和参考区间，删除错配项；确认前不会进入分析。此版本不自动诊断。"

def extract_rows(text):
    rows = []
    aliases = {"项目": "name", "名称": "name", "name": "name", "结果": "value", "数值": "value", "value": "value", "单位": "unit", "unit": "unit", "参考区间": "reference", "参考范围": "reference", "reference": "reference"}
    lines = text.splitlines()
    # Explicit four-column CSV/TSV. Unknown layouts stay as text, never invent rows.
    for delimiter in (",", "\t", "，"):
        parsed = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if not parsed:
            continue
        fields = [aliases.get(h.strip()) for h in parsed[0]]
        if "name" not in fields or "value" not in fields:
            continue
        for i, line in enumerate(parsed[1:81], 2):
            data = {field: value.strip() for field, value in zip(fields, line) if field}
            if data.get("name") and data.get("value"):
                try:
                    rows.append(ReportRow(**data, location=f"原文第{i}行"))
                except ValidationError:
                    continue
        return rows
    # Conservative single-line format: name value unit reference. Ambiguity -> no row.
    for i, line in enumerate(lines, 1):
        parts = re.split(r"\s{2,}|\t", line.strip())
        if len(parts) == 4 and re.fullmatch(r"[<>≤≥]?\d+(?:\.\d+)?", parts[1]) and all(len(x) <= 80 for x in parts):
            rows.append(ReportRow(name=parts[0], value=parts[1], unit=parts[2], reference=parts[3], location=f"原文第{i}行"))
        if len(rows) >= 80:
            break
    return rows

def make_report(session_id, name, text):
    if not text.strip():
        raise ProviderError("report_empty", "未提取到文字，请粘贴内容或启用图片OCR。")
    if len(text) > 18000:
        raise ProviderError("report_too_long", "报告文字过长，请拆分为较小的报告。")
    return ReportView(id=str(uuid4()), session_id=session_id, name=Path(name).name[:120] or "报告",
        original_text=text, rows=extract_rows(text), created_at=now(), notice=NOTICE)

def extract_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 5:
            raise ValueError("pages")
        return "\n".join(f"[第{i}页]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages, 1))
    except Exception:
        raise ProviderError("report_pdf", "请上传未加密且不超过5页的PDF；扫描PDF请转为图片或粘贴原文。") from None

def check_image(data):
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in {"PNG", "JPEG"} or img.width*img.height > 12000000:
                raise ValueError("image")
            img.verify()
    except Exception:
        raise ProviderError("report_image", "图片需为有效的PNG/JPEG，且不超过1200万像素。") from None

async def extract_upload(data, name, ocr_enabled):
    if not data or len(data) > 8*1024*1024:
        raise ProviderError("report_size", "报告需小于8MB且不能为空。")
    suffix = Path(name).suffix.lower()
    if suffix in {".txt", ".csv", ".tsv"}:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ProviderError("report_encoding", "文本需为UTF-8编码；也可以直接粘贴报告。") from None
    if suffix == ".pdf":
        text = await asyncio.to_thread(extract_pdf, data)
        if not re.sub(r"\[第\d+页\]|\s", "", text):
            raise ProviderError("report_scanned", "这是扫描PDF，当前未提取到文字。请转为PNG/JPEG并启用OCR，或手动粘贴。")
        return text
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ProviderError("report_format", "支持TXT、CSV、TSV、文字PDF及可选PNG/JPEG OCR。")
    await asyncio.to_thread(check_image, data)
    if not ocr_enabled:
        raise ProviderError("ocr_disabled", "尚未启用图片OCR。请按README安装OCR依赖，或先粘贴报告内容。")
    # Heavy inference is isolated; killing it on timeout does not keep a worker thread alive.
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory)/("report"+suffix), Path(directory)/"result.json"
        source.write_bytes(data)
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.ocr_worker", str(source), str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout=120)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill(); await process.wait()
            raise ProviderError("ocr_timeout", "OCR未在时限内完成；首次模型下载可能较慢，请先预热OCR。") from None
        if process.returncode != 0 or not output.exists():
            raise ProviderError("ocr_failed", "图片OCR失败，请检查可选依赖与模型下载，或手动粘贴。")
        text = json.loads(output.read_text(encoding="utf-8"))["text"]
        return text

def describe_report(report):
    rows = report.rows
    if not rows:
        return "已记录你确认的报告文字。当前排版未生成可核对的指标表，可以回到报告页手动添加项目。尚不能据此给出医学结论；就诊时请携带原报告。"
    fields = [f"{r.name}：{r.value}{(' '+r.unit) if r.unit else ''}（报告参考区间：{r.reference or '未提供'}）" for r in rows[:8]]
    return "已记录你确认的报告字段：\n" + "\n".join(fields) + "\n这些是报告内容整理，尚不能单独说明病因。进一步评估还需报告日期、检查背景、当前症状及医生意见。"
