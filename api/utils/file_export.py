# api/utils/file_export.py
"""
ファイルエクスポートユーティリティ

役割:
- Agentが生成したコンテンツをdata/exports/に保存する
- csv・txtの2形式に対応する
- ファイル名の重複を避けるためタイムスタンプを付与する
"""
from datetime import datetime
from pathlib import Path

from api.utils.logger import get_logger

logger = get_logger(__name__)

# エクスポート先ディレクトリ
# docker-compose.ymlで./data:/app/dataにマウントされているため
# ホスト側のdata/exports/に保存される
EXPORTS_DIR = Path("/app/data/exports")


def export_file(
    file_type: str,
    filename: str,
    content: str,
) -> dict:
    """コンテンツをファイルに保存する。

    ファイル名が重複しないようにタイムスタンプを付与する。
    例: 2026-03_report.txt → 2026-03_report_20260326_153045.txt

    Args:
        file_type: "csv" or "txt"。
        filename: 保存するファイル名（拡張子を含む）。
        content: ファイルに書き込む内容。

    Returns:
        dict: 保存結果。
            {
                "success": True,
                "filepath": "data/exports/2026-03_report_20260326_153045.txt",
                "filename": "2026-03_report_20260326_153045.txt",
            }

    Raises:
        ValueError: file_typeがcsv・txt以外の場合。
    """
    if file_type not in ("csv", "txt"):
        raise ValueError(f"未対応のファイル形式: {file_type}")

    # エクスポートディレクトリが存在しない場合は作成する
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # タイムスタンプを付与してファイル名の重複を避ける
    # 例: 2026-03_report.txt → 2026-03_report_20260326_153045.txt
    stem = Path(filename).stem      # 拡張子なしのファイル名
    suffix = Path(filename).suffix  # 拡張子（.csv / .txt）
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_filename = f"{stem}_{timestamp}{suffix}"

    filepath = EXPORTS_DIR / unique_filename

    # UTF-8で保存する（日本語が含まれるため）
    filepath.write_text(content, encoding="utf-8")

    logger.info(f"ファイル出力: {filepath}")

    return {
        "success": True,
        "filepath": str(filepath),
        "filename": unique_filename,
    }
