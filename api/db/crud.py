# api/db/crud.py
"""
取引データのCRUD操作モジュール

役割:
- 取引の登録（+ store_category_mapping の自動更新）
- 取引の一覧取得（フィルタ付き）
- 取引の削除

他モジュールとの関係:
- connection.py の get_connection() を使ってDBに接続する
- agent/core.py（Step 6）から呼ばれる
- 集計関数（Step 5）もこのファイルに追加予定
"""

import calendar
from datetime import date

from api.db.connection import get_connection
from api.utils.business_day import next_business_day
from api.utils.logger import get_logger

logger = get_logger(__name__)


def register_transaction(
    date: str,
    type: str,
    amount: int,
    category: str,
    store_name: str | None = None,
    item: str | None = None,
    memo: str | None = None,
    payment_method: str = "現金",
    card_name: str | None = None,
    person: str = "自分",
) -> dict:
    """
    取引を1件登録し、登録されたレコードを返す。

    処理の流れ:
    1. transactions テーブルに INSERT
    2. store_name がある場合、store_category_mapping を UPSERT
    3. 登録されたレコードを SELECT して返す

    なぜ2と3が同じ関数内にあるか:
    - 取引登録とマッピング更新は常にセットで行うべき処理
    - 分離すると呼び忘れで不整合が起きる
    - 1つのconnection内でcommitすることで、片方だけ成功することを防ぐ

    Args:
        date: 取引日（"YYYY-MM-DD" 形式）
        type: "income" or "expense"
        amount: 金額（正の整数）
        category: カテゴリ名（"食費", "給与" 等）
        store_name: 店名（任意）
        item: 品目（任意）
        memo: メモ（任意）
        payment_method: 支払方法（デフォルト "現金"）
        person: 誰の取引か（デフォルト "自分"）

    Returns:
        登録されたレコードの辞書（id を含む）
    """
    conn = get_connection()
    try:
        # 1. transactions に INSERT
        cursor = conn.execute(
            """
            INSERT INTO transactions
                (date, type, amount, category, store_name, item, memo,
                 payment_method, card_name, person)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, type, amount, category, store_name, item, memo,
                payment_method, card_name, person,
            ),
        )
        transaction_id = cursor.lastrowid
        # 2. store_name がある場合、store_category_mapping を更新
        #    UPSERT: 同じ店名×カテゴリがなければ INSERT、あれば count+1
        if store_name:
            conn.execute(
                """
                INSERT INTO store_category_mapping
                    (store_name, category, count, last_used)
                VALUES (?, ?, 1, ?)
                ON CONFLICT (store_name, category) DO UPDATE SET
                    count = count + 1,
                    last_used = ?
                """,
                (store_name, category, date, date),
            )
        conn.commit()
        logger.info(
            f"取引登録: {date} {type} {category} {amount}円"
            f" store={store_name} item={item}"
            f" method={payment_method} card={card_name}"
        )

        # 3. 登録されたレコードを返す
        #    lastrowid で取得した id を使って SELECT する
        #    cursor.lastrowid は INSERT 直後にしか取れないのでここで取得
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()

        return dict(row)

    finally:
        conn.close()


def get_transactions(
    year_month: str | None = None,
    category: str | None = None,
    person: str | None = None,
    type: str | None = None,
    store_name: str | None = None,
    item: str | None = None,
) -> list[dict]:
    """
    取引一覧を取得する。条件を指定すると絞り込める。

    フィルタの仕組み:
    - 各引数が None でなければ WHERE 句に条件を追加する
    - 複数指定すると AND で結合される
    - 全部 None なら全件取得

    year_month のフィルタ方法:
    - SQLite の strftime で date カラムから年月を抽出して比較する
    - 例: year_month="2025-04" → strftime('%Y-%m', date) = '2025-04'
    - インデックスが効かないが、個人の家計データは多くても年間数千件なので問題ない

    store_name, item の検索方法:
    - LIKE で部分一致検索にしている
    - 理由: ユーザーが「セブン」と略して言う場合や、
      「雪見」だけで「雪見だいふく」を探す場合に対応するため
    - 完全一致が必要な場面（集計等）は別の関数で対応する

    Args:
        year_month: "YYYY-MM" 形式（例: "2025-04"）
        category: カテゴリ名で絞り込み
        person: person で絞り込み
        type: "income" or "expense" で絞り込み
        store_name: 店名で部分一致検索
        item: 品目で部分一致検索

    Returns:
        該当レコードのリスト（辞書のリスト）。日付降順。
    """
    conn = get_connection()
    try:
        conditions = []
        params = []

        if year_month:
            conditions.append("strftime('%Y-%m', date) = ?")
            params.append(year_month)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if person:
            conditions.append("person = ?")
            params.append(person)
        if type:
            conditions.append("type = ?")
            params.append(type)
        if store_name:
            conditions.append("store_name LIKE ?")
            params.append(f"%{store_name}%")
        if item:
            conditions.append("item LIKE ?")
            params.append(f"%{item}%")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        rows = conn.execute(
            f"SELECT * FROM transactions {where_clause} ORDER BY date DESC, id DESC",
            params,
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def delete_transaction(transaction_id: int) -> bool:
    """
    取引を1件削除する。

    id で指定する理由:
    - 同じ日・同じ金額・同じ店の取引が複数ありうるため、
      date + amount 等の組み合わせでは一意に特定できない
    - Agent が「どの取引を削除しますか？」とユーザーに確認し、
      id を特定してからこの関数を呼ぶ想定（Step 6 で実装）

    注意:
    - store_category_mapping の count は減算しない
    - 理由: マッピングは「傾向の推定」用途なので、厳密な整合性より
      シンプルさを優先。削除は稀な操作なので傾向への影響は軽微

    Args:
        transaction_id: 削除する取引の id

    Returns:
        True: 削除成功（1件削除された）
        False: 該当 id が存在しなかった
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM transactions WHERE id = ?",
            (transaction_id,),
        )
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"取引削除: id={transaction_id}")
        else:
            logger.warning(f"取引削除失敗: id={transaction_id} が存在しません")

        # rowcount: DELETE で影響を受けた行数
        # 0 なら該当 id が存在しなかった
        return cursor.rowcount > 0

    finally:
        conn.close()


def update_transaction(transaction_id: int, **kwargs) -> dict:
    """
    取引を1件更新する。

    指定されたフィールドのみを更新し、指定されていないフィールドは
    元の値を維持する。部分更新（PATCH的な挙動）。

    更新対象のIDが不明な場合は、Agent が先に get_transactions で
    候補を表示してユーザーに確認する想定。

    Args:
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

    # ホワイトリスト外のキーを除外
    updates = {k: v for k, v in kwargs.items() if k in UPDATABLE_COLUMNS}

    if not updates:
        return {"error": "更新するフィールドが指定されていません"}

    conn = get_connection()
    try:
        # 対象の存在確認
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()

        if not row:
            return {"error": f"ID {transaction_id} の取引が見つかりません"}

        # SET句を動的に組み立てる
        # 例: updates = {"amount": 3500, "category": "食費"}
        # → "amount = ?, category = ?" と (3500, "食費") を生成
        set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
        values = list(updates.values())
        values.append(transaction_id)

        conn.execute(
            f"UPDATE transactions SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()

        logger.info(
            f"取引更新: id={transaction_id} "
            f"更新項目={list(updates.keys())}"
        )

        # 更新後のレコードを返す
        updated = conn.execute(
            "SELECT * FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()

        return dict(updated)

    finally:
        conn.close()


def register_fixed_expense(
    name: str,
    amount: int,
    category: str,
    day_of_month: int,
    start_date: str,
    payment_method: str = "口座振替",
    end_date: str | None = None,
) -> dict:
    """
    固定出金を1件登録する。

    固定出金とは毎月決まった金額が発生する支出のこと。
    例: 家賃、Netflix、電気代の基本料金など。

    このテーブルはマスタデータ（設定情報）であり、
    実際の支出記録（transactions）とは別。
    apply_fixed_expenses() で transactions に反映する。

    Args:
        name: 固定出金の名称（"家賃", "Netflix" 等）
        amount: 金額
        category: カテゴリ（"家賃/住居", "サブスク" 等）
        day_of_month: 毎月の計上日（1〜31）
        start_date: 開始日（"YYYY-MM-DD"）
        payment_method: 支払方法（デフォルト "口座振替"）
        end_date: 終了日（None なら継続中）

    Returns:
        登録されたレコードの辞書
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO fixed_expenses
                (name, amount, category, day_of_month,
                 payment_method, start_date, end_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, amount, category, day_of_month,
                payment_method, start_date, end_date,
            ),
        )
        conn.commit()
        logger.info(
            f"固定出金登録: {name} {amount}円 {category}"
            f" 毎月{day_of_month}日 method={payment_method}"
        )

        row = conn.execute(
            "SELECT * FROM fixed_expenses WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

        return dict(row)

    finally:
        conn.close()


def get_fixed_expenses(active_only: bool = True) -> list[dict]:
    """
    固定出金の一覧を取得する。

    Args:
        active_only: True なら is_active=1 のみ。
                     False なら無効化されたものも含む全件。

    Returns:
        固定出金レコードのリスト
    """
    conn = get_connection()
    try:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM fixed_expenses WHERE is_active = 1"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM fixed_expenses"
            ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def apply_fixed_expenses(year: int, month: int) -> list[dict]:
    """
    指定した年月の固定出金を transactions に自動計上する。

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
      （店舗での支払いは土日祝でも発生するため）

    計上済みの判定:
    - memo に "[固定] {name}" を入れて手動登録と区別する
    - 同じ年月に同じ memo の取引があれば計上済みとみなす

    Args:
        year: 年（例: 2025）
        month: 月（例: 4）

    Returns:
        今回新たに計上されたレコードのリスト
    """
    conn = get_connection()
    try:
        fixed_list = conn.execute(
            "SELECT * FROM fixed_expenses WHERE is_active = 1"
        ).fetchall()

        year_month = f"{year:04d}-{month:02d}"
        last_day = calendar.monthrange(year, month)[1]

        added = []

        for fe in fixed_list:
            # --- 有効期間チェック ---
            if fe["start_date"] > f"{year_month}-31":
                continue
            if fe["end_date"] and fe["end_date"] < f"{year_month}-01":
                continue

            # --- 計上日の決定 ---
            # Step 1: 月末フォールバック
            actual_day = min(fe["day_of_month"], last_day)
            tx_date = date(year, month, actual_day)

            # Step 2: 口座振替の場合のみ平日調整
            if fe["payment_method"] == "口座振替":
                tx_date = next_business_day(tx_date)

            tx_date_str = tx_date.isoformat()

            # --- 計上済みチェック ---
            memo = f"[固定] {fe['name']}"
            existing = conn.execute(
                """
                SELECT id FROM transactions
                WHERE strftime('%Y-%m', date) = ?
                    AND memo = ?
                """,
                (year_month, memo),
            ).fetchone()

            if existing:
                logger.debug(f"固定出金スキップ（計上済み）: {fe['name']} {year_month}")
                continue

            # --- 計上 ---
            cursor = conn.execute(
                """
                INSERT INTO transactions
                    (date, type, amount, category, memo,
                     payment_method, person)
                VALUES (?, 'expense', ?, ?, ?, ?, '自分')
                """,
                (
                    tx_date_str, fe["amount"], fe["category"],
                    memo, fe["payment_method"],
                ),
            )

            row = conn.execute(
                "SELECT * FROM transactions WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()

            added.append(dict(row))
            logger.info(
                f"固定出金計上: {fe['name']} {fe['amount']}円"
                f" → {tx_date_str} ({fe['payment_method']})"
            )
        conn.commit()
        return added

    finally:
        conn.close()


def deactivate_fixed_expense(fixed_expense_id: int) -> dict:
    """
    固定出金を無効化する。

    DELETEではなく is_active=0 にする論理削除。
    end_date に今日の日付を設定する。
    履歴を残す設計のため、レコード自体は削除しない。
    get_fixed_expenses(active_only=False) で過去の固定出金も確認できる。

    Args:
        fixed_expense_id: 無効化する固定出金のID。

    Returns:
        無効化した固定出金の辞書。
        該当IDが存在しない場合や既に無効化済みの場合は
        {"error": "..."} を返す。
    """
    conn = get_connection()
    try:
        # 対象の存在確認
        row = conn.execute(
            "SELECT * FROM fixed_expenses WHERE id = ?",
            (fixed_expense_id,),
        ).fetchone()

        if not row:
            return {"error": f"ID {fixed_expense_id} の固定出金が見つかりません"}

        if not row["is_active"]:
            return {"error": f"ID {fixed_expense_id} は既に無効化されています"}

        # 無効化
        today = date.today().isoformat()
        conn.execute(
            """
            UPDATE fixed_expenses
            SET is_active = 0, end_date = ?
            WHERE id = ?
            """,
            (today, fixed_expense_id),
        )
        conn.commit()

        logger.info(f"固定出金無効化: ID={fixed_expense_id} {row['name']}")

        # 更新後のレコードを返す
        updated = conn.execute(
            "SELECT * FROM fixed_expenses WHERE id = ?",
            (fixed_expense_id,),
        ).fetchone()

        return dict(updated)

    finally:
        conn.close()


def get_payment_methods(category: str | None = None) -> list[dict]:
    """
    支払方法の一覧を取得する。

    Args:
        category: "現金" or "非現金" で絞り込み。None なら全件。

    Returns:
        支払方法レコードのリスト
    """
    conn = get_connection()
    try:
        if category:
            rows = conn.execute(
                "SELECT * FROM payment_methods WHERE category = ?",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM payment_methods"
            ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def add_payment_method(
    name: str,
    category: str = "非現金",
    linked_card: str | None = None,
) -> dict:
    """
    新しい支払方法を追加する。

    Agent がユーザーに二重確認を取った上で呼ぶ想定。
    例: 「楽天ペイを新しい支払方法として登録しますか？」→「はい」

    Args:
        name: 支払方法名（"楽天ペイ" 等）
        category: "現金" or "非現金"（ほぼ全て非現金）
        linked_card: 決済元カード名（任意）

    Returns:
        追加されたレコードの辞書
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO payment_methods (name, category, linked_card)
            VALUES (?, ?, ?)
            """,
            (name, category, linked_card),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM payment_methods WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

        return dict(row)

    finally:
        conn.close()


def update_payment_method_linked_card(
    name: str,
    linked_card: str,
) -> bool:
    """
    支払方法の決済元（linked_card）を設定・変更する。

    用途: QUICPayの決済元をJCBに設定する等。
    Agent が初回使用時に「QUICPayの決済元はどのカードですか？」と
    確認し、回答を受けてこの関数を呼ぶ想定。

    Args:
        name: 支払方法名（"QUICPay" 等）
        linked_card: 決済元カード名（"JCB" 等）

    Returns:
        True: 更新成功
        False: 該当する支払方法が存在しなかった
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE payment_methods
            SET linked_card = ?
            WHERE name = ?
            """,
            (linked_card, name),
        )
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"決済元設定: {name} → {linked_card}")
        else:
            logger.warning(f"決済元設定失敗: {name} が存在しません")

        return cursor.rowcount > 0

    finally:
        conn.close()


def register_credit_card(
    name: str,
    billing_close_day: int | None = None,
    payment_day: int | None = None,
) -> dict:
    """
    クレジットカードを登録する。

    1枚目のカードは自動的にデフォルト（is_default=1）になる。
    2枚目以降は is_default=0。
    デフォルトカードの変更機能は v2 で実装する。

    締め日・引き落とし日は初期はNULLで、後からチャットで設定可能。

    Args:
        name: カード名（"JCB", "三井住友VISA" 等）
        billing_close_day: 締め日（1〜31。NULLなら未設定）
        payment_day: 引き落とし日（1〜31。NULLなら未設定）

    Returns:
        登録されたレコードの辞書
    """
    conn = get_connection()
    try:
        # カードが1枚もなければデフォルトにする
        existing = conn.execute(
            "SELECT COUNT(*) as cnt FROM credit_cards"
        ).fetchone()
        is_default = 1 if existing["cnt"] == 0 else 0

        cursor = conn.execute(
            """
            INSERT INTO credit_cards
                (name, billing_close_day, payment_day, is_default)
            VALUES (?, ?, ?, ?)
            """,
            (name, billing_close_day, payment_day, is_default),
        )
        conn.commit()
        default_str = "デフォルト" if is_default else ""
        logger.info(f"カード登録: {name} {default_str}")

        row = conn.execute(
            "SELECT * FROM credit_cards WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

        return dict(row)

    finally:
        conn.close()


def get_credit_cards() -> list[dict]:
    """
    クレジットカードの一覧を取得する。
    デフォルトカードが先頭に来るよう is_default DESC でソート。

    Returns:
        クレジットカードレコードのリスト
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM credit_cards ORDER BY is_default DESC, id ASC"
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def set_budget(
    category: str,
    amount: int,
    person: str = "自分",
    alert_threshold: int | None = None,
) -> dict:
    """
    カテゴリ別の月額予算を設定する。

    既に同じ category × person の予算があれば金額を更新する（UPSERT）。
    新規なら INSERT する。

    alert_threshold の扱い:
    - 値を指定すると閾値を設定する
    - None を指定すると閾値なし（超過時のみ通知）
    - 予算変更時に閾値を変えたくない場合も、
      現状の設計では毎回指定する必要がある。
      ただし Agent 側で「閾値はそのままでいいですか？」と
      確認するフローにするため、実用上は問題ない。

    Args:
        category: カテゴリ名（"食費", "交際費" 等）
        amount: 月額予算（正の整数）
        person: 誰の予算か（デフォルト "自分"）
        alert_threshold: 残額警告の閾値（None なら警告なし）

    Returns:
        設定された予算レコードの辞書
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO budgets (category, amount, alert_threshold, person)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (category, person) DO UPDATE SET
                amount = ?,
                alert_threshold = ?,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                category, amount, alert_threshold, person,
                amount, alert_threshold,
            ),
        )
        conn.commit()

        threshold_str = f" 閾値{alert_threshold}円" if alert_threshold else ""
        logger.info(f"予算設定: {person} {category} {amount}円{threshold_str}")

        row = conn.execute(
            """
            SELECT * FROM budgets
            WHERE category = ? AND person = ?
            """,
            (category, person),
        ).fetchone()

        return dict(row)

    finally:
        conn.close()


def get_budgets(person: str = "自分") -> list[dict]:
    """
    予算一覧を取得する。

    Args:
        person: 誰の予算か（デフォルト "自分"）

    Returns:
        予算レコードのリスト
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM budgets WHERE person = ? ORDER BY category",
            (person,),
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def check_budget(
    category: str,
    year_month: str,
    person: str = "自分",
) -> dict | None:
    """
    特定カテゴリの予算と当月支出を比較する。

    処理の流れ:
    1. budgets テーブルから該当カテゴリの予算額と閾値を取得
    2. transactions テーブルから該当月の支出合計を集計
    3. 残額・超過フラグ・警告レベルを計算して返す

    alert_level の3段階:
    - "ok": 残額が閾値以上（or 閾値未設定）。通知不要
    - "warning": 残額が閾値未満だが超過していない。
                 Agent が「残りXXX円です」と通知する
    - "over": 予算超過。Agent が「XXX円超過しています」と通知する

    予算が設定されていないカテゴリの場合は None を返す。

    SUM の COALESCE:
    - 該当月に1件も支出がない場合、SUM は NULL を返す
    - COALESCE(NULL, 0) で 0 に変換する

    Args:
        category: カテゴリ名
        year_month: "YYYY-MM" 形式
        person: 誰の予算か

    Returns:
        予算チェック結果の辞書。予算未設定の場合は None。
    """
    conn = get_connection()
    try:
        # 1. 予算額と閾値を取得
        budget_row = conn.execute(
            """
            SELECT amount, alert_threshold FROM budgets
            WHERE category = ? AND person = ?
            """,
            (category, person),
        ).fetchone()

        if not budget_row:
            logger.debug(f"予算未設定: {person} {category}")
            return None

        budget_amount = budget_row["amount"]
        threshold = budget_row["alert_threshold"]

        # 2. 当月の支出合計を集計
        spent_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE strftime('%Y-%m', date) = ?
                AND category = ?
                AND person = ?
                AND type = 'expense'
            """,
            (year_month, category, person),
        ).fetchone()

        spent = spent_row["total"]
        remaining = budget_amount - spent

        # 3. 警告レベルの判定
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

    finally:
        conn.close()


def get_monthly_summary(
    year_month: str,
    person: str = "自分",
) -> dict:
    """
    月次の収入合計・支出合計・差額を返す。

    Agent が「今月いくら使った？」「今月の収支は？」等に応答する際に使う。

    Args:
        year_month: "YYYY-MM" 形式
        person: 誰の集計か

    Returns:
        {
            "year_month": "2025-04",
            "income": 250000,
            "expense": 180000,
            "balance": 70000,
        }
    """
    conn = get_connection()
    try:
        # 収入合計
        income_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE strftime('%Y-%m', date) = ?
                AND person = ?
                AND type = 'income'
            """,
            (year_month, person),
        ).fetchone()

        # 支出合計
        expense_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE strftime('%Y-%m', date) = ?
                AND person = ?
                AND type = 'expense'
            """,
            (year_month, person),
        ).fetchone()

        income = income_row["total"]
        expense = expense_row["total"]

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

    finally:
        conn.close()


def get_category_summary(
    year_month: str,
    type: str = "expense",
    person: str = "自分",
) -> list[dict]:
    """
    カテゴリ別の集計を返す。金額が大きい順。

    Agent が「食費いくら？」「何に一番使ってる？」等に応答する際に使う。
    type 引数で支出カテゴリ別・収入カテゴリ別を切り替えられる。

    GROUP BY category で各カテゴリの合計を出し、
    ORDER BY total DESC で金額が大きい順に並べる。

    Args:
        year_month: "YYYY-MM" 形式
        type: "expense" or "income"
        person: 誰の集計か

    Returns:
        [
            {"category": "食費", "total": 35000, "count": 12},
            {"category": "交際費", "total": 15000, "count": 3},
            ...
        ]
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                category,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            WHERE strftime('%Y-%m', date) = ?
                AND type = ?
                AND person = ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (year_month, type, person),
        ).fetchall()

        result = [dict(row) for row in rows]

        logger.info(
            f"カテゴリ集計: {year_month} {type} {person}"
            f" {len(result)}カテゴリ"
        )

        return result

    finally:
        conn.close()


def get_monthly_comparison(
    year_month: str,
    compare_to: str | None = None,
    person: str = "自分",
) -> list[dict]:
    """
    指定月と比較対象月のカテゴリ別支出を比較する。

    compare_to を省略すると前月と比較する。
    明示的に指定すると任意の月と比較できる。

    使用例:
    - 「先月と比べて」→ get_monthly_comparison("2025-04")
    - 「去年の4月と比べて」→ get_monthly_comparison("2025-04", "2024-04")
    - 「1月と比べて」→ get_monthly_comparison("2025-04", "2025-01")

    Args:
        year_month: "YYYY-MM" 形式（当月）
        compare_to: "YYYY-MM" 形式（比較対象月）。None なら前月
        person: 誰の集計か

    Returns:
        カテゴリごとの比較結果リスト。差額の絶対値が大きい順。
    """
    conn = get_connection()
    try:
        # 1. 比較対象月の決定
        if compare_to:
            prev_year_month = compare_to
        else:
            # デフォルト: 前月を計算
            year = int(year_month[:4])
            month = int(year_month[5:7])
            if month == 1:
                prev_year = year - 1
                prev_month = 12
            else:
                prev_year = year
                prev_month = month - 1
            prev_year_month = f"{prev_year:04d}-{prev_month:02d}"

        # 2. 両月のカテゴリ別集計を辞書化
        # {カテゴリ名: 合計金額} の形にする
        current_rows = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM transactions
            WHERE strftime('%Y-%m', date) = ?
                AND type = 'expense'
                AND person = ?
            GROUP BY category
            """,
            (year_month, person),
        ).fetchall()

        prev_rows = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM transactions
            WHERE strftime('%Y-%m', date) = ?
                AND type = 'expense'
                AND person = ?
            GROUP BY category
            """,
            (prev_year_month, person),
        ).fetchall()

        current_dict = {row["category"]: row["total"] for row in current_rows}
        prev_dict = {row["category"]: row["total"] for row in prev_rows}

        # 3. 全カテゴリを合体
        # set() で重複を除いた全カテゴリ名を取得
        all_categories = set(current_dict.keys()) | set(prev_dict.keys())

        # 4. カテゴリごとに比較
        result = []
        for cat in all_categories:
            current = current_dict.get(cat, 0)
            previous = prev_dict.get(cat, 0)
            diff = current - previous

            # 増減率: 比較対象月が0なら計算不可
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

        # 差額の絶対値が大きい順にソート
        result.sort(key=lambda x: abs(x["diff"]), reverse=True)

        logger.info(
            f"月比較: {year_month} vs {prev_year_month} {person}"
            f" {len(result)}カテゴリ"
        )

        return result

    finally:
        conn.close()


def get_custom_summary(
    year_month: str,
    group_by: str = "category",
    type: str = "expense",
    person: str = "自分",
    category: str | None = None,
    store_name: str | None = None,
    item: str | None = None,
    payment_method: str | None = None,
) -> list[dict]:
    """
    柔軟な集計軸とフィルタで集計する汎用関数。

    group_by で集計軸を指定し、フィルタ引数で絞り込む。
    これにより以下のような多様な集計に1つの関数で対応できる:
    - 「セブンイレブンでいくら使った？」
      → group_by="store_name", store_name="セブン"
    - 「おやつにいくら？」
      → group_by="item", item="おやつ"
    - 「セブンイレブンで何を買ってる？」
      → group_by="item", store_name="セブンイレブン"
    - 「どの店で一番使ってる？」
      → group_by="store_name"
    - 「支払方法別の支出は？」
      → group_by="payment_method"
    - 「クレカでいくら使ってる？」
      → group_by="category", payment_method="クレジットカード"

    group_by のホワイトリスト:
    - SQLに直接埋め込むため、任意の文字列を受け付けると
      SQLインジェクションのリスクがある
    - 許可するカラム名を明示的に制限する

    Args:
        year_month: "YYYY-MM" 形式
        group_by: 集計軸（"category", "store_name", "item",
                  "payment_method", "person"）
        type: "expense" or "income"
        person: 誰の集計か
        category: カテゴリで絞り込み（完全一致）
        store_name: 店名で絞り込み（部分一致）
        item: 品目で絞り込み（部分一致）
        payment_method: 支払方法で絞り込み（完全一致）

    Returns:
        [
            {"key": "セブンイレブン", "total": 12000, "count": 5},
            {"key": "スーパー", "total": 8000, "count": 3},
            ...
        ]
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
        # WHERE 句の動的組み立て（get_transactions と同じ方式）
        conditions = [
            "strftime('%Y-%m', date) = ?",
            "type = ?",
            "person = ?",
        ]
        params = [year_month, type, person]

        if category:
            conditions.append("category = ?")
            params.append(category)
        if store_name:
            conditions.append("store_name LIKE ?")
            params.append(f"%{store_name}%")
        if item:
            conditions.append("item LIKE ?")
            params.append(f"%{item}%")
        if payment_method:
            conditions.append("payment_method = ?")
            params.append(payment_method)

        where_clause = "WHERE " + " AND ".join(conditions)

        # group_by はホワイトリストで検証済みなので
        # f-string で直接埋め込んでも安全
        rows = conn.execute(
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
        ).fetchall()

        result = [dict(row) for row in rows]

        logger.info(
            f"カスタム集計: {year_month} group_by={group_by}"
            f" {type} {person} → {len(result)}件"
        )

        return result

    finally:
        conn.close()


def get_setting(key: str) -> str | None:
    """
    設定値を取得する。

    settingsテーブルから指定したkeyの値を返す。
    存在しなければNoneを返す。

    Args:
        key: 設定キー（"default_payment_method" 等）

    Returns:
        設定値の文字列。未設定ならNone。
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()

        if row:
            return row["value"]
        return None

    finally:
        conn.close()


def set_setting(key: str, value: str) -> dict:
    """
    設定値を保存する。

    既に同じkeyが存在すればvalueを上書きする（UPSERT）。
    新規ならINSERTする。

    updated_atを更新することで、最後にいつ変更されたかがわかる。

    Args:
        key: 設定キー
        value: 設定値（文字列）

    Returns:
        {"key": key, "value": value}
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET
                value = ?,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value, value),
        )
        conn.commit()

        logger.info(f"設定変更: {key} = {value}")

        return {"key": key, "value": value}

    finally:
        conn.close()


def get_store_summary(
    year_month: str,
    type: str = "expense",
    person: str = "自分",
    store_name: str | None = None,
) -> list[dict]:
    """店別の集計を返す。金額が大きい順。

    store_nameを指定すると特定店舗に絞り込める。
    指定しない場合は全店舗の集計を返す。

    Args:
        year_month: "YYYY-MM" 形式。
        type: "expense" or "income"。
        person: 誰の集計か。
        store_name: 店名で部分一致絞り込み（省略時は全店舗）。

    Returns:
        [
            {"store_name": "セブンイレブン", "total": 15000, "count": 8},
            ...
        ]
    """
    conn = get_connection()
    try:
        conditions = [
            "strftime('%Y-%m', date) = ?",
            "type = ?",
            "person = ?",
            "store_name IS NOT NULL",  # 店名未登録の取引を除外する
        ]
        params = [year_month, type, person]

        if store_name:
            conditions.append("store_name LIKE ?")
            params.append(f"%{store_name}%")

        where_clause = "WHERE " + " AND ".join(conditions)
        rows = conn.execute(
            f"""
            SELECT
                store_name,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY store_name
            ORDER BY total DESC
            """,
            params,
        ).fetchall()
        result = [dict(row) for row in rows]
        logger.info(f"店別集計: {year_month} {type} {person} {len(result)}店舗")
        return result
    finally:
        conn.close()


def get_item_summary(
    year_month: str,
    type: str = "expense",
    person: str = "自分",
    item: str | None = None,
) -> list[dict]:
    """品目別の集計を返す。金額が大きい順。

    itemを指定すると特定品目に絞り込める。
    指定しない場合は全品目の集計を返す。

    Args:
        year_month: "YYYY-MM" 形式。
        type: "expense" or "income"。
        person: 誰の集計か。
        item: 品目で部分一致絞り込み（省略時は全品目）。

    Returns:
        [
            {"item": "弁当", "total": 8000, "count": 12},
            ...
        ]
    """
    conn = get_connection()
    try:
        conditions = [
            "strftime('%Y-%m', date) = ?",
            "type = ?",
            "person = ?",
            "item IS NOT NULL",  # 品目未登録の取引を除外する
        ]
        params = [year_month, type, person]

        if item:
            conditions.append("item LIKE ?")
            params.append(f"%{item}%")

        where_clause = "WHERE " + " AND ".join(conditions)
        rows = conn.execute(
            f"""
            SELECT
                item,
                SUM(amount) as total,
                COUNT(*) as count
            FROM transactions
            {where_clause}
            GROUP BY item
            ORDER BY total DESC
            """,
            params,
        ).fetchall()
        result = [dict(row) for row in rows]
        logger.info(f"品目別集計: {year_month} {type} {person} {len(result)}品目")
        return result
    finally:
        conn.close()
