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

## Factory v5.13 – Workflow-Verhalten

Es werden **keine neuen KI-Worker für die GitHub-Verwaltung** benötigt.
Workflow 04 richtet Boards und fehlende Kategorien ein und scannt Issues;
Workflow 07 beansprucht Jobs in der Reihenfolge **Bug vor Feature**.
Die Verarbeitung pro Bug und Feature Request startet zwingend mit `planner`.
Nach erfolgreicher Planung werden nur die verknüpften Arbeitsschritt-Issues
zur Entwicklung freigegeben. Die bestehenden Workflows 08 (Merge-Gate) und 09
(Board-Provisionierung) bleiben erhalten.

Die JSON-Workflows werden vom Container `workflow-sync` automatisch importiert;
ein manueller Import oder ein zusätzlicher n8n-Worker ist nicht erforderlich.

---

# Workflows added/updated in v5.12

- `04-factory-github-registry-scan.json`: every two minutes, n8n **first** ensures every configured Project v2 board, its Status categories and its Board view, **then** scans for GitHub issues and task dependencies. The scanner is not an AI worker.
- `09-factory-kanban-ensure.json`: manual n8n trigger to provision/verify boards without a GitHub scan.
- `07-factory-github-issue-runner.json`: existing n8n job loop also handles `planner` runs before development. All coding and reviewing still happens in independent disposable agent containers.

All files are synchronized on start by `workflow-sync`; an imported schedule workflow must be published (or enable `FACTORY_GITHUB_AUTO_PUBLISH=true` after configuring GitHub). The registry's write token stays in the `github-registry` container.

---

## Workflow 08 (neu in v5.11)

`08-factory-pr-merge-gate.json` wird automatisch synchronisiert. Es prüft alle zwei Minuten die bereits **erfolgreich reviewten** Pull Requests auf aktuelle GitHub Actions Checks, den richtigen Head SHA und die Mergeability. Nur bei aktivierter globaler und repo-spezifischer `auto_merge`-Freigabe wird automatisch gemergt. GitHub-API-Fehler führen zu keinem Merge.

Workflow 07 ist weiterhin der zustandslose n8n-Dispatcher: `Bereit` oder `In Arbeit` nach QA-Fehler → Developer, `Review` ohne gültiges Review auf dem aktuellen SHA → Reviewer. **Keine zusätzlichen Orchestrator-KI-Worker** werden gestartet.

---

# Factory v5.6 – Workflows automatisch synchronisieren

Seit v5.6 wird **kein manueller Import** mehr benoetigt. Der Compose-Dienst
`workflow-sync` wartet auf n8n (`GET /healthz/readiness`), liest das gemountete Verzeichnis
`/home/node/.n8n-files/workflows` und fuehrt den offiziellen n8n-CLI-Import
fuer neue oder geaenderte JSON-Dateien aus.

## Start

```bash
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
sudo ./factory.sh workflows-sync-status
```

In der n8n-Oberflaeche **Overview -> Workflows** aktualisieren. Bereits
installierte Factory-Workflows haben feste IDs und werden nicht mehrfach angelegt.

### Neue n8n-Installation

Zuerst das n8n-Administratorkonto in der Weboberflaeche einrichten. Der Importer
wartet bei fehlgeschlagenem Import und versucht es erneut, sobald n8n bereit
und die Konto-Einrichtung abgeschlossen ist.

## Verhalten

- **Bei Factory-/Containerstart:** Neue JSON-Dateien werden automatisch
  importiert; bei unveraenderten Dateien findet kein erneuter Import statt.
- **Beim Bearbeiten einer JSON-Datei auf dem Host:** Nur die betreffende Datei
  wird beim naechsten Durchlauf erneut importiert.
- **Beim Bearbeiten in der Weboberflaeche:** Ein gewoehnlicher Neustart
  ueberschreibt diese Aenderungen nicht, solange die zugehoerige JSON-Quelldatei
  unveraendert ist.
- **Wenn die JSON-Quelldatei geaendert wurde:** Die Version im n8n-Editor
  wird anhand der festen Workflow-ID ersetzt. Deshalb eigene UI-Aenderungen
  vor dem Aendern der Quelldatei exportieren.
- **Wenn eine Datei vom Host geloescht wird:** Der Workflow wird aus
  Sicherheitsgruenden NICHT automatisch aus n8n geloescht.
- **Beim ersten Start nach dem v5.5-Update:** Die Dateien werden einmalig
  importiert. Zuvor im UI angepasste gleichnamige Factory-Workflows koennen
  dadurch ueberschrieben werden.
- **Fehler:** Bei n8n-/Postgres-Ausfall, noch fehlendem Owner-Setup oder einem
  fehlgeschlagenen CLI-Import wird beim naechsten Durchlauf erneut versucht.
  Erfolge werden je Datei persistent gespeichert.

Die automatische Synchronisierung erzeugt **keine Workflows aus einem
GitHub-Repository** und startet auch keine KI-Agenten. Sie registriert
vorhandene, lokale JSON-Dateien in der n8n-Datenbank.

## Konfiguration nur in `.env`

```ini
FACTORY_N8N_WORKFLOWS_DIR=./n8n-workflows
FACTORY_N8N_AUTO_IMPORT=true
FACTORY_N8N_SYNC_INTERVAL_SEC=20
FACTORY_N8N_SYNC_IMPORT_TIMEOUT_SEC=180
FACTORY_N8N_SYNC_DATA_DIR=./data/n8n-workflow-sync
FACTORY_N8N_IMPORT_PROJECT_ID=owner
```

`FACTORY_N8N_IMPORT_PROJECT_ID=owner` verwendet das persoenliche Projekt des
Instanzbesitzers. Alternativ ist eine gueltige Projekt-ID moeglich.

**Datenschutz / Rechte:** Der `workflow-sync`-Container ist ausschliesslich am
`factory_internal`-Netz angeschlossen, hat keinen freigegebenen Port,
keinen Docker-Socket und sieht das Workflow-Quellverzeichnis nur lesend. Er
benoetigt dieselben PostgreSQL-Verbindungsdaten wie n8n, um den offiziellen
n8n-CLI-Import auszufuehren.

## Diagnose

```bash
sudo ./factory.sh workflows-check
sudo ./factory.sh workflows-sync-status
sudo docker compose logs --tail=100 workflow-sync
```

Die Datei `data/n8n-workflow-sync/.factory-workflow-sync-state.json` vermerkt
fuer jedes bereits importierte JSON den SHA-256-Hash. Damit werden bei einem
Neustart keine unveraenderten Dateien importiert. **Nicht manuell bearbeiten**.

Fuer eine Neuuebernahme einer JSON-Datei empfiehlt es sich, die Datei gezielt
zu aendern. Fuer einen bewussten manuellen Force-Import gibt es weiterhin
`sudo ./factory.sh workflows-import`; die automatische Synchronisierung ist
unabhaengig davon.

### Wichtige n8n-Einschraenkungen

Der n8n-Server-CLI-Import deaktiviert importierte Workflows standardmaessig.
Insbesondere nach einer Quelldatei-Aktualisierung muss ein zuvor aktivierter
Workflow moeglicherweise erneut veroeffentlicht werden. Das ist der Grund,
warum Factory v5.6 **nur Aenderungen** importiert und nicht bei jedem Restart
alle Dateien blind importiert.

Die HTTP-Request-Nodes brauchen weiterhin die von dir in n8n gespeicherte
`Header Auth`-Credential fuer den internen Agent-Manager. Ein Import erstellt
weder Zugangsdaten noch KI-Anmeldungen.

## Neu ab v5.8: GitHub-Registry-Workflows

Die JSONs `04-factory-github-registry-scan.json`,
`05-factory-github-issue-inbox.json` und `06-factory-github-registry-status.json`
liegen unter demselben automatisch gemounteten `n8n-workflows/`-Ordner.

- 04: Schedule alle 2 Minuten -> `github-registry` Scan -> neu erkannte Issues anzeigen.
- 05: Noch nicht bearbeitete GitHub-Issue-Ereignisse aus PostgreSQL auflisten.
- 06: Status/konfigurierten Repo-Scanner pruefen.

Wichtig: Ein in n8n importierter Schedule-Workflow ist erst nach
**Publish** automatisch aktiv. Nur Workflow 04 kann auf Wunsch per
`FACTORY_GITHUB_AUTO_PUBLISH=true` nach dem Import automatisch publiziert
werden. Diese Einstellung nicht vor der GitHub-Konfiguration einschalten.
Weitere Details: `GITHUB-REGISTRY.md`.

## Factory 07 – GitHub Issue Runner (v5.9)

Der Runner kontrolliert den in GitHub Projects v2 angezeigten Issue-Status.
`Bereit` -> `In Arbeit` -> (`Rueckfrage` oder `Review`/`Fehler`).
Bei Rueckfrage kommentiert er im **bestehenden Issue** und endet; eine Antwort oder Issue-Aenderung allein bewirkt NICHTS. **Erst deine manuelle
Verschiebung nach `Bereit`** wird beim naechsten Scan zur Freigabe eines ganz neuen
Containers. Der neue Agent prueft erneut, ob die Informationen reichen, und kann
bei Bedarf eine weitere Frage stellen. Diese Schleife wird nicht durch einen
lang laufenden Worker aufrechterhalten.

Veröffentlichung standardmaessig aus: `FACTORY_GITHUB_AUTORUN_ENABLED=false`.
Mit GitHub-Berechtigungen und Agent-Logins nach Pruefung auf `true` setzen
und in der YAML `auto_execute: true` freigeben.


### Factory 10 – KI Limits automatisch prüfen (v5.15)

Der Quoten-Workflow wird mit dem normalen Workflow-Sync automatisch importiert
und bei aktiviertem `FACTORY_LIMIT_MONITOR_ENABLED=true` auch veröffentlicht.
Er benötigt keine separaten n8n-Logins, da die HTTP-Knoten ausschließlich
den internen `github-registry`-Dienst ansprechen. Er löst nur Status-Probes,
keine Modell-Prompts aus. Auslastung bei Codex ist in den strukturierten Daten
ablesbar; für Claude und Gemini ist `unknown` bei nicht klar lesbaren TUI-
Quoten ein erwarteter Sicherheitszustand.
