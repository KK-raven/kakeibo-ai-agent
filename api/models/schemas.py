# api/models/schemas.py
"""
FastAPI のリクエスト/レスポンスの型定義

役割:
- リクエストの JSON 形式を定義・バリデーションする
- レスポンスの形式を定義する
- FastAPI が自動で API ドキュメント（/docs）を生成する際にも使われる

Pydantic の BaseModel を継承して定義する。
フィールド名と型を書くだけで、FastAPI がリクエストの検証を自動で行う。
"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """
    チャットAPIのリクエスト。

    Streamlit → FastAPI に送るJSON:
    {
        "message": "セブンで弁当500円買った",
        "conversation_history": [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "こんにちは！"},
            ...
        ]
    }
    """
    message: str = Field(
        ...,
        description="ユーザーの入力メッセージ",
        min_length=1,
    )
    conversation_history: list[dict] | None = Field(
        default=None,
        description="会話履歴。role/content の辞書リスト。",
    )


class ToolResult(BaseModel):
    """
    Tool 実行結果の1件分。デバッグ・ログ用。
    """
    tool: str = Field(description="Tool名")
    args: dict = Field(description="Toolに渡した引数")
    result: dict | list | bool | str | None = Field(
        default=None,
        description="Tool の実行結果",
    )
    error: str | None = Field(
        default=None,
        description="エラー発生時のメッセージ",
    )


class ChatResponse(BaseModel):
    """
    チャットAPIのレスポンス。

    FastAPI → Streamlit に返すJSON:
    {
        "response": "セブンイレブンで弁当500円を食費として登録しました。",
        "tool_results": [...]
    }
    """
    response: str = Field(description="LLMの応答テキスト")
    tool_results: list[ToolResult] = Field(
        default_factory=list,
        description="実行された Tool の結果リスト",
    )
    