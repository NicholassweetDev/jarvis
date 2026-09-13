"""Ferramentas que o Jarvis pode usar para controlar o PC (Linux / GNOME)."""

import configparser
import os
import re
import shlex
import shutil
import subprocess
import unicodedata
import webbrowser
from datetime import datetime
from pathlib import Path


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


# ---------------------------------------------------------------- aplicativos

def _app_dirs() -> list[Path]:
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs = [Path.home() / ".local/share/applications"]
    dirs += [Path(d) / "applications" for d in data_dirs]
    dirs += [
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    return [d for d in dirs if d.is_dir()]


def _load_apps() -> dict[str, dict]:
    apps: dict[str, dict] = {}
    for d in _app_dirs():
        for f in d.glob("*.desktop"):
            app_id = f.stem
            if app_id in apps:
                continue
            cp = configparser.RawConfigParser(strict=False, interpolation=None)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false").lower() == "true" or e.get("Type") != "Application":
                continue
            names = {e.get("Name", ""), e.get("Name[pt_BR]", ""), e.get("Name[pt]", ""),
                     e.get("GenericName", ""), e.get("GenericName[pt_BR]", ""), app_id}
            keywords = e.get("Keywords[pt_BR]", "") + ";" + e.get("Keywords", "")
            exec_line = e.get("Exec", "")
            binary = ""
            try:
                parts = [p for p in shlex.split(exec_line) if not p.startswith("%")]
                if parts and parts[0] == "env":
                    parts = [p for p in parts[1:] if "=" not in p]
                if parts:
                    binary = os.path.basename(parts[0])
            except ValueError:
                pass
            apps[app_id] = {
                "id": app_id,
                "name": e.get("Name[pt_BR]") or e.get("Name", app_id),
                "names": [_norm(n) for n in names if n],
                "keywords": [_norm(k) for k in keywords.split(";") if k.strip()],
                "binary": binary,
            }
    return apps


_APPS: dict[str, dict] | None = None


def _apps() -> dict[str, dict]:
    global _APPS
    if _APPS is None:
        _APPS = _load_apps()
    return _APPS


def _find_app(query: str) -> dict | None:
    q = _norm(query)
    best, best_score = None, 0
    for app in _apps().values():
        score = 0
        for n in app["names"]:
            if n == q:
                score = max(score, 100)
            elif n.startswith(q) or q in n.split():
                score = max(score, 70)
            elif q in n:
                score = max(score, 50)
        if any(q == k for k in app["keywords"]):
            score = max(score, 40)
        elif any(q in k for k in app["keywords"]):
            score = max(score, 20)
        if score > best_score:
            best, best_score = app, score
    return best


def list_applications(filter: str = "") -> str:
    names = sorted({a["name"] for a in _apps().values()
                    if not filter or _norm(filter) in " ".join(a["names"] + a["keywords"])})
    return ", ".join(names) if names else "Nenhum aplicativo encontrado."


def open_application(name: str) -> str:
    app = _find_app(name)
    if not app:
        return f"Não encontrei nenhum aplicativo parecido com '{name}'. Use list_applications para ver os disponíveis."
    subprocess.Popen(["gtk-launch", app["id"]], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return f"Abrindo {app['name']}."


def close_application(name: str) -> str:
    app = _find_app(name)
    label = app["name"] if app else name
    if app and app["binary"] == "flatpak":
        r = subprocess.run(["flatpak", "kill", app["id"]], capture_output=True)
        return f"{label} fechado." if r.returncode == 0 else f"{label} não parecia estar aberto."
    target = app["binary"] if app and app["binary"] else name
    # casa só o executável (não qualquer processo que tenha o nome no meio dos argumentos)
    r = subprocess.run(["pkill", "-f", f"(^|/){re.escape(target)}( |$)"], capture_output=True)
    return f"{label} fechado." if r.returncode == 0 else f"{label} não parecia estar aberto."


# ---------------------------------------------------------------- web / arquivos

def open_url(url: str) -> str:
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    webbrowser.open(url)
    return f"Abrindo {url}."


def search_web_in_browser(query: str) -> str:
    from urllib.parse import quote_plus
    webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))
    return f"Pesquisa por '{query}' aberta no navegador."


def open_folder(path: str) -> str:
    aliases = {
        "downloads": "Downloads", "documentos": "Documentos", "imagens": "Imagens",
        "musicas": "Músicas", "videos": "Vídeos", "area de trabalho": "Área de trabalho",
        "desktop": "Área de trabalho", "home": "", "pasta pessoal": "", "projetos": "Projetos",
    }
    key = _norm(path)
    p = Path.home() / aliases[key] if key in aliases else Path(os.path.expanduser(path))
    if not p.exists():
        return f"A pasta {p} não existe."
    subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return f"Abrindo {p}."


# ---------------------------------------------------------------- sistema

def set_volume(percent: int | None = None, change: int | None = None, mute: bool | None = None) -> str:
    sink = "@DEFAULT_AUDIO_SINK@"
    if mute is not None:
        subprocess.run(["wpctl", "set-mute", sink, "1" if mute else "0"])
        return "Som mutado." if mute else "Som reativado."
    if percent is not None:
        percent = max(0, min(150, int(percent)))
        subprocess.run(["wpctl", "set-volume", sink, f"{percent}%"])
        return f"Volume em {percent}%."
    if change is not None:
        sign = "+" if change >= 0 else "-"
        subprocess.run(["wpctl", "set-volume", "-l", "1.5", sink, f"{abs(int(change))}%{sign}"])
    out = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True).stdout
    return f"Volume atual: {out.strip()}"


def media_control(action: str) -> str:
    """play_pause, next, previous, stop via MPRIS (D-Bus)."""
    methods = {"play_pause": "PlayPause", "next": "Next", "previous": "Previous", "stop": "Stop"}
    if action not in methods:
        return "Ação inválida."
    names = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
         "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames"],
        capture_output=True, text=True).stdout
    players = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", names)
    if not players:
        return "Nenhum player de mídia aberto."
    for p in players:
        subprocess.run(["gdbus", "call", "--session", "--dest", p, "--object-path",
                        "/org/mpris/MediaPlayer2", "--method",
                        f"org.mpris.MediaPlayer2.Player.{methods[action]}"], capture_output=True)
    return "Feito."


def system_info() -> str:
    def sh(cmd):
        return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
    return (f"Data/hora: {datetime.now():%d/%m/%Y %H:%M}\n"
            f"Uptime: {sh('uptime -p')}\n"
            f"Memória: {sh('free -h | sed -n 2p')}\n"
            f"Disco /: {sh('df -h / | tail -1')}\n"
            f"Bateria: {sh('cat /sys/class/power_supply/BAT*/capacity 2>/dev/null') or 'sem bateria'}")


def lock_screen() -> str:
    subprocess.run(["loginctl", "lock-session"])
    return "Tela bloqueada."


_BLOCKED = [r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)\s*$", r"\bmkfs", r"\bdd\s+if=", r":\(\)\s*\{",
            r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r">\s*/dev/sd", r"\bchmod\s+-R\s+777\s+/"]


def run_command(command: str) -> str:
    for pat in _BLOCKED:
        if re.search(pat, command):
            return "Comando bloqueado por segurança. Peça para o usuário executar manualmente."
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30,
                           cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        return "O comando demorou mais de 30s e foi interrompido."
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    return f"(código {r.returncode})\n{out[:4000]}" if out else f"(código {r.returncode}, sem saída)"


# ---------------------------------------------------------------- definições para o Claude

TOOLS = [
    {"name": "open_application",
     "description": "Abre um aplicativo instalado no PC pelo nome (ex: 'calculadora', 'firefox', 'spotify', 'terminal', 'configurações').",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "close_application", "description": "Fecha um aplicativo aberto pelo nome.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "list_applications",
     "description": "Lista os aplicativos instalados (opcionalmente filtrando por um termo). Use quando não achar um app.",
     "input_schema": {"type": "object", "properties": {"filter": {"type": "string"}}}},
    {"name": "open_url", "description": "Abre um site no navegador padrão (ex: youtube.com).",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "search_web_in_browser",
     "description": "Abre uma pesquisa do Google no navegador, para quando o usuário quer VER os resultados.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "open_folder",
     "description": "Abre uma pasta no gerenciador de arquivos (ex: 'downloads', 'documentos', ou um caminho).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "set_volume",
     "description": "Controla o volume do sistema. Use percent para valor absoluto, change para aumentar/diminuir (ex: 10 ou -10), mute para mutar/desmutar. Sem argumentos retorna o volume atual.",
     "input_schema": {"type": "object", "properties": {
         "percent": {"type": "integer"}, "change": {"type": "integer"}, "mute": {"type": "boolean"}}}},
    {"name": "media_control", "description": "Controla a música/vídeo tocando (Spotify, YouTube no navegador etc).",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}, "required": ["action"]}},
    {"name": "system_info", "description": "Informações do PC: hora, memória, disco, bateria, uptime.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "lock_screen", "description": "Bloqueia a tela do computador.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "run_command",
     "description": "Executa um comando de shell (fish/bash) no Linux do usuário e retorna a saída. Use para tarefas que as outras ferramentas não cobrem. Nunca rode comandos destrutivos sem o usuário confirmar antes.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]

FUNCTIONS = {
    "open_application": open_application, "close_application": close_application,
    "list_applications": list_applications, "open_url": open_url,
    "search_web_in_browser": search_web_in_browser, "open_folder": open_folder,
    "set_volume": set_volume, "media_control": media_control, "system_info": system_info,
    "lock_screen": lock_screen, "run_command": run_command,
}


def call_tool(name: str, args: dict) -> tuple[str, bool]:
    fn = FUNCTIONS.get(name)
    if not fn:
        return f"Ferramenta desconhecida: {name}", True
    try:
        return fn(**args), False
    except Exception as e:  # noqa: BLE001
        return f"Erro: {e}", True
