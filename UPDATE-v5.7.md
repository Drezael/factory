# Factory v5.7 - Reset der ausgelagerten Daten

Dieses Update erweitert `factory.sh`. Es veraendert weder das Compose-Deployment
noch die bestehende `.env` oder den automatischen Workflow-Import.

## Installation auf deiner bestehenden Factory

Das UPDATE-Archiv in dem Ordner oberhalb von `factory/` entpacken:

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.7-reset-update.zip
cd factory
chmod +x factory.sh
sudo ./factory.sh repair-env
```

`repair-env` ergaenzt `FACTORY_RESET_ALLOWED_ROOTS=./data` und bewahrt
bestehende Passwoerter. Keine Container muessen fuer das reine Update neu
gestartet werden.

## Vorschau ohne Aenderungen

```bash
sudo ./factory.sh reset-data --dry-run
```

Die Vorschau zeigt exakt, welche Datenverzeichnisse geloescht wuerden.

## Factory zuruecksetzen (Datenverlust!)

```bash
sudo ./factory.sh reset-data
```

Zur Bestaetigung `RESET-FACTORY` eingeben. Anschliessend:

```bash
sudo ./factory.sh start
```

Der Reset stoppt die Compose-Dienste und laufende Agenten, entfernt deren
verwaltete Container und leert die Inhalte dieser .env-konfigurierten Verzeichnisse:

- `FACTORY_POSTGRES_DATA_DIR` - n8n-Datenbank
- `FACTORY_N8N_DATA_DIR` - n8n-Einstellungen und lokale Dateien
- `FACTORY_N8N_SYNC_DATA_DIR` - Workflow-Sync-Status
- `FACTORY_AUTH_DATA_DIR` - Codex/Claude/Gemini-Anmeldungen
- `FACTORY_WORKSPACE_DATA_DIR` - Agent-Arbeitsverzeichnisse

Erhalten bleiben die Verzeichnisse selbst, `.env`, `compose.yaml`,
`factory.sh`, die Dateien in `n8n-workflows/`, Docker-Images und deine
Projektdateien. Die n8n-Administratoren und n8n-Credentials gehen verloren.
Beim naechsten Start ist eine neue n8n-Einrichtung erforderlich.
Der Workflow-Sync importiert danach wieder die vorhandenen JSON-Dateien.

## Sichere Pfadfreigabe

Standardmaessig ist das Loeschen nur innerhalb von `./data` erlaubt:

```ini
FACTORY_RESET_ALLOWED_ROOTS=./data
```

Wenn du Bind-Mounts bewusst auf ein externes Laufwerk gelegt hast, musst du
auch dessen **uebergeordnetes Verzeichnis** explizit ergaenzen, zum Beispiel:

```ini
FACTORY_RESET_ALLOWED_ROOTS=./data:/srv/my-factory-data
```

Eine Datei, ein Symlink, das Factory-Projektverzeichnis, ein gemeinsames
Elternverzeichnis oder sich ueberschneidende Datenpfade werden nicht geloescht.
Die Workflow-Quelle wird niemals geloescht. Wenn die Compose-Konfiguration
ungueltig ist oder Docker das Deployment nicht stoppen kann, wird nichts
geloescht. Bei bereits laufender Loeschung kann ein einzelner
Dateisystemfehler dennoch einen **teilweisen** Reset verursachen.

### Unbeaufsichtigter Reset (nur bewusst einsetzen)

```bash
sudo ./factory.sh reset-data --yes --confirm RESET-FACTORY
```

Ein solcher Aufruf ist unwiderruflich und sollte nur in einer bewusst
wegwerfbaren Testumgebung verwendet werden.
