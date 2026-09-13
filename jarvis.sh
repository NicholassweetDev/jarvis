#!/usr/bin/env bash
# Inicia o Jarvis: instala dependências na primeira vez, sobe o servidor e abre a janela.
set -e
cd "$(dirname "$(readlink -f "$0")")"
PORT="${JARVIS_PORT:-8765}"

if [ ! -d .venv ]; then
  echo "[jarvis] criando ambiente Python (só na primeira vez)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] || cp .env.example .env

if ! command -v claude >/dev/null && [ ! -x "$HOME/.local/bin/claude" ]; then
  echo "[jarvis] o Claude Code não está instalado. Instale em https://claude.com/claude-code e faça login."
  exit 1
fi

# Se o Jarvis já estiver rodando, só abre a janela
if curl -s "http://127.0.0.1:$PORT" >/dev/null; then
  exec chromium --app="http://127.0.0.1:$PORT" --user-data-dir="$HOME/.local/share/jarvis-chromium" \
    --use-fake-ui-for-media-stream --autoplay-policy=no-user-gesture-required 2>/dev/null
fi

.venv/bin/python -u server.py &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT

until curl -s "http://127.0.0.1:$PORT" >/dev/null; do
  kill -0 $SERVER 2>/dev/null || { echo "[jarvis] o servidor falhou ao iniciar"; exit 1; }
  sleep 0.5
done

# Janela própria, sem barra de navegador; o microfone é liberado automaticamente
chromium --app="http://127.0.0.1:$PORT" \
  --user-data-dir="$HOME/.local/share/jarvis-chromium" \
  --use-fake-ui-for-media-stream \
  --autoplay-policy=no-user-gesture-required \
  --window-size=900,900 2>/dev/null
