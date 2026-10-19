"""Local diagnostics using the same isolated environment as browser_reader."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv

CHILD = r'''
import asyncio, contextlib, io, json, sys, traceback
from importlib.metadata import PackageNotFoundError, version
try:
    for name in ("crawl4ai", "playwright"):
        try:
            print(name + ": " + version(name), flush=True)
        except PackageNotFoundError:
            print(name + ": NOT_INSTALLED", flush=True)
    from app.crawl4ai_worker import crawl
    from crawl4ai import AsyncWebCrawler
    original_arun = AsyncWebCrawler.arun
    async def traced_arun(self, *args, **kwargs):
        result = await original_arun(self, *args, **kwargs)
        if not result.success:
            print("CRAWLER_ERROR: " + str(result.error_message)[:6000], file=sys.stderr, flush=True)
        return result
    AsyncWebCrawler.arun = traced_arun
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    with contextlib.redirect_stdout(io.StringIO()):
        result = asyncio.run(crawl(payload))
    page = result.get("page", {})
    print(json.dumps({"ok": result.get("ok"), "reader": page.get("reader"),
                      "text_chars": len(page.get("text", "")),
                      "browser_diagnostics": page.get("browser_diagnostics", {})},
                     ensure_ascii=False), flush=True)
except Exception:
    traceback.print_exc()
    sys.exit(1)
'''


def diagnose(root, url):
    load_dotenv(root / ".env", override=False)
    default = root / ".venv-crawl" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = Path(os.getenv("CRAWL4AI_PYTHON", str(default)))
    if not python.is_absolute():
        python = root / python
    worker = root / "app" / "crawl4ai_worker.py"
    if not python.is_file() or not worker.is_file():
        print("Missing Python or worker:", python, worker)
        return 1
    raw = worker.read_bytes()
    source = raw.decode("utf-8-sig")
    print("Worker:", worker)
    print("Worker SHA256:", hashlib.sha256(raw).hexdigest())
    print("Budget fix present:", '"resource_budget_exhausted"' in source)
    print("Child Python:", python, flush=True)
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "HOMEDRIVE",
            "LOCALAPPDATA", "APPDATA", "PLAYWRIGHT_BROWSERS_PATH", "LANG", "LD_LIBRARY_PATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    payload = json.dumps({"url": url, "browser": os.getenv("CRAWL4AI_BROWSER", "msedge").lower(),
                          "http_hosts": os.getenv("WEB_HTTP_ALLOWED_HOSTS", "")}).encode("utf-8")
    process = subprocess.Popen([str(python), "-c", CHILD], cwd=root, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name != "nt")
    try:
        output, errors = process.communicate(payload, timeout=45)
    except BaseException as error:
        from app.browser_reader import terminate_tree
        terminate_tree(process)
        output, errors = process.communicate()
        print("Child stopped:", type(error).__name__)
        print(output.decode("utf-8", errors="replace"))
        print(errors.decode("utf-8", errors="replace"))
        if isinstance(error, subprocess.TimeoutExpired):
            return 1
        raise
    print(output.decode("utf-8", errors="replace"))
    if errors:
        print("CHILD STDERR / TRACEBACK:")
        print(errors.decode("utf-8", errors="replace"))
    print("Child exit code:", process.returncode)
    return process.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://pnhqyy.com/ksdh/info_36.aspx?itemid=80&lcid=5")
    args = parser.parse_args()
    sys.exit(diagnose(Path(__file__).resolve().parents[1], args.url))
