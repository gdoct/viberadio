from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite+aiosqlite:///{BACKEND_DIR / 'data' / 'viberadio.db'}"

    data_dir: Path = BACKEND_DIR / "data"

    # The dial: one Markdown file per station, applied to `channels` on boot.
    stations_dir: Path = BACKEND_DIR / "stations"

    # Built listener console. Served at / when the directory exists, which is how
    # the container puts the UI, API and stream on a single port. Empty in dev,
    # where Vite serves the console and proxies to this backend.
    frontend_dist: Path = BACKEND_DIR.parent / "frontend" / "dist"

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

    # TTS (Kokoro). Per-station voices come from the station files; this is the fallback.
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
