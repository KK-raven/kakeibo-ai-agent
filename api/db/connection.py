# api/db/connection.py
"""
DB接続・初期化モジュール（PostgreSQL版）

役割:
- コネクションプールを管理し、効率的なDB接続を提供する
- アプリ起動時にテーブルが存在しなければ作成する
- 新規ユーザー登録時にデフォルトデータを自動挿入する
"""

import os

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

from api.utils.logger import get_logger

logger = get_logger(__name__)

# --- コネクションプール ---
# モジュールレベルで保持し、アプリのライフサイクル全体で再利用する
_pool: SimpleConnectionPool | None = None

# デモユーザーの識別子。LINEユーザーIDの形式（U + 32桁hex）とは
# 明らかに異なる文字列にして衝突を防ぐ
DEMO_USER_LINE_ID = "DEMO_USER"


def _get_pool() -> SimpleConnectionPool:
    """プールを遅延初期化して返す。

    初回呼び出し時にプールを作成し、以降は同じプールを返す。
    DATABASE_URL環境変数が未設定の場合はエラーを出す。

    Returns:
        初期化済みのコネクションプール。
    """
    global _pool
    if _pool is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL が設定されていません。"
                ".env ファイルを確認してください。"
            )
        # cursor_factory をプールレベルで設定することで、
        # 取得した全コネクションのカーソルがRealDictCursorになる
        # → fetchone() が辞書、fetchall() が辞書のリストを返す
        _pool = SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=database_url,
            cursor_factory=RealDictCursor,
        )
        logger.info("コネクションプール初期化完了")
    return _pool


def get_connection():
    """プールからコネクションを取得する。

    使用後は必ず release_connection() で返却すること。
    返却しないとプールが枯渇し、新しい接続を取得できなくなる。

    Returns:
        psycopg2 のコネクションオブジェクト。
    """
    return _get_pool().getconn()


def release_connection(conn) -> None:
    """コネクションをプールに返却する。

    crud.pyの各関数のfinally句で呼び出す。
    SQLite版でのconn.close()に相当する。

    Args:
        conn: 返却するコネクション。
    """
    _get_pool().putconn(conn)


def close_pool() -> None:
    """全コネクションを閉じてプールを破棄する。

    アプリ終了時に呼び出す。FastAPIのlifespanのshutdownで使用。
    """
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("コネクションプール終了")


def init_db() -> None:
    """全テーブルを作成し、初期データを投入する。

    アプリ起動時に1回呼ばれる想定。
    CREATE TABLE IF NOT EXISTS なので、既にテーブルがあれば何もしない。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # --- users テーブル ---
        # マルチユーザー対応の基盤。全テーブルがこのテーブルのidを参照する。
        # line_user_idでLINEアカウントと紐付ける。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                line_user_id TEXT NOT NULL UNIQUE,
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- transactions テーブル ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
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
        # is_activeは0/1のINTEGERで管理（0=無効、1=有効）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fixed_expenses (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                category TEXT NOT NULL,
                day_of_month INTEGER NOT NULL
                    CHECK (day_of_month BETWEEN 1 AND 31),
                payment_method TEXT NOT NULL DEFAULT '口座振替',
                is_active INTEGER NOT NULL DEFAULT 1,
                start_date DATE NOT NULL,
                end_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- conversation_history テーブル ---
        # LINE Bot用の会話履歴。ユーザーごとに直近の会話を保持し、
        # 複数ターンにまたがる対話（削除確認、固定出金登録等）を実現する。
        # tool_calls: assistantのツール呼び出し情報（JSONB）
        # tool_call_id: toolロールのメッセージがどのtool_callへの応答かを示すID
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_calls JSONB,
                tool_call_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # conversation_history: マイグレーション
        # 削除・更新の確認フローで tool_calls / tool ロールの
        # メッセージを復元するために必要なカラムを追加
        for col, col_type in [
            ("tool_calls", "JSONB"),
            ("tool_call_id", "TEXT"),
        ]:
            cur.execute(f"""
                ALTER TABLE conversation_history
                ADD COLUMN IF NOT EXISTS {col} {col_type}
            """)

        # ツール呼び出しのみ（テキストなし）のassistantメッセージに対応
        cur.execute("""
            ALTER TABLE conversation_history
            ALTER COLUMN content DROP NOT NULL
        """)

        # --- store_category_mapping テーブル ---
        # UNIQUE制約にuser_idを含め、ユーザーごとに独立した傾向を蓄積する
        cur.execute("""
            CREATE TABLE IF NOT EXISTS store_category_mapping (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                store_name TEXT NOT NULL,
                category TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                last_used DATE NOT NULL,
                UNIQUE (user_id, store_name, category)
            )
        """)

        # --- budgets テーブル ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                category TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                alert_threshold INTEGER
                    CHECK (alert_threshold IS NULL OR alert_threshold > 0),
                person TEXT NOT NULL DEFAULT '自分',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, category, person)
            )
        """)

        # --- settings テーブル ---
        # v1ではkeyがPRIMARY KEYだったが、user_id追加に伴い
        # SERIAL PKに変更し、UNIQUE(user_id, key) で一意性を担保する
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, key)
            )
        """)

        # --- payment_methods テーブル ---
        # linked_cardがユーザーごとに異なるため（例: QUICPayの紐付け先）、
        # user_idで分離する。デフォルトの12種はユーザー登録時にコピーする。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_methods (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('現金', '非現金')),
                linked_card TEXT,
                UNIQUE (user_id, name)
            )
        """)

        # --- credit_cards テーブル ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS credit_cards (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                billing_close_day INTEGER
                    CHECK (billing_close_day IS NULL
                           OR billing_close_day BETWEEN 1 AND 31),
                payment_day INTEGER
                    CHECK (payment_day IS NULL
                           OR payment_day BETWEEN 1 AND 31),
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, name)
            )
        """)

        # --- デモユーザーの作成 ---
        cur.execute(
            """
            INSERT INTO users (line_user_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (line_user_id) DO NOTHING
            """,
            (DEMO_USER_LINE_ID, "デモユーザー"),
        )

        # デモユーザーのIDを取得してデフォルト支払方法を投入
        cur.execute(
            "SELECT id FROM users WHERE line_user_id = %s",
            (DEMO_USER_LINE_ID,),
        )
        demo_user = cur.fetchone()
        if demo_user:
            _insert_default_payment_methods(cur, demo_user["id"])

        conn.commit()
        logger.info("データベース初期化完了")

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def _insert_default_payment_methods(cur, user_id: int) -> None:
    """デフォルトの支払方法12種を投入する。

    新規ユーザー登録時にも呼ばれる共通処理。
    ON CONFLICT DO NOTHING で既存データがあればスキップする。

    Args:
        cur: カーソル（呼び出し元がcommitを管理する）。
        user_id: 対象ユーザーのID。
    """
    default_methods = [
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
    for name, cat in default_methods:
        cur.execute(
            """
            INSERT INTO payment_methods (user_id, name, category)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, name) DO NOTHING
            """,
            (user_id, name, cat),
        )


def get_or_create_user(
    line_user_id: str,
    display_name: str | None = None,
) -> int:
    """LINEユーザーIDからuser_idを取得する。未登録なら新規作成する。

    LINE Bot経由の初回メッセージ時に呼ばれる。
    新規作成時はデフォルトの支払方法12種も自動挿入する。

    Args:
        line_user_id: LINEのユーザーID（U + 32桁hex）。
        display_name: LINEの表示名。初回取得時に保存する。

    Returns:
        usersテーブルのid（整数）。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 既存ユーザーを検索
        cur.execute(
            "SELECT id FROM users WHERE line_user_id = %s",
            (line_user_id,),
        )
        row = cur.fetchone()

        if row:
            return row["id"]

        # 新規ユーザーを作成
        cur.execute(
            """
            INSERT INTO users (line_user_id, display_name)
            VALUES (%s, %s)
            RETURNING id
            """,
            (line_user_id, display_name),
        )
        new_user = cur.fetchone()
        user_id = new_user["id"]

        # デフォルトの支払方法を挿入
        _insert_default_payment_methods(cur, user_id)

        conn.commit()
        logger.info(
            f"新規ユーザー登録: user_id={user_id}"
            f" display_name={display_name}"
        )

        return user_id

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_demo_user_id() -> int:
    """デモユーザーのuser_idを返す。

    Streamlit UIのデモモードで使用する。

    Returns:
        デモユーザーのusersテーブルのid。

    Raises:
        RuntimeError: デモユーザーが存在しない場合
            （init_db()が未実行の可能性）。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM users WHERE line_user_id = %s",
            (DEMO_USER_LINE_ID,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(
                "デモユーザーが見つかりません。init_db() を実行してください。"
            )
        return row["id"]
    finally:
        release_connection(conn)
