# Factory v5.13 – Installation: Bug-/Feature-Intake mit Pflicht-Planung

## Bestehende Factory aktualisieren

1. ZIP `factory-v5.13-typed-intake-update.zip` in `~/Documents/` entpacken.
   Das Update enthält **keine `.env`**; verwende es für dein laufendes Deployment.
2. Aus dem Factory-Verzeichnis:

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.13-typed-intake-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
sudo ./factory.sh github-boards-ensure
sudo ./factory.sh github-scan
```

`./factory.sh start` baut den internen `github-registry`-Docker-Dienst mit den
neuen Formularvorlagen neu. Die anderen Factory-Services, Bind-Mounts und
Anmeldungen bleiben erhalten.

## Privates Registry-Repository je Zielprojekt aktualisieren

Siehe `registry-example/repos/backend.yaml` und `frontend.yaml`.
Pflichtfelder bei aktivierter automatischer Pipeline:

```yaml
kanban:
  enabled: true
  auto_create: true
  columns:
    bug_intake: Bug Eingang
    feature_intake: Feature Eingang
    backlog: Backlog
    planning: Planung
    ready: Bereit
    in_progress: In Arbeit
    waiting_for_answer: Rueckfrage
    review: Review
    done: Erledigt
    failed: Fehler
planning:
  enabled: true
  provider: codex
  max_tasks: 40
intake:
  enabled: true
  auto_install_forms: true
  auto_plan_on_create: true
```

Die übrigen Pflichtfelder (`kanban.owner`, `pipeline.required_checks`,
`agent.auto_execute` usw.) stehen in den vollständigen YAML-Vorlagen.
Die beiden Eingangsstatus werden über `github-boards-ensure` bei Bedarf angelegt;
es werden keine bestehenden Status-IDs oder Karten überschrieben.

Mit `auto_install_forms: true` legt der interne GitHub-Adapter *nur fehlende* Dateien
`.github/ISSUE_TEMPLATE/01-factory-bug.yml` und
`.github/ISSUE_TEMPLATE/02-factory-feature.yml` im Ziel-Repository an.
Die Forms vergeben die Labels `factory:bug` und `factory:feature`.
Die benötigten Labels werden zuvor vom Adapter idempotent erstellt.
Bei Branch-Schutz darf der Adapter die Formulare gegebenenfalls nicht direkt
auf dem Default-Branch anlegen; installiere sie dann per Pull Request.

## Ablauf

1. Du legst einen **Bug Report** oder **Feature Request** per GitHub-Formular an.
2. Die Factory nimmt ihn in das richtige Repository-Project auf: `Bug Eingang`
   oder `Feature Eingang`. Bei `intake.auto_plan_on_create: true` startet die
   **Planung automatisch**, sonst erst nach manueller Freigabe mit `Bereit`.
3. Ein **frischer Planner-Worker** prüft den Bedarf. Bei fehlenden Angaben stellt
   er Fragen im ursprünglichen Issue und beendet den Container. Status `Rueckfrage`.
   Du antwortest und bewegst das Issue manuell nach `Bereit`; ein neuer Planner startet.
4. Bei ausreichenden Informationen erstellt die Factory eigenständige
   GitHub-Fixbereich-/Feature-Issues und ausführbare Task-Unter-Issues.
5. Das Haupt-Issue wird erst nach erfolgreicher Erstellung aller Unter-Issues
   auf `Erledigt` gesetzt; erst dann gehen nicht blockierte Tasks nach `Bereit`.
6. Ein neuer Developer-Worker implementiert einen Task auf eigenem Branch,
   ein unabhängiger Reviewer prüft ihn und GitHub Actions liefert Tests.
7. Nur nach bestandenen Prüfungen und freigegebenem Merge-Gate kann ein automatischer
   Merge stattfinden. QA-Probleme werden als eigene verknüpfte Fehler-Issues angelegt.

**Priorität:** Die SQL-Jobqueue sortiert `bug` vor `feature` über *alle*
registrierten Repositories. Bereits laufende Feature-Worker werden nicht
abgebrochen. Die Zahl paralleler Worker bleibt begrenzt.

## Sicherheit und Aktivierung

Automatische KI-Starts bleiben global aus, solange
`FACTORY_GITHUB_AUTORUN_ENABLED=false` in der lokalen `.env` gesetzt ist.
Auch pro Repo muss `agent.auto_execute: true` aktiviert werden.
Automerge bleibt über `FACTORY_GITHUB_AUTOMERGE_ENABLED=false` aus,
bis echte CI-Checks, Branch-Schutz und KI-Anmeldungen auf dem Test-Repository
erfolgreich geprüft sind.

**Nicht aktivieren**, bevor GitHub Projects v2-Rechte, GitHub-Issues- und
Contents-Rechte (bei Auto-Formularinstallation) und die drei KI-Logins getestet sind.

## Prüfstand

Automatisierte Offline-Unit-Tests und YAML/Bash-Prüfungen wurden durchgeführt.
Eine Live-GitHub-Interaktion sowie Docker-Containerstarts sind auf deinem Testserver
noch auszuführen. Es wurden keine GitHub-Repositories verändert.
