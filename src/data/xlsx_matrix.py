"""Small streaming XLSX reader for the row-oriented matrices supplied to research."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, TypeAlias
from xml.etree import ElementTree as ET
from zipfile import ZipFile

WorkbookSource: TypeAlias = str | Path | BinaryIO
_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def sheet_paths(source: WorkbookSource) -> dict[str, str]:
    """Return an exact name-to-XML mapping without loading worksheet contents."""
    with ZipFile(source) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        element.attrib["Id"]: element.attrib["Target"]
        for element in relationships.findall(f"{_PACKAGE_REL}Relationship")
    }
    paths: dict[str, str] = {}
    for sheet in workbook.findall(f"{_MAIN}sheets/{_MAIN}sheet"):
        target = rel_targets[sheet.attrib[f"{_REL}id"]]
        paths[sheet.attrib["name"]] = f"xl/{target}" if not target.startswith("/") else target.lstrip("/")
    return paths


def iter_sheet_rows(source: WorkbookSource, name: str) -> Iterator[list[str | float | bool | None]]:
    """Yield rows in order, retaining sparse cells and numerical precision."""
    paths = sheet_paths(source)
    if name not in paths:
        raise ValueError(f"worksheet not found: {name}")
    with ZipFile(source) as archive, archive.open(paths[name]) as xml_stream:
        for event, element in ET.iterparse(xml_stream, events=("end",)):
            if event != "end" or element.tag != f"{_MAIN}row":
                continue
            values: list[str | float | bool | None] = []
            for cell in element.findall(f"{_MAIN}c"):
                index = _column_index(cell.attrib["r"])
                while len(values) <= index:
                    values.append(None)
                values[index] = _cell_value(cell)
            yield values
            element.clear()


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    value = 0
    for character in letters:
        value = value * 26 + ord(character.upper()) - ord("A") + 1
    return value - 1


def _cell_value(cell: ET.Element) -> str | float | bool | None:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(cell.itertext())
    raw = cell.findtext(f"{_MAIN}v")
    if raw is None:
        return None
    if cell_type == "b":
        return raw == "1"
    if cell_type in {"str", "e"}:
        return raw
    return float(raw)
