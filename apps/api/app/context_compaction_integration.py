"""The Agent.run hook. All graph nodes get one bounded context snapshot."""
import asyncio
import logging
import os
import time
from pathlib import Path
from .context_compaction import Config, SummaryStore, compact

log = logging.getLogger(__name__)


class PrefixHistory:
    """A bounded view without copying the entire history list."""
    def __init__(self, history, end):
        self.history, self.end = history, end

    def __len__(self):
        return self.end

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.end)
            return [self.history[i] for i in range(start, stop, step)]
        if index < 0:
            index += self.end
        if not 0 <= index < self.end:
            raise IndexError(index)
        return self.history[index]



def request_key(explicit=None):
    value = explicit
    if value is None:
        try:
            from .workspace.tools import identity
            value = identity.get()
        except (ImportError, AttributeError):
            value = None
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 2 or not all(isinstance(x, str) and 0 < len(x) <= 256 for x in value):
        raise ValueError("context_key must be (owner_id, session_id), taken from authorized backend state")
    return tuple(value)


async def prepare_context(agent, history, facts, run_id, context_key=None, current_message=None):
    cfg = Config.from_env()
    # History may already contain the current user input. Agent supplies this
    # input separately to graph nodes; remove only the exact matching final row.
    deduplicated = False
    if history and current_message is not None:
        last = history[-1] if isinstance(history[-1], dict) else history[-1].model_dump()
        if last.get("role") == "user" and last.get("content") == current_message:
            history = PrefixHistory(history, len(history) - 1)
            deduplicated = True
    if not cfg.enabled:
        from .context import build_history
        return build_history(history, max_turns=agent.settings.history_max_turns,
                             max_chars=agent.settings.history_max_chars)
    key = request_key(context_key)
    path = Path(os.getenv("CONTEXT_SUMMARY_DB", str(Path(__file__).resolve().parents[1] / ".cache" / "context_summaries.sqlite3")))
    started = time.monotonic()
    try:
        store = await asyncio.to_thread(SummaryStore, path) if key else None
    except Exception:
        log.warning("Context summary persistence unavailable; bounded transient context used")
        store = None
    # demo has no model calls. No shared/anonymous persistent summary is ever created.
    client = agent.client if getattr(agent.settings, "mode", "demo") == "live" and key and store else None
    result, details = await compact(history, facts, client, store, key, cfg)
    details["current_input_deduplicated"] = deduplicated
    details["duration_ms"] = round((time.monotonic() - started) * 1000)
    # CAREBRIEF_CONTEXT_BUDGET_METRICS_V1
    details.update(trigger_budget=cfg.trigger, target_budget=cfg.target,
                   summary_budget=cfg.summary_budget, recent_turns=cfg.recent_turns,
                   max_batches=cfg.max_batches)
    if not key:
        details["binding"] = "unbound_recent_only"
        # Do not pay for ephemeral summaries that would be rebuilt on each request.
    try:
        await agent.events.write(run_id, "context_compaction", **details)
    except Exception:
        log.warning("Context event write failed; no conversation content logged")
    return result
