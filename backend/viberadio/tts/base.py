from pathlib import Path
from typing import Protocol


class TTSProvider(Protocol):
    """Synthesize spoken audio. Kept as a protocol so a cloud voice can be swapped in."""

    def synthesize(self, text: str, dst: Path) -> Path:
        """Write speech for `text` to `dst` and return the path."""
        ...
