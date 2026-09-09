"""Die Jagd auf Fehltarife: Takt, Erkennung, Meldung.

Drei Teile, drei Module, und eine Reihenfolge, in der sie voneinander wissen:

* `cadence` haelt Takt und Obergrenzen. Reine Zahlen, keine Datenbank.
* `budget` fuehrt Buch darueber, was eine Quelle in der letzten Stunde
  abbekommen hat, und wer gerade gesperrt hat.
* `errorfare` entscheidet, ob ein Preis ein Fehltarif ist.
* `alerts` schreibt den Fund weg und entscheidet, ob er gemeldet werden darf.
* `discord` ist der Kanal, und nur der Kanal.

Der Grund fuer ein eigenes Paket statt weiterer Funktionen in `storage`: die
Jagd hat einen anderen Takt als alles andere im Werkzeug. Eine Suche laeuft,
wenn ein Mensch sie startet; die Beobachtung laeuft einmal am Tag; die Jagd
laeuft alle zwanzig Minuten und darf deshalb als einziger Teil des Systems
fremde Server in kurzen Abstaenden fragen. Was diese Erlaubnis begrenzt,
gehoert an eine Stelle und nicht verteilt ueber drei fremde Module.
"""
