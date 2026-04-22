# tests/test_has_confirmable_result.py
"""_has_confirmable_result のユニットテスト。

削除・更新のガード判定が正しく機能することを確認する。
DB接続不要。
"""
import unittest


def _make_assistant_tool_call(tool_name: str, content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "function": {"name": tool_name},
        }],
    }


def _make_user_msg(content: str = "はい") -> dict:
    return {"role": "user", "content": content}


def _make_tool_result(tool_call_id: str = "1", content: str = "{}") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class TestHasConfirmableResult(unittest.TestCase):

    def _call(self, messages):
        from api.agent.core import _has_confirmable_result
        return _has_confirmable_result(messages)

    # --- 起点ツールなし → False ---

    def test_empty_messages(self):
        self.assertFalse(self._call([]))

    def test_no_confirmable_tool(self):
        """get_transactions も register_transaction もない場合。"""
        messages = [
            _make_user_msg("こんにちは"),
            {"role": "assistant", "content": "こんにちは！"},
        ]
        self.assertFalse(self._call(messages))

    # --- get_transactions 後にユーザー確認1通 → True ---

    def test_get_transactions_then_one_user_msg(self):
        """検索後にユーザーが1通返信 → 削除許可。"""
        messages = [
            _make_user_msg("ファミマのやつ消して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
            {"role": "assistant", "content": "この取引を削除しますか？"},
            _make_user_msg("はい"),
        ]
        self.assertTrue(self._call(messages))

    # --- register_transaction 後にユーザー確認1通 → True ---

    def test_register_then_cancel(self):
        """登録直後に「やっぱ消して」→ 削除許可。"""
        messages = [
            _make_user_msg("ランチ800円"),
            _make_assistant_tool_call("register_transaction"),
            _make_tool_result(),
            {"role": "assistant", "content": "登録しました！"},
            _make_user_msg("やっぱ消して"),
        ]
        self.assertTrue(self._call(messages))

    # --- 起点ツール後にユーザーメッセージ0通 → False ---

    def test_get_transactions_no_user_msg(self):
        """検索直後、ユーザー応答なし → ブロック。"""
        messages = [
            _make_user_msg("消して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
        ]
        self.assertFalse(self._call(messages))

    # --- 起点ツール後に更新系ツールが挟まる → False ---

    def test_invalidated_by_update_tool(self):
        """起点ツール後に register_transaction が挟まると無効化。"""
        messages = [
            _make_user_msg("検索して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
            _make_user_msg("やっぱ登録して"),
            _make_assistant_tool_call("register_transaction"),
            _make_tool_result(),
            _make_user_msg("やっぱ消して"),
        ]
        # 最後の起点は register_transaction なので True
        # (register_transaction 自体は CONFIRMABLE_TOOLS)
        self.assertTrue(self._call(messages))

    def test_invalidated_by_unrelated_update_tool(self):
        """起点ツール後に無関係な更新系ツール(add_payment_method)が挟まる → False。"""
        messages = [
            _make_user_msg("消して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
            _make_user_msg("支払方法追加して"),
            _make_assistant_tool_call("add_payment_method"),
            _make_tool_result(),
            _make_user_msg("はい消して"),
        ]
        self.assertFalse(self._call(messages))

    # --- delete/update 自体はガードを無効化しない（連続削除許可） ---

    def test_consecutive_deletes_allowed(self):
        """delete_transaction 後に続けて別件削除 → 許可。"""
        messages = [
            _make_user_msg("検索して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
            _make_user_msg("1番目消して"),
            _make_assistant_tool_call("delete_transaction"),
            _make_tool_result(),
            _make_user_msg("2番目も消して"),
        ]
        self.assertTrue(self._call(messages))

    # --- register_fixed_expense は削除フローを無効化しない ---

    def test_register_fixed_expense_does_not_invalidate(self):
        """固定費登録が間に入っても削除フローは有効。"""
        messages = [
            _make_user_msg("検索して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
            _make_user_msg("固定費も登録して"),
            _make_assistant_tool_call("register_fixed_expense"),
            _make_tool_result(),
            _make_user_msg("さっきの取引消して"),
        ]
        self.assertTrue(self._call(messages))

    # --- 参照系ツール(get_/check_)は無効化しない ---

    def test_read_tool_does_not_invalidate(self):
        """get_ 系ツールが間に入っても削除フローは有効。"""
        messages = [
            _make_user_msg("検索して"),
            _make_assistant_tool_call("get_transactions"),
            _make_tool_result(),
            _make_user_msg("予算も見せて"),
            _make_assistant_tool_call("get_budgets"),
            _make_tool_result(),
            _make_user_msg("さっきのやつ消して"),
        ]
        self.assertTrue(self._call(messages))


if __name__ == "__main__":
    unittest.main()
