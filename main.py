"""
LocaGenius (不動産立地調査ロボット)
LINE Bot + FastAPI + Claude API + MLIT Geospatial MCP Server

このファイルはルーティング専用。
ロジックは core/ および modes/ モジュールに委譲する。
"""

import os
import re
import logging

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
    FileMessageContent,
)
from linebot.v3.exceptions import InvalidSignatureError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.config import (
    line_parser,
    LIBRARY_API_KEY,
    MCP_SERVER_PATH,
    HELP_TEXT,
    GREETING_KEYWORDS,
    HELP_KEYWORDS,
    pending_type_confirm,
)
from core.geocoding  import geocode, extract_location_query, is_property_info_only, looks_like_location
from core.overpass   import get_nearest_station, get_nearest_school, get_nearest_medical
from core.line_api   import reply, push

from modes.location     import investigate_and_push
from modes.assessment   import assess_and_push
from modes.investment   import process_maisoku_image, process_maisoku_pdf, run_investment
from modes.url_property import process_property_url

_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# FastAPI アプリ
# ──────────────────────────────────────
app = FastAPI(title="立地調査ロボット")


@app.get("/")
async def health_check():
    """Render のヘルスチェック用エンドポイント"""
    return {"status": "ok", "service": "立地調査ロボット"}


@app.get("/debug")
async def debug_info():
    """デバッグ用エンドポイント。MCPツール一覧・ジオコーディング・Overpass API の動作確認"""
    result: dict = {}

    # 1. ジオコーディング
    test_address = "東京都千代田区丸の内1-1"
    try:
        coords = await geocode(test_address)
        result["geocoding"] = {"status": "ok", "address": test_address, "coords": coords}
    except Exception as e:
        coords = None
        result["geocoding"] = {"status": "error", "error": str(e)}

    # 2. Overpass API（最寄駅・小学校・医療）
    if coords:
        lat, lon = coords
        for label, coro in [
            ("nearest_station", get_nearest_station(lat, lon)),
            ("nearest_school",  get_nearest_school(lat, lon)),
            ("nearest_medical", get_nearest_medical(lat, lon)),
        ]:
            try:
                val = await coro
                result[label] = {"status": "ok", "value": val}
            except Exception as e:
                result[label] = {"status": "error", "error": str(e)}
    else:
        result["overpass"] = "skipped (geocoding failed)"

    # 3. MCP サーバー起動 & ツール一覧
    try:
        server_params = StdioServerParameters(
            command="python",
            args=[MCP_SERVER_PATH],
            env={
                **os.environ.copy(),
                "LIBRARY_API_KEY": LIBRARY_API_KEY,
                "PYTHONUNBUFFERED": "1",
                "LOG_LEVEL": "WARNING",
            },
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                tool_info = []
                for t in tools_response.tools:
                    props    = list((t.inputSchema or {}).get("properties", {}).keys())
                    required = (t.inputSchema or {}).get("required", [])
                    tool_info.append({
                        "name":     t.name,
                        "params":   props,
                        "required": required,
                        "desc":     (t.description or "")[:120],
                    })
                result["mcp_tools"]      = tool_info
                result["mcp_tool_count"] = len(tool_info)

                # 4. 代表ツールを試し呼び出し
                if coords:
                    lat, lon = coords
                    test_calls = []
                    for t in tools_response.tools:
                        props  = (t.inputSchema or {}).get("properties", {})
                        params: dict = {}
                        for k in props:
                            kl = k.lower()
                            if any(x in kl for x in ["lat", "latitude", "緯度"]):
                                params[k] = lat
                            elif any(x in kl for x in ["lon", "lng", "longitude", "経度"]):
                                params[k] = lon
                        if not params:
                            continue
                        try:
                            call_result = await session.call_tool(t.name, params)
                            parts = [
                                item.text if hasattr(item, "text") else str(item)
                                for item in (call_result.content or [])
                            ]
                            raw = "\n".join(parts)
                            test_calls.append({
                                "tool":           t.name,
                                "params":         params,
                                "is_error":       getattr(call_result, "isError", False),
                                "result_preview": raw[:600],
                            })
                        except Exception as e:
                            test_calls.append({"tool": t.name, "params": params, "error": str(e)})
                    result["test_tool_calls"] = test_calls

    except Exception as e:
        result["mcp_tools"] = {"status": "error", "error": str(e)}

    return result


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """LINE からの Webhook を受け取るエンドポイント"""

    # 1. 署名を検証
    signature = request.headers.get("X-Line-Signature", "")
    body      = (await request.body()).decode("utf-8")

    try:
        events = line_parser.parse(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid LINE signature received")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # 2. 各イベントを処理
    for event in events:
        if not isinstance(event, MessageEvent):
            continue

        user_id     = event.source.user_id
        reply_token = event.reply_token

        # ── 画像メッセージ（マイソク画像）──────────────────────
        if isinstance(event.message, ImageMessageContent):
            await reply(
                reply_token,
                "📄 マイソク（画像）を受け取りました！\n物件情報を読み取っています...\n少々お待ちください 🔍",
            )
            background_tasks.add_task(process_maisoku_image, user_id, event.message.id)
            continue

        # ── ファイルメッセージ（マイソク PDF）──────────────────
        if isinstance(event.message, FileMessageContent):
            file_name = event.message.file_name or ""
            if not file_name.lower().endswith(".pdf"):
                await reply(reply_token, "⚠️ PDFファイルのみ対応しています。\nマイソクのPDFを送ってください。")
                continue
            await reply(
                reply_token,
                "📄 マイソク（PDF）を受け取りました！\n物件情報を読み取っています...\n少々お待ちください 🔍",
            )
            background_tasks.add_task(process_maisoku_pdf, user_id, event.message.id)
            continue

        if not isinstance(event.message, TextMessageContent):
            continue

        user_text = event.message.text.strip()
        logger.info(f"Message from {user_id}: {user_text[:50]}")

        # ── 区分/一棟 確認待ちの回答を処理 ──────────────────────
        if user_id in pending_type_confirm:
            lower = user_text.lower()
            if "区分" in lower:
                prop_type = "区分"
            elif "一棟" in lower or "いっとう" in lower:
                prop_type = "一棟"
            else:
                await reply(reply_token, "「区分」または「一棟」と入力してください。\n\n例：区分")
                continue
            extracted = pending_type_confirm.pop(user_id)
            extracted["property_category"] = prop_type
            await reply(
                reply_token,
                f"✅ {prop_type}物件として\n投資分析を開始します...\n\n30秒〜1分程度お待ちください 🏃",
            )
            background_tasks.add_task(run_investment, user_id, extracted)
            continue

        # ── ヘルプ・あいさつ ─────────────────────────────────
        if user_text.lower() in HELP_KEYWORDS:
            await reply(reply_token, HELP_TEXT)
            continue

        if user_text.lower() in GREETING_KEYWORDS:
            await reply(reply_token, f"こんにちは！🏠\n\n調査したい住所や地名を送ってください。\n\n{HELP_TEXT}")
            continue

        # ── URL → 物件ページ分析 ─────────────────────────────
        url_match = _URL_RE.search(user_text)
        if url_match:
            url = url_match.group(0).rstrip("）」』])")   # 末尾の括弧・記号を除去
            await reply(
                reply_token,
                "🔗 URLを受け取りました\n物件情報を読み取っています...\n\n30秒〜1分程度お待ちください 🏃",
            )
            background_tasks.add_task(process_property_url, user_id, url)
            continue

        # ── 物件情報のみで住所なし ───────────────────────────
        if is_property_info_only(user_text):
            await reply(
                reply_token,
                "📍 住所も一緒に入力してください。\n\n"
                "【積算価格を試算する場合の入力例】\n"
                "─────────────\n"
                "東京都台東区浅草2-1-1\n"
                "土地面積：50㎡\n"
                "建物面積：80㎡\n"
                "構造：木造\n"
                "築年数：15年\n"
                "路線価：520,000円/㎡（任意）\n"
                "─────────────\n"
                "住所のみの入力でも立地調査は行えます。",
            )
            continue

        # ── 地名らしくない入力を弾く ─────────────────────────
        if not looks_like_location(user_text):
            await reply(
                reply_token,
                "📍 調査したい住所や地名を入力してください。\n\n"
                "例：東京都渋谷区恵比寿1-1-1\n"
                "例：横浜駅から徒歩5分圏内\n\n"
                "詳しい使い方は「ヘルプ」と送ってください。",
            )
            continue

        # ── モード判定 → バックグラウンドタスク起動 ────────────
        _, _, is_building = extract_location_query(user_text)
        if is_building:
            await reply(
                reply_token,
                f"🏢 資産性評価を開始します\n「{user_text}」\n\n30秒〜1分程度お待ちください 🏃",
            )
            background_tasks.add_task(assess_and_push, user_id, user_text)
        else:
            await reply(
                reply_token,
                f"🔍 立地調査を開始します\n「{user_text}」\n\n30秒〜1分程度お待ちください 🏃",
            )
            background_tasks.add_task(investigate_and_push, user_id, user_text)

    return "OK"
