# Factory v5.15 – KI-Limit-Monitoring über kurzlebige Status-Container

## Zweck

Vor der Planung, Entwicklung oder dem Review wird geprüft, ob das zugewiesene
Login-Profil ausreichende und aktuelle Kontingentinformationen hat. Die Prüfung
startet pro Anbieter einen **eigenen kurzlebigen Status-Container auf Basis des
jeweiligen Worker-Images**, keinen zusätzlichen Modell-Turn.

- **Codex:** `codex app-server` → `account/rateLimits/read` (offizielle JSON-RPC-Schnittstelle). Liefert `usedPercent`, `resetsAt` und die Länge der Zeitfenster. Dies ist **kein** genauer Wert für verbleibende Tokens oder verbleibende Jobs.
- **Claude Code:** interaktiver CLI-Befehl `/usage`. Eine quota-spezifische, stabile nicht-interaktive JSON-API ist nicht verfügbar; bei unklarer TUI-Ausgabe bleibt der Status `unknown`.
- **Gemini CLI:** interaktiver CLI-Befehl `/model` zur Quotenanzeige. Bei unklarer Ausgabe ebenfalls `unknown`.

**Wichtig:** Die Status-Probes verwenden keine Modell-Prompts. Sie benötigen aber
Netzwerkzugang und eine bereits eingerichtete Anmeldung; OAuth-Tokens können
gegebenenfalls aktualisiert werden. Statusdaten werden nur vorübergehend in den
Logs ihrer kurzlebigen Docker-Container gehalten. Der Manager meldet keine
Roh-Ausgabe mit möglichen persönlichen Daten an n8n zurück.

## Update auf bestehender Factory

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.15-quota-monitor-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh build-workers all
sudo ./factory.sh start
sudo ./factory.sh limits-refresh
sudo ./factory.sh limits-status
```

**Bestehende `.env` nie überschreiben!** Die Update-ZIP enthält keine `.env`.
Passwörter, Login-Volumes, n8n-Daten und GitHub-Repos bleiben unverändert.

## Konfiguration in .env

```ini
FACTORY_LIMIT_MONITOR_ENABLED=true
FACTORY_LIMIT_UNKNOWN_POLICY=block
FACTORY_LIMIT_REFRESH_SEC=300
FACTORY_LIMIT_CHECK_TIMEOUT_SEC=25
FACTORY_LIMIT_MIN_REMAINING_PERCENT=5
FACTORY_LIMIT_CODEX_BUCKETS=codex
FACTORY_LIMIT_MAX_PROBES=3
```

`block` (Voreinstellung): Bei `unknown`, veralteten Daten, fehlendem Login,
`checking` oder ausgeschöpften Limits **keinen** Job starten. Dass Claude/Gemini
nicht immer maschinenlesbare Quoten liefern, kann dort bewusst zu gesperrten
Jobs führen. `FACTORY_LIMIT_UNKNOWN_POLICY=allow` erlaubt diese Jobs trotz
`unknown` (unsicherer; echte Limitüberschreitungen bleiben gesperrt). Eine
**veraltete positive Codex-Messung** ist auch mit `allow` keine Freigabe.

Der Codex-Schwellenwert `5` bedeutet: Mindestens 5 % *angezeigtes* Kontingent
müssen im gewählten Limit-Bucket frei sein. Das garantiert nicht, dass ein
langlaufender Entwicklungsjob vollständig in dieses Fenster passt.

## Neue API und n8n-Workflow

Der Agent-Manager führt die Probe über folgende **internen** APIs aus:

- `POST /limits/refresh` mit `{}`: Probes für noch nicht frisch geprüfte Profile starten
- `POST /limits/check` mit `{"provider":"codex","role":"developer"}`: explizite Einzelprüfung
- `GET /limits`: letzter bekannter Zustand je Login-Profil
- `GET /limits/availability?provider=codex&role=developer`: technische Freigabe

Die API verlangt wie bisher den internen Bearer-Token. Der Dienst
`github-registry` bietet für n8n intern `POST /limits/refresh` und `GET /limits`
als Proxy an, damit im Workflow kein neues Credential gespeichert werden muss.

Der neue automatisch importierte und veröffentlichte Workflow **Factory 10 –
KI Limits automatisch prüfen** startet alle fünf Minuten die kurzlebigen
Status-Container. Er kann auch manuell ausgeführt werden.

Die GitHub-Issue-Pipeline prüft die Kapazität **vor** dem Wechsel der
Kanban-Karte von `Bereit` nach `Planung`/`In Arbeit`. Wird eine Prüfung blockiert,
bleibt das Ticket im selben Status; ein späterer n8n-Zeitplan prüft erneut.
Der Manager prüft ein zweites Mal unmittelbar vor dem tatsächlichen Start.
**Smoke-Agents** bleiben von diesem Gate ausgenommen.

## Sicherheit und Grenzen

- Es werden nur die zugelassenen Provider-Images und Login-Profile verwendet.
- Status-Container bekommen keinen Workspace und keinen GitHub-Token.
- Sie erhalten ausschließlich das eigene Login-Verzeichnis und den separaten
  ausgehenden Netzwerkzugang. Keine eigenen Host-Ports.
- Ein Kontingent-Status ist eine **Momentaufnahme**. Auch bei Freigabe kann ein
  Anbieter während der Laufzeit ein Limit erreichen.
- Ein fehlendes offizielles Maschinenformat wird nicht durch erfundene
  Prozentzahlen ersetzt. Claude/Gemini sollten erst nach einem Live-Test
  ihrer jeweiligen CLI-Ausgabe zuverlässig automatisiert freigegeben werden.

## Prüfungen

Python-Unit-Tests, Bash-Syntax, JSON, JavaScript, YAML und Archivstruktur
wurden statisch geprüft. Ein Docker-/Login-Live-Test muss auf deinem Host erfolgen.
