"""Content-free metrics allowed into the workspace run ledger."""

COUNTS = {
    "before_estimate_at_least", "after_estimate", "cursor_before", "cursor_after",
    "summary_calls", "summary_input_estimate", "summary_output_estimate",
    "pending_messages", "retained_messages", "omitted_recent_messages",
    "unsummarized_old_messages", "summary_estimate", "trigger_budget",
    "target_budget", "summary_budget", "recent_turns", "max_batches",
}
FLAGS = {"persistent", "reset", "persistence_error", "current_input_deduplicated"}
ENUMS = {
    "estimator": {"utf8_byte_upper_bound"},
    "binding": {"unbound_recent_only"},
}
ERRORS = {"turn_exceeds_batch_budget", "summary_version_conflict", "context_envelope_budget",
          "TimeoutError", "ValidationError", "ValueError", "RuntimeError", "ProviderError",
          "OSError", "TypeError", "KeyError"}


def context_compaction_metrics(event):
    if event.get("stage") != "context_compaction":
        return {}
    result = {}
    for key in COUNTS:
        value = event.get(key)
        if type(value) is int and 0 <= value <= 10**12:
            result[key] = value
    for key in FLAGS:
        if type(event.get(key)) is bool:
            result[key] = event[key]
    for key, choices in ENUMS.items():
        value = event.get(key)
        if isinstance(value, str) and value in choices:
            result[key] = value
    if isinstance(event.get("error"), str) and event["error"]:
        result["compaction_error"] = event["error"] if event["error"] in ERRORS else "other_summary_error"
    # No summaries, document text, owner identifiers, prompts or free-form errors.
    return result
