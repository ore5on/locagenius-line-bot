"""
LocaGenius — 設定・定数・共有クライアント
環境変数の読み込み、LINEクライアント、Anthropicクライアント、
グローバル定数、ヘルプテキスト、共有状態をまとめる。
"""

import os
import logging
from datetime import timezone, timedelta

from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import Configuration
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
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY         = os.environ["ANTHROPIC_API_KEY"]
LIBRARY_API_KEY           = os.environ["LIBRARY_API_KEY"]
GOOGLE_MAPS_API_KEY       = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# MLIT MCPサーバーのパス（Dockerfileでcloneする場所に合わせる）
MCP_SERVER_PATH = os.environ.get(
    "MCP_SERVER_PATH",
    "mcp/mlit-geospatial-mcp/src/server.py",
)

# ──────────────────────────────────────
# LINE SDK / Anthropic クライアント
# ──────────────────────────────────────
line_parser      = WebhookParser(LINE_CHANNEL_SECRET)
line_config      = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ──────────────────────────────────────
# システムプロンプト
# ──────────────────────────────────────
with open("system_prompt.txt", encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read()

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
例：ブリリアタワー東京

評価内容：
📊 周辺相場・推定利回り
📈 将来の資産価値トレンド
⚠️ ハザード・法規制
🏦 投資適格評価

━━━━━━━━━━━━━━━━━━
【モード③】物件情報 → 投資分析
以下のいずれかを送ると割安・割高を判定！

📎 マイソク画像 / PDF を送信
🔗 物件サイトのURL を貼り付け
（SUUMO・ノムコム・アットホーム等）

■ 区分マンションの場合
周辺成約事例との価格比較
推定利回り・実質収益試算
投資判断（◎/○/△/×）

■ 一棟物件の場合
積算価格・収益還元価格の2軸評価
割安・割高の定量判定
投資判断（◎/○/△/×）

━━━━━━━━━━━━━━━━━━
⏱ 結果まで30秒〜1分程度かかります。"""

# ──────────────────────────────────────
# キーワードセット
# ──────────────────────────────────────
GREETING_KEYWORDS = {"こんにちは", "はじめまして", "hello", "hi", "よろしく", "テスト", "test"}
HELP_KEYWORDS     = {"/help", "help", "ヘルプ", "使い方", "操作方法"}

# ──────────────────────────────────────
# Mode 3: 区分/一棟 確認待ちユーザー状態
# { user_id: extracted_dict }
# ──────────────────────────────────────
pending_type_confirm: dict[str, dict] = {}
