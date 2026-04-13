# api/db/crud.py
"""
取引データのCRUD操作モジュール

全関数がuser_idを第一引数に取り、ユーザーごとのデータ分離を保証する。
"""

import calendar
from datetime import date

from api.db.connection import get_connection, release_connection
from api.utils.business_day import next_business_day
from api.utils.logger import get_logger

logger = get_logger(__name__)


def register_transaction(
    user_id: int,
    date: str,
    type: str,
    amount: int,
    category: str,
    store_name: str | None = None,
    item: str | None = None,
    memo: str | None = None,
    payment_method: str | None = None,
    card_name: str | None = None,
    person: str = "自分",
) -> dict:
    """取引を1件登録し、登録されたレコードを返す。

    処理の流れ:
    1. transactions テーブルに INSERT
    2. store_name がある場合、store_category_mapping を UPSERT
    3. 登録されたレコードを SELECT して返す

    取引登録とマッピング更新は常にセットで行うべき処理のため、
    同一トランザクション内で実行する。

    Args:
        user_id: ユーザーID。
        date: 取引日（"YYYY-MM-DD" 形式）。
        type: "income" or "expense"。
        amount: 金額（正の整数）。
        category: カテゴリ名（"食費", "給与" 等）。
        store_name: 店名（任意）。
        item: 品目（任意）。
        memo: メモ（任意）。
        payment_method: 支払方法（デフォルト "現金"）。
        card_name: クレジットカード名（任意）。
        person: 誰の取引か（デフォルト "自分"）。

    Returns:
        登録されたレコードの辞書（id を含む）。
    """
    conn = get_connection()

    # 収入には支払方法の概念がないため "-" を格納する。
    # 支出でpayment_methodが省略された場合は "現金" をデフォルトとする。
    # （DB列はNOT NULL制約のため、Noneのまま渡せない）
    if type == "income":
        payment_method = "-"
    elif payment_method is None:
        payment_method = "現金"

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO transactions
                (user_id, date, type, amount, category, store_name, item,
                 memo, payment_method, card_name, person)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id, date, type, amount, category, store_name, item,
                memo, payment_method, card_name, person,
            ),
        )
        transaction_id = cur.fetchone()["id"]

        if store_name:
            cur.execute(
                """
                INSERT INTO store_category_mapping
                    (user_id, store_name, category, count, last_used)
                VALUES (%s, %s, %s, 1, %s)
                ON CONFLICT (user_id, store_name, category) DO UPDATE SET
                    count = store_category_mapping.count + 1,
                    last_used = %s
                """,
                (user_id, store_name, category, date, date),
            )

        conn.commit()
        logger.info(
            f"取引登録: {date} {type} {category} {amount}円"
            f" store={store_name} item={item}"
            f" method={payment_method} card={card_name}"
        )

        cur.execute(
            "SELECT * FROM transactions WHERE id = %s",
            (transaction_id,),
        )
        return dict(cur.fetchone())

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_transactions(
    user_id: int,
    year_month: str | None = None,
    category: str | None = None,
    person: str | None = None,
    type: str | None = None,
    store_name: str | None = None,
    item: str | None = None,
    payment_method: str | None = None,
    amount: int | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> list[dict]:
    """取引一覧を取得する。条件を指定すると絞り込める。

    各引数が None でなければ WHERE 句に条件を追加する。
    複数指定すると AND で結合される。
    year_month と start_month/end_month はどちらか一方を指定する。
    両方指定された場合は start_month/end_month を優先する。
    store_name / item はLIKEで部分一致検索。
    ユーザーが「セブン」と略して言う場合や、
    「雪見」だけで「雪見だいふく」を探す場合に対応するため。
    payment_method は完全一致検索。
    amount は完全一致検索。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式（例: "2025-04"）。
        category: カテゴリ名で絞り込み。
        person: person で絞り込み。
        type: "income" or "expense" で絞り込み。
        store_name: 店名で部分一致検索。
        item: 品目で部分一致検索。
        payment_method: 支払方法で完全一致検索。
        amount: 金額で完全一致検索。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。

    Returns:
        該当レコードのリスト（辞書のリスト）。日付降順。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        conditions = ["user_id = %s"]
        params = [user_id]

        if start_month and end_month:
            conditions.append("to_char(date, 'YYYY-MM') >= %s")
            conditions.append("to_char(date, 'YYYY-MM') <= %s")
            params.extend([start_month, end_month])
        elif year_month:
            conditions.append("to_char(date, 'YYYY-MM') = %s")
            params.append(year_month)

        if category:
            conditions.append("category = %s")
            params.append(category)
        if person:
            conditions.append("person = %s")
            params.append(person)
        if type:
            conditions.append("type = %s")
            params.append(type)
        if store_name:
            conditions.append("store_name LIKE %s")
            params.append(f"%{store_name}%")
        if item:
            conditions.append("item LIKE %s")
            params.append(f"%{item}%")
        if payment_method:
            conditions.append("payment_method = %s")
            params.append(payment_method)
        if amount is not None:
            conditions.append("amount = %s")
            params.append(amount)

        where_clause = "WHERE " + " AND ".join(conditions)
        cur.execute(
            f"""
            SELECT * FROM transactions
            {where_clause}
            ORDER BY date DESC, id DESC
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_transaction(user_id: int, transaction_id: int) -> bool:
    """取引を1件削除する。

    user_idとidの両方で特定することで、
    他ユーザーの取引を削除できないようにする。

    store_category_mapping の count は減算しない。
    マッピングは「傾向の推定」用途なので厳密な整合性より
    シンプルさを優先。削除は稀な操作なので傾向への影響は軽微。

    Args:
        user_id: ユーザーID。
        transaction_id: 削除する取引の id。

    Returns:
        True: 削除成功（1件削除された）。
        False: 該当 id が存在しなかった。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s",
            (transaction_id, user_id),
        )
        conn.commit()

        if cur.rowcount > 0:
            logger.info(f"取引削除: id={transaction_id}")
            return True
        else:
            logger.warning(
                f"取引削除失敗: id={transaction_id} が存在しません"
            )
            return False

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_transaction(
    user_id: int,
    transaction_id: int,
    **kwargs,
) -> dict:
    """取引を1件更新する。

    指定されたフィールドのみを更新し、指定されていないフィールドは
    元の値を維持する。部分更新（PATCH的な挙動）。

    user_idとidの両方で特定することで、
    他ユーザーの取引を更新できないようにする。

    Args:
        user_id: ユーザーID。
        transaction_id: 更新する取引のID。
        **kwargs: 更新するフィールド名と値。
            指定可能: date, type, amount, category, store_name,
                      item, memo, payment_method, card_name, person

    Returns:
        更新後の取引の辞書。
        該当IDが存在しない場合は {"error": "..."} を返す。
    """
    # 更新可能なカラムをホワイトリストで制限
    # 任意のカラム名を受け付けるとSQLインジェクションのリスクがあるため
    UPDATABLE_COLUMNS = {
        "date", "type", "amount", "category", "store_name",
        "item", "memo", "payment_method", "card_name", "person",
    }

    updates = {k: v for k, v in kwargs.items() if k in UPDATABLE_COLUMNS}

    if not updates:
        return {"error": "更新するフィールドが指定されていません"}

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM transactions WHERE id = %s AND user_id = %s",
            (transaction_id, user_id),
        )
        if not cur.fetchone():
            return {"error": f"ID {transaction_id} の取引が見つかりません"}

        set_clause = ", ".join(f"{col} = %s" for col in updates.keys())
        values = list(updates.values())
        values.extend([transaction_id, user_id])

        cur.execute(
            f"""
            UPDATE transactions SET {set_clause}
            WHERE id = %s AND user_id = %s
            """,
            values,
        )
        conn.commit()

        logger.info(
            f"取引更新: id={transaction_id} "
            f"更新項目={list(updates.keys())}"
        )

        cur.execute(
            "SELECT * FROM transactions WHERE id = %s",
            (transaction_id,),
        )
        return dict(cur.fetchone())

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def register_fixed_expense(
    user_id: int,
    name: str,
    amount: int,
    category: str,
    day_of_month: int,
    start_date: str,
    payment_method: str = "口座振替",
    end_date: str | None = None,
) -> dict:
    """固定出金を1件登録する。

    固定出金とは毎月決まった金額が発生する支出のこと。
    例: 家賃、Netflix、電気代の基本料金など。

    このテーブルはマスタデータ（設定情報）であり、
    実際の支出記録（transactions）とは別。
    apply_fixed_expenses() で transactions に反映する。

    Args:
        user_id: ユーザーID。
        name: 固定出金の名称（"家賃", "Netflix" 等）。
        amount: 金額。
        category: カテゴリ（"家賃/住居", "サブスク" 等）。
        day_of_month: 毎月の計上日（1〜31）。
        start_date: 開始日（"YYYY-MM-DD"）。
        payment_method: 支払方法（デフォルト "口座振替"）。
        end_date: 終了日（None なら継続中）。

    Returns:
        登録されたレコードの辞書。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO fixed_expenses
                (user_id, name, amount, category, day_of_month,
                 payment_method, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                user_id, name, amount, category, day_of_month,
                payment_method, start_date, end_date,
            ),
        )
        row = cur.fetchone()
        conn.commit()

        logger.info(
            f"固定出金登録: {name} {amount}円 {category}"
            f" 毎月{day_of_month}日 method={payment_method}"
        )
        return dict(row)

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_fixed_expenses(
    user_id: int,
    active_only: bool = True,
) -> list[dict]:
    """固定出金の一覧を取得する。

    Args:
        user_id: ユーザーID。
        active_only: True なら is_active=1 のみ。
                     False なら無効化されたものも含む全件。

    Returns:
        固定出金レコードのリスト。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        if active_only:
            cur.execute(
                """
                SELECT * FROM fixed_expenses
                WHERE user_id = %s AND is_active = 1
                """,
                (user_id,),
            )
        else:
            cur.execute(
                "SELECT * FROM fixed_expenses WHERE user_id = %s",
                (user_id,),
            )
        return [dict(row) for row in cur.fetchall()]

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def apply_fixed_expenses(
    user_id: int,
    year: int,
    month: int,
) -> list[dict]:
    """指定した年月の固定出金を transactions に自動計上する。

    処理の流れ:
    1. fixed_expenses から有効なレコードを取得
    2. 各レコードについて、有効期間内かチェック
    3. 計上日を決定（月末フォールバック + 平日調整）
    4. 該当月に既に計上済みかチェック（重複防止）
    5. 未計上のものだけ transactions に INSERT

    計上日の決定ロジック:
    - day_of_month が月の末日を超える場合、末日にフォールバック
    - 口座振替の場合、土日祝なら翌平日に調整
      （銀行の口座振替は営業日に処理されるため）
    - 現金やクレカの場合は平日調整しない

    計上済みの判定:
    - memo に "[固定] {name}" を入れて手動登録と区別する
    - 同じ年月に同じ memo の取引があれば計上済みとみなす

    Args:
        user_id: ユーザーID。
        year: 年（例: 2025）。
        month: 月（例: 4）。

    Returns:
        今回新たに計上されたレコードのリスト。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT * FROM fixed_expenses
            WHERE user_id = %s AND is_active = 1
            """,
            (user_id,),
        )
        fixed_list = cur.fetchall()

        year_month = f"{year:04d}-{month:02d}"
        last_day = calendar.monthrange(year, month)[1]

        added = []

        for fe in fixed_list:
            # --- 有効期間チェック ---
            if fe["start_date"].isoformat() > f"{year_month}-31":
                continue
            if (
                fe["end_date"]
                and fe["end_date"].isoformat() < f"{year_month}-01"
            ):
                continue

            # --- 計上日の決定 ---
            actual_day = min(fe["day_of_month"], last_day)
            tx_date = date(year, month, actual_day)

            # 口座振替の場合のみ平日調整
            if fe["payment_method"] == "口座振替":
                tx_date = next_business_day(tx_date)

            tx_date_str = tx_date.isoformat()

            # --- 計上済みチェック ---
            memo = f"[固定] {fe['name']}"
            cur.execute(
                """
                SELECT id FROM transactions
                WHERE user_id = %s
                    AND to_char(date, 'YYYY-MM') = %s
                    AND memo = %s
                """,
                (user_id, year_month, memo),
            )

            if cur.fetchone():
                logger.debug(
                    f"固定出金スキップ（計上済み）: {fe['name']} {year_month}"
                )
                continue

            # --- 計上 ---
            cur.execute(
                """
                INSERT INTO transactions
                    (user_id, date, type, amount, category, item, memo,
                     payment_method, person)
                VALUES (%s, %s, 'expense', %s, %s, %s, %s, %s, '自分')
                RETURNING *
                """,
                (
                    user_id, tx_date_str, fe["amount"], fe["category"],
                    fe["name"], memo, fe["payment_method"],
                ),
            )
            row = cur.fetchone()
            added.append(dict(row))

            logger.info(
                f"固定出金計上: {fe['name']} {fe['amount']}円"
                f" → {tx_date_str} ({fe['payment_method']})"
            )

        conn.commit()
        return added

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def deactivate_fixed_expense(
    user_id: int,
    fixed_expense_id: int,
) -> dict:
    """固定出金を無効化する。

    DELETEではなく is_active=0 にする論理削除。
    end_date に今日の日付を設定する。
    履歴を残す設計のため、レコード自体は削除しない。

    Args:
        user_id: ユーザーID。
        fixed_expense_id: 無効化する固定出金のID。

    Returns:
        無効化した固定出金の辞書。
        該当IDが存在しない場合や既に無効化済みの場合は
        {"error": "..."} を返す。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT * FROM fixed_expenses
            WHERE id = %s AND user_id = %s
            """,
            (fixed_expense_id, user_id),
        )
        row = cur.fetchone()

        if not row:
            return {
                "error": f"ID {fixed_expense_id} の固定出金が見つかりません"
            }

        if not row["is_active"]:
            return {
                "error": f"ID {fixed_expense_id} は既に無効化されています"
            }

        today = date.today().isoformat()
        cur.execute(
            """
            UPDATE fixed_expenses
            SET is_active = 0, end_date = %s
            WHERE id = %s AND user_id = %s
            """,
            (today, fixed_expense_id, user_id),
        )
        conn.commit()

        logger.info(
            f"固定出金無効化: ID={fixed_expense_id} {row['name']}"
        )

        cur.execute(
            "SELECT * FROM fixed_expenses WHERE id = %s",
            (fixed_expense_id,),
        )
        return dict(cur.fetchone())

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_payment_methods(
    user_id: int,
    category: str | None = None,
) -> list[dict]:
    """支払方法の一覧を取得する。

    Args:
        user_id: ユーザーID。
        category: "現金" or "非現金" で絞り込み。None なら全件。

    Returns:
        支払方法レコードのリスト。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        if category:
            cur.execute(
                """
                SELECT * FROM payment_methods
                WHERE user_id = %s AND category = %s
                """,
                (user_id, category),
            )
        else:
            cur.execute(
                "SELECT * FROM payment_methods WHERE user_id = %s",
                (user_id,),
            )
        return [dict(row) for row in cur.fetchall()]

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def add_payment_method(
    user_id: int,
    name: str,
    category: str = "非現金",
    linked_card: str | None = None,
) -> dict:
    """新しい支払方法を追加する。

    Agent がユーザーに二重確認を取った上で呼ぶ想定。
    例: 「楽天ペイを新しい支払方法として登録しますか？」→「はい」

    Args:
        user_id: ユーザーID。
        name: 支払方法名（"楽天ペイ" 等）。
        category: "現金" or "非現金"（ほぼ全て非現金）。
        linked_card: 決済元カード名（任意）。

    Returns:
        追加されたレコードの辞書。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO payment_methods
                (user_id, name, category, linked_card)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, name, category, linked_card),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_payment_method_linked_card(
    user_id: int,
    name: str,
    linked_card: str,
) -> bool:
    """支払方法の決済元（linked_card）を設定・変更する。

    用途: QUICPayの決済元をJCBに設定する等。
    Agent が初回使用時に「QUICPayの決済元はどのカードですか？」と
    確認し、回答を受けてこの関数を呼ぶ想定。

    Args:
        user_id: ユーザーID。
        name: 支払方法名（"QUICPay" 等）。
        linked_card: 決済元カード名（"JCB" 等）。

    Returns:
        True: 更新成功。
        False: 該当する支払方法が存在しなかった。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE payment_methods
            SET linked_card = %s
            WHERE user_id = %s AND name = %s
            """,
            (linked_card, user_id, name),
        )
        conn.commit()

        if cur.rowcount > 0:
            logger.info(f"決済元設定: {name} → {linked_card}")
            return True
        else:
            logger.warning(f"決済元設定失敗: {name} が存在しません")
            return False

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def register_credit_card(
    user_id: int,
    name: str,
    billing_close_day: int | None = None,
    payment_day: int | None = None,
) -> dict:
    """クレジットカードを登録する。

    そのユーザーの1枚目のカードは自動的にデフォルト（is_default=1）。
    2枚目以降は is_default=0。

    Args:
        user_id: ユーザーID。
        name: カード名（"JCB", "三井住友VISA" 等）。
        billing_close_day: 締め日（1〜31。NULLなら未設定）。
        payment_day: 引き落とし日（1〜31。NULLなら未設定）。

    Returns:
        登録されたレコードの辞書。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # そのユーザーのカードが1枚もなければデフォルトにする
        cur.execute(
            "SELECT COUNT(*) as cnt FROM credit_cards WHERE user_id = %s",
            (user_id,),
        )
        existing = cur.fetchone()
        is_default = 1 if existing["cnt"] == 0 else 0

        cur.execute(
            """
            INSERT INTO credit_cards
                (user_id, name, billing_close_day, payment_day, is_default)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, name, billing_close_day, payment_day, is_default),
        )
        row = cur.fetchone()
        conn.commit()

        default_str = "デフォルト" if is_default else ""
        logger.info(f"カード登録: {name} {default_str}")

        return dict(row)

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_credit_cards(user_id: int) -> list[dict]:
    """クレジットカードの一覧を取得する。

    デフォルトカードが先頭に来るよう is_default DESC でソート。

    Args:
        user_id: ユーザーID。

    Returns:
        クレジットカードレコードのリスト。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM credit_cards
            WHERE user_id = %s
            ORDER BY is_default DESC, id ASC
            """,
            (user_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def set_budget(
    user_id: int,
    category: str,
    amount: int,
    person: str = "自分",
    alert_threshold: int | None = None,
) -> dict:
    """カテゴリ別の月額予算を設定する。

    既に同じ user_id × category × person の予算があれば
    金額を更新する（UPSERT）。新規なら INSERT する。

    Args:
        user_id: ユーザーID。
        category: カテゴリ名（"食費", "交際費" 等）。
        amount: 月額予算（正の整数）。
        person: 誰の予算か（デフォルト "自分"）。
        alert_threshold: 残額警告の閾値（None なら警告なし）。

    Returns:
        設定された予算レコードの辞書。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO budgets
                (user_id, category, amount, alert_threshold, person)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, category, person) DO UPDATE SET
                amount = %s,
                alert_threshold = %s,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                user_id, category, amount, alert_threshold, person,
                amount, alert_threshold,
            ),
        )
        conn.commit()

        threshold_str = (
            f" 閾値{alert_threshold}円" if alert_threshold else ""
        )
        logger.info(
            f"予算設定: {person} {category} {amount}円{threshold_str}"
        )

        cur.execute(
            """
            SELECT * FROM budgets
            WHERE user_id = %s AND category = %s AND person = %s
            """,
            (user_id, category, person),
        )
        return dict(cur.fetchone())

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_budgets(
    user_id: int,
    person: str = "自分",
) -> list[dict]:
    """予算一覧を取得する。

    Args:
        user_id: ユーザーID。
        person: 誰の予算か（デフォルト "自分"）。

    Returns:
        予算レコードのリスト。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM budgets
            WHERE user_id = %s AND person = %s
            ORDER BY category
            """,
            (user_id, person),
        )
        return [dict(row) for row in cur.fetchall()]

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def check_budget(
    user_id: int,
    category: str,
    year_month: str,
    person: str = "自分",
) -> dict | None:
    """特定カテゴリの予算と当月支出を比較する。

    alert_level の3段階:
    - "ok": 残額が閾値以上（or 閾値未設定）。通知不要
    - "warning": 残額が閾値未満だが超過していない
    - "over": 予算超過

    予算が設定されていないカテゴリの場合は None を返す。

    Args:
        user_id: ユーザーID。
        category: カテゴリ名。
        year_month: "YYYY-MM" 形式。
        person: 誰の予算か。

    Returns:
        予算チェック結果の辞書。予算未設定の場合は None。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT amount, alert_threshold FROM budgets
            WHERE user_id = %s AND category = %s AND person = %s
            """,
            (user_id, category, person),
        )
        budget_row = cur.fetchone()

        if not budget_row:
            logger.debug(f"予算未設定: {person} {category}")
            return None

        budget_amount = budget_row["amount"]
        threshold = budget_row["alert_threshold"]

        # COALESCE: 該当月に1件も支出がない場合、SUMはNULLを返す
        # COALESCE(NULL, 0) で 0 に変換する
        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE user_id = %s
                AND to_char(date, 'YYYY-MM') = %s
                AND category = %s
                AND person = %s
                AND type = 'expense'
            """,
            (user_id, year_month, category, person),
        )
        spent = cur.fetchone()["total"]
        remaining = budget_amount - spent

        if remaining < 0:
            alert_level = "over"
            logger.warning(
                f"予算超過: {person} {category}"
                f" 予算{budget_amount}円 支出{spent}円"
                f" ({abs(remaining)}円超過)"
            )
        elif threshold and remaining < threshold:
            alert_level = "warning"
            logger.info(
                f"予算警告: {person} {category}"
                f" 残{remaining}円（閾値{threshold}円）"
            )
        else:
            alert_level = "ok"

        return {
            "category": category,
            "budget": budget_amount,
            "spent": spent,
            "remaining": remaining,
            "is_over": remaining < 0,
            "alert_level": alert_level,
            "alert_threshold": threshold,
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_monthly_summary(
    user_id: int,
    year_month: str,
    person: str = "自分",
) -> dict:
    """月次の収入合計・支出合計・差額を返す。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。
        person: 誰の集計か。

    Returns:
        {"year_month": "2025-04", "income": 250000,
         "expense": 180000, "balance": 70000}
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE user_id = %s
                AND to_char(date, 'YYYY-MM') = %s
                AND person = %s
                AND type = 'income'
            """,
            (user_id, year_month, person),
        )
        income = cur.fetchone()["total"]

        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE user_id = %s
                AND to_char(date, 'YYYY-MM') = %s
                AND person = %s
                AND type = 'expense'
            """,
            (user_id, year_month, person),
        )
        expense = cur.fetchone()["total"]

        logger.info(
            f"月次集計: {year_month} {person}"
            f" 収入{income}円 支出{expense}円 差額{income - expense}円"
        )

        return {
            "year_month": year_month,
            "income": income,
            "expense": expense,
            "balance": income - expense,
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_category_summary(
    user_id: int,
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
) -> list[dict]:
    """カテゴリ別の集計を返す。金額が大きい順。

    year_month と start_month/end_month はどちらか一方を指定する。
    両方指定された場合は start_month/end_month を優先する。
    どちらも指定しない場合は全期間を集計する。

    fixed_modeで固定費（memo が '[固定]' で始まる取引）の扱いを制御する。
    show:  固定費を通常通り集計する（デフォルト）。
    group: 固定費を「固定費」カテゴリに統合して集計する。
    hide:  固定費を集計から除外する。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。

    Returns:
        [{"category": "食費", "total": 35000, "count": 12}, ...]
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        conditions = [
            "user_id = %s",
            "type = %s",
            "person = %s",
        ]
        params = [user_id, type, person]

        if start_month and end_month:
            conditions.append("to_char(date, 'YYYY-MM') >= %s")
            conditions.append("to_char(date, 'YYYY-MM') <= %s")
            params.extend([start_month, end_month])
        elif year_month:
            conditions.append("to_char(date, 'YYYY-MM') = %s")
            params.append(year_month)

        if fixed_mode == "hide":
            conditions.append("(memo IS NULL OR memo NOT LIKE '[固定]%')")

        where_clause = "WHERE " + " AND ".join(conditions)

        # group モードでは固定費を「固定費」カテゴリに統合する。
        # CASE式でmemoが '[固定]' で始まる取引のcategoryを上書きする。
        category_expr = (
            "CASE WHEN memo LIKE '[固定]%' THEN '固定費' ELSE category END"
            if fixed_mode == "group"
            else "category"
        )

        cur.execute(
            f"""
            SELECT
                {category_expr} as category,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY {category_expr}
            ORDER BY total DESC
            """,
            params,
        )
        result = [dict(row) for row in cur.fetchall()]
        logger.info(
            f"カテゴリ集計: "
            f"{year_month or f'{start_month}〜{end_month}'}"
            f" {type} {person} fixed={fixed_mode} {len(result)}カテゴリ"
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_monthly_comparison(
    user_id: int,
    year_month: str,
    compare_to: str | None = None,
    person: str = "自分",
) -> list[dict]:
    """指定月と比較対象月のカテゴリ別支出を比較する。

    compare_to を省略すると前月と比較する。
    明示的に指定すると任意の月と比較できる。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式（当月）。
        compare_to: "YYYY-MM" 形式（比較対象月）。None なら前月。
        person: 誰の集計か。

    Returns:
        カテゴリごとの比較結果リスト。差額の絶対値が大きい順。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        if compare_to:
            prev_year_month = compare_to
        else:
            year = int(year_month[:4])
            month = int(year_month[5:7])
            if month == 1:
                prev_year = year - 1
                prev_month = 12
            else:
                prev_year = year
                prev_month = month - 1
            prev_year_month = f"{prev_year:04d}-{prev_month:02d}"

        cur.execute(
            """
            SELECT category, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s
                AND to_char(date, 'YYYY-MM') = %s
                AND type = 'expense'
                AND person = %s
            GROUP BY category
            """,
            (user_id, year_month, person),
        )
        current_dict = {
            row["category"]: row["total"] for row in cur.fetchall()
        }

        cur.execute(
            """
            SELECT category, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s
                AND to_char(date, 'YYYY-MM') = %s
                AND type = 'expense'
                AND person = %s
            GROUP BY category
            """,
            (user_id, prev_year_month, person),
        )
        prev_dict = {
            row["category"]: row["total"] for row in cur.fetchall()
        }

        all_categories = set(current_dict.keys()) | set(prev_dict.keys())

        result = []
        for cat in all_categories:
            current = current_dict.get(cat, 0)
            previous = prev_dict.get(cat, 0)
            diff = current - previous

            if previous > 0:
                change_rate = round((diff / previous) * 100, 1)
            else:
                change_rate = None

            result.append({
                "category": cat,
                "current": current,
                "previous": previous,
                "diff": diff,
                "change_rate": change_rate,
            })

        result.sort(key=lambda x: abs(x["diff"]), reverse=True)

        logger.info(
            f"月比較: {year_month} vs {prev_year_month} {person}"
            f" {len(result)}カテゴリ"
        )
        return result

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_custom_summary(
    user_id: int,
    year_month: str,
    group_by: str = "category",
    type: str = "expense",
    person: str = "自分",
    category: str | None = None,
    store_name: str | None = None,
    item: str | None = None,
    payment_method: str | None = None,
) -> list[dict]:
    """柔軟な集計軸とフィルタで集計する汎用関数。

    group_by で集計軸を指定し、フィルタ引数で絞り込む。
    例:
    - 「セブンイレブンでいくら使った？」
      → group_by="store_name", store_name="セブン"
    - 「支払方法別の支出は？」
      → group_by="payment_method"

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。
        group_by: 集計軸（"category", "store_name", "item",
                  "payment_method", "person"）。
        type: "expense" or "income"。
        person: 誰の集計か。
        category: カテゴリで絞り込み（完全一致）。
        store_name: 店名で絞り込み（部分一致）。
        item: 品目で絞り込み（部分一致）。
        payment_method: 支払方法で絞り込み（完全一致）。

    Returns:
        [{"key": "セブンイレブン", "total": 12000, "count": 5}, ...]
        金額が大きい順にソート。
    """
    # SQLインジェクション対策: 許可するカラム名のみ受け付ける
    allowed_group_by = {
        "category", "store_name", "item",
        "payment_method", "person",
    }
    if group_by not in allowed_group_by:
        raise ValueError(
            f"group_by は {allowed_group_by} のいずれかを指定してください。"
            f" '{group_by}' は許可されていません。"
        )

    conn = get_connection()
    try:
        cur = conn.cursor()

        conditions = [
            "user_id = %s",
            "to_char(date, 'YYYY-MM') = %s",
            "type = %s",
            "person = %s",
        ]
        params = [user_id, year_month, type, person]

        if category:
            conditions.append("category = %s")
            params.append(category)
        if store_name:
            conditions.append("store_name LIKE %s")
            params.append(f"%{store_name}%")
        if item:
            conditions.append("item LIKE %s")
            params.append(f"%{item}%")
        if payment_method:
            conditions.append("payment_method = %s")
            params.append(payment_method)

        where_clause = "WHERE " + " AND ".join(conditions)

        # group_by はホワイトリストで検証済みなので
        # f-string で直接埋め込んでも安全
        cur.execute(
            f"""
            SELECT
                {group_by} as key,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY {group_by}
            ORDER BY total DESC
            """,
            params,
        )
        result = [dict(row) for row in cur.fetchall()]

        logger.info(
            f"カスタム集計: {year_month} group_by={group_by}"
            f" {type} {person} → {len(result)}件"
        )
        return result

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_setting(user_id: int, key: str) -> str | None:
    """設定値を取得する。

    settingsテーブルから指定したkeyの値を返す。
    存在しなければNoneを返す。

    Args:
        user_id: ユーザーID。
        key: 設定キー（"default_payment_method" 等）。

    Returns:
        設定値の文字列。未設定ならNone。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = %s",
            (user_id, key),
        )
        row = cur.fetchone()
        return row["value"] if row else None

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def set_setting(user_id: int, key: str, value: str) -> dict:
    """設定値を保存する。

    既に同じuser_id × keyが存在すればvalueを上書きする（UPSERT）。
    新規ならINSERTする。

    Args:
        user_id: ユーザーID。
        key: 設定キー。
        value: 設定値（文字列）。

    Returns:
        {"key": key, "value": value}
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settings (user_id, key, value, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, key) DO UPDATE SET
                value = %s,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, key, value, value),
        )
        conn.commit()

        logger.info(f"設定変更: {key} = {value}")
        return {"key": key, "value": value}

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_store_summary(
    user_id: int,
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    store_name: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
) -> list[dict]:
    """店別の集計を返す。金額が大きい順。

    store_nameを指定すると特定店舗に絞り込める。
    指定しない場合は全店舗の集計を返す。
    year_month と start_month/end_month はどちらか一方を指定する。
    両方指定された場合は start_month/end_month を優先する。
    どちらも指定しない場合は全期間を集計する。

    fixed_modeで固定費（memo が '[固定]' で始まる取引）の扱いを制御する。
    show:  固定費を通常通り集計する（デフォルト）。
    group: 固定費の店名を「固定費」に統合して集計する。
    hide:  固定費を集計から除外する。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        store_name: 店名で部分一致絞り込み（省略時は全店舗）。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。

    Returns:
        [{"store_name": "セブンイレブン", "total": 15000, "count": 8}, ...]
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        conditions = [
            "user_id = %s",
            "type = %s",
            "person = %s",
            "store_name IS NOT NULL",
        ]
        params = [user_id, type, person]

        if start_month and end_month:
            conditions.append("to_char(date, 'YYYY-MM') >= %s")
            conditions.append("to_char(date, 'YYYY-MM') <= %s")
            params.extend([start_month, end_month])
        elif year_month:
            conditions.append("to_char(date, 'YYYY-MM') = %s")
            params.append(year_month)

        if store_name:
            conditions.append("store_name LIKE %s")
            params.append(f"%{store_name}%")

        if fixed_mode == "hide":
            conditions.append("(memo IS NULL OR memo NOT LIKE '[固定]%')")

        where_clause = "WHERE " + " AND ".join(conditions)

        store_expr = (
            "CASE WHEN memo LIKE '[固定]%' THEN '固定費' ELSE store_name END"
            if fixed_mode == "group"
            else "store_name"
        )

        cur.execute(
            f"""
            SELECT
                {store_expr} as store_name,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY {store_expr}
            ORDER BY total DESC
            """,
            params,
        )
        result = [dict(row) for row in cur.fetchall()]
        logger.info(
            f"店別集計: "
            f"{year_month or f'{start_month}〜{end_month}'}"
            f" {type} {person} fixed={fixed_mode} {len(result)}店舗"
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_item_summary(
    user_id: int,
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    item: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
) -> list[dict]:
    """品目別の集計を返す。金額が大きい順。

    itemを指定すると特定品目に絞り込める。
    指定しない場合は全品目の集計を返す。
    year_month と start_month/end_month はどちらか一方を指定する。
    両方指定された場合は start_month/end_month を優先する。
    どちらも指定しない場合は全期間を集計する。

    fixed_modeで固定費（memo が '[固定]' で始まる取引）の扱いを制御する。
    show:  固定費を通常通り集計する（デフォルト）。itemがNULLの取引は除外。
    group: 固定費を「固定費」品目に統合して集計する。
           固定費はitemがNULLのケースが多いため、このモードのみ
           item IS NOT NULL 条件を外して固定費も集計対象に含める。
    hide:  固定費を集計から除外する。itemがNULLの取引は除外。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        item: 品目で部分一致絞り込み（省略時は全品目）。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。

    Returns:
        [{"item": "弁当", "total": 8000, "count": 12}, ...]
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        conditions = [
            "user_id = %s",
            "type = %s",
            "person = %s",
        ]
        # group モードでは固定費（item が NULL のケースが多い）も
        # 「固定費」として集計するため、item IS NOT NULL 条件を外す。
        # show / hide モードでは item が NULL の取引は集計対象外とする。
        if fixed_mode != "group":
            conditions.append("item IS NOT NULL")
        params = [user_id, type, person]

        if start_month and end_month:
            conditions.append("to_char(date, 'YYYY-MM') >= %s")
            conditions.append("to_char(date, 'YYYY-MM') <= %s")
            params.extend([start_month, end_month])
        elif year_month:
            conditions.append("to_char(date, 'YYYY-MM') = %s")
            params.append(year_month)

        if item:
            conditions.append("item LIKE %s")
            params.append(f"%{item}%")

        if fixed_mode == "hide":
            conditions.append("(memo IS NULL OR memo NOT LIKE '[固定]%')")

        where_clause = "WHERE " + " AND ".join(conditions)

        item_expr = (
            "CASE WHEN memo LIKE '[固定]%' THEN '固定費' ELSE item END"
            if fixed_mode == "group"
            else "item"
        )

        cur.execute(
            f"""
            SELECT
                {item_expr} as item,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY {item_expr}
            ORDER BY total DESC
            """,
            params,
        )
        result = [dict(row) for row in cur.fetchall()]
        logger.info(
            f"品目別集計: "
            f"{year_month or f'{start_month}〜{end_month}'}"
            f" {type} {person} fixed={fixed_mode} {len(result)}品目"
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_payment_method_summary(
    user_id: int,
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
) -> list[dict]:
    """支払方法別の集計を返す。金額が大きい順。

    year_month と start_month/end_month はどちらか一方を指定する。
    両方指定された場合は start_month/end_month を優先する。
    どちらも指定しない場合は全期間を集計する。

    fixed_modeで固定費（memo が '[固定]' で始まる取引）の扱いを制御する。
    show:  固定費を通常通り集計する（デフォルト）。
    group: 固定費の支払方法を「固定費」に統合して集計する。
    hide:  固定費を集計から除外する。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。

    Returns:
        [{"payment_method": "現金", "total": 15000, "count": 8}, ...]
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        conditions = [
            "user_id = %s",
            "type = %s",
            "person = %s",
        ]
        params = [user_id, type, person]

        if start_month and end_month:
            conditions.append("to_char(date, 'YYYY-MM') >= %s")
            conditions.append("to_char(date, 'YYYY-MM') <= %s")
            params.extend([start_month, end_month])
        elif year_month:
            conditions.append("to_char(date, 'YYYY-MM') = %s")
            params.append(year_month)

        if fixed_mode == "hide":
            conditions.append("(memo IS NULL OR memo NOT LIKE '[固定]%')")

        where_clause = "WHERE " + " AND ".join(conditions)

        payment_expr = (
            "CASE WHEN memo LIKE '[固定]%' THEN '固定費' ELSE payment_method END"
            if fixed_mode == "group"
            else "payment_method"
        )

        cur.execute(
            f"""
            SELECT
                {payment_expr} as payment_method,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY {payment_expr}
            ORDER BY total DESC
            """,
            params,
        )
        result = [dict(row) for row in cur.fetchall()]
        logger.info(
            f"支払方法別集計: "
            f"{year_month or f'{start_month}〜{end_month}'}"
            f" {type} {person} fixed={fixed_mode} {len(result)}種類"
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_monthly_trend(
    user_id: int,
    start_month: str,
    end_month: str,
    type: str = "expense",
    person: str = "自分",
    fixed_mode: str = "show",
) -> list[dict]:
    """月別推移を返す。カテゴリ別の積み上げと月合計を含む。

    start_month〜end_monthの範囲で月ごとに集計する。
    グラフ描画側でカテゴリを積み上げて表示するため、
    month・category・totalの3列を返す。

    fixed_modeで固定費（memo が '[固定]' で始まる取引）の扱いを制御する。
    show:  固定費を通常通り集計する（デフォルト）。
    group: 固定費を「固定費」カテゴリに統合して集計する。
    hide:  固定費を集計から除外する。

    Args:
        user_id: ユーザーID。
        start_month: 開始月 "YYYY-MM" 形式。
        end_month: 終了月 "YYYY-MM" 形式。
        type: "expense" or "income"。
        person: 誰の集計か。
        fixed_mode: "show" / "group" / "hide"。

    Returns:
        [{"month": "2024-01", "category": "食費", "total": 38000}, ...]
        月・カテゴリの昇順。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        conditions = [
            "user_id = %s",
            "to_char(date, 'YYYY-MM') >= %s",
            "to_char(date, 'YYYY-MM') <= %s",
            "type = %s",
            "person = %s",
        ]
        params = [user_id, start_month, end_month, type, person]

        if fixed_mode == "hide":
            conditions.append("(memo IS NULL OR memo NOT LIKE '[固定]%')")

        where_clause = "WHERE " + " AND ".join(conditions)

        category_expr = (
            "CASE WHEN memo LIKE '[固定]%' THEN '固定費' ELSE category END"
            if fixed_mode == "group"
            else "category"
        )

        cur.execute(
            f"""
            SELECT
                to_char(date, 'YYYY-MM') as month,
                {category_expr} as category,
                SUM(amount) as total
            FROM transactions
            {where_clause}
            GROUP BY month, {category_expr}
            ORDER BY month ASC, total DESC
            """,
            params,
        )
        result = [dict(row) for row in cur.fetchall()]
        logger.info(
            f"月別推移: {start_month}〜{end_month} {type} {person}"
            f" fixed={fixed_mode} {len(result)}件"
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_conversation_history(
    user_id: int,
    timeout_minutes: int = 10,
) -> tuple[list[dict], bool]:
    """直近の会話履歴を取得する。

    最終メッセージから一定時間経過していた場合は
    履歴をクリアして新しいセッションとして扱う。

    Args:
        user_id: ユーザーID。
        timeout_minutes: セッションタイムアウト（分）。
            この時間以上メッセージがなければ履歴をリセットする。

    Returns:
        (会話履歴リスト, セッションリセットされたかどうか) のタプル。
        会話履歴は [{"role": "user", "content": "..."}, ...] 形式。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 最新メッセージのタイムスタンプを確認
        cur.execute(
            """
            SELECT created_at FROM conversation_history
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        last_row = cur.fetchone()

        is_new_session = False

        if last_row:
            from datetime import datetime, timedelta, timezone

            now = datetime.now(timezone.utc)
            last_time = last_row["created_at"]

            # PostgreSQLのTIMESTAMPはタイムゾーン情報なしで返る場合がある
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)

            if now - last_time > timedelta(minutes=timeout_minutes):
                # タイムアウト: 古い履歴を全削除
                cur.execute(
                    "DELETE FROM conversation_history WHERE user_id = %s",
                    (user_id,),
                )
                conn.commit()
                is_new_session = True
                return [], is_new_session

        # セッション継続中: 全履歴を取得
        cur.execute(
            """
            SELECT role, content FROM conversation_history
            WHERE user_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

        return (
            [{"role": row["role"], "content": row["content"]}
             for row in rows],
            is_new_session,
        )

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def save_conversation_messages(
    user_id: int,
    messages: list[dict],
) -> None:
    """会話メッセージを保存する。

    セッション内のメッセージを蓄積する。
    古い履歴の削除はget_conversation_historyの
    タイムアウト判定時に行う。

    Args:
        user_id: ユーザーID。
        messages: 保存するメッセージのリスト。
            [{"role": "user", "content": "..."},
             {"role": "assistant", "content": "..."}]
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        for msg in messages:
            cur.execute(
                """
                INSERT INTO conversation_history
                    (user_id, role, content)
                VALUES (%s, %s, %s)
                """,
                (user_id, msg["role"], msg["content"]),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)
