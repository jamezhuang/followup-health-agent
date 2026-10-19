import asyncio
import json
from datetime import datetime, timezone

class EventLog:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()

    async def write(self, run_id: str, stage: str, **details):
        # Only allow sanitized metadata. No user text, raw provider data or keys.
        event = {"time": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                 "stage": stage, **{k: v for k, v in details.items()
                    if k in {'authority', 'cached', 'candidate_count', 'document_type', 'duration_ms', 'error_code', 'http_status', 'mode', 'outcome', 'provider_attempt_count', 'reader', 'repairs', 'source_count', 'status', 'step', 'tool', 'tool_call_count', 'tool_call_id', 'used_calls'}}}
        async with self.lock:
            await asyncio.to_thread(self._append, event)

    def _append(self, event):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read(self, run_id):
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [row for line in handle if (row := json.loads(line))["run_id"] == run_id]
