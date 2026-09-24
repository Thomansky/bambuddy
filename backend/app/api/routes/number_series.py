"""API routes for the running-number series (order folders, projects, queued jobs)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.number_series import NumberSeries
from backend.app.models.user import User
from backend.app.schemas.number_series import NumberSeriesResponse, NumberSeriesUpdate
from backend.app.services.number_series import MAX_RENDERED_LENGTH, render_number

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/number-series", tags=["number-series"])

# A series is a per-install setting, not a per-user one, so it lives behind the
# same permissions as the Settings page it is edited from.
_READ = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ)
_UPDATE = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE)


def _to_response(series: NumberSeries) -> NumberSeriesResponse:
    return NumberSeriesResponse(
        key=series.key,
        enabled=series.enabled,
        prefix=series.prefix or "",
        suffix=series.suffix or "",
        next_value=series.next_value,
        padding=series.padding,
        updated_at=series.updated_at,
        preview=render_number(series.prefix, series.next_value, series.padding, series.suffix),
    )


@router.get("/", response_model=list[NumberSeriesResponse])
async def list_number_series(
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    """List every series, seeded order (project first)."""
    result = await db.execute(select(NumberSeries).order_by(NumberSeries.id))
    return [_to_response(series) for series in result.scalars().all()]


@router.patch("/{key}", response_model=NumberSeriesResponse)
async def update_number_series(
    key: str,
    data: NumberSeriesUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = _UPDATE,
):
    """Update one series. Unknown keys are 404 — the rows are seeded, not created here."""
    result = await db.execute(select(NumberSeries).where(NumberSeries.key == key))
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="Number series not found")

    if data.enabled is not None:
        series.enabled = data.enabled
    if data.prefix is not None:
        series.prefix = data.prefix
    if data.suffix is not None:
        series.suffix = data.suffix
    if data.next_value is not None:
        series.next_value = data.next_value
    if data.padding is not None:
        series.padding = data.padding

    rendered = render_number(series.prefix, series.next_value, series.padding, series.suffix)
    if len(rendered) > MAX_RENDERED_LENGTH:
        # Refused here rather than at allocation time: the columns that store
        # the result are VARCHAR(32), and a PostgreSQL install would start
        # failing every create instead of every save.
        raise HTTPException(
            status_code=400,
            detail=f"A number from this series would be {len(rendered)} characters, more than the {MAX_RENDERED_LENGTH} allowed",
        )

    await db.flush()
    await db.refresh(series)
    return _to_response(series)
