import unittest

from app.context import build_history, HistoryBudgetExceeded


def turn(question, answer):
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


class ContextTests(unittest.TestCase):
    def test_welcome_is_not_a_turn(self):
        welcome = [{"role": "assistant", "content": "你好"}]

        self.assertEqual(build_history(welcome), [])

        messages = turn("咳嗽3天", "请补充年龄。")
        self.assertEqual(
            build_history(welcome + messages),
            messages,
        )

    def test_keep_latest_complete_turns(self):
        history = turn("第一轮", "回答一") + turn("第二轮", "回答二")

        self.assertEqual(
            build_history(history, max_turns=1),
            history[-2:],
        )

    def test_budget_drops_old_turn_as_a_whole(self):
        history = turn("aaaa", "bbbb") + turn("cccc", "dddd")

        self.assertEqual(
            build_history(history, max_chars=10),
            history[-2:],
        )

    def test_latest_turn_is_not_silently_dropped(self):
        with self.assertRaises(HistoryBudgetExceeded):
            build_history(
                turn("aaaa", "bbbb"),
                max_chars=7,
            )

    def test_stored_history_is_not_modified(self):
        history = turn("28岁", "已记录年龄。")

        result = build_history(history)
        result[0]["content"] = "changed"

        self.assertEqual(history[0]["content"], "28岁")

    def test_invalid_history_and_limits(self):
        with self.assertRaises(ValueError):
            build_history([{"role": "user", "content": "未完成"}])

        with self.assertRaises(ValueError):
            build_history(
                turn("问题", "回答"),
                max_turns=0,
            )

        with self.assertRaises(ValueError):
            build_history(
                turn("问题", "回答"),
                max_chars=0,
            )


if __name__ == "__main__":
    unittest.main()
