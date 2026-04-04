# api/db/connection.py
"""
DB初期化・接続管理モジュール

役割:
- SQLiteデータベースファイルへの接続を管理する
- アプリ起動時にテーブルが存在しなければ作成する（CREATE IF NOT EXISTS）
- 他モジュール（crud.py, seed.py等）はここからconnectionを取得して使う

SQLiteを選んだ理由:
- シングルユーザーのローカルアプリなのでサーバーDBは不要
- ファイル1つで完結し、Docker volumeで永続化できる
"""

import sqlite3
import os
from pathlib import Path

# --- DB ファイルパス ---
# 環境変数で上書き可能にしておく（テスト時やDocker環境で便利）
# デフォルトは api/db/connection.py
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                os.path.abspath(__file__)
            )
        )
    ),
    "data",
    "kakeibo.db",
)
DB_PATH = os.environ.get("KAKEIBO_DB_PATH", DEFAULT_DB_PATH)


def get_connection() -> sqlite3.Connection:
    """
    SQLiteコネクションを返す。

    row_factory = sqlite3.Row:
      結果を辞書ライクに扱える（row["category"] のようにカラム名でアクセス）。
      タプルのインデックスアクセス（row[3]等）だとコードの可読性が落ちるため。

    PRAGMA journal_mode=WAL:
      Write-Ahead Logging。読み取りと書き込みを同時に行える。
      FastAPIで複数リクエストが来た場合にロック競合を減らす安全策。

    PRAGMA foreign_keys=ON:
      SQLiteはデフォルトで外部キー制約が無効。明示的にONにする。
      現時点では外部キーを使っていないが、将来の拡張に備える。
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """
    全テーブルを作成する（存在しなければ）。

    アプリ起動時に1回呼ばれる想定。
    CREATE TABLE IF NOT EXISTS なので、既にテーブルがあれば何もしない。
    """
    conn = get_connection()
    try:
        # --- transactions テーブル ---
        # 入出金の個別取引明細。アプリの中心データ。
        #
        # カラム設計の根拠:
        # ・id: 更新・削除時にレコードを一意に特定するため必要
        # ・date: 取引が発生した日（ユーザー指定 or 当日）
        # ・type: "income" or "expense" — CHECK制約で2値に限定
        # ・amount: 金額。常に正の値で格納し、type で収入/支出を区別する
        # ・category: 食費・給与等。LLMが自然言語から自動分類する
        # ・store_name, item: 任意。店名と品目を分けて持つことで、
        #                    カテゴリ推定の精度向上と品目レベルの分析に対応
        # ・memo: ユーザーの自由メモ
        # ・payment_method: 支払方法（現金/クレジットカード/QUICPay等）
        # ・card_name: クレジットカード払いの場合、どのカードで払ったか。
        #             QUICPay等はpayment_methods.linked_cardから辿れるのでNULL。
        #             現金等カード無関係の場合もNULL。
        # ・person: 誰の支出か。デフォルト "自分"
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                amount INTEGER NOT NULL CHECK (amount > 0),
                category TEXT NOT NULL,
                store_name TEXT,
                item TEXT,
                memo TEXT,
                payment_method TEXT NOT NULL DEFAULT '現金',
                card_name TEXT,
                person TEXT NOT NULL DEFAULT '自分'
            )
        """)

        # --- fixed_expenses テーブル ---
        # 毎月固定で発生する支出のマスタデータ。
        # 月初（or 指定日）にこのテーブルを参照し、is_active=1 かつ
        # 有効期間内のレコードを transactions に自動計上する（Step 3 で実装）。
        #
        # ・day_of_month: 毎月何日に計上するか（1〜31）
        #   該当日が存在しない月（例: 31日指定で2月）は末日にフォールバック
        #   （この処理は自動計上ロジック側で行う）
        # ・is_active: 解約時に0にする。DELETEせず履歴を残す
        # ・start_date / end_date: 有効期間。end_date が NULL なら継続中
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fixed_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                category TEXT NOT NULL,
                day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
                payment_method TEXT NOT NULL DEFAULT '口座振替',
                is_active INTEGER NOT NULL DEFAULT 1,
                start_date DATE NOT NULL,
                end_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- store_category_mapping テーブル ---
        # 店名→カテゴリの「傾向」を蓄積するテーブル。
        #
        # 目的: LLMが新しい入力に対してカテゴリを推定する際の補助情報。
        # 例: 過去にセブンイレブンでの取引が 食費15回・日用品3回 なら、
        #     次にセブンイレブンと言われたら食費の可能性が高いとLLMに伝える。
        #
        # 注意: 個別の品目情報はこのテーブルには入らない。
        #       品目の詳細は transactions.item に記録される。
        #       このテーブルはあくまで「店名×カテゴリの出現頻度」の統計情報。
        #
        # UNIQUE制約: 同じ店名×カテゴリの組み合わせは1行に集約し、
        #             count を加算更新する（UPSERT、Step 2 で実装）。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_category_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT NOT NULL,
                category TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                last_used DATE NOT NULL,
                UNIQUE (store_name, category)
            )
        """)

        # --- budgets テーブル ---
        # カテゴリ別×person別の月額予算。
        # 例: 自分の食費=30000円、自分の交際費=15000円
        #
        # 予算超過アラート（Step 4）で、当月の支出合計と比較する。
        # UNIQUE(category, person) で同じ組み合わせの重複を防ぐ。
        #
        # alert_threshold:
        #   残額がこの値を下回ったら警告を出す閾値。
        #   例: 5000 → 残り5000円未満で「予算残りわずかです」と通知。
        #   NULLの場合は警告なし（超過時のみ通知）。
        #   Agent が取引登録のたびに check_budget を呼び、
        #   alert_level を見て応答を組み立てる（Step 6 で実装）。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                alert_threshold INTEGER CHECK (
                     alert_threshold IS NULL OR alert_threshold > 0
                     ),
                person TEXT NOT NULL DEFAULT '自分',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (category, person)
            )
        """)

        # --- settings テーブル ---
        # 汎用キーバリューストア。
        # v1 では最小限の用途。v2 でキャラ設定（名前・性格・口調）等を格納予定。
        # 構造化が不要な単純な設定値を入れる場所。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- payment_methods テーブル ---
        # 利用可能な支払方法のマスタデータ。
        #
        # 大区分（category）:
        #   "現金" or "非現金" の2値。
        #
        # 紐付けカード（linked_card）:
        #   QUICPay→JCB、Amazon Pay→三井住友VISA のように、
        #   最終的にどのカードで決済されるかを記録する。
        #   NULLの場合は直接決済（現金、口座振替等）。
        #   「クレジットカード」もNULL（その時々で異なるカードを使うため、
        #   取引ごとにtransactions.card_nameで記録する）。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL CHECK (category IN ('現金', '非現金')),
                linked_card TEXT
            )
        """)

        # --- credit_cards テーブル ---
        # クレジットカードのマスタデータ。
        #
        # 用途:
        # 1. カード別の利用合計を集計する
        # 2. 引き落とし日・締め日から「次回いくら引き落とされるか」を算出する（v2）
        #
        # is_default:
        #   ユーザーが「クレカ」「カード」とだけ言った場合に使うカード。
        #   1枚だけ is_default=1 にする。Agent 側で制御する。
        #
        # billing_close_day / payment_day:
        #   締め日と引き落とし日。初期はNULLで、後からチャットで設定可能。
        #   例: 15日締め翌月10日払い → billing_close_day=15, payment_day=10
        #   引き落とし金額の集計ロジックは v2 で実装する。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                billing_close_day INTEGER CHECK (
                    billing_close_day IS NULL
                    OR billing_close_day BETWEEN 1 AND 31
                ),
                payment_day INTEGER CHECK (
                    payment_day IS NULL
                    OR payment_day BETWEEN 1 AND 31
                ),
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- デフォルトの支払方法を投入 ---
        # アプリ初回起動時のみ実行される（既にデータがあればスキップ）。
        # INSERT OR IGNORE: UNIQUE制約に衝突したら何もしない。
        # つまり2回目以降の init_db() では追加されない。
        default_payment_methods = [
            ("現金", "現金"),
            ("口座振替", "非現金"),
            ("クレジットカード", "非現金"),
            ("QUICPay", "非現金"),
            ("PayPay", "非現金"),
            ("Suica", "非現金"),
            ("PASMO", "非現金"),
            ("Amazon Pay", "非現金"),
            ("楽天ペイ", "非現金"),
            ("メルペイ", "非現金"),
            ("PayPal", "非現金"),
            ("その他", "非現金"),
            ]
        for name, cat in default_payment_methods:
            conn.execute(
                """
                INSERT OR IGNORE INTO payment_methods (name, category)
                VALUES (?, ?)
                """,
                (name, cat),
            )

        conn.commit()
    finally:
        conn.close()


# --- モジュール単体テスト用 ---
# python3 -m api.db で直接実行すると、テーブル作成だけ行う
if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")

    # 確認: 作成されたテーブル一覧を表示
    conn = get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    
    for t in tables:
        print(f"  - {t['name']}")
    conn.close()
