"""App options.

The Supervisor writes the options the owner set in the Home Assistant UI to
``/data/options.json`` (already type-checked against the ``schema`` in
``config.yaml``). This module re-validates them, applies cross-field rules, and
makes sure the API key can never leak through ``repr``, logs or the API.

If the options are invalid the app does not crash: it starts in *safe mode*
(built-in defaults, dry-run forced on) and shows the errors in the dashboard.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from . import paths

log = logging.getLogger(__name__)


class ModelPrice(BaseModel):
    """USD per million tokens for one model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1, max_length=100)
    input: float = Field(ge=0, le=1000)
    output: float = Field(ge=0, le=1000)
    cache_write_5m: float = Field(ge=0, le=1000)
    cache_write_1h: float = Field(ge=0, le=1000)
    cache_read: float = Field(ge=0, le=1000)

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        return value.strip()


# Defaults from https://platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-09-27). Prices change: the owner should verify them and can
# edit them in the app options. Must match the defaults in config.yaml.
DEFAULT_PRICE_TABLE: tuple[ModelPrice, ...] = (
    ModelPrice(model="claude-sonnet-5", input=2.0, output=10.0, cache_write_5m=2.5, cache_write_1h=4.0, cache_read=0.2),
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    anthropic_api_key: SecretStr = SecretStr("")
    agent_name: str = Field(default="Ember", min_length=1, max_length=40)
    starting_balance_usd: float = Field(default=20.0, ge=0, le=100_000)
    daily_spend_cap_usd: float = Field(default=1.0, ge=0, le=1_000)
    cycle_spend_cap_usd: float = Field(default=0.25, ge=0, le=1_000)
    wake_interval_minutes: int = Field(default=240, ge=5, le=10_080)
    min_sleep_minutes: int = Field(default=30, ge=5, le=10_080)
    max_sleep_minutes: int = Field(default=1_440, ge=5, le=10_080)
    max_tool_steps: int = Field(default=15, ge=1, le=100)
    planner_model: str = Field(default="claude-sonnet-5", min_length=1, max_length=100)
    worker_model: str = Field(default="claude-sonnet-5", min_length=1, max_length=100)
    price_table: tuple[ModelPrice, ...] = DEFAULT_PRICE_TABLE
    web_search_usd_per_1000: float = Field(default=10.0, ge=0, le=1_000)
    dry_run: bool = True
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _strip_key(cls, value: Any) -> Any:
        # A key pasted with a stray space or newline would otherwise look "set"
        # but fail every API call.
        return value.strip() if isinstance(value, str) else value

    @field_validator("agent_name", "planner_model", "worker_model", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Settings:
        problems: list[str] = []
        if self.cycle_spend_cap_usd > self.daily_spend_cap_usd:
            problems.append("cycle_spend_cap_usd must not be larger than daily_spend_cap_usd")
        if self.min_sleep_minutes > self.max_sleep_minutes:
            problems.append("min_sleep_minutes must not be larger than max_sleep_minutes")
        elif not self.min_sleep_minutes <= self.wake_interval_minutes <= self.max_sleep_minutes:
            problems.append("wake_interval_minutes must be between min_sleep_minutes and max_sleep_minutes")
        names = [p.model for p in self.price_table]
        if len(names) != len(set(names)):
            problems.append("price_table lists the same model more than once")
        for role in ("planner_model", "worker_model"):
            if self.price_for(getattr(self, role)) is None:
                problems.append(f"{role} '{getattr(self, role)}' has no entry in price_table")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def price_for(self, model: str) -> ModelPrice | None:
        """Exact match only: an unpriced model must never be treated as free."""
        for price in self.price_table:
            if price.model == model:
                return price
        return None

    @property
    def api_key_set(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value().strip())

    def public_dict(self) -> dict[str, Any]:
        """Options safe to show in the dashboard. The API key is replaced by a flag."""
        data = self.model_dump(mode="json", exclude={"anthropic_api_key"})
        data["anthropic_api_key_set"] = self.api_key_set
        return data


@dataclass(frozen=True)
class LoadedSettings:
    settings: Settings
    errors: list[str] = field(default_factory=list)
    source: str = "defaults"

    @property
    def safe_mode(self) -> bool:
        return bool(self.errors)


def _format_validation_error(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in err["loc"]) or "options"
        messages.append(f"{location}: {err['msg']}")
    return messages


def load_settings(path: Path | None = None) -> LoadedSettings:
    """Read and validate the options file. Never raises."""
    path = path or paths.options_path()
    if not path.exists():
        return LoadedSettings(Settings(), source="defaults (no options file)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("options file must contain a JSON object")
    except (OSError, ValueError) as exc:
        return _safe_mode([f"could not read {path.name}: {exc}"])
    try:
        return LoadedSettings(Settings.model_validate(raw), source=str(path))
    except ValidationError as exc:
        return _safe_mode(_format_validation_error(exc))


def _safe_mode(errors: list[str]) -> LoadedSettings:
    for error in errors:
        log.error("Invalid option: %s", error)
    log.error("Starting in safe mode: built-in defaults, dry-run forced on.")
    return LoadedSettings(Settings(dry_run=True), errors=errors, source="safe mode (built-in defaults)")
