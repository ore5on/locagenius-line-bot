"""
LocaGenius — 設定・定数・共有クライアント
環境変数の読み込み、LINEクライアント、Anthropicクライアント、
グローバル定数、ヘルプテキスト、共有状態をまとめる。

LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN は任意。
未設定時は LINE 機能を無効化し、Web API（PrimeAsset）のみで動作する。
"""

import os
import logging
from datetime import timezone, timedelta

import anthropic

# ──────────────────────────────────────
# タイムゾーン
# ──────────────────────────────────────
JST = timezone(timedelta(hours=9))

# ──────────────────────────────────────
# ログ設定
# ──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# 環境変数
# ──────────────────────────────────────

# LINE 関連（任意 — 未設定時は LINE 機能が無効になる）
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_ENABLED              = bool(LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN)

# Anthropic（必須）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    logger.warning("ANTHROPIC_API_KEY が未設定です。分析機能は動作しません。")

# 国土交通省 不動産情報ライブラリ（必須）
LIBRARY_API_KEY = os.environ.get("LIBRARY_API_KEY", "")
if not LIBRARY_API_KEY:
    logger.warning("LIBRARY_API_KEY が未設定です。MCP ツールが動作しません。")

# MLIT MCPサーバーのパス
MCP_SERVER_PATH = os.environ.get(
    "MCP_SERVER_PATH",
    "mcp/mlit-geospatial-mcp/src/server.py",
)

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# ──────────────────────────────────────
# LINE SDK クライアント（LINE_ENABLED の場合のみ初期化）
# ──────────────────────────────────────
line_parser = None
line_config = None

if LINE_ENABLED:
    from linebot.v3.webhook import WebhookParser
    from linebot.v3.messaging import Configuration

    line_parser = WebhookParser(LINE_CHANNEL_SECRET)
    line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    logger.info("LINE Bot: 有効")
else:
    logger.info("LINE Bot: 無効（LINE_CHANNEL_SECRET / ACCESS_TOKEN 未設定）")

# ──────────────────────────────────────
# Anthropic クライアント
# ──────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY or "dummy")

# ──────────────────────────────────────
# システムプロンプト
# ──────────────────────────────────────
try:
    with open("system_prompt.txt", encoding="utf-8") as _f:
        SYSTEM_PROMPT = _f.read()
except FileNotFoundError:
    SYSTEM_PROMPT = ""
    logger.warning("system_prompt.txt が見つかりません")

# ──────────────────────────────────────
# ヘルプメッセージ
# ──────────────────────────────────────
HELP_TEXT = """🏠 LocaGeniusの使い方

━━━━━━━━━━━━━━━━━━
【モード①】住所 → 立地調査
住所や地名を送るだけ！

例：東京都渋谷区恵比寿1-1-1
例：横浜駅

調査内容：
💰 地価・売買相場・推定賃料
⚠️ ハザード（洪水・津波・液状化等）
🚉 最寄駅・学校・病院
🏛️ 用途地域・建蔽率・容積率
👥 将来人口推計・DID

━━━━━━━━━━━━━━━━━━
【モード②】マンション名 → 資産性評価
建物名を送ると将来価値まで評価！

例：パークコート渋谷ザ・タワー

━━━━━━━━━━━━━━━━━━
【モード③】物件情報 → 投資分析
マイソク画像/PDF または物件URLを送信

⏱ 結果まで30秒〜1分程度かかります。"""

# ──────────────────────────────────────
# キーワードセット
# ──────────────────────────────────────
GREETING_KEYWORDS = {"こんにちは", "はじめまして", "hello", "hi", "よろしく", "テスト", "test"}
HELP_KEYWORDS     = {"/help", "help", "ヘルプ", "使い方", "操作方法"}

# ──────────────────────────────────────
# Mode 3: 区分/一棟 確認待ちユーザー状態（LINE 専用）
# ──────────────────────────────────────
pending_type_confirm: dict[str, dict] = {}
