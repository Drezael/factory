# Factory v5.16 – GitHub-Registry ueber URL-Dateien

**Du gibst nur ein leeres, privates GitHub-Registry-Repository an.** n8n richtet
`README.md` und `repos/.gitkeep` beim Start ein. Jedes zusaetzlich ueberwachte
Repository wird durch **eine Datei `repos/<name>.repo` mit genau einer
`https://github.com/OWNER/REPO`-URL** registriert. Datei loeschen = nach dem
naechsten erfolgreichen Scan nicht mehr ueberwachen. Es gibt keine automatische
Repository-Suche. Kein KI-Worker ist fuer das Scanning noetig.

**Anleitung:** [GITHUB-REGISTRY.md](GITHUB-REGISTRY.md) und
[UPDATE-v5.16.md](UPDATE-v5.16.md). Die alte YAML-basierte Registry wird ab
v5.16 nicht mehr eingelesen. Factory v5.15 und fruehere Versionen sind weiter
in den historischen `UPDATE-v*.md`-Dateien dokumentiert.

---

# Factory v5.14 – Entwicklung als Eingangskategorie mit höchster Priorität

Die Factory hat **drei getrennte GitHub-Issue-Formulare** und pro Repository **drei Eingangsspalten** im Kanban:

1. **Entwicklung Eingang** – neue Entwicklungsaufträge, Architekturänderungen und Änderungen an laufender Entwicklung (`factory:development`), **höchste Priorität**.
2. **Bug Eingang** – Fehlerberichte (`factory:bug`), zweithöchste Priorität.
3. **Feature Eingang** – Feature Requests (`factory:feature`), dritte Priorität.

**Alle drei Haupt-Issue-Typen durchlaufen verpflichtend die Planungsphase.** Der Planner erstellt erst die Feature-/Entwicklungsbereichs- und Task-Unter-Issues, danach erst gibt n8n die Entwicklung frei. Die neuen Tasks erben `factory:development`, `factory:bug` bzw. `factory:feature`, damit die Priorität **über alle überwachten Repositories** in der SQL-Dispatch-Warteschlange erhalten bleibt. Bereits laufende Worker werden **nicht** unterbrochen.

Für eine Änderung an laufender Entwicklung erstellst du ein **Entwicklungs-Issue** mit Referenz auf das ursprüngliche Issue bzw. den PR. Rückfragen des Agents bleiben **Kommentare im jeweiligen ursprünglichen Arbeits-Issue**; bei `Rueckfrage` beendet sich der Container und erst dein manueller Wechsel nach `Bereit` startet einen frischen Worker.

Das Fach-Statusfeld des GitHub-Projects bleibt die einzige Prozesssteuerung. Der vorhandene Status `In Arbeit` bleibt für laufende Umsetzung bestehen; **Entwicklung Eingang** ist die neue Eingangskategorie, nicht ein weiterer Laufzeitstatus.

Für bereits konfigurierte Registry-Repositories ist vor dem Upgrade in **jeder** `repos/*.yaml` bei `kanban.columns` der Eintrag `development_intake: Entwicklung Eingang` zu ergänzen. Siehe [UPDATE-v5.14.md](UPDATE-v5.14.md) und `registry-example/repos/`. Das Update liefert keine `.env` und ersetzt keine gespeicherten Logins.

---

# Factory v5.13 – Bug Reports und Feature Requests: getrennte Eingänge, gemeinsame Planung

Die komplette Factory bleibt ein einziges Docker-Compose-Deployment mit n8n, PostgreSQL,
GitHub-Registry, Agent-Manager und getrennten Codex-/Claude-/Gemini-Images.

**Jeder Bug und jeder Feature Request muss zuerst einen Planungs-Worker durchlaufen.**
GitHub-Formulare vergeben Typ-Labels (`factory:bug`, `factory:feature`); das Repository-Board
bekommt `Bug Eingang` und `Feature Eingang` als getrennte Statuskategorien.
Der Planner erstellt verknüpfte Fixbereich-/Feature- und ausführbare Task-Issues.
Erst wenn das übergeordnete Planungs-Issue auf `Erledigt` steht, werden Task-Issues
entsprechend ihren Abhängigkeiten auf `Bereit` gesetzt. Developer und Reviewer
sind immer neue, voneinander unabhängige Container.

**Priorität:** n8n beansprucht freie Jobs für Bug Reports und deren Fix-Tasks vor
Feature Requests. Bereits laufende Feature-Worker werden dabei nicht abgebrochen.

**Automatischer Planungsstart:** `intake.auto_plan_on_create: true` startet den Planner
bereits aus der Eingangsspalte (bei `agent.auto_execute: true` und globalem AutoRun).
Mit `false` bleibt die Eingangsspalte ein manueller Freigabepunkt: erst `Bereit`
startet die Planung. Bei `Rueckfrage` bleibt der Worker immer beendet; nach einer Antwort
wird das ursprüngliche Issue manuell auf `Bereit` verschoben.

**Start:** [UPDATE-v5.13.md](UPDATE-v5.13.md); Beispielkonfiguration:
`registry-example/repos/`; GitHub-Issue-Formulare werden bei Opt-in
(`intake.auto_install_forms: true`) idempotent im Ziel-Repository installiert,
sofern der Token die nötigen Schreibrechte besitzt.

---

# Factory v5.12 - Planning Issues and automatic Kanban boards

**GitHub controls the business state; n8n schedules and dispatches; AI agents are disposable.**

- A private registry repository lists monitored target repositories (`repos/*.yaml`).
- Workflow 04 calls the internal `/boards/ensure` endpoint **before** scanning. Missing GitHub Projects v2 boards are created and linked to the repository; configured Status options and a Kanban view are added without replacing existing status IDs. Workflow 09 provides a manual recheck. No AI worker is used for board setup.
- Each root issue moved to `Bereit` is handled as a Planning Issue. One **fresh Planner worker** returns a feature/task breakdown. The adapter creates linked GitHub feature and task sub-issues, each initially in `Backlog`.
- The Planning Issue goes to `Erledigt` and is closed only after all child Issues were created and linked. Only then does the scheduled scanner release unblocked task issues to `Bereit`. Later tasks require their dependencies in `Erledigt`.
- New disposable Developer workers implement individual tasks in their own `factory/issue-N` branches. Independent reviewers check the PR; CI checks must pass before an enabled automatic merge. Questions stay as comments in the respective original issue; the worker exits. Moving `Rueckfrage` to `Bereit` creates another fresh worker.
- Auto-run and auto-merge remain disabled in `.env` until GitHub permissions, provider logins, CI and branch rules are verified. Repository YAML also requires `agent.auto_execute: true`.

**Start here:** [UPDATE-v5.12.md](UPDATE-v5.12.md); private registry examples: `registry-example/repos/`. The HTTP-only setup is for a trusted local network.

---

## Neu in v5.11 – Feature-/Bug-Pipeline

Siehe [UPDATE-v5.11.md](UPDATE-v5.11.md) für die komplette Konfiguration, GitHub-Rechte, CI-Checks, Branch-Protection und die v5.11-Installation.

**Issue → frischer Developer → eigener Branch/PR → frischer Reviewer + GitHub Actions → automatischer Merge.** Bei Befunden wird ein verknüpftes Fehler-Issue erstellt, der Commit als failed markiert und mit neuem Worker automatisch nachgebessert. Rückfragen bleiben Kommentare im Ursprungs-Issue und erfordern eine manuelle Kanban-Freigabe.

**Standardmäßig ist Auto-Merge deaktiviert.** Ein Aktivieren setzt ein korrekt konfiguriertes Ziel-Repository mit GitHub CI/Branch Protection sowie beiden GitHub-Zugangstoken und den KI-Anmeldungen voraus.

---

# Factory v5.10 – Manuelle Rueckfragen-Schleife via n8n

Die vollstaendige Factory enthaelt n8n, PostgreSQL, dynamische Codex-/Claude-/Gemini-
Worker, ein privates Registry-Repository, GitHub-Projects-v2-Board je Zielrepo,
Issue-Rueckfragen als Kommentar im Ursprungs-Issue und **zustandslose** Worker-Läufe.

**Aktuelle Dokumentation:** [GITHUB-REGISTRY.md](GITHUB-REGISTRY.md) und
[UPDATE-v5.10.md](UPDATE-v5.10.md).

**Besonders wichtig:** Tickets werden ausschliesslich ueber GitHub Projects Status
(Backlog, Bereit, In Arbeit, Rueckfrage, Review, Erledigt, Fehler) gesteuert.
Nach einer Rueckfrage schreibt der Agent ins **Original-Issue** und endet. Nur du
kannst einen neuen Lauf freigeben, indem du das Ticket **manuell auf `Bereit`**
verschiebst. Der naechste Worker wird stets frisch gestartet. Die `.env` und Daten
des laufenden Deployments beim Update **nicht ueberschreiben**.

---

# Factory v5.8 – Gesamtes Docker-Compose-Deployment

**Factory** ist das ganze Docker-Compose-Projekt. Die Weboberflaeche ist n8n
unter `http://localhost:5678` (lokaler HTTP-Testbetrieb ohne SSL).

## Bestandteile

| Service | Funktion | Host-Port |
| --- | --- | --- |
| n8n | Zentrale Orchestrierung und Weboberflaeche | 5678 (konfigurierbar) |
| postgres | Persistenz fuer n8n und GitHub Issue Inbox | keiner |
| workflow-sync | Importiert JSON-Workflows automatisch | keiner |
| agent-manager | Erstellt dynamisch Codex-, Claude- und Gemini-Worker | keiner |
| github-registry **NEU** | Liest private Repo-Registry und GitHub Issues | keiner |

Codex, Claude und Gemini werden in **separaten Docker-Images** verwaltet.
Die Agent-Container entstehen nur bei einem Auftrag, nicht als permanente Services.

## Bestehende Factory aktualisieren

Die bisherige `.env` **behalten**, sonst stimmen Datenbankpasswort und
`N8N_ENCRYPTION_KEY` nicht mehr mit den gespeicherten Daten ueberein.

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.8-github-registry-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
```

**Aus dem Komplettpaket darf die `.env` bei einem Update nicht ueberschrieben werden:**
`unzip -o factory-v5.8-complete.zip -x 'factory/.env'`.

Fuer den GitHub-Registry-Dienst zusaetzlich die Anleitung
**`GITHUB-REGISTRY.md`** befolgen. Der Dienst startet auch ohne GitHub-Token,
aber meldet dann `needs_configuration` und fuehrt keine Scans aus.

## Neuinstallation

Das Komplettpaket enthaelt eine `.env` mit neu erzeugten Geheimnissen.

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.8-complete.zip
cd factory
sudo ./factory.sh config
sudo ./factory.sh start
```

Danach n8n einmalig einrichten. Die Workflow-JSON-Dateien werden automatisch
von `workflow-sync` in die n8n-Datenbank importiert. Das Auto-Publish fuer den
neuen GitHub-Monitor ist standardmaessig deaktiviert, bis du GitHub konfiguriert
hast. `FACTORY_GITHUB_AUTO_PUBLISH=true` aktiviert nur den neuen Scanner.

## Neue GitHub-Steuerung

```bash
sudo ./factory.sh github-health   # Registry-Dienst und Issue Inbox pruefen
sudo ./factory.sh github-scan     # Sofort scan ausloesen
sudo ./factory.sh github-events   # Offene Tickets anzeigen
sudo ./factory.sh github-logs     # Fehler und Scanprotokoll
```

Ein **privates** GitHub-Registry-Repository enthaelt `repos/*.yaml`, eine Datei
je ueberwachtem Repository. Zugangsdaten bleiben ausschliesslich lokal in der
Factory-`.env`. In v5.8 erfolgt die Issue-Erfassung **lesend**; es werden noch
keine autonomen Code-Aenderungen ausgelöst. Details: `GITHUB-REGISTRY.md`.

## Bestehende Factory-Werkzeuge

```bash
sudo ./factory.sh smoke all                  # Worker starten ohne AI-Login
sudo ./factory.sh auth-login codex default   # ChatGPT-Login (spaeter)
sudo ./factory.sh auth-login claude default  # Claude Code-Login (spaeter)
sudo ./factory.sh auth-login gemini default  # Gemini CLI-Login (spaeter)
sudo ./factory.sh update-worker codex        # Nur Codex neu bauen
sudo ./factory.sh update-worker claude       # Nur Claude neu bauen
sudo ./factory.sh update-worker gemini       # Nur Gemini neu bauen
sudo ./factory.sh workflows-check            # Gemountete Workflow-JSONs pruefen
sudo ./factory.sh workflows-sync-status      # Automatischen Import pruefen
sudo ./factory.sh reset-data --dry-run        # Vorschaumodus fuer Factory-Reset
sudo ./factory.sh reset-data                  # Factory-Bind-Daten nach Bestaetigung loeschen
sudo ./factory.sh stop                       # Gesamtes Deployment stoppen
sudo ./factory.sh status                     # Status anzeigen
```

Der Reset leert weiterhin die registrierten Bind-Datenverzeichnisse und auch
die GitHub-Issue-Inbox, weil diese im PostgreSQL-Bind-Mount liegt. Deine
lokalen JSON-Dateien, die gesamte Factory-Konfiguration und die privaten
GitHub-Registry-YAML-Dateien werden nicht geloescht.

## Docker-Netzwerke

- `factory_internal`: n8n, postgres, workflow-sync, agent-manager,
  github-registry; internes Bridge-Netz ohne direkten Internetzugriff.
- `factory_egress`: n8n, github-registry und dynamische Worker erhalten
  ausgehenden Internetzugriff fuer GitHub und KI-Login.
- Einzig n8n besitzt einen konfigurierbaren Host-Port.
- Alle dauerhaften Factory-Daten verwenden konfigurierbare Bind-Mounts.
- Docker-Socket exklusiv beim Agent-Manager, nicht bei n8n oder github-registry.

**Ohne SSL bitte keine oeffentliche Erreichbarkeit im Internet konfigurieren.**

## Dateien

```
factory/
  compose.yaml
  .env                         # lokal, geheim, NICHT einchecken
  .env.example
  factory.sh
  n8n-workflows/              # automatischer Import in n8n
  github-registry/            # neuer REST-Reader und Issue-Inbox
  registry-example/repos/     # VORLAGEN fuer separates privates GitHub-Repo
  manager/                    # Docker Worker Control API
  worker/                     # separate Provider Images
  workflow-sync/              # automatischer n8n Workflow-Import
  tests/
  data/                       # Bind-Daten; NICHT im Komplettpaket
```

## Versionshinweise und Tests

`CHANGELOG.md`, `GITHUB-REGISTRY.md`, `WORKFLOWS-README.md`,
`UPDATE-v5.8.md`.

Automatisierte Python- und Bash-Regressionstests pruefen Schema, YAML-Filter,
Worker-Management, Env-Reparatur und Workflow-Import. Ein vollstaendiger
Live-Test auf GitHub und Docker muss auf dem Host erfolgen.


## KI-Kontingente: Factory 10 (v5.15)

Die Factory startet bei vorhandener KI-Anmeldung pro Anbieter einen eigenen
kurzlebigen *Status-Container*. Über `sudo ./factory.sh limits-refresh` und
`sudo ./factory.sh limits-status` kannst du aktuelle Ergebnisse anzeigen.
Der importierte Workflow **Factory 10** überprüft die Profile alle fünf
Minuten automatisch. Codex nutzt `account/rateLimits/read`, Claude/Gemini
versuchen ihre jeweiligen CLI-Statusanzeigen. Unklare oder erschöpfte Limits
verhindern standardmäßig neue Entwicklungs-/Planungs-/Review-Aufträge, und
die GitHub-Karte bleibt `Bereit`. Weitere Informationen stehen in
`UPDATE-v5.15.md`.
