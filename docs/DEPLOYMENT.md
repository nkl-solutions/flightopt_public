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
3. Stack starten.
4. Einen HTTPS-Reverse-Proxy davor setzen, z.B. Nginx Proxy Manager, Caddy oder
   Traefik.

Die Compose-Datei bindet den App-Port bewusst nur an `127.0.0.1:8000`. Damit
ist die App auf dem VPS selbst erreichbar, aber nicht direkt offen im Internet.
Der Reverse-Proxy reicht die Domain dann intern an `http://127.0.0.1:8000`
weiter.

Fuer einen schnellen Test ohne Proxy kann die Port-Zeile auf `"8000:8000"`
geaendert werden. Das sollte nur kurzfristig passieren, weil Basic Auth ohne
HTTPS den Header nur base64-kodiert, nicht verschluesselt.

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

