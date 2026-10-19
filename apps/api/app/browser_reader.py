"""Crawl4AI isolated worker client. No Crawl4AI import in the API environment."""
import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .web_access import AccessError, checked_addresses, clean_url

_BROWSER_SLOT = threading.BoundedSemaphore(1)
API_ROOT = Path(__file__).resolve().parents[1]


def enabled():
    return os.getenv("CRAWL4AI_ENABLED", "false").lower() in {"true", "1", "yes"}


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


class Crawl4AIReader:
    def __init__(self, policy, *, runner=None):
        self.policy, self.runner = policy, runner
        self.enabled = enabled()
        self.calls = 0
        try:
            self.max_reads = int(os.getenv("WEB_BROWSER_MAX_READS", "5"))
        except ValueError:
            raise AccessError("browser_config_invalid") from None
        if not 1 <= self.max_reads <= 10:
            raise AccessError("browser_config_invalid")

    async def fetch(self, url):
        if not self.enabled:
            raise AccessError("browser_disabled")
        url = clean_url(url)
        if not self.policy.may_read(url):
            raise AccessError("source_unverified")
        await checked_addresses(url)
        if self.calls >= self.max_reads:
            raise AccessError("browser_budget_exceeded")
        self.calls += 1
        started = time.monotonic()
        if self.runner is None:
            page = await self._worker(url)
        else:
            page = await self.runner(url)
        final_url = clean_url(page["url"])
        if not self.policy.may_read(final_url):
            raise AccessError("source_unverified")
        await checked_addresses(final_url)
        if not isinstance(page.get("text"), str) or len(page["text"]) < 40:
            raise AccessError("text_too_short")
        page.update(url=final_url, reader="crawl4ai", seconds=round(time.monotonic() - started, 2))
        return page

    async def _worker(self, url):
        browser = os.getenv("CRAWL4AI_BROWSER", "msedge").lower()
        if browser not in {"msedge", "chromium", "chrome"}:
            raise AccessError("browser_config_invalid")
        default_python = API_ROOT / ".venv-crawl" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python = Path(os.getenv("CRAWL4AI_PYTHON") or str(default_python))
        if not python.is_absolute():
            python = API_ROOT / python
        if not python.is_file():
            raise AccessError("browser_python_missing")
        worker = API_ROOT / "app" / "crawl4ai_worker.py"
        if not worker.is_file():
            raise AccessError("browser_worker_missing")
        if not _BROWSER_SLOT.acquire(blocking=False):
            raise AccessError("browser_busy", retryable=True)
        process = None
        task = None
        try:
            payload = json.dumps({"url": url, "browser": browser,
                                  "http_hosts": os.getenv("WEB_HTTP_ALLOWED_HOSTS", "")}).encode("utf-8")
            # The browser worker does not receive model/search API keys.
            keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
                    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "HOMEDRIVE",
                    "LOCALAPPDATA", "APPDATA", "PLAYWRIGHT_BROWSERS_PATH", "LANG", "LD_LIBRARY_PATH"}
            env = {key: value for key, value in os.environ.items() if key.upper() in keep}
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            process = subprocess.Popen([str(python), str(worker)], cwd=API_ROOT, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       start_new_session=os.name != "nt")
            task = asyncio.create_task(asyncio.to_thread(process.communicate, payload, timeout=45))
            try:
                output, _ = await asyncio.shield(task)
            except subprocess.TimeoutExpired:
                await asyncio.to_thread(terminate_tree, process)
                await asyncio.to_thread(process.communicate)
                raise AccessError("browser_timeout", retryable=True) from None
            if len(output) > 4_000_000:
                raise AccessError("browser_output_too_large")
            try:
                result = json.loads(output.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise AccessError("browser_output_invalid") from None
            if not result.get("ok"):
                raise AccessError(result.get("error", "browser_failed"),
                                  retryable=bool(result.get("retryable")), status=result.get("http_status"))
            if process.returncode != 0:
                raise AccessError("browser_worker_failed")
            return result["page"]
        except BaseException:
            if process is not None and process.poll() is None:
                await asyncio.to_thread(terminate_tree, process)
            if task is not None and not task.done():
                try:
                    await asyncio.shield(task)
                except (Exception, asyncio.CancelledError):
                    pass
            raise
        finally:
            _BROWSER_SLOT.release()
