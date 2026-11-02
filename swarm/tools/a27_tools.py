"""A27 Smart Contract Auditor tools: pure analyses over supplied audit facts (no keys, no signing, no network).

Tools: audit_classify_vulnerability (SWC-registry class and default severity per signal), audit_score_severity (OWASP-style
impact x likelihood matrix, L3 override flag), audit_reentrancy_order (checks-effects-interactions over a call/state trace),
audit_oracle_risk (oracle type, TWAP window, constant-product attack cost versus profit), audit_flashloan_profit
(constant-product price impact and break-even of a flash-funded manipulation), audit_invariant_coverage (invariants versus
state variables and functions), audit_fuzz_adequacy (runs, depth, seeds, coverage), audit_signature_replay (nonce, chain id,
deadline, domain separation), audit_role_centralization (privileged powers versus timelock and multisig),
audit_vault_inflation (ERC-4626 share rounding and first-depositor donation attack), audit_dedupe_findings (duplicate
findings and severity conflicts), audit_report_completeness (sections, PoC, fix status), audit_fix_verification (fix
verification and residual risk).

Severity scale of every finding: info < low < medium < high < critical. A verdict is "fail" when any finding is high or
critical, "warn" when the worst is medium, otherwise "pass". The auditor reports: it never changes a severity (an override
needs L3 approval) and never accepts risk (acknowledged or won't-fix on high/critical needs an L4 acceptance).

Standards versus house policy. What follows is stated by a primary source (checked against it):
* SWC Registry, https://swcregistry.io (SWC-100 .. SWC-136 identifiers and titles). The registry says it has not been
  thoroughly updated since 2020 and points to the EEA EthTrust Security Levels specification for maintained guidance. It
  carries no severities: the default severity of every signal below is this module's own triage starting point (several
  pre-date Solidity 0.8 checked arithmetic, explicit visibility and EIP-6780); audit_score_severity gives the contextual one.
* OWASP Risk Rating Methodology, https://owasp.org/www-community/OWASP_Risk_Rating_Methodology: the 3 x 3 overall-severity
  matrix (its "Note" cell is called info here). The mapping from loss, attacker privilege and preconditions to the impact and
  likelihood LEVELS (10 % / 1 % of TVL, preconditions penalty, irrecoverable bump) is this module's policy, not OWASP's.
* ERC-4626 / OpenZeppelin ERC4626 virtual shares (shares = assets * (supply + 10**offset) / (totalAssets + 1), rounding
  down; assets = shares * (totalAssets + 1) / (supply + 10**offset)), https://eips.ethereum.org/EIPS/eip-4626. "legacy" is
  the pre-virtual-shares Solmate/OpenZeppelin 4.x conversion: first deposit 1:1, then assets * supply / totalAssets.
* Uniswap V2 constant-product swap with fee (amountOut = in*(1-fee)*reserveOut / (reserveIn + in*(1-fee))), reserves grow by
  the full amount in. Aave V3 flash-loan premium: 0.05 % when the pool is initialized, changeable by governance per
  deployment (the default of 5 bps here is that initial value, read FLASHLOAN_PREMIUM_TOTAL of the real pool).
* Chainlink data feeds: updatedAt staleness; minAnswer/maxAnswer are "no longer used on most Data Feeds" (Data Feeds API
  reference), so a contract needs its own sanity bounds; L2 Sequencer Uptime Feeds (answer 0 up, 1 down) plus a grace period.

Modelling assumptions that bound what a result means: pools are balanced constant-product (no concentrated liquidity, no
stable curves, no routing, no MEV competition); a TWAP is the arithmetic mean over the window (Uniswap V2 cumulative price;
Uniswap V3 averages ticks, a geometric-mean price, so the same upward move needs a larger push and a TWAP cost here is a lower bound); a "not
profitable" result is not a safety proof. House policy, not a standard: TWAP window floor of 1800 s, 48 h timelock,
strong multisig (>= 3 needed, >= 5 signers, majority), the fuzz minimums, the privileged-power base severities and the
residual-risk weights 16/8/4/2/0. Embedded tables are small and pinned in tests/test_tools_a27.py.

Echoed text: finding ids, summaries, error messages and listed names repeat caller-chosen identifiers. Every such string goes
through _clean(), which replaces secret-looking spans (provider tokens, URL credentials, 64-hex keys, 12+ word mnemonics)
with [REDACTED]; no tool echoes a numeric or free-text input otherwise.
"""
from __future__ import annotations

import math
import re
import string as _string
import sys
from decimal import Decimal
from fractions import Fraction
from typing import Any, TypeVar

from swarm.redact import redact_text
from swarm.tools.support import respond

SEVERITIES = ("info", "low", "medium", "high", "critical")
_RANK = {name: index for index, name in enumerate(SEVERITIES)}

# --- SWC Registry subset: signal -> (SWC id, title, default severity) --------------------------------------------
SWC_TABLE = {
    "reentrant-external-call": ("SWC-107", "Reentrancy", "high"),
    "tx-origin-auth": ("SWC-115", "Authorization through tx.origin", "high"),
    "unchecked-call-return": ("SWC-104", "Unchecked Call Return Value", "medium"),
    "unprotected-selfdestruct": ("SWC-106", "Unprotected SELFDESTRUCT Instruction", "critical"),
    "delegatecall-untrusted": ("SWC-112", "Delegatecall to Untrusted Callee", "critical"),
    "timestamp-dependence": ("SWC-116", "Block values as a proxy for time", "low"),
    "weak-randomness": ("SWC-120", "Weak Sources of Randomness from Chain Attributes", "high"),
    "floating-pragma": ("SWC-103", "Floating Pragma", "info"),
    "outdated-compiler": ("SWC-102", "Outdated Compiler Version", "low"),
    "default-visibility": ("SWC-100", "Function Default Visibility", "high"),
    "state-default-visibility": ("SWC-108", "State Variable Default Visibility", "low"),
    "unprotected-ether-withdrawal": ("SWC-105", "Unprotected Ether Withdrawal", "critical"),
    "signature-replay": ("SWC-121", "Missing Protection against Signature Replay Attacks", "high"),
    "signature-malleability": ("SWC-117", "Signature Malleability", "medium"),
    "missing-signature-verification": ("SWC-122", "Lack of Proper Signature Verification", "high"),
    "unbounded-loop": ("SWC-128", "DoS With Block Gas Limit", "medium"),
    "failed-call-dos": ("SWC-113", "DoS with Failed Call", "medium"),
    "arbitrary-storage-write": ("SWC-124", "Write to Arbitrary Storage Location", "critical"),
    "integer-overflow": ("SWC-101", "Integer Overflow and Underflow", "high"),
    "deprecated-functions": ("SWC-111", "Use of Deprecated Solidity Functions", "info"),
    "assert-violation": ("SWC-110", "Assert Violation", "low"),
    "shadowed-state-variable": ("SWC-119", "Shadowing State Variables", "low"),
    "hardcoded-gas-call": ("SWC-134", "Message call with hardcoded gas amount", "low"),
    "transaction-order-dependence": ("SWC-114", "Transaction Order Dependence", "medium"),
    "private-data-onchain": ("SWC-136", "Unencrypted Private Data On-Chain", "medium"),
    "hash-collision-varlen": ("SWC-133", "Hash Collisions With Multiple Variable Length Arguments", "medium"),
    "unexpected-ether-balance": ("SWC-132", "Unexpected Ether balance", "low"),
    "uninitialized-storage-pointer": ("SWC-109", "Uninitialized Storage Pointer", "high"),
    "incorrect-constructor-name": ("SWC-118", "Incorrect Constructor Name", "high"),
    "insufficient-gas-griefing": ("SWC-126", "Insufficient Gas Griefing", "low"),
}

# --- OWASP likelihood x impact matrix, columns: likelihood low, medium, high ----------------------------------------
_LEVELS = ("low", "medium", "high")
_MATRIX = {
    "high": ("medium", "high", "critical"),
    "medium": ("low", "medium", "high"),
    "low": ("info", "low", "medium"),
}
IMPACT_HIGH_FRACTION = Decimal("0.1")  # loss >= 10% of TVL
IMPACT_MEDIUM_FRACTION = Decimal("0.01")  # loss >= 1% of TVL
_PRIVILEGE_LIKELIHOOD = {"permissionless": 2, "participant": 1, "privileged": 0}

# --- oracle / AMM -----------------------------------------------------------------------------------------------------
MIN_TWAP_WINDOW_S = 1800  # heuristic floor: 30 minutes
FLASHLOAN_FEE_BPS_DEFAULT = 5  # Aave V3 flash loan premium 0.05%
BPS = Decimal(10000)
ORACLE_TYPES = ("spot_amm", "twap", "chainlink", "admin_set")

# --- fuzz policy ----------------------------------------------------------------------------------------------------
FUZZ_RUNS_MIN = 10_000
INVARIANT_DEPTH_MIN = 50
INVARIANT_CALLS_MIN = 100_000
FUZZ_SEEDS_MIN = 3
LINE_COVERAGE_MIN = Decimal(95)
BRANCH_COVERAGE_MIN = Decimal(90)
COVERAGE_HIGH_SLACK = Decimal(15)  # more than 15 points under the minimum is high instead of medium
REVERT_RATIO_MAX = Decimal(50)

# --- centralization -------------------------------------------------------------------------------------------------
POWER_SEVERITY = {
    "drain_funds": "critical",
    "upgrade_implementation": "critical",
    "mint_unbounded": "critical",
    "freeze_user_funds": "high",
    "set_oracle": "high",
    "set_fee_uncapped": "high",
    "set_parameters_unbounded": "medium",
    "pause_deposits": "low",
    "set_fee_capped": "low",
}
HOLDERS = ("eoa", "multisig", "governance", "contract", "renounced")
TIMELOCK_MIN_S = 172_800  # 48 hours
STRONG_MULTISIG_MIN_THRESHOLD = 3
STRONG_MULTISIG_MIN_SIGNERS = 5

# --- vault ----------------------------------------------------------------------------------------------------------
VAULT_LOSS_FAIL_PER_100 = 1  # victim loses >= 1% of the deposit
VAULT_LOSS_WARN_PER_10000 = 1  # victim loses >= 1 basis point

# --- report ---------------------------------------------------------------------------------------------------------
REQUIRED_SECTIONS = (
    "executive_summary",
    "scope",
    "methodology",
    "severity_definitions",
    "findings_summary",
    "findings_detail",
    "disclaimer",
)
OPTIONAL_SECTIONS = ("appendix", "fix_review", "tooling", "change_log")
REPORT_FINDINGS_CAP = 600  # maxItems of audit_report_completeness output findings in agents.json
FIX_STATUSES = ("open", "fixed", "mitigated", "acknowledged", "wontfix")
_FIX_FACTOR = {"verified_fixed": Decimal(0), "verified_mitigated": Decimal("0.25"), "unverified": Decimal("0.5"), "unresolved": Decimal(1)}
SEVERITY_WEIGHT = {"critical": 16, "high": 8, "medium": 4, "low": 2, "info": 0}

_HEX = frozenset(_string.hexdigits)
# swarm.redact catches provider tokens, URL credentials and credential-named pairs; a raw 64-hex key and a mnemonic (12 or
# more short lowercase words) are caught here. Over-redacting an identifier is the safe failure.
_KEY_RE = re.compile(r"(?<![0-9A-Za-z])(?:0[xX])?[0-9a-fA-F]{64}(?![0-9A-Za-z])")
_MNEMONIC_RE = re.compile(r"(?<![A-Za-z])(?:[a-z]{3,8}[ ,\t\n]+){11,}[a-z]{3,8}(?![A-Za-z])")
REDACTED = "[REDACTED]"


def _clean(text: object) -> str:
    """A caller-chosen string made safe to echo: secret-looking spans become [REDACTED]."""
    value = _MNEMONIC_RE.sub(REDACTED, _KEY_RE.sub(REDACTED, redact_text(str(text))))
    return value


def _invalid(field: str, message: str) -> dict[str, Any]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": _clean(message)}


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _frac(value: object) -> Fraction:
    """Exact rational of the number as written (shortest repr); unlike Decimal it never rounds to a context precision."""
    return Fraction(repr(value))


def _f(value: Decimal | Fraction | float, digits: int = 4) -> float:
    """Rounded float; a value beyond the float range (an exact ratio of absurd reserves) saturates at +-1.7976931348623157e308."""
    try:
        return round(float(value), digits)
    except OverflowError:
        return sys.float_info.max if value > 0 else -sys.float_info.max


def _finding(fid: str, severity: str, summary: str) -> dict[str, Any]:
    return {"id": _clean(fid)[:200], "severity": severity, "summary": _clean(summary)[:400]}


def _ordered(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda f: (-_RANK[f["severity"]], f["id"]))


def _verdict(findings: list[dict[str, Any]]) -> str:
    top = max((_RANK[f["severity"]] for f in findings), default=-1)
    return "fail" if top >= _RANK["high"] else "warn" if top >= _RANK["medium"] else "pass"


def _matrix(impact: str, likelihood: str) -> str:
    """OWASP overall severity for impact and likelihood levels (low | medium | high)."""
    return _MATRIX[impact][_LEVELS.index(likelihood)]


def _is_hex(value: str, low: int, high: int) -> bool:
    return low <= len(value) <= high and all(ch in _HEX for ch in value)


def _is_commit(value: str) -> bool:
    """A git object id as written in a report: 7..40 hex characters (SHA-1) or exactly 64 (SHA-256 repositories)."""
    return _is_hex(value, 7, 40) or _is_hex(value, 64, 64)


_Num = TypeVar("_Num", Decimal, Fraction)  # Decimal for the AMM sweeps, exact Fraction for flashloan_profit


def _cp_out(amount_in: _Num, reserve_in: _Num, reserve_out: _Num, fee: _Num) -> _Num:
    """Uniswap V2 getAmountOut: the fee is taken from the input, the full input stays in the pool."""
    effective = amount_in * (1 - fee)
    return effective * reserve_out / (reserve_in + effective)


def _push_size(reserve: Decimal, deviation: Decimal, fee: Decimal) -> Decimal:
    """Input amount on the quote side that moves a constant-product spot price by `deviation` (fraction), fee included.

    Spot after the swap is (R + d)(R + (1-f) d) / R**2, so (1-f) d**2 + R (2-f) d - R**2 p = 0 and
    d = R (sqrt((2-f)**2 + 4 (1-f) p) - (2-f)) / (2 (1-f)); with f = 0 this is R (sqrt(1+p) - 1).
    """
    two_minus_fee = 2 - fee
    root = (two_minus_fee * two_minus_fee + 4 * (1 - fee) * deviation).sqrt()
    return reserve * (root - two_minus_fee) / (2 * (1 - fee))


# ===================================================================================================================
# 1. vulnerability taxonomy
# ===================================================================================================================


def classify_vulnerability(payload: dict[str, Any]) -> dict[str, Any]:
    """Map code signals to SWC registry classes with a default severity; counts by severity and a verdict."""

    def work() -> dict[str, Any]:
        observations = payload["observations"]
        seen: set[str] = set()
        counts = dict.fromkeys(SEVERITIES, 0)
        swc_ids: set[str] = set()
        findings = []
        for index, observation in enumerate(observations):
            if observation["id"] in seen:
                return _invalid("observations", f"duplicate observation id {observation['id']!r} at index {index}")
            seen.add(observation["id"])
            row = SWC_TABLE.get(observation["signal"])
            if row is None:
                return _invalid("observations", f"unknown signal {observation['signal']!r} at index {index}")
            swc, title, severity = row
            counts[severity] += 1
            swc_ids.add(swc)
            where = observation.get("location")
            findings.append(_finding(observation["id"], severity, f"{swc} {title}" + (f" at {where}" if where else "")))
        return {
            "state": "SUCCESS",
            "total": len(observations),
            "counts": counts,
            "swc_ids": sorted(swc_ids),
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 2. severity scoring
# ===================================================================================================================


def score_severity(payload: dict[str, Any]) -> dict[str, Any]:
    """Severity = OWASP matrix of impact (loss versus TVL, irrecoverable bump) and likelihood (privilege, preconditions)."""

    def work() -> dict[str, Any]:
        loss, tvl = _dec(payload["loss_usd"]), _dec(payload["tvl_usd"])
        if loss > tvl:
            return _invalid("loss_usd", "loss_usd must not exceed tvl_usd")
        if tvl <= 0:
            return _invalid("tvl_usd", "tvl_usd must be above zero to size a loss fraction")
        fraction = loss / tvl
        impact_index = 2 if fraction >= IMPACT_HIGH_FRACTION else 1 if fraction >= IMPACT_MEDIUM_FRACTION else 0
        if payload["irrecoverable"]:
            impact_index = min(2, impact_index + 1)
        preconditions = payload["preconditions"]
        penalty = 0 if preconditions <= 1 else 1 if preconditions <= 3 else 2
        likelihood_index = max(0, _PRIVILEGE_LIKELIHOOD[payload["attacker_privilege"]] - penalty)
        impact, likelihood = _LEVELS[impact_index], _LEVELS[likelihood_index]
        severity = _matrix(impact, likelihood)
        proposed = payload.get("proposed_severity")
        needs_l3 = proposed is not None and proposed != severity
        findings = []
        if _RANK[severity] >= _RANK["medium"]:
            findings.append(_finding("SEV-COMPUTED", severity, f"computed {severity} severity from impact {impact} and likelihood {likelihood}: a {severity} result needs triage, it is not a pass"))
        if needs_l3:
            findings.append(
                _finding("SEV-OVERRIDE", "medium", f"proposed severity {proposed} differs from the computed {severity}; a change needs L3 approval")
            )
        return {
            "state": "SUCCESS",
            "impact": impact,
            "likelihood": likelihood,
            "severity": severity,
            "loss_fraction": _f(fraction, 6),
            "requires_l3_approval": needs_l3,
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 3. reentrancy ordering
# ===================================================================================================================


def reentrancy_order(payload: dict[str, Any]) -> dict[str, Any]:
    """Checks-effects-interactions: a state write after an untrusted external call; stale checks and cross-function reach."""

    def work() -> dict[str, Any]:
        functions = payload["functions"]
        names = [fn["name"] for fn in functions]
        if len(set(names)) != len(names):
            return _invalid("functions", "function names must be unique")
        guarded = {fn["name"]: fn["non_reentrant"] for fn in functions}
        touched: dict[str, set[str]] = {}
        for fn in functions:
            for step in fn["steps"]:
                if step["kind"] in ("check", "effect"):
                    touched.setdefault(step["target"], set()).add(fn["name"])
        findings: list[dict[str, Any]] = []
        untrusted = 0
        compliant = 0
        for fn in sorted(functions, key=lambda item: item["name"]):
            name = fn["name"]
            checked: set[str] = set()
            checked_before_call: set[str] | None = None
            call = ""
            violated: set[str] = set()
            for step in fn["steps"]:
                kind, target = step["kind"], step["target"]
                if kind == "check":
                    checked.add(target)
                elif kind == "interaction":
                    if step.get("trusted") is True:
                        continue
                    untrusted += 1
                    if checked_before_call is None:
                        checked_before_call, call = set(checked), target
                elif checked_before_call is not None and target not in violated:
                    violated.add(target)
                    stale = target in checked_before_call
                    severity = "low" if guarded[name] else "high" if stale else "medium"
                    how = "checked before the external call and written after it (stale check)" if stale else "written after the external call"
                    guard = "; nonReentrant guard present" if guarded[name] else ""
                    findings.append(_finding(f"REENT-{name}-{target}", severity, f"{name}: state {target} {how} ({call}){guard}"))
                    # One shared lock: a nonReentrant function rejects a nested call only while the lock is held, and only a
                    # nonReentrant `name` holds it during its external call. So g is shielded only when both are guarded.
                    others = sorted(g for g in touched[target] if g != name and not (guarded[name] and guarded[g]))
                    if others:
                        shown = ", ".join(f"unguarded {g}" if not guarded[g] else f"{g} (nonReentrant, but {name} holds no lock)" for g in others[:5])
                        findings.append(_finding(f"REENT-X-{name}-{target}", "high", f"cross-function reentrancy: {shown} can read or write {target} during {name}"))
            if not violated:
                compliant += 1
        return {
            "state": "SUCCESS",
            "functions_checked": len(functions),
            "cei_compliant": compliant,
            "untrusted_interactions": untrusted,
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 4. oracle manipulation risk
# ===================================================================================================================

_ORACLE_REQUIRED = {
    "spot_amm": ("pool_liquidity_usd", "target_deviation_pct", "extractable_profit_usd"),
    "twap": ("pool_liquidity_usd", "target_deviation_pct", "extractable_profit_usd", "twap_window_s"),
    "chainlink": ("staleness_check", "answer_bounds_check"),
    "admin_set": (),
}


def oracle_risk(payload: dict[str, Any]) -> dict[str, Any]:
    """Oracle manipulation risk: constant-product attack cost versus extractable profit (spot, TWAP) or feed hygiene (Chainlink)."""

    def work() -> dict[str, Any]:
        kind = payload["oracle_type"]
        for field in _ORACLE_REQUIRED[kind]:
            if field not in payload:
                return _invalid(field, f"{field} is required for oracle_type {kind}")
        findings: list[dict[str, Any]] = []
        capital = cost = gain = Decimal(0)
        feasible = profitable = modelled = False
        if kind in ("spot_amm", "twap"):
            modelled = True
            reserve = _dec(payload["pool_liquidity_usd"]) / 2  # a balanced xyk pool holds half its value on each side
            target = _dec(payload["target_deviation_pct"]) / 100
            profit = _dec(payload["extractable_profit_usd"])
            fee = _dec(payload.get("fee_bps", 30)) / BPS
            if kind == "spot_amm":
                capital = _push_size(reserve, target, fee)
                total_capital = _dec(payload.get("flashloan_liquidity_usd", 0)) + _dec(payload.get("attacker_capital_usd", 0))
                out = _cp_out(capital, reserve, reserve, fee)
                unwind = _cp_out(out, reserve - out, reserve + capital, fee)
                borrowed = max(Decimal(0), capital - _dec(payload.get("attacker_capital_usd", 0)))
                flash_fee = borrowed * _dec(payload.get("flashloan_fee_bps", FLASHLOAN_FEE_BPS_DEFAULT)) / BPS
                cost = capital - unwind + flash_fee
                feasible = total_capital >= capital
                findings.append(_finding("ORACLE-SPOT", "high", "pool spot price used as oracle: movable within one transaction by a swap"))
            else:
                window = int(payload["twap_window_s"])
                block_time = _dec(payload.get("block_time_s", 12))
                hold_blocks = int(payload.get("hold_blocks", 1))
                blocks = min(hold_blocks, math.ceil(Decimal(window) / block_time))
                held = min(blocks * block_time, Decimal(window))
                spot_deviation = target * window / held  # the arithmetic mean over the window carries held/window of the spot shift
                capital = _push_size(reserve, spot_deviation, fee)
                out = _cp_out(capital, reserve, reserve, fee)
                cost = blocks * (capital - out)  # arbitrageurs take the other side every block: the attacker keeps `out` base tokens at fair value
                feasible = _dec(payload.get("attacker_capital_usd", 0)) >= capital
                if window < MIN_TWAP_WINDOW_S:
                    findings.append(_finding("ORACLE-TWAP-WINDOW", "medium", f"TWAP window {window}s is below {MIN_TWAP_WINDOW_S}s: cheap to skew"))
            profitable = profit > cost
            gain = profit - cost
            if profitable and feasible:
                findings.append(_finding("ORACLE-ATTACK", "critical", f"manipulation is feasible and profitable: cost {_f(cost, 2)} < profit {_f(profit, 2)} USD"))
            elif profitable:
                findings.append(_finding("ORACLE-ATTACK-CAPITAL", "medium", f"profitable (cost {_f(cost, 2)} < profit {_f(profit, 2)} USD) but needs {_f(capital, 2)} USD capital the attacker lacks"))
            elif feasible and kind == "twap":
                findings.append(_finding("ORACLE-ATTACK-UNPROFITABLE", "low", f"feasible but cost {_f(cost, 2)} >= profit {_f(profit, 2)} USD"))
        elif kind == "chainlink":
            if not payload["staleness_check"]:
                findings.append(_finding("ORACLE-STALE", "high", "no updatedAt staleness check: a stalled feed keeps pricing"))
            if not payload["answer_bounds_check"]:
                findings.append(
                    _finding("ORACLE-BOUNDS", "medium", "no answer sanity bounds: minAnswer/maxAnswer are no longer used on most feeds, so the contract needs its own sanity bound (or circuit breaker)")
                )
            if payload.get("l2", False) and not payload.get("sequencer_uptime_check", False):
                findings.append(_finding("ORACLE-SEQUENCER", "high", "L2 deployment without a sequencer uptime feed check (answer 0 = up) and a grace period after it restarts"))
        else:
            findings.append(_finding("ORACLE-ADMIN", "high", "price is set by a privileged key: compromise or error misprices the protocol"))
        risk = max((f["severity"] for f in findings), key=_RANK.__getitem__, default="none")
        return {
            "state": "SUCCESS",
            "modelled": modelled,
            "required_capital_usd": _f(capital, 2),
            "attack_cost_usd": _f(cost, 2),
            "net_gain_usd": _f(gain, 2),
            "feasible": feasible,
            "profitable": profitable,
            "risk": risk,
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 5. flash-loan profitability
# ===================================================================================================================


def flashloan_profit(payload: dict[str, Any]) -> dict[str, Any]:
    """Constant-product price impact of a flash-funded swap, its round-trip and flash-loan cost, and the break-even exploit gain."""

    def work() -> dict[str, Any]:
        # Fractions, not Decimal: a pool reserve far below the borrow makes reserve_out - amount_out vanish at 28 digits.
        reserve_in, reserve_out = _frac(payload["reserve_in"]), _frac(payload["reserve_out"])
        borrow = _frac(payload["borrow_amount"])
        fee = _frac(payload["fee_bps"]) / 10000
        price = _frac(payload["in_token_price_usd"])
        out = _cp_out(borrow, reserve_in, reserve_out, fee)
        spot_before = reserve_in / reserve_out
        after_in, after_out = reserve_in + borrow, reserve_out - out
        impact = (after_in / after_out) / spot_before - 1
        unwind = _cp_out(out, after_out, after_in, fee)  # sell the bought tokens straight back into the same pool
        round_trip_loss = borrow - unwind
        flash_fee = borrow * _frac(payload["flashloan_fee_bps"]) / 10000
        cost = (round_trip_loss + flash_fee) * price + _frac(payload["fixed_cost_usd"])
        gain = _frac(payload["exploit_gain_usd"])
        net = gain - cost
        profitable = net > 0
        if profitable:
            findings = [_finding("FLASH-PROFIT", "high", f"flash-funded manipulation nets {_f(net, 2)} USD (cost {_f(cost, 2)} < gain {_f(gain, 2)})")]
        else:
            findings = [_finding("FLASH-UNPROFITABLE", "info", f"cost {_f(cost, 2)} USD >= gain {_f(gain, 2)} USD at this borrow size")]
        return {
            "state": "SUCCESS",
            "amount_out": _f(out, 6),
            "price_impact_pct": _f(impact * 100, 4),
            "borrow_pct_of_reserve": _f(borrow / reserve_in * 100, 4),
            "round_trip_loss": _f(round_trip_loss, 6),
            "flashloan_fee": _f(flash_fee, 6),
            "total_cost_usd": _f(cost, 2),
            "net_profit_usd": _f(net, 2),
            "break_even_gain_usd": _f(cost, 2),
            "profitable": profitable,
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 6. invariant coverage
# ===================================================================================================================


def invariant_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Coverage of mutable state variables and state-changing functions by the invariant suite."""

    def work() -> dict[str, Any]:
        variables = payload["state_variables"]
        if len(set(variables)) != len(variables):
            return _invalid("state_variables", "state variable names must be unique")
        functions = {fn["name"]: set(fn["writes"]) for fn in payload["functions"]}
        if len(functions) != len(payload["functions"]):
            return _invalid("functions", "function names must be unique")
        known_vars = set(variables)
        for name, writes in functions.items():
            unknown = sorted(writes - known_vars)
            if unknown:
                return _invalid("functions", f"{name} writes unknown state variable {unknown[0]!r}")
        invariant_ids = [inv["id"] for inv in payload["invariants"]]
        if len(set(invariant_ids)) != len(invariant_ids):
            return _invalid("invariants", "invariant ids must be unique")
        covered_vars: set[str] = set()
        covered_fns: set[str] = set()
        for inv in payload["invariants"]:
            unknown_vars = sorted(set(inv["variables"]) - known_vars)
            if unknown_vars:
                return _invalid("invariants", f"{inv['id']} references unknown state variable {unknown_vars[0]!r}")
            unknown_fns = sorted(set(inv["functions"]) - set(functions))
            if unknown_fns:
                return _invalid("invariants", f"{inv['id']} references unknown function {unknown_fns[0]!r}")
            covered_vars.update(inv["variables"])
            covered_fns.update(inv["functions"])
        mutable = sorted(set().union(*functions.values())) if functions else []
        changing = sorted(name for name, writes in functions.items() if writes)
        uncovered_vars = [v for v in mutable if v not in covered_vars]
        uncovered_fns = [name for name in changing if name not in covered_fns]
        findings = [_finding(f"INV-VAR-{v}", "medium", f"state variable {v} is written but no invariant constrains it") for v in uncovered_vars]
        for name in uncovered_fns:
            risky = sorted(v for v in functions[name] if v in uncovered_vars)
            severity = "medium" if risky else "low"
            extra = f" (writes uncovered {', '.join(risky[:5])})" if risky else ""
            findings.append(_finding(f"INV-FN-{name}", severity, f"state-changing function {name} is not exercised by any invariant{extra}"))
        if payload["holds_funds"] and not any(inv["kind"] in ("conservation", "solvency") and inv["variables"] for inv in payload["invariants"]):
            findings.append(_finding("INV-SOLVENCY", "high", "contract holds funds but no conservation or solvency invariant over a state variable exists"))
        n_mutable, n_changing = len(mutable), len(changing)
        return {
            "state": "SUCCESS",
            "mutable_variables": n_mutable,
            "covered_variables": n_mutable - len(uncovered_vars),
            "variable_coverage": _f(Decimal(n_mutable - len(uncovered_vars)) / n_mutable, 4) if n_mutable else 1.0,
            "state_changing_functions": n_changing,
            "covered_functions": n_changing - len(uncovered_fns),
            "function_coverage": _f(Decimal(n_changing - len(uncovered_fns)) / n_changing, 4) if n_changing else 1.0,
            "uncovered_variables": [_clean(v) for v in uncovered_vars],
            "uncovered_functions": [_clean(name) for name in uncovered_fns],
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 7. fuzz campaign adequacy
# ===================================================================================================================


def fuzz_adequacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Runs, depth, seed diversity, coverage and revert ratio of a fuzz or invariant campaign against the audit policy."""

    def work() -> dict[str, Any]:
        mode, runs = payload["mode"], payload["runs"]
        if payload["functions_exercised"] > payload["functions_total"]:
            return _invalid("functions_exercised", "functions_exercised must not exceed functions_total")
        depth = payload.get("depth")
        if mode == "invariant" and depth is None:
            return _invalid("depth", "depth is required for mode invariant")
        checks: list[tuple[bool, str, str, str]] = []  # passed, severity if failed, id, summary
        if mode == "stateless":
            total_calls = runs
            minimum = payload.get("min_total_calls", FUZZ_RUNS_MIN)  # the same override applies: a stateless run is one call
            severity = "medium" if runs * 10 >= minimum else "high"
            checks.append((runs >= minimum, severity, "FUZZ-RUNS", f"{runs} runs, policy minimum {minimum}"))
        else:
            assert depth is not None  # the schema admits only stateless and invariant; invariant without depth returned above
            total_calls = runs * depth
            minimum = payload.get("min_total_calls", INVARIANT_CALLS_MIN)
            severity = "medium" if total_calls * 10 >= minimum else "high"
            checks.append((depth >= INVARIANT_DEPTH_MIN, "medium", "FUZZ-DEPTH", f"depth {depth}, policy minimum {INVARIANT_DEPTH_MIN}"))
            checks.append((total_calls >= minimum, severity, "FUZZ-CALLS", f"{total_calls} total calls (runs x depth), policy minimum {minimum}"))
            if "revert_ratio_pct" in payload:
                ratio = _dec(payload["revert_ratio_pct"])
                checks.append((ratio <= REVERT_RATIO_MAX, "medium", "FUZZ-REVERTS", f"{ratio}% of calls revert, policy maximum {REVERT_RATIO_MAX}%"))
        checks.append((payload["distinct_seeds"] >= FUZZ_SEEDS_MIN, "low", "FUZZ-SEEDS", f"{payload['distinct_seeds']} distinct seeds, policy minimum {FUZZ_SEEDS_MIN}"))
        for key, label, minimum in (
            ("line_coverage_pct", "FUZZ-LINE", LINE_COVERAGE_MIN),
            ("branch_coverage_pct", "FUZZ-BRANCH", BRANCH_COVERAGE_MIN),
        ):
            value = _dec(payload[key])
            severity = "high" if value < minimum - COVERAGE_HIGH_SLACK else "medium"
            checks.append((value >= minimum, severity, label, f"{key} {value}%, policy minimum {minimum}%"))
        total, exercised = payload["functions_total"], payload["functions_exercised"]
        severity = "medium" if exercised * 10 >= total * 9 else "high"
        checks.append((exercised >= total, severity, "FUZZ-FUNCTIONS", f"{exercised} of {total} functions exercised"))
        findings = [_finding(fid, sev, summary) for passed, sev, fid, summary in checks if not passed]
        lowered = payload.get("min_total_calls")
        default_floor = FUZZ_RUNS_MIN if mode == "stateless" else INVARIANT_CALLS_MIN
        if lowered is not None and lowered < default_floor:
            findings.append(_finding("FUZZ-FLOOR", "medium", f"caller lowered the policy minimum from {default_floor} to {lowered} total calls; a lowered floor needs L3 approval"))
        passed_count = sum(1 for passed, *_ in checks if passed)
        return {
            "state": "SUCCESS",
            "total_calls": total_calls,
            "checks_total": len(checks),
            "checks_passed": passed_count,
            "adequacy_score": _f(Decimal(passed_count) * 100 / len(checks), 2),
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 8. signature replay surface
# ===================================================================================================================


def signature_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Replay surface of a signature-consuming function: nonce, chain id, contract binding, deadline, zero address, malleability."""

    def work() -> dict[str, Any]:
        if payload["deadline_enforced"] and not payload["deadline_in_message"]:
            return _invalid("deadline_enforced", "a deadline cannot be enforced when it is not part of the signed message")
        if payload["chainid_recomputed"] and not payload["domain_separator_cached"]:
            return _invalid("chainid_recomputed", "chainid_recomputed only applies to a cached domain separator")
        findings: list[dict[str, Any]] = []
        if not payload["nonce_consumed"]:
            findings.append(_finding("SIG-REPLAY", "high", "SWC-121: no nonce or digest is marked used, the same signature can be submitted again"))
        if not payload["chain_id_bound"]:
            severity = "high" if payload["multichain_deployment"] else "medium"
            findings.append(_finding("SIG-CHAINID", severity, "chain id is not in the signed digest (EIP-712 domain chainId): replay on another chain or fork"))
        if not payload["contract_bound"]:
            findings.append(_finding("SIG-CONTRACT", "high", "verifying contract is not in the signed digest: replay across deployments sharing the signer"))
        if payload["scheme"] == "raw_hash":
            findings.append(_finding("SIG-SCHEME", "medium", "raw 32-byte digest without the EIP-191 prefix can coincide with a transaction hash; use EIP-712"))
        elif payload["scheme"] == "eip191_personal":
            findings.append(_finding("SIG-SCHEME", "low", "EIP-191 personal_sign signs opaque bytes; EIP-712 typed data shows the fields to the signer"))
        if not payload["deadline_in_message"]:
            findings.append(_finding("SIG-DEADLINE", "medium", "no deadline in the signed message: a signature stays valid forever"))
        elif not payload["deadline_enforced"]:
            findings.append(_finding("SIG-DEADLINE-UNENFORCED", "high", "deadline is signed but never compared with block.timestamp"))
        if not payload["zero_address_check"]:
            findings.append(_finding("SIG-ZERO", "high", "SWC-122: ecrecover returns address(0) for invalid signatures and the result is not rejected"))
        if not payload["malleability_protected"]:
            severity = "low" if payload["nonce_consumed"] else "medium"
            findings.append(_finding("SIG-MALLEABLE", severity, "SWC-117: high-s signatures accepted, a second valid signature exists for the same message"))
        if payload["domain_separator_cached"] and not payload["chainid_recomputed"]:
            findings.append(_finding("SIG-FORK", "medium", "domain separator cached at deployment without a chain id check: replayable after a chain fork"))
        return {
            "state": "SUCCESS",
            "replay_protected": bool(payload["nonce_consumed"] and payload["chain_id_bound"] and payload["contract_bound"]),
            "finding_count": len(findings),
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 9. privileged-role centralization
# ===================================================================================================================


def role_centralization(payload: dict[str, Any]) -> dict[str, Any]:
    """Effective severity of each privileged power after multisig strength, timelock delay and governance mitigation."""

    def work() -> dict[str, Any]:
        roles = payload["roles"]
        names = [role["name"] for role in roles]
        if len(set(names)) != len(names):
            return _invalid("roles", "role names must be unique")
        findings: list[dict[str, Any]] = []
        powers = 0
        for role in sorted(roles, key=lambda item: item["name"]):
            holder, name = role["holder"], role["name"]
            if holder == "renounced":
                continue
            steps = 0
            notes = [holder]
            if holder == "multisig":
                threshold, signers = role.get("multisig_threshold"), role.get("multisig_signers")
                if threshold is None or signers is None:
                    return _invalid("roles", f"role {name!r}: multisig needs multisig_threshold and multisig_signers")
                if threshold > signers:
                    return _invalid("roles", f"role {name!r}: multisig_threshold must not exceed multisig_signers")
                notes[0] = f"{threshold}-of-{signers} multisig"
                if threshold >= STRONG_MULTISIG_MIN_THRESHOLD and signers >= STRONG_MULTISIG_MIN_SIGNERS and 2 * threshold > signers:
                    steps += 1
            elif holder == "governance":
                steps += 1
            delay = role.get("timelock_delay_s", 0)
            if delay >= TIMELOCK_MIN_S and not role.get("bypasses_timelock", False):
                steps += 1
                notes.append(f"timelock {delay}s")
            elif delay and role.get("bypasses_timelock", False):
                notes.append("timelock bypassable")
            for power in sorted(set(role["powers"])):
                powers += 1
                base = POWER_SEVERITY[power]
                effective = SEVERITIES[max(0, _RANK[base] - steps)]
                findings.append(_finding(f"ROLE-{name}-{power}", effective, f"{name} ({', '.join(notes)}) can {power}: base {base}, effective {effective}"))
        top = max((f["severity"] for f in findings), key=_RANK.__getitem__, default="none")
        return {
            "state": "SUCCESS",
            "roles_assessed": len(roles),
            "powers_assessed": powers,
            "critical_exposures": sum(1 for f in findings if f["severity"] == "critical"),
            "highest_severity": top,
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 10. ERC-4626 share rounding and inflation attack
# ===================================================================================================================


def _shares(mode: str, offset: int, assets: int, supply: int, total: int) -> int:
    if mode == "legacy":
        return assets if supply == 0 else assets * supply // total
    result: int = assets * (supply + 10**offset) // (total + 1)
    return result


def _assets(mode: str, offset: int, shares: int, supply: int, total: int) -> int:
    if mode == "legacy":
        return shares if supply == 0 else shares * total // supply
    result: int = shares * (total + 1) // (supply + 10**offset)
    return result


def vault_inflation(payload: dict[str, Any]) -> dict[str, Any]:
    """First-depositor donation attack on an ERC-4626 vault: victim shares and loss, attacker profit, donation that zeroes the victim."""

    def work() -> dict[str, Any]:
        mode, offset = payload["mode"], payload.get("decimals_offset", 0)
        supply, total = payload.get("initial_shares", 0), payload.get("initial_assets", 0)
        attacker, donation, victim = payload["attacker_deposit"], payload["donation"], payload["victim_deposit"]
        if mode == "legacy" and supply > 0 and total == 0:
            return _invalid("initial_assets", "a legacy vault with shares but no assets cannot convert assets to shares")
        attacker_shares = _shares(mode, offset, attacker, supply, total)
        supply, total = supply + attacker_shares, total + attacker
        # smallest donation that makes the victim's deposit mint zero shares
        if mode == "legacy":
            needed = max(0, victim * supply - total + 1)
        else:
            needed = max(0, victim * (supply + 10**offset) - total)
        total += donation
        victim_shares = _shares(mode, offset, victim, supply, total)
        supply, total = supply + victim_shares, total + victim
        victim_value = _assets(mode, offset, victim_shares, supply, total)
        attacker_value = _assets(mode, offset, attacker_shares, supply, total)
        loss = victim - victim_value
        profit = attacker_value - attacker - donation
        findings: list[dict[str, Any]] = []
        if victim_shares == 0:
            findings.append(_finding("VAULT-ZERO-SHARES", "critical", f"victim deposit of {victim} mints zero shares: the whole deposit is lost"))
        elif loss * 100 >= victim * VAULT_LOSS_FAIL_PER_100:
            findings.append(_finding("VAULT-INFLATION", "high", f"victim loses {loss} of {victim} assets to share rounding after the donation"))
        elif loss * 10000 >= victim * VAULT_LOSS_WARN_PER_10000:
            findings.append(_finding("VAULT-ROUNDING", "medium", f"victim loses {loss} of {victim} assets to share rounding"))
        return {
            "state": "SUCCESS",
            "attacker_shares": attacker_shares,
            "victim_shares": victim_shares,
            "victim_value": victim_value,
            "victim_loss": loss,
            "victim_loss_pct": _f(Decimal(loss) * 100 / Decimal(victim), 6),
            "attacker_profit": profit,
            "donation_to_zero_shares": needed,
            "attacker_cost_to_zero_shares": attacker + needed,
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 11. finding deduplication
# ===================================================================================================================


def _path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _tokens(title: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", title.lower()))


def dedupe_findings(payload: dict[str, Any]) -> dict[str, Any]:
    """Group findings that share category, file and root cause (or function), or whose titles are >= 80% alike; flag severity conflicts and surface the highest input severity, which the verdict covers."""

    def work() -> dict[str, Any]:
        items = payload["findings"]
        ids = [item["id"] for item in items]
        if len(set(ids)) != len(ids):
            return _invalid("findings", "finding ids must be unique")
        parent = list(range(len(items)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        keys = []
        for item in items:
            anchor = (item.get("root_cause") or item.get("function") or "").strip().lower()
            keys.append((item["category"].strip().lower(), _path(item["file"]), anchor, _tokens(item["title"])))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (cat_i, file_i, anchor_i, tok_i), (cat_j, file_j, anchor_j, tok_j) = keys[i], keys[j]
                if cat_i != cat_j or file_i != file_j:
                    continue
                union = len(tok_i | tok_j)
                same_anchor = anchor_i != "" and anchor_i == anchor_j
                similar_title = union > 0 and len(tok_i & tok_j) * 5 >= union * 4
                if same_anchor or similar_title:
                    parent[find(j)] = find(i)
        members: dict[int, list[dict[str, Any]]] = {}
        for index, item in enumerate(items):
            members.setdefault(find(index), []).append(item)
        groups, findings = [], []
        for group in members.values():
            if len(group) < 2:
                continue
            ranked = sorted(group, key=lambda item: (-_RANK[item["severity"]], item["id"]))
            primary = ranked[0]
            conflict = len({item["severity"] for item in group}) > 1
            groups.append(
                {
                    "primary_id": _clean(primary["id"]),
                    "duplicate_ids": [_clean(i) for i in sorted(item["id"] for item in ranked[1:])],
                    "severity": primary["severity"],
                    "severity_conflict": conflict,
                }
            )
            if conflict:
                severities = ", ".join(sorted({item["severity"] for item in group}, key=_RANK.__getitem__))
                findings.append(_finding(f"DEDUP-{primary['id']}", "medium", f"duplicates of {primary['id']} disagree on severity ({severities}); resolving needs L3 approval"))
        if items:
            top = max((item["severity"] for item in items), key=_RANK.__getitem__)
            if _RANK[top] >= _RANK["medium"]:
                at_top = sum(1 for item in items if item["severity"] == top)
                findings.append(_finding("INPUT-SEVERITY", top, f"{at_top} input finding(s) at {top} severity: the verdict covers the highest input severity"))
        groups.sort(key=lambda group: group["primary_id"])
        duplicates = sum(len(group["duplicate_ids"]) for group in groups)
        return {
            "state": "SUCCESS",
            "input_count": len(items),
            "unique_count": len(items) - duplicates,
            "duplicate_count": duplicates,
            "groups": groups,
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 12. audit report completeness
# ===================================================================================================================


def report_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    """Required sections, a proof of concept for every high or critical finding, recommendation, location and fix status."""

    def work() -> dict[str, Any]:
        commit = payload["scope_commit"]
        if not _is_hex(commit, 40, 40) and not _is_hex(commit, 64, 64):
            return _invalid("scope_commit", "scope_commit must be a full git object id (40 or 64 hex characters)")
        items = payload["findings"]
        ids = [item["id"] for item in items]
        if len(set(ids)) != len(ids):
            return _invalid("findings", "finding ids must be unique")
        present = set(payload["sections"])
        missing = [section for section in REQUIRED_SECTIONS if section not in present]
        findings = []
        for section in missing:
            findings.append(_finding(f"REP-SECTION-{section}", "high" if section == "findings_detail" else "medium", f"required section {section} is missing"))
        checks_total = len(REQUIRED_SECTIONS)
        checks_passed = checks_total - len(missing)
        poc_missing = acceptances = incomplete = 0
        final = payload["stage"] == "final"
        for item in sorted(items, key=lambda entry: entry["id"]):
            serious = item["severity"] in ("critical", "high")
            problems = 0
            checks_total += 2 + (1 if serious else 0)
            checks_passed += (1 if item["has_location"] else 0) + (1 if item["has_recommendation"] else 0)
            if not item["has_location"]:
                problems += 1
                findings.append(_finding(f"REP-{item['id']}-location", "medium", f"{item['id']} has no code location"))
            if not item["has_recommendation"]:
                problems += 1
                findings.append(_finding(f"REP-{item['id']}-recommendation", "medium", f"{item['id']} has no recommendation"))
            if serious:
                if item["has_poc"]:
                    checks_passed += 1
                else:
                    problems += 1
                    poc_missing += 1
                    findings.append(_finding(f"REP-{item['id']}-poc", "high", f"{item['severity']} finding {item['id']} has no proof of concept"))
                if item["fix_status"] in ("acknowledged", "wontfix"):
                    acceptances += 1
                    findings.append(_finding(f"REP-{item['id']}-acceptance", "medium", f"{item['id']} is {item['fix_status']}: needs an L4 risk acceptance"))
                elif item["fix_status"] == "open":
                    if final:
                        open_severity = "critical" if item["severity"] == "critical" else "high"
                        findings.append(_finding(f"REP-{item['id']}-open", open_severity, f"final report lists {item['severity']} finding {item['id']} as open"))
                    else:
                        findings.append(_finding(f"REP-{item['id']}-open", "medium", f"draft lists {item['severity']} finding {item['id']} as open; fix, mitigate or accept it before the final report"))
            incomplete += 1 if problems else 0
        ordered = _ordered(findings)
        if len(ordered) > REPORT_FINDINGS_CAP:  # the registered output_schema allows this many; the verdict below still uses every finding
            omitted = len(ordered) - (REPORT_FINDINGS_CAP - 1)
            ordered = ordered[: REPORT_FINDINGS_CAP - 1] + [
                _finding("REP-TRUNCATED", "info", f"{omitted} lower-ranked findings omitted from this list ({len(findings)} raised); every count above is exact")
            ]
        return {
            "state": "SUCCESS",
            "sections_missing": missing,
            "findings_total": len(items),
            "findings_incomplete": incomplete,
            "poc_missing": poc_missing,
            "risk_acceptances_needed": acceptances,
            "completeness_pct": _f(Decimal(checks_passed) * 100 / checks_total, 2),
            "findings": ordered,
            "verdict": _verdict(findings),
        }

    return respond(payload, work)


# ===================================================================================================================
# 13. fix verification and residual risk
# ===================================================================================================================


def fix_verification(payload: dict[str, Any]) -> dict[str, Any]:
    """A fix counts as verified only with a commit, a regression test and a re-audit; residual risk is severity-weighted."""

    def work() -> dict[str, Any]:
        items = payload["findings"]
        ids = [item["id"] for item in items]
        if len(set(ids)) != len(ids):
            return _invalid("findings", "finding ids must be unique")
        counts = {"verified_fixed": 0, "verified_mitigated": 0, "unverified": 0, "open": 0, "accepted": 0}
        findings: list[dict[str, Any]] = []
        residual = weight_total = Decimal(0)
        worst = -1
        for item in sorted(items, key=lambda entry: entry["id"]):
            status, severity, commit = item["fix_status"], item["severity"], item["fix_commit"]
            if commit and not _is_commit(commit):
                return _invalid("findings", f"{item['id']}: fix_commit is not a git object id")
            weight = Decimal(SEVERITY_WEIGHT[severity])
            weight_total += weight
            if status in ("fixed", "mitigated"):
                lacking = [text for ok, text in ((bool(commit), "no fix commit"), (item["regression_test"], "no regression test"), (item["reaudited"], "not re-audited")) if not ok]
                if lacking:
                    counts["unverified"] += 1
                    factor, reason = _FIX_FACTOR["unverified"], f"{status} but not verified: {', '.join(lacking)}"
                elif status == "fixed":
                    counts["verified_fixed"] += 1
                    factor, reason = _FIX_FACTOR["verified_fixed"], ""
                else:
                    counts["verified_mitigated"] += 1
                    factor, reason = _FIX_FACTOR["verified_mitigated"], "mitigated, not fixed: residual risk remains"
            elif status == "open":
                counts["open"] += 1
                factor, reason = _FIX_FACTOR["unresolved"], "open"
            else:
                counts["accepted"] += 1
                reason = f"{status}: needs an L4 risk acceptance" if severity in ("high", "critical") else f"{status}: the risk is unresolved, not fixed"
                factor = _FIX_FACTOR["unresolved"]
            residual += weight * factor
            if factor > 0:
                worst = max(worst, _RANK[severity])
                findings.append(_finding(f"FIX-{item['id']}", severity, f"{item['id']} {reason}"))
        return {
            "state": "SUCCESS",
            "verified_fixed": counts["verified_fixed"],
            "verified_mitigated": counts["verified_mitigated"],
            "unverified": counts["unverified"],
            "open": counts["open"],
            "accepted": counts["accepted"],
            "residual_score": _f(residual, 4),
            "residual_pct": _f(residual * 100 / weight_total, 2) if weight_total else 0.0,
            "residual_level": SEVERITIES[worst] if worst >= 0 else "none",
            "release_ready": _verdict(findings) != "fail",
            "findings": _ordered(findings),
            "verdict": _verdict(findings),
        }

    return respond(payload, work)
