# Factory v5.14 – Update: Entwicklung Eingang + globale Priorisierung

## Änderungen

- Separates GitHub-Issue-Formular **Entwicklung / Entwicklungsänderung** mit dem Label `factory:development`.
- Neue Project-Kanban-Kategorie **Entwicklung Eingang** (`development_intake`).
- Dispatcher-Priorität über alle Repositories: **Entwicklung (0) > Bug (1) > Feature (2)**; gleiche Priorität nach Ereignis-ID, laufende Worker bleiben aktiv.
- Pflicht-Planung auch für Entwicklungsaufträge und Änderungswünsche: Haupt-Issue -> Planner -> verknüpfte Unter-Issues -> Developer -> Review/CI -> Merge.
- Unter-Issues erben die Kategorie des ursprünglichen Planungs-Issues; kein direkter Developer-Start für ungeplante Haupt-Issues.

## 1. Zuerst die private Registry aktualisieren (wichtig!)

**Vor dem Factory-Update** ergänze in **jeder überwachten** `repos/*.yaml` mit `intake.enabled: true` den neuen Eintrag bei `kanban.columns`:

```yaml
kanban:
  enabled: true
  columns:
    development_intake: Entwicklung Eingang
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
intake:
  enabled: true
  auto_install_forms: true
  auto_plan_on_create: true
planning:
  enabled: true
```

Dies ist ein Auszug. `owner`, `pipeline`, `agent` und die restlichen Einträge deiner YAML bleiben bestehen; die vollständigen Beispielkonfigurationen stehen unter `registry-example/repos/`.

Wenn die neue Kategorie bei eingeschaltetem `intake.enabled` fehlt, **verweigert die Factory die Konfiguration des Repositories** bewusst, damit keine Entwicklung falsch gestartet wird.

## 2. Update-Archiv installieren

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.14-development-priority-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
sudo ./factory.sh github-boards-ensure
sudo ./factory.sh github-scan
```

`github-boards-ensure` ergänzt den neuen Status und das fehlende Formular, wenn die Repository-YAML `intake.auto_install_forms: true` setzt und die GitHub-Rechte dafür ausreichen. Ein bestehendes Formular wird **nicht überschrieben**. Auf einem geschützten Standardbranch muss die Formular-Datei möglicherweise per PR ergänzt werden.

**Das Update überschreibt keine `.env`, keine Postgres-/n8n-Daten, keine Agent-Logins, keinen Workflow-Sync-Stand.** Die existierenden n8n-Workflows bleiben unverändert und müssen nicht erneut importiert werden; sie sprechen weiterhin mit dem aktualisierten internen GitHub-Adapter.

## 3. Funktion testen

1. Im überwachten GitHub-Repository: **New issue** -> **Entwicklung / Entwicklungsänderung** wählen.
2. Einen neuen Entwicklungsauftrag oder eine Änderung am laufenden Code beschreiben, optional das betroffene Issue/den PR referenzieren.
3. Der nächste Scan legt die Karte in **Entwicklung Eingang** ab. Mit `intake.auto_plan_on_create: true` und bei aktiviertem AutoRun kann der **Planner** beginnen; andernfalls verschiebst du die Karte manuell nach **Bereit**.
4. Der Planungs-Worker kann im bestehenden Issue nachfragen und endet bei **Rueckfrage**. Nach deiner Antwort musst du **Bereit** setzen, worauf ein frischer Planner startet.
5. Erst nach abgeschlossenem Plan werden die angelegten Entwicklungs-Unter-Issues umgesetzt und unabhängig geprüft.

Die globale Priorität gilt immer nur für **wartende** Jobs. Ein bereits laufender Bug- oder Feature-Worker wird nicht abgebrochen.

## 4. Freischaltung / Sicherheit

Bis zur Überprüfung deiner KI-Anmeldungen, GitHub-Rechte und CI-Checks sollte Folgendes gelten:

```ini
FACTORY_GITHUB_AUTORUN_ENABLED=false
FACTORY_GITHUB_AUTOMERGE_ENABLED=false
```

Für eine spätere automatische Bearbeitung zusätzlich in der Registry-YAML pro Repository `agent.auto_execute: true` setzen. GitHub-Formulare vergeben Typ-Labels; die **fachliche** Statussteuerung bleibt ausschließlich das GitHub-Projects-`Status`-Feld.

## Verifikation

Automatisierte Offline-Tests wurden ausgeführt. Ein Live-Docker-/GitHub-End-to-End-Test muss in der lokalen Factory durchgeführt werden.
