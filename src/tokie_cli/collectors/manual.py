"""Manual collector for web-only AI tools.

Many AI products — Manus, WisperFlow, v0, Gemini web, ChatGPT web, Claude.ai,
Perplexity, etc. — produce no local signal we can parse. The user is the only
one who knows they happened. This collector reads user-maintained drop files
(CSV or YAML) under ``$TOKIE_DATA_HOME/manual/`` (or explicit paths) and emits
:class:`UsageEvent`\\ s with :attr:`Confidence.INFERRED`.

Rows that cannot be parsed are *skipped with a warning*, never crashed on, so
a single bad line in a user's log never blocks an entire scan. ``raw_hash`` is
derived from the row's content so re-imports dedupe deterministically.

See ``manual_templates/README.md`` for the header contract and examples.
"""

from __future__ import annotations

import csv
import logging
import os
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from tokie_cli.collectors.base import Collector, CollectorHealth, aiterate
from tokie_cli.config import data_dir
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

logger = logging.getLogger(__name__)

_ENV_VAR = "TOKIE_MANUAL_LOG"
_SUPPORTED_SUFFIXES = (".csv", ".yaml", ".yml")
_REQUIRED_FIELDS = ("occurred_at", "provider", "product", "model")


def _default_manual_dir() -> Path:
    """Return ``$TOKIE_DATA_HOME/manual`` — the auto-discovered drop folder."""

    return data_dir() / "manual"


def _parse_timestamp(value: Any) -> datetime | None:
    """Return a tz-aware UTC datetime, or ``None`` if the input is naive/garbage.

    Accepts either a pre-parsed ``datetime`` (YAML) or an ISO-8601 string (CSV).
    Naive datetimes are rejected — users must include a timezone (typically
    ``Z``) so we never silently misattribute usage to the wrong day.
    """

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _coerce_int(value: Any, *, default: int = 0) -> int | None:
    """Parse ``value`` as a non-negative int, or return ``None`` on failure."""

    if value is None or value == "":
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _coerce_float(value: Any) -> float | None:
    """Parse ``value`` as a non-negative float, or ``None`` if absent/invalid."""

    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _redact_commas(text: str) -> str:
    """Strip characters that would confuse a comma-delimited ``source`` string."""

    return text.replace(",", ";").replace("\n", " ").replace("\r", " ").strip()


class ManualCollector(Collector):
    """Collector for user-maintained CSV/YAML logs of web-only AI usage."""

    name = "manual"
    default_confidence = Confidence.INFERRED

    def __init__(
        self,
        *,
        log_paths: tuple[Path, ...] = (),
    ) -> None:
        self.log_paths: tuple[Path, ...] = tuple(log_paths)

    @classmethod
    def detect(cls) -> bool:
        """True if the env var points somewhere real or the default dir has files."""

        env_value = os.environ.get(_ENV_VAR)
        if env_value:
            env_path = Path(env_value).expanduser()
            if env_path.exists():
                return True

        default = _default_manual_dir()
        if default.exists() and default.is_dir():
            for suffix in _SUPPORTED_SUFFIXES:
                try:
                    next(default.glob(f"*{suffix}"))
                except StopIteration:
                    continue
                else:
                    return True
        return False

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield events from every readable CSV/YAML file we can find."""

        return aiterate(self._iter_events(since))

    def health(self) -> CollectorHealth:
        """Report how many manual log files are discoverable right now."""

        files = list(self._discover_files())
        detected = bool(files) or self.detect()
        if not detected:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=(
                    "no manual logs found; drop a CSV/YAML into "
                    f"{_default_manual_dir()} or set {_ENV_VAR}"
                ),
            )
        warnings: list[str] = []
        for path in files:
            if not os.access(path, os.R_OK):
                warnings.append(f"unreadable: {path.name}")
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=not warnings,
            last_scan_at=None,
            last_scan_events=0,
            message=f"{len(files)} manual log file(s)",
            warnings=tuple(warnings),
        )

    def _discover_files(self) -> Iterator[Path]:
        """Yield every CSV/YAML file reachable through configured sources."""

        seen: set[Path] = set()
        candidates: list[Path] = list(self.log_paths)
        candidates.append(_default_manual_dir())
        env_value = os.environ.get(_ENV_VAR)
        if env_value:
            candidates.append(Path(env_value).expanduser())

        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.is_file():
                if candidate.suffix.lower() in _SUPPORTED_SUFFIXES:
                    yield from _yield_once(candidate, seen)
                continue
            if candidate.is_dir():
                for suffix in _SUPPORTED_SUFFIXES:
                    for path in sorted(candidate.glob(f"*{suffix}")):
                        yield from _yield_once(path, seen)

    def _iter_events(self, since: datetime | None) -> Iterator[UsageEvent]:
        for path in self._discover_files():
            suffix = path.suffix.lower()
            try:
                rows: Iterable[tuple[int, Mapping[str, Any]]] = (
                    _read_csv(path) if suffix == ".csv" else _read_yaml(path)
                )
            except OSError as exc:
                logger.warning("manual: cannot open %s (%s)", path.name, type(exc).__name__)
                continue
            except yaml.YAMLError as exc:
                logger.warning("manual: invalid yaml in %s (%s)", path.name, type(exc).__name__)
                continue

            for lineno, row in rows:
                event = self._row_to_event(row, path=path, lineno=lineno)
                if event is None:
                    continue
                if since is not None and event.occurred_at < since:
                    continue
                yield event

    def _row_to_event(
        self,
        row: Mapping[str, Any],
        *,
        path: Path,
        lineno: int,
    ) -> UsageEvent | None:
        missing = [f for f in _REQUIRED_FIELDS if not _nonempty(row.get(f))]
        if missing:
            logger.warning(
                "manual: %s:%d missing required field(s) %s",
                path.name,
                lineno,
                ",".join(missing),
            )
            return None

        occurred_at = _parse_timestamp(row.get("occurred_at"))
        if occurred_at is None:
            logger.warning(
                "manual: %s:%d naive or unparsable timestamp; skipping",
                path.name,
                lineno,
            )
            return None

        provider = str(row["provider"]).strip()
        product = str(row["product"]).strip()
        model = str(row["model"]).strip()
        account_id_raw = row.get("account_id")
        account_id = str(account_id_raw).strip() if _nonempty(account_id_raw) else "default"

        input_tokens = _coerce_int(row.get("input_tokens"), default=0)
        if input_tokens is None:
            logger.warning("manual: %s:%d invalid input_tokens; skipping", path.name, lineno)
            return None

        messages_value = row.get("messages")
        if _nonempty(messages_value):
            output_tokens = _coerce_int(messages_value, default=0)
        else:
            output_tokens = _coerce_int(row.get("output_tokens"), default=0)
        if output_tokens is None:
            logger.warning("manual: %s:%d invalid output_tokens; skipping", path.name, lineno)
            return None

        cost_usd = _coerce_float(row.get("cost_usd"))

        notes_raw = row.get("notes")
        notes = str(notes_raw).strip() if _nonempty(notes_raw) else ""

        raw_hash = compute_raw_hash(
            {
                "occurred_at": occurred_at.isoformat(),
                "provider": provider,
                "product": product,
                "account_id": account_id,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "notes": notes,
            }
        )

        source = f"manual:{path.name}:{lineno}"
        if notes:
            source = f"{source}[{_redact_commas(notes)}]"

        try:
            return self.make_event(
                occurred_at=occurred_at,
                provider=provider,
                product=product,
                account_id=account_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                raw_hash=raw_hash,
                source=source,
            )
        except ValueError as exc:
            logger.warning(
                "manual: %s:%d rejected by schema (%s)",
                path.name,
                lineno,
                type(exc).__name__,
            )
            return None


def _nonempty(value: Any) -> bool:
    """True if ``value`` is neither ``None`` nor an empty/whitespace string."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _yield_once(path: Path, seen: set[Path]) -> Iterator[Path]:
    """Yield ``path`` exactly once across repeated discovery passes."""

    try:
        key = path.resolve()
    except OSError:
        key = path
    if key in seen:
        return
    seen.add(key)
    yield path


def _read_csv(path: Path) -> Iterator[tuple[int, Mapping[str, Any]]]:
    """Yield ``(line_number, row)`` pairs from a CSV with a header row."""

    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            yield reader.line_num, row


def _read_yaml(path: Path) -> Iterator[tuple[int, Mapping[str, Any]]]:
    """Yield ``(index, row)`` pairs from a YAML document.

    Accepts either a top-level ``entries:`` list or a bare top-level list.
    Each entry must be a mapping; non-mapping entries are skipped with a
    warning rather than raising.
    """

    with path.open("r", encoding="utf-8") as fp:
        payload = yaml.safe_load(fp)

    if payload is None:
        return
    entries = payload.get("entries", []) if isinstance(payload, dict) else payload

    if not isinstance(entries, list):
        logger.warning("manual: %s has no 'entries' list; skipping", path.name)
        return

    for idx, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            logger.warning("manual: %s entry %d is not a mapping; skipping", path.name, idx)
            continue
        yield idx, entry


__all__ = ["ManualCollector"]
