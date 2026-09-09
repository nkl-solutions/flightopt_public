# Zwei Bauwege aus einer Datei, ausgewaehlt ueber `FLIGHTOPT_VARIANT`.
#
# `lean` ist der Standard und bleibt, was das Image immer war: Python, die
# Kernabhaengigkeiten, die Anwendung. `hotels` legt Playwright und einen
# Chromium dazu, damit die Booking-Quelle auf dem VPS ueberhaupt laufen kann.
#
# Warum ein Image mit zwei Abschnitten und nicht zwei getrennte Images: die
# Anwendung ist ein Prozess und eine SQLite-Datei. Zwei Container gegen
# dasselbe Volume waeren zwei Schreiber auf derselben Datenbank, und das ist
# kein Betriebsmodell, sondern eine Wette. Ein zweites Image mit eigener Kopie
# der Anwendung waere ausserdem eine zweite Wahrheit, die auseinanderlaeuft.
#
# Warum der Standard klein bleibt: ein Chromium samt Systembibliotheken wiegt
# mehrere hundert Megabyte. Wer ihn nicht braucht, soll ihn nicht bezahlen -
# und wer ihn einschaltet, soll es bewusst tun und dabei `mem_limit` mitgeben.
ARG FLIGHTOPT_VARIANT=lean

FROM python:3.13-slim AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

# --- der schlanke Weg ------------------------------------------------------

FROM base AS lean-env

RUN uv sync --frozen --no-dev

# --- der Weg mit Browser ---------------------------------------------------

FROM base AS hotels-env

# Die Browser liegen ausserhalb von /root, damit ein spaeterer Wechsel auf
# einen anderen Benutzer sie nicht verliert und `du` sie an einer Stelle
# findet.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# Die Umgebung ist im Bauabschnitt fertig, also wird beim Start nichts mehr
# abgeglichen: das spart Zeit und nimmt `uv run` jede Gelegenheit, an einer
# Gruppe zu drehen, die nicht zu den Standardgruppen gehoert - und `hotels`
# ist keine davon. Nachgemessen mit uv in dieser Fassung: `uv run --frozen`
# baut Playwright *nicht* aus. Verlassen wollen wir uns darauf trotzdem
# nicht, dafuer haengt zu viel daran.
ENV UV_NO_SYNC=1

# Der Schalter gehoert ins Image und nicht in die Stack-Variablen: ein Image
# mit Browser und ohne Schalter waere ein Image ohne Zweck.
ENV FLIGHTOPT_HOTELS_BOOKING=1

# Im Container laeuft alles als root, und ein Chromium startet als root nur
# ohne seine eigene Sandbox. Die Grenze ist hier der Container, nicht der
# Browser-Prozess. `--disable-dev-shm-usage` haelt Chromium von /dev/shm fern,
# das in Docker standardmaessig 64 MB gross ist und sonst mitten in einer
# Seite ausgeht. Die Heap-Obergrenze ist das Gegenstueck zu NODE_OPTIONS: sie
# deckelt den Renderer, bevor `mem_limit` den ganzen Container abraeumt.
ENV FLIGHTOPT_HOTELS_BROWSER_ARGS="--no-sandbox,--disable-dev-shm-usage,--disable-gpu,--js-flags=--max-old-space-size=256"

RUN uv sync --frozen --no-dev --group hotels \
    && uv run --frozen playwright install --with-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache/uv

# --- gemeinsamer Abschluss -------------------------------------------------
#
# Die Anwendung wird zuletzt kopiert, und zwar in beiden Faellen. Eine
# Codeaenderung soll die Browser-Schicht nicht ungueltig machen; sonst faellt
# bei jedem Push ein halbes Gigabyte Download neu an.
FROM ${FLIGHTOPT_VARIANT}-env AS runtime

COPY flightopt ./flightopt

EXPOSE 8000

CMD ["uv", "run", "--frozen", "uvicorn", "flightopt.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
