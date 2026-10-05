"""Fenêtres quotidiennes et état persistant du check-in."""

from datetime import datetime, timedelta, time
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    url = config.get("channel_url", "")
    if not isinstance(url, str) or not re.fullmatch(r"https://discord\.com/channels/[1-9][0-9]*/[1-9][0-9]*/?", url):
        raise ValueError("Colle le lien du salon : https://discord.com/channels/ID_SERVEUR/ID_SALON")
    config["channel_url"] = url.rstrip("/")
    message = config.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        raise ValueError("Le message doit contenir entre 1 et 2000 caractères.")
    if message == "REMPLACE PAR TON CHECK-IN EXACT":
        raise ValueError("Remplace le message d'exemple par ton vrai check-in.")
    opening = config.get("opening_time", "20:00")
    if not isinstance(opening, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", opening):
        raise ValueError("opening_time doit être au format HH:MM (par exemple 20:00).")
    config["opening_time"] = opening
    config["timezone"] = config.get("timezone", "Indian/Antananarivo")
    ZoneInfo(config["timezone"])
    for key, default, low, high in (
        ("minutes_before", 5, 0, 180),
        ("minutes_after", 15, 1, 180),
        ("check_interval_seconds", 2, 1, 60),
    ):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} doit être compris entre {low} et {high}.")
        config[key] = value
    return config


def window(config: dict, now: datetime) -> tuple[datetime, datetime, str]:
    """Fenêtre active, sinon la prochaine, et date du check-in (Madagascar)."""
    zone = ZoneInfo(config["timezone"])
    local = now.astimezone(zone)
    opening = time.fromisoformat(config["opening_time"])
    for offset in (-1, 0, 1):
        day = local.date() + timedelta(days=offset)
        target = datetime.combine(day, opening, tzinfo=zone)
        start = target - timedelta(minutes=config["minutes_before"])
        end = target + timedelta(minutes=config["minutes_after"])
        if local < end:
            return start, end, day.isoformat()
    raise ValueError("Fenêtre introuvable.")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class DailyState:
    def __init__(self, path: Path):
        self.path = path
        self.records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(self.records, dict) or not all(
            isinstance(key, str) and value in ("reserved", "confirmed")
            for key, value in self.records.items()
        ):
            raise ValueError("state.json invalide. Vérifie-le avant de relancer.")

    def key(self, url: str, day: str) -> str:
        return f"{url}|{day}"

    def blocked(self, url: str, day: str) -> bool:
        return self.key(url, day) in self.records

    def record(self, url: str, day: str, status: str) -> None:
        self.records[self.key(url, day)] = status
        write_json(self.path, self.records)


@contextmanager
def project_lock(path: Path):
    """Verrou libéré par le système même en cas de crash du programme."""
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Une autre instance est déjà active. Ferme-la avant de relancer.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
