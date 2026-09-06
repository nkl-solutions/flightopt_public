# Deployment

Ziel fuer den ersten VPS-Schritt: flightopt laeuft als einzelner Container
auf dem Portainer-VPS, speichert SQLite in einem Volume und ist per Basic Auth
geschuetzt. Das ist gut genug fuer private Handy-Nutzung, solange HTTPS davor
liegt.

## Portainer

1. In Portainer einen neuen Stack aus `docker-compose.portainer.yml` anlegen.
2. Unter Environment variables setzen:
   - `FLIGHTOPT_BASIC_USER`
   - `FLIGHTOPT_BASIC_PASSWORD`
   - `OPENAI_API_KEY` optional leer lassen, bis der KI-Modus echte
     Modellaufrufe nutzt.
3. Stack starten.
4. Einen HTTPS-Reverse-Proxy davor setzen, z.B. Nginx Proxy Manager, Caddy oder
   Traefik.

Die Compose-Datei veroeffentlicht `8000:8000`, damit Nginx Proxy Manager auf
demselben VPS den Dienst ohne gemeinsames Docker-Netz erreichen kann. Bis eine
Domain eingerichtet ist, sollte der Zugriff mindestens durch Basic Auth
geschuetzt bleiben.

Sobald eine Domain existiert, eignet sich in Nginx Proxy Manager:

- Domain: `flightopt.nkl-solutions.de`
- Scheme: `http`
- Forward Hostname / IP: VPS-IP oder Host-Gateway
- Forward Port: `8000`
- SSL: Let's Encrypt, Force SSL aktivieren

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
