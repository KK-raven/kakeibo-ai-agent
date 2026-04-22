# tests/test_group_payment.py
"""_resolve_group_payment_method のユニットテスト。

DB接続を必要としないよう、crud.get_payment_methods をモックする。
"""
import unittest
from unittest.mock import patch

MOCK_METHODS = [
    {
        "name": "QUICPay（JCB）",
        "group_name": "QUICPay",
        "is_group_default": True,
        "linked_card": "JCB",
    },
    {
        "name": "PayPay（JCB）",
        "group_name": "PayPay",
        "is_group_default": True,
        "linked_card": "JCB",
    },
    {
        "name": "PayPay（口座振替）",
        "group_name": "PayPay",
        "is_group_default": False,
        "linked_card": None,
    },
]


class TestResolveGroupPaymentMethod(unittest.TestCase):

    def _call(self, payment_method, card_name=None):
        from api.agent.core import _resolve_group_payment_method
        return _resolve_group_payment_method(1, payment_method, card_name)

    # --- 完全一致 → 解決不要 ---

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_exact_name_match(self, _mock):
        """エントリ名と完全一致する場合は解決不要。"""
        result = self._call("QUICPay（JCB）")
        self.assertIsNone(result)

    # --- グループ名 → デフォルトエントリ ---

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_group_default(self, _mock):
        """グループ名 → デフォルトエントリに解決。"""
        result = self._call("QUICPay")
        self.assertEqual(result, "QUICPay（JCB）")

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_paypay_default(self, _mock):
        """PayPayグループ → デフォルトに解決。"""
        result = self._call("PayPay")
        self.assertEqual(result, "PayPay（JCB）")

    # --- グループ名 + card_name → 特定エントリ ---

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_group_with_card_name_linked(self, _mock):
        """グループ名 + card_name → リンクされたエントリに解決。"""
        result = self._call("PayPay", card_name="JCB")
        self.assertEqual(result, "PayPay（JCB）")

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_group_with_card_name_no_link(self, _mock):
        """card_nameにリンクされたエントリがない場合 → デフォルトにフォールバック。"""
        result = self._call("PayPay", card_name="Viewcard")
        self.assertEqual(result, "PayPay（JCB）")

    # --- カッコ付き入力 → ベース名で解決 ---

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_parenthesized_input(self, _mock):
        """カッコ付き入力 → ベース名で解決。"""
        result = self._call("QUICPay（Airペイ）")
        self.assertEqual(result, "QUICPay（JCB）")

    # --- 一致なし → None ---

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": MOCK_METHODS})
    def test_no_match(self, _mock):
        """一致するグループなし → None。"""
        result = self._call("現金")
        self.assertIsNone(result)

    # --- card_name でグループ内の非デフォルトを選択 ---

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": [
               {
                   "name": "PayPay（JCB）",
                   "group_name": "PayPay",
                   "is_group_default": False,
                   "linked_card": "JCB",
               },
               {
                   "name": "PayPay（口座振替）",
                   "group_name": "PayPay",
                   "is_group_default": True,
                   "linked_card": None,
               },
           ]})
    def test_card_name_selects_non_default(self, _mock):
        """card_name指定で非デフォルトのエントリを選択。"""
        # デフォルトはPayPay（口座振替）だが、card_name="JCB"でPayPay（JCB）を選択
        result = self._call("PayPay", card_name="JCB")
        self.assertEqual(result, "PayPay（JCB）")

    @patch("api.agent.core.crud.get_payment_methods",
           return_value={"payment_methods": [
               {
                   "name": "PayPay（JCB）",
                   "group_name": "PayPay",
                   "is_group_default": False,
                   "linked_card": "JCB",
               },
               {
                   "name": "PayPay（口座振替）",
                   "group_name": "PayPay",
                   "is_group_default": True,
                   "linked_card": None,
               },
           ]})
    def test_no_card_name_uses_default(self, _mock):
        """card_name未指定 → デフォルトエントリ。"""
        result = self._call("PayPay")
        self.assertEqual(result, "PayPay（口座振替）")


if __name__ == "__main__":
    unittest.main()
