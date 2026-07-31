from datetime import UTC, datetime, timedelta

from .config import settings


def now() -> datetime:
    return datetime.now(UTC)


class StationClock:
    """Maps between wall-clock time, HLS segment sequence numbers, and PCM sample offsets.

    The renderer is the timeline authority: `epoch` is fixed at first boot (or re-anchored
    after a restart that fell behind real time), and segment n covers
    [epoch + n*D, epoch + (n+1)*D) where D = segment_duration_sec.
    """

    def __init__(self, epoch: datetime):
        self.epoch = epoch

    def seq_start(self, seq: int) -> datetime:
        return self.epoch + timedelta(seconds=seq * settings.segment_duration_sec)

    def seq_for_time(self, t: datetime) -> int:
        return int((t - self.epoch).total_seconds() // settings.segment_duration_sec)

    def samples_to_time(self, samples: int) -> datetime:
        return self.epoch + timedelta(seconds=samples / settings.sample_rate)

    def time_to_samples(self, t: datetime) -> int:
        return int((t - self.epoch).total_seconds() * settings.sample_rate)

    def rendered_edge(self, samples_rendered: int) -> datetime:
        """Wall-clock time up to which audio has been rendered."""
        return self.samples_to_time(samples_rendered)
