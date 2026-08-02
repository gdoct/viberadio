from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite+aiosqlite:///{BACKEND_DIR / 'data' / 'viberadio.db'}"

    data_dir: Path = BACKEND_DIR / "data"

    # Kept outside data_dir so the run log can be mounted, rotated and discarded
    # without touching the library or the database.
    log_dir: Path = BACKEND_DIR / "logs"
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 5

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
    hls_window_segments: int = 30
    # How far back the station can be listened to. Segment files stay on disk for
    # this long so a listener who pauses can catch up again; past it nothing can
    # reach them any more and the janitor takes them.
    listen_back_sec: int = 7200  # 120 minutes

    # Housekeeping
    janitor_interval_sec: float = 60.0
    # Rendered DJ audio is mixed into the segments as it goes to air, so the source
    # file is dead weight afterwards. The grace covers a restart that resumes into
    # a break it has already rendered but not yet finished broadcasting.
    voice_audio_grace_sec: int = 300

    # The DJ break. A break is not dropped into a gap between records — the DJ
    # opens over the outgoing song's outro and then rides the next song's intro,
    # with the music ducked underneath the whole way. Dead air is what made the
    # old 0.7s butt-splice sound like an audiobook chapter marker.
    voice_ramp_in_sec: float = 5.0  # DJ opens over the end of the outgoing song
    voice_ramp_out_max_sec: float = 20.0  # cap on how much next-song intro they eat
    voice_duck_db: float = -11.0  # how far the music sits under the DJ
    voice_duck_edge_sec: float = 0.7  # how fast the ducker closes and opens again

    # Programming. The station's day is decided in advance, one hour at a time,
    # and each half-hour block is fitted so it ends on its mark — that is what
    # makes it possible to put anything (the news, an ident) at :00 and :30.
    #
    # The mark is hit within a tolerance rather than exactly: song lengths are
    # what they are, and no filler is inserted to make up the difference.
    programme_interval_sec: float = 30.0
    programme_block_sec: int = 1800
    programme_mark_tolerance_sec: float = 15.0
    # How much of the day must always be planned ahead. Beyond this the plan is
    # extended towards the end of tomorrow at one hour per tick, so building two
    # days never monopolises the one-at-a-time LLM lock.
    programme_min_hours_ahead: int = 4
    # What one DJ break costs the timeline: the break itself advances the clock by
    # `voice_ramp_in_sec` + renderer.DRY_SEC, and the song before it gives up
    # `voice_ramp_in_sec` instead of `crossfade_sec` to make room for the opening.
    # Breaks are placed opportunistically, so this is a reservation, not a promise —
    # the next block is re-fitted against the real cursor either way.
    programme_break_cost_sec: float = 7.6
    # New records the DJ may ask for per hour they programme. This is the only
    # thing growing the library now that songs are not picked one at a time.
    programme_new_songs_per_hour: int = 2
    programme_hour_candidates: int = 18
    # Rotation fallback: how long before the same artist may come round again.
    programme_artist_spacing_sec: float = 1800.0
    # Only the today/tomorrow boundary depends on this. The :00 and :30 marks do
    # not — every whole-hour offset has them in the same places as UTC.
    station_timezone: str = "Europe/Amsterdam"

    # Agents
    selector_interval_sec: float = 5.0
    voice_interval_sec: float = 3.0
    engineer_interval_sec: float = 2.0
    min_queued_songs: int = 3
    # Must cover the outgoing song's ramp-in: the renderer can only open the DJ over
    # an outro it has not committed yet.
    voice_safety_margin_sec: float = 8.0  # voice_ramp_in_sec + crossfade_sec
    # How many previous breaks the DJ is reminded of, so bits can pay off later.
    voice_history_breaks: int = 4

    # Newsroom. The feeds are public and shared by every station, so they are polled
    # process-wide and never more than once an hour each — the personalization
    # happens downstream, when a station's anchor writes their own copy from them.
    news_interval_sec: float = 60.0
    news_min_fetch_interval_sec: float = 3600.0
    news_fetch_timeout_sec: float = 15.0
    news_sources: list[str] = [
        "https://www.nu.nl/rss",
        "https://www.nu.nl/rss/tech-wetenschap",
    ]
    news_gossip_sources: list[str] = ["https://www.nu.nl/rss/Achterklap"]
    news_remarkable_sources: list[str] = ["https://www.nu.nl/rss/Opmerkelijk"]
    # How many items of each kind the anchor is handed to write from.
    news_headline_count: int = 3
    news_gossip_count: int = 2
    news_remarkable_count: int = 2
    # Items are kept long enough to still be there after an overnight gap; copy
    # goes stale far sooner, since it is written against one hour's headlines.
    news_retention_hours: int = 48
    news_segment_ttl_sec: float = 7200.0
    # A station spinning up is already queued behind its own LLM calls for the
    # first songs and the first break. The newsroom waits that burst out.
    news_generate_grace_sec: float = 45.0

    llm_timeout_sec: float = 120.0

    # Running every station at once would triple the LLM, TTS and download load for
    # listeners who are only on one. Stations start on demand and shut down again
    # once nobody has asked about them for this long.
    station_idle_timeout_sec: float = 300.0

    # DJ voice chain. The music is only ducked, not removed, so the voice has to be
    # denser and louder than the record it sits on — a flat -16 LUFS read disappears
    # under a chorus. Drive is the difference between "audiobook" and "microphone".
    voice_target_lufs: float = -11.0
    voice_drive: float = 0.4  # 0 = clean, 1 = crunchy

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
