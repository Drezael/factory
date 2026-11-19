# Update von Factory v5.5 auf v5.6

**Die bestehende `.env` NICHT ueberschreiben.** Ebenso bleiben PostgreSQL,
n8n-Daten, Login-Profile und Agent-Workspaces unangetastet.

1. `factory-v5.6-auto-workflow-import-update.zip` nach `~/Downloads` laden.
2. Das Archiv im Verzeichnis *oberhalb* von `factory` entpacken:

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.6-auto-workflow-import-update.zip
cd factory
chmod +x factory.sh
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
sudo ./factory.sh workflows-sync-status
```

3. n8n unter der gewohnten URL oeffnen, **Overview -> Workflows** aktualisieren.
   Ein manueller Import ist nun nicht mehr notwendig.

**Beim ersten v5.6-Start** werden alle im Bind-Mount vorliegenden Workflow-JSONs
einmal importiert. Wenn die fuenf Factory-Workflows seit v5.5 im n8n-Editor
veraendert wurden, deren Fassungen bitte vorher als JSON sichern.

Falls n8n noch nicht erstmalig eingerichtet wurde, zuerst das Adminkonto
anlegen. Der Importer versucht den Import selbststaendig erneut.

Falls `workflow-sync` Probleme meldet:

```bash
sudo ./factory.sh workflows-check
sudo ./factory.sh workflows-sync-status
```

Der neue Dienst hat **keinen** Docker-Socket und **keinen** Host-Port, sondern
nutzt nur `factory_internal` und eigene Bind-Mounts.
