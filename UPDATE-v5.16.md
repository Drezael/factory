# Factory v5.16 – URL-basierte GitHub Repo-Registry

## Update auf laufende Factory

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.16-registry-urls-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh start
sudo ./factory.sh github-bootstrap
sudo ./factory.sh github-scan
```

Das Updatearchiv enthaelt **keine `.env`**, keine Bind-Mount-Daten und keine
Logins. Datenbank und n8n-Benutzer bleiben erhalten. Der
`github-registry`-Container wird durch den neuen Build aktualisiert.

## Registry ab jetzt nur noch mit URL-Dateien

Ein neues `repos/projekt.repo` enthaelt **nur** eine Zeile:

```text
https://github.com/MEINE-ORG/projekt
```

Loeschst du die Datei, wird das Repo beim naechsten Scan nicht mehr ueberwacht;
keine weiteren Worker-Auftraege daraus. Der n8n-Bootstrap erstellt ausschliesslich
README.md und `repos/.gitkeep` – keine Repo-Dateien.

**Breaking Change:** Alte Registry-YAML-Dateien werden nicht mehr ausgelesen.
Wer bereits YAML-Dateien im privaten Registry-Repository hatte, sollte
pro YAML eine neue `.repo`-Datei mit der GitHub-URL anlegen, bevor die
Automatik wieder aktiviert wird. Individuelle YAML-Regeln entfallen zugunsten
der zentralen, versionierten Factory-Defaults.

Die eigenen Datenbankzustandsdaten bleiben fuer die technische Historie erhalten.
Loescht du einen Repo-Eintrag, wird beim naechsten erfolgreichen Scan der
entsprechende Repo-Status auf nicht ueberwacht gesetzt und seine ausstehenden,
noch nicht gestarteten Ereignisse werden deaktiviert. Kein Reset notwendig.

## GitHub-Zugriffsrechte

- Privates Registry-Repository: Contents **write** fuer den Bootstrap,
  Contents **read** zum Scannen.
- Verwaltete Repositories: Issues, Contents, Pull Requests und Projects wie
  bisher je nach aktivierter Pipeline. `FACTORY_GITHUB_AUTORUN_ENABLED` und
  `FACTORY_GITHUB_AUTOMERGE_ENABLED` bleiben bis zum erfolgreichen Live-Test aus.

## Technische Validierung

Regressionstests fuer URL-Parsing, das explizite Allowlist-Verhalten, leere
Bootstrap-Repositories, Deduplizierung, Datei-Loeschung und die vorherigen
Factory-Komponenten. Kein echter Docker-/GitHub-Live-Test in dieser Umgebung.
