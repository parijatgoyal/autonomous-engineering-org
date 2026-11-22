from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AEEO_DATA_DIR", ROOT / ".aeeo")))
    provider: str = field(default_factory=lambda: os.environ.get("AEEO_PROVIDER", "mock"))
    model: str = field(default_factory=lambda: os.environ.get("AEEO_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.environ.get("AEEO_EFFORT", "medium"))

    # Bounded autonomy
    max_dev_iterations: int = 3  # develop -> test/security -> fix loops
    max_output_repairs: int = 2  # re-asks when an agent returns malformed output
    max_design_negotiations: int = 1  # architect/developer disagreement rounds
    provider_retries: int = 3  # transient provider errors before pausing the run
    provider_backoff_s: float = 0.5
    command_timeout_s: int = 120

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aeeo.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


# USD per 1M tokens (input, output). Used for cost tracking.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "mock": (4.00, 20.00),  # mock reports what the default model *would* cost
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, PRICING["claude-opus-5-5"])
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
