# scripts/seed.py
"""
ダミーデータ投入スクリプト

実行方法: python3 scripts/seed.py（プロジェクトルートから）
前提: docker compose up db でPostgreSQLが起動していること

接続先:
- ホストから実行する場合は localhost:5432 を使用する
- Docker内のサービス名(db)はコンテナ間通信用なのでホストからは使えない
- .envのDATABASE_URLを上書きしてから connection.py をインポートする
"""

import csv
import os
import sys
from pathlib import Path

# --- ホスト実行時の接続先設定 ---
# connection.py のインポート前に設定する必要がある
# .envにはDocker内用の接続先(db:5432)が書かれているが、
# ホストからはlocalhost:5432で接続する
os.environ["DATABASE_URL"] = (
    "postgresql://kakeibo:kakeibo_dev@localhost:5432/kakeibo"
)

# プロジェクトルートをsys.pathに追加（api.db.connectionをインポートするため）
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from api.db.connection import (
    get_connection,
    get_demo_user_id,
    init_db,
    release_connection,
)
from psycopg2.extras import execute_values

# データファイルのパス
TRANSACTIONS_CSV = BASE_DIR / "data" / "dummy" / "dummy_transactions.csv"
FIXED_EXPENSES_CSV = BASE_DIR / "data" / "dummy" / "dummy_fixed_expenses.csv"


def _read_csv(path: Path) -> list[dict]:
    """CSVファイルを読み込み、辞書のリストとして返す。

    空文字列はNoneに変換する（PostgreSQLのNULLに対応）。

    Args:
        path: CSVファイルのパス。

    Returns:
        各行を辞書にしたリスト。
    """
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append(
                {k: (v if v != "" else None) for k, v in row.items()}
            )
    return rows


def seed_transactions(conn, user_id: int) -> None:
    """既存の取引明細を削除し、CSVからダミーデータを投入する。

    Args:
        conn: PostgreSQLコネクション。
        user_id: デモユーザーのID。全レコードに付与する。
    """
    rows = _read_csv(TRANSACTIONS_CSV)

    cur = conn.cursor()
    cur.execute("DELETE FROM transactions")

    values = [
        (
            user_id,
            row["date"],
            row["type"],
            int(row["amount"]),
            row["category"],
            row["store_name"],
            row["item"],
            row["memo"],
            row["payment_method"],
            None,  # card_name（CSVに列がないためNULL）
            row["person"],
        )
        for row in rows
    ]

    execute_values(
        cur,
        """
        INSERT INTO transactions
            (user_id, date, type, amount, category, store_name,
             item, memo, payment_method, card_name, person)
        VALUES %s
        """,
        values,
    )

    print(f"transactions: {len(values)}件投入")


def seed_fixed_expenses(conn, user_id: int) -> None:
    """既存の固定費を削除し、CSVからダミーデータを投入する。

    CSVにないstart_dateとpayment_methodはデフォルト値を補完する。

    Args:
        conn: PostgreSQLコネクション。
        user_id: デモユーザーのID。全レコードに付与する。
    """
    rows = _read_csv(FIXED_EXPENSES_CSV)

    cur = conn.cursor()
    cur.execute("DELETE FROM fixed_expenses")

    values = [
        (
            user_id,
            row["name"],
            int(row["amount"]),
            row["category"],
            int(row["day_of_month"]),
            row.get("payment_method") or "口座振替",
            row.get("start_date") or "2024-01-01",
            row.get("end_date"),
        )
        for row in rows
    ]

    execute_values(
        cur,
        """
        INSERT INTO fixed_expenses
            (user_id, name, amount, category, day_of_month,
             payment_method, start_date, end_date)
        VALUES %s
        """,
        values,
    )

    print(f"fixed_expenses: {len(values)}件投入")


def main() -> None:
    """テーブル作成 → デモユーザー取得 → ダミーデータ投入。"""
    # テーブルとデモユーザーを作成（既に存在すればスキップ）
    init_db()

    user_id = get_demo_user_id()
    print(f"デモユーザー: user_id={user_id}")

    conn = get_connection()
    try:
        seed_transactions(conn, user_id)
        seed_fixed_expenses(conn, user_id)
        conn.commit()
        print("シード完了")
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


if __name__ == "__main__":
    main()
