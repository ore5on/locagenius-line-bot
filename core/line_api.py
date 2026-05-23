"""
LocaGenius — LINE メッセージ送受信ヘルパー
"""

import asyncio
import logging

from linebot.v3.messaging import (
    ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage,
)

from core.config import line_config

logger = logging.getLogger(__name__)


async def reply(reply_token: str, message: str) -> None:
    """reply_token を使って即時返信する"""
    def _send():
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=message)],
                )
            )
    await asyncio.to_thread(_send)


async def push(user_id: str, message: str) -> None:
    """push_message でユーザーにメッセージを送信する（reply_token 不要）

    LINEの1メッセージ上限は5000文字。超える場合は分割して送信する。
    """
    max_len = 4900
    chunks  = [message[i:i + max_len] for i in range(0, len(message), max_len)]

    def _send(chunk: str):
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=chunk)],
                )
            )

    for chunk in chunks[:3]:   # 最大3通まで送信
        await asyncio.to_thread(_send, chunk)


async def download_content(message_id: str) -> bytes | None:
    """LINE コンテンツAPIからデータをダウンロードして bytes で返す"""
    def _fetch():
        with ApiClient(line_config) as api_client:
            return MessagingApiBlob(api_client).get_message_content(message_id)
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning(f"Content download failed: {e}")
        return None
