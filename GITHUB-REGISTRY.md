# Factory v5.16 – Private GitHub-Registry als URL-Dateiliste

## Grundregel

**Nur das private Registry-Repository musst du selbst anlegen.** Danach reichen
GitHub-Repository-URL-Dateien als ausdrueckliche Allowlist. Es findet **keine
Suche nach weiteren Repositories** statt. Kein KI-Worker ist fuer Bootstrap,
Board-Anlage oder GitHub-Scanning noetig: n8n ruft den internen GitHub-Adapter auf.

```
factory-registry (private)
├── README.md                     # automatisch von n8n erzeugt
└── repos/
    ├── .gitkeep                  # automatisch von n8n erzeugt
    ├── backend.repo              # manuell angelegt, eine URL je Datei
    └── website.repo              # manuell angelegt, eine URL je Datei
```

Inhalt von `repos/backend.repo`:

```text
https://github.com/organisation/backend
```

Mehr ist nicht erforderlich: keine YAML-Felder, kein `enabled`, keine Tokens.
Die Factory ermittelt die Basisinformationen (Owner, Branch, Project-Owner)
aus der GitHub-API und verwendet zentrale Standardregeln:
- Eigenes Projects-v2-Board je Repository, alle Statuskategorien inklusive
  Entwicklung, Bug, Feature, Planung, Bereit, In Arbeit, Rueckfrage, Review,
  Erledigt und Fehler
- Die drei Issue-Formulare (Entwicklung > Bug > Feature)
- Pflicht-Planung vor allen Entwicklungs-/Bug-/Feature-Auftraegen
- Einmalige Worker-Container fuer Planung, Entwicklung und Review
- Rueckfragen nur im urspruenglichen Issue; neuer Worker erst nach manuellem
  Statuswechsel von `Rueckfrage` auf `Bereit`
- Modell-Logins und Quota-Gate wie bisher; AutoRun und AutoMerge bleiben in
  `.env` zunaechst **ausgeschaltet**

Die nicht vom User gelieferten Einstellungen stammen aus den versionierten
Factory-Defaults (`github-registry/bootstrap.py`), nicht aus weiteren Dateien
im Registry-Repository. Historische `*.yaml`-Repositoryeintraege werden von
v5.16 **nicht mehr eingelesen**. Bestehende YAML-Konfigurationen muessen bei
Bedarf in URL-Dateien umgewandelt werden.

## Einrichten

1. Ein **leeres, privates** GitHub-Repository, z. B.
   `organisation/factory-registry`, anlegen. Keine README erforderlich.
2. GitHub-Token in der lokalen `.env` hinterlegen. Er braucht `Contents: write`
   im Registry-Repository (erste README und Platzhalterdatei) und geeignete
   Issues-/Projects-/Content-/PR-Rechte fuer die ueberwachten Repos.
3. In `.env`:

   ```ini
   FACTORY_GITHUB_REGISTRY_REPO=organisation/factory-registry
   FACTORY_GITHUB_REGISTRY_REF=auto
   FACTORY_GITHUB_REGISTRY_DIR=repos
   FACTORY_GITHUB_REGISTRY_TOKEN=github_pat_...
   FACTORY_GITHUB_AUTO_PUBLISH=true
   FACTORY_GITHUB_AUTORUN_ENABLED=false
   FACTORY_GITHUB_AUTOMERGE_ENABLED=false
   ```

4. `sudo ./factory.sh start` ausfuehren. Workflow 04 initialisiert zuerst die
   leere Registry und scannt dann alle vorhandenen `.repo`-Dateien. Manuell:
   `sudo ./factory.sh github-bootstrap`, `github-boards-ensure`, `github-scan`.
5. Ein Repo durch eine Datei `repos/<name>.repo` mit genau einer
   `https://github.com/OWNER/REPO`-URL aufnehmen und committen.

## Entfernen / Deaktivieren

Datei `repos/<name>.repo` loeschen und committen. Beim naechsten erfolgreichen
n8n-Scan (normalerweise ca. zwei Minuten) wird das Repo aus der Allowlist
entfernt. Der Adapter setzt ausstehende, **noch nicht gestartete** technische
Ereignisse dieses Repos auf erledigt. Der Job-Dispatcher liest die Allowlist
vor jeder neuen Beanspruchung; ein entferntes Repo kann nicht mehr neu
beansprucht werden. Bereits laufende Agent-Container werden nicht erzwungen
abgebrochen. Die GitHub-Daten (Board, Issues, PRs, Branches) bleiben erhalten.

**Wieder aufnehmen:** Neue `.repo`-Datei mit derselben URL anlegen. Beim
naechsten Scan wird das Repository wieder registriert.

## Fehlerverhalten und Sicherheit

- Die Registry ist eine Allowlist. Ungueltige URLs, doppelte Repositories,
  URLs zu anderen Hosts sowie private Repositories ohne ausreichenden Zugriff
  fuehren zu einem **sichtbaren Scan-Fehler**. Ein fehlerhafter Scan entfernt
  keine bestehende Konfiguration.
- Keine automatische Wiederherstellung geloeschter `.repo`-Dateien, kein
  Discovery-Endpoint, keine Terraform-/GitHub-Suche nach verfuegbaren Repos.
- Der GitHub-Zugriffstoken bleibt ausschliesslich im `github-registry`-Service.
  Die KI-Worker erhalten ihn nicht.
- Der Adapter behaelt das bisherige interne Netz `factory_internal`; Internet
  erreicht GitHub nur ueber das getrennte Egress-Netz.
- Offene Pull Requests oder bereits laufende Worker muessen ggf. manuell
  beendet werden, wenn eine sofortige Unterbrechung gewuenscht ist.

## Zentrale Defaults in `.env`

Da die `.repo`-Dateien ausschliesslich URLs enthalten, stellst du das
Verhalten fuer **alle** Repositories zentral ein:

```ini
FACTORY_GITHUB_DEFAULT_PLANNER_PROVIDER=codex
FACTORY_GITHUB_DEFAULT_DEVELOPER_PROVIDER=codex
FACTORY_GITHUB_DEFAULT_REVIEWER_PROVIDER=claude
FACTORY_GITHUB_DEFAULT_TIMEOUT_SEC=900
FACTORY_GITHUB_DEFAULT_MAX_REVIEW_CYCLES=3
FACTORY_GITHUB_DEFAULT_MAX_PLAN_TASKS=40
FACTORY_GITHUB_DEFAULT_AUTO_PLAN_ON_CREATE=true
FACTORY_GITHUB_DEFAULT_AUTO_INSTALL_FORMS=true
FACTORY_GITHUB_DEFAULT_REQUIRED_CHECKS=
FACTORY_GITHUB_DEFAULT_AUTO_MERGE=false
```

Automatischen Merge nur aktivieren, wenn du zusaetzlich die Namen der
notwendigen GitHub-CI-Checks in `FACTORY_GITHUB_DEFAULT_REQUIRED_CHECKS`
(kommagetrennt) gesetzt und die Branch-Protection konfiguriert hast.
`FACTORY_GITHUB_AUTOMERGE_ENABLED=true` aktiviert erst dann den zentralen
Merge-Workflow. Ein Merge ohne erforderliche Checks bleibt gesperrt.

**Hinweis:** Ein neuer Repo-Eintrag alleine startet niemals ein Modell,
solange `FACTORY_GITHUB_AUTORUN_ENABLED=false` ist. Die URL-Allowlist
berechtigt die Factory jedoch, Boards und Issue-Vorlagen einzurichten.
