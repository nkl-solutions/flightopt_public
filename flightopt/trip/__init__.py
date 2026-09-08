"""Die ganze Reise, nicht nur der Flug.

Zwischen zwei Fluegen einer Kandidatenzeile liegt immer ein Aufenthalt, und
der ist durch den Datumsgraphen vollstaendig bestimmt: Ort, Anreise und
Naechte stehen fest, sobald das Datumspaar feststeht. Genau das kann kein
Portal, das nur im Raum vergleicht.

Das Paket haengt bewusst an nichts aus `flightopt.jobs`: der Aufrufer reicht
den Quellen-Katalog und den Abbruch-Merker herein. So bleibt der Flug-Runner
frei von allem, was mit Uebernachtungen zu tun hat.
"""
