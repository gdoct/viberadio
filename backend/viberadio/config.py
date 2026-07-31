from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class StationPreset:
    """A station's identity. Seeded into `channels` on first boot."""

    slug: str
    name: str
    style: str
    dj_name: str
    dj_persona: str
    catchphrase: str
    tts_voice: str


# The dial. Each station runs its own agents, timeline and HLS stream; they share
# the media library so a track downloaded for one is instantly available to all.
STATIONS: tuple[StationPreset, ...] = (
    StationPreset(
        slug="kgor",
        name="Goldie Oldie Rock KGOR",
        style="60s 70s rock",
        dj_name="Kyle",
        dj_persona="Male, enthusiastic, dark voice",
        catchphrase="Where your best memories happen",
        tts_voice="am_onyx",
    ),
    StationPreset(
        slug="kjfk",
        name="Jazzy Funky Soul KJFK",
        style="jazz, funk and classic soul",
        dj_name="Vivian",
        dj_persona="Female, warm and unhurried, late-night velvet",
        catchphrase="Keeping it smooth after dark",
        tts_voice="af_bella",
    ),
    StationPreset(
        slug="kbon",
        name="Best of the Nineties KBON",
        style="90s alternative, grunge, britpop and hip hop",
        dj_name="Dez",
        dj_persona="Male, fast-talking and irreverent, radio-brat energy",
        catchphrase="All killer, no filler",
        tts_voice="am_michael",
    ),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite+aiosqlite:///{BACKEND_DIR / 'data' / 'viberadio.db'}"

    data_dir: Path = BACKEND_DIR / "data"

    # Audio timeline
    sample_rate: int = 48000
    channels: int = 2
    segment_duration_sec: int = 10
    lookahead_sec: float = 75.0
    crossfade_sec: float = 3.0
    voice_overlap_sec: float = 0.7
    voice_duck_db: float = -4.0
    hls_window_segments: int = 30
    segment_ttl_sec: int = 1800  # keep segment files ~30 min for pause/rewind

    # Agents
    selector_interval_sec: float = 5.0
    voice_interval_sec: float = 3.0
    engineer_interval_sec: float = 2.0
    min_queued_songs: int = 3
    voice_safety_margin_sec: float = 8.0  # crossfade + 5s

    llm_timeout_sec: float = 120.0

    # Running every station at once would triple the LLM, TTS and download load for
    # listeners who are only on one. Stations start on demand and shut down again
    # once nobody has asked about them for this long.
    station_idle_timeout_sec: float = 300.0

    # TTS (Kokoro). Per-station voices are set in STATIONS; this is the fallback.
    tts_voice: str = "am_onyx"
    tts_speed: float = 1.0

    @property
    def samples_per_segment(self) -> int:
        return self.sample_rate * self.segment_duration_sec

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "media" / "normalized"

    @property
    def voice_dir(self) -> Path:
        return self.data_dir / "voice"

    @property
    def hls_dir(self) -> Path:
        return self.data_dir / "hls"

    def hls_dir_for(self, slug: str) -> Path:
        """Each station gets its own segment directory and playlist."""
        return self.hls_dir / slug

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for d in (
            self.media_dir,
            self.normalized_dir,
            self.voice_dir,
            self.hls_dir,
            self.models_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
