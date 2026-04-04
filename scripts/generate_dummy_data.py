# scripts/generate_dummy_data.py
import pandas as pd
import numpy as np
from datetime import date, timedelta
import random
import os

# 再現性のためシード固定
np.random.seed(42)
random.seed(42)

# ==================== 定数定義 ====================

START_DATE = date(2023, 1, 1)
END_DATE = date(2024, 12, 31)

# 通常カテゴリ（月次ループで生成する対象）
# 以下は専用の生成関数で処理するためここには含めない：
#   光熱費       → generate_all_utility_transactions()
#   医療費       → generate_medical_transactions() + generate_dental_transactions()
#   サブスク     → generate_fixed_expense_transactions()
#   家賃/住居    → generate_fixed_expense_transactions()
#   保険         → generate_fixed_expense_transactions()
EXPENSE_CATEGORIES = [
    "食費", "交通費", "日用品", "交際費",
    "衣服", "娯楽", "教育",
]

# カテゴリ別の月あたり件数（min, max）
# EXPENSE_CATEGORIES に含まれるカテゴリのみ定義
MONTHLY_COUNTS = {
    "食費":      (8, 15),
    "交通費":    (3, 8),    # ANA を除いた通常交通費の件数
    "日用品":    (2, 5),    # ドラッグストアを除いた件数
    "交際費":    (1, 4),
    "衣服":      (0, 2),
    "娯楽":      (1, 3),
    "教育":      (0, 2),
}

# 店舗別の金額レンジ（min, max）
# 旧 BASE_AMOUNTS（カテゴリ単位の平均値）を廃止し、店舗ごとのレンジに置き換えた。
# 理由：同一カテゴリでも店舗によって金額帯が大きく異なる
#       （例：JR東日本 200円 vs ANA 30,000円）
# 医療費は専用生成関数から参照されるが、データ定義としてここに集約する。
STORE_AMOUNT_RANGES = {
    "食費": {
        "松屋":           (500, 1000),
        "セブンイレブン": (200, 1200),
        "サイゼリヤ":     (600, 1500),
        "スーパーマルエツ": (1500, 5000),
        "ローソン":       (200, 800),
        "すき家":         (500, 1000),
    },
    "交通費": {
        # ANA はここには含めない（専用ロジックで年3〜4回生成するため）
        "JR東日本":   (200, 1000),
        "東京メトロ": (170, 400),
        "タクシー":   (1000, 3500),
    },
    "日用品": {
        # ドラッグストアはここには含めない（3カテゴリ横断の専用ロジックに移行）
        "100均":         (100, 500),
        "ホームセンター": (1000, 5000),
        "セブンイレブン": (100, 500),
    },
    "交際費": {
        "居酒屋":       (3000, 6000),
        "カフェ":       (500, 800),
        "ギフトショップ": (1000, 5000),
    },
    "医療費": {
        # 薬局はここには含めない（内科セット生成の専用ロジックに移行）
        # 歯科は間隔ベース生成だが、金額レンジの定義はここに集約
        "内科クリニック": (1000, 3000),
        "歯科":          (2000, 8000),
    },
    "衣服": {
        "ユニクロ": (2000, 8000),
        "GU":       (1000, 4000),
        "ZARA":     (3000, 15000),
    },
    "娯楽": {
        "映画館":   (1500, 2000),
        "カラオケ": (1000, 3000),
        "書店":     (500, 2000),
        "ゲーム":   (1000, 8000),
    },
    "教育": {
        "Udemy":   (1500, 2500),
        "書店":    (1000, 5000),
        "セミナー": (5000, 20000),
    },
}

# 季節係数（月ごと。1.0が基準）
SEASONAL_FACTORS = {
    "食費": {
        1: 1.2, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.0, 6: 1.0,
        7: 1.2, 8: 1.2, 9: 1.0, 10: 1.0, 11: 1.1, 12: 1.4
    },
    "光熱費": {
        1: 1.5, 2: 1.4, 3: 1.1, 4: 0.8, 5: 0.7, 6: 0.8,
        7: 1.2, 8: 1.3, 9: 0.9, 10: 0.8, 11: 1.1, 12: 1.4
    },
    "交通費": {
        1: 0.9, 2: 0.9, 3: 1.2, 4: 1.3, 5: 1.3, 6: 1.0,
        7: 1.1, 8: 1.2, 9: 1.2, 10: 1.3, 11: 1.1, 12: 1.0
    },
    "交際費": {
        1: 1.0, 2: 1.0, 3: 1.0, 4: 1.1, 5: 1.1, 6: 1.0,
        7: 1.1, 8: 1.1, 9: 1.0, 10: 1.0, 11: 1.1, 12: 1.8
    },
    "娯楽": {
        1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.3, 6: 1.0,
        7: 1.3, 8: 1.3, 9: 1.0, 10: 1.0, 11: 1.0, 12: 1.3
    },
    "衣服": {
        1: 0.8, 2: 0.8, 3: 1.5, 4: 1.2, 5: 1.0, 6: 0.8,
        7: 0.8, 8: 0.8, 9: 1.5, 10: 1.2, 11: 1.0, 12: 0.8
    },
}

# セブンイレブンのカテゴリ別品目
SEVEN_ITEMS = {
    "食費":   ["パン", "おにぎり", "弁当", "飲み物", "スイーツ"],
    "日用品": ["歯ブラシ", "歯磨き粉", "シャンプー", "洗剤"],
}

# 支払方法の確率（クレカ, 現金, 口座振込）
# 専用関数で処理するカテゴリ（光熱費・サブスク・家賃/住居・保険）は
# 各関数内で直接指定するためここには含めない
PAYMENT_PROBS = {
    "食費":      (0.70, 0.30, 0.00),
    "交通費":    (0.60, 0.40, 0.00),
    "日用品":    (0.60, 0.40, 0.00),
    "交際費":    (0.50, 0.50, 0.00),
    "医療費":    (0.30, 0.70, 0.00),
    "衣服":      (0.80, 0.20, 0.00),
    "娯楽":      (0.70, 0.30, 0.00),
    "教育":      (0.80, 0.20, 0.00),
}

PAYMENT_METHODS = ["クレカ", "現金", "口座振込"]

# personの確率（自分, 妻, 共通）
# 専用関数で処理するカテゴリ（光熱費・家賃/住居・保険）は
# 各関数内で直接指定するためここには含めない
PERSON_PROBS = {
    "食費":      (0.60, 0.20, 0.20),
    "日用品":    (0.50, 0.20, 0.30),
}
DEFAULT_PERSON_PROB = (0.90, 0.05, 0.05)
PERSONS = ["自分", "妻", "共通"]

# イレギュラー支出
IRREGULAR_EXPENSES = [
    {"name": "旅行",     "category": "娯楽",   "amount_range": (50000, 150000)},
    {"name": "家電購入", "category": "日用品", "amount_range": (30000, 100000)},
    {"name": "冠婚葬祭", "category": "交際費", "amount_range": (30000, 50000)},
]


# ==================== ANA（飛行機）の設定 ====================
# 通常の交通費とは別に、年3〜4回だけ発生する高額交通費
# 候補月を限定することで、旅行シーズンに集中する現実的なパターンを再現
#
# annual_count: 1年あたりの搭乗回数のレンジ（min, max）
# candidate_months: 出現候補月（3月=春旅行、5月=GW、8月=夏休み、10月=秋旅行、12月=年末年始）

ANA_CONFIG = {
    "amount_range": (15000, 45000),
    "annual_count": (3, 4),
    "candidate_months": [3, 5, 8, 10, 12],
}


# ==================== 歯科の設定 ====================
# 定期検診ベースなので、月次のランダム生成ではなく間隔で制御
# 金額は STORE_AMOUNT_RANGES["医療費"]["歯科"] を参照

DENTAL_CONFIG = {
    "interval_months": (3, 6),
}


# ==================== 薬局（保険薬局）の設定 ====================
# 内科受診時にセットで発生（処方箋調剤は必ず、付加購入は確率的）
# 薬局単独での訪問は発生しない設計
#
# prescription: 処方箋調剤（内科受診時に必ず1件発生）
# additional_purchases: 薬局訪問のついでに確率的に追加購入
#   prob: 各購入が独立して発生する確率

PHARMACY_CONFIG = {
    "prescription": {
        "category": "医療費",
        "amount_range": (500, 5000),
        "items": ["処方薬"],
        "memo": None,
    },
    "additional_purchases": {
        "食品・飲料": {
            "category": "食費",
            "amount_range": (100, 500),
            "prob": 0.10,
            "items": ["健康食品", "栄養ドリンク", "サプリメント"],
            "memo": None,
        },
        "OTC医薬品": {
            "category": "医療費",
            "amount_range": (800, 2500),
            "prob": 0.05,
            "items": ["湿布", "胃腸薬", "目薬"],
            "memo": "セルフメディケーション",
        },
    },
}


# ==================== ドラッグストアの設定 ====================
# 日用品・食費・医療費の3カテゴリに横断
# weight で来店ごとの購入内容を確率的に決定

DRUGSTORE_CONFIG = {
    "store_name": "ウエルシア",
    "purchase_types": {
        "日用消耗品": {
            "category": "日用品",
            "amount_range": (300, 2000),
            "weight": 0.50,
            "items": ["シャンプー", "洗剤", "歯磨き粉", "ティッシュ"],
            "memo": None,
        },
        "食品・飲料": {
            "category": "食費",
            "amount_range": (200, 1200),
            "weight": 0.30,
            "items": ["水", "栄養ドリンク", "お菓子", "プロテイン"],
            "memo": None,
        },
        "OTC医薬品": {
            "category": "医療費",
            "amount_range": (800, 2500),
            "weight": 0.20,
            "items": ["風邪薬", "胃腸薬", "湿布", "目薬"],
            "memo": "セルフメディケーション",
        },
    },
    "monthly_count": (1, 3),
}


# ==================== ヘルパー関数 ====================

def get_random_date_in_month(year: int, month: int) -> date:
    """指定した年月内のランダムな日付を返す。

    Args:
        year: 対象年。
        month: 対象月（1〜12）。

    Returns:
        指定月内のランダムな日付。
    """
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, random.randint(1, last_day))


def get_seasonal_factor(category: str, month: int) -> float:
    """カテゴリと月に対応する季節係数を返す。

    Args:
        category: 支出カテゴリ名。
        month: 対象月（1〜12）。

    Returns:
        季節係数。定義がない場合は1.0。
    """
    return SEASONAL_FACTORS.get(category, {}).get(month, 1.0)


def generate_amount_from_range(
    amount_range: tuple[int, int],
    category: str,
    month: int,
) -> int:
    """店舗別の金額レンジと季節係数から支出金額を生成する。

    金額レンジ(min, max)内で一様分布からベース金額を決め、
    そこに季節係数を乗じて最終金額とする。
    10円単位に丸め、最小100円を保証する。

    Args:
        amount_range: (min, max) の金額レンジ。店舗ごとに定義される。
        category: 支出カテゴリ名。季節係数の参照に使う。
        month: 対象月（1〜12）。季節係数の参照に使う。

    Returns:
        10円単位で丸めた支出金額（最小100円）。
    """
    low, high = amount_range
    base = random.randint(low, high)

    seasonal = get_seasonal_factor(category, month)
    amount = int(base * seasonal)

    return max(round(amount / 10) * 10, 100)


def get_payment_method(category: str) -> str:
    """カテゴリの確率分布に従って支払い方法をランダムに選択する。

    Args:
        category: 支出カテゴリ名。

    Returns:
        選択された支払い方法の文字列。
    """
    probs = PAYMENT_PROBS[category]
    return random.choices(PAYMENT_METHODS, weights=probs, k=1)[0]


def get_person(category: str) -> str:
    """カテゴリの確率分布に従って支出者をランダムに選択する。

    Args:
        category: 支出カテゴリ名。

    Returns:
        選択された支出者の文字列。
    """
    probs = PERSON_PROBS.get(category, DEFAULT_PERSON_PROB)
    return random.choices(PERSONS, weights=probs, k=1)[0]


def get_store_and_item(category: str) -> tuple[str | None, str | None, tuple[int, int]]:
    """カテゴリに対応する店舗・品目・金額レンジをランダムに選択して返す。

    STORE_AMOUNT_RANGES から店舗をランダムに選択し、
    その店舗に対応する金額レンジも一緒に返す。
    セブンイレブンが選ばれた場合はカテゴリ別の品目リストからも選択する。

    Args:
        category: 支出カテゴリ名。

    Returns:
        (店名, 品目, 金額レンジ) のタプル。品目は該当なしの場合 None。
    """
    stores = STORE_AMOUNT_RANGES.get(category, {})

    if not stores:
        return None, None, (0, 0)

    store = random.choice(list(stores.keys()))
    amount_range = stores[store]

    if store == "セブンイレブン" and category in SEVEN_ITEMS:
        item = random.choice(SEVEN_ITEMS[category])
    else:
        item = None

    return store, item, amount_range


# ==================== データ生成 ====================

def generate_utility_transactions(year: int, month: int) -> list[dict]:
    """光熱費の取引を1ヶ月分生成する。

    東京ガスは「ガス」「電気」を同日に別取引として生成。
    水道局は隔月（奇数月）で追加。

    Args:
        year: 対象年。
        month: 対象月（1〜12）。

    Returns:
        光熱費取引データの辞書リスト。
    """
    transactions = []
    tx_date = get_random_date_in_month(year, month)
    seasonal = get_seasonal_factor("光熱費", month)

    # 東京ガス（ガス・電気）を必ず1セット生成
    for item, base in [("ガス", 5000), ("電気", 7000)]:
        noise = np.random.normal(1.0, 0.15)
        amount = max(round(int(base * seasonal * max(noise, 0.1)) / 10) * 10, 10)
        transactions.append({
            "date":           tx_date,
            "type":           "expense",
            "amount":         amount,
            "category":       "光熱費",
            "store_name":     "東京ガス",
            "item":           item,
            "memo":           None,
            "payment_method": "口座振込",
            "person":         "共通",
        })

    # 水道局を隔月で追加（奇数月）
    if month % 2 == 1:
        noise = np.random.normal(1.0, 0.15)
        amount = max(round(int(3000 * max(noise, 0.1)) / 10) * 10, 10)
        transactions.append({
            "date":           tx_date,
            "type":           "expense",
            "amount":         amount,
            "category":       "光熱費",
            "store_name":     "水道局",
            "item":           None,
            "memo":           None,
            "payment_method": "口座振込",
            "person":         "共通",
        })

    return transactions


def generate_all_utility_transactions() -> list[dict]:
    """全期間の光熱費取引を生成する。

    光熱費は「毎月必ず発生するが金額が季節で変動する」という
    固定出金とも通常支出とも異なる性質を持つ。
    generate_utility_transactions() を月ごとに呼び出して全期間分をまとめる。

    Returns:
        光熱費取引データの辞書リスト。
    """
    transactions = []

    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        transactions.extend(
            generate_utility_transactions(current.year, current.month)
        )

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return transactions


def generate_expense_transactions() -> list[dict]:
    """通常カテゴリ（食費・交通費・日用品・交際費・衣服・娯楽・教育）の
    月次支出取引データを生成する。

    EXPENSE_CATEGORIESに含まれるカテゴリについて、月ごとに
    MONTHLY_COUNTSの範囲でランダムな件数の取引を生成する。
    店舗はSTORE_AMOUNT_RANGESからランダムに選択し、
    その店舗の金額レンジ内で季節係数を加味した金額を決定する。

    光熱費・医療費・固定出金（サブスク・家賃・保険）は
    それぞれ専用の生成関数で処理するため、この関数の対象外。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []

    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        year, month = current.year, current.month

        for category in EXPENSE_CATEGORIES:
            min_count, max_count = MONTHLY_COUNTS[category]
            count = random.randint(min_count, max_count)

            for _ in range(count):
                tx_date = get_random_date_in_month(year, month)
                store, item, amount_range = get_store_and_item(category)
                amount = generate_amount_from_range(amount_range, category, month)

                transactions.append({
                    "date":           tx_date,
                    "type":           "expense",
                    "amount":         amount,
                    "category":       category,
                    "store_name":     store,
                    "item":           item,
                    "memo":           None,
                    "payment_method": get_payment_method(category),
                    "person":         get_person(category),
                })

        if month == 12:
            current = date(year + 1, 1, 1)
        else:
            current = date(year, month + 1, 1)

    return transactions


def generate_medical_transactions() -> list[dict]:
    """内科受診と薬局（処方箋調剤＋付加購入）のセット取引を生成する。

    内科クリニックの受診は月0〜2回発生し、受診した日には
    必ず同日に薬局で処方箋調剤が行われる。
    さらに薬局訪問のついでに、確率的に食品やOTC医薬品を
    追加購入する場合がある。

    1回の内科受診で生成される取引：
      - 内科クリニック（医療費）: 必ず1件
      - 薬局・処方箋調剤（医療費）: 必ず1件
      - 薬局・食品（食費）: 10%の確率で1件
      - 薬局・OTC（医療費）: 5%の確率で1件

    Returns:
        取引データの辞書リスト。
    """
    transactions = []
    monthly_count_range = (0, 2)

    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        year, month = current.year, current.month
        count = random.randint(*monthly_count_range)

        for _ in range(count):
            tx_date = get_random_date_in_month(year, month)

            # --- 内科クリニック ---
            clinic_range = STORE_AMOUNT_RANGES["医療費"]["内科クリニック"]
            transactions.append({
                "date":           tx_date,
                "type":           "expense",
                "amount":         generate_amount_from_range(clinic_range, "医療費", month),
                "category":       "医療費",
                "store_name":     "内科クリニック",
                "item":           None,
                "memo":           None,
                "payment_method": get_payment_method("医療費"),
                "person":         get_person("医療費"),
            })

            # --- 薬局：処方箋調剤（必ず発生） ---
            rx_config = PHARMACY_CONFIG["prescription"]
            transactions.append({
                "date":           tx_date,
                "type":           "expense",
                "amount":         generate_amount_from_range(
                                      rx_config["amount_range"], "医療費", month
                                  ),
                "category":       rx_config["category"],
                "store_name":     "薬局",
                "item":           random.choice(rx_config["items"]),
                "memo":           rx_config["memo"],
                "payment_method": get_payment_method("医療費"),
                "person":         get_person("医療費"),
            })

            # --- 薬局：付加購入（確率的に発生） ---
            for purchase_name, config in PHARMACY_CONFIG["additional_purchases"].items():
                if random.random() < config["prob"]:
                    transactions.append({
                        "date":           tx_date,
                        "type":           "expense",
                        "amount":         generate_amount_from_range(
                                              config["amount_range"],
                                              config["category"],
                                              month,
                                          ),
                        "category":       config["category"],
                        "store_name":     "薬局",
                        "item":           random.choice(config["items"]),
                        "memo":           config["memo"],
                        "payment_method": get_payment_method(config["category"]),
                        "person":         get_person(config["category"]),
                    })

        if month == 12:
            current = date(year + 1, 1, 1)
        else:
            current = date(year, month + 1, 1)

    return transactions


def generate_dental_transactions() -> list[dict]:
    """歯科受診の取引を全期間にわたって生成する。

    歯科は定期検診ベースで3〜6ヶ月間隔で受診する。
    通常の月次ランダム生成とは異なり、前回の受診から
    一定間隔を空けて次の受診を決める方式。

    薬局とのセット生成は行わない（歯科は院内処方が多いため）。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []
    dental_range = STORE_AMOUNT_RANGES["医療費"]["歯科"]
    min_interval, max_interval = DENTAL_CONFIG["interval_months"]

    # 最初の受診月：開始月からランダムなオフセット
    offset = random.randint(0, max_interval - 1)
    current_month_index = offset

    # 全期間の月数を計算
    total_months = (
        (END_DATE.year - START_DATE.year) * 12
        + (END_DATE.month - START_DATE.month) + 1
    )

    while current_month_index < total_months:
        # 経過月数から年・月を計算
        year = START_DATE.year + (START_DATE.month - 1 + current_month_index) // 12
        month = (START_DATE.month - 1 + current_month_index) % 12 + 1

        tx_date = get_random_date_in_month(year, month)

        transactions.append({
            "date":           tx_date,
            "type":           "expense",
            "amount":         generate_amount_from_range(dental_range, "医療費", month),
            "category":       "医療費",
            "store_name":     "歯科",
            "item":           None,
            "memo":           None,
            "payment_method": get_payment_method("医療費"),
            "person":         get_person("医療費"),
        })

        # 次の受診までの間隔をランダムに決定
        interval = random.randint(min_interval, max_interval)
        current_month_index += interval

    return transactions


def generate_ana_transactions() -> list[dict]:
    """ANA（飛行機）の取引を全期間にわたって生成する。

    通常の交通費とは別に、年3〜4回だけ発生する高額交通費。
    候補月（旅行シーズン）からランダムに選択する。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []
    config = ANA_CONFIG

    for year in range(START_DATE.year, END_DATE.year + 1):
        # その年の搭乗回数を決定
        annual_count = random.randint(*config["annual_count"])

        # 候補月から重複なしで搭乗月を選択
        flight_months = random.sample(config["candidate_months"], k=annual_count)

        for month in flight_months:
            # その月が対象期間内かチェック
            check_date = date(year, month, 1)
            if check_date < START_DATE.replace(day=1) or check_date > END_DATE.replace(day=1):
                continue

            tx_date = get_random_date_in_month(year, month)

            transactions.append({
                "date":           tx_date,
                "type":           "expense",
                "amount":         generate_amount_from_range(
                                      config["amount_range"], "交通費", month
                                  ),
                "category":       "交通費",
                "store_name":     "ANA",
                "item":           None,
                "memo":           None,
                "payment_method": "クレカ",
                "person":         "自分",
            })

    return transactions


def generate_drugstore_transactions() -> list[dict]:
    """ドラッグストア（ウエルシア）の取引を全期間にわたって生成する。

    ドラッグストアは日用品・食費・医療費の3カテゴリに横断する。
    毎月1〜3回来店し、来店ごとに購入内容を確率的に決定する。
    OTC医薬品の場合は memo に「セルフメディケーション」を記録する。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []
    config = DRUGSTORE_CONFIG
    store_name = config["store_name"]

    # 購入タイプの選択用にweightリストを準備
    purchase_types = list(config["purchase_types"].keys())
    weights = [config["purchase_types"][pt]["weight"] for pt in purchase_types]

    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        year, month = current.year, current.month
        count = random.randint(*config["monthly_count"])

        for _ in range(count):
            tx_date = get_random_date_in_month(year, month)

            # 購入内容をweightに基づいて確率的に選択
            selected = random.choices(purchase_types, weights=weights, k=1)[0]
            pt_config = config["purchase_types"][selected]

            transactions.append({
                "date":           tx_date,
                "type":           "expense",
                "amount":         generate_amount_from_range(
                                      pt_config["amount_range"],
                                      pt_config["category"],
                                      month,
                                  ),
                "category":       pt_config["category"],
                "store_name":     store_name,
                "item":           random.choice(pt_config["items"]),
                "memo":           pt_config["memo"],
                "payment_method": get_payment_method(pt_config["category"]),
                "person":         get_person(pt_config["category"]),
            })

        if month == 12:
            current = date(year + 1, 1, 1)
        else:
            current = date(year, month + 1, 1)

    return transactions


def generate_fixed_expense_transactions() -> list[dict]:
    """固定出金（サブスク・家賃・保険）の取引を全期間にわたって生成する。

    generate_fixed_expenses() のマスタデータを元に、
    毎月の正しい日付に正しい金額で取引を生成する。
    memo に「[固定] 名称」を設定し、Phase 2 の自動計上と同じ形式にする。

    day_of_month が月の日数を超える場合は月末日にフォールバックする。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []
    fixed_items = generate_fixed_expenses()

    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        year, month = current.year, current.month

        # その月の末日を計算
        if month == 12:
            last_day = 31
        else:
            last_day = (date(year, month + 1, 1) - timedelta(days=1)).day

        for item in fixed_items:
            day = min(item["day_of_month"], last_day)
            tx_date = date(year, month, day)

            transactions.append({
                "date":           tx_date,
                "type":           "expense",
                "amount":         item["amount"],
                "category":       item["category"],
                "store_name":     item["name"],
                "item":           None,
                "memo":           f"[固定] {item['name']}",
                "payment_method": "クレカ" if item["category"] == "サブスク" else "口座振込",
                "person":         "共通" if item["category"] == "家賃/住居" else "自分",
            })

        if month == 12:
            current = date(year + 1, 1, 1)
        else:
            current = date(year, month + 1, 1)

    return transactions


def generate_irregular_expenses() -> list[dict]:
    """冠婚葬祭・家電購入など不定期支出の取引データを生成する。

    各イベントを期間内にランダムな日付で1〜2回発生させる。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []
    all_dates = [
        START_DATE + timedelta(days=i)
        for i in range((END_DATE - START_DATE).days + 1)
    ]

    for event in IRREGULAR_EXPENSES:
        count = random.randint(1, 2)
        for _ in range(count):
            tx_date = random.choice(all_dates)
            amount = random.randint(*event["amount_range"])
            amount = round(amount / 1000) * 1000
            transactions.append({
                "date":           tx_date,
                "type":           "expense",
                "amount":         amount,
                "category":       event["category"],
                "store_name":     event["name"],
                "item":           None,
                "memo":           event["name"],
                "payment_method": get_payment_method(event["category"]),
                "person":         "自分",
            })

    return transactions


def generate_income_transactions() -> list[dict]:
    """給与・賞与・臨時収入の取引データを生成する。

    給与は毎月25日、賞与は6月・12月25日、臨時収入は期間内に2〜3回発生。

    Returns:
        取引データの辞書リスト。
    """
    transactions = []

    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        year, month = current.year, current.month

        # 給与（毎月25日）
        transactions.append({
            "date":           date(year, month, 25),
            "type":           "income",
            "amount":         int(np.random.normal(280000, 5000)),
            "category":       "給与",
            "store_name":     None,
            "item":           None,
            "memo":           None,
            "payment_method": "口座振込",
            "person":         "自分",
        })

        # 賞与（6月・12月）
        if month in (6, 12):
            transactions.append({
                "date":           date(year, month, 25),
                "type":           "income",
                "amount":         int(np.random.normal(500000, 20000)),
                "category":       "賞与",
                "store_name":     None,
                "item":           None,
                "memo":           None,
                "payment_method": "口座振込",
                "person":         "自分",
            })

        if month == 12:
            current = date(year + 1, 1, 1)
        else:
            current = date(year, month + 1, 1)

    # 臨時収入（2〜3回）
    all_dates = [
        START_DATE + timedelta(days=i)
        for i in range((END_DATE - START_DATE).days + 1)
    ]
    for _ in range(random.randint(2, 3)):
        transactions.append({
            "date":           random.choice(all_dates),
            "type":           "income",
            "amount":         random.randint(10000, 50000),
            "category":       "臨時収入",
            "store_name":     None,
            "item":           None,
            "memo":           None,
            "payment_method": "口座振込",
            "person":         "自分",
        })

    return transactions


def generate_fixed_expenses() -> list[dict]:
    """固定費マスターデータのリストを返す。

    Returns:
        固定費データの辞書リスト（name, amount, category, day_of_month）。
    """
    return [
        {"name": "家賃",         "amount": 80000, "category": "家賃/住居", "day_of_month": 27},
        {"name": "Netflix",      "amount": 1490,  "category": "サブスク",  "day_of_month": 15},
        {"name": "Spotify",      "amount": 980,   "category": "サブスク",  "day_of_month": 10},
        {"name": "Amazon Prime", "amount": 600,   "category": "サブスク",  "day_of_month": 1},
        {"name": "生命保険",     "amount": 5000,  "category": "保険",      "day_of_month": 20},
    ]


# ==================== メイン ====================

def main():
    output_dir = os.path.join(os.path.dirname(__file__), "..", "data", "dummy")
    os.makedirs(output_dir, exist_ok=True)

    # 取引明細生成
    # 各生成関数が独立した取引リストを返すので、全て結合する
    transactions = []
    transactions.extend(generate_expense_transactions())
    transactions.extend(generate_all_utility_transactions())
    transactions.extend(generate_medical_transactions())
    transactions.extend(generate_dental_transactions())
    transactions.extend(generate_ana_transactions())
    transactions.extend(generate_drugstore_transactions())
    transactions.extend(generate_fixed_expense_transactions())
    transactions.extend(generate_irregular_expenses())
    transactions.extend(generate_income_transactions())

    df = pd.DataFrame(transactions)
    df = df.sort_values("date").reset_index(drop=True)

    transactions_path = os.path.join(output_dir, "dummy_transactions.csv")
    df.to_csv(transactions_path, index=False, encoding="utf-8-sig")
    print(f"取引明細：{len(df)}件 → {transactions_path}")

    # 固定出金マスタ生成
    fixed_df = pd.DataFrame(generate_fixed_expenses())
    fixed_path = os.path.join(output_dir, "dummy_fixed_expenses.csv")
    fixed_df.to_csv(fixed_path, index=False, encoding="utf-8-sig")
    print(f"固定出金：{len(fixed_df)}件 → {fixed_path}")


if __name__ == "__main__":
    main()
    