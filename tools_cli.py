#!/usr/bin/env python3
"""Linha de comando para as ferramentas do Jarvis (usada pelo Claude Code).

Exemplos:
  tools_cli.py open_application calculadora
  tools_cli.py set_volume '{"change": 10}'
  tools_cli.py system_info
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tools  # noqa: E402

if len(sys.argv) < 2 or sys.argv[1] not in tools.FUNCTIONS:
    print("Uso: tools_cli.py <ferramenta> [argumento | json]. Ferramentas: " + ", ".join(tools.FUNCTIONS))
    sys.exit(1)

name, rest = sys.argv[1], sys.argv[2:]
schema = next(t for t in tools.TOOLS if t["name"] == name)["input_schema"]
if rest and rest[0].lstrip().startswith("{"):
    args = json.loads(rest[0])
elif rest:
    # argumento simples vai para o primeiro parâmetro (ex: o nome do app)
    args = {next(iter(schema["properties"])): " ".join(rest)}
else:
    args = {}

out, err = tools.call_tool(name, args)
print(out)
sys.exit(1 if err else 0)
