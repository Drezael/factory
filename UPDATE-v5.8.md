# Update Factory v5.7 -> v5.8

1. Vollstaendiges Backup der bestehenden `.env` und (falls wichtig) von `data/` erstellen.
2. `factory-v5.8-github-registry-update.zip` nach `~/Documents` kopieren.
3. `cd ~/Documents && unzip -o ~/Downloads/factory-v5.8-github-registry-update.zip`
4. `cd factory && sudo ./factory.sh repair-env`
5. Privates GitHub-Repository `OWNER/factory-registry` anlegen, `registry-example/repos/*.yaml` nach `repos/` uebertragen und die Namen korrigieren.
6. In der **bestehenden** `.env` `FACTORY_GITHUB_REGISTRY_REPO`, `FACTORY_GITHUB_REGISTRY_TOKEN`, optional `FACTORY_GITHUB_AUTO_PUBLISH=true` setzen.
7. `sudo ./factory.sh config && sudo ./factory.sh start`
8. `sudo ./factory.sh github-health && sudo ./factory.sh github-scan && sudo ./factory.sh github-events`
9. In n8n die importierten Workflows **Factory 04**, **Factory 05** und **Factory 06** pruefen. Bei deaktiviertem Auto-Publish den Workflow **Factory 04** einmal veroeffentlichen.

**Wichtig:** Beim Entpacken des *Komplettpakets* fuer ein bestehendes Deployment `.env` mit `-x 'factory/.env'` ausschliessen! N8N_ENCRYPTION_KEY und Datenbankpasswort muessen unveraendert bleiben.

Keine neuen oeffentlichen Ports. Die Docker-Worker, Anmeldungen, Reset-Funktionen und der automatische n8n-Workflow-Import bleiben bestehen.

Weitere Informationen: `GITHUB-REGISTRY.md`.
