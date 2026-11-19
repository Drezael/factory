# GitHub Registry: URL-only allowlist (Factory v5.16)

Du gibst der Factory ein **leeres, privates** GitHub-Repository an.
Beim ersten n8n-Zyklus erstellt `POST /bootstrap` automatisch `README.md` und `repos/.gitkeep`.

**Neues Projekt:** In diesem privaten Registry-Repository `repos/projekt.repo` anlegen.
Der **gesamte Dateiinhalt** ist genau eine GitHub-URL, z. B.:

```text
https://github.com/DEINE-ORG/dein-projekt
```

**Projekt entfernen:** Datei loeschen und committen. Nach dem naechsten erfolgreichen
Scan startet die Factory keine neuen Jobs fuer das Repo. Bereits laufende Worker
koennen ihren aktuellen Auftrag noch abschliessen. Existierende GitHub-Issues,
PRs und Boards werden nicht geloescht.

Dateinamen muessen eindeutig sein. `.repo` ist die einzige eingelesene Dateiendung.
Andere Dateien, insbesondere alte `*.yaml`, werden ignoriert.
Es gibt **keine automatische Repo-Entdeckung** und kein Zurueckschreiben
von Repo-Eintraegen durch die Factory.

Die Beispiele im Ordner `examples/` sind Platzhalter. Nicht unveraendert in
`repos/` kopieren. Passe den Link auf ein Repository an, das dein GitHub-Token
lesen und verwalten darf.
