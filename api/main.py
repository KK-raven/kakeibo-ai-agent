# api/main.py
"""
FastAPI アプリケーション

役割:
- チャット API エンドポイントを提供する
- アプリ起動時に DB を初期化する
- Streamlit（Phase 3）からのリクエストを受け付ける

エンドポイント:
- GET /health: ヘルスチェック（Phase 1 で作成済み）
- POST /chat: チャット API（ユーザー入力 → Agent → 応答）
"""

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from api.db.connection import init_db
from api.models.schemas import ChatRequest, ChatResponse
from api.agent.core import chat
from api.utils.logger import get_logger

logger = get_logger(__name__)

# .env から環境変数を読み込む（OPENAI_API_KEY 等）
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    アプリのライフサイクル管理。

    yield の前: 起動時の処理（DB 初期化）
    yield の後: 終了時の処理（現時点では特になし）
    """
    # --- startup ---
    logger.info("アプリ起動: DB初期化")
    init_db()
    yield
    # --- shutdown ---
    logger.info("アプリ終了")


app = FastAPI(
    title="家計簿AIエージェント",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health_check():
    """ヘルスチェック。サービスが起動しているか確認する。"""
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(request: ChatRequest):
    """
    チャット API エンドポイント。

    処理の流れ:
    1. リクエストから message と conversation_history を取り出す
    2. agent.core.chat() に渡す
    3. 結果を ChatResponse 形式で返す

    リクエスト例:
    {
        "message": "セブンで弁当500円買った",
        "conversation_history": [...]
    }

    レスポンス例:
    {
        "response": "セブンイレブンで弁当500円を食費として登録しました。",
        "tool_results": [...]
    }
    """
    logger.info(f"リクエスト受信: {request.message}")

    result = chat(
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
    limit: int = 20,
):
    """
    取引履歴取得エンドポイント。
    Streamlitのサイドバー表示など、チャットを経由しないデータ取得に使う。
    クエリパラメータで絞り込みが可能。limitで取得件数を制限する。

    Args:
        year_month: "YYYY-MM" 形式で月絞り込み（例: "2026-03"）。
        category: カテゴリ名で絞り込み。
        type: "income" or "expense" で絞り込み。
        limit: 取得件数の上限。デフォルト20件。
    """
    from api.db.crud import get_transactions
    transactions = get_transactions(
        year_month=year_month,
        category=category,
        type=type,
    )
    return {"transactions": transactions[:limit]}


@app.get("/category_summary")
def get_category_summary_endpoint(
    year_month: str,
    type: str = "expense",
    person: str = "自分",
):
    """
    カテゴリ別集計取得エンドポイント。
    Streamlitのグラフ表示など、チャットを経由しないデータ取得に使う。

    Args:
        year_month: "YYYY-MM" 形式（必須）。
        type: "expense" or "income"。デフォルトは支出。
        person: 誰の集計か。デフォルトは自分。
    """
    from api.db.crud import get_category_summary
    summary = get_category_summary(
        year_month=year_month,
        type=type,
        person=person,
    )
    return {"summary": summary}


@app.get("/exports/{filename}")
def download_export(filename: str):
    """
    エクスポートファイルのダウンロードエンドポイント。
    Agentがdata/exports/に保存したファイルをStreamlitが取得するために使う。

    Args:
        filename: ダウンロードするファイル名。export_fileツールが返したfilenameを使う。
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    filepath = Path("/app/data/exports") / filename

    # ファイルが存在しない場合は404を返す
    if not filepath.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"ファイルが見つかりません: {filename}")

    # media_typeはファイル形式に応じて切り替える
    # text/plainにしないとブラウザが直接開こうとする場合がある
    media_type = "text/csv" if filename.endswith(".csv") else "text/plain"

    return FileResponse(
        path=filepath,
        filename=filename,
        media_type=media_type,
    )
