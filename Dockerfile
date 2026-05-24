# ──────────────────────────────────────
# LocaGenius Dockerfile
# ──────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# git をインストール（MLIT MCPサーバーのcloneに必要）
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# ── アプリの依存ライブラリをインストール ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── MLIT Geospatial MCP Server をclone ──
# 国土交通省が公開しているMCPサーバー
RUN git clone https://github.com/chirikuuka/mlit-geospatial-mcp.git \
    mcp/mlit-geospatial-mcp

# MCPサーバー自体の依存ライブラリをインストール
RUN pip install --no-cache-dir \
    -r mcp/mlit-geospatial-mcp/requirements.txt

# ── アプリコードをコピー ──
COPY main.py .
COPY system_prompt.txt .
COPY yield_table.yaml .
COPY api/ ./api/
COPY core/ ./core/
COPY modes/ ./modes/

# Python が /app 配下の core/ modes/ を見つけられるようにする
ENV PYTHONPATH=/app

# Renderはデフォルトで PORT 環境変数を設定する
EXPOSE 10000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
