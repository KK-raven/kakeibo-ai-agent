# scripts/seed.py
"""
ダミーデータ投入スクリプト
実行方法: python3 scripts/seed.py（プロジェクトルートから）
"""
import sqlite3
import pandas as pd
from pathlib import Path

# パス定義
BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "kakeibo.db"
TRANSACTIONS_CSV = BASE_DIR / "data" / "dummy" / "dummy_transactions.csv"
FIXED_EXPENSES_CSV = BASE_DIR / "data" / "dummy" / "dummy_fixed_expenses.csv"


def seed_transactions(conn: sqlite3.Connection) -> None:
    """既存の取引明細を削除し、CSVからダミーデータを投入する。

    Args:
        conn: SQLiteデータベース接続。
    """
    df = pd.read_csv(TRANSACTIONS_CSV)
    conn.execute("DELETE FROM transactions")
    df.to_sql("transactions", conn, if_exists="append", index=False)
    print(f"transactions: {len(df)}件投入")


def seed_fixed_expenses(conn: sqlite3.Connection) -> None:
    """既存の固定費を削除し、CSVからダミーデータを投入する。

    必須列（start_date、payment_method）が存在しない場合はデフォルト値を補完する。

    Args:
        conn: SQLiteデータベース接続。
    """
    df = pd.read_csv(FIXED_EXPENSES_CSV)
    # Phase 1生成のCSVにはない列をここで補完する
    # start_dateはNOT NULL制約あり。ダミーデータの開始日として2024-01-01を設定
    if "start_date" not in df.columns:
        df["start_date"] = "2024-01-01"
    # payment_methodはDEFAULT '口座振替'だが、to_sqlはDEFAULTを使わないため明示的に補完
    if "payment_method" not in df.columns:
        df["payment_method"] = "口座振替"
    conn.execute("DELETE FROM fixed_expenses")
    df.to_sql("fixed_expenses", conn, if_exists="append", index=False)
    print(f"fixed_expenses: {len(df)}件投入")


def main() -> None:
    if not DB_PATH.exists():
        print(f"エラー: DBファイルが見つかりません: {DB_PATH}")
        return
    with sqlite3.connect(DB_PATH) as conn:
        seed_transactions(conn)
        seed_fixed_expenses(conn)
        conn.commit()
    print("シード完了")


if __name__ == "__main__":
    main()
