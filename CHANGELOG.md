## Factory v5.16 – URL-Allowlist und leerer Registry-Bootstrap

- Leeres privates GitHub-Repo automatisch via n8n initialisieren (README,
  `repos/.gitkeep`), ohne Repository-Discovery.
- User verwaltet pro Repo eine `.repo`-Datei mit exakt einer GitHub-URL.
- Loeschen der Datei beendet die Ueberwachung und deaktiviert nicht gestartete
  Ereignisse; bestehende GitHub-Daten bleiben bestehen.
- Board/Kanban/Issue-Formulare und Planning-Pipeline werden aus zentralen
  Factory-Defaults erstellt.
- GitHub-Scan, n8n-Orchestrierung, Worker- und Quota-Infrastruktur bleiben im
  selben Docker-Compose-Deployment.

---

## v5.15 – Kontingent-Monitor über status-only Provider-Container

- Eigene kurzlebige Codex-/Claude-/Gemini-Status-Container ohne Modell-Prompts.
- Codex App-Server-Quoten (prozentuale Belegung, Reset-Zeitfenster).
- Konservativ unbekannter Status bei fehlendem verlässlichem TUI-Quotenformat.
- Manager-API `/limits`, `/limits/refresh`, `/limits/check`, `/limits/availability`.
- Quotenprüfung vor GitHub-Kanban-Claim und nochmals vor Agent-Spawning.
- Neuer Workflow Factory 10 mit automatischem Scheduling und Import.
- Konfigurierbare Fail-Closed-Policy, TTL und Sicherheitsschwelle in `.env`.

## v5.14 – Entwicklung Eingang: höchste Priorität

- Neues GitHub-Issue-Formular `00-factory-development.yml` für neue Entwicklung und Änderungen an laufender Entwicklung (optional verknüpftes Issue/PR).
- GitHub-Label `factory:development` wird bei `github-boards-ensure` erstellt; Formular nur bei `intake.auto_install_forms=true` und nur wenn noch nicht vorhanden installiert.
- Kanban-Status `development_intake` (`Entwicklung Eingang`) wird pro Repository idempotent provisioniert; bestehende Karten und Status-IDs bleiben erhalten.
- Automatischer Intake startet **ausschließlich den Planner**. Keine ungeplante direkte Entwicklung; Rückfragen weiterhin im bestehenden Issue.
- Vererbte Herkunft `source=development` in den nativen Sub-Issues; neue Entwicklungs-Tasks können erst nach abgeschlossener Planung freigegeben werden.
- Globale Dispatch-Reihenfolge: **Entwicklung > Bug > Feature**; FIFO innerhalb derselben Priorität, keine Unterbrechung laufender Worker.
- Regressionsprüfungen für Intake, Label-Konflikte, Planung, Unter-Issues und Queue-Sortierung ergänzt.

---

## v5.13 – Typisierte Issue-Eingänge mit verpflichtender Planung und Bug-Priorität

- GitHub-Issue-Formulare für Bug Reports und Feature Requests mit bestehenden Typ-Labels.
- Idempotentes Anlegen fehlender Issue-Labels und optionaler Formulare in Ziel-Repositories.
- Zwei neue GitHub-Project-Statuskategorien `Bug Eingang` und `Feature Eingang`, falls fehlend.
- Optional automatischer Planungsstart direkt bei neuer Issue-Karte; alternativ Freigabe via `Bereit`.
- Pflicht-Planung vor Developer: Root-Issues können keine direkten Entwicklungsjobs erzeugen.
- Verknüpfte Fixbereiche/Feature-Gruppen und ausführbare Task-Unter-Issues; diese erben den Typ.
- `Bug` hat Vorrang vor `Feature` im technischen Job-Dispatcher (nicht-preemptiv).
- Keine Wiederaufnahme alter KI-Session; Rückfragen bleiben im ursprünglichen Ticket.
- Automatische Review-/CI-/PR-Pipeline aus v5.11 bleibt erhalten.

---

## Factory v5.12 - Automatic Kanban boards and issue-driven Planning Gate

- New n8n workflow 09 for manual board provisioning, plus mandatory board provisioning before every workflow 04 scan; no AI workers are used for setup.
- GitHub Projects v2 per monitored repository: create private project as needed, repository link, unique board view, missing Status options (including Planung), preserve existing option IDs.
- New optional repo YAML `planning.enabled`, `planning.provider`, `planning.max_tasks` and `kanban.auto_create`, `kanban.project_title`, `kanban.view_name`.
- A root planning issue receives a fresh `planner` worker; structured feature/task plan is validated and published as linked GitHub native sub-issues.
- Parent Planning Issue closes only when child publishing succeeded; task dispatch is gated on parent Done and dependency Done.
- Fix missing pipeline/planning settings in the event snapshot; add independent Codex, Claude and Gemini Planner profile mappings.
- n8n workflows auto-import and all previous Factory services remain in the same Compose deployment.

## v5.11 – Autonome Entwicklung, Review-Issues und CI-Merge-Gate

- Zwei Ticketarten (Features und Bugs) über dieselbe GitHub Projects v2 Kanban-Steuerung.
- Developer-Worker je Lauf frisch; trusted GitHub Data API Publisher pusht Code in `factory/issue-N` und eröffnet/aktualisiert Pull Requests, ohne einen GitHub-Schreibschlüssel an KI-Container zu geben.
- Frischer Reviewer prüft PR unabhängig, gibt JSON-Befunde und success/failure als GitHub Commit Status weiter.
- Bei fehlgeschlagenem Review/CI: verknüpftes `factory:review-defect`-Issue, Parent auf `In Arbeit`, Retry in neuem Container; begrenzte `max_review_cycles`.
- Merge-Workflow 08 prüft exakte CI-Checks, Head-SHA, Review-Ergebnis und GitHub-Branch-Schutzregeln; nach Merge Issue/QA-Defekte schließen.
- GitHub-Arbeitsverzeichnisse nur für den Registry-Publisher lesbar (Gruppenrechte); Containergrenzen und Daten-Bind-Mounts bleiben bestehen.
- Auto-Merge ist ausdrücklich opt-in (`FACTORY_GITHUB_AUTOMERGE_ENABLED=false` voreingestellt).

---

## v5.10 – Manuell freigegebene Rueckfrageschleife ueber n8n

- **Keine automatische** `Rueckfrage -> Bereit`-Umschaltung bei Kommentar oder Issue-Edit.
- Ausschliesslich manuell gesetzter GitHub-Projects-Status `Bereit` erlaubt n8n einen NEUEN Worker.
- Agent stellt Rueckfragen im bestehenden Issue, beendet Container und setzt `Rueckfrage`.
- Jeder frische Run prueft Antwort + aktuelle Issue-Beschreibung erneut; beliebig viele Rueckfragen.
- GitHub-Auftragskontext ist auf das Manager-Limit von 12.000 Zeichen begrenzt,
  neueste Kommentare werden bevorzugt.
- Striktes JSON-Ergebnisprotokoll fuer Issue-Worker, damit freie Textantworten
  nicht als erfolgreiches `Review` durchgehen. Manuelle Agent-API ist kompatibel.
- Keine neue Compose-Komponente, kein zweiter Worker zur Statusueberwachung.

## v5.9 – Issue-Status-zentrierte Factory
- GitHub Projects v2 `Status` als einzige fachliche Steuerung, kein Label-Ausloeser.
- Eigene Boards pro Repo mit konfigurierbarem Statusfeld und sieben Spalten.
- Neue Issues automatisch im Backlog, Ready-Karten in der n8n-Queue.
- Rueckfrage ausschliesslich als Kommentar im bestehenden Issue; Worker beendet.
- Menschliche Antwort oder relevante Titel-/Body-Aenderung -> Bereit -> neuer, zustandsloser Worker.
- n8n-Runner 07 mit kontrolliertem Start, Status-Polling und Ergebnis-Kommentar.
- Keine automatische KI-Ausfuehrung ohne doppelte Freigabe (globale .env + Repo-YAML).
- Developer/Tester-AutoRun bis GitHub-PR-Persistenz gesperrt.

## v5.8 (2026-11-18) – Privates GitHub Registry Repository

- Neuer `github-registry`-Dienst: lesender GitHub-API-Zugriff und YAML-Registry mit einer Datei pro Repo.
- Scans aus n8n via Factory 04, Issue Inbox via Factory 05, Health via Factory 06.
- Dedup/Scan-Checkpoints und Events im PostgreSQL-Schema `factory_registry`.
- Filter: enabled, open/closed/all, required/ignored labels, first-sync existing/new_only.
- Automatisches Publish fuer ausschliesslich Factory 04 optional via `FACTORY_GITHUB_AUTO_PUBLISH=true`.
- `factory.sh github-health|github-scan|github-events|github-logs`.
- Bestehendes `.env`-Upgrade ergaenzt optionale leere Werte korrekt.
- Keine neuen externen Ports und keine Aenderung der bestehenden Agent-Logins.

# Factory Changelog

## v5.7 - Destruktiver Datenreset mit Sicherheitspruefungen

- `./factory.sh reset-data` (Alias `reset`) setzt das komplette persistente
  Factory-Deployment zurueck, laesst aber Projektcode, `.env` und Workflow-JSON
  unangetastet.
- `--dry-run` zeigt alle betroffenen Host-Pfade ohne Aenderung.
- Interaktive Bestaetigung `RESET-FACTORY`; skriptgesteuerte Nutzung nur mit
  `--yes --confirm RESET-FACTORY`.
- Erlaubte Reset-Wurzeln stehen in `FACTORY_RESET_ALLOWED_ROOTS`, Standard
  `./data`. Keine Symlinks, ueberschneidenden Mounts oder zu breiten Verzeichnisse.
- Docker muss erfolgreich heruntergefahren werden, bevor geloescht wird.
- Fuenf Data-Bind-Mounts werden geleert; Workflow-Quellen werden explizit bewahrt.
- Neue Offline-Regressionstests fuer Fehler- und Schutzfaelle.

## v5.6 – Automatischer Workflow-Import beim Start

- Neuer Compose-Dienst `workflow-sync` (nur internes Netzwerk, ohne Docker-Socket).
- Automatischer Import neuer/geaenderter JSON-Dateien ueber n8n Server CLI.
- Read-only-Workflow-Quelle, eigener bind-gemounteter persistenter Import-State.
- Erneuter Start ueberschreibt keine unveraenderten Workflows.
- Retry nach n8n-Neustart bzw. sobald das Administratorkonto eingerichtet ist.
- Neue `.env`-Variablen und `./factory.sh workflows-sync-status`.
- `./factory.sh workflows-import` bleibt fuer bewussten Force-Import verfuegbar.

## v5.5 – Workflow-Import in n8n

- `./factory.sh workflows-import` registriert die gemounteten JSON-Dateien direkt in der n8n-Datenbank.
- Feste Workflow-IDs verhindern doppelte Datensaetze bei wiederholten Imports (Achtung: Reimport ueberschreibt UI-Aenderungen).
- `FACTORY_N8N_IMPORT_PROJECT_ID=owner` (optional: konkrete Projekt-ID) steuert die Zuordnung.
- `workflows-check` prueft weiterhin nur den Mount; `workflows-import` ist ein separater expliziter Schritt.
- Keine automatische Ausfuehrung oder Freischaltung von Workflows; keine neuen Container erforderlich.

# Factory – Änderungsprotokoll

## v5.4 – Workflow-Bind-Mount

- Alle `n8n-workflows/*.json` des Factory-Projektverzeichnisses sind im n8n-Container
  unter `/home/node/.n8n-files/workflows` schreibgeschuetzt verfuegbar.
- `FACTORY_N8N_WORKFLOWS_DIR` in `.env` steuert den Host-Pfad; fehlende Variable
  wird bei `./factory.sh repair-env` ergaenzt.
- `./factory.sh workflows-check` prueft im laufenden n8n-Container die Lesbarkeit
  und JSON-Gültigkeit der Dateien.
- Die drei n8n-Workflows aus v5.3 sind im Gesamtpaket enthalten.
- Es findet kein automatischer Import in die n8n-Datenbank statt.

## v5.3 – n8n-Workflows

- Systemstatus, manueller Agent-Auftrag mit Polling und provideruebergreifender Smoke-Test.

## v5.2 – Konfigurationsreparatur und Gesamtarchiv

- Vollständiges Deployment mit konsistenter Compose-Datei und zugehöriger `.env`.
- `POSTGRES_PASSWORD` und älteres `FACTORY_DB_PASSWORD` als synchronisierte Aliasse.
- Auch die Aliasnamen für Benutzer/DB (`FACTORY_DB_NAME` / `FACTORY_DB_USER`) werden unterstützt.
- Neues `./factory.sh repair-env` (Alias `upgrade-env`) ergänzt fehlende Einstellungen,
  bewahrt bestehende Secrets und sichert die alte `.env`.
- Konkurrierende, unterschiedliche DB-Passwörter werden nicht automatisch geändert.
- `./factory.sh stop` verwendet Docker-Projektlabels und funktioniert bei unvollständiger `.env`.
- Bestehender Workspace-Berechtigungsfix v5.1 bleibt enthalten.
- Drei unabhängig aktualisierbare Worker-Images, Bind-Mounts, getrennte Netzwerke bleiben bestehen.
- Offline-Regressionstests für DB-Alias, widersprüchliche Werte und defekte `.env` ergänzt.

## v5.1 – Berechtigungen

- Workspace-Reihenfolge `mkdir -> chmod -> chown`; kein `CAP_FOWNER` nötig.
- `doctor` und `repair-workspaces` ergänzt.

## v5 – Bind-Mounts und interne Netzwerke

- Konfigurierbare Host-Verzeichnisse für PostgreSQL, n8n, Login-Profile und Workspaces.
- Separates internes Netz und Internet-Egress-Netz.

## v4 – Multi-Provider Worker

- Eigenständige Images für Codex, Claude Code und Gemini CLI.
