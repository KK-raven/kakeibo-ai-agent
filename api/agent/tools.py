# api/agent/tools.py
"""
Function Calling 用の Tool 定義

役割:
- OpenAI API に渡す Tool のリスト（JSON スキーマ）を定義する
- LLM はこの定義を読み、ユーザーの入力に対して
  どの Tool をどの引数で呼ぶかを判断する

構造:
- 各 Tool は {"type": "function", "function": {...}} の形式
- function 内に name, description, parameters を定義
- parameters は JSON Schema 形式

注意:
- description は LLM の Tool 選択精度に直結する
  曖昧な説明だと間違った Tool が選ばれるため、
  「いつ使うか」「何をするか」を具体的に書く
- 日本語で書くのは、ユーザー入力が日本語のため
  LLM の判断精度が上がるから

命名規則:
- 参照系ツール: get_ または check_ で始める
- 更新系ツール: register_, set_, apply_, delete_,
  update_, add_, deactivate_, export_ で始める
- UI側でDB更新後の再描画判定にこの規則を使っている
  (ui/app.py の should_rerun)
- 新しいツール追加時は必ずこの規則に従うこと
"""

TOOLS = [
    # === 入出金管理 ===
    {
        "type": "function",
        "function": {
            "name": "register_transaction",
            "description": (
                "入出金を1件登録する。"
                "ユーザーが「○○で△△円使った」「給料が入った」"
                "のように支出や収入を報告したときに使う。"
                "日付の指定がなければ今日の日付を使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "取引日。YYYY-MM-DD形式。省略時は今日。",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["income", "expense"],
                        "description": "income（収入）または expense（支出）。",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "金額（正の整数）。",
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "カテゴリ。"
                            "支出: 食費/光熱費/交通費/日用品/交際費/"
                            "サブスク/医療費/衣服/娯楽/教育/家賃・住居/保険/その他。"
                            "収入: 給与/賞与/副業・フリーランス/金融資産/"
                            "ギャンブル/臨時収入/その他。"
                        ),
                    },
                    "store_name": {
                        "type": "string",
                        "description": "店名・支払先。わかる場合のみ。",
                    },
                    "item": {
                        "type": "string",
                        "description": "品目。わかる場合のみ。",
                    },
                    "memo": {
                        "type": "string",
                        "description": "自由メモ。ユーザーが補足情報を言った場合。",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": (
                            "支払方法。ユーザーが明示しなければ「現金」。"
                            "選択肢: 現金/口座振替/クレジットカード/"
                            "QUICPay/PayPay/Suica/PASMO/"
                            "Amazon Pay/楽天ペイ/メルペイ/PayPal/その他。"
                        ),
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の取引か。デフォルトは「自分」。",
                    },
                },
                "required": ["date", "type", "amount", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transactions",
            "description": (
                "取引履歴を検索する。"
                "「今月の食費を見せて」「セブンで何買った？」"
                "「先月の支出一覧」のように取引を探すときに使う。"
                "支払方法・金額での絞り込みも可能。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM形式。「今月」「先月」等から判断。",
                    },
                    "category": {
                        "type": "string",
                        "description": "カテゴリで絞り込み。",
                    },
                    "store_name": {
                        "type": "string",
                        "description": "店名で部分一致検索。",
                    },
                    "item": {
                        "type": "string",
                        "description": "品目で部分一致検索。",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["income", "expense"],
                        "description": "収入または支出で絞り込み。",
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の取引か。",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": (
                            "支払方法で完全一致検索。"
                            "「QUICPayの取引を全部見せて」等、"
                            "支払方法での一括操作前の絞り込みに使う。"
                        ),
                    },
                    "amount": {
                        "type": "integer",
                        "description": (
                            "金額で完全一致検索。"
                            "日付・店名と組み合わせて特定の取引を"
                            "ピンポイントで探すときに使う。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_transaction",
            "description": (
                "取引を1件削除する。"
                "ユーザーが「さっきの取引消して」「間違えたから削除」"
                "と言ったときに使う。"
                "削除対象の取引IDが必要。不明な場合は先に"
                "get_transactionsで候補を表示してユーザーに確認する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "integer",
                        "description": "削除する取引のID。",
                    },
                },
                "required": ["transaction_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_transaction",
            "description": (
                "取引の内容を修正する。"
                "「さっきの金額間違えた、3500円に直して」"
                "「カテゴリを食費に変更して」のように言われたときに使う。"
                "修正対象の取引IDが必要。不明な場合は先に"
                "get_transactionsで候補を表示してユーザーに確認すること。"
                "変更したいフィールドのみ指定すればよい。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "integer",
                        "description": "修正する取引のID。",
                    },
                    "date": {
                        "type": "string",
                        "description": "修正後の取引日。YYYY-MM-DD形式。",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["income", "expense"],
                        "description": "修正後の種別。",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "修正後の金額。",
                    },
                    "category": {
                        "type": "string",
                        "description": "修正後のカテゴリ。",
                    },
                    "store_name": {
                        "type": "string",
                        "description": "修正後の店名。",
                    },
                    "item": {
                        "type": "string",
                        "description": "修正後の品目。",
                    },
                    "memo": {
                        "type": "string",
                        "description": "修正後のメモ。",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": "修正後の支払方法。",
                    },
                    "person": {
                        "type": "string",
                        "description": "修正後の対象者。",
                    },
                },
                "required": ["transaction_id"],
            },
        },
    },

    # === 固定出金管理 ===
    {
        "type": "function",
        "function": {
            "name": "register_fixed_expense",
            "description": (
                "毎月の固定出金を登録する。"
                "「家賃8万円を毎月27日に登録して」"
                "「Netflixを固定費に追加して」のように言われたときに使う。"
                "実行前に必ず登録内容を提示し、ユーザーに確認すること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "固定出金の名称（家賃、Netflix等）。",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "月額金額。",
                    },
                    "category": {
                        "type": "string",
                        "description": "カテゴリ（家賃・住居、サブスク等）。",
                    },
                    "day_of_month": {
                        "type": "integer",
                        "description": "毎月の計上日（1〜31）。",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "開始日。YYYY-MM-DD形式。",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": "支払方法。デフォルトは「口座振替」。",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "終了日。継続中ならなし。",
                    },
                },
                "required": ["name", "amount", "category", "day_of_month", "start_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fixed_expenses",
            "description": (
                "固定出金の一覧を表示する。"
                "「固定費の一覧見せて」「毎月の固定出金は？」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {
                        "type": "boolean",
                        "description": "有効なもののみ表示するか。デフォルトtrue。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_fixed_expenses",
            "description": (
                "指定した年月の固定出金を一括計上する。"
                "「今月の固定費を計上して」と言われたときに使う。"
                "既に計上済みのものはスキップされる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "年（例: 2025）。",
                    },
                    "month": {
                        "type": "integer",
                        "description": "月（例: 4）。",
                    },
                },
                "required": ["year", "month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deactivate_fixed_expense",
            "description": (
                "固定出金を無効化（停止）する。"
                "「Spotify解約したから固定費から消して」"
                "「家賃の固定費を止めて」のように言われたときに使う。"
                "対象のIDが不明な場合は先にget_fixed_expensesで"
                "一覧を表示してユーザーに確認すること。"
                "実行前に必ず「○○を固定費から削除しますか？」と確認すること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fixed_expense_id": {
                        "type": "integer",
                        "description": "無効化する固定出金のID。",
                    },
                },
                "required": ["fixed_expense_id"],
            },
        },
    },

    # === 予算管理 ===
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": (
                "カテゴリ別の月額予算を設定する。"
                "「食費の予算を3万円にして」"
                "「交際費1万5千円、残り5千円で警告して」"
                "のように言われたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "カテゴリ名。",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "月額予算。",
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の予算か。デフォルトは「自分」。",
                    },
                    "alert_threshold": {
                        "type": "integer",
                        "description": (
                            "残額警告の閾値。"
                            "残額がこの値を下回ったら警告する。"
                            "指定がなければ警告なし。"
                        ),
                    },
                },
                "required": ["category", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_budgets",
            "description": (
                "予算一覧を表示する。"
                "「予算の設定を見せて」「いくらに設定してたっけ」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person": {
                        "type": "string",
                        "description": "誰の予算か。デフォルトは「自分」。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_budget",
            "description": (
                "特定カテゴリの予算状況を確認する。"
                "「食費あといくら使える？」「予算大丈夫？」"
                "と聞かれたときに使う。"
                "予算額・支出額・残額・警告レベルを返す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "カテゴリ名。",
                    },
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM形式。「今月」「先月」等から判断して指定する。",
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の予算か。デフォルトは「自分」。",
                    },
                },
                "required": ["category", "year_month"],
            },
        },
    },

    # === 集計 ===
    {
        "type": "function",
        "function": {
            "name": "get_monthly_summary",
            "description": (
                "月次の収入合計・支出合計・差額を返す。"
                "「今月いくら使った？」「今月の収支は？」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM形式。",
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の集計か。デフォルトは「自分」。",
                    },
                },
                "required": ["year_month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_category_summary",
            "description": (
                "カテゴリ別の支出または収入の内訳を返す。"
                "「今月のカテゴリ別支出は？」「何に一番使ってる？」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM形式。",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["expense", "income"],
                        "description": "支出か収入か。デフォルトは expense。",
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の集計か。デフォルトは「自分」。",
                    },
                },
                "required": ["year_month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monthly_comparison",
            "description": (
                "2つの月のカテゴリ別支出を比較する。"
                "「先月と比べてどう？」「去年の4月と比較して」"
                "と聞かれたときに使う。"
                "compare_toを省略すると前月と比較する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM形式（当月）。",
                    },
                    "compare_to": {
                        "type": "string",
                        "description": (
                            "YYYY-MM形式（比較対象月）。"
                            "省略すると前月。"
                        ),
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の集計か。デフォルトは「自分」。",
                    },
                },
                "required": ["year_month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_custom_summary",
            "description": (
                "柔軟な軸で集計する。"
                "「セブンでいくら使った？」→ group_by=store_name, store_name=セブン。"
                "「おやつにいくら？」→ group_by=item, item=おやつ。"
                "「どの店で一番使ってる？」→ group_by=store_name。"
                "「支払方法別で見せて」→ group_by=payment_method。"
                "「クレカで何に使った？」→ group_by=category, payment_method=クレジットカード。"
                "カテゴリ別以外の集計にはこのToolを使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM形式。",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": [
                            "category", "store_name", "item",
                            "payment_method", "person",
                        ],
                        "description": "集計軸。",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["expense", "income"],
                        "description": "支出か収入か。デフォルトは expense。",
                    },
                    "person": {
                        "type": "string",
                        "description": "誰の集計か。デフォルトは「自分」。",
                    },
                    "category": {
                        "type": "string",
                        "description": "カテゴリで絞り込み。",
                    },
                    "store_name": {
                        "type": "string",
                        "description": "店名で絞り込み（部分一致）。",
                    },
                    "item": {
                        "type": "string",
                        "description": "品目で絞り込み（部分一致）。",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": "支払方法で絞り込み。",
                    },
                },
                "required": ["year_month", "group_by"],
            },
        },
    },

    # === 支払方法・カード管理 ===
    {
        "type": "function",
        "function": {
            "name": "get_payment_methods",
            "description": (
                "登録されている支払方法の一覧を返す。"
                "「支払方法の一覧を見せて」や、"
                "ユーザーが未登録の支払方法を使おうとしたときに"
                "候補を提示するために使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["現金", "非現金"],
                        "description": "大区分で絞り込み。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_payment_method",
            "description": (
                "新しい支払方法を追加する。"
                "ユーザーが未登録の支払方法を使いたいと言ったとき、"
                "確認を取った上で使う。"
                "必ずユーザーに「○○を新しい支払方法として追加しますか？」"
                "と確認してから実行すること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "支払方法名。",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["現金", "非現金"],
                        "description": "大区分。ほぼ全て非現金。",
                    },
                    "linked_card": {
                        "type": "string",
                        "description": "決済元カード名。わかる場合のみ。",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_payment_method_linked_card",
            "description": (
                "支払方法の決済元カードを設定する。"
                "「QUICPayの決済元はJCBにして」のように言われたとき、"
                "または初めて使う支払方法の決済元を確認するときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "支払方法名（QUICPay等）。",
                    },
                    "linked_card": {
                        "type": "string",
                        "description": "決済元カード名（JCB等）。",
                    },
                },
                "required": ["name", "linked_card"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_credit_card",
            "description": (
                "クレジットカードを登録する。"
                "「JCBカードを登録して」「新しいカードを追加したい」"
                "と言われたときに使う。"
                "1枚目は自動的にデフォルトカードになる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "カード名（JCB、三井住友VISA等）。",
                    },
                    "billing_close_day": {
                        "type": "integer",
                        "description": "締め日（1〜31）。わかる場合のみ。",
                    },
                    "payment_day": {
                        "type": "integer",
                        "description": "引き落とし日（1〜31）。わかる場合のみ。",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_credit_cards",
            "description": (
                "登録されているクレジットカードの一覧を返す。"
                "「カードの一覧見せて」「デフォルトカードはどれ？」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_setting",
            "description": (
                "アプリの設定を変更する。"
                "「デフォルトの支払方法をクレカにして」"
                "「支払方法の初期値をPayPayに変えて」"
                "のように言われたときに使う。"
                "設定可能なキー: "
                "default_payment_method（デフォルト支払方法）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": ["default_payment_method"],
                        "description": "設定キー。",
                    },
                    "value": {
                        "type": "string",
                        "description": "設定値。",
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_setting",
            "description": (
                "アプリの設定値を確認する。"
                "「デフォルトの支払方法は何？」"
                "のように聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": ["default_payment_method"],
                        "description": "設定キー。",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_file",
            "description": (
                "集計結果やレポートをファイルに保存する。"
                "ユーザーがcsvやtxtでの出力を求めた場合に使う。"
                "必ず事前に必要なデータ取得ツールを呼び出し、"
                "正確な数値を取得してからcontentを生成すること。"
                "数値の計算・集計はツールに任せ、LLMは文章生成のみ行うこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_type": {
                        "type": "string",
                        "enum": ["csv", "txt"],
                        "description": "出力ファイル形式。csv または txt。",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "保存するファイル名。必ずタイムスタンプなしの名前を指定すること。"
                            "タイムスタンプはシステムが自動付与する。"
                            "例: 2026-03_report.txt, 2026-03_transactions.csv"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "ファイルに書き込む内容。"
                            "txtの場合: 事実に基づいた解説付きのレポートテキスト。"
                            "csvの場合: ヘッダー行を含むCSV形式の文字列。"
                        ),
                    },
                },
                "required": ["file_type", "filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_summary",
            "description": (
                "店別の支出・収入集計を返す。"
                "「セブンでいくら使った？」「どの店が一番多い？」等に使う。"
                "store_nameを指定すると特定店舗に絞り込める。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "集計対象月。YYYY-MM形式。例: 2026-03",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["expense", "income"],
                        "description": "支出はexpense、収入はincome。デフォルトはexpense。",
                    },
                    "person": {
                        "type": "string",
                        "description": "集計対象者。自分・妻・共通のいずれか。デフォルトは自分。",
                    },
                    "store_name": {
                        "type": "string",
                        "description": "店名で部分一致絞り込み。省略時は全店舗。",
                    },
                },
                "required": ["year_month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_summary",
            "description": (
                "品目別の支出・収入集計を返す。"
                "「チョコにいくら使った？」「弁当の合計は？」等に使う。"
                "itemを指定すると特定品目に絞り込める。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "集計対象月。YYYY-MM形式。例: 2026-03",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["expense", "income"],
                        "description": "支出はexpense、収入はincome。デフォルトはexpense。",
                    },
                    "person": {
                        "type": "string",
                        "description": "集計対象者。自分・妻・共通のいずれか。デフォルトは自分。",
                    },
                    "item": {
                        "type": "string",
                        "description": "品目で部分一致絞り込み。省略時は全品目。",
                    },
                },
                "required": ["year_month"],
            },
        },
    },

    # === ヘルプ ===
    {
        "type": "function",
        "function": {
            "name": "get_help",
            "description": (
                "アプリの機能一覧や使い方を案内する。"
                "「何ができる？」「使い方教えて」「ヘルプ」"
                "「○○ってどうやるの？」「○○の使い方」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "知りたい機能のトピック。省略時は全体概要を返す。"
                            "例: 登録、集計、予算、固定費、レシート、"
                            "ダッシュボード、ファイル出力、支払方法、設定"
                        ),
                    },
                },
                "required": [],
            },
        },
    },

    # === キャラ設定 ===
    {
        "type": "function",
        "function": {
            "name": "set_character",
            "description": (
                "アシスタントのキャラクターを変更する。"
                "「ぴよちゃんにして」「キャラを鴉に変えて」"
                "「キャラをデフォルトに戻して」「キャラリセット」"
                "のように言われたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preset": {
                        "type": "string",
                        "enum": ["デフォルト", "ぴよちゃん", "鴉", "賢者"],
                        "description": (
                            "キャラクターのプリセット名。"
                            "デフォルト: 通常の家計簿アシスタント。"
                            "ぴよちゃん: 癒し系。"
                            "鴉: 尊大だが忠実。"
                            "賢者: 哲学的。"
                        ),
                    },
                },
                "required": ["preset"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_character",
            "description": (
                "現在のキャラクター設定を確認する。"
                "「今のキャラは？」「キャラ設定を見せて」"
                "と聞かれたときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    
    # === 家計健全性指標 ===
    {
        "type": "function",
        "function": {
            "name": "get_health_indicators",
            "description": (
                "家計健全性指標（エンゲル係数・住居費比率・固定費比率・貯蓄率）を算出する。"
                "「家計の健全性を確認したい」「貯蓄率はどのくらい？」"
                "「家計診断して」などと聞かれたときに使う。"
                "結果のreferenceフィールドに各指標の参考値が含まれるため、"
                "目安値は必ずreferenceフィールドを参照すること。"
                "自分の知識から閾値・目安値を生成しないこと。"
                "has_incomeがFalseの場合、貯蓄率は計算不可であることを伝えること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": (
                            "集計対象年月（YYYY-MM形式）。"
                            "指定がない場合は省略する（当月が自動適用される）。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
]
