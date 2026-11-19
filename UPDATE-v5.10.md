# Update auf Factory v5.10 – Manuelle Freigabe der Rueckfragen

Das Update enthaelt **keine .env** und aendert keine dauerhaften Bind-Mounts.

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.10-kanban-loop-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
# Neue Agent-Protokollpruefung in alle drei Worker-Images einbauen:
sudo ./factory.sh build-workers all
sudo ./factory.sh start
sudo ./factory.sh github-health
sudo ./factory.sh github-scan
```

Alle n8n-Workflows werden wie bisher durch `workflow-sync` automatisch
aktualisiert. Workflow 07 heisst jetzt **n8n Kanban-Dispatcher – frische Worker**.
Die Workflow-ID bleibt unveraendert; bei einem bereits bearbeiteten Workflow
kann der JSON-Sync die UI-Aenderungen ueberschreiben. Ggf. zuvor exportieren.

## Ablauf

1. Ticket in GitHub Projects **manuell auf `Bereit`** setzen.
2. n8n scannt (Workflow 04) und beansprucht (Workflow 07) nur bereite Tickets.
3. Ein NEUER Worker verarbeitet Issue-Beschreibung und juengste Kommentare.
4. Fehlen Angaben, kommt eine Rueckfrage **im bestehenden Issue** und Status
   `Rueckfrage`. Der Worker ist beendet und wartet NICHT.
5. Du antwortest im Issue und verschiebst die Board-Karte **selbst nach `Bereit`**.
6. N8n startet einen neuen Container. Der Agent entscheidet erneut. Beliebig oft.
7. Wenn ausreichend Informationen vorliegen, geht die Rollenarbeit nach `Review`.

**Keine neuen Issues**, keine automatische Rueckkehr von `Rueckfrage` nach
`Bereit`, kein extra Worker fuer Kanban-/Kommentarueberwachung.
`github-registry` bleibt ein bestehender interner API-/YAML-Adapter fuer GitHub;
der Zeitplan, die API-Aufrufe und das Starten der Worker werden in n8n ausgefuehrt.

## Sicherheit und Grenzen

- Standard `FACTORY_GITHUB_AUTORUN_ENABLED=false`. Erst nach Login, Freigabe
  und dem Test mit deinem GitHub Project `true` setzen; in Repo-YAML
  `agent.auto_execute:true` ebenfalls separat freigeben.
- Sobald GitHub-Projects-Status `Bereit` gesetzt wird, darf die Factory starten.
  Nur berechtigte Project-Mitglieder sollten diesen Status veraendern koennen.
- Worker startet mit frischem Verzeichnis und ohne Session-Resume; die CLI-
  Login-Profile sind weiterhin persistent fuer OAuth-Token, keine Agent-Erinnerungen.
- Im MVP lassen wir `developer`/`tester` AutoRun weiterhin aus, bis Commits/PRs
  versioniert und sicher persistiert werden. Ein abgeschlossener Requirements-
  Lauf dokumentiert sein Ergebnis im Issue und verschiebt auf `Review`.
- Ohne angemeldeten KI-Anbieter ist die Statusschleife konzeptionell und per
  Simulation testbar; reale KI-Ausfuehrung erfordert einen Anbieter-Login.
- Der GitHub-Scan laeuft alle zwei Minuten, der Dispatcher alle eine Minute;
  keine oeffentlichen Webhooks und kein TLS fuer den lokalen Testbetrieb noetig.
