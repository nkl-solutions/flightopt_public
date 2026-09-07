# Deployment

Ziel fuer den ersten VPS-Schritt: flightopt laeuft als einzelner Container
auf dem Portainer-VPS, speichert SQLite in einem Volume und ist per Basic Auth
geschuetzt. Das ist gut genug fuer private Handy-Nutzung, solange HTTPS davor
liegt.

## Portainer

Der Stack baut aus dem oeffentlichen Repo
`https://github.com/nkl-solutions/flightopt_public.git`, nicht aus der privaten
Arbeitsfassung. Was dort nicht ankommt, existiert auf dem VPS nicht;
uebertragen wird mit `scripts/sync_public.py`.

1. In Portainer einen neuen Stack aus `docker-compose.portainer.yml` anlegen.
2. Unter Environment variables setzen:
   - `FLIGHTOPT_BASIC_USER`
   - `FLIGHTOPT_BASIC_PASSWORD`
   - `OPENAI_API_KEY` optional leer lassen, bis der KI-Modus echte
     Modellaufrufe nutzt.
   - `SERPAPI_KEY` optional. Ohne Wert bleibt SerpApi aus dem Quellenkatalog
     und die Suche laeuft ohne diese Verify-Schicht.
   - `SERPAPI_MONTHLY_CAP` optional, Standard 200. Deckelt die bezahlten
     Aufrufe je Kalendermonat; der Free Tier liegt bei 250.
3. Stack starten.
4. Einen HTTPS-Reverse-Proxy davor setzen, z.B. Nginx Proxy Manager, Caddy oder
   Traefik.

Die Namen stehen ohne Werte in `deploy/portainer.env.example`. Schluessel
gehoeren ausschliesslich in die Stack-Variablen von Portainer, nie ins Repo.

## Neu ausrollen

1. Im privaten Repo `uv run python scripts/sync_public.py` laufen lassen und
   den Secret-Scan gruen sehen.
2. Im oeffentlichen Repo committen und pushen. Push nur nach Freigabe.
3. In Portainer beim Stack `flightopt` "Pull and redeploy" ausloesen. Portainer
   holt den neuen Stand aus dem Repo und baut das Image neu.
4. Danach `/api/health` pruefen. Der Endpunkt bleibt absichtlich ohne Basic
   Auth erreichbar.

Geaenderte Environment-Variablen wirken erst nach einem Redeploy.

Die Compose-Datei haengt den Container an das vorhandene Docker-Netz
`proxy-network`. Damit kann Nginx Proxy Manager den Dienst intern erreichen,
ohne einen Host-Port oeffentlich freizugeben.

Sobald eine Domain existiert, eignet sich in Nginx Proxy Manager:

- Domain: `flightopt.nkl-solutions.de`
- Scheme: `http`
- Forward Hostname / IP: `flightopt`
- Forward Port: `8000`
- SSL: Let's Encrypt, Force SSL aktivieren

Stand 2026-09-06: Portainer-Stack und Nginx Proxy Manager sind eingerichtet.
NPM zeigt `flightopt.nkl-solutions.de -> http://flightopt:8000` als online,
mit Let's Encrypt, Force SSL und HTTP/2. Der direkte Test gegen die VPS-IP
liefert erwartungsgemaess Basic Auth. Nach DNS-Propagation zeigt die normale
Domain oeffentlich auf `159.195.64.156`; HTTP leitet auf HTTPS weiter, HTTPS
liefert Basic Auth.

Falls die normale Domain noch nicht erreichbar ist, aber dieser Test klappt,
ist NPM korrekt und nur DNS noch nicht durch:

```bash
curl --resolve flightopt.nkl-solutions.de:443:159.195.64.156 -I https://flightopt.nkl-solutions.de/
```

Langfristig kann Flightopt spaeter unter einer Deal-Seite als `/flights` oder
eigener Subdomain laufen. Fuer den ersten privaten Handy-Zugriff ist
`flightopt.nkl-solutions.de` am klarsten.

## Ressourcen

Der Stack setzt:

- `mem_limit: 512m`
- `cpus: "1.0"`
- `MALLOC_ARENA_MAX=2`

Python hat keine portable Heap-Grenze wie `NODE_OPTIONS` oder `GOMEMLIMIT`.
Die harte Grenze ist deshalb das Docker-/cgroup-Limit; ein einzelner Uvicorn-
Prozess bleibt bewusst schlicht.

## Healthcheck

`/api/health` bleibt ohne Basic Auth erreichbar, damit Docker den Container
einfach pruefen kann. Die Oberflaeche und alle Such-Endpunkte sind geschuetzt,
sobald User und Passwort gesetzt sind.
