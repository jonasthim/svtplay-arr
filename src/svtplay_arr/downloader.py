import asyncio
import logging
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]


class DownloaderError(RuntimeError):
    """Raised when a downloader fails to produce output."""


class Downloader(Protocol):
    async def download(
        self, svt_id: str, dest_dir: Path, stem: str, on_progress: ProgressFn
    ) -> Path:
        """Download svt_id into dest_dir as <stem>.mkv, reporting progress."""


class FakeDownloader:
    """Test double that models progress over time.

    Deliberately asynchronous with observable intermediate states: a fake that
    returned instantly would let queue/progress tests pass without exercising
    the behaviour Sonarr actually polls.
    """

    def __init__(self, steps: int = 4, total_bytes: int = 1000, delay: float = 0.0):
        self._steps = steps
        self._total = total_bytes
        self._delay = delay

    async def download(
        self, svt_id: str, dest_dir: Path, stem: str, on_progress: ProgressFn
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        chunk = self._total // self._steps
        for i in range(1, self._steps + 1):
            if self._delay:
                await asyncio.sleep(self._delay)
            else:
                await asyncio.sleep(0)
            # Integer division can under-report the last step (e.g. 999/1000
            # for steps=3). The final call must land on the exact total, or a
            # download modeled by this fake never actually reaches 100%.
            done = self._total if i == self._steps else chunk * i
            on_progress(done, self._total)
        out = dest_dir / f"{stem}.mkv"
        out.write_bytes(b"\0")
        return out


class SvtplayDlDownloader:
    """Real downloader. Runs svtplay-dl in a worker thread.

    svtplay-dl is synchronous, so it is driven via asyncio.to_thread to keep
    the event loop (and therefore the SAB queue endpoint) responsive.

    While the worker thread is running, a concurrent poller periodically sums
    the sizes of files currently in dest_dir and reports that as progress.
    svtplay-dl exposes no progress-callback API, so this is the only way to
    give Sonarr's mode=queue bar anything other than 0% followed by a jump to
    100%. svtplay-dl writes intermediate files (e.g. .ts) and then remuxes to
    the final .mkv, deleting the originals, so summing everything currently
    on disk is the right measure of bytes downloaded so far. The total isn't
    known here (svtplay-dl doesn't expose it up front either), so total=0 is
    reported during polling; the job's nzb-derived size_bytes is what the SAB
    queue actually uses to compute a percentage, so this is not load-bearing.

    Only exercised by the opt-in integration test (Task 13); the unit suite
    never drives this against the network.
    """

    # The slug after the id is cosmetic: `/video/KZmQ5JY/x` was verified on
    # 2026-08-24 to resolve identically to the real slug, so no slug lookup is
    # needed at download time.
    def __init__(
        self,
        url_template: str = "https://www.svtplay.se/video/{svt_id}/x",
        poll_interval: float = 1.5,
    ):
        self._url_template = url_template
        self._poll_interval = poll_interval

    async def download(
        self, svt_id: str, dest_dir: Path, stem: str, on_progress: ProgressFn
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        poller = asyncio.create_task(self._poll_progress(dest_dir, on_progress))
        try:
            await asyncio.to_thread(self._run, svt_id, dest_dir, stem)
        except (SystemExit, Exception) as exc:
            # get_media() uses sys.exit() as its failure signal on several
            # paths (unsupported URL, bad output format, download error)
            # instead of raising -- and on others (network failures inside
            # service_handler()/Generic.get(), stream.get()) it lets ordinary
            # exceptions propagate. Both would otherwise leak out of this
            # module raw: SystemExit escapes asyncio.to_thread just like any
            # other exception, since the underlying ThreadPoolExecutor
            # captures BaseException on the worker and re-raises it here.
            raise DownloaderError(f"svtplay-dl failed while fetching {svt_id!r}") from exc
        finally:
            # A monitoring mechanism must never be able to fail the thing it
            # monitors: progress reporting is a nice-to-have, the download is
            # the product. cancel() is a no-op if the poller already finished
            # (e.g. with an exception) before we got here, and awaiting it
            # can therefore re-raise whatever it died with -- every outcome
            # from the poller is caught and discarded below, so nothing it
            # does can override a real DownloaderError from the try block, or
            # turn a successful download into a raised exception.
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("progress poller for %s terminated abnormally", dest_dir, exc_info=True)
        out = dest_dir / f"{stem}.mkv"
        if not out.exists():
            raise DownloaderError(f"svtplay-dl produced no output for {svt_id!r}")
        size = out.stat().st_size
        on_progress(size, size)
        return out

    async def _poll_progress(self, dest_dir: Path, on_progress: ProgressFn) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            # Everything in one poll iteration -- the directory scan itself,
            # not just the per-file stat() -- is guarded. A file vanishing
            # mid-scan during svtplay-dl's remux-and-delete is routine, and
            # so is a transient PermissionError or any other OSError; none of
            # it may be allowed to kill the poller (see download()'s finally
            # block for the second layer of the same guarantee).
            try:
                total = 0
                for entry in dest_dir.iterdir():
                    try:
                        if entry.is_file():
                            total += entry.stat().st_size
                    except OSError:
                        continue
                on_progress(total, 0)
            except Exception:
                log.debug("progress poll failed for %s", dest_dir, exc_info=True)

    def _run(self, svt_id: str, dest_dir: Path, stem: str) -> None:
        from svtplay_dl import get_media, setup_defaults

        options = setup_defaults()
        options.set("output", str(dest_dir / stem))
        options.set("subtitle", True)
        options.set("output_format", "mkv")
        get_media(self._url_template.format(svt_id=svt_id), options)
