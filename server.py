"""Jarvis — assistente de voz local usando o Claude Code (sua assinatura do Claude, sem API).

Fluxo: navegador capta a voz -> Whisper transcreve -> `claude -p` decide/usa ferramentas
-> resposta falada com edge-tts -> o círculo anima com o áudio.
"""

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import edge_tts
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel

import tools

ROOT = Path(__file__).parent


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

MODEL = os.environ.get("JARVIS_MODEL", "")  # vazio = modelo padrão do seu Claude Code
VOICE = os.environ.get("JARVIS_VOICE", "pt-BR-AntonioNeural")
WHISPER_SIZE = os.environ.get("JARVIS_WHISPER", "small")
WAKE_WORD = os.environ.get("JARVIS_WAKE_WORD", "0") == "1"
USER_NAME = os.environ.get("JARVIS_USER_NAME", "senhor")
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
TOOL_CMD = f"python3 {ROOT / 'tools_cli.py'}"


def _tool_help() -> str:
    lines = []
    for t in tools.TOOLS:
        params = ", ".join(t["input_schema"].get("properties", {}))
        lines.append(f"- {t['name']}({params}): {t['description']}")
    return "\n".join(lines)


SYSTEM = f"""Você é o J.A.R.V.I.S., o assistente pessoal de voz do usuário, rodando no computador dele \
(Linux CachyOS, desktop GNOME, shell fish). Chame-o de "{USER_NAME}".

Tudo o que você escreve é convertido em fala, então:
- Responda em português do Brasil, curto e natural, como numa conversa falada (1 a 3 frases, salvo se pedirem detalhes).
- Nunca use markdown, listas, emojis, código ou URLs na resposta — só texto que soe bem falado.
- Tom: educado, confiante, levemente espirituoso, como o Jarvis dos filmes.

Você controla o PC pela ferramenta Bash. Para as ações comuns, use estes atalhos prontos:
  {TOOL_CMD} <ferramenta> <argumento>        (ex: {TOOL_CMD} open_application calculadora)
  {TOOL_CMD} <ferramenta> '<json>'           (ex: {TOOL_CMD} set_volume '{{"change": -10}}')
Ferramentas disponíveis:
{_tool_help()}
Para o resto, use comandos de shell normais. Quando o usuário pedir uma ação, execute e confirme brevemente \
("Pronto, calculadora aberta."). Use WebSearch para clima, notícias, cotações e fatos atuais.
Antes de qualquer ação destrutiva ou irreversível (apagar arquivos, desligar, instalar coisas), pergunte e espere \
confirmação. A transcrição da voz pode ter erros — interprete com bom senso."""

# Comandos que o Jarvis nunca roda sozinho
DISALLOWED = ["Bash(sudo:*)", "Bash(rm -rf:*)", "Bash(mkfs:*)", "Bash(dd:*)",
              "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(poweroff:*)"]

print(f"[jarvis] carregando Whisper '{WHISPER_SIZE}'... (na primeira vez ele baixa o modelo)")
whisper = WhisperModel(WHISPER_SIZE, device="auto", compute_type="int8")
print("[jarvis] Whisper pronto.")

app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


# ---------------------------------------------------------------- voz

def transcribe(pcm: np.ndarray) -> str:
    segments, _ = whisper.transcribe(pcm, language="pt", beam_size=1, vad_filter=True,
                                     initial_prompt="Jarvis, abre a calculadora.")
    return " ".join(s.text for s in segments).strip()


VOICE_RATE = os.environ.get("JARVIS_RATE", "+0%")
VOICE_PITCH = os.environ.get("JARVIS_PITCH", "-8Hz")
VOICE_FX = os.environ.get("JARVIS_FX", "1") == "1"
# efeito "IA dentro da armadura": graves e presença, leve metálico e eco curto
FX_CHAIN = ("highpass=f=90,equalizer=f=250:t=q:w=1:g=2,equalizer=f=3500:t=q:w=1:g=3,"
            "flanger=delay=0.6:depth=0.6:regen=0:width=30:speed=0.25,"
            "aecho=0.85:0.5:22|37:0.22|0.12,loudnorm")


async def synthesize(text: str) -> bytes:
    comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH)
    audio = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    if not VOICE_FX or not shutil.which("ffmpeg"):
        return bytes(audio)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-af", FX_CHAIN, "-f", "mp3", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate(bytes(audio))
    return out or bytes(audio)


_WAKE = re.compile(r"\b(jarvis|jarvi|jarves|jervis|djarvis|jarbas|jarviz)\b[,.!]?\s*", re.I)


# ---------------------------------------------------------------- cérebro (Claude Code)

class Brain:
    def __init__(self, emit):
        self.session_id: str | None = None
        self.emit = emit  # função thread-safe para mandar eventos para a UI

    def reset(self):
        self.session_id = None

    def ask(self, text: str) -> str:
        now = datetime.now().strftime("%A, %d/%m/%Y %H:%M")
        cmd = [CLAUDE, "-p", f"[{now}] {text}",
               "--output-format", "stream-json", "--verbose",
               "--system-prompt", SYSTEM,
               "--tools", "Bash,WebSearch,WebFetch",
               "--allowedTools", "Bash,WebSearch,WebFetch",
               "--disallowedTools", *DISALLOWED,
               "--strict-mcp-config"]
        if MODEL:
            cmd += ["--model", MODEL]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        # remove variáveis que fariam o claude usar API em vez da sua assinatura
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(Path.home()), env=env)
        result, texts = None, []
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        inp = block.get("input", {})
                        label = inp.get("command") or inp.get("query") or inp.get("url") or json.dumps(inp, ensure_ascii=False)
                        label = label.replace(TOOL_CMD + " ", "")
                        self.emit({"type": "action", "text": label[:160]})
                        print(f"[tool] {block.get('name')}: {label}")
                    elif block.get("type") == "text":
                        texts.append(block["text"])
            elif ev.get("type") == "result":
                result = ev
        proc.wait()

        if result is None:
            err = proc.stderr.read().strip()
            print("[claude] erro:", err)
            if "login" in err.lower() or "auth" in err.lower():
                return "Não estou logado no Claude Code. Rode claude no terminal e faça login."
            return "Tive um problema ao falar com o Claude."
        self.session_id = result.get("session_id") or self.session_id
        if result.get("is_error"):
            print("[claude] erro:", result)
            msg = str(result.get("result", ""))
            if "limit" in msg.lower():
                return "Atingimos o limite de uso da sua assinatura por agora. Tente mais tarde."
            return "Tive um problema ao falar com o Claude."
        return (result.get("result") or (texts[-1] if texts else "")).strip()


# ---------------------------------------------------------------- websocket com a interface

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    lock = asyncio.Lock()
    awake_until = 0.0

    async def send(msg):
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    def emit(msg):
        asyncio.run_coroutine_threadsafe(send(msg), loop)

    brain = Brain(emit)

    async def speak(text: str):
        try:
            audio = await synthesize(text)
            await send({"type": "speak", "text": text, "audio": base64.b64encode(audio).decode()})
        except Exception as e:
            print("[tts] erro:", e)
            await send({"type": "speak", "text": text, "audio": None})

    async def handle_text(text: str, from_voice: bool):
        nonlocal awake_until
        if from_voice and WAKE_WORD:
            now = loop.time()
            if _WAKE.search(text):
                text = _WAKE.sub("", text, count=1).strip()
                awake_until = now + 15
                if not text:
                    await speak(f"Pois não, {USER_NAME}?")
                    return
            elif now > awake_until:
                await send({"type": "state", "state": "idle"})
                return
        await send({"type": "user", "text": text})
        await send({"type": "state", "state": "thinking"})
        try:
            reply = await asyncio.to_thread(brain.ask, text)
        except Exception as e:  # noqa: BLE001
            print("[jarvis] erro inesperado:", repr(e))
            reply = "Algo deu errado aqui dentro. Veja o terminal para detalhes."
        awake_until = loop.time() + 15
        await speak(reply or "Pronto.")

    try:
        await send({"type": "config", "wake_word": WAKE_WORD})
        while True:
            msg = await ws.receive()
            if msg.get("bytes") is not None:  # áudio PCM float32 16 kHz
                if lock.locked():
                    continue
                async with lock:
                    await send({"type": "state", "state": "transcribing"})
                    pcm = np.frombuffer(msg["bytes"], dtype=np.float32)
                    text = await asyncio.to_thread(transcribe, pcm)
                    print(f"[ouvi] {text!r}")
                    if len(text) < 2:
                        await send({"type": "state", "state": "idle"})
                        continue
                    await handle_text(text, from_voice=True)
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data.get("type") == "text" and data.get("text", "").strip():
                    async with lock:
                        await handle_text(data["text"].strip(), from_voice=False)
                elif data.get("type") == "reset":
                    brain.reset()
            elif msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("JARVIS_PORT", "8765")), log_level="warning")
