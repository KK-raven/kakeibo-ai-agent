# api/agent/core.py
"""
Agent のコアパイプライン

ユーザーの自然言語入力を受け取り、OpenAI API（Function Calling）で
意図判定・ツール選択を行い、対応するcrud関数を実行し、
実行結果をLLMに返して自然言語の応答を生成する。

処理フロー:
1. システムプロンプト + 会話履歴 + ユーザー入力を組み立てる
2. OpenAI API に送信（Tool 定義を添付）
3. LLM が tool_calls を返した場合:
   a) 引数のデフォルト値を補完（日付・支払方法・カード名）
   b) user_id を注入し、対応する crud 関数を実行
   c) 特殊処理（取引登録後の予算チェック等）
   d) 実行結果を tool ロールで LLM に返す
   e) LLM が自然言語応答を生成（再度 tool_calls なら繰り返し、最大5回）
4. 応答を返す
"""

import json
from datetime import date
from openai import OpenAI

from api.agent.tools import TOOLS
from api.db import crud
from api.db.crud import set_payment_method_group_default as _set_group_default
from api.utils.file_export import export_file as export_file_util
from api.agent.help import get_help as get_help_func
from api.agent.character import set_character, get_character
from api.utils.logger import get_logger

logger = get_logger(__name__)

client = OpenAI()

MODEL = "gpt-4o-mini"

MAX_TOOL_CALLS = 5

# user_idを必要としないツールの一覧。
# これ以外のツールは全てcrud関数であり、user_idを第一引数に取る。
_NO_USER_ID_TOOLS = {"export_file", "get_help"}


def _build_system_prompt(user_id: int) -> str:
    """システムプロンプトを生成する。

    今日の日付を動的に埋め込む。
    LLM は学習データの日付を使ってしまうことがあるため、
    明示的に「今日はYYYY-MM-DD」と伝える必要がある。

    キャラ設定がある場合は、名前・性格・口調を
    プロンプト冒頭に埋め込む。LLMはプロンプトの先頭付近の
    指示をより強く遵守するため、キャラ情報は冒頭に配置する。

    Args:
        user_id: ユーザーID。キャラ設定の読み込みに使用。
    """
    today = date.today().isoformat()

    # 登録済みクレジットカード情報の読み込み
    credit_cards = crud.get_credit_cards(user_id)
    if credit_cards:
        card_lines = []
        for c in credit_cards:
            suffix = "（デフォルト）" if c["is_default"] else ""
            card_lines.append(f"  - {c['name']}{suffix}")
        card_info = (
            "登録済みクレジットカード:\n"
            + "\n".join(card_lines)
        )
    else:
        card_info = "登録済みクレジットカード: なし"

    # 登録済み支払方法グループの読み込み
    # LLMがカタカナ表記（クイックペイ等）を正しい登録名（QUICPay等）に
    # マッピングできるよう、グループ情報を注入する。
    try:
        pm_result = crud.get_payment_methods(user_id)
        pm_methods = pm_result.get("payment_methods", [])
        groups: dict[str, list[dict]] = {}
        for m in pm_methods:
            gn = m.get("group_name")
            if gn:
                groups.setdefault(gn, []).append(m)
        if groups:
            group_lines = []
            for gn, entries in groups.items():
                entry_parts = []
                for e in entries:
                    suffix = ""
                    if e.get("is_group_default"):
                        suffix += "（デフォルト）"
                    lc = e.get("linked_card")
                    if lc:
                        suffix += f"（紐付: {lc}）"
                    entry_parts.append(f"{e['name']}{suffix}")
                group_lines.append(f"  - {gn}: {', '.join(entry_parts)}")
            pm_group_info = (
                "登録済み支払方法グループ:\n"
                + "\n".join(group_lines)
            )
        else:
            pm_group_info = ""
    except Exception:
        pm_group_info = ""

    # キャラ設定の読み込み
    character = get_character(user_id)

    if character:
        name = character.get("character_name", "アシスタント")
        personality = character.get("character_personality", "")
        tone = character.get("character_tone", "")
        role_section = (
            f"あなたは「{name}」という名前の家計簿アシスタントです。\n"
            f"性格: {personality}\n"
            f"口調: {tone}\n\n"
            "ユーザーの自然言語入力から意図を判断し、適切なToolを使って家計管理を支援します。\n"
            "上記の性格と口調を守りつつ、以下のルールに従って応答してください。"
        )
    else:
        role_section = (
            "あなたは家計簿アシスタントです。\n"
            "ユーザーの自然言語入力から意図を判断し、適切なToolを使って家計管理を支援します。"
        )

    return f"""{role_section}

今日の日付: {today}
{card_info}
{pm_group_info}

基本ルール:
- 日付の指定がなければ今日の日付（{today}）を使ってください
- 金額は正の整数で扱います
- 支出カテゴリ: 食費/光熱費/交通費/日用品/交際費/サブスク/医療費/衣服/娯楽/教育/家賃・住居/保険/その他
- 収入カテゴリ: 給与/賞与/副業・フリーランス/金融資産/ギャンブル/臨時収入/その他
- 支払方法が明示されなければ省略してください（システムがデフォルト値を適用します）。クレジットカード払いの場合のみ payment_method='クレジットカード' を設定すること（card_name はバックエンドが補完するため、わかる場合のみ設定すればよい）
- 削除・更新の確認フロー（取引・固定費 共通）:
  対象の内容を以下の定型フォーマットで提示し、ユーザーの明示的な肯定応答が来て初めてToolを実行する。承認なしに削除・更新のToolを実行することは絶対に禁止。
  対象の特定方法: get_transactionsで検索するか、直前のregister_transactionの結果を使う。会話履歴に結果がある場合は新たにget_transactionsを呼ばず、履歴の内容を提示して確認すること。
  get_transactionsの検索結果に複数件ヒットした場合は、全件を表示してどれを対象とするかユーザーに選ばせること。
  確認フォーマット（取引）:
    以下の取引を削除しますか？
    日付: ○○
    店名: ○○
    品目: ○○
    金額: ○○円
    カテゴリ: ○○
    支払方法: Toolの実行結果のdisplay_payment_methodの値をそのまま表示
- 削除・更新の対象は、直前のget_transactionsで取得した結果、または直前のregister_transactionで登録した取引からのみ選ぶこと。過去の会話で取得した取引IDを再利用してはならない
- 通常の取引登録（register_transaction）は事前確認なしで即座に実行すること。登録後、Toolの実行結果の値を以下の定型フォーマットで表示する。ユーザーの入力テキストから推測した値を使わないこと:
    登録しました！
    日付: ○○
    店名: ○○
    品目: ○○
    金額: ○○円
    カテゴリ: ○○
    支払方法: Toolの実行結果のdisplay_payment_methodの値をそのまま表示
- 品目名だけでカテゴリが曖昧な場合（「水」「チョコ」等の短い語）は、ユーザーにカテゴリを確認すること。店名等の文脈から明らかな場合は確認不要
- 回答は簡潔に、親しみやすい口調でお願いします
- ファイル出力時は必ず専用の集計ツールで数値を取得してからcontentを生成すること。数値の計算は絶対に自分で行わないこと
- ファイル出力後は保存完了とファイル名のみを伝えること。リンクやURLは生成しないこと
- 「確認します」「調べます」と言うだけで終わらず、必ずToolを実行して結果を返してください
- get_transactionsを呼ぶときは、特に指定がなければ今月のyear_monthを指定してください
- ユーザーが明示的にカテゴリや店名を指定した場合は、そのまま使うこと。勝手に変換しない
- カテゴリの判定は常識的に行うこと。食べ物・飲み物（お菓子・アイス・ジュース等も含む）は「食費」、日用消耗品は「日用品」が基本。「娯楽」は遊興・レジャー・ゲーム等に限定する。「その他」は他のカテゴリに該当しない場合にのみ使う
- テキストで複数件の支出をまとめて入力された場合は、1件ずつregister_transactionを実行すること。レシートOCRの確認フロー（ステップ1〜4）はテキスト入力には適用しない
- クレジットカード払い（取引登録・固定費登録 共通）:
  - カード払いを示唆する場合は payment_method='クレジットカード' を設定する。get_credit_cardsの呼び出しは不要
  - ユーザーがカード名を指定した場合は、プロンプト上部の登録済みカード一覧から該当する登録名を探し、card_nameに設定すること。該当するものがない場合もユーザーの指定をそのままcard_nameに設定する（バックエンドが検証しエラーを返す）。カード名の指定がない場合はcard_nameを省略してよい（バックエンドがデフォルトカードを補完する）
  - ツール実行結果にerrorが含まれる場合は、そのメッセージをユーザーに伝え、登録を中止すること
- 固定費登録はregister_fixed_expenseを2回呼ぶこと:
  1回目: confirmなしで呼ぶ → 返されたdisplay文字列をそのままユーザーに提示する。カード名等はバックエンドが解決済みなので、独自に「デフォルトカード」等と書き換えず、displayの内容をそのまま使うこと
  2回目: ユーザーの承認後にconfirm=trueで同じ内容を再度呼ぶ → 実際に登録される
  previewにerrorが含まれる場合はエラー内容をユーザーに伝え、カードの登録を促すこと
- 支払方法の情報取得について:
  - クレジットカード: get_payment_methodsやget_credit_cardsの呼び出しは不要（プロンプトに登録済みカード情報あり）
  - グループ型支払方法（QUICPay・PayPay等）: get_payment_methodsの呼び出しは不要（プロンプトに登録済みグループ情報あり）
  - 未登録の支払方法: get_payment_methodsで一覧を取得し、ユーザーの指定に近いものがあれば「○○のことですか？」と確認する。近いものがなければ「登録されていません。新しく追加しますか？」と聞いてからadd_payment_methodを実行する
- 新しい支払方法を追加する場合は、必ずユーザーに確認してください
- グループ型支払方法（QUICPay・PayPay等）の取引登録フロー（クレジットカードはグループフロー対象外）:
  - ユーザーが「クイックペイ」「ペイペイ」等と言った場合、プロンプト上部の登録済み支払方法グループから該当するグループを探し、デフォルトのエントリのnameをpayment_methodに設定してregister_transactionを呼ぶ。get_payment_methodsの呼び出しは不要（プロンプトに情報あり）
  - デフォルトが未設定の場合のみ、ユーザーにどのエントリを使うか確認する
- グループデフォルト変更は必ず「○○のデフォルトを△△に変更しますか？」と確認してからset_payment_method_group_defaultを実行すること

---

応答スタイル:
- キャラ設定がある場合は、その性格・口調を維持したまま以下のルールに従うこと
- 家計に関係ない話題（天気、雑談、相談等）にも応答してよい
- ただし応答の最後に、自然な形で家計の話題に繋げること
  例（デフォルト）: 「暑いですね」→「こういう日はつい飲み物代がかさみますよね」
  例（鴉）: 「暑いですね」→「ククク……こんな日は冷たい飲み物に金を使いたくなるものだな、主よ」
- 雑談への応答は2〜3文程度の簡潔なものにすること
- 家計の話題に繋げるのが不自然な場合は無理に繋げなくてよい
- 挨拶（おはよう、こんにちは等）には普通に返し、家計への誘導は不要

---

ファイナンシャル助言:
- 「節約したい」「貯金を増やしたい」「○万円貯めたい」「食費を減らすには？」等の家計改善の相談には、まず支出データを参照してから助言すること
- 助言に必要なToolを呼んでデータを取得し、事実に基づいて助言する
  例: 「5万円貯めたい」→ get_category_summaryで支出内訳を確認 → 「食費が○円で全体の△%を占めています。ここを□円に抑えると達成に近づきます」
- 合理性を重視すること。感情的・精神論的な助言（「頑張って節約しましょう」等）ではなく、データに基づく具体的な数値と根拠を示す
- 具体的な数値を示すこと。「節約しましょう」だけの漠然とした助言は避ける
- あくまで支出データの分析に基づく助言に留めること
- データが不足している場合（登録が少ない等）は、その旨を伝えた上で一般的な助言をする

---

禁止事項:
- 具体的な投資商品・銘柄の推奨は行わない。「投資についてはファイナンシャルプランナー等の専門家にご相談ください」と案内する
- 医療・法律・税務の専門的な助言は行わない。一般的な情報提供に留め、専門家への相談を勧める
- 不適切・攻撃的な入力に対しては、穏やかに家計管理の話題に戻す
- ユーザーの支出を批判したり罪悪感を与える表現は使わない。例: ×「また無駄遣いしましたね」 ○「登録しました！」

---

ヘルプ応答:
- 「何ができる？」「使い方教えて」等の機能案内にはget_helpを使う
- 特定の機能の詳しい動作仕様を聞かれた場合は、このプロンプト内のルールに基づいて直接回答する

---

レシートOCRフロー:

ユーザーメッセージが「[レシートOCR結果]」で始まる場合、以下のフローで処理してください。
Toolは呼ばず、まず確認メッセージを返してください。

【ステップ1: 抽出結果の表示と確認】
以下の形式でOCR結果を表示し、日付・支払方法・登録方式の確認をまとめて1つのメッセージで行ってください。

---（表示例）---
📄 レシートを読み取りました！

🏪 店名: ○○スーパー
📅 日付: 2024-03-15（この日付で登録しますか？）
💳 支払方法: 現金（この支払方法で登録しますか？）

🛒 商品一覧:
  1. 牛乳（食費）: 198円
  2. 洗剤（日用品）: 320円
  3. パン（食費）: 150円

💰 合計金額: 668円

登録方法を選んでください：
  A) 商品ごとに個別登録（3件登録されます）
  B) 合計金額のみ登録（668円・1件）→ カテゴリを教えてください

修正したい点があれば一緒に教えていただいても構いません。
---（表示例ここまで）---

合計金額が「記載なし」の場合は選択肢Bを表示しないでください。
店名が「不明」の場合は「店名が読み取れませんでした。店名を教えていただけますか？」と追加で質問してください。
支払方法が「不明」の場合は「支払方法を教えていただけますか？」と追加で質問してください。

支払方法が読み取れた場合は、get_payment_methodsで既存の支払方法一覧を取得し、
最も近いものがあれば、そちらでよいかどうかを確認すること。
例：「QUICPay (AirPay)」→ 既存の「QUICPay」でよいかどうかを確認。

【ステップ2: ユーザーの回答を受けての処理】
ユーザーから日付・支払方法・登録方式の回答が来たら、内容を解釈してください。

日付について:
- 「OK」「はい」「そのままで」などの肯定はOCR結果の日付を使用
- 別の日付が指定された場合はその日付を使用
- 日付に関する言及がなければOCR結果の日付をそのまま使用

支払方法について:
- 肯定または言及がなければOCR結果の支払方法（または「不明」時はユーザー入力値）を使用
- 別の支払方法が指定された場合はそれを使用

登録方式について:
- A（個別登録）が選ばれた場合: ステップ3へ
- B（合計のみ）が選ばれた場合: カテゴリを問う（「カテゴリは何にしますか？」）
  - カテゴリが指定された: そのカテゴリで合計金額を1件登録する（Toolを実行）
  - 「適当でいい」「なんでもいい」など明確な指定がない: 「カテゴリを『その他』として登録しました」と伝えて「その他」で登録する（Toolを実行）

【ステップ3: 個別登録の修正フロー】
A（個別登録）が選ばれた場合、現在の商品リストを表示して修正を促してください。

---（表示例）---
以下の内容で登録します。修正があればお知らせください👇

🏪 店名: ○○スーパー
  1. 牛乳（食費）: 198円
  2. 洗剤（日用品）: 320円
  3. パン（食費）: 150円

📅 日付: 2024-03-15
💳 支払方法: 現金
👤 person: 自分

問題なければ「登録して」と教えてください。
---（表示例ここまで）---

修正入力の解釈:
- 「〇〇は食費」「〇〇のカテゴリを日用品に」→ 該当商品のカテゴリを変更
- 「〇〇の価格を200円に」→ 該当商品の価格を変更
- 「〇〇は削除」→ 該当商品をリストから除外
- 「person（人物）は妻」→ 以降の登録のpersonを変更
- 複数の修正が1メッセージに含まれていれば全て適用

修正のたびに全商品リストを更新して再表示してください（修正された項目は変更後の値で表示）。
修正は何度でも受け付けてください。「登録して」「これでOK」「問題ない」などの承認が来たら登録に進みます。

【ステップ4: 登録実行】
承認が来たらregister_transactionを商品の件数分だけ実行してください。
全件登録完了後に「○件登録しました」と伝えてください。
登録後は通常の家計簿アシスタントの動作に戻ってください。

【エラー処理】
「[レシートOCR結果]\\nレシートを読み取れませんでした。」が来た場合:
「レシートの読み取りに失敗しました。もう一度画像を送ってみてください。画像が不鮮明な場合は、明るい場所で撮り直すと改善することがあります。」と返してください。
"""


# --- Tool名 → 関数 のマッピング ---
# 命名規則:
#   参照系: get_ または check_ で始める
#   更新系: register_, set_, apply_, delete_,
#           update_, add_, deactivate_, export_ で始める
#   UI側の再描画判定に使われるため、規則を守ること。
TOOL_FUNCTIONS = {
    "register_transaction": crud.register_transaction,
    "get_transactions": crud.get_transactions,
    "delete_transaction": crud.delete_transaction,
    "update_transaction": crud.update_transaction,
    "register_fixed_expense": crud.register_fixed_expense,
    "get_fixed_expenses": crud.get_fixed_expenses,
    "apply_fixed_expenses": crud.apply_fixed_expenses,
    "deactivate_fixed_expense": crud.deactivate_fixed_expense,
    "set_budget": crud.set_budget,
    "get_budgets": crud.get_budgets,
    "check_budget": crud.check_budget,
    "get_monthly_summary": crud.get_monthly_summary,
    "get_category_summary": crud.get_category_summary,
    "get_monthly_comparison": crud.get_monthly_comparison,
    "get_custom_summary": crud.get_custom_summary,
    "get_payment_methods": crud.get_payment_methods,
    "add_payment_method": crud.add_payment_method,
    "update_payment_method_linked_card": crud.update_payment_method_linked_card,
    "register_credit_card": crud.register_credit_card,
    "get_credit_cards": crud.get_credit_cards,
    "get_setting": crud.get_setting,
    "set_setting": crud.set_setting,
    "set_payment_method_group_default": _set_group_default,
    "export_file": export_file_util,
    "get_store_summary": crud.get_store_summary,
    "get_item_summary": crud.get_item_summary,
    "get_help": get_help_func,
    "set_character": set_character,
    "get_character": get_character,
    "get_health_indicators": crud.get_health_indicators,
    "get_categories": crud.get_categories,
}


def _ensure_credit_card_payment_method(args: dict) -> dict:
    """card_nameが指定されている場合、payment_methodをクレジットカードに強制する。

    LLMがcard_nameを設定しつつpayment_methodを口座振替等にする
    不整合を防ぐ。
    """
    if args.get("card_name") and args.get("payment_method") != "クレジットカード":
        logger.debug(
            f"payment_method強制変更: {args.get('payment_method')} → "
            "クレジットカード（card_name指定あり）"
        )
        args["payment_method"] = "クレジットカード"
    return args


def _resolve_card_name(user_id: int, args: dict) -> dict:
    """クレジットカード払い時のcard_nameを解決・照合する。

    register_transaction / register_fixed_expense 共通で使用する。

    処理:
    1. card_name 未指定 → デフォルトカードで補完
    2. card_name 指定あり → 登録済みカードと照合
       a. 完全一致 → そのまま使用
       b. 部分一致（片方が片方を含む）→ 登録名に置換
       c. 一致なし → error を付与

    カード未登録（0枚）の場合もerrorを付与。

    Args:
        user_id: ユーザーID。
        args: LLMが生成した引数の辞書。

    Returns:
        card_name が解決（またはerror付与）された引数の辞書。
    """
    if args.get("payment_method") != "クレジットカード":
        return args

    cards = crud.get_credit_cards(user_id)

    if not cards:
        args["error"] = (
            "クレジットカードが1枚も登録されていません。"
            "先にカードを登録してください。"
        )
        return args

    if not args.get("card_name"):
        # card_name 未指定 → デフォルトカードで補完
        default_card = next(
            (c for c in cards if c["is_default"]),
            None,
        )
        if default_card:
            args["card_name"] = default_card["name"]
            logger.debug(f"カード補完: {args['card_name']}")
        else:
            args["error"] = (
                "デフォルトのクレジットカードが登録されていません。"
                "先にカードを登録してください。"
            )
        return args

    # card_name 指定あり → 登録済みカードと照合
    specified = args["card_name"]
    card_names = [c["name"] for c in cards]

    # 完全一致チェック
    if specified in card_names:
        return args

    # 部分一致チェック（指定名が登録名に含まれる、またはその逆）
    matches = [
        cn for cn in card_names
        if specified in cn or cn in specified
    ]
    if len(matches) == 1:
        logger.debug(f"カード名解決: {specified} → {matches[0]}")
        args["card_name"] = matches[0]
        return args

    if len(matches) > 1:
        args["error"] = (
            f"「{specified}」に該当するカードが複数あります: "
            f"{', '.join(matches)}。どのカードか指定してください。"
        )
    else:
        args["error"] = (
            f"「{specified}」に該当する登録済みカードが見つかりません。"
            f"登録済みカード: {', '.join(card_names)}。"
            "先にカードを登録してください。"
        )
    return args


def _complement_defaults(
    user_id: int,
    tool_name: str,
    args: dict,
) -> dict:
    """LLM が省略・誤設定した引数にデフォルト値を補完する。

    補完対象:
    1. date: 省略時は今日の日付（register_transaction のみ）
    2. payment_method: 省略時は settings("default_payment_method") or "現金"
       （register_transaction のみ）
    3. payment_method がグループ名（例: "QUICPay"）の場合、
       同 group_name の is_group_default=True エントリ名に解決する
       （例: "QUICPay" → "QUICPay（JCB）"）
    4. card_name: クレカ払いで未指定の場合、デフォルトカードを適用。
       指定ありの場合、登録済みカードと照合する。
       （register_transaction / register_fixed_expense 共通）

    Args:
        user_id: ユーザーID。crud関数呼び出しに必要。
        tool_name: ツール名。
        args: LLMが生成した引数の辞書。

    Returns:
        デフォルト値が補完された引数の辞書。
    """
    if tool_name == "register_transaction":
        # 収入・支出共通で日付を補完する
        if "date" not in args or not args["date"]:
            args["date"] = date.today().isoformat()
            logger.debug(f"日付補完: {args['date']}")

        # 収入には支払方法・カード情報は不要なため補完しない
        if args.get("type") == "income":
            return args

        if "payment_method" not in args or not args["payment_method"]:
            default_pm = crud.get_setting(user_id, "default_payment_method")
            args["payment_method"] = default_pm or "現金"
            logger.debug(f"支払方法補完: {args['payment_method']}")

        # グループ名のまま渡された場合（例: "QUICPay"）、
        # is_group_default=True のエントリ名に解決する。
        # "クレジットカード" は別ルートで処理するため対象外。
        pm = args.get("payment_method", "")
        if pm and pm != "クレジットカード":
            resolved = _resolve_group_payment_method(
                user_id, pm, card_name=args.get("card_name"),
            )
            if resolved and resolved != pm:
                logger.debug(f"グループ支払方法解決: {pm} → {resolved}")
                args["payment_method"] = resolved
                # グループ解決済みの場合、card_name は不要
                # （カード情報は支払方法名に含まれる）。
                # card_name が残ると _ensure_credit_card_payment_method が
                # 誤って payment_method を「クレジットカード」に上書きする。
                args.pop("card_name", None)

        args = _ensure_credit_card_payment_method(args)
        args = _resolve_card_name(user_id, args)

    elif tool_name == "register_fixed_expense":
        # 「カード」「クレカ」等の略称を「クレジットカード」に正規化
        pm = args.get("payment_method", "")
        if pm and pm != "クレジットカード":
            if "カード" in pm or "クレカ" in pm:
                args["payment_method"] = "クレジットカード"
                logger.debug(f"固定費 支払方法正規化: {pm} → クレジットカード")

        args = _ensure_credit_card_payment_method(args)
        args = _resolve_card_name(user_id, args)

    return args


def _resolve_group_payment_method(
    user_id: int,
    payment_method: str,
    card_name: str | None = None,
) -> str | None:
    """支払方法名がグループ名と一致する場合、適切なエントリ名に解決する。

    例: payment_methods テーブルに
        name="QUICPay（JCB）", group_name="QUICPay", is_group_default=True
    がある場合、"QUICPay" → "QUICPay（JCB）" を返す。

    card_name が指定された場合、そのカードにリンクされたエントリを優先する。
    例: "PayPay" + card_name="JCB" → "PayPay（JCB）"

    一致するグループがない、またはそのまま使えるエントリがある場合は None を返す。

    Args:
        user_id: ユーザーID。
        payment_method: LLMが渡した支払方法名。
        card_name: 指定されたカード名（特定エントリの選択に使用）。

    Returns:
        解決後の支払方法名。解決不要なら None。
    """
    try:
        result = crud.get_payment_methods(user_id)
        methods = result.get("payment_methods", [])

        # 完全一致するエントリがあればそのまま使える（解決不要）
        exact = [m for m in methods if m["name"] == payment_method]
        if exact:
            return None

        # group_name が一致するエントリを検索
        group_entries = [
            m for m in methods
            if m.get("group_name") == payment_method
        ]

        if group_entries:
            # card_name 指定あり → そのカードにリンクされたエントリを探す
            if card_name:
                linked = [
                    m for m in group_entries
                    if m.get("linked_card")
                    and (card_name in m["linked_card"]
                         or m["linked_card"] in card_name)
                ]
                if len(linked) == 1:
                    return linked[0]["name"]

            # card_name なし or リンク一致なし → デフォルトエントリ
            defaults = [m for m in group_entries if m.get("is_group_default")]
            if defaults:
                return defaults[0]["name"]

        # OCRや外部入力で「QUICPay（Airペイ）」のように
        # カッコ付きで渡された場合、ベース名を抽出して再試行する。
        # 例: "QUICPay（Airペイ）" → "QUICPay"
        if "（" in payment_method:
            base = payment_method.split("（")[0].strip()
            base_entries = [
                m for m in methods
                if m.get("group_name") == base
            ]
            if base_entries:
                if card_name:
                    linked = [
                        m for m in base_entries
                        if m.get("linked_card")
                        and (card_name in m["linked_card"]
                             or m["linked_card"] in card_name)
                    ]
                    if len(linked) == 1:
                        return linked[0]["name"]
                defaults = [m for m in base_entries if m.get("is_group_default")]
                if defaults:
                    logger.debug(
                        f"ベース名でグループ解決: {payment_method} → {defaults[0]['name']}"
                    )
                    return defaults[0]["name"]

    except Exception as e:
        logger.warning(f"グループ支払方法解決エラー: {e}")

    return None


def _post_process(
    user_id: int,
    tool_name: str,
    result: dict | list | bool | str | None,
) -> str | None:
    """Tool 実行後の特殊処理。

    現在は register_transaction 後の自動予算チェックのみ。
    予算が設定されているカテゴリの取引を登録した場合、
    alert_level が "ok" 以外なら予算情報を追加テキストとして返す。

    Args:
        user_id: ユーザーID。
        tool_name: ツール名。
        result: ツール実行結果。

    Returns:
        追加情報のテキスト。不要なら None。
    """
    logger.debug(
        f"_post_process呼び出し: tool={tool_name},"
        f" result_type={type(result)}"
    )

    if tool_name == "register_transaction" and isinstance(result, dict):
        category = result.get("category")
        if category:
            today = date.today()
            year_month = f"{today.year:04d}-{today.month:02d}"
            budget_info = crud.check_budget(
                user_id, category, year_month,
                result.get("person", "自分"),
            )
            logger.debug(f"予算チェック結果: {budget_info}")

            if budget_info and budget_info["alert_level"] != "ok":
                if budget_info["alert_level"] == "warning":
                    return (
                        f"【予算警告】{category}の残り予算は"
                        f"{budget_info['remaining']}円です"
                        f"（予算{budget_info['budget']}円）"
                    )
                elif budget_info["alert_level"] == "over":
                    return (
                        f"【予算超過】{category}は"
                        f"{abs(budget_info['remaining'])}円超過しています"
                        f"（予算{budget_info['budget']}円、"
                        f"支出{budget_info['spent']}円）"
                    )
    return None


# 確認フローが必要な更新系ツール。
# これらのツールは事前に get_transactions で候補を表示し、
# ユーザーの承認を得てから実行する必要がある。
_CONFIRMATION_REQUIRED_TOOLS = {"delete_transaction", "update_transaction"}


def _has_confirmable_result(messages: list[dict]) -> bool:
    """直近の確認フローが有効か判定する。

    以下の条件を全て満たす場合に True を返す:
    1. 会話履歴内に get_transactions または register_transaction
       の実行結果がある
    2. その後に必要数の user メッセージがある
       - get_transactions 後: >= 1（LLMが既に内容を提示して確認済み）
       - register_transaction 後: >= 2（削除指示 + 確認応答の2ステップ）
    3. その後に他の更新系ツールが実行されていない

    このガードは「検索も登録もしていない」または
    「ユーザーの確認がない」状態での削除・更新をブロックする。
    ブロック時は呼び出し元がreturnで即座にユーザーに確認を求める。

    Args:
        messages: 現在の会話のメッセージリスト。

    Returns:
        直近の確認フローが有効なら True。
    """
    CONFIRMABLE_TOOLS = {"get_transactions", "register_transaction"}

    last_idx = None
    last_tool_name = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            name = tc.get("function", {}).get("name")
            if name in CONFIRMABLE_TOOLS:
                last_idx = i
                last_tool_name = name
                break
        if last_idx is not None:
            break

    if last_idx is None:
        return False

    user_msg_count = 0
    for i in range(last_idx + 1, len(messages)):
        msg = messages[i]
        if msg.get("role") == "user":
            user_msg_count += 1
        elif msg.get("role") == "assistant":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                # delete/update 自体はガードを無効化しない。
                # 同一検索結果から複数件を連続削除・更新するフローを許容する。
                if name in _CONFIRMATION_REQUIRED_TOOLS:
                    continue
                # 固定費のプレビュー/登録は削除フローと無関係なので無視する
                if name == "register_fixed_expense":
                    continue
                if not name.startswith(("get_", "check_")):
                    return False

    # get_transactions 後: LLMが内容を提示→ユーザーが「はい」(1通)で十分。
    # register_transaction 後: 「消して」(削除指示) + 「はい」(確認) の2通が必要。
    #   1通だけだと削除指示のみで確認なしに実行されてしまう。
    if last_tool_name == "get_transactions":
        return user_msg_count >= 1
    else:
        return user_msg_count >= 2


def chat(
    user_id: int,
    user_message: str,
    conversation_history: list[dict] | None = None,
) -> dict:
    """ユーザーの入力を受け取り、Agentの応答を返す。

    FastAPIのエンドポイントから呼ばれる。
    会話履歴は呼び出し元（Streamlit / LINE Bot）が管理し、毎回渡す。

    Args:
        user_id: ユーザーID。全ツール実行時にcrud関数へ渡される。
        user_message: ユーザーの自然言語入力。
        conversation_history: これまでの会話履歴（role/content の辞書リスト）。
                              None の場合は新規会話として扱う。

    Returns:
        {
            "response": "LLMの応答テキスト",
            "tool_results": [...],
        }
    """
    logger.info(f"ユーザー入力: {user_message}")

    messages = [{"role": "system", "content": _build_system_prompt(user_id)}]

    if conversation_history:
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

    tool_results = []
    messages_to_save = []

    tool_results = []

    for iteration in range(MAX_TOOL_CALLS):
        logger.debug(f"LLM呼び出し: iteration={iteration + 1}")

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
        except Exception as e:
            logger.error(f"OpenAI API エラー: {e}")
            return {
                "response": "申し訳ありません、処理中にエラーが発生しました。",
                "tool_results": tool_results,
                "messages_to_save": messages_to_save,
            }

        choice = response.choices[0]
        assistant_message = choice.message

        if not assistant_message.tool_calls:
            logger.info(f"LLM応答: {assistant_message.content}")
            return {
                "response": assistant_message.content,
                "tool_results": tool_results,
                "messages_to_save": messages_to_save,
            }

        messages.append({
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_message.tool_calls
            ],
        })
        messages_to_save.append(messages[-1])

        guard_messages = messages[:-1]

        for tc in assistant_message.tool_calls:
            tool_name = tc.function.name
            tool_args = json.loads(tc.function.arguments)

            logger.info(f"Tool呼び出し: {tool_name}({tool_args})")

            func = TOOL_FUNCTIONS.get(tool_name)
            if not func:
                error_msg = f"不明なTool: {tool_name}"
                logger.error(error_msg)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"error": error_msg}, ensure_ascii=False,
                    ),
                })
                messages_to_save.append(messages[-1])
                continue
            
            # 確認フローが必要なツールのプログラム的ガード。
            # 直近のget_transactionsの結果が有効な状態でないと
            # delete/updateを実行できない。
            # messages[:-1]で現在のターンのassistantメッセージを
            # 除外する。自身のtool_callが更新系ツールとして
            # 検出され、誤ってブロックされるのを防ぐため。
            if (
                tool_name in _CONFIRMATION_REQUIRED_TOOLS
                and not _has_confirmable_result(guard_messages)
            ):
                logger.warning(
                    f"確認フロー未完了のためブロック: {tool_name}"
                )
                # 現在のassistantメッセージ（tool_call含む）を履歴から除去
                messages.pop()
                messages_to_save.pop()
                # ブロック理由をシステムメッセージとして注入し、
                # LLMに文脈に応じた応答を生成させる
                messages.append({
                    "role": "system",
                    "content": (
                        "削除・更新のToolを実行する前に、対象の取引を"
                        "ユーザーに確認する必要があります。"
                        "会話履歴に取引結果が含まれている場合はその内容を"
                        "提示してください。含まれていない場合はget_transactions"
                        "で取引を検索してください。"
                        "いずれの場合も、取引内容を示した上で"
                        "削除・更新してよいかユーザーに確認すること。"
                    ),
                })
                try:
                    block_response = client.chat.completions.create(
                        model=MODEL,
                        messages=messages,
                        tools=TOOLS,
                        tool_choice="auto",
                    )
                    block_content = block_response.choices[0].message.content or ""
                    block_tool_calls = block_response.choices[0].message.tool_calls
                except Exception as e:
                    logger.error(f"ブロック時LLM呼び出しエラー: {e}")
                    return {
                        "response": "対象の取引を確認します。どの取引を削除・更新しますか？",
                        "tool_results": tool_results,
                        "messages_to_save": messages_to_save,
                    }
                # ブロック時にget_transactionsを呼んだ場合は実行して返す
                if block_tool_calls:
                    block_tc = block_tool_calls[0]
                    block_name = block_tc.function.name
                    if block_name == "get_transactions":
                        block_args = json.loads(block_tc.function.arguments)
                        block_args = _complement_defaults(user_id, block_name, block_args)
                        try:
                            block_result = crud.get_transactions(user_id, **block_args)
                        except Exception as e:
                            block_result = {"error": str(e)}
                        messages.append({
                            "role": "assistant",
                            "content": block_content,
                            "tool_calls": [{
                                "id": block_tc.id,
                                "type": "function",
                                "function": {
                                    "name": block_name,
                                    "arguments": block_tc.function.arguments,
                                },
                            }],
                        })
                        messages_to_save.append(messages[-1])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": block_tc.id,
                            "content": json.dumps(block_result, ensure_ascii=False, default=str),
                        })
                        messages_to_save.append(messages[-1])
                        tool_results.append({"tool": block_name, "args": block_args, "result": block_result})
                        # 検索結果を踏まえた最終応答を生成
                        try:
                            final_resp = client.chat.completions.create(
                                model=MODEL,
                                messages=messages,
                            )
                            block_content = final_resp.choices[0].message.content or ""
                        except Exception as e:
                            logger.error(f"ブロック後最終応答エラー: {e}")
                return {
                    "response": block_content,
                    "tool_results": tool_results,
                    "messages_to_save": messages_to_save,
                }

            # ユーザーメッセージにカード系キーワードがあるのに
            # LLMが payment_method を正しく設定しなかった場合の補正
            if (
                tool_name == "register_fixed_expense"
                and tool_args.get("payment_method") != "クレジットカード"
                and any(kw in user_message for kw in ("カード", "クレカ", "クレジット"))
            ):
                logger.debug(
                    f"ユーザーメッセージのカードキーワード検出: "
                    f"payment_method={tool_args.get('payment_method')} → クレジットカード"
                )
                tool_args["payment_method"] = "クレジットカード"

            tool_args = _complement_defaults(user_id, tool_name, tool_args)

            # _complement_defaults がエラーを付与した場合、
            # ツール実行をスキップしてエラーをLLMに返す
            if "error" in tool_args:
                error_msg = tool_args.pop("error")
                result = {"error": error_msg}
                logger.info(f"補完エラー: {tool_name} → {error_msg}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        result, ensure_ascii=False,
                    ),
                })
                messages_to_save.append(messages[-1])
                tool_results.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "error": error_msg,
                })
                continue

            # register_fixed_expense の confirm=true 時:
            # 直前のプレビュー結果から解決済みの値を引き継ぐ。
            # LLMが payment_method や card_name を省略/変更しても、
            # プレビューで確定した値が優先される。
            if (
                tool_name == "register_fixed_expense"
                and tool_args.get("confirm")
            ):
                for msg in reversed(messages):
                    if msg.get("role") == "tool":
                        try:
                            prev = json.loads(msg["content"])
                            if "preview" in prev:
                                for k, v in prev["preview"].items():
                                    if k not in tool_args or not tool_args[k]:
                                        tool_args[k] = v
                                logger.debug(
                                    f"固定費confirm: preview値をマージ"
                                )
                                break
                        except (json.JSONDecodeError, KeyError):
                            continue

            # register_fixed_expense のプレビューモード:
            # confirm=true でない場合はDBに書き込まず、
            # 解決済みの登録内容をプレビューとして返す。
            if (
                tool_name == "register_fixed_expense"
                and not tool_args.pop("confirm", False)
            ):
                preview = {k: v for k, v in tool_args.items()}
                # LLMが確認画面に全項目を表示できるよう、
                # 表示すべきフィールドを明示的に列挙する
                display_fields = []
                display_fields.append(f"名称: {preview.get('name', '不明')}")
                display_fields.append(f"月額金額: {preview.get('amount', '不明')}円")
                display_fields.append(f"カテゴリ: {preview.get('category', '不明')}")
                display_fields.append(f"毎月の計上日: {preview.get('day_of_month', '不明')}日")
                pm = preview.get('payment_method', '口座振替')
                cn = preview.get('card_name')
                if cn:
                    display_fields.append(f"支払方法: {pm}（{cn}）")
                else:
                    display_fields.append(f"支払方法: {pm}")
                display_fields.append(f"開始日: {preview.get('start_date', '不明')}")
                if preview.get('end_date'):
                    display_fields.append(f"終了日: {preview['end_date']}")
                result = {
                    "preview": preview,
                    "display": "\n".join(display_fields),
                    "message": "以下の内容で固定費を登録します。よろしいですか？",
                }
                logger.info(f"固定費プレビュー: {preview}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        result, ensure_ascii=False, default=str,
                    ),
                })
                messages_to_save.append(messages[-1])
                tool_results.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result,
                })
                continue

            try:
                # crud関数にはuser_idを第一引数として渡す。
                # export_file等のuser_id不要な関数はそのまま呼ぶ。
                if tool_name not in _NO_USER_ID_TOOLS:
                    result = func(user_id, **tool_args)
                else:
                    result = func(**tool_args)
            except Exception as e:
                error_msg = f"Tool実行エラー: {tool_name}: {e}"
                logger.error(error_msg)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"error": error_msg}, ensure_ascii=False,
                    ),
                })
                messages_to_save.append(messages[-1])
                tool_results.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "error": str(e),
                })
                continue

            logger.info(f"Tool結果: {tool_name} → {result}")

            # register_transaction / get_transactions の結果に
            # 表示用支払方法フィールドを注入。
            # LLMが括弧付き名称（QUICPay（JCB）等）を省略表示するのを防ぐ。
            if tool_name == "register_transaction" and isinstance(result, dict) and "error" not in result:
                pm = result.get("payment_method", "")
                cn = result.get("card_name")
                if cn:
                    result["display_payment_method"] = f"{pm}（{cn}）"
                else:
                    result["display_payment_method"] = pm
            elif tool_name == "get_transactions" and isinstance(result, list):
                for row in result:
                    if isinstance(row, dict):
                        pm = row.get("payment_method", "")
                        cn = row.get("card_name")
                        if cn:
                            row["display_payment_method"] = f"{pm}（{cn}）"
                        else:
                            row["display_payment_method"] = pm

            tool_results.append({
                "tool": tool_name,
                "args": tool_args,
                "result": result,
            })

            extra_info = _post_process(user_id, tool_name, result)

            tool_content = result
            if extra_info:
                tool_content = {
                    "result": result,
                    "extra": extra_info,
                }

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(
                    tool_content, ensure_ascii=False, default=str,
                ),
            })
            messages_to_save.append(messages[-1])

    logger.warning(f"Tool呼び出し上限到達: {MAX_TOOL_CALLS}回")
    messages.append({
        "role": "user",
        "content": (
            "ツール実行が上限に達しました。"
            "ここまでの結果をまとめ、残りの処理があれば"
            "ユーザーに追加で質問するよう案内してください。"
        ),
    })

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
        )
        final_content = response.choices[0].message.content
    except Exception as e:
        logger.error(f"最終応答生成エラー: {e}")
        final_content = (
            "処理が複雑になりすぎました。"
            "質問を分けて聞いていただけますか？"
        )

    return {
        "response": final_content,
        "tool_results": tool_results,
        "messages_to_save": messages_to_save,
    }
