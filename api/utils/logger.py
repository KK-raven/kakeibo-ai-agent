# api/utils/logger.py
"""
ロギング設定モジュール

役割:
- アプリ全体で統一されたログ出力を提供する
- コンソールとファイルの両方にログを出力する
- 各モジュールは get_logger(__name__) で自分用のロガーを取得する

使い方:
    from api.utils.logger import get_logger
    logger = get_logger(__name__)

    logger.debug("詳細情報")      # 開発中の調査用
    logger.info("取引を登録しました")  # 正常動作の記録
    logger.warning("予算を超過")    # 注意すべき状態
    logger.error("DB接続失敗")     # エラー発生

ログレベルの切り替え:
    環境変数 LOG_LEVEL で制御する（デフォルト: DEBUG）
    本番では LOG_LEVEL=INFO にして DEBUG ログを抑制する
"""

import logging
import os
from pathlib import Path

# ログファイルの保存先
# プロジェクトルート/logs/ に出力する
LOG_DIR = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    ),
    "logs",
)

# ログレベルを環境変数で制御（デフォルト: DEBUG）
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()

# 初期化済みフラグ（複数回セットアップされるのを防ぐ）
_initialized = False


def _setup_logging() -> None:
    """
    ロギングの初期設定を行う（アプリ起動時に1回だけ実行）。

    設定内容:
    - ルートロガーのレベルを設定
    - コンソールハンドラを追加（stdout に出力）
    - ファイルハンドラを追加（logs/app.log に出力）
    - フォーマットを統一

    フォーマットの説明:
    - %(asctime)s: タイムスタンプ（2025-04-01 12:00:00,000）
    - %(name)s: ロガー名（モジュールパス。api.db.crud 等）
    - %(levelname)s: ログレベル（INFO, ERROR 等）
    - %(message)s: ログメッセージ本文
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    # ログディレクトリがなければ作成
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

    # フォーマット定義
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ルートロガーの設定
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))

    # コンソールハンドラ
    # Docker環境では docker compose logs で確認する
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # ファイルハンドラ
    # encoding="utf-8" で日本語のログも文字化けしない
    file_handler = logging.FileHandler(
        os.path.join(LOG_DIR, "app.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """
    指定した名前のロガーを返す。

    各モジュールで以下のように使う:
        logger = get_logger(__name__)
        logger.info("メッセージ")

    __name__ を渡すことで、ログにモジュールパスが記録される。
    例: api.db.crud で呼ぶと、ログに "api.db.crud" と表示される。
    どのモジュールから出たログかが一目でわかる。

    初回呼び出し時にロギングの初期設定を行う。
    2回目以降は設定済みなのでロガーを返すだけ。

    Args:
        name: ロガー名（通常は __name__ を渡す）

    Returns:
        設定済みのロガー
    """
    _setup_logging()
    return logging.getLogger(name)
