"""
Cost metering for the Inspection Agent Service (OpenAI-powered).

Calculates real token expenditures and USD costs across autonomous agent turns,
supporting the OpenAI frontier model family (GPT-5.6 Luna / Terra / Sol) and
standard OpenAI models, with prompt cache discounting.

One CostRecord is emitted per phase (recon, codegen) and combined for the
overall job.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# OpenAI Pricing Matrix (per 1,000,000 tokens)
# Sourced from COST_AND_MODEL_COMPARISON_REPORT.md and OpenAI pricing tiers
# --------------------------------------------------------------------------
OPENAI_MODEL_PRICING = {
    # Frontier GPT-5.6 Family
    "gpt-5.6-luna": {
        "input_per_m": 0.20,
        "cached_read_per_m": 0.02,
        "output_per_m": 1.20,
    },
    "gpt-5.6-terra": {
        "input_per_m": 2.00,
        "cached_read_per_m": 0.20,
        "output_per_m": 12.00,
    },
    "gpt-5.6-sol": {
        "input_per_m": 5.00,
        "cached_read_per_m": 0.50,
        "output_per_m": 30.00,
    },
    # Standard OpenAI models
    "gpt-4o": {
        "input_per_m": 2.50,
        "cached_read_per_m": 1.25,
        "output_per_m": 10.00,
    },
    "gpt-4o-mini": {
        "input_per_m": 0.15,
        "cached_read_per_m": 0.075,
        "output_per_m": 0.60,
    },
    "o1": {
        "input_per_m": 15.00,
        "cached_read_per_m": 7.50,
        "output_per_m": 60.00,
    },
    "o3-mini": {
        "input_per_m": 1.10,
        "cached_read_per_m": 0.55,
        "output_per_m": 4.40,
    },
}

DEFAULT_PRICING = OPENAI_MODEL_PRICING["gpt-5.6-luna"]


def get_pricing_for_model(model_name: str | None) -> dict[str, float]:
    if not model_name:
        return DEFAULT_PRICING
    lowered = model_name.lower().strip()
    for key, pricing in OPENAI_MODEL_PRICING.items():
        if key in lowered:
            return pricing
    return DEFAULT_PRICING


def calculate_cost_usd(
    model: str | None,
    input_tokens: int = 0,
    cached_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Computes exact USD cost given input, cached input, and output tokens."""
    pricing = get_pricing_for_model(model)
    # Uncached base input tokens
    base_input = max(0, input_tokens - cached_tokens)
    cost = (
        (base_input / 1_000_000.0) * pricing["input_per_m"]
        + (cached_tokens / 1_000_000.0) * pricing["cached_read_per_m"]
        + (output_tokens / 1_000_000.0) * pricing["output_per_m"]
    )
    return round(cost, 6)


@dataclass
class CostRecord:
    job_id: str
    session_id: str
    recorded_at: str
    subtype: str                    # "success" | "error_max_turns" | "error_max_budget_usd" | ...
    is_error: bool
    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    num_turns: int
    duration_ms: int
    duration_api_ms: int
    model_usage: dict | None        # per-model breakdown
    phase: str = ""                 # "recon" | "codegen" | ""


def build_cost_record(
    job_id: str,
    usage_info: Any,
    phase: str = "",
    session_id: str = "openai-session",
    subtype: str = "success",
    is_error: bool = False,
    num_turns: int = 1,
    duration_ms: int = 0,
    model: str = "gpt-5.6-luna",
) -> CostRecord:
    """
    Builds a structured CostRecord from OpenAI usage or dict metadata.
    """
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0

    if hasattr(usage_info, "prompt_tokens"):
        input_tokens = getattr(usage_info, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage_info, "completion_tokens", 0) or 0
        prompt_details = getattr(usage_info, "prompt_tokens_details", None)
        if prompt_details and hasattr(prompt_details, "cached_tokens"):
            cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0
    elif isinstance(usage_info, dict):
        input_tokens = usage_info.get("input_tokens") or usage_info.get("prompt_tokens") or 0
        output_tokens = usage_info.get("output_tokens") or usage_info.get("completion_tokens") or 0
        cached_tokens = (
            usage_info.get("cache_read_input_tokens")
            or usage_info.get("cached_tokens")
            or (usage_info.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
        subtype = usage_info.get("subtype", subtype)
        is_error = usage_info.get("is_error", is_error)
        num_turns = usage_info.get("num_turns", num_turns)
        duration_ms = usage_info.get("duration_ms", duration_ms)
        session_id = usage_info.get("session_id", session_id)
        model = usage_info.get("model", model)

    total_cost = calculate_cost_usd(
        model=model,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
    )

    model_breakdown = {
        model: {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": total_cost,
        }
    }

    return CostRecord(
        job_id=job_id,
        session_id=session_id,
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        subtype=subtype,
        is_error=is_error,
        total_cost_usd=total_cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached_tokens,
        num_turns=num_turns,
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        model_usage=model_breakdown,
        phase=phase,
    )


def build_fallback_cost_record(job_id: str, status: str, phase: str = "") -> CostRecord:
    """Used when a job phase errored before completion."""
    return CostRecord(
        job_id=job_id,
        session_id="unknown",
        recorded_at=datetime.now().isoformat(timespec="seconds"),
        subtype=f"no_result ({status})",
        is_error=True,
        total_cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
        num_turns=0,
        duration_ms=0,
        duration_api_ms=0,
        model_usage=None,
        phase=phase,
    )


def combine_cost_records(records: list[CostRecord]) -> dict:
    """
    Merge per-phase CostRecords (recon + codegen) into a single flat dict
    preserving top-level summary keys and a 'phases' list.
    """
    if not records:
        return {
            "job_id": None, "session_id": None, "recorded_at": None,
            "subtype": "", "is_error": False, "total_cost_usd": None,
            "input_tokens": None, "output_tokens": None,
            "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
            "num_turns": 0, "duration_ms": 0, "duration_api_ms": 0,
            "model_usage": None, "phases": [],
        }

    def _sum(attr):
        vals = [getattr(r, attr) for r in records if getattr(r, attr) is not None]
        return sum(vals) if vals else None

    return {
        "job_id": records[0].job_id,
        "session_id": [r.session_id for r in records],
        "recorded_at": records[-1].recorded_at,
        "subtype": "; ".join(f"{r.phase or 'unknown'}={r.subtype}" for r in records),
        "is_error": any(r.is_error for r in records),
        "total_cost_usd": _sum("total_cost_usd"),
        "input_tokens": _sum("input_tokens"),
        "output_tokens": _sum("output_tokens"),
        "cache_creation_input_tokens": _sum("cache_creation_input_tokens"),
        "cache_read_input_tokens": _sum("cache_read_input_tokens"),
        "num_turns": sum(r.num_turns for r in records),
        "duration_ms": sum(r.duration_ms for r in records),
        "duration_api_ms": sum(r.duration_api_ms for r in records),
        "model_usage": None,
        "phases": [asdict(r) for r in records],
    }


# --------------------------------------------------------------------------
# Cost Ops sink
# --------------------------------------------------------------------------

class CostSink(ABC):
    @abstractmethod
    def record(self, cost_record: CostRecord) -> None: ...


class JSONLCostSink(CostSink):
    """Default sink: appends one JSON line per job phase to a local ledger file."""

    def __init__(self, path: str = "cost_ledger.jsonl"):
        self.path = Path(path)

    def record(self, cost_record: CostRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(cost_record)) + "\n")


class StdoutCostSink(CostSink):
    """Prints cost records to stdout."""

    def record(self, cost_record: CostRecord) -> None:
        print(f"[cost] job={cost_record.job_id} "
              f"phase={cost_record.phase} "
              f"cost=${cost_record.total_cost_usd or 0:.4f} "
              f"turns={cost_record.num_turns} "
              f"tokens(in/out/cached)={cost_record.input_tokens}/{cost_record.output_tokens}/{cost_record.cache_read_input_tokens}")


# --------------------------------------------------------------------------
# Budget Ceiling Defaults
# --------------------------------------------------------------------------

DEFAULT_MAX_BUDGET_USD = 1.50
DEFAULT_RECON_BUDGET_SHARE = 0.7
SDK_BUDGET_SAFETY_MARGIN = 0.85


def resolve_budget_ceiling(job_max_budget_usd: float | None) -> float:
    if job_max_budget_usd is None or job_max_budget_usd <= 0:
        return DEFAULT_MAX_BUDGET_USD
    return job_max_budget_usd


def split_budget_ceiling(
    total_budget_usd: float, recon_share: float = DEFAULT_RECON_BUDGET_SHARE
) -> tuple[float, float]:
    recon_budget = total_budget_usd * recon_share
    codegen_budget = total_budget_usd - recon_budget
    return recon_budget, codegen_budget
