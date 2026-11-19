# Factory v5.11 – Feature-/Bug-Issue → Branch → QA → Merge

Dieser Release baut auf Factory v5.10 auf. Er erweitert die vorhandenen n8n-Workflows,
ohne die bestehenden n8n-Bind-Mounts, Login-Volumes oder PostgreSQL-Daten zu ersetzen.

## Architektur und Statuslogik

- **GitHub Projects v2** ist die einzige fachliche Statusquelle.
- **n8n** führt die Taktung und den Workflow aus. Kein KI-Worker für Kanban-Kontrolle.
- **GitHub Registry** prüft Status, verwaltet das private YAML-Registry-Repo und publiziert Commits/PRs/Review-Tickets mit seinem GitHub-Token.
- **Agent-Manager** startet je Job einen frischen, zustandslosen Codex-/Claude-/Gemini-Container.
- **Entwickler-Worker** bearbeitet isolierten Code. Der Registry-Adapter veröffentlicht die geänderten Dateien per GitHub-Git-Data-API im `factory/issue-N`-Branch; dem KI-Worker wird kein Write-Token ausgehändigt.
- **Reviewer-Worker** wird danach frisch und unabhängig gestartet; er prüft den PR und liefert einen strukturierten `pass`-/`fail`-Befund.
- **GitHub Actions** führt reguläre CI-Prüfungen aus. Nur deren explizit konfigurierte Checks und der erfolgreiche AI-Review erlauben den Merge.

```text
Backlog --[Mensch: Bereit]--> In Arbeit --[Dev+PR]--> Review
                                  |                      |
                            [Rueckfrage]          [Review/CI failed]
                                  |                      |
                              Rueckfrage            Fehler-Issue
                                  |                      |
                       [Mensch: Bereit]             In Arbeit
                                  |                      |
                             Neuer Dev <---------------+

Review + AI-Review pass + alle required_checks success --> Squash-Merge --> Erledigt
```

Rückfragen werden **als Kommentar im bestehenden Issue** gestellt. Der Worker endet. Erst wenn ein Mensch nach der Antwort das Issue wieder nach `Bereit` verschiebt, wird ein neuer Worker gestartet. Wiederholte Rückfragen sind möglich.

Review-Fehler bekommen hingegen ein **neues, verknüpftes Fehler-Issue** mit Label `factory:review-defect`. Die Factory legt dieses Label bei Bedarf selbst an. Der Fehlerstatus wird auf dem betroffenen Commit als `code-factory/agent-review: failure` vermerkt. Nach max. `max_review_cycles` ist der übergeordnete Auftrag in `Fehler` und muss manuell geprüft werden. Fehler-Issues werden nach erfolgreichem Merge des Ursprungsauftrags geschlossen.

## 1. Update der bestehenden Installation

Im Elternordner des bestehenden `factory`-Verzeichnisses:

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.11-github-pipeline-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh build-workers all
sudo ./factory.sh start
```

Der Update-ZIP enthält **keine `.env`**; sämtliche vorhandenen Daten und Logins bleiben erhalten. Der neue Status-/Merge-Workflow `08` wird über den bereits existierenden `workflow-sync` importiert.

## 2. Zugangsdaten / Schutzschalter in .env

```ini
# Bereits aus v5.8+:
FACTORY_GITHUB_REGISTRY_REPO=meine-org/factory-registry
FACTORY_GITHUB_REGISTRY_TOKEN=github_pat_WRITE_TOKEN
FACTORY_GITHUB_TOKEN=github_pat_READ_ONLY_FOR_CLONE
FACTORY_GITHUB_AUTO_PUBLISH=true
FACTORY_GITHUB_AUTORUN_ENABLED=true

# Neu (standardmäßig AUS; erst nach GitHub CI und Branch Protection einschalten):
FACTORY_GITHUB_AUTOMERGE_ENABLED=true
```

**Schutztrennung:** Das `FACTORY_GITHUB_REGISTRY_TOKEN` mit Schreibrechten ist nur im internen `github-registry`-Dienst vorhanden. `FACTORY_GITHUB_TOKEN` in `agent-manager` darf **nur Contents: read** auf die überwachten privaten Repositories haben; dieser Token dient dem Clone in Agent-Containern. Für öffentliche Repositories kann er leer bleiben.

Für das **Registry-Token** sind Berechtigungen für Registry Contents lesen, Ziel-Repo Contents schreiben, Pull Requests schreiben, Issues schreiben, Commit Statuses schreiben, Checks lesen und Projects v2 Felder lesen/schreiben erforderlich. GitHub Projects v2 benötigt je nach Token-Typ GitHub-App- oder Projects-Berechtigungen. PATs sollten auf die erlaubten Repositories begrenzt und regelmäßig rotiert werden.

Der KI-Anbieter-Login bleibt wie bisher separat (`./factory.sh auth-login ...`); es werden **keine Modell-API-Keys** benötigt.

## 3. Private Registry-YAML (einmal je Ziel-Repository)

Beispiel aus `registry-example/repos/backend.yaml`:

```yaml
version: 1
repository: meine-org/backend
...
agent:
  provider: codex
  role: developer
  auto_execute: true
  timeout_seconds: 900
pipeline:
  enabled: true
  base_branch: main
  developer_provider: codex
  reviewer_provider: claude
  max_review_cycles: 3
  required_checks: [factory-tests]
  auto_merge: true
```

Jedes überwachte Repository braucht ein eigenes GitHub Projects v2 Board. Die Kanban-Spalten aus v5.10 bleiben: `Backlog`, `Bereit`, `In Arbeit` (alternativ `Entwicklung`), `Rueckfrage`, `Review`, `Erledigt`, `Fehler`. Die genaue Schreibweise wird in der YAML auf tatsächliche GitHub-Statusoptionen abgebildet.

**Feature Requests und Bug Reports** brauchen keine unterschiedlichen Pipelines. Beide sind normale GitHub Issues; ein Mensch setzt die Karte `Bereit`. Neu angelegte Tickets gehen zuerst in `Backlog`.

## 4. GitHub Actions & Branch-Protection

Kopiere die Beispiel-CI unter `registry-example/ci-templates/factory-ci.yml` **bewusst** in jedes überwachte Repo nach `.github/workflows/factory-ci.yml`, oder verwende dessen bereits vorhandene Test-/Build-Pipeline. Passe sie an deine Sprache und Abhängigkeiten an.

Setze `pipeline.required_checks` auf die **exakten GitHub-Check-Namen**, zum Beispiel `factory-tests`. CI-Checks müssen auf dem **aktuellsten PR-Head-SHA** erfolgreich sein. Zusätzlich erstellt die Factory den Commit-Status `code-factory/agent-review` auf diesem SHA. Konfiguriere in GitHub Rulesets/Branch Protection beide Statuschecks als *required*. Die Factory liest die Checks erneut vor dem Merge und GitHub erzwingt seine übrigen Branch-Regeln.

Für vollständig automatisierten Merge sollten nicht zusätzlich manuelle PR-Review-Freigaben vorgeschrieben sein, die kein AI-Commitstatus erfüllen kann. Ohne kompatible Branch-Protection kann keine hinreichende End-to-End-Absicherung behauptet werden. Setze die Schutzregeln vor dem Aktivieren von `FACTORY_GITHUB_AUTOMERGE_ENABLED=true`.

## 5. Test

```bash
sudo ./factory.sh config
sudo ./factory.sh github-health
sudo ./factory.sh github-scan
sudo ./factory.sh workflows-sync-status
sudo docker compose logs --tail=100 github-registry
sudo docker compose logs --tail=100 workflow-sync
```

In n8n müssen `Factory 04` (Scan), `Factory 07` (Issue-Agent-Runs) und `Factory 08` (Merge-Gate) automatisch erscheinen. Veröffentlichung ist nur bei der bewusst aktivierten Kombination aus `FACTORY_GITHUB_AUTO_PUBLISH`, `FACTORY_GITHUB_AUTORUN_ENABLED` und `FACTORY_GITHUB_AUTOMERGE_ENABLED` vorgesehen.

Erst mit einem **Test-Repo**, zwei gültigen Anbieter-Logins und CI lässt sich die End-to-End-Pipeline live prüfen. Automatische Tests in diesem Archiv ersetzen diesen Live-Test nicht.

## Grenzen der MVP-Sicherheitsarchitektur

- Keine fremden, nicht vertrauenswürdigen Repositories ausführen; das Docker-Socket des Managers ist hochprivilegiert.
- GitHub-Token nie direkt im Issue, Agent-Prompt oder Code hinterlegen. Agenten bekommen nur einen Repo-Clone-Token mit Leserechten.
- Der Publisher blockiert Änderungen an `.github/workflows/`, `.env`, `CODEOWNERS` und weiteren sensitiven Pfaden. Binäre Änderungen, Symlinks, Submodule und große Repositories werden im MVP nicht automatisch publiziert.
- Race Conditions zwischen externen menschlichen Pushes, GitHub API-Ausfällen oder Redispatching nach einem Stromausfall sollten später mit einem dauerhaften Reconciliation-Controller und Monitoring vollständig abgesichert werden.
- `max_review_cycles` verhindert unendliche automatische Code-Korrekturen; nach dem Limit wechselt das Ticket auf `Fehler`.
