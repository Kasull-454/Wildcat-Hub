<p align="center">
  <img src="branding/icon-concept.webp" width="160" alt="Wildcat Hub">
</p>

# Wildcat-Hub

Repository Flatpak self-hosted con interfaccia web.

> [!WARNING]
> **Dichiarazione.** Il software qui presente è pensato a uso strettamente
> personale. È **AI-slop**: è stato scritto in gran parte da un modello
> linguistico, perché mi serviva in tempi brevi un software con le
> caratteristiche che presenta. Non è revisionato, non è supportato e non ha
> garanzie. Se lo usi, lo fai a tuo rischio.

È un fork di [dim145/fdroid-store](https://github.com/dim145/fdroid-store)
(licenza MIT), riadattato da repository F-Droid a repository Flatpak.

## Funzioni

**Repository**
- Remote Flatpak servito come repository OSTree (`archive-z2`).
- Commit e `summary` firmati con una chiave GPG ed25519, generata o importata dalla procedura guidata.
- Dati AppStream generati per GNOME Software e KDE Discover.
- File `.flatpakrepo` per il repository e `.flatpakref` per ogni app.
- Le versioni precedenti restano nel repository e si installano con `flatpak update --commit=…`.
- Delta statici opzionali.

**Visibilità**
- App pubbliche e private.
- Varianti del repository: pubblica, pubblica con contenuti NSFW, una per ogni utente con app private.
- Gli oggetti delle app private stanno in uno store separato per utente e non sono raggiungibili dalle varianti pubbliche.
- Remote privato tramite chiave API inserita nell'URL (`/r/<chiave>/flatpak/…`).
- Modalità privata: catalogo e repository solo per utenti autenticati.

**Import delle app**
- Upload manuale di bundle `.flatpak` dalla GUI.
- Lettura dal bundle di ID, versione, runtime, permessi del sandbox, icona e metadati AppStream (nome, riepilogo, descrizione, licenza, sito).
- Import automatico delle release `.flatpak` da GitHub, GitLab e Gitea/Forgejo, anche self-hosted, con token per i repository privati.
- Build da manifest `flatpak-builder`, preso da un repository Git (branch o tag) o incollato nella GUI.
- Le build girano in un container separato, senza accesso al database né alla chiave di firma.
- Log di build visibile nella GUI e pulsante di ricostruzione forzata.
- Upload dalla CI con deploy token limitati a una singola app.

**Interfaccia**
- Catalogo con ricerca, categorie, screenshot, changelog e traduzioni per lingua.
- Permessi del sandbox raggruppati per tipo, con livello di rischio.
- Comandi `flatpak` da copiare e download di `.flatpakrepo`, `.flatpakref` e `.flatpak`.
- Tema Nord chiaro e scuro con accento lilla-rosa.
- Lingue: italiano, inglese, francese.

**Utenti e sicurezza**
- Account locali, OIDC/SSO, TOTP e passkey (WebAuthn).
- Registrazione aperta, su invito o chiusa.
- Ruoli: utente, publisher, amministratore. Co-manutentori per app.
- Quote per utente: numero di app, spazio, upload mensili.
- Registro di audit, cronologia dei download, statistiche.
- Scansione antivirus con ClamAV e CVE con Trivy, entrambe opzionali.
- Backup e ripristino cifrati (AES-256-GCM) di database, chiave di firma e storage.
- Container in sola lettura, senza capability, con utente non root.

## Componenti

| Servizio   | Ruolo                                                    | Profilo   |
|------------|----------------------------------------------------------|-----------|
| `postgres` | database                                                 | sempre    |
| `redis`    | coda dei job                                             | sempre    |
| `backend`  | API, lettura dei bundle, distribuzione del repository    | sempre    |
| `worker`   | firma e pubblicazione del repository, import, backup     | sempre    |
| `frontend` | GUI e reverse proxy nginx                                | sempre    |
| `builder`  | `flatpak-builder` per i manifest                         | `builder` |
| `clamav`   | antivirus                                                | `clamav`  |
| `trivy`    | scansione CVE                                            | `trivy`   |

## Avvio

```bash
cp .env.example .env            # imposta password, URL e percorsi
docker compose build backend    # il worker e il builder partono da questa immagine
docker compose build
docker compose up -d
docker compose --profile builder up -d   # solo se vuoi le build da manifest
```

Al primo accesso con l'account admin del `.env`, la procedura guidata
genera o importa la chiave GPG. Poi, su ogni macchina:

```bash
flatpak remote-add --if-not-exists wildcat https://<sito>/flatpak/wildcat.flatpakrepo
flatpak install wildcat <app.id>
```

Per le app private, crea una chiave API dal tuo account e aggiungi il remote
`https://<sito>/r/<chiave>/flatpak/wildcat.flatpakrepo`.

## Deploy con le immagini della CI

La GitHub Action [publish-images.yml](.github/workflows/publish-images.yml)
compila le quattro immagini a ogni push su `main` (tag `latest` e
`sha-<commit>`) e a ogni tag `vX.Y.Z` (tag `X.Y.Z` e `X.Y`). I test del
backend girano prima della pubblicazione: se falliscono non viene
pubblicato niente.

Sul server, nel `.env`:

```bash
WILDCAT_IMAGE_PREFIX=ghcr.io/kasull-454/wildcat-hub
WILDCAT_TAG=latest
```

poi:

```bash
docker compose pull
docker compose up -d
docker compose --profile builder up -d   # se usi le build da manifest
```

Se i pacchetti su GHCR sono privati, prima fai `docker login ghcr.io` con un
token che abbia lo scope `read:packages`.

## Aggiornamenti e What's Up Docker

Nel `docker-compose.yml` ogni servizio ha le label `wud.*` commentate.
In sintesi:

- `backend`, `worker`, `builder` e `frontend` sono costruiti in locale: `wud.watch=false`. Se usi le immagini GHCR, vanno aggiornati insieme e allo stesso tag.
- `postgres` resta sulla major 16: passare a un'altra major richiede dump e restore.
- `redis` e `clamav` seguono solo il digest del loro tag.
- `trivy` non va aggiornato da solo: la versione del server deve coincidere con quella del client nell'immagine worker (`TRIVY_IMAGE` in `backend/Dockerfile.worker`).

## Note su mergerfs

OSTree scrive ogni oggetto in `repo/tmp` e poi lo rinomina dentro
`repo/objects/`. Se le due cartelle finiscono su dischi diversi del pool, il
rename fallisce (`EXDEV`). Tieni `WILDCAT_STORAGE` su un solo ramo (policy
`category.create=epmfs` o `epff`, cartella creata su un solo disco) oppure
monta direttamente il disco sottostante. La cache del builder usa molti
hardlink: `WILDCAT_DATA` va fuori da mergerfs.

## Limiti noti

- Due build della stessa app create nello stesso secondo entrano in conflitto: la versione interna è il timestamp del commit.
- L'accesso privato passa per una chiave nell'URL del remote: chi ha l'URL vede le app private.
- Alcune pagine di amministrazione ereditate dall'upstream (proxy esterni, categorie "ufficiali", anti-feature) sono pensate per F-Droid.
- Solo app: i runtime arrivano da Flathub o dal repository indicato in `FLATPAK_RUNTIME_REPO`.

## Licenza

MIT, come il progetto originale. Vedi [LICENSE](LICENSE).
