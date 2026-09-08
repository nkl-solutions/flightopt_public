"""Ein sehr kleiner Baum ueber `html.parser`.

Booking liefert eine Seite, kein JSON. Eine Parser-Bibliothek dafuer waere eine
weitere Abhaengigkeit im Image, und gebraucht wird genau dreierlei: Karten
finden, darin nach `data-testid` suchen, Text und Attribute lesen.

Der Baum merkt sich ausserdem, wo im Quelltext ein Element anfing und aufhoerte.
Damit kann `scripts/record_hotel_fixtures.py` aus einer 2-MB-Seite die ersten
Karten wortgetreu herausschneiden, statt sie nachzubauen: eine nachgebaute
Testdatei prueft den Parser gegen sich selbst.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterator

# Elemente ohne Ende-Tag. Wer sie auf den Stapel legt, verschachtelt den Rest
# der Seite in ein <img>.
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
# Deren Inhalt ist Programm oder Stil, kein Text.
OPAQUE = {"script", "style", "template", "noscript"}

_WS = re.compile(r"\s+")


@dataclass(slots=True)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    source_start: int = -1
    source_end: int = -1

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def walk(self) -> Iterator["Node"]:
        for child in self.children:
            yield child
            yield from child.walk()

    def find_all(self, match: Callable[["Node"], bool]) -> list["Node"]:
        return [node for node in self.walk() if match(node)]

    def find(self, match: Callable[["Node"], bool]) -> "Node | None":
        for node in self.walk():
            if match(node):
                return node
        return None

    @property
    def text(self) -> str:
        """Der sichtbare Text dieses Teilbaums, Leerraum zusammengefasst."""
        if self.tag in OPAQUE:
            return ""
        chunks = list(self.parts)
        for child in self.children:
            chunks.append(child.text)
        return _WS.sub(" ", "".join(chunks)).strip()

    def source(self, raw: str) -> str:
        """Der Quelltext dieses Elements, unveraendert."""
        if self.source_start < 0 or self.source_end <= self.source_start:
            return ""
        return raw[self.source_start:self.source_end]


def testid(value: str) -> Callable[[Node], bool]:
    return lambda node: node.attrs.get("data-testid") == value


def testid_in(*values: str) -> Callable[[Node], bool]:
    wanted = set(values)
    return lambda node: node.attrs.get("data-testid") in wanted


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]
        self.raw = ""
        self._line_starts: list[int] = [0]

    def parse(self, raw: str) -> Node:
        self.raw = raw
        self._line_starts = [0]
        for index, char in enumerate(raw):
            if char == "\n":
                self._line_starts.append(index + 1)
        self.feed(raw)
        self.close()
        return self.root

    def _offset(self) -> int:
        line, column = self.getpos()
        if 1 <= line <= len(self._line_starts):
            return self._line_starts[line - 1] + column
        return -1

    def _tag_end(self, start: int) -> int:
        end = self.raw.find(">", start)
        return end + 1 if end >= 0 else len(self.raw)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start = self._offset()
        node = Node(tag, {k: (v or "") for k, v in attrs}, source_start=start)
        self.stack[-1].children.append(node)
        if tag in VOID:
            node.source_end = self._tag_end(start)
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start = self._offset()
        node = Node(tag, {k: (v or "") for k, v in attrs}, source_start=start)
        node.source_end = self._tag_end(start)
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        # Von innen nach aussen den passenden Anfang suchen. Fehlt er, war das
        # Ende-Tag verwaist; eine echte Seite hat davon mehr als eines, und ein
        # Parser, der daran den Stapel leert, verliert den Rest des Dokuments.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].source_end = self._tag_end(self._offset())
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].parts.append(data)


def parse_html(raw: str) -> Node:
    """Die Seite als Baum. Der Wurzelknoten ist das Dokument selbst."""
    return _Builder().parse(raw)
