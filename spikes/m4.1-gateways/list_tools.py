"""tools/list per agent through the gateway (matrix item 1/4: what each tenant sees). Usage: list_tools.py <dir>"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "m4-contextforge"))
import spike

ST = json.loads((Path(sys.argv[1]) / "secrets/stand.json").read_text())
out = {}
for who, own in ST["tokens"].items():
    url = ST["paths"]["t-alpha"][0]["url"] if who == "admin" else ST["paths"][who.split(":")[1]][0]["url"]
    token = ST.get("token_for_url", {}).get(url, own)
    if token is None:
        continue
    hh, err = spike.mcp_session(url, token)
    if err:
        out[who] = {"error": err}
        continue
    s, _, t = spike.http("POST", url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token, hh)
    r = spike.j(t)
    out[who] = {"status": s, "tools": sorted(x["name"] for x in r.get("result", {}).get("tools", [])) or r}
print(json.dumps(out, indent=1, sort_keys=True))
