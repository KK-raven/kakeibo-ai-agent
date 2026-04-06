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
from api.utils.file_export import export_file as export_file_util
from api.utils.logger import get_logger

logger = get_logger(__name__)

client = OpenAI()

MODEL = "gpt-4o-mini"

MAX_TOOL_CALLS = 5

# user_idを必要としないツールの一覧。
# これ以外のツールは全てcrud関数であり、user_idを第一引数に取る。
_NO_USER_ID_TOOLS = {"export_file"}


def _build_system_prompt() -> str:
    """システムプロンプトを生成する。

    今日の日付を動的に埋め込む。
    LLM は学習データの日付を使ってしまうことがあるため、
    明示的に「今日はYYYY-MM-DD」と伝える必要がある。
    """
    today = date.today().isoformat()
    return f"""あなたは家計簿アシスタントです。
    ユーザーの自然言語入力から意図を判断し、適切なToolを使って家計管理を支援します。

    今日の日付: {today}

    基本ルール:
    - 日付の指定がなければ今日の日付（{today}）を使ってください
    - 金額は正の整数で扱います
    - 支出カテゴリ: 食費/光熱費/交通費/日用品/交際費/サブスク/医療費/衣服/娯楽/教育/家賃・住居/保険/その他
    - 収入カテゴリ: 給与/賞与/副業・フリーランス/金融資産/ギャンブル/臨時収入/その他
    - 支払方法が明示されなければ省略してください（システムがデフォルト値を適用します）
    - 取引を削除する場合は、まず候補を表示してユーザーに確認してください
    - 新しい支払方法を追加する場合は、必ずユーザーに確認してください
    - 回答は簡潔に、親しみやすい口調でお願いします
    - ファイル出力時は必ず専用の集計ツールで数値を取得してからcontentを生成すること。数値の計算は絶対に自分で行わないこと
    - ファイル出力後は保存完了とファイル名のみを伝えること。リンクやURLは生成しないこと
    - 「確認します」「調べます」と言うだけで終わらず、必ずToolを実行して結果を返してください
    - get_transactionsを呼ぶときは、特に指定がなければ今月のyear_monthを指定してください
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
    "export_file": export_file_util,
    "get_store_summary": crud.get_store_summary,
    "get_item_summary": crud.get_item_summary,
}


def _complement_defaults(
    user_id: int,
    tool_name: str,
    args: dict,
) -> dict:
    """LLM が省略した引数にデフォルト値を補完する。

    補完対象:
    1. date: 省略時は今日の日付
    2. payment_method: 省略時は settings("default_payment_method") or "現金"
    3. card_name: クレカ払いで未指定の場合、デフォルトカードを適用

    Args:
        user_id: ユーザーID。crud関数呼び出しに必要。
        tool_name: ツール名。
        args: LLMが生成した引数の辞書。

    Returns:
        デフォルト値が補完された引数の辞書。
    """
    if tool_name == "register_transaction":
        if "date" not in args or not args["date"]:
            args["date"] = date.today().isoformat()
            logger.debug(f"日付補完: {args['date']}")

        if "payment_method" not in args or not args["payment_method"]:
            default_pm = crud.get_setting(user_id, "default_payment_method")
            args["payment_method"] = default_pm or "現金"
            logger.debug(f"支払方法補完: {args['payment_method']}")

        if (
            args.get("payment_method") == "クレジットカード"
            and not args.get("card_name")
        ):
            cards = crud.get_credit_cards(user_id)
            default_card = next(
                (c for c in cards if c["is_default"]),
                None,
            )
            if default_card:
                args["card_name"] = default_card["name"]
                logger.debug(f"カード補完: {args['card_name']}")

    return args


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

    messages = [{"role": "system", "content": _build_system_prompt()}]

    if conversation_history:
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

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
            }

        choice = response.choices[0]
        assistant_message = choice.message

        if not assistant_message.tool_calls:
            logger.info(f"LLM応答: {assistant_message.content}")
            return {
                "response": assistant_message.content,
                "tool_results": tool_results,
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
                continue

            tool_args = _complement_defaults(user_id, tool_name, tool_args)

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
                tool_results.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "error": str(e),
                })
                continue

            logger.info(f"Tool結果: {tool_name} → {result}")

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
    }
