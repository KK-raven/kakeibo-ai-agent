# tests/test_card_resolution.py
"""_resolve_card_name と _complement_defaults のカード名解決テスト。

DB接続を必要としないよう、crud.get_credit_cards をモックする。
"""
import unittest
from unittest.mock import patch


class TestResolveCardName(unittest.TestCase):
    """_resolve_card_name の単体テスト。"""

    def _call(self, user_id, args):
        from api.agent.core import _resolve_card_name
        return _resolve_card_name(user_id, args)

    # --- payment_method が クレジットカード以外 → スキップ ---

    def test_non_credit_card_skipped(self):
        """クレジットカード以外の支払方法は何も変更しない。"""
        args = {"payment_method": "現金", "card_name": "何か"}
        result = self._call(1, args)
        self.assertNotIn("error", result)
        self.assertEqual(result["card_name"], "何か")

    def test_no_payment_method_skipped(self):
        """payment_method がない場合も何も変更しない。"""
        args = {"amount": 1000}
        result = self._call(1, args)
        self.assertNotIn("error", result)

    # --- カード未登録（0枚） ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[])
    def test_no_cards_registered_error(self, _mock):
        """カードが1枚も登録されていない場合、errorを返す。"""
        args = {"payment_method": "クレジットカード"}
        result = self._call(1, args)
        self.assertIn("error", result)
        self.assertIn("1枚も登録されていません", result["error"])

    # --- card_name 未指定 → デフォルトカード補完 ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_default_card_complement(self, _mock):
        """card_name 未指定時にデフォルトカードで補完される。"""
        args = {"payment_method": "クレジットカード"}
        result = self._call(1, args)
        self.assertNotIn("error", result)
        self.assertEqual(result["card_name"], "ViewCard")

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": False},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_no_default_card_error(self, _mock):
        """デフォルトカードが未設定の場合、errorを返す。"""
        args = {"payment_method": "クレジットカード"}
        result = self._call(1, args)
        self.assertIn("error", result)
        self.assertIn("デフォルト", result["error"])

    # --- card_name 指定あり: 完全一致 ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_exact_match(self, _mock):
        """完全一致する場合はそのまま使用。"""
        args = {"payment_method": "クレジットカード", "card_name": "ViewCard"}
        result = self._call(1, args)
        self.assertNotIn("error", result)
        self.assertEqual(result["card_name"], "ViewCard")

    # --- card_name 指定あり: 部分一致 ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_partial_match_specified_in_registered(self, _mock):
        """指定名が登録名に含まれる場合、登録名に解決される。"""
        args = {"payment_method": "クレジットカード", "card_name": "View"}
        result = self._call(1, args)
        self.assertNotIn("error", result)
        self.assertEqual(result["card_name"], "ViewCard")

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_partial_match_registered_in_specified(self, _mock):
        """登録名が指定名に含まれる場合（例: ビューカード → ViewCard は文字列一致しないが
        「楽天」→「楽天カード」のケース）、登録名に解決される。"""
        args = {"payment_method": "クレジットカード", "card_name": "楽天カードVISA"}
        result = self._call(1, args)
        self.assertNotIn("error", result)
        self.assertEqual(result["card_name"], "楽天カード")

    # --- card_name 指定あり: 複数部分一致 → エラー ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "楽天カード（JCB）", "is_default": True},
        {"name": "楽天カード（VISA）", "is_default": False},
    ])
    def test_multiple_partial_matches_error(self, _mock):
        """複数のカードに部分一致する場合、errorを返す。"""
        args = {"payment_method": "クレジットカード", "card_name": "楽天カード"}
        result = self._call(1, args)
        self.assertIn("error", result)
        self.assertIn("複数あります", result["error"])

    # --- card_name 指定あり: 一致なし → エラー ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_no_match_error(self, _mock):
        """一致するカードがない場合、errorを返す。"""
        args = {"payment_method": "クレジットカード", "card_name": "ドコモカード"}
        result = self._call(1, args)
        self.assertIn("error", result)
        self.assertIn("見つかりません", result["error"])
        self.assertIn("ViewCard", result["error"])
        self.assertIn("楽天カード", result["error"])


class TestComplementDefaultsCardResolution(unittest.TestCase):
    """_complement_defaults 経由でのカード解決テスト。

    register_transaction と register_fixed_expense 両方で
    _resolve_card_name が適用されることを確認する。
    """

    def _call(self, user_id, tool_name, args):
        from api.agent.core import _complement_defaults
        return _complement_defaults(user_id, tool_name, args)

    # --- register_transaction ---

    @patch("api.agent.core._resolve_group_payment_method", return_value=None)
    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
    ])
    def test_transaction_card_complement(self, _mock_cards, _mock_group):
        """register_transaction でカード補完が効く。"""
        args = {
            "type": "expense",
            "payment_method": "クレジットカード",
            "date": "2026-04-22",
        }
        result = self._call(1, "register_transaction", args)
        self.assertEqual(result["card_name"], "ViewCard")
        self.assertNotIn("error", result)

    @patch("api.agent.core._resolve_group_payment_method", return_value=None)
    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_transaction_card_name_resolution(self, _mock_cards, _mock_group):
        """register_transaction で部分一致解決が効く。"""
        args = {
            "type": "expense",
            "payment_method": "クレジットカード",
            "card_name": "楽天",
            "date": "2026-04-22",
        }
        result = self._call(1, "register_transaction", args)
        self.assertEqual(result["card_name"], "楽天カード")
        self.assertNotIn("error", result)

    @patch("api.agent.core._resolve_group_payment_method", return_value=None)
    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
    ])
    def test_transaction_no_match_error(self, _mock_cards, _mock_group):
        """register_transaction で一致なしならerrorが付く。"""
        args = {
            "type": "expense",
            "payment_method": "クレジットカード",
            "card_name": "ドコモ",
            "date": "2026-04-22",
        }
        result = self._call(1, "register_transaction", args)
        self.assertIn("error", result)

    # --- register_transaction: 収入 → カード解決スキップ ---

    def test_transaction_income_skipped(self):
        """収入の場合はカード解決しない。"""
        args = {"type": "income", "date": "2026-04-22"}
        result = self._call(1, "register_transaction", args)
        self.assertNotIn("error", result)
        self.assertNotIn("card_name", result)

    # --- register_fixed_expense ---

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
    ])
    def test_fixed_expense_card_complement(self, _mock_cards):
        """register_fixed_expense でデフォルトカード補完が効く。"""
        args = {"payment_method": "クレジットカード"}
        result = self._call(1, "register_fixed_expense", args)
        self.assertEqual(result["card_name"], "ViewCard")
        self.assertNotIn("error", result)

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
        {"name": "楽天カード", "is_default": False},
    ])
    def test_fixed_expense_card_name_resolution(self, _mock_cards):
        """register_fixed_expense で部分一致解決が効く。"""
        args = {
            "payment_method": "クレジットカード",
            "card_name": "View",
        }
        result = self._call(1, "register_fixed_expense", args)
        self.assertEqual(result["card_name"], "ViewCard")
        self.assertNotIn("error", result)

    @patch("api.agent.core.crud.get_credit_cards", return_value=[])
    def test_fixed_expense_no_cards_error(self, _mock_cards):
        """register_fixed_expense でカード未登録ならerror。"""
        args = {"payment_method": "クレジットカード"}
        result = self._call(1, "register_fixed_expense", args)
        self.assertIn("error", result)

    @patch("api.agent.core.crud.get_credit_cards", return_value=[
        {"name": "ViewCard", "is_default": True},
    ])
    def test_fixed_expense_no_match_error(self, _mock_cards):
        """register_fixed_expense で一致なしならerror。"""
        args = {
            "payment_method": "クレジットカード",
            "card_name": "ドコモ",
        }
        result = self._call(1, "register_fixed_expense", args)
        self.assertIn("error", result)

    # --- 口座振替 → カード解決しない ---

    def test_fixed_expense_bank_transfer_skipped(self):
        """口座振替の場合はカード解決しない。"""
        args = {"payment_method": "口座振替"}
        result = self._call(1, "register_fixed_expense", args)
        self.assertNotIn("error", result)
        self.assertNotIn("card_name", result)

    # --- 無関係のツール → スキップ ---

    def test_unrelated_tool_skipped(self):
        """対象外のツールは何も変更しない。"""
        args = {"payment_method": "クレジットカード"}
        result = self._call(1, "get_fixed_expenses", args)
        self.assertNotIn("error", result)


if __name__ == "__main__":
    unittest.main()
