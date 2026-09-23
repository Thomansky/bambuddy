"""Tests for the bulk-ZIP streaming helpers in library routes.

Everything here happens after the 200 has been sent, where an exception can no
longer become a status code -- it can only truncate the archive. Each test pins
one way the stream used to die mid-body.
"""

import io
import zipfile

import pytest

from backend.app.api.routes.library import _stream_zip, _zip_member_timestamp

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _archive(chunks) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(b"".join(chunks)))


class TestStreamZipLargeMembers:
    def test_member_over_the_zip64_limit_is_streamed_not_refused(self, tmp_path, monkeypatch):
        """A single member past ZIP64_LIMIT must not raise once bytes are on the wire.

        zipfile decides the 32-bit layout up front from ZipInfo.file_size and
        only discovers the overflow in the entry's close(), which raises
        RuntimeError -- not an OSError, so the skip handler never sees it. The
        limit is shrunk here so the real 2 GiB threshold is exercised without a
        2 GiB fixture; the code path is identical.
        """
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 4096)
        payload = b"x" * 5000
        big = tmp_path / "big.bin"
        big.write_bytes(payload)

        with _archive(_stream_zip([("big.bin", big)])) as archive:
            assert archive.read("big.bin") == payload

    def test_a_large_member_does_not_spoil_the_members_after_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 4096)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 5000)
        small = tmp_path / "small.bin"
        small.write_bytes(b"tail")

        with _archive(_stream_zip([("big.bin", big), ("small.bin", small)])) as archive:
            assert archive.namelist() == ["big.bin", "small.bin"]
            assert archive.read("small.bin") == b"tail"


class TestStreamZipVanishingMembers:
    def test_member_deleted_mid_stream_is_skipped_not_fatal(self, tmp_path):
        """One dead row must not cost the user the other nine -- during the stream too.

        External-folder bytes live on a mount Bambuddy does not own, and the
        trash sweeper deletes in the background, so a member can disappear
        between the pre-flight walk and its turn in the archive.
        """
        first, doomed, last = (tmp_path / name for name in ("first.bin", "doomed.bin", "last.bin"))
        for path in (first, doomed, last):
            path.write_bytes(b"y" * 64)

        stream = _stream_zip([("first.bin", first), ("doomed.bin", doomed), ("last.bin", last)])
        chunks = [next(stream)]
        doomed.unlink()
        chunks.extend(stream)

        with _archive(chunks) as archive:
            assert archive.namelist() == ["first.bin", "last.bin"]
            assert archive.read("last.bin") == b"y" * 64

    def test_every_member_missing_still_yields_a_readable_archive(self, tmp_path):
        gone = tmp_path / "gone.bin"

        with _archive(_stream_zip([("gone.bin", gone)])) as archive:
            assert archive.namelist() == []


class TestZipMemberTimestamp:
    def test_ordinary_mtime_round_trips(self):
        # 2026-09-23 12:00:00 local time.
        import datetime as dt

        mtime = dt.datetime(2026, 9, 23, 12, 0, 0).timestamp()

        assert _zip_member_timestamp(mtime) == (2026, 9, 23, 12, 0, 0)

    def test_pre_1980_mtime_is_clamped_to_the_zip_epoch(self):
        """1975: representable as a datetime, but not as a ZIP date."""
        import datetime as dt

        mtime = dt.datetime(1975, 6, 2, 3, 4, 5).timestamp()

        assert _zip_member_timestamp(mtime) == ZIP_EPOCH

    @pytest.mark.parametrize(
        ("label", "mtime"),
        [
            ("pre-epoch", -157766400.0),  # OSError on Windows, before any clamping
            ("year 36812", float(1 << 40)),
            ("not a number", float("nan")),
            ("out of time_t range", 1e300),
        ],
    )
    def test_unconvertible_mtime_falls_back_to_the_epoch(self, label, mtime):
        """A scanned file's nonsense mtime must not cost the archive.

        On Windows ``fromtimestamp`` raises OSError for a negative timestamp
        rather than returning a pre-1980 date to clamp, so the guard cannot be
        a year comparison alone.
        """
        assert _zip_member_timestamp(mtime) == ZIP_EPOCH
