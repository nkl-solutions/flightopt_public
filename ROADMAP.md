# flightopt Roadmap

Stand: 5. Oktober 2026. Dieser Plan beschreibt den aktuellen Funktionsstand
und die nächsten Entwicklungsschritte. Die Reihenfolge richtet sich nach
verlässlichen Preisen, Multistop-Nutzen und einem stabilen täglichen Betrieb.
Er ist kein Versprechen eines Fertigstellungstermins.

## Ziel

Flexible Multistop-Reisen über ein Datumsfenster optimieren, die besten
Kandidaten mit echten Angeboten prüfen und den Gesamtpreis nachvollziehbar
anzeigen. Später wird der Flugbereich Teil einer Deal-Anwendung mit Hotels.

Ein Preis aus einem Kalender oder Vergleichsportal bleibt ein Kandidat.
Eine vollständig geprüfte Flugroute braucht geprüfte Angebote für jede
Teilstrecke. Ein Hotel-Richtwert macht eine Gesamtreise nicht buchungsbestätigt.

## Bereits umgesetzt

- [x] Datumsgraph und dynamische Programmierung statt einer Abfrage je Reise.
- [x] Hinflug, Hin und zurück sowie feste Routen mit mehreren Stopps.
- [x] Individuelle Aufenthaltsdauer je Stopp und flexible Datumsfenster.
- [x] Weltweite Flughäfen, deutsche Aliasse, Metro- und Ländergruppen.
- [x] Variantenbudget für Airport-Gruppen.
- [x] Parallele Kalenderabfragen, gemeinsame Quellentaktung und TTL-Cache.
- [x] Live-Prüfung des Kandidatenpools, soweit Quellen Tagessuchen anbieten.
- [x] Airline-Registry, Adapter und aufgezeichnete Antwort-Fixtures.
- [x] Kiwi als Richtwert; Google-Flights-Daten optional über SerpApi mit Budget.
- [x] EZB-Umrechnung mit sichtbarem Originalpreis.
- [x] Quellenstatus, Fortschritt, Zwischenresultate und Suchabbruch.
- [x] Ergebnisfilter und Sortierung ohne erneute Suche.
- [x] Getrennte Gruppen für geprüfte Routen und Kandidaten.
- [x] Favorit aus geprüften Flugpreisen; mobile Strecke-/Datumsanzeige.
- [x] Tab-Vervollständigung ab einem Buchstaben und Schutz vor alten Antworten.
- [x] Getrennte Ansichten für Flugsuche, Hotels, gespeicherte Suchen und Preisradar.
- [x] Sichtbare Routenlabels, kompakte Ergebnisliste und kurze Statusmeldungen.
- [x] Lokale Satz-Eingabe als Eingabeschicht über der normalen Suche.
- [x] Browser-Spracheingabe mit anschließender Bestätigung.
- [x] Gespeicherte Flugprofile, Scheduler und eindeutige Profil-Job-Zuordnung.
- [x] Profilverwaltung: Name, Scanrhythmus, Pausieren und gezielter Einzelstart.
- [x] Ablaufprüfung fester Profilfenster und Schutz vor doppelten laufenden Jobs.
- [x] Rollende Strecken-Watchlist und tägliche Preisbeobachtung.
- [x] Optionaler 20-Minuten-Takt mit Quellenbudgets und Cache-Frische.
- [x] Getrennte Preisbaselines, Ausreißer- und Fehltarif-Erkennung.
- [x] Persistierte Teilstrecken-Funde, Entdopplung, Ruhezeit und Discord-Versand.
- [x] Preisverläufe und Abhaken von Funden in der Oberfläche.
- [x] Hotelsuche über Trivago MCP und optional Booking/Playwright.
- [x] Gespeicherte Hotelbeobachtungen, Tagesplaner und Preisfehler-Signale.
- [x] Hotelüberwachung in der UI: speichern, pausieren und fällige Suchen prüfen.
- [x] Neue Hotelbeobachtungen explizit je Watch und erfolgreichem Scan zuordnen.
- [x] Sichtbare sichere Buchungslinks je Flug-Teilstrecke und Hotelangebot.
- [x] Fehlende Buchungslinks ausdrücklich markieren, keine Gesamtbuchung erfinden.
- [x] Getrennte Flug-/Hotel-Scheduler-Bahnen, Sammler gegen Überlappung gesperrt.
- [x] Fester Scrollbar-Platz und kein automatischer Scrollsprung bei Suchabschluss.
- [x] Optionale Gesamtreise mit Flug- und Hotelanteil; fehlende Summe bleibt offen.
- [x] Docker-Bauwege `lean` und `hotels`, Basic Auth, Healthcheck und SQLite-Volume.
- [x] Datenbank-Backup, additive Migrationen, Tests und GitHub CI.
- [x] Public-/Private-Sync mit Ausschlüssen und Secret-Scan.

Diese Liste bedeutet, dass Funktionen im Code vorhanden und getestet sind.
Sie bestätigt keine heutige Erreichbarkeit jeder Airline oder einen bestimmten
Stand des laufenden VPS-Containers.

## P0: Verlässlicher Betrieb und Preisvertrauen

Vor weiterer Funktionsbreite abschließen oder dauerhaft prüfen.

- [ ] Beide Docker-Bauwege auf einem Docker-Wirt bauen und starten.
  Abschluss: Healthcheck, Basic Auth, SQLite-Persistenz und Neustart geprüft;
  Speicher unter Suchlast gemessen, Container- und Browsergrenzen eingehalten.
- [ ] Aktuellen Public-Stand in Portainer ausrollen und extern prüfen.
  Abschluss: ausgelieferte Asset-Version stimmt; Login, Suche, Ereignisstrom
  und Handyansicht funktionieren. Redeploy als eigener Betriebsschritt.
- [ ] Regelmäßiges VPS-Backup inklusive Wiederherstellungstest etablieren.
  Abschluss: Wiederherstellung auf separater Testdatenbank ohne Datenverlust.
- [ ] Laufzeit-Quellenstatus mit Messzeitpunkt und Fehlergrund verbessern.
  Abschluss: „kein Tarif“, „nicht abgefragt“ und „Quelle gestört“ unterscheidbar.
- [ ] Buchungslink-Abdeckung je Adapter mit realen Antworten prüfen.
  Abschluss: die besten geprüften Routen haben für jede Teilstrecke einen
  passenden Link; fehlende oder allgemeine Links bleiben eine sichtbare Lücke.
- [x] Hotel-Historie eindeutig der gespeicherten Überwachung zuordnen.
  Abschluss: Bereitschaft und Beobachtungszahlen je Ziel/Filter sind nicht
  aus der gemeinsamen Historie gleicher Belegung abgeleitet. Altbestand bleibt
  unzugeordnet; fehlgeschlagene und abgebrochene Scans zählen nicht zur Bereitschaft.
- [ ] Gepäck- und Zuschlagsangaben je Ergebnis konsistent prüfen.
  Abschluss: Flugsumme und sichtbare Teilpreise passen; bekannte Zusatzkosten
  und nicht abgefragte Leistungen sind eindeutig benannt.
- [x] Vergangene Suchfenster gespeicherter Profile behandeln.
  Abschluss: abgelaufene Profile starten keinen sinnlosen Dauerscan;
  Mindestaufenthalte werden berücksichtigt, vergangene Tage nicht abgefragt.
  Das gespeicherte feste Fenster bleibt unverändert. Rollende Flugprofile sind separat offen.
- [ ] Wiederverbindung und Wiederaufnahme von Suchläufen im Browser prüfen.
  Abschluss: Neuladen oder Netzverlust führt nicht zu einer doppelten Suche,
  und vorhandene Ergebnisse lassen sich wieder öffnen.

## P1: Multistop-Mehrwert

Der nächste Funktionsschritt ist ein Preiszielalarm für vollständige Reisen.

- [ ] Profil-Preisziel in Euro eingeben, intern in Cent speichern.
- [ ] Nach abgeschlossenem Profiljob die billigste vollständig geprüfte,
  nicht-indikative Flugroute gegen das Ziel vergleichen.
- [ ] Alarm mit Profil, Route, Datumskette, Währung, Preis und Links speichern.
- [ ] Wiederholungen entdoppeln und relevante Preisrückgänge berücksichtigen.
- [ ] Zunächst lokal als Trockenlauf anzeigen, anschließend Discord optional
  anschließen. Die Prüfung verwendet fertige Jobs ohne weitere Preisabrufe.

Abschluss: Schätzungen, Richtwerte, abgebrochene Jobs und Teilprüfungen lösen
keinen bestätigten Reisealarm aus. Identische Profile bleiben getrennt.
Budgetgrenzen, Wiederholungen und Neustarts sind durch Tests abgedeckt.

Danach:

- [ ] Top-10-Prüfung als nachvollziehbares Ergebnis ausweisen: geprüft,
  preislich verändert, nicht verfügbar oder Quelle ohne Tagessuche.
- [ ] Zeit- und Umstiegspräferenzen sowie sinnvolle Mindestanschlusszeiten
  ergänzen, soweit die Quellen konkrete Segmente liefern.
- [ ] Flughafenwechsel innerhalb einer Stadt sichtbar machen; unerlaubte
  Wechsel aus einer Datumsfolge ausschließen.
- [ ] Ähnliche Datumsketten gruppieren, ohne die günstigste zu verlieren.
- [ ] Route teilen oder exportieren: Datum, Preisstatus und Prüfaltersangabe
  mitgeben, persönliche Daten und Zugangsschlüssel auslassen.

## P1: Schnellere Suche

Die Engine arbeitet bereits parallel. Zusätzliche Nebenläufigkeit muss sich
an Quellenlimits orientieren und mit Messungen begründet werden.

- [ ] Referenzsuchen festlegen: Kurzstrecke, drei Legs, Airport-Gruppe,
  internationales Fenster; kalt und mit Cache messen.
- [ ] Dauer je Phase, Quellenaufrufe, Cache-Treffer und erste sichtbare Route
  erfassen. Keine API-Schlüssel oder vollständigen Antwort-Dumps protokollieren.
- [ ] Identische Abfragen gleichzeitig laufender Jobs zusammenfassen.
- [ ] Globale Ressourcen- und Nebenläufigkeitsgrenzen über Jobs prüfen.
- [ ] Kandidatenprüfung priorisieren und Abbruch schneller wirksam machen.
- [ ] Suchraumgröße mit brauchbarer Zeitabschätzung zeigen, ohne erfundene
  Prozentwerte oder eine garantierte Dauer.

Abschluss: gleiche oder bessere geprüfte Ergebnisse bei geringerer gemessener
Wartezeit; keine zusätzlichen Sperren oder unkontrollierten API-Kosten.

## P2: Weitere Quellen

- [ ] TAP und Volotea anhand echter Browser-Anfragen erneut untersuchen.
- [ ] Für Lücken wie Deutschland–Türkei/Griechenland weitere einzelne
  Airline-Spikes priorisieren; jeden Befund mit Datum und Antwort belegen.
- [ ] SerpApi-Airlinefilter bis zur API durchreichen.
- [ ] Offizielle Partnerzugänge für Skyscanner und Kiwi separat bewerten.
- [ ] PanFlights, Google Flights, ITA Matrix und ähnliche Werkzeuge nach
  Rolle einordnen: UX-Referenz, manueller Vergleich oder nutzbare Datenquelle.
  Eine sichtbare Webseite ist noch keine integrierbare Preis-API.

Abschluss je Adapter: belegter Endpunkt, Währung, Datumssemantik, Gepäck,
Fehlerfälle, höfliche Taktung, gespeicherte Fixture und Regressionstests.
Ein gesperrter Zugang zählt als dokumentierter Befund, nicht als Integration.

## P2: KI- und Spracheingabe

- [ ] Komplexe Reisewünsche in eine strukturierte Suchanfrage übersetzen.
- [ ] Route, Zeitfenster, Aufenthalte, Gruppen, Gepäck und Umstiege validieren.
- [ ] Unsichere oder fehlende Angaben als bearbeitbare Vorschau darstellen.
- [ ] Optionalen OpenAI-Zugang serverseitig anschließen: Timeout, begrenzte
  Eingabelänge, konfigurierbares Modell und Anfragelimit.
- [ ] Lokalen Parser als Fallback erhalten; Modellantworten enthalten keine
  erfundenen Flugpreise und starten die Suche erst nach Bestätigung.
- [ ] Sprachunterstützung auf Android und iOS prüfen; für Browser ohne
  Erkennung bleibt Texteingabe verfügbar.
- [ ] Kleinere Modelle anhand derselben Beispielsätze gegen Qualität,
  Latenz und Kosten vergleichen.

Eine Vektordatenbank ist für Preisoptimierung und Airport-Lookup derzeit
nicht erforderlich. Erst bei einer konkreten semantischen Inhaltssuche prüfen,
zum Beispiel bei umfangreichen Reisebeschreibungen oder Unterkunftstexten.

## P2: Oberfläche

- [ ] Mobilen Multistop-Ablauf vollständig prüfen, inklusive Gruppen,
  mehrerer Aufenthalte, Hotels, leerer Treffer und Fehlerzustände.
- [x] Flughafenfelder sichtbar als Start, Stopp und Rückkehr beziehungsweise Endziel benennen.
- [ ] Quellenberichte und Preisalter näher an das betroffene Ergebnis setzen.
- [x] Namen und Scanrhythmus gespeicherter Profile bearbeiten, pausieren und gezielt starten.
- [ ] Route und Reisezeitraum bestehender Profile ändern; alternativ explizite rollende Fenster.
  Bis dahin wird eine geänderte Route als neue Suche gespeichert, ohne alte Ergebnisse umzudeuten.
- [x] Erweiterte Scanner-/Jagd-Funktionen aus dem normalen Suchablauf lösen.
- [x] Hotel- und Flugansicht in Navigation, Formularaufbau und Schrift angleichen.
- [ ] Quellenalter und Preisvergleich in Flug- und Hoteldetails vereinheitlichen.
- [ ] Airline-Logos nur mit geklärten Nutzungsbedingungen verwenden;
  verständliche Airline-Kürzel als Fallback behalten.
- [ ] Projektlogo mit Multistop-Punkten und Globus konsistent einsetzen.
- [ ] Deutsche Umlaute, lange Städtenamen, Tastaturbedienung, Fokus,
  Kontraste und reduzierte Bewegung in jeder UI-Runde prüfen.

Abschluss: wichtige Daten ohne Tooltip lesbar, kein Seitenüberlauf,
keine überlappenden Texte; Bewegungen erklären Zustandswechsel und bleiben
abschaltbar. Mobile Tabellen dürfen im benannten Bereich horizontal scrollen.

## P3: Deal-Anwendung und Portfolio

- [ ] Flug- und Hotelangebote in einer gemeinsamen Deal-Ansicht zusammenführen.
- [ ] Preisfehler von normalen guten Angeboten klar unterscheiden.
- [ ] Reiseprofile, Filter und Favoriten für wiederkehrende Nutzung ausbauen.
- [ ] Bei mehreren Nutzern Konten, Datenzuordnung und Zugriffsrechte planen.
- [ ] Repräsentative Screenshots und ein reproduzierbares Demo-Szenario ergänzen.
- [ ] Architektur, Datenfluss, Teststrategie und technische Entscheidungen für
  Recruiter verständlich dokumentieren.
- [ ] Public-Code, private Betriebskonfiguration und lokale Daten weiter
  getrennt halten; Public-Sync vor Schreibvorgängen auf Secrets prüfen.
- [ ] Apache-2.0 und Lizenzen der eingebundenen Assets dokumentiert halten.

## Ablauf je Arbeitsrunde

1. Übergabe, Repo-Status und Roadmap lesen; Tests vor Codeänderungen ausführen.
2. Einen zusammenhängenden nächsten Schritt wählen, unabhängige Aufgaben
   parallel bearbeiten und Zuständigkeiten nach Dateien trennen.
3. Verhalten testen; UI zusätzlich auf Desktop und Handy visuell prüfen.
4. Ergebnisse, Einschränkungen und nächste Schritte dokumentieren.
5. Public-Dry-Run und Secret-Scan durchführen, freigegebene Dateien
   synchronisieren und Public-Version testen.
6. Kleine Commits im eigenen Git-Account erstellen. Push und Redeploy nur
   mit entsprechender Freigabe; keine AI-Co-Author-Trailer.

Bezahlte Integrationen bekommen vor Aktivierung ein ausdrückliches Budget.
Eine offene Aufgabe braucht eine Voraussetzung, ein Abschlusskriterium und
einen nächsten konkreten Schritt. So lässt sich die Arbeit auch mit begrenztem
Kontingent auf einem anderen Rechner fortsetzen.
