"""The dial, read from `stations/*.md`.

One file per station so a station's identity can be edited — or a new one added —
without touching code. See `stations/README.md` for the format.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .config import settings

# `01-kgor.md` -> position 1 on the dial, slug `kgor`.
FILENAME_RE = re.compile(r"^(?P<order>\d+)[-_](?P<slug>[a-z0-9][a-z0-9-]*)$")

# Headings as written in the file (matched case-insensitively) -> preset fields.
FIELDS = {
    "Style": "style",
    "DJ": "dj_name",
    "Persona": "dj_persona",
    "Catchphrase": "catchphrase",
    "TTS voice": "tts_voice",
}

# Sections a station may leave out. A DJ with no running bits still works; they
# just have less to call back to.
OPTIONAL_FIELDS = {
    "Bits": "dj_bits",
}


class StationFileError(Exception):
    """A station file is missing, misnamed or missing a section."""


@dataclass(frozen=True)
class StationPreset:
    """A station's identity. Applied to `channels` on every boot."""

    slug: str
    name: str
    style: str
    dj_name: str
    dj_persona: str
    catchphrase: str
    tts_voice: str
    dj_bits: str = ""


def _sections(text: str) -> tuple[str, dict[str, str]]:
    """Split a station file into its `# title` and its `## heading` sections."""
    title = ""
    sections: dict[str, str] = {}
    heading: str | None = None
    body: list[str] = []

    def flush() -> None:
        if heading is not None:
            sections[heading] = "\n".join(body).strip()

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            heading = line[3:].strip().lower()
            body = []
        elif line.startswith("# ") and heading is None:
            title = line[2:].strip()
        elif heading is not None:
            body.append(line)
    flush()
    return title, sections


def parse_station(path: Path) -> tuple[int, StationPreset]:
    """Read one station file, returning its dial position and its preset."""
    match = FILENAME_RE.match(path.stem)
    if match is None:
        raise StationFileError(
            f"{path.name}: file name must look like `01-kgor.md` — a dial position, "
            "a dash, then the station slug in lowercase"
        )

    title, sections = _sections(path.read_text(encoding="utf-8"))
    if not title:
        raise StationFileError(
            f"{path.name}: needs a `# ` heading with the station name on the first line"
        )

    values = {}
    for heading, field in FIELDS.items():
        value = sections.get(heading.lower(), "")
        if not value:
            raise StationFileError(
                f"{path.name}: missing or empty section `## {heading}`"
            )
        values[field] = value
    for heading, field in OPTIONAL_FIELDS.items():
        values[field] = sections.get(heading.lower(), "")

    preset = StationPreset(slug=match["slug"], name=title, **values)
    return int(match["order"]), preset


def load_stations(stations_dir: Path | None = None) -> tuple[StationPreset, ...]:
    """Every station on the dial, ordered by the number its file name starts with.

    Read on call rather than cached at import, so an edited file takes effect on
    the next bootstrap without anything else having to know it changed.
    """
    directory = stations_dir or settings.stations_dir
    if not directory.is_dir():
        raise StationFileError(f"no station directory at {directory}")

    paths = sorted(
        p
        for p in directory.glob("*.md")
        if not p.name.startswith((".", "_")) and p.stem.lower() != "readme"
    )
    stations = [parse_station(p) for p in paths]
    if not stations:
        raise StationFileError(f"no station files in {directory}")

    seen: set[str] = set()
    for _, preset in stations:
        if preset.slug in seen:
            raise StationFileError(
                f"two station files share the slug `{preset.slug}` — slugs must be unique"
            )
        seen.add(preset.slug)

    stations.sort(key=lambda item: item[0])
    return tuple(preset for _, preset in stations)
