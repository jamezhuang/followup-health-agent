"""HTML 清洗、相关段落选择和隔离进程 PDF 解析。"""

import asyncio
import io
import json
import re
import subprocess
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .web_access import AccessError, clean_url


def parse_html(download):
    soup = BeautifulSoup(download.content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else "官方网页"
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        try:
            url = clean_url(urljoin(download.url, anchor["href"]))
        except AccessError:
            continue
        if url not in seen:
            seen.add(url)
            links.append({"url": url, "title": anchor.get_text(" ", strip=True)[:180]})
        if len(links) >= 300:
            break
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    block = soup.find("article") or soup.find("main") or soup.body or soup
    text = block.get_text("\n", strip=True)[:80_000]
    if len(text) < 40:
        raise AccessError("text_too_short")
    return {"url": download.url, "title": title[:180], "text": text,
            "links": links, "document_type": "html"}


def excerpt(text, query, limit=7000):
    if len(text) <= limit:
        return text
    # 每块保持原文连续；长表格也拆成连续字符块。
    blocks = [text[i:i + 700] for i in range(0, len(text), 700)]
    terms = set(re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", query or ""))
    for term in list(terms):
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            terms.update(term[i:i + 2] for i in range(len(term) - 1))
    ranked = sorted(range(len(blocks)), key=lambda i: (-sum(term in blocks[i] for term in terms), i))
    selected = sorted(ranked[:max(1, limit // 710)])
    # 显式段落边界；不得将多个块拼成一个连续引用。
    return "\n\n[原文片段边界]\n\n".join(blocks[i] for i in selected)[:limit]


def directory_excerpt(text, query, limit=7000):
    """Prefer actual name/address entries over doctors, menus and related links.

    Keep each selected range verbatim. Unstructured official lists still use the
    normal excerpt reader; this is independent of a city or a website's CSS.
    """
    lines=list(re.finditer(r'[^\n]+',text))
    starts=[]
    for index,line in enumerate(lines[:-1]):
        name=line.group().strip()
        if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9·（）()\s]{2,80}(?:医院|卫生院|医学中心|医疗中心|眼科中心|妇幼保健院)(?:[（(][^）)]{1,20}[）)])?',name):
            continue
        address=lines[index+1].group().strip()
        if not re.search(r'(?:地址|院址)[：:]|[路街道巷].{0,20}[号段]',address):
            continue
        starts.append(line.start())
    if len(starts)<2:
        return excerpt(text,query,limit)
    entries=[]
    for index,start in enumerate(starts):
        end=starts[index+1] if index+1<len(starts) else len(text)
        section=text[start:min(end,start+1500)]
        boundary=re.search(r'(?m)^(?:专家团队|医生推荐|热门城市|点击加载更多|最新资讯|相关医院|热门推荐)\s*$',section)
        entries.append(section[:boundary.start()].rstrip() if boundary else section.rstrip())
    return '\n\n[原文片段边界]\n\n'.join(entries)[:limit]


async def parse_pdf(download):
    # Windows Uvicorn reload环境可能使用Selector循环；普通Popen不依赖异步subprocess支持。
    process = subprocess.Popen(
        [sys.executable, "-m", "app.web_documents", "--pdf-worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.to_thread(process.communicate, download.content, timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.communicate)
        raise AccessError("pdf_timeout", retryable=True) from None
    except BaseException:
        if process.poll() is None:
            process.kill()
        await asyncio.to_thread(process.wait)
        raise
    if process.returncode != 0:
        raise AccessError("pdf_parse_failed")
    data = json.loads(output)
    if not data.get("text"):
        raise AccessError("pdf_text_empty")
    return {"url": download.url, "title": "官方 PDF 附件", "text": data["text"],
            "links": [], "document_type": "pdf", "pages_scanned": data["pages_scanned"]}


def pdf_worker():
    from pypdf import PdfReader
    try:
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
        raw = sys.stdin.buffer.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("size")
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted")
        rows = []
        for number, page in enumerate(reader.pages[:40], 1):
            text = (page.extract_text() or "").strip()
            if text:
                rows.append(f"[PDF第{number}页]\n{text[:12000]}")
            if sum(len(row) for row in rows) >= 80_000:
                break
        sys.stdout.buffer.write(json.dumps({"text": "\n\n".join(rows)[:80_000],
                                          "pages_scanned": min(len(reader.pages), 40)}, ensure_ascii=False).encode())
    except Exception:
        raise SystemExit(2)


if __name__ == "__main__" and "--pdf-worker" in sys.argv:
    pdf_worker()
