"""The cover endpoint answers the printer's own jobs without touching FTP (#3127).

A calibration run has no 3MF anywhere on the printer. Before this the cover
route swept eight FTP paths for it -- "Trying to download cover for
'calibrate_motion_precision.gcode'" -- on every dashboard refresh while the
printer was calibrating, and the status response even handed out the
cover_url that triggered it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import backend.app.api.routes.printers as printers_mod
from backend.app.api.routes.printers import get_printer_cover

pytestmark = pytest.mark.unit


class _FakeSession:
    def __init__(self, printer):
        self._printer = printer

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: self._printer)


@pytest.fixture(autouse=True)
def _clear_cover_state():
    printers_mod._cover_cache.clear()
    printers_mod._cover_404_cache.clear()
    printers_mod._cover_inflight.clear()
    yield
    printers_mod._cover_cache.clear()
    printers_mod._cover_404_cache.clear()
    printers_mod._cover_inflight.clear()


_PRINTER = SimpleNamespace(id=1, ip_address="127.0.0.1", access_code="x", model="H2S", name="P")


@pytest.mark.parametrize(
    ("gcode_file", "subtask_name"),
    [
        ("/usr/etc/print/O1S/calibrate_motion_precision.gcode", "calibrate_motion_precision.gcode"),
        ("/usr/etc/print/O1S/auto_cali_for_user_param.gcode", "auto_cali_for_user_param.gcode"),
        (None, "auto_pa_line_calib_mode"),
    ],
)
@pytest.mark.asyncio
async def test_internal_jobs_are_404_before_any_download(gcode_file, subtask_name):
    state = SimpleNamespace(subtask_name=subtask_name, gcode_file=gcode_file, state="RUNNING")
    produce = AsyncMock()

    with (
        patch("backend.app.core.database.async_session", lambda: _FakeSession(_PRINTER)),
        patch.object(printers_mod.printer_manager, "get_status", MagicMock(return_value=state)),
        patch.object(printers_mod, "_produce_cover_image", produce),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_printer_cover(1, None, None)

    assert exc_info.value.status_code == 404
    produce.assert_not_called()
    assert not printers_mod._cover_inflight


@pytest.mark.asyncio
async def test_a_users_print_still_downloads():
    state = SimpleNamespace(subtask_name="Benchy", gcode_file="/data/Metadata/plate_1.gcode", state="RUNNING")

    async def produce(printer_row, printer_id, subtask_name, view, view_key, plate_num, cache_key, archive_path=None):
        return b"PNGDATA"

    with (
        patch("backend.app.core.database.async_session", lambda: _FakeSession(_PRINTER)),
        patch.object(printers_mod.printer_manager, "get_status", MagicMock(return_value=state)),
        patch.object(printers_mod, "resolve_plate_id", MagicMock(return_value=1)),
        patch.object(printers_mod, "_produce_cover_image", produce),
    ):
        response = await get_printer_cover(1, None, None)
    assert bytes(response.body) == b"PNGDATA"


async def _status_cover_url(async_client, printer_id: int, gcode_file: str | None, subtask_name: str | None):
    from backend.app.services.bambu_mqtt import PrinterState

    state = PrinterState()
    state.connected = True
    state.state = "RUNNING"
    state.gcode_file = gcode_file
    state.subtask_name = subtask_name
    state.current_print = subtask_name

    with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
        mock_pm.get_status = MagicMock(return_value=state)
        mock_pm.is_awaiting_plate_clear = MagicMock(return_value=False)
        response = await async_client.get(f"/api/v1/printers/{printer_id}/status")
    assert response.status_code == 200, response.text
    return response.json()["cover_url"]


@pytest.mark.asyncio
async def test_status_hands_out_no_cover_url_for_an_internal_job(async_client, printer_factory):
    """The status response is what makes the dashboard ask for the cover in
    the first place; for the printer's own job it must not."""
    printer = await printer_factory(model="H2S")
    assert (
        await _status_cover_url(
            async_client,
            printer.id,
            "/usr/etc/print/O1S/calibrate_motion_precision.gcode",
            "calibrate_motion_precision.gcode",
        )
        is None
    )
    assert await _status_cover_url(async_client, printer.id, "", "auto_pa_line_calib_mode") is None


@pytest.mark.asyncio
async def test_status_keeps_the_cover_url_for_a_users_print(async_client, printer_factory):
    printer = await printer_factory(model="H2S")
    assert (
        await _status_cover_url(async_client, printer.id, "/data/Metadata/plate_1.gcode", "Benchy")
        == f"/api/v1/printers/{printer.id}/cover"
    )
