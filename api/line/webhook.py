# api/line/webhook.py
"""
LINE Bot webhook エンドポイント

LINEプラットフォームからのwebhookリクエストを受け取り、
Agentパイプラインに中継して応答を返す。

処理フロー:
1. LINEプラットフォームからPOSTリクエストを受信
2. 署名を検証（不正なリクエストを排除）
3. テキストメッセージイベントを処理
4. LINEユーザーIDからアプリのuser_idを解決
5. chat()でAgentの応答を生成
6. Reply APIで応答を返信（課金対象外）
"""

import os

from fastapi import APIRouter, Request, HTTPException

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FollowEvent,
    MessageEvent,
    TextMessageContent,
)

from api.agent.core import chat
from api.db import crud
from api.db.connection import get_or_create_user
from api.utils.logger import get_logger

logger = get_logger(__name__)

# --- LINE SDK 初期化 ---
channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

if not channel_secret or not channel_access_token:
    logger.warning(
        "LINE_CHANNEL_SECRET または LINE_CHANNEL_ACCESS_TOKEN が"
        "未設定です。LINE Bot機能は無効になります。"
    )

configuration = Configuration(access_token=channel_access_token or "")
handler = WebhookHandler(channel_secret or "dummy")

router = APIRouter()


@router.post("/callback")
async def line_callback(request: Request):
    """LINE webhookエンドポイント。

    LINEプラットフォームがユーザーのアクション（メッセージ送信等）を
    このエンドポイントにPOSTする。署名検証により、
    LINEプラットフォーム以外からのリクエストを拒否する。
    """
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")

    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        logger.warning("LINE webhook署名検証失敗")
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    """テキストメッセージを受信した時の処理。

    セッションタイムアウト（10分）が発生していた場合は、
    新しい会話である旨をユーザーに通知する。
    """
    line_user_id = event.source.user_id
    user_message = event.message.text

    logger.info(
        f"LINE受信: user={line_user_id} message={user_message}"
    )

    user_id = get_or_create_user(
        line_user_id=line_user_id,
        display_name=None,
    )

    # 会話履歴を取得（タイムアウト判定付き）
    conversation_history, is_new_session = (
        crud.get_conversation_history(user_id)
    )

    result = chat(
        user_id=user_id,
        user_message=user_message,
        conversation_history=conversation_history,
    )

    response_text = result["response"]

    # セッションリセット時はその旨を先頭に付加
    if is_new_session:
        response_text = (
            "（前回の会話から時間が空いたため、"
            "新しい会話として対応しています）\n\n"
            + response_text
        )

    # 今回のやりとりを保存
    crud.save_conversation_messages(user_id, [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": response_text},
    ])

    # LINEのメッセージ上限は5,000文字。
    # 超える場合は末尾を切り詰める（安全策。通常の応答では到達しない）
    if len(response_text) > 5000:
        response_text = response_text[:4990] + "\n...（省略）"

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=response_text)],
            )
        )

    logger.info(f"LINE返信: user={line_user_id}")


@handler.add(FollowEvent)
def handle_follow(event: FollowEvent):
    """友だち追加時の処理。

    ユーザーがLINE公式アカウントを友だち追加した時に呼ばれる。
    ユーザー登録とウェルカムメッセージの送信を行う。
    """
    line_user_id = event.source.user_id

    user_id = get_or_create_user(
        line_user_id=line_user_id,
        display_name=None,
    )

    logger.info(
        f"LINE友だち追加: user={line_user_id} user_id={user_id}"
    )

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=(
                            "友だち追加ありがとうございます！\n"
                            "家計簿AIアシスタントです。\n\n"
                            "「コンビニで300円」のように話しかけると、"
                            "支出を記録できます。\n"
                            "「今月の支出を教えて」で集計も確認できます。"
                        )
                    )
                ],
            )
        )
