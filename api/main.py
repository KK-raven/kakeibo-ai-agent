# api/main.py
"""
FastAPI アプリケーション

エンドポイント:
- GET /health: ヘルスチェック
- POST /chat: チャット API（ユーザー入力 → Agent → 応答）
- GET /transactions: 取引履歴取得（サイドバー表示用）
- GET /category_summary: カテゴリ別集計取得（グラフ表示用）
- GET /exports/{filename}: エクスポートファイルのダウンロード
"""

import os
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from api.db.connection import close_pool, get_demo_user_id, init_db
from api.models.schemas import ChatRequest, ChatResponse
from api.agent.core import chat
from api.utils.logger import get_logger

logger = get_logger(__name__)

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """アプリのライフサイクル管理。"""
    logger.info("アプリ起動: DB初期化")
    init_db()
    yield
    logger.info("アプリ終了: コネクションプール解放")
    close_pool()


app = FastAPI(
    title="家計簿AIエージェント",
    version="1.5.0",
    lifespan=lifespan,
)

from api.line.webhook import router as line_router
app.include_router(line_router, prefix="/line")


@app.get("/health")
def health_check():
    """ヘルスチェック。"""
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(request: ChatRequest):
    """チャット API エンドポイント。

    リクエスト例:
    {
        "user_id": 1,
        "message": "セブンで弁当500円買った",
        "conversation_history": [...]
    }
    """
    logger.info(
        f"リクエスト受信: user_id={request.user_id}"
        f" message={request.message}"
    )

    result = chat(
        user_id=request.user_id,
        user_message=request.message,
        conversation_history=request.conversation_history,
    )

    return ChatResponse(
        response=result["response"],
        tool_results=result.get("tool_results", []),
    )


@app.get("/transactions")
def get_transactions_endpoint(
    year_month: str | None = None,
    category: str | None = None,
    type: str | None = None,
    payment_method: str | None = None,
    amount: int | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    limit: int = 100,
    user_id: int | None = None,
):
    """取引履歴取得エンドポイント。

    year_month と start_month/end_month はどちらか一方を指定する。
    user_id省略時はデモユーザーのデータを返す。

    Args:
        year_month: "YYYY-MM" 形式で月絞り込み。
        category: カテゴリ名で絞り込み。
        type: "income" or "expense" で絞り込み。
        payment_method: 支払方法で完全一致検索。
        amount: 金額で完全一致検索。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        limit: 取得件数の上限。デフォルト20件。
        user_id: ユーザーID。省略時はデモユーザー。
    """
    from api.db.crud import get_transactions
    uid = user_id if user_id is not None else get_demo_user_id()
    transactions = get_transactions(
        user_id=uid,
        year_month=year_month,
        category=category,
        type=type,
        payment_method=payment_method,
        amount=amount,
        start_month=start_month,
        end_month=end_month,
    )
    return {"transactions": transactions[:limit]}


@app.get("/category_summary")
def get_category_summary_endpoint(
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
    user_id: int | None = None,
):
    """カテゴリ別集計取得エンドポイント。

    year_month と start_month/end_month はどちらか一方を指定する。
    user_id省略時はデモユーザーのデータを返す。

    Args:
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。固定費の表示制御。
        user_id: ユーザーID。省略時はデモユーザー。
    """
    from api.db.crud import get_category_summary
    uid = user_id if user_id is not None else get_demo_user_id()
    summary = get_category_summary(
        user_id=uid,
        year_month=year_month,
        type=type,
        person=person,
        start_month=start_month,
        end_month=end_month,
        fixed_mode=fixed_mode,
    )
    return {"summary": summary}


@app.get("/store_summary")
def get_store_summary_endpoint(
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
    user_id: int | None = None,
):
    """店別集計取得エンドポイント。

    year_month と start_month/end_month はどちらか一方を指定する。
    user_id省略時はデモユーザーのデータを返す。

    Args:
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。固定費の表示制御。
        user_id: ユーザーID。省略時はデモユーザー。
    """
    from api.db.crud import get_store_summary
    uid = user_id if user_id is not None else get_demo_user_id()
    summary = get_store_summary(
        user_id=uid,
        year_month=year_month,
        type=type,
        person=person,
        start_month=start_month,
        end_month=end_month,
        fixed_mode=fixed_mode,
    )
    return {"summary": summary}


@app.get("/item_summary")
def get_item_summary_endpoint(
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
    user_id: int | None = None,
):
    """品目別集計取得エンドポイント。

    year_month と start_month/end_month はどちらか一方を指定する。
    user_id省略時はデモユーザーのデータを返す。

    Args:
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。固定費の表示制御。
        user_id: ユーザーID。省略時はデモユーザー。
    """
    from api.db.crud import get_item_summary
    uid = user_id if user_id is not None else get_demo_user_id()
    summary = get_item_summary(
        user_id=uid,
        year_month=year_month,
        type=type,
        person=person,
        start_month=start_month,
        end_month=end_month,
        fixed_mode=fixed_mode,
    )
    return {"summary": summary}


@app.get("/payment_method_summary")
def get_payment_method_summary_endpoint(
    year_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
    start_month: str | None = None,
    end_month: str | None = None,
    fixed_mode: str = "show",
    user_id: int | None = None,
):
    """支払方法別集計取得エンドポイント。

    year_month と start_month/end_month はどちらか一方を指定する。
    user_id省略時はデモユーザーのデータを返す。

    Args:
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        type: "expense" or "income"。
        person: 誰の集計か。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        fixed_mode: "show" / "group" / "hide"。固定費の表示制御。
        user_id: ユーザーID。省略時はデモユーザー。
    """
    from api.db.crud import get_payment_method_summary
    uid = user_id if user_id is not None else get_demo_user_id()
    summary = get_payment_method_summary(
        user_id=uid,
        year_month=year_month,
        type=type,
        person=person,
        start_month=start_month,
        end_month=end_month,
        fixed_mode=fixed_mode,
    )
    return {"summary": summary}


@app.get("/monthly_trend")
def get_monthly_trend_endpoint(
    start_month: str,
    end_month: str,
    type: str = "expense",
    person: str = "自分",
    fixed_mode: str = "show",
    user_id: int | None = None,
):
    """月別推移取得エンドポイント。

    start_month〜end_monthの範囲で月別・カテゴリ別に集計する。
    user_id省略時はデモユーザーのデータを返す。

    Args:
        start_month: 開始月 "YYYY-MM" 形式（必須）。
        end_month: 終了月 "YYYY-MM" 形式（必須）。
        type: "expense" or "income"。
        person: 誰の集計か。
        fixed_mode: "show" / "group" / "hide"。固定費の表示制御。
        user_id: ユーザーID。省略時はデモユーザー。
    """
    from api.db.crud import get_monthly_trend
    uid = user_id if user_id is not None else get_demo_user_id()
    trend = get_monthly_trend(
        user_id=uid,
        start_month=start_month,
        end_month=end_month,
        type=type,
        person=person,
        fixed_mode=fixed_mode,
    )
    return {"trend": trend}


@app.get("/exports/{filename}")
def download_export(filename: str):
    """エクスポートファイルのダウンロードエンドポイント。

    Args:
        filename: ダウンロードするファイル名。
    """
    from pathlib import Path
    from fastapi.responses import FileResponse
    from fastapi import HTTPException

    filepath = Path("/app/data/exports") / filename

    if not filepath.exists():
        raise HTTPException(
            status_code=404,
            detail=f"ファイルが見つかりません: {filename}",
        )

    media_type = (
        "text/csv" if filename.endswith(".csv") else "text/plain"
    )

    return FileResponse(
        path=filepath,
        filename=filename,
        media_type=media_type,
    )


@app.get("/line/user_id")
def resolve_user_id(line_user_id: str):
    """LINEユーザーIDからアプリのuser_idを返す。

    LIFFダッシュボードがデータ取得時にuser_idを必要とするため、
    LIFF SDKで取得したLINEユーザーIDをこのエンドポイントで変換する。

    Args:
        line_user_id: LINEのユーザーID。
    """
    from api.db.connection import get_or_create_user

    user_id = get_or_create_user(line_user_id=line_user_id)
    return {"user_id": user_id}


@app.get("/liff/dashboard", response_class=HTMLResponse)
def liff_dashboard():
    """LIFFダッシュボードのHTMLを返す。

    LIFF IDを環境変数から読み取り、HTMLに埋め込んで返す。
    LIFF IDはLINE DevelopersコンソールでLIFFアプリ登録後に取得する。
    """
    liff_id = os.environ.get("LIFF_ID", "")

    html_path = os.path.join(
        os.path.dirname(__file__), "liff", "dashboard.html",
    )
    with open(html_path, encoding="utf-8") as f:
        html_content = f.read()

    html_content = html_content.replace("{{LIFF_ID}}", liff_id)
    return HTMLResponse(content=html_content)


@app.get("/liff/guide", response_class=HTMLResponse)
def liff_guide():
    """使い方ガイドのHTMLを返す。

    LINEリッチメニューからアクセスする説明書ページ。
    認証不要で誰でも閲覧できる。
    """
    html_path = os.path.join(
        os.path.dirname(__file__), "liff", "guide.html",
    )
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/download/csv")
def download_csv(
    user_id: int,
    year_month: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    person: str = "自分",
):
    """取引データをCSVファイルとして返す。

    year_month と start_month/end_month はどちらか一方を指定する。
    LIFFダッシュボードのダウンロードボタンから呼ばれる。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        person: 誰のデータか。
    """
    from io import StringIO
    import csv
    from fastapi.responses import StreamingResponse
    from api.db.crud import get_transactions
    transactions = get_transactions(
        user_id=user_id,
        year_month=year_month,
        start_month=start_month,
        end_month=end_month,
        person=person,
    )
    output = StringIO()
    # BOM付きUTF-8でExcelでも文字化けしない
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "日付", "種別", "金額", "カテゴリ",
        "店名", "品目", "メモ", "支払方法", "名義",
    ])
    for tx in transactions:
        writer.writerow([
            tx["date"],
            "収入" if tx["type"] == "income" else "支出",
            tx["amount"],
            tx["category"],
            tx.get("store_name", ""),
            tx.get("item", ""),
            tx.get("memo", ""),
            tx.get("payment_method", ""),
            tx.get("person", ""),
        ])
    output.seek(0)
    from urllib.parse import quote
    period = year_month or f"{start_month}_{end_month}"
    filename = f"家計簿_{period}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            ),
        },
    )


@app.get("/download/txt")
def download_txt(
    user_id: int,
    year_month: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    type: str = "expense",
    person: str = "自分",
):
    """集計レポートをTXTファイルとして返す。

    year_month と start_month/end_month はどちらか一方を指定する。
    LIFFダッシュボードのダウンロードボタンから呼ばれる。

    Args:
        user_id: ユーザーID。
        year_month: "YYYY-MM" 形式。1ヶ月指定の場合に使用。
        start_month: 開始月 "YYYY-MM" 形式。期間指定の場合に使用。
        end_month: 終了月 "YYYY-MM" 形式。期間指定の場合に使用。
        type: "expense" or "income"。
        person: 誰のデータか。
    """
    from fastapi.responses import StreamingResponse
    from api.db.crud import get_category_summary, get_transactions
    summary = get_category_summary(
        user_id=user_id,
        year_month=year_month,
        start_month=start_month,
        end_month=end_month,
        type=type,
        person=person,
    )
    transactions = get_transactions(
        user_id=user_id,
        year_month=year_month,
        start_month=start_month,
        end_month=end_month,
        person=person,
    )
    period = year_month or f"{start_month}〜{end_month}"
    type_label = "収入" if type == "income" else "支出"
    text = f"家計簿レポート（{period}）\n"
    text += "=" * 40 + "\n\n"
    if summary:
        text += f"【カテゴリ別{type_label}】\n"
        text += "-" * 30 + "\n"
        total = 0
        for s in summary:
            text += f"{s['category']}: ¥{s['total']:,}（{s['count']}件）\n"
            total += s["total"]
        text += "-" * 30 + "\n"
        text += f"合計: ¥{total:,}\n\n"
    if transactions:
        text += "【取引明細】\n"
        text += "-" * 30 + "\n"
        for tx in transactions:
            type_str = "収入" if tx["type"] == "income" else "支出"
            store = f" {tx['store_name']}" if tx.get("store_name") else ""
            text += (
                f"{tx['date']} [{type_str}] "
                f"{tx['category']}{store} "
                f"¥{tx['amount']:,}\n"
            )
    from urllib.parse import quote
    filename = f"家計簿_{period.replace('〜', '_')}.txt"
    return StreamingResponse(
        iter([text]),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            ),
        },
    )
