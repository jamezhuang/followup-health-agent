"""Select model context without modifying the stored conversation."""


class HistoryBudgetExceeded(ValueError):
    """The latest complete turn cannot fit in the history budget."""


def build_history(
    history: list[dict[str, str]],
    *,
    max_turns: int = 6,
    max_chars: int = 6000,
) -> list[dict[str, str]]:
    if max_turns < 1 or max_chars < 1:
        raise ValueError("History limits must be positive")

    # 创建副本，避免修改数据库或会话里的完整消息。
    messages = [
        {"role": message["role"], "content": message["content"]} for message in history
    ]

    # 当前项目在第一轮之前会创建一条助手欢迎语。
    if messages and messages[0]["role"] == "assistant":
        messages = messages[1:]

    if len(messages) % 2:
        raise ValueError("History must contain complete user/assistant turns")

    turns = []

    for index in range(0, len(messages), 2):
        pair = messages[index : index + 2]

        if pair[0]["role"] != "user" or pair[1]["role"] != "assistant":
            raise ValueError("Invalid history role order")

        turns.append(pair)

    selected = []
    used_chars = 0

    # 从最近的一轮开始选择。
    for pair in reversed(turns[-max_turns:]):
        size = sum(len(message["content"]) for message in pair)

        if used_chars + size > max_chars:
            if not selected:
                # 最近一轮不能被悄悄忽略，否则短回复容易失去上下文。
                raise HistoryBudgetExceeded("Latest turn exceeds history budget")

            break

        selected.append(pair)
        used_chars += size

    # 返回时恢复正常时间顺序。
    return [message for pair in reversed(selected) for message in pair]
