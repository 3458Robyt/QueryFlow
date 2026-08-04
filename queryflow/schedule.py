from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


class ScheduleError(RuntimeError):
    """A scheduled-query specification is unsafe or invalid."""


# BigQuery allows longer identifiers, but keeping the pilot's names within the
# portable lower-case identifier subset prevents quoting surprises in the
# destination table template.
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,1023}$")
_LOCATION = re.compile(r"^[a-z0-9-]{1,63}$")
_DAILY = re.compile(r"^every day ([01][0-9]|2[0-3]):([0-5][0-9])$")
_INTERVAL = re.compile(r"^every ([1-9][0-9]*) (minutes?|hours?|days?)$")


@dataclass(frozen=True)
class ScheduleSpec:
    """Immutable metadata required to create a disabled scheduled query."""

    schedule: str
    location: str
    destination_dataset: str
    destination_table: str
    write_disposition: str = "WRITE_APPEND"
    disabled: bool = True

    @classmethod
    def from_values(
        cls,
        *,
        schedule: str,
        location: str,
        destination_dataset: str,
        destination_table: str,
        write_disposition: str = "WRITE_APPEND",
        disabled: bool = True,
    ) -> "ScheduleSpec":
        normalized_schedule = str(schedule or "").strip()
        if not normalized_schedule or "\n" in normalized_schedule or "\r" in normalized_schedule:
            raise ScheduleError("schedule debe ser una expresión no vacía de una sola línea")
        if len(normalized_schedule) > 256:
            raise ScheduleError("schedule es demasiado largo")
        # The pilot is intentionally off the exact hour.  Support the common
        # documented interval forms as well, while keeping their minimum
        # interval bounded to five minutes.
        daily = _DAILY.fullmatch(normalized_schedule)
        interval = _INTERVAL.fullmatch(normalized_schedule)
        if daily and daily.group(2) == "00":
            raise ScheduleError("el horario debe evitar la hora exacta (minuto 00)")
        if not daily and not interval:
            raise ScheduleError(
                "schedule debe usar 'every day HH:MM' o 'every N minutes|hours|days'"
            )
        if interval:
            amount = int(interval.group(1))
            unit = interval.group(2).lower()
            minutes = amount
            if unit.startswith("hour"):
                minutes *= 60
            elif unit.startswith("day"):
                minutes *= 24 * 60
            if minutes < 5:
                raise ScheduleError("el intervalo mínimo permitido es de cinco minutos")
        normalized_location = str(location or "").strip().lower()
        if not _LOCATION.fullmatch(normalized_location):
            raise ScheduleError("location no es una región válida")
        for label, value in (
            ("destination_dataset", destination_dataset),
            ("destination_table", destination_table),
        ):
            if not _IDENTIFIER.fullmatch(str(value or "")):
                raise ScheduleError(f"{label} debe ser un identificador lower-case seguro")
        if write_disposition != "WRITE_APPEND":
            raise ScheduleError("el piloto solo admite WRITE_APPEND")
        if disabled is not True:
            raise ScheduleError("la TransferConfig del piloto debe quedar deshabilitada")
        return cls(
            schedule=normalized_schedule,
            location=normalized_location,
            destination_dataset=str(destination_dataset),
            destination_table=str(destination_table),
            write_disposition=write_disposition,
            disabled=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
