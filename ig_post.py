#!/usr/bin/env python3
"""
Postet den naechsten faelligen, freigegebenen Beitrag aus queue.json
auf Instagram @fabrikstadt_gz - ueber Composios MCP-Schnittstelle.

  python3 ig_post.py --check     Verbindung und Konto pruefen
  python3 ig_post.py --dry-run   zeigen, was gepostet wuerde
  python3 ig_post.py --once      den naechsten faelligen Beitrag posten
  python3 ig_post.py --force     sofort posten, Termin ignorieren
  python3 ig_post.py             dasselbe wie --once (Aufruf durch launchd)
"""
import json, os, sys, time, datetime, pathlib, urllib.request, urllib.error

BASE = pathlib.Path(__file__).parent
QUEUE = BASE / "queue.json"
LOG = BASE / "post_log.txt"
MCP_URL = "https://connect.composio.dev/mcp"

T_INFO = "INSTAGRAM_GET_USER_INFO"
T_CONTAINER = "INSTAGRAM_POST_IG_USER_MEDIA"
T_PUBLISH = "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH"
T_PIN = "PINTEREST_CREATE_PIN"


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


KEYS = ("COMPOSIO_API_KEY", "IG_USER_ID", "PUBLIC_IMAGE_BASE_URL",
        "PINTEREST_BOARD_ID", "PIN_LINK")


def load_env(required=("COMPOSIO_API_KEY", "IG_USER_ID", "PUBLIC_IMAGE_BASE_URL")):
    """Liest .env - und faellt auf Umgebungsvariablen zurueck (GitHub Actions)."""
    env = {}
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in KEYS:                       # Secrets aus der Umgebung ergaenzen
        if os.environ.get(k):
            env[k] = os.environ[k]
    missing = [k for k in required if not env.get(k)]
    if missing:
        sys.exit(f"FEHLER: fehlt in .env bzw. Umgebung: {', '.join(missing)}")
    return env


class MCP:
    """Minimaler MCP-Client ueber HTTP - reicht fuer Tool-Aufrufe."""

    def __init__(self, key):
        self.key = key
        self.sid = None
        self._connect()

    def _post(self, payload):
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "x-consumer-api-key": self.key}
        if self.sid:
            hdrs["Mcp-Session-Id"] = self.sid
        # Beim Start um 9:00 ist das WLAN oft noch nicht bereit -> mehrfach versuchen.
        attempts = 6
        for n in range(1, attempts + 1):
            req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(),
                                         method="POST", headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return dict(r.headers), r.read().decode()
            except urllib.error.HTTPError as e:
                sys.exit(f"MCP-Fehler {e.code}: {e.read().decode()[:500]}")
            except urllib.error.URLError as e:
                if n == attempts:
                    sys.exit(f"Netzwerk nach {attempts} Versuchen nicht erreichbar: {e.reason}")
                wait = 30 * n
                log(f"Netzwerk nicht erreichbar ({e.reason}) - "
                    f"Versuch {n}/{attempts}, neuer Versuch in {wait}s")
                time.sleep(wait)

    @staticmethod
    def _parse(body):
        for line in body.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        try:
            return json.loads(body)
        except Exception:
            return None

    def _connect(self):
        h, _ = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05",
                                      "capabilities": {},
                                      "clientInfo": {"name": "fabrikstadt-igpost",
                                                     "version": "1.0"}}})
        self.sid = h.get("Mcp-Session-Id") or h.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def run_soft(self, slug, arguments, account=None):
        """Wie run(), bricht aber nicht ab - gibt (ok, daten_oder_fehlertext)."""
        try:
            return True, self.run(slug, arguments, account)
        except SystemExit as e:
            return False, str(e)

    def run(self, slug, arguments, account=None):
        """Fuehrt ein Composio-Tool aus, gibt dessen Nutzdaten zurueck.

        `account` waehlt die Verbindung aus (Alias oder Konto-ID). Sobald unter
        demselben Composio-Zugang mehr als ein Konto desselben Dienstes haengt,
        ist die Angabe Pflicht - sonst trifft es das falsche Konto.
        """
        item = {"tool_slug": slug, "arguments": arguments}
        if account:
            item["account"] = account
        _, body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL",
                                         "arguments": {"tools": [item]}}})
        env = self._parse(body)
        if not env:
            sys.exit(f"Unlesbare MCP-Antwort: {body[:400]}")
        if "error" in env:
            sys.exit(f"MCP meldet Fehler: {env['error']}")
        texts = [c.get("text", "") for c in
                 env.get("result", {}).get("content", [])]
        try:
            outer = json.loads("\n".join(texts))
        except Exception:
            sys.exit(f"Unerwartete Antwort auf {slug}: {texts[:1]}")
        results = outer.get("data", {}).get("results", [])
        if not results:
            sys.exit(f"Keine Ergebnisse fuer {slug}: {json.dumps(outer)[:400]}")
        resp = results[0].get("response", {})
        if not resp.get("successful"):
            sys.exit(f"{slug} fehlgeschlagen: {json.dumps(resp)[:600]}")
        return resp.get("data", {})


def check(env):
    data = MCP(env["COMPOSIO_API_KEY"]).run(T_INFO, {}, env.get("COMPOSIO_IG_ACCOUNT"))
    log(f"@{data.get('username')} | {data.get('account_type')} | "
        f"{data.get('followers_count')} Follower | "
        f"{data.get('media_count')} Beitraege | ID {data.get('id')}")
    if data.get("account_type") != "BUSINESS":
        log("WARNUNG: kein BUSINESS-Konto - Publishing wird scheitern.")
    if str(data.get("id")) != str(env["IG_USER_ID"]):
        log(f"WARNUNG: IG_USER_ID in .env ({env['IG_USER_ID']}) weicht ab "
            f"von {data.get('id')}")


def image_url(env, item):
    return env["PUBLIC_IMAGE_BASE_URL"].rstrip("/") + "/" + item["file"]


def derive_pin(item):
    """Pinterest fuehrt Titel und Beschreibung getrennt; Hashtags bringen dort nichts."""
    lines = [l.strip() for l in item["caption"].splitlines() if l.strip()]
    body = [l for l in lines if not l.startswith("#")]
    title = body[0][:100] if body else item["file"]
    rest = " ".join(body[1:])
    rest = rest.replace("wa.me/8618566581431", "").strip()
    return {"title": title, "description": rest[:800]}


def post_pinterest(env, item, mcp):
    """Setzt denselben Beitrag als Pin. Fehler hier duerfen Instagram nicht kippen."""
    board = env.get("PINTEREST_BOARD_ID")
    if not board:
        log("Pinterest uebersprungen (PINTEREST_BOARD_ID fehlt)")
        return None
    pin = derive_pin(item)
    args = {"board_id": board,
            "title": pin["title"],
            "description": pin["description"],
            "alt_text": pin["title"][:500],
            "media_source": {"source_type": "image_url",
                             "url": image_url(env, item)}}
    if env.get("PIN_LINK"):
        args["link"] = env["PIN_LINK"]
    ok, data = mcp.run_soft(T_PIN, args, env.get("COMPOSIO_PIN_ACCOUNT"))
    if not ok:
        log(f"PINTEREST FEHLGESCHLAGEN (Instagram-Post bleibt gueltig): {str(data)[:300]}")
        return None
    pid = data.get("id", "?")
    log(f"PIN GESETZT: {item['file']} -> Pin-ID {pid}")
    return str(pid)


def publish(env, item):
    mcp = MCP(env["COMPOSIO_API_KEY"])
    url = image_url(env, item)
    log(f"Container anlegen: {item['file']}")
    acct = env.get("COMPOSIO_IG_ACCOUNT")
    data = mcp.run(T_CONTAINER, {"ig_user_id": env["IG_USER_ID"],
                                 "image_url": url,
                                 "caption": item["caption"]}, acct)
    cid = data.get("id") or data.get("creation_id")
    if not cid:
        sys.exit(f"Keine creation_id: {json.dumps(data)[:400]}")
    log(f"Container {cid} angelegt, warte auf Verarbeitung")
    time.sleep(10)
    try:
        res = mcp.run(T_PUBLISH, {"ig_user_id": env["IG_USER_ID"],
                                  "creation_id": str(cid)}, acct)
    except SystemExit:
        log("Erster Versuch fehlgeschlagen, warte 20s und versuche erneut")
        time.sleep(20)
        res = mcp.run(T_PUBLISH, {"ig_user_id": env["IG_USER_ID"],
                                  "creation_id": str(cid)}, acct)
    mid = res.get("id", "?")
    log(f"VEROEFFENTLICHT: {item['file']} -> Media-ID {mid}")
    pin_id = post_pinterest(env, item, mcp)
    return str(mid), pin_id


def main():
    if "--check" in sys.argv:
        return check(load_env(("COMPOSIO_API_KEY", "IG_USER_ID")))
    env = load_env()

    queue = json.loads(QUEUE.read_text())
    now = datetime.datetime.now()
    force = "--force" in sys.argv   # Termin ignorieren, naechsten freigegebenen posten
    due = [i for i in queue if i["status"] == "approved"
           and (force or
                datetime.datetime.fromisoformat(i["scheduled_for"]) <= now)]
    if not due:
        pending = [i for i in queue if i["status"] == "approved"]
        # Leerlauf nur auf die Konsole, sonst laeuft die Logdatei voll.
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
              f"Nichts faellig. {len(pending)} freigegebene Beitraege wartend.")
        if len(pending) <= 2:
            log("HINWEIS: Warteschlange laeuft leer - neue Poster freigeben!")
        return

    item = sorted(due, key=lambda i: i["scheduled_for"])[0]
    if "--dry-run" in sys.argv:
        log(f"[DRY-RUN] wuerde posten: {item['file']}")
        log(f"[DRY-RUN] Bild-URL: {image_url(env, item)}")
        log(f"[DRY-RUN] Caption:\n{item['caption']}")
        pin = derive_pin(item)
        log(f"[DRY-RUN] Pinterest-Titel: {pin['title']}")
        log(f"[DRY-RUN] Pinterest-Text : {pin['description']}")
        log(f"[DRY-RUN] Pin-Board {env.get('PINTEREST_BOARD_ID')} -> {env.get('PIN_LINK')}")
        return

    media_id, pin_id = publish(env, item)
    item["status"] = "posted"
    item["posted_at"] = now.isoformat(timespec="seconds")
    item["media_id"] = media_id
    if pin_id:
        item["pin_id"] = pin_id
    QUEUE.write_text(json.dumps(queue, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
