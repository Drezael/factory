# Update auf Factory v5.9 – GitHub-Issue-Status-gesteuerte Worker

Bestehende Factory v5.8 bleibt bestehen, PostgreSQL-/n8n-Bind-Mounts und Login-Profile
werden nicht geloescht. Das Update-ZIP enthaelt **keine `.env`**.

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.9-status-driven-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
sudo ./factory.sh github-health
sudo ./factory.sh github-scan
```

Die YAML-Dateien liegen in deinem **separaten** privaten Registry-Repository.
Entsprechend `GITHUB-REGISTRY.md` jede YAML auf `kanban.enabled: true`,
`issues.required_labels: []` und die sieben Status-Spalten umstellen.
Vorher im GitHub-Project die sieben Statusoptionen anlegen.

`FACTORY_GITHUB_AUTORUN_ENABLED=false` belassen, solange keine KI-Logins und
keine Freigaben vorliegen. Neuen Runner-Workflow 07 importiert `workflow-sync`
automatisch. Er bleibt unveroeffentlicht, solange AutoRun global deaktiviert ist.

**Datenreset**: `reset-data` aendert GitHub-Board-Status und Issue-Kommentare nicht.
Ein Issue in `In Arbeit` muss ggf. nach Pruefung laufender Worker manuell
auf `Bereit` zurueckgesetzt werden.

Zur Installation einer NEUEN Factory das Komplettpaket verwenden, seine `.env`
konfigurieren und erst danach die GitHub-Registry anbinden.
