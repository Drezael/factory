# Factory v5.5 – Workflows im n8n-Editor sichtbar machen

## Problem

Der Workflow-Ordner ist schon in v5.4 nach
`/home/node/.n8n-files/workflows` gemountet. Ein Bind-Mount legt jedoch
**keine** Workflows in der n8n-Datenbank an. Darum sind die JSON-Dateien
im Container lesbar, aber nicht automatisch in der Weboberflaeche zu finden.

## Update einer bestehenden Factory (empfohlen)

Archiv in `~/Documents` entpacken und dabei **keine alte `.env` ueberschreiben**:

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.5-workflow-import-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh workflows-check
sudo ./factory.sh workflows-import
```

Kein Compose-Neustart und keine Docker-Image-Updates noetig.

In n8n anschliessend **Overview -> Workflows** oeffnen und Seite aktualisieren.
Die Workflows erscheinen im persoenlichen Projekt des Instanzbesitzers und
sind zunaechst unveroeffentlichte Entwuerfe.

## Wichtige Hinweise

- `workflows-check` = Bind-Mount und JSON lesbar; `workflows-import` = in n8n-Datenbank registrieren.
- n8n-Admin-Konto muss vorher angelegt sein.
- Feste Workflow-IDs verhindern Duplikate, **aber erneuter Import ersetzt
  die entsprechenden Workflow-Entwuerfe in der n8n-Datenbank**. Vor dem
  Reimport eigene Aenderungen aus der n8n-Oberflaeche sichern.
- Wurden die Dateien zuvor manuell ohne feste IDs importiert, koennen bereits
  existierende Kopien mit anderen IDs auftreten. Diese bei Bedarf in der
  n8n-Oberflaeche entfernen.
- Bei einer Mehrbenutzer-Instanz optional `FACTORY_N8N_IMPORT_PROJECT_ID`
  in `.env` auf eine Projekt-ID setzen (Standard: `owner`).
- KI-Anmeldungen sind nicht noetig, um die Workflows zu importieren. Die
  Header-Auth-Credential fuer den Agent-Manager muss in n8n weiterhin
  manuell ausgewaehlt werden.

## Passen die Dateien im Container?

```bash
sudo ./factory.sh workflows-check
```

Sollte hier eine Fehlermeldung kommen, den Mount separat pruefen:

```bash
sudo docker compose exec -T n8n ls -la /home/node/.n8n-files/workflows
```

Wird der Import nicht angenommen, den eigentlichen Fehler im Terminal
beachten. Es ist kein SSL-Zertifikat und kein Modell-API-Key erforderlich.
