import asyncio
import time
from pathlib import Path

import pytest

from svtplay_arr.downloader import DownloaderError, FakeDownloader, SvtplayDlDownloader


async def test_fake_reports_intermediate_progress(tmp_path: Path):
    seen: list[int] = []
    d = FakeDownloader(steps=4, total_bytes=1000)
    await d.download("KZmQ5JY", tmp_path, "stem", lambda done, total: seen.append(done))
    # The point of this test: NOT just the terminal value.
    assert seen == [250, 500, 750, 1000]
    assert len(seen) > 1, "a synchronous fake cannot test an async protocol"


async def test_fake_writes_output_file(tmp_path: Path):
    d = FakeDownloader(steps=2, total_bytes=10)
    out = await d.download("x", tmp_path, "My Show - S01E01 - WEBDL-1080p", lambda *a: None)
    assert out.name == "My Show - S01E01 - WEBDL-1080p.mkv"
    assert out.exists()


async def test_fake_reports_exact_total_when_steps_do_not_divide_evenly(tmp_path: Path):
    seen: list[int] = []
    d = FakeDownloader(steps=3, total_bytes=1000)
    await d.download("x", tmp_path, "stem", lambda done, total: seen.append(done))
    # 1000 // 3 == 333, so a naive chunk*i sequence would end at 999, never
    # reaching 100%. The final call must be exact.
    assert seen[-1] == 1000
    assert seen == [333, 666, 1000]


async def test_progress_is_observable_while_download_runs(tmp_path: Path):
    seen: list[int] = []
    d = FakeDownloader(steps=5, total_bytes=500, delay=0.01)
    task = asyncio.create_task(
        d.download("x", tmp_path, "s", lambda done, total: seen.append(done))
    )
    await asyncio.sleep(0.025)
    mid = len(seen)
    await task
    assert 0 < mid < len(seen), "progress must be visible mid-flight"


async def test_svtplaydl_downloader_wraps_system_exit(tmp_path: Path, monkeypatch):
    # get_media() signals several failure modes (unsupported URL, download
    # error) via sys.exit() rather than raising, which would otherwise
    # surface as an uncaught SystemExit out of the worker thread. No network
    # call happens here: get_media is replaced entirely.
    import svtplay_dl

    def fake_get_media(url, options):
        raise SystemExit(2)

    monkeypatch.setattr(svtplay_dl, "get_media", fake_get_media)

    d = SvtplayDlDownloader()
    with pytest.raises(DownloaderError):
        await d.download("x", tmp_path, "stem", lambda *a: None)


async def test_svtplaydl_downloader_wraps_ordinary_exception(tmp_path: Path, monkeypatch):
    # get_media() doesn't only fail via sys.exit(): network errors inside
    # service_handler()/Generic.get() and stream.get() propagate as ordinary
    # exceptions. Those must not leak across the module boundary raw either.
    import svtplay_dl

    def fake_get_media(url, options):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(svtplay_dl, "get_media", fake_get_media)

    d = SvtplayDlDownloader()
    with pytest.raises(DownloaderError):
        await d.download("x", tmp_path, "stem", lambda *a: None)


async def test_svtplaydl_downloader_raises_when_no_output_produced(tmp_path: Path, monkeypatch):
    # If get_media() returns normally without writing the expected output
    # (another of its silent-failure paths), the downloader must still
    # surface an error rather than returning a Path that doesn't exist.
    import svtplay_dl

    monkeypatch.setattr(svtplay_dl, "get_media", lambda url, options: None)

    d = SvtplayDlDownloader()
    with pytest.raises(DownloaderError):
        await d.download("x", tmp_path, "stem", lambda *a: None)


async def test_svtplaydl_downloader_reports_progress_while_running(tmp_path: Path, monkeypatch):
    # No progress-callback API exists in svtplay-dl, so SvtplayDlDownloader
    # must poll bytes-on-disk itself. This stub simulates svtplay-dl writing
    # an intermediate .ts file in stages and then remuxing to the final .mkv
    # (deleting the .ts) -- exactly the on-disk pattern the poller has to
    # tolerate. No network call happens: get_media is replaced entirely.
    import svtplay_dl

    def fake_get_media(url, options):
        prefix = Path(options.get("output"))
        partial = prefix.with_suffix(".ts")
        for chunk in (b"a" * 100, b"a" * 100, b"a" * 100):
            with open(partial, "ab") as f:
                f.write(chunk)
            time.sleep(0.03)
        final = prefix.with_suffix(".mkv")
        final.write_bytes(b"a" * 300)
        partial.unlink()
        time.sleep(0.03)

    monkeypatch.setattr(svtplay_dl, "get_media", fake_get_media)

    seen: list[tuple[int, int]] = []
    d = SvtplayDlDownloader(poll_interval=0.01)
    out = await d.download(
        "x", tmp_path, "stem", lambda done, total: seen.append((done, total))
    )

    assert out.exists()
    final_size = out.stat().st_size
    sizes = [done for done, _ in seen]
    # More than just the single terminal call: progress must be visible
    # mid-flight, not just a jump from 0 to 100%.
    assert len(sizes) > 1
    assert sizes == sorted(sizes), "progress must never go backwards"
    assert any(s < final_size for s in sizes[:-1]), "an intermediate value below the final total must be observed"
    assert seen[-1] == (final_size, final_size), "the terminal call must report the exact final size"


def _boom_iterdir(self):
    raise PermissionError("nope")


async def test_svtplaydl_downloader_survives_poller_failure_on_success(tmp_path: Path, monkeypatch):
    # A monitoring mechanism must never be able to fail the thing it
    # monitors: if the poller's directory scan blows up (e.g. a permissions
    # error) while the download is actually succeeding, the download must
    # still complete and return the expected Path -- not raise the poller's
    # exception, and not turn a successful download into a false failure.
    import svtplay_dl

    def fake_get_media(url, options):
        time.sleep(0.05)
        Path(f"{options.get('output')}.mkv").write_bytes(b"a" * 10)

    monkeypatch.setattr(svtplay_dl, "get_media", fake_get_media)
    monkeypatch.setattr(Path, "iterdir", _boom_iterdir)

    d = SvtplayDlDownloader(poll_interval=0.01)
    out = await d.download("x", tmp_path, "stem", lambda *a: None)

    assert out.exists()
    assert out.name == "stem.mkv"


async def test_svtplaydl_downloader_reports_run_failure_not_poller_failure(tmp_path: Path, monkeypatch):
    # If _run() fails AND the poller is also failing at the same time,
    # finally-supersedes-except semantics must not let the poller's
    # exception replace the real cause: the caller must still see the
    # DownloaderError describing the download failure, not the poller's
    # PermissionError.
    import svtplay_dl

    def fake_get_media(url, options):
        time.sleep(0.05)
        raise RuntimeError("boom-in-run")

    monkeypatch.setattr(svtplay_dl, "get_media", fake_get_media)
    monkeypatch.setattr(Path, "iterdir", _boom_iterdir)

    d = SvtplayDlDownloader(poll_interval=0.01)
    with pytest.raises(DownloaderError) as exc_info:
        await d.download("x", tmp_path, "stem", lambda *a: None)

    assert "failed while fetching" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "boom-in-run"
