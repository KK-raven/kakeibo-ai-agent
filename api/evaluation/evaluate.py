# api/evaluation/evaluate.py
"""
Agent 評価スクリプト

目的:
- 52問のテストケースを各N回実行し、Agentのツール選択精度を測定する
- ツール選択の Confusion Matrix・Precision/Recall/F1 を算出する
- 引数の主要項目の正否を判定する
- 結果をJSON + テキストレポートで出力する

使い方:
  プロジェクトルートから実行する:
  python3 -m api.evaluation.evaluate

注意:
- 実行にはOpenAI APIキーが必要（環境変数 OPENAI_API_KEY）
- 実行ごとにテスト用DBが初期化される（本番DBには影響しない）
- N=5で260回のAPI呼び出しが発生する（GPT-4o-mini、数百円程度）
"""

# === DB切り替え ===
# connection.py は最初の接続取得時に DATABASE_URL を確定するため、
# 他のモジュールを import する前に環境変数を設定する必要がある。
# これにより chat() 内の crud 関数が評価専用DBを参照する。
#
# 評価専用DB(kakeibo_eval)は本番(kakeibo)と同じPostgreSQLコンテナ内の
# 別データベース。本番DBには一切書き込まず、起動時にデータをスナップショット
# するのみ。各ケース実行前に kakeibo_eval を本番データの状態へ復元する。
import os

# 接続のベース（認証情報＋ホスト）。ホストから実行する場合、composeの
# ポート公開によりlocalhost:5432で到達できる。Docker内から実行する等で
# 変える場合は環境変数 EVAL_DB_BASE で上書きする。
EVAL_DB_BASE = os.environ.get(
    "EVAL_DB_BASE", "postgresql://kakeibo:kakeibo_dev@localhost:5432"
)
PROD_DB_NAME = "kakeibo"
EVAL_DB_NAME = "kakeibo_eval"

os.environ["DATABASE_URL"] = f"{EVAL_DB_BASE}/{EVAL_DB_NAME}"

import json
import time
from datetime import date, datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from statsmodels.stats.proportion import proportion_confint

from api.agent.core import chat
from api.db.connection import get_connection, init_db, release_connection
from api.utils.logger import get_logger

logger = get_logger(__name__)


# --- 定数 ---

# 各テストケースの実行回数
# N=5 × 52問 = 260回。95%CI は Wilson法で ±約4%（p=0.9の場合）
N_RUNS = 5

# 評価で使用する user_id。
# 本番DBのコピー上で動作し、最初に作られるデモユーザー（id=1）を対象とする。
EVAL_USER_ID = 1

TEST_CASES_PATH = Path(__file__).parent / "test_cases.json"
RESULTS_DIR = Path(__file__).parent / "results"

# スナップショット/復元の対象テーブル（親→子のFK依存順）。
# 全テーブルが users.id のみを参照するため、users を先頭にすれば足りる。
SNAPSHOT_TABLES = [
    "users",
    "payment_methods",
    "credit_cards",
    "transactions",
    "fixed_expenses",
    "budgets",
    "settings",
    "store_category_mapping",
    "conversation_history",
]

# 起動時に本番DBから読み込んだ各テーブルの全行（リセット時に復元する）。
_PRODUCTION_SNAPSHOT: dict[str, list[dict]] = {}


# ==================== ヘルパー関数 ====================

def _resolve_relative_dates(expected_args: dict) -> dict:
    """expected_args 内の _relative: プレースホルダを実際の値に変換する。

    テストケースは実行日に依存しない形で定義されているため、
    「今月」「昨日」等の相対表現をプレースホルダで記述している。
    評価時に実際の日付に変換することで、正解判定が可能になる。

    Args:
        expected_args: テストケースの期待引数（変換前）。

    Returns:
        プレースホルダを実際の値に置換した辞書。
    """
    today = date.today()
    resolved = {}

    for key, value in expected_args.items():
        # メタ情報（_note等）はスキップ
        if key.startswith("_"):
            continue

        if isinstance(value, str) and value.startswith("_relative:"):
            tag = value.replace("_relative:", "")

            if tag == "this_month":
                resolved[key] = f"{today.year:04d}-{today.month:02d}"
            elif tag == "last_month":
                last = today.replace(day=1) - timedelta(days=1)
                resolved[key] = f"{last.year:04d}-{last.month:02d}"
            elif tag == "this_year":
                resolved[key] = today.year
            elif tag == "this_month_num":
                resolved[key] = today.month
            elif tag == "specific_date":
                # 具体的な日付はケースごとに異なるためスキップ
                continue
            elif tag == "last_december":
                resolved[key] = f"{today.year - 1}-12"
            else:
                logger.warning(f"未知の相対日付タグ: {tag}")
                continue
        elif isinstance(value, list):
            # 複数正解の場合はそのまま保持
            resolved[key] = value
        else:
            resolved[key] = value

    return resolved


# ==================== 正解判定 ====================

def _check_tool_match(expected_tool, actual_tools: list[str]) -> dict:
    """ツール選択の正解判定を行う。

    Args:
        expected_tool: 期待されるツール名。
            - str: 1つのツールが正解
            - list: 複数のうちいずれかが正解
            - None: ツールを呼ばないのが正解
            - "_missing:xxx": ツールが未実装であることを確認するケース
            - "_special:xxx": 標準的な判定ができないケース
        actual_tools: 実際に呼ばれたツール名のリスト（0個以上）。

    Returns:
        {
            "tool_correct": bool|None,  # ツール選択が正しいか（特殊ケースはNone）
            "actual_tool": str|None,    # 実際に呼ばれた最初のツール名
            "is_special": bool,         # 特殊ケースか（自動判定の対象外）
            "selected_alternative": str|None,  # 複数正解ケースで選ばれたツール名
        }
    """
    actual_first = actual_tools[0] if actual_tools else None

    # 特殊ケース：ツール未実装・標準判定不可
    if isinstance(expected_tool, str) and expected_tool.startswith(("_missing:", "_special:")):
        return {
            "tool_correct": None,
            "actual_tool": actual_first,
            "is_special": True,
            "selected_alternative": None,
        }

    # ツール不要ケース：ツールが1つも呼ばれなければ正解
    if expected_tool is None:
        return {
            "tool_correct": len(actual_tools) == 0,
            "actual_tool": actual_first,
            "is_special": False,
            "selected_alternative": None,
        }

    # 複数正解ケース：リスト内のいずれかに一致すれば正解
    if isinstance(expected_tool, list):
        correct = actual_first in expected_tool
        return {
            "tool_correct": correct,
            "actual_tool": actual_first,
            "is_special": False,
            "selected_alternative": actual_first if correct else None,
        }

    # export_file は事前にデータ取得ツールが呼ばれるため、
    # 最初のツールではなく、ツール一覧に含まれているかで判定する
    if expected_tool == "export_file":
        return {
            "tool_correct": "export_file" in actual_tools,
            "actual_tool": actual_first,
            "is_special": False,
            "selected_alternative": None,
        }

    # 単一正解ケース（expected_tool = str）
    return {
        "tool_correct": actual_first == expected_tool,
        "actual_tool": actual_first,
        "is_special": False,
        "selected_alternative": None,
    }


def _check_args_match(expected_args: dict, actual_args: dict) -> dict:
    """引数の主要項目の正解判定を行う。

    expected_args に定義されたキーのみを評価対象とする。
    定義されていない引数（LLMが追加で推定したもの）は判定しない。

    Args:
        expected_args: 期待される引数（_resolve_relative_dates 適用済み）。
        actual_args: 実際にLLMが生成した引数。

    Returns:
        {
            "args_results": {key: {"expected": ..., "actual": ..., "correct": bool}},
            "args_accuracy": float,  # 正解率（0.0〜1.0）
        }
    """
    if not expected_args:
        return {"args_results": {}, "args_accuracy": 1.0}

    results = {}
    correct_count = 0

    for key, expected_value in expected_args.items():
        actual_value = actual_args.get(key)

        # 複数正解の場合（例：category が ["食費", "日用品"]）
        if isinstance(expected_value, list):
            is_correct = actual_value in expected_value
        else:
            is_correct = actual_value == expected_value

        results[key] = {
            "expected": expected_value,
            "actual": actual_value,
            "correct": is_correct,
        }
        if is_correct:
            correct_count += 1

    accuracy = correct_count / len(results) if results else 1.0
    return {"args_results": results, "args_accuracy": accuracy}


# ==================== DB管理 ====================

def _ensure_eval_database():
    """評価専用DB(kakeibo_eval)が無ければ作成する。

    CREATE DATABASE はトランザクション内で実行できないため、
    maintenance DB(postgres)へ autocommit で接続して実行する。
    既存の本番DB(kakeibo)には接続せず、影響を与えない。
    """
    conn = psycopg2.connect(f"{EVAL_DB_BASE}/postgres")
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (EVAL_DB_NAME,)
        )
        if cur.fetchone() is None:
            # データベース名は識別子のためパラメータ化できない。
            # 定数なのでインジェクションの懸念はない。
            cur.execute(f'CREATE DATABASE "{EVAL_DB_NAME}"')
            logger.info(f"評価用DBを作成: {EVAL_DB_NAME}")
        else:
            logger.info(f"評価用DBは既存: {EVAL_DB_NAME}")
    finally:
        conn.close()


def _snapshot_production():
    """本番DB(kakeibo)の全データをメモリに読み込む。

    SELECT のみで本番DBへの書き込みは一切行わない。
    読み込んだデータは各ケース実行前のリセットで kakeibo_eval に復元する。
    """
    conn = psycopg2.connect(
        f"{EVAL_DB_BASE}/{PROD_DB_NAME}", cursor_factory=RealDictCursor
    )
    try:
        cur = conn.cursor()
        for table in SNAPSHOT_TABLES:
            cur.execute(f"SELECT * FROM {table} ORDER BY id")
            _PRODUCTION_SNAPSHOT[table] = cur.fetchall()
    finally:
        conn.close()

    counts = {t: len(rows) for t, rows in _PRODUCTION_SNAPSHOT.items() if rows}
    logger.info(f"本番データをスナップショット: {counts}")


def _setup_test_db():
    """評価専用DBを準備する。

    1. kakeibo_eval を作成（無ければ）
    2. スキーマを作成（init_db）
    3. 本番データをメモリにスナップショット
    4. 初回リセットで本番の状態へ復元

    本番DB(kakeibo)へは SELECT するのみで、書き込みは行わない。
    """
    _ensure_eval_database()
    init_db()  # DATABASE_URL=kakeibo_eval なので評価DBにスキーマを作る
    _snapshot_production()
    _reset_test_db()
    logger.info(f"評価用DBを準備完了: {EVAL_DB_NAME}")


def _reset_test_db():
    """評価専用DBを本番データの状態へ戻す。

    各テストケースの実行前に呼び出して、前のケースの副作用
    （register_transaction 等の書き込み）を除去する。
    全テーブルを TRUNCATE 後にスナップショットを復元し、
    SERIAL のシーケンスを最大idへ補正する。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 全テーブルを空にする（子テーブルのFKも一括処理するため CASCADE）
        cur.execute(
            "TRUNCATE {} RESTART IDENTITY CASCADE".format(
                ", ".join(SNAPSHOT_TABLES)
            )
        )

        # 親→子の順でスナップショットを復元
        for table in SNAPSHOT_TABLES:
            rows = _PRODUCTION_SNAPSHOT.get(table) or []
            if not rows:
                continue

            columns = list(rows[0].keys())
            col_sql = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            insert_sql = (
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
            )
            cur.executemany(
                insert_sql,
                [[row[c] for c in columns] for row in rows],
            )

            # 明示的にidを挿入したためSERIALのシーケンスが進んでいない。
            # 次の自動採番が衝突しないよう最大idへ補正する。
            if "id" in columns:
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    "MAX(id)) FROM {}".format(table),
                    (table,),
                )

        conn.commit()
    finally:
        release_connection(conn)


# ==================== メイン実行 ====================

def run_evaluation():
    """評価を実行するメイン関数。

    1. テストケースを読み込む
    2. テスト用DBを準備
    3. 各ケースをN回実行し、ツール選択・引数の正否を判定
    4. 結果を集計してレポート出力

    Returns:
        集計レポートの辞書。
    """
    # テストケース読み込み
    with open(TEST_CASES_PATH, encoding="utf-8") as f:
        test_cases = json.load(f)

    logger.info(f"テストケース数: {len(test_cases)}, 実行回数: N={N_RUNS}")
    logger.info(f"総API呼び出し予定: {len(test_cases) * N_RUNS}回")

    # 結果格納
    all_results = []
    # 選択分布記録（track_alternative 用）
    alternative_distribution = defaultdict(Counter)

    # テスト用DB準備
    _setup_test_db()

    start_time = time.time()

    for case in test_cases:
        case_id = case["id"]
        case_input = case["input"]
        expected_tool = case["expected_tool"]
        expected_args = _resolve_relative_dates(case.get("expected_args", {}))
        track = case.get("track_alternative", False)

        logger.info(f"--- ケース {case_id}: {case_input} ---")

        case_results = []

        for run in range(N_RUNS):
            # 各実行前にDBリセット
            # register_transaction 等の副作用を除去するため
            _reset_test_db()

            try:
                result = chat(EVAL_USER_ID, case_input, conversation_history=None)
            except Exception as e:
                logger.error(f"ケース {case_id} 実行 {run + 1} エラー: {e}")
                case_results.append({
                    "run": run + 1,
                    "error": str(e),
                    "tool_check": {
                        "tool_correct": False,
                        "actual_tool": None,
                        "is_special": False,
                        "selected_alternative": None,
                    },
                    "args_check": {"args_results": {}, "args_accuracy": 0.0},
                })
                continue

            # 実際に呼ばれたツール名・引数を抽出
            actual_tools = [tr["tool"] for tr in result.get("tool_results", [])]
            actual_args = (
                result["tool_results"][0].get("args", {})
                if result.get("tool_results")
                else {}
            )

            # ツール選択の正解判定
            tool_check = _check_tool_match(expected_tool, actual_tools)

            # 引数の正解判定（ツールが正しい場合のみ意味がある）
            if tool_check["tool_correct"]:
                args_check = _check_args_match(expected_args, actual_args)
            else:
                args_check = {"args_results": {}, "args_accuracy": 0.0}

            # 複数正解ケースの選択分布を記録
            if track and tool_check["selected_alternative"]:
                alternative_distribution[case_id][
                    tool_check["selected_alternative"]
                ] += 1

            case_results.append({
                "run": run + 1,
                "tool_check": tool_check,
                "args_check": args_check,
            })

            # API レートリミット対策
            time.sleep(0.5)

        all_results.append({
            "case_id": case_id,
            "group": case["group"],
            "input": case_input,
            "expected_tool": expected_tool,
            "difficulty": case["difficulty"],
            "note": case.get("note", ""),
            "runs": case_results,
        })

    elapsed = time.time() - start_time
    logger.info(f"評価完了: {elapsed:.1f}秒")

    # 集計・レポート出力
    report = _generate_report(all_results, alternative_distribution, elapsed)
    _save_results(all_results, report, alternative_distribution)

    # MLflowに記録
    _log_to_mlflow(report, RESULTS_DIR)

    return report


# ==================== 集計・レポート ====================

def _generate_report(
    all_results: list,
    alternative_distribution: dict,
    elapsed: float,
) -> dict:
    """全結果を集計してレポートを生成する。

    Args:
        all_results: 全ケースの実行結果。
        alternative_distribution: 選択分布記録。
        elapsed: 実行時間（秒）。

    Returns:
        集計レポートの辞書。
    """
    today = date.today().isoformat()

    # --- ツール選択の集計（特殊ケースを除く） ---
    tool_correct_count = 0
    tool_total_count = 0
    confusion_pairs = []
    case_accuracy = []
    group_stats = defaultdict(lambda: {"correct": 0, "total": 0})
    difficulty_stats = defaultdict(lambda: {"correct": 0, "total": 0})

    for case_result in all_results:
        case_correct = 0
        case_total = 0

        for run in case_result["runs"]:
            tc = run["tool_check"]

            # 特殊ケース（_missing, _special）は精度計算から除外
            if tc["is_special"]:
                continue

            case_total += 1
            tool_total_count += 1

            if tc["tool_correct"]:
                tool_correct_count += 1
                case_correct += 1

            # Confusion Matrix 用のペア作成
            expected = case_result["expected_tool"]
            if isinstance(expected, list):
                expected = "|".join(expected)
            if expected is None:
                expected = "NO_TOOL"
            actual = tc["actual_tool"] or "NO_TOOL"
            confusion_pairs.append((expected, actual))

            # グループ別集計
            group = case_result["group"]
            group_stats[group]["total"] += 1
            if tc["tool_correct"]:
                group_stats[group]["correct"] += 1

            # 難易度別集計
            diff = case_result["difficulty"]
            difficulty_stats[diff]["total"] += 1
            if tc["tool_correct"]:
                difficulty_stats[diff]["correct"] += 1

        if case_total > 0:
            case_accuracy.append({
                "case_id": case_result["case_id"],
                "input": case_result["input"],
                "accuracy": case_correct / case_total,
                "correct": case_correct,
                "total": case_total,
            })

    # 全体精度
    overall_accuracy = (
        tool_correct_count / tool_total_count if tool_total_count > 0 else 0.0
    )

    # 95%信頼区間（Wilson法）
    # 正規近似より統計的に正確。特に p が 0 や 1 に近い場合に差が出る。
    if tool_total_count > 0:
        ci_low, ci_high = proportion_confint(
            tool_correct_count,
            tool_total_count,
            alpha=0.05,
            method="wilson",
        )
    else:
        ci_low, ci_high = 0.0, 0.0

    # --- 引数精度の集計 ---
    args_correct_count = 0
    args_total_count = 0

    for case_result in all_results:
        for run in case_result["runs"]:
            for key, arg_result in run["args_check"]["args_results"].items():
                args_total_count += 1
                if arg_result["correct"]:
                    args_correct_count += 1

    args_accuracy = (
        args_correct_count / args_total_count if args_total_count > 0 else 0.0
    )

    # --- ツールごとの Precision / Recall / F1 ---
    tool_metrics = _calc_per_tool_metrics(confusion_pairs)

    # --- 不安定ケース（N回中で結果がばらつくケース） ---
    unstable_cases = [
        ca for ca in case_accuracy if 0 < ca["accuracy"] < 1.0
    ]

    report = {
        "metadata": {
            "date": today,
            "n_runs": N_RUNS,
            "n_cases": len(all_results),
            "total_runs": tool_total_count,
            "elapsed_seconds": round(elapsed, 1),
        },
        "overall": {
            "tool_selection_accuracy": round(overall_accuracy, 4),
            "ci_method": "wilson",
            "ci_low": round(ci_low, 4),
            "ci_high": round(ci_high, 4),
            "args_accuracy": round(args_accuracy, 4),
        },
        "by_group": {
            group: {
                "accuracy": round(
                    s["correct"] / s["total"], 4,
                ) if s["total"] > 0 else 0,
                "correct": s["correct"],
                "total": s["total"],
            }
            for group, s in sorted(group_stats.items())
        },
        "by_difficulty": {
            diff: {
                "accuracy": round(
                    s["correct"] / s["total"], 4,
                ) if s["total"] > 0 else 0,
                "correct": s["correct"],
                "total": s["total"],
            }
            for diff, s in sorted(difficulty_stats.items())
        },
        "per_tool_metrics": tool_metrics,
        "unstable_cases": unstable_cases,
        "alternative_distribution": {
            str(k): dict(v) for k, v in alternative_distribution.items()
        },
    }

    return report


def _calc_per_tool_metrics(confusion_pairs: list[tuple]) -> dict:
    """ツールごとの Precision / Recall / F1 を算出する。

    Precision = TP / (TP + FP)  そのツールだと判定したうち、実際に正しかった割合
    Recall    = TP / (TP + FN)  実際にそのツールが正解のケースのうち、正しく判定できた割合
    F1        = 2 * P * R / (P + R)  PrecisionとRecallの調和平均

    Args:
        confusion_pairs: (expected, actual) のタプルリスト。

    Returns:
        ツール名をキーとする {precision, recall, f1, tp, fp, fn} の辞書。
    """
    all_tools = set()
    for expected, actual in confusion_pairs:
        all_tools.add(expected)
        all_tools.add(actual)

    metrics = {}
    for tool in sorted(all_tools):
        tp = sum(1 for e, a in confusion_pairs if e == tool and a == tool)
        fp = sum(1 for e, a in confusion_pairs if e != tool and a == tool)
        fn = sum(1 for e, a in confusion_pairs if e == tool and a != tool)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )

        metrics[tool] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }

    return metrics


# ==================== 結果保存 ====================

def _save_results(
    all_results: list,
    report: dict,
    alternative_distribution: dict,
):
    """評価結果をファイルに保存する。

    3種類のファイルを出力する:
    - detail_YYYY-MM-DD.json: 全ケース × 全実行の詳細結果
    - report_YYYY-MM-DD.json: 集計レポート（プログラムで読みやすい）
    - report_YYYY-MM-DD.txt: テキストレポート（人間が読みやすい）

    Args:
        all_results: 全ケースの詳細結果。
        report: 集計レポート。
        alternative_distribution: 選択分布。
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # 詳細結果（全ケース × 全実行）
    detail_path = RESULTS_DIR / f"detail_{timestamp}.json"
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"詳細結果: {detail_path}")

    # 集計レポート（JSON）
    report_path = RESULTS_DIR / f"report_{timestamp}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"集計レポート: {report_path}")

    # テキストレポート
    text_path = RESULTS_DIR / f"report_{timestamp}.txt"
    with open(text_path, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("Agent 評価レポート\n")
        f.write(f"実行日: {report['metadata']['date']}\n")
        f.write(f"テストケース数: {report['metadata']['n_cases']}\n")
        f.write(f"実行回数: N={report['metadata']['n_runs']}\n")
        f.write(f"総実行数: {report['metadata']['total_runs']}\n")
        f.write(f"実行時間: {report['metadata']['elapsed_seconds']}秒\n")
        f.write("=" * 60 + "\n\n")

        f.write("--- 全体精度 ---\n")
        o = report["overall"]
        f.write(f"ツール選択精度: {o['tool_selection_accuracy']:.1%}\n")
        f.write(
            f"95%信頼区間 (Wilson): "
            f"[{o['ci_low']:.1%}, {o['ci_high']:.1%}]\n"
        )
        f.write(f"引数精度: {o['args_accuracy']:.1%}\n\n")

        f.write("--- グループ別精度 ---\n")
        for group, stats in report["by_group"].items():
            f.write(
                f"  {group}: {stats['accuracy']:.1%} "
                f"({stats['correct']}/{stats['total']})\n"
            )
        f.write("\n")

        f.write("--- 難易度別精度 ---\n")
        for diff, stats in report["by_difficulty"].items():
            f.write(
                f"  {diff}: {stats['accuracy']:.1%} "
                f"({stats['correct']}/{stats['total']})\n"
            )
        f.write("\n")

        f.write("--- ツール別 Precision / Recall / F1 ---\n")
        for tool, m in report["per_tool_metrics"].items():
            f.write(
                f"  {tool}: P={m['precision']:.2f} R={m['recall']:.2f} "
                f"F1={m['f1']:.2f} (TP={m['tp']} FP={m['fp']} FN={m['fn']})\n"
            )
        f.write("\n")

        if report["unstable_cases"]:
            f.write(
                f"--- 不安定ケース "
                f"({N_RUNS}回中で結果がばらつくケース) ---\n"
            )
            for uc in report["unstable_cases"]:
                f.write(
                    f"  ID {uc['case_id']}: {uc['accuracy']:.0%} "
                    f"({uc['correct']}/{uc['total']}) - {uc['input']}\n"
                )
            f.write("\n")

        if report["alternative_distribution"]:
            f.write("--- 複数正解ケースの選択分布 ---\n")
            for case_id, dist in report["alternative_distribution"].items():
                f.write(f"  ケース {case_id}: {dict(dist)}\n")

    logger.info(f"テキストレポート: {text_path}")


def _log_to_mlflow(report: dict, results_dir: Path):
    """評価結果を MLflow に記録する。

    MLflow の Run 1つに以下を記録する：
    - Parameters: モデル名・テストケース数・実行回数N等の実験設定
    - Metrics: ツール選択精度・引数精度・グループ別/難易度別の精度
    - Artifacts: 評価レポート（JSON/TXT）・詳細結果

    MLflow サーバーが起動していない場合はスキップする。

    Args:
        report: _generate_report() の戻り値。
        results_dir: レポートファイルが保存されたディレクトリ。
    """
    try:
        import mlflow
    except ImportError:
        logger.warning("mlflow がインストールされていません。MLflow記録をスキップします")
        return

    tracking_uri = "http://127.0.0.1:5001"
    experiment_name = "agent_evaluation"

    try:
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)
    except Exception as e:
        logger.warning(f"MLflow サーバーに接続できません: {e}。MLflow記録をスキップします")
        return

    try:
        with mlflow.start_run():
            # --- Parameters ---
            # 実験の再現に必要な設定値
            mlflow.log_param("model", "gpt-5-nano")
            mlflow.log_param("n_cases", report["metadata"]["n_cases"])
            mlflow.log_param("n_runs", report["metadata"]["n_runs"])
            mlflow.log_param("total_runs", report["metadata"]["total_runs"])

            # --- Metrics ---
            # 全体精度
            mlflow.log_metric(
                "tool_selection_accuracy",
                report["overall"]["tool_selection_accuracy"],
            )
            mlflow.log_metric("ci_low", report["overall"]["ci_low"])
            mlflow.log_metric("ci_high", report["overall"]["ci_high"])
            mlflow.log_metric("args_accuracy", report["overall"]["args_accuracy"])

            # グループ別精度
            for group, stats in report["by_group"].items():
                # MLflow のメトリクス名にスラッシュや中黒は使えないので
                # 安全な文字に変換する
                safe_name = group.replace("/", "_").replace("・", "_")
                mlflow.log_metric(f"group_{safe_name}", stats["accuracy"])

            # 難易度別精度
            for diff, stats in report["by_difficulty"].items():
                mlflow.log_metric(f"difficulty_{diff}", stats["accuracy"])

            # 不安定ケース数
            mlflow.log_metric(
                "unstable_cases", len(report["unstable_cases"]),
            )

            # --- Artifacts ---
            # results ディレクトリ内の最新ファイルを全てアップロード
            for filepath in results_dir.glob("*"):
                if filepath.is_file():
                    mlflow.log_artifact(str(filepath))

            logger.info("MLflow に記録しました")

    except Exception as e:
        logger.error(f"MLflow 記録中にエラー: {e}")


# ==================== エントリポイント ====================

if __name__ == "__main__":
    report = run_evaluation()

    print("\n" + "=" * 60)
    print("評価完了")
    o = report["overall"]
    print(f"ツール選択精度: {o['tool_selection_accuracy']:.1%}")
    print(
        f"95%信頼区間 (Wilson): "
        f"[{o['ci_low']:.1%}, {o['ci_high']:.1%}]"
    )
    print(f"引数精度: {o['args_accuracy']:.1%}")
    print(f"不安定ケース: {len(report['unstable_cases'])}件")
    print("=" * 60)
