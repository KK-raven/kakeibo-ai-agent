# api/utils/business_day.py
"""
営業日（平日）調整ユーティリティ

役割:
- 土日祝を判定し、翌平日 or 前平日に調整する
- 日本の祝日判定には jpholiday ライブラリを使用

使い分け:
- next_business_day: 引き落とし日・口座振替（土日祝→翌平日）
- prev_business_day: 給与・賞与（土日祝→前平日）

呼び出し元:
- api/db/crud.py の apply_fixed_expenses（固定出金の計上日調整）
- api/agent/core.py（給与・賞与登録時の日付調整、Step 6 で実装）
- v2 のクレカ引き落とし日調整
"""

from datetime import date, timedelta
import jpholiday


def is_holiday(d: date) -> bool:
    """
    指定した日付が土日または日本の祝日かを判定する。

    判定の順序:
    1. 土曜 or 日曜 → True
    2. jpholiday で祝日判定 → True
    3. どちらでもない → False（平日）

    Args:
        d: 判定する日付

    Returns:
        True なら休日、False なら平日
    """
    # weekday(): 月=0, 火=1, ..., 土=5, 日=6
    if d.weekday() >= 5:
        return True
    if jpholiday.is_holiday(d):
        return True
    return False


def next_business_day(d: date) -> date:
    """
    土日祝なら翌平日を返す。平日ならそのまま返す。

    用途: 口座振替・クレカ引き落とし日の調整
    例: 2025-04-26（土）→ 2025-04-28（月）
        2025-05-03（祝・土）→ 2025-05-07（水）
        （5/3〜5/6がGW連休のため5/7まで飛ぶ）

    Args:
        d: 元の日付

    Returns:
        d が平日ならそのまま、休日なら翌平日
    """
    while is_holiday(d):
        d += timedelta(days=1)
    return d


def prev_business_day(d: date) -> date:
    """
    土日祝なら前平日を返す。平日ならそのまま返す。

    用途: 給与・賞与の支給日調整
    例: 2025-04-26（土）→ 2025-04-25（金）
        2025-01-01（祝）→ 2024-12-31（火）...ではなく
        12/31も確認が必要（大晦日は祝日ではないが年末休業の場合あり）
        jpholiday では大晦日は祝日扱いではないので 12/31 が返る

    Args:
        d: 元の日付

    Returns:
        d が平日ならそのまま、休日なら前平日
    """
    while is_holiday(d):
        d -= timedelta(days=1)
    return d
