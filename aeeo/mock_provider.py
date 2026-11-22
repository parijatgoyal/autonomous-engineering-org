"""Deterministic offline provider.

Returns realistic, scripted agent outputs for the bundled sample repository
and requirement (discount codes), so the full workflow - including real git
branches, real pytest/ruff/bandit runs - can be demonstrated without an API
key. Faults are injected by name to exercise every exception path:

  buggy_first_impl      developer's first attempt fails an acceptance test
  security_issue        developer's first attempt uses eval() -> critical finding
  security_persistent   developer never removes eval() -> escalates to a human
  disagreement          architect and developer disagree; architect accepts revision
  disagreement_deadlock architect and developer disagree; nobody yields -> human
  malformed_output      analyst returns broken JSON first, then repairs it
  provider_outage       provider goes down at the security stage -> run pauses
  scope_violation       developer also edits an unrelated service file
  stuck_developer       developer resubmits the same failing change -> loop stopped
"""

from __future__ import annotations

import json
import time

from .llm import LLMResponse, ProviderUnavailable

FAULTS = {
    "buggy_first_impl": "Developer's first attempt fails an acceptance test (case-sensitive codes)",
    "security_issue": "Developer's first attempt uses eval() - critical security finding",
    "security_persistent": "Developer keeps using eval() - escalates to a human",
    "disagreement": "Architect and developer disagree on design; resolved by negotiation",
    "disagreement_deadlock": "Architect and developer disagree; escalated to a human",
    "malformed_output": "Analyst returns malformed JSON first, then repairs it",
    "provider_outage": "Model provider unavailable mid-workflow; run pauses, resume continues",
    "scope_violation": "Developer tries to modify an unrelated service file",
    "stuck_developer": "Developer repeats the same failing change; loop is stopped",
}

DISCOUNTS_PATH = "services/pricing/discounts.py"
PRICING_PATH = "services/pricing/pricing.py"
TESTS_PATH = "tests/test_discount_codes.py"

GOOD_DISCOUNTS = '''"""Promotional discount codes."""

from __future__ import annotations

from decimal import Decimal


class InvalidDiscountError(ValueError):
    """Raised when a discount code is not recognised."""


PERCENT_OFF = {"SAVE10": Decimal("0.10")}
AMOUNT_OFF = {"FLAT5": Decimal("5.00")}


def normalize_code(code: str) -> str:
    return code.strip().upper()


def apply_discount(amount: Decimal, code: str) -> Decimal:
    """Apply a discount code to a pre-tax amount. Never returns a negative value."""
    key = normalize_code(code)
    if key in PERCENT_OFF:
        discounted = amount * (1 - PERCENT_OFF[key])
    elif key in AMOUNT_OFF:
        discounted = amount - AMOUNT_OFF[key]
    else:
        raise InvalidDiscountError(f"unknown discount code: {code!r}")
    return max(discounted, Decimal("0"))
'''

# Bug: no normalisation, so " save10 " is rejected (violates AC3).
BUGGY_DISCOUNTS = GOOD_DISCOUNTS.replace(
    '''def normalize_code(code: str) -> str:
    return code.strip().upper()
''',
    '''def normalize_code(code: str) -> str:
    return code
''',
)

# Security problem: discount rules evaluated with eval().
EVAL_DISCOUNTS = '''"""Promotional discount codes."""

from __future__ import annotations

from decimal import Decimal


class InvalidDiscountError(ValueError):
    """Raised when a discount code is not recognised."""


# Rules are expressions so marketing can add codes without a deploy.
RULES = {
    "SAVE10": "amount * (1 - Decimal('0.10'))",
    "FLAT5": "amount - Decimal('5.00')",
}


def apply_discount(amount: Decimal, code: str) -> Decimal:
    key = code.strip().upper()
    if key not in RULES:
        raise InvalidDiscountError(f"unknown discount code: {code!r}")
    discounted = eval(RULES[key])
    return max(discounted, Decimal("0"))
'''

PRICING_WITH_DISCOUNT_MODULE = '''"""Order pricing: subtotal, tax and total."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from services.pricing.discounts import apply_discount

TAX_RATE = Decimal("0.08")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def subtotal(items: list[LineItem]) -> Decimal:
    for item in items:
        if item.quantity <= 0:
            raise ValueError(f"quantity must be positive for {item.sku}")
        if item.unit_price < 0:
            raise ValueError(f"unit price must not be negative for {item.sku}")
    return _money(sum((i.unit_price * i.quantity for i in items), Decimal("0")))


def calculate_total(
    items: list[LineItem],
    tax_rate: Decimal = TAX_RATE,
    discount_code: str | None = None,
) -> Decimal:
    """Return the order total including tax, rounded to cents.

    A discount code, if given, is applied to the subtotal before tax.
    """
    base = subtotal(items)
    if discount_code is not None:
        base = apply_discount(base, discount_code)
    return _money(base * (1 + tax_rate))
'''

UNRELATED_EMAIL_EDIT = '''"""Outbound email (stubbed: records messages instead of sending)."""

from __future__ import annotations

from decimal import Decimal

OUTBOX: list[dict] = []


def send_order_confirmation(to: str, order_id: str, total: Decimal, discount_code: str | None = None) -> None:
    subject = f"Order {order_id} confirmed" + (f" (code {discount_code})" if discount_code else "")
    OUTBOX.append({"to": to, "subject": subject, "total": str(total)})
'''

ACCEPTANCE_TESTS = '''"""Acceptance tests for discount codes (authored by the QA agent)."""

from decimal import Decimal

import pytest

from services.pricing.discounts import InvalidDiscountError, apply_discount
from services.pricing.pricing import LineItem, calculate_total


def _items(price: str = "100.00", qty: int = 1) -> list[LineItem]:
    return [LineItem("SKU-1", Decimal(price), qty)]


def test_ac1_save10_takes_ten_percent_off_before_tax():
    # 100.00 - 10% = 90.00, +8% tax = 97.20
    assert calculate_total(_items(), discount_code="SAVE10") == Decimal("97.20")


def test_ac2_flat5_takes_five_off_before_tax():
    # 100.00 - 5.00 = 95.00, +8% tax = 102.60
    assert calculate_total(_items(), discount_code="FLAT5") == Decimal("102.60")


def test_ac3_code_matching_ignores_case_and_whitespace():
    assert calculate_total(_items(), discount_code="  save10 ") == Decimal("97.20")


def test_ac4_unknown_code_is_rejected():
    with pytest.raises(InvalidDiscountError):
        calculate_total(_items(), discount_code="BOGUS")


def test_ac5_total_never_below_zero():
    assert calculate_total(_items("3.00"), discount_code="FLAT5") == Decimal("0.00")


def test_ac6_no_code_prices_exactly_as_before():
    assert calculate_total(_items()) == Decimal("108.00")


def test_apply_discount_percent_on_raw_amount():
    assert apply_discount(Decimal("50.00"), "SAVE10") == Decimal("45.00")
'''


def _tokens(s: str) -> int:
    return max(1, len(s) // 4)


class MockProvider:
    name = "mock"
    model = "mock"

    def __init__(self, faults: list[str] | None = None, latency_s: float = 0.02):
        unknown = set(faults or []) - set(FAULTS)
        if unknown:
            raise ValueError(f"unknown faults: {sorted(unknown)}")
        self.faults = set(faults or [])
        self.latency_s = latency_s

    def complete(self, *, agent: str, system: str, user: str, schema: dict, ctx: dict) -> LLMResponse:
        t0 = time.perf_counter()
        time.sleep(self.latency_s)
        if "provider_outage" in self.faults and agent == "security":
            raise ProviderUnavailable("503 Service Unavailable (simulated outage)")
        handler = getattr(self, f"_{agent}")
        out = handler(ctx)
        text = out if isinstance(out, str) else json.dumps(out)
        return LLMResponse(
            text=text,
            model="mock",
            input_tokens=_tokens(system) + _tokens(user),
            output_tokens=_tokens(text),
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    # ------------------------------------------------------------ analyst
    def _analyst(self, ctx: dict):
        if "malformed_output" in self.faults and ctx.get("attempt") == 1:
            # Chatty preamble + missing required fields: must be rejected and repaired.
            return 'Sure! Here is the analysis:\n{"summary": "discount codes", "acceptance_criteria": "TBD"'
        return {
            "summary": "Allow shoppers to apply a promotional discount code to the pre-tax subtotal at checkout.",
            "acceptance_criteria": [
                {"id": "AC1", "given": "an order with subtotal 100.00", "when": "code SAVE10 is applied",
                 "then": "10% is deducted before 8% tax, total 97.20"},
                {"id": "AC2", "given": "an order with subtotal 100.00", "when": "code FLAT5 is applied",
                 "then": "5.00 is deducted before tax, total 102.60"},
                {"id": "AC3", "given": "a valid code typed with different case or surrounding spaces",
                 "when": "it is applied", "then": "it is accepted as the canonical code"},
                {"id": "AC4", "given": "an unknown code", "when": "it is applied",
                 "then": "InvalidDiscountError is raised and no total is produced"},
                {"id": "AC5", "given": "a fixed discount larger than the subtotal", "when": "it is applied",
                 "then": "the total is 0.00, never negative"},
                {"id": "AC6", "given": "no discount code", "when": "the order is priced",
                 "then": "the total is identical to current behaviour"},
            ],
            "tasks": [
                {"id": "T1", "title": "Identify impacted components and design", "owner_agent": "architect",
                 "description": "Locate pricing logic and decide where discount rules live."},
                {"id": "T2", "title": "Implement discount application", "owner_agent": "developer",
                 "description": "Add discount rules and apply them in calculate_total before tax."},
                {"id": "T3", "title": "Acceptance tests for AC1-AC6", "owner_agent": "qa",
                 "description": "One test per acceptance criterion plus regression run."},
                {"id": "T4", "title": "Security review of the change", "owner_agent": "security",
                 "description": "Static analysis and manual review of new code paths."},
                {"id": "T5", "title": "Assemble release evidence", "owner_agent": "release",
                 "description": "Summarise tests, risks and rollback for human approval."},
            ],
            "assumptions": [
                "Discount codes are static configuration for now (no database or admin UI).",
                "Only one code can be applied per order.",
                "Rounding follows the existing ROUND_HALF_UP to cents.",
            ],
            "open_questions": ["Should discount usage be reported in the order confirmation email?"],
        }

    # ---------------------------------------------------------- architect
    def _design(self, separate_module: bool, candidates: list[dict]) -> dict:
        readers = []
        for c in candidates:
            if c["path"] != PRICING_PATH and PRICING_PATH.replace("/", ".")[:-3] in " ".join(c.get("imports", [])):
                readers.append(c["path"])
        components = [
            {"path": PRICING_PATH, "reason": "Owns calculate_total(); discount must apply before tax here.",
             "change_type": "modify"},
        ]
        if separate_module:
            components.append({"path": DISCOUNTS_PATH, "reason": "New home for discount rules and validation.",
                               "change_type": "create"})
        components += [{"path": p, "reason": "Calls calculate_total(); must keep working unchanged.",
                        "change_type": "read_only"} for p in readers]
        return {
            "impacted_components": components,
            "design": (
                "Add an optional `discount_code` parameter to `calculate_total` (default None keeps current "
                "behaviour). "
                + ("Discount rules and validation live in a new `services/pricing/discounts.py` module exposing "
                   "`apply_discount(amount, code)` and `InvalidDiscountError`. "
                   if separate_module else
                   "Keep discount rules inline in pricing.py as a small dict to minimise surface area. ")
                + "Apply the discount to the subtotal before tax and clamp at zero."
            ),
            "files_to_modify": [PRICING_PATH],
            "files_to_create": [DISCOUNTS_PATH] if separate_module else [],
            "test_strategy": "One acceptance test per AC in tests/test_discount_codes.py; full regression suite.",
            "risks": ["Rounding differences when discount and tax combine.",
                      "Checkout callers must not break - new parameter is optional."],
        }

    def _architect(self, ctx: dict):
        conflict = self.faults & {"disagreement", "disagreement_deadlock"}
        return self._design(separate_module=not conflict, candidates=ctx.get("candidates", []))

    def _architect_reconcile(self, ctx: dict):
        if "disagreement_deadlock" in self.faults:
            return {"accept_developer_proposal": False,
                    "rationale": "A new module adds surface area for a two-entry table; keep it inline.",
                    "revised_design": None}
        return {"accept_developer_proposal": True,
                "rationale": "Agreed: a dedicated module keeps pricing.py focused and is easier to test.",
                "revised_design": self._design(separate_module=True, candidates=ctx.get("candidates", []))}

    # ---------------------------------------------------------- developer
    def _developer(self, ctx: dict):
        design = ctx["design"]
        iteration = ctx.get("iteration", 1)
        if DISCOUNTS_PATH not in design.get("files_to_create", []):
            return {
                "agrees_with_design": False,
                "design_concerns": [
                    "Inline rules in pricing.py mix configuration with arithmetic and cannot be validated "
                    "or tested in isolation; InvalidDiscountError also needs a public home.",
                ],
                "proposed_files_to_create": [DISCOUNTS_PATH],
                "edits": [],
                "summary": "Requesting design change before implementing.",
            }

        discounts = GOOD_DISCOUNTS
        if "stuck_developer" in self.faults:
            discounts = BUGGY_DISCOUNTS
        elif "buggy_first_impl" in self.faults and iteration == 1:
            discounts = BUGGY_DISCOUNTS
        elif "security_persistent" in self.faults:
            discounts = EVAL_DISCOUNTS.replace("# Rules are", f"# (rev {iteration}) Rules are")
        elif "security_issue" in self.faults and iteration == 1:
            discounts = EVAL_DISCOUNTS

        edits = [
            {"path": DISCOUNTS_PATH, "content": discounts},
            {"path": PRICING_PATH, "content": PRICING_WITH_DISCOUNT_MODULE},
        ]
        if "scope_violation" in self.faults and iteration == 1:
            edits.append({"path": "services/notifications/email.py", "content": UNRELATED_EMAIL_EDIT})
        summary = "Added discounts module and optional discount_code on calculate_total."
        if ctx.get("feedback"):
            summary += f" Addressed feedback from iteration {iteration - 1}."
        return {"agrees_with_design": True, "design_concerns": [], "proposed_files_to_create": [],
                "edits": edits, "summary": summary}

    # ----------------------------------------------------------------- qa
    def _qa(self, ctx: dict):
        return {
            "test_plan": "Black-box tests through calculate_total for each AC, plus one unit test of "
                         "apply_discount. Existing suite re-run as regression.",
            "test_files": [{"path": TESTS_PATH, "content": ACCEPTANCE_TESTS}],
            "ac_coverage": [
                {"ac_id": "AC1", "tests": ["test_ac1_save10_takes_ten_percent_off_before_tax",
                                           "test_apply_discount_percent_on_raw_amount"]},
                {"ac_id": "AC2", "tests": ["test_ac2_flat5_takes_five_off_before_tax"]},
                {"ac_id": "AC3", "tests": ["test_ac3_code_matching_ignores_case_and_whitespace"]},
                {"ac_id": "AC4", "tests": ["test_ac4_unknown_code_is_rejected"]},
                {"ac_id": "AC5", "tests": ["test_ac5_total_never_below_zero"]},
                {"ac_id": "AC6", "tests": ["test_ac6_no_code_prices_exactly_as_before",
                                           "test_total_includes_tax"]},
            ],
        }

    # ----------------------------------------------------------- security
    def _security(self, ctx: dict):
        findings = [dict(f, source="agent-triage") for f in ctx.get("scanner_findings", [])
                    if f["severity"] in ("critical", "high")]
        extra = []
        if "Decimal(\"5.00\")" in ctx.get("diff", "") or "FLAT5" in ctx.get("diff", ""):
            extra.append({"severity": "low", "title": "Discount codes are guessable static strings",
                          "file": DISCOUNTS_PATH, "line": 1, "source": "agent",
                          "recommendation": "Track redemption and rate-limit code attempts at the API layer."})
        blocking = any(f["severity"] in ("critical", "high") for f in findings)
        return {
            "findings": findings + extra,
            "verdict": "block" if blocking else "pass",
            "summary": ("Blocking: dynamic code evaluation reachable from configuration."
                        if blocking else "No blocking issues. One low-severity business-logic note."),
        }

    # ------------------------------------------------------------ release
    def _release(self, ctx: dict):
        tests = ctx.get("tests", {})
        return {
            "review_summary": (
                f"Discount codes implemented across {len(ctx.get('changed_files', []))} files after "
                f"{ctx.get('iterations')} development iteration(s). All acceptance criteria are covered by "
                "automated tests."
            ),
            "test_evidence": f"{tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed; lint "
                             f"{'clean' if ctx.get('lint_ok') else 'has issues'}.",
            "known_risks": [
                "Static code table: adding a code requires a deploy.",
                "No redemption limits or expiry dates yet.",
                "Checkout does not yet pass a discount code through (follow-up).",
            ],
            "rollback_plan": "Revert the merge commit; parameter is optional so callers are unaffected.",
            "recommendation": "approve" if tests.get("failed", 1) == 0 else "request_changes",
        }
