# api/line/webhook.py
"""
LINE Bot webhook エンドポイント

LINEプラットフォームからのwebhookリクエストを受け取り、
Agentパイプラインに中継して応答を返す。

処理フロー:
1. LINEプラットフォームからPOSTリクエストを受信
2. 署名を検証（不正なリクエストを排除）
3. テキスト or 画像メッセージイベントを処理
4. LINEユーザーIDからアプリのuser_idを解決
5. chat()でAgentの応答を生成
6. Reply APIで応答を返信（課金対象外）
"""

import base64
import json
import os

from fastapi import APIRouter, Request, HTTPException
from openai import OpenAI

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FollowEvent,
    ImageMessageContent,
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

openai_client = OpenAI()

router = APIRouter()

# レシートOCR結果をchat()に渡す際のプレフィックス。
# core.pyのシステムプロンプトがこの文字列を検知してOCRフローに入る。
_OCR_PREFIX = "[レシートOCR結果]"

# Vision APIに渡すプロンプト。
# JSON以外の文字列を出力させないため、指示を明示する。
_VISION_PROMPT = """
このレシート画像から以下の情報をJSON形式で抽出してください。
読み取れない項目はnullにしてください。
カテゴリは以下から最も適切なものをLLMで推測して付与してください：
食費・光熱費・交通費・日用品・交際費・サブスク・医療費・衣服・娯楽・教育・家賃/住居・保険・その他

出力はJSON文字列のみとし、マークダウンのコードブロックや説明文は一切含めないこと。

{
  "store_name": "店名（文字列 or null）",
  "date": "日付（YYYY-MM-DD形式 or null）",
  "payment_method": "支払方法（文字列 or null）",
  "items": [
    {
      "name": "商品名",
      "category": "カテゴリ",
      "price": 金額（整数 or null）
    }
  ],
  "total": 合計金額（整数 or null）
}
"""


def extract_receipt_info(image_bytes: bytes) -> dict:
    """レシート画像からGPT-4o Visionで情報を抽出する。

    LINEから取得した画像バイナリをbase64エンコードし、
    GPT-4o Visionに送信して構造化JSONを得る。

    Args:
        image_bytes: LINEのContent APIから取得した画像バイナリ。

    Returns:
        抽出結果のdict。キーはstore_name, date, payment_method,
        items（name/category/priceのリスト）, total。
        読み取れない項目はNone。
        Vision APIの呼び出し失敗時は空のdictを返す。
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=1000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": _VISION_PROMPT,
                        },
                    ],
                }
            ],
        )
        raw = response.choices[0].message.content or ""
        return json.loads(raw)

    except json.JSONDecodeError as e:
        logger.error(f"Vision APIのJSON解析失敗: {e} / raw={raw!r}")
        return {}
    except Exception as e:
        logger.error(f"Vision API呼び出し失敗: {e}")
        return {}


def _format_ocr_result(ocr: dict) -> str:
    """OCR結果dictをchat()に渡すテキスト形式に変換する。

    core.pyのシステムプロンプトが解釈しやすいよう、
    _OCR_PREFIXで始まる構造化テキストに変換する。
    Vision APIが空dictを返した場合（読み取り失敗）は
    その旨を示す文字列を返す。

    Args:
        ocr: extract_receipt_info()の戻り値。

    Returns:
        _OCR_PREFIXで始まるテキスト文字列。
    """
    if not ocr:
        return f"{_OCR_PREFIX}\nレシートを読み取れませんでした。"

    lines = [_OCR_PREFIX]

    store = ocr.get("store_name") or "不明"
    lines.append(f"店名: {store}")

    date = ocr.get("date") or "不明"
    lines.append(f"日付: {date}")

    payment = ocr.get("payment_method") or "不明"
    lines.append(f"支払方法: {payment}")

    items = ocr.get("items") or []
    if items:
        lines.append("商品一覧:")
        for item in items:
            name = item.get("name", "不明")
            category = item.get("category", "その他")
            price = item.get("price")
            price_str = f"{price}円" if price is not None else "不明"
            lines.append(f"  - {name}（{category}）: {price_str}")
    else:
        lines.append("商品一覧: 読み取れませんでした")

    total = ocr.get("total")
    if total is not None:
        lines.append(f"合計金額: {total}円")
    else:
        lines.append("合計金額: 記載なし")

    return "\n".join(lines)


def _reply_text(reply_token: str, text: str) -> None:
    """LINEにテキストメッセージを返信する。

    5000文字を超える場合は末尾を切り詰める。
    テキストメッセージとLINE Reply APIの呼び出しを
    1か所に集約し、ハンドラ関数の重複を排除するためのヘルパー。

    Args:
        reply_token: LINEイベントのreply_token。
        text: 返信するテキスト。
    """
    if len(text) > 5000:
        text = text[:4990] + "\n...（省略）"

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


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


def _handle_message_common(
    event: MessageEvent,
    user_message: str,
) -> None:
    """テキスト・画像共通のメッセージ処理ロジック。

    user_messageにはテキストメッセージそのもの、または
    _format_ocr_result()で変換したOCR結果テキストが入る。
    どちらも同じchat()パイプラインで処理する。

    Args:
        event: LINEのMessageEvent。
        user_message: chat()に渡すテキスト。
    """
    line_user_id = event.source.user_id

    user_id = get_or_create_user(
        line_user_id=line_user_id,
        display_name=None,
    )

    conversation_history, is_new_session = (
        crud.get_conversation_history(user_id)
    )

    result = chat(
        user_id=user_id,
        user_message=user_message,
        conversation_history=conversation_history,
    )

    response_text = result["response"]

    if is_new_session:
        response_text = (
            "（前回の会話から時間が空いたため、"
            "新しい会話として対応しています）\n\n"
            + response_text
        )

    crud.save_conversation_messages(user_id, [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": response_text},
    ])

    _reply_text(event.reply_token, response_text)
    logger.info(f"LINE返信: user={line_user_id}")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    """テキストメッセージを受信した時の処理。"""
    line_user_id = event.source.user_id
    user_message = event.message.text
    logger.info(
        f"LINE受信(text): user={line_user_id} message={user_message}"
    )
    _handle_message_common(event, user_message)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event: MessageEvent):
    """画像メッセージを受信した時の処理。

    LINEから画像バイナリを取得し、GPT-4o Visionでレシート情報を
    抽出する。OCR結果を_OCR_PREFIXで始まるテキストに変換して
    chat()に渡すことで、通常のメッセージと同じAgentパイプラインを
    再利用する。

    OCRに失敗した場合は「読み取れませんでした」をそのまま渡し、
    Agentが自然言語でリカバリーする。
    """
    line_user_id = event.source.user_id
    message_id = event.message.id
    logger.info(f"LINE受信(image): user={line_user_id}")

    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)

    ocr = extract_receipt_info(image_bytes)
    user_message = _format_ocr_result(ocr)

    logger.info(f"OCR結果: {user_message}")
    _handle_message_common(event, user_message)


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
        f"LINE友だち追加: line_user_id={line_user_id} user_id={user_id}"
    )

    _reply_text(
        event.reply_token,
        (
            "友だち追加ありがとうございます！\n"
            "家計簿AIアシスタントです。\n\n"
            "「コンビニで300円」のように話しかけると、"
            "支出を記録できます。\n"
            "「今月の支出を教えて」で集計も確認できます。"
        ),
    )
    