"""A28 Blockchain Infrastructure Engineer tools: pure checks over supplied chain-infrastructure facts.

Tools: chain_client_diversity (client share concentration), chain_node_sizing (disk, memory, IOPS and network floor),
chain_rpc_failover_plan (provider order, hedging, capacity), chain_confirmation_depth (confirmations and block tag by
finality model and value at risk), chain_l2_withdrawal_window (rollup withdrawal timeline), chain_indexer_lag
(head versus indexed block against a freshness SLO), chain_backfill_plan (log-query chunking and backfill duration),
chain_fee_policy (EIP-1559 base-fee projection and fee caps), chain_stuck_transactions (nonce gaps, stuck
transactions, replacement bids), chain_validator_slashing_risk (double signing and failover safety by key label),
chain_bridge_monitor (message lag, collateral invariant, pause advice), chain_network_guard (chain id versus
environment and approval level), chain_mempool_privacy (public versus private routing for MEV-exposed transactions).

Nothing here holds a key, signs, or calls a network: handlers decide over data the caller supplies. Key-like input is
refused by field name and never echoed. Public sources of the embedded reference data are named at each constant.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, localcontext
from collections.abc import Callable
from typing import Any

from swarm.tools.support import respond

# Chain ids: ethereum-lists/chains (https://github.com/ethereum-lists/chains, browsable as https://chainlist.org). All 28 ids were
# checked against eip155-<id>.json there; the display names are the networks' own brand names (the list says "Polygon Mainnet",
# "zkSync Mainnet", "Ethereum Hoodi"...), the class is this table's judgement. Holesky (shut down Oct 2025) and Goerli are
# deprecated but stay listed so a stale config is still classified as a testnet. 1337 and 31337 are the conventional local-chain
# ids (Ganache and geth --dev, Hardhat and Anvil); the registry lists unrelated test chains under them.
# Mumbai (80001), Ropsten (3), Rinkeby (4) and Kovan (42) are deprecated testnets added from their well-known ids; re-check them
# against that registry at the next refresh.
# id -> (name, class, aliases). class is "mainnet", "testnet" or "local". Aliases are normalised names ([a-z0-9-]).
CHAINS: dict[int, tuple[str, str, tuple[str, ...]]] = {
    1: ("Ethereum Mainnet", "mainnet", ("ethereum", "ethereum-mainnet", "eth", "homestead")),
    10: ("OP Mainnet", "mainnet", ("optimism", "op-mainnet", "op")),
    56: ("BNB Smart Chain Mainnet", "mainnet", ("bsc", "bnb", "bnb-smart-chain")),
    100: ("Gnosis", "mainnet", ("gnosis", "gnosis-chain", "xdai")),
    137: ("Polygon PoS", "mainnet", ("polygon", "polygon-pos", "matic")),
    324: ("ZKsync Era Mainnet", "mainnet", ("zksync", "zksync-era")),
    8453: ("Base", "mainnet", ("base", "base-mainnet")),
    42161: ("Arbitrum One", "mainnet", ("arbitrum", "arbitrum-one")),
    42220: ("Celo Mainnet", "mainnet", ("celo",)),
    43114: ("Avalanche C-Chain", "mainnet", ("avalanche", "avalanche-c-chain", "avax")),
    59144: ("Linea", "mainnet", ("linea", "linea-mainnet")),
    534352: ("Scroll", "mainnet", ("scroll", "scroll-mainnet")),
    5: ("Goerli", "testnet", ("goerli",)),
    97: ("BNB Smart Chain Testnet", "testnet", ("bsc-testnet", "bnb-testnet")),
    300: ("ZKsync Sepolia Testnet", "testnet", ("zksync-sepolia",)),
    10200: ("Gnosis Chiado", "testnet", ("chiado", "gnosis-chiado")),
    17000: ("Holesky", "testnet", ("holesky",)),
    43113: ("Avalanche Fuji", "testnet", ("fuji", "avalanche-fuji")),
    59141: ("Linea Sepolia", "testnet", ("linea-sepolia",)),
    80002: ("Polygon Amoy", "testnet", ("amoy", "polygon-amoy")),
    84532: ("Base Sepolia", "testnet", ("base-sepolia",)),
    421614: ("Arbitrum Sepolia", "testnet", ("arbitrum-sepolia",)),
    534351: ("Scroll Sepolia", "testnet", ("scroll-sepolia",)),
    560048: ("Hoodi", "testnet", ("hoodi",)),
    11155111: ("Sepolia", "testnet", ("sepolia", "ethereum-sepolia")),
    11155420: ("OP Sepolia", "testnet", ("op-sepolia", "optimism-sepolia")),
    80001: ("Polygon Mumbai", "testnet", ("mumbai", "polygon-mumbai")),
    3: ("Ropsten", "testnet", ("ropsten",)),
    4: ("Rinkeby", "testnet", ("rinkeby",)),
    42: ("Kovan", "testnet", ("kovan",)),
    1337: ("Local development chain", "local", ("ganache",)),
    31337: ("Hardhat/Anvil local chain", "local", ("hardhat", "anvil", "foundry")),
}
# Test-network words, matched inside a name's or host's letters: a long word is unambiguous ("SepoliaTestnet", "BaseSepolia").
TESTNET_NAME_WORDS = ("sepolia", "holesky", "goerli", "amoy", "chiado", "testnet", "devnet", "mumbai", "ropsten", "rinkeby", "kovan")
# Short words that count only as whole tokens: "fuji" sits inside "fujitsu" and "hoodi" inside "hoodie", both ordinary words.
TESTNET_NAME_TOKENS = ("fuji", "hoodi")
MAX_CHAIN_ID = 2**53 - 1  # the contract's bound: the largest integer a JSON number carries exactly in JavaScript (not an EIP-2294 value)
LEVELS = ("L0", "L1", "L2", "L3", "L4")  # autonomy ladder of the swarm: L2 testnet ops, L3 mainnet node change, L4 keys

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
# Client shares (see client_diversity): > 1/3 stops finality when that client fails, > 1/2 is the tool's own majority tier,
# >= 2/3 can finalize its own chain; all three are applied as exact integer comparisons in the handler.
# ethereum.org "Spin up your own Ethereum node": MINIMUM 2 cores, 16 GB RAM, 2 TB NVMe SSD, 25+ Mbit/s bandwidth; the same
# page (EIP-7870) recommends 8+ cores and 64 GB RAM when validating. Disk is decimal GB (10^9 bytes).
FLOOR_CORES, FLOOR_RAM_GB, FLOOR_DISK_GB, FLOOR_UPLINK_MBPS = 2, Decimal(16), Decimal(2000), Decimal(25)
VALIDATING_CORES, VALIDATING_RAM_GB = 8, Decimal(64)
DISK_SOFT_FILL, DISK_HARD_FILL = Decimal("0.8"), Decimal("0.9")  # keep 20% free for compaction, pruning and resync
IOPS_HEADROOM = Decimal("0.7")  # sustained peak above 70% of provisioned IOPS leaves no room for sync bursts
HEDGE_LOAD = Decimal("1.05")  # hedging after the p95 latency duplicates about 5% of requests (Dean and Barroso, "The Tail at Scale")
SLOTS_PER_EPOCH = 32  # Ethereum consensus spec; justification takes up to 2 epochs, finality up to 3 from a block's slot
POLICY_SAFE_MULTIPLE = 100  # policy constant of this tool: value above 100x the loss tolerance asks for finalized
MAX_NAKAMOTO_DEPTH = 2000
MAX_AMOUNT = Decimal(10) ** 30
LOGS_SAFETY = 2  # plan getLogs windows to fill at most half of the provider's response cap
PRICE_BUMP_PERCENT = 110  # go-ethereum txpool.pricebump default is 10: a replacement raises both fee fields by >= 10%
SANDWICH_KINDS = ("swap",)
FRONTRUN_KINDS = ("liquidation", "nft_mint", "arbitrage")


class _Bad(Exception):
    """A semantic problem the schema cannot express; carries the input property it concerns."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def _safe(work: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    def run() -> dict[str, Any]:
        try:
            return work()
        except _Bad as bad:
            return {"state": "INVALID_INPUT", "field": bad.field, "message": bad.message}

    return run


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr), so 0.1 is 0.1 and not its binary neighbour."""
    return Decimal(repr(value))


def _finding(fid: str, severity: str, summary: str) -> dict[str, Any]:
    if len(fid) > 120:  # a long public key plus a host must not collapse two findings onto one truncated id
        fid = fid[:107] + "~" + hashlib.sha256(fid.encode("utf-8")).hexdigest()[:12]
    return {"id": fid, "severity": severity, "summary": summary[:400]}


def _ordered(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"]))


def _verdict(findings: list[dict[str, Any]]) -> str:
    worst = max((SEVERITY_RANK[f["severity"]] for f in findings), default=0)
    return "fail" if worst >= 2 else "warn" if worst == 1 else "pass"


def _ts(field: str, value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _Bad(field, f"{field} must be an ISO-8601 timestamp with an explicit offset") from None
    if parsed.tzinfo is None:
        raise _Bad(field, f"{field} needs an explicit UTC offset")
    return parsed


def _iso(moment: datetime) -> str:
    """UTC text at whole seconds, rounded UP so a 'ready at' time never lies before the exact moment."""
    moment = moment.astimezone(timezone.utc)
    if moment.microsecond:
        moment = moment.replace(microsecond=0) + timedelta(seconds=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _secs(delta: timedelta) -> Decimal:
    """Exact seconds of a timedelta (microseconds kept), so a strict > against a limit is not decided by a truncated second."""
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 10**6


_SECRET_PREFIX = re.compile(
    r"(?:^|[\s:=,;\"'(])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|sk-[A-Za-z0-9_-]{16,}|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}"
    r"|ya29\.[A-Za-z0-9_.-]{20,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{2,})"
)
_SECRET_ASSIGN = re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|mnemonic|seed[_ -]?phrase|authorization)\s*[=:]\s*\S|\b(?:bearer|basic)\s+\S{8,}|-----BEGIN")
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_PUBLIC_HEX = re.compile(r"0[xX][0-9a-fA-F]{40}|(?:0[xX])?[0-9a-fA-F]{96}")  # an address and a 48-byte BLS public key are public data


def _secret_like(text: str) -> bool:
    """Input that could be a credential: a 32-byte hex key, a 12+ word phrase, a URL (it may carry user:pass or an API key),
    a known token or key-id shape, a key=value credential assignment, or any other long opaque token. Addresses and BLS
    public keys are public and pass."""
    if re.fullmatch(r"(0[xX])?[0-9a-fA-F]{64}", text):
        return True
    words = text.split()
    if len(words) >= 12 and all(w.isalpha() for w in words):
        return True
    if "://" in text or re.search(r"\S+:\S+@\S+", text) or _SECRET_PREFIX.search(text) or _SECRET_ASSIGN.search(text):
        return True
    if _PUBLIC_HEX.fullmatch(text):
        return False
    if _TOKEN_SHAPE.fullmatch(text) and sum(ch in "-_" for ch in text) <= 2 and re.search(r"\d", text) and re.search(r"[A-Za-z]", text):
        return True
    return False


def _label(field: str, text: str) -> str:
    """Free-text identifiers (client, provider, account, key, host names) are echoed in findings: a value that looks like a
    credential is refused by property name only, never repeated."""
    if _secret_like(text):
        raise _Bad(field, f"{field} holds a value that looks like a credential or key material; supply a label or a public identifier")
    return text


def _gwei_to_wei(value: object) -> int:
    return int((_dec(value) * 10**9).to_integral_value(ROUND_HALF_EVEN))


def _wei_to_gwei(wei: int) -> float:
    return round(wei / 10**9, 9)


def client_diversity(payload: dict[str, Any]) -> dict[str, Any]:
    """Share of one layer's clients: more than 1/3 is minor, more than 1/2 major, 2/3 or more blocker (exact integers).

    1/3 and 2/3 come from the Ethereum consensus spec: finality needs attesting balance * 3 >= total * 2, so a client that
    holds exactly 2/3 can finalize its own (buggy) chain, while taking strictly more than 1/3 offline stops finality.
    The 1/2 tier is this tool's own intermediate step (a majority wins fork choice), not a protocol constant."""

    def work() -> dict[str, Any]:
        clients = payload["clients"]
        names = [_label("clients", c["name"]).strip().lower() for c in clients]
        if any(not n for n in names):
            raise _Bad("clients", "client names must not be blank")
        if len(set(names)) != len(names):
            raise _Bad("clients", "client names must be unique ignoring case")
        total = sum(c["nodes"] for c in clients)
        if total <= 0:
            raise _Bad("clients", "at least one client needs a node count above zero")
        ordered = sorted(clients, key=lambda c: (-c["nodes"], c["name"].strip().lower()))
        findings = []
        shares = []
        hhi = Decimal(0)
        for client in ordered:
            nodes = client["nodes"]
            share = Decimal(nodes) * 100 / total
            hhi += share * share
            shares.append({"client": client["name"].strip(), "nodes": nodes, "share_pct": round(float(share), 4)})
            cid = client["name"].strip().lower()
            # exact integer cross-multiplication: share >= 2/3 is 3*nodes >= 2*total, share > 1/2 is 2*nodes > total, share > 1/3 is 3*nodes > total
            if 3 * nodes >= 2 * total:
                findings.append(_finding(f"supermajority:{cid}", "blocker", f"{cid} runs {float(share):.2f}% of the {payload['layer']} layer: at 2/3 or more a bug can be finalized as canonical"))
            elif 2 * nodes > total:
                findings.append(_finding(f"majority:{cid}", "major", f"{cid} runs {float(share):.2f}% of the {payload['layer']} layer: a bug can fork the majority away from the rest"))
            elif 3 * nodes > total:
                findings.append(_finding(f"stall-risk:{cid}", "minor", f"{cid} runs {float(share):.2f}% of the {payload['layer']} layer: its failure can stop finality"))
        active = sum(1 for c in clients if c["nodes"] > 0)
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "total_nodes": total,
            "active_clients": active,
            "largest_client": shares[0]["client"],
            "largest_share_pct": shares[0]["share_pct"],
            "hhi": round(float(hhi), 1),
            "effective_clients": round(float(Decimal(10000) / hhi), 2),
            "shares": shares,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def node_sizing(payload: dict[str, Any]) -> dict[str, Any]:
    """Disk runway to a horizon, memory, cores, uplink, IOPS headroom and disk class against the node floors.

    Floors are the ethereum.org minimums for an Ethereum full node (the 8 cores / 64 GB for a validating host are its
    EIP-7870 recommendation, the 2 TB disk is compared in decimal GB, and `uplink_mbps` is compared with the guide's 25 Mbit/s
    bandwidth figure). The 80 % soft and 90 % hard disk fill and the 70 % IOPS headroom are this tool's own policy."""

    def work() -> dict[str, Any]:
        if payload["node_type"] == "archive" and payload["sync_mode"] == "snap":
            raise _Bad("sync_mode", "an archive node keeps every historical state and must be synced in full mode from genesis, not snap")
        used, prov = _dec(payload["disk_used_gb"]), _dec(payload["disk_provisioned_gb"])
        if used > prov:
            raise _Bad("disk_used_gb", "disk_used_gb exceeds disk_provisioned_gb")
        growth, days = _dec(payload["growth_gb_per_day"]), payload["horizon_days"]
        projected = used + growth * days
        required = projected / DISK_SOFT_FILL
        fill = used / prov
        if used >= DISK_SOFT_FILL * prov:
            to_soft = Decimal(0)
        elif growth == 0:
            to_soft = Decimal(-1)
        else:
            to_soft = (DISK_SOFT_FILL * prov - used) / growth
        if used >= prov:
            to_full = Decimal(0)
        elif growth == 0:
            to_full = Decimal(-1)
        else:
            to_full = (prov - used) / growth
        iops_util = _dec(payload["peak_iops"]) / _dec(payload["provisioned_iops"])
        findings = []
        if payload["disk_kind"] == "hdd":
            findings.append(_finding("disk-hdd", "blocker", "spinning disks cannot sustain the random IO of state access: use NVMe or SSD"))
        elif payload["disk_kind"] == "network_ssd":
            findings.append(_finding("disk-network", "minor", "network-attached SSD adds latency to state reads: prefer local NVMe"))
        if fill >= DISK_HARD_FILL:
            findings.append(_finding("disk-nearly-full", "blocker", f"disk is {float(fill * 100):.1f}% full: pruning and compaction need free space, grow the volume now"))
        elif fill >= DISK_SOFT_FILL:
            findings.append(_finding("disk-fill-high", "major", f"disk is {float(fill * 100):.1f}% full, above the 80% soft limit"))
        if projected > DISK_SOFT_FILL * prov and fill < DISK_SOFT_FILL:
            findings.append(_finding("disk-horizon", "major", f"projected use {float(projected):.0f} GB passes 80% of the volume within {days} days: provision at least {math.ceil(required)} GB"))
        if prov < FLOOR_DISK_GB:
            findings.append(_finding("disk-below-floor", "minor", "provisioned disk is under the 2 TB minimum of the ethereum.org node guide"))
        if _dec(payload["memory_gb"]) < FLOOR_RAM_GB:
            findings.append(_finding("memory-below-min", "major", "memory is under the 16 GB minimum"))
        elif payload["validating"] and _dec(payload["memory_gb"]) < VALIDATING_RAM_GB:
            findings.append(_finding("memory-below-validating", "minor", "a node that also validates is advised 64 GB of memory"))
        if payload["cpu_cores"] < FLOOR_CORES:
            findings.append(_finding("cores-below-min", "major", "fewer than 2 CPU cores"))
        elif payload["validating"] and payload["cpu_cores"] < VALIDATING_CORES:
            findings.append(_finding("cores-below-validating", "minor", "a node that also validates is advised 8 cores"))
        if _dec(payload["uplink_mbps"]) < FLOOR_UPLINK_MBPS:
            findings.append(_finding("uplink-below-min", "major", "uplink is under the 25 Mbit/s minimum"))
        if iops_util > IOPS_HEADROOM:
            findings.append(_finding("iops-saturation", "major", f"peak IOPS is {float(iops_util * 100):.1f}% of provisioned: keep sustained peaks under 70%"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "projected_disk_gb": round(float(projected), 1),
            "required_disk_gb": round(float(required), 1),
            "disk_fill_pct": round(float(fill * 100), 2),
            "days_until_80pct": round(float(to_soft), 1),
            "days_until_full": round(float(to_full), 1),
            "iops_utilization_pct": round(float(iops_util * 100), 2),
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def rpc_failover_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Order eligible providers by p95 latency, hedge after the primary's p95, check capacity and N-1 survival.

    Hedging after the 95th percentile adds about 5 % load (Dean and Barroso, "The Tail at Scale", CACM 2013), the 1.05 factor
    applied to demand whenever two or more providers are eligible. Ordering by p95 is this tool's policy; rate limits are
    compared as given (the caller states requests or compute units per second)."""

    def work() -> dict[str, Any]:
        providers = payload["providers"]
        names = [_label("providers", p["name"]).strip().lower() for p in providers]
        if any(not n for n in names) or len(set(names)) != len(names):
            raise _Bad("providers", "provider names must be unique, non-blank and compared ignoring case")
        eligible, ineligible = [], []
        for p in providers:
            reasons = []
            if _dec(p["error_rate_pct"]) > _dec(payload["max_error_rate_pct"]):
                reasons.append("error-rate")
            if p["lag_blocks"] > payload["max_lag_blocks"]:
                reasons.append("block-lag")
            if payload["need_archive"] and not p["archive"]:
                reasons.append("no-archive")
            if reasons:
                ineligible.append({"name": p["name"].strip(), "reason": "+".join(reasons)})
            else:
                eligible.append(p)
        eligible.sort(key=lambda p: (_dec(p["p95_latency_ms"]), _dec(p["error_rate_pct"]), p["name"].strip().lower()))
        ineligible.sort(key=lambda r: r["name"].lower())
        demand = _dec(payload["demand_rps"])
        capacity = sum((_dec(p["rate_limit_rps"]) for p in eligible), Decimal(0))
        largest = max((_dec(p["rate_limit_rps"]) for p in eligible), default=Decimal(0))

        def needed(count: int) -> Decimal:
            return demand * HEDGE_LOAD if count >= 2 else demand

        meets = bool(eligible) and capacity >= needed(len(eligible))
        survives = len(eligible) >= 2 and capacity - largest >= needed(len(eligible) - 1)
        findings = []
        if not eligible:
            findings.append(_finding("no-eligible-provider", "blocker", "no provider passes the error-rate, block-lag and archive requirements"))
        elif len(eligible) == 1:
            findings.append(_finding("single-provider", "major", "only one eligible provider: there is nothing to fail over to"))
        if eligible and not meets:
            findings.append(_finding("capacity-short", "major", f"eligible rate limits total {float(capacity):.1f} rps for a demand of {float(needed(len(eligible))):.1f} rps including hedge load"))
        if len(eligible) >= 2 and not survives:
            findings.append(_finding("no-n-minus-1", "minor", "losing the largest eligible provider leaves too little rate limit for the demand"))
        for row in ineligible:
            findings.append(_finding(f"provider-excluded:{row['name'].lower()}", "info", f"{row['name']} excluded: {row['reason']}"))
        findings = _ordered(findings)
        hedge = float(math.ceil(_dec(eligible[0]["p95_latency_ms"]))) if len(eligible) >= 2 else -1.0
        primary_share = (min(Decimal(100), _dec(eligible[0]["rate_limit_rps"]) * 100 / needed(len(eligible))) if eligible else Decimal(0))
        return {
            "state": "SUCCESS",
            "primary": eligible[0]["name"].strip() if eligible else "",
            "order": [p["name"].strip() for p in eligible],
            "hedge_after_ms": hedge,
            "primary_share_pct": round(float(primary_share), 2),
            "eligible_count": len(eligible),
            "capacity_rps": round(float(capacity), 4),
            "capacity_after_loss_rps": round(float(capacity - largest), 4),
            "meets_demand": meets,
            "survives_single_loss": survives,
            "ineligible": ineligible,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def _attacker_success(q: float, z: int) -> float:
    """Nakamoto (2008) section 11: probability an attacker with hash share q ever catches up from z blocks behind.

    P = sum_{k<=z} Pois(k; z q/p) (q/p)^(z-k) + sum_{k>z} Pois(k; z q/p), summed as positive terms only so tiny
    probabilities keep their precision."""
    p = 1.0 - q
    ratio = q / p
    lam = z * ratio
    total = 0.0
    log_lam = math.log(lam)
    for k in range(z + 1):
        total += math.exp(-lam + k * log_lam - math.lgamma(k + 1)) * ratio ** (z - k)
    k = z + 1
    while True:
        term = math.exp(-lam + k * log_lam - math.lgamma(k + 1))
        total += term
        if term < 1e-18 * max(total, 1e-300) or k > z + 5000:
            break
        k += 1
    return min(1.0, total)


def confirmation_depth(payload: dict[str, Any]) -> dict[str, Any]:
    """Confirmations and block tag from the finality model, value at risk, loss tolerance and observed reorg depth.

    probabilistic: Nakamoto section 11 counts z blocks linked AFTER the block that holds the transaction, so the number of
    confirmations (that block counts as the first) is z + 1; a reorg of r blocks needs r + 1. ethereum_pos: 64 and 96
    are the worst-case slots until the block's epoch is justified (safe) or finalized (consensus spec: finality needs two
    consecutive justified epochs); the 100x value-to-tolerance switch between them is this tool's own policy. An observed reorg of r blocks bounds the depth too: it needs r + 1."""

    def work() -> dict[str, Any]:
        model = payload["finality_model"]
        value, tolerance = _dec(payload["value_at_risk_usd"]), _dec(payload["loss_tolerance_usd"])
        slot_s = _dec(payload["block_time_s"])
        reorg = payload["observed_max_reorg_blocks"]
        ratio = value / tolerance
        findings = []
        if model == "probabilistic":
            q = payload["attacker_share"]
            epsilon = float(min(Decimal(1), tolerance / value)) if value > 0 else 1.0
            if epsilon >= 1.0:
                after = 0  # losing the whole value is tolerated: the block that holds the transaction is enough
            else:
                lo, hi = 1, MAX_NAKAMOTO_DEPTH
                if _attacker_success(q, hi) > epsilon:
                    raise _Bad("attacker_share", "no depth within 2000 blocks keeps the catch-up probability under loss_tolerance_usd / value_at_risk_usd at this attacker share")
                while lo < hi:
                    mid = (lo + hi) // 2
                    if _attacker_success(q, mid) <= epsilon:
                        hi = mid
                    else:
                        lo = mid + 1
                after = lo
            depth, basis, tag = max(after, reorg) + 1, "nakamoto", "latest"
            if reorg > after:
                basis = "reorg-history"
                findings.append(_finding("reorg-history-dominates", "info", f"an observed {reorg}-block reorg needs {reorg + 1} confirmations, more than the attacker model ({after + 1})"))
        elif model == "ethereum_pos":
            if ratio <= 1:
                depth, tag, basis = max(1, reorg + 1), "latest", "loss-within-tolerance"
            else:
                safe = ratio <= POLICY_SAFE_MULTIPLE
                policy = 2 * SLOTS_PER_EPOCH if safe else 3 * SLOTS_PER_EPOCH
                tag, basis, depth = ("safe", "justified-checkpoint", policy) if safe else ("finalized", "economic-finality", policy)
                if reorg + 1 > policy:
                    depth, basis = reorg + 1, "reorg-history"
                    findings.append(_finding("reorg-history-dominates", "info", f"an observed {reorg}-block reorg needs {reorg + 1} confirmations, more than the policy depth ({policy})"))
        else:
            depth, tag, basis = 1, "n/a", "instant-finality"
            if reorg > 0:
                findings.append(_finding("bft-reorg-observed", "blocker", "a chain assumed to have instant finality reorganised: this breaks the finality model, investigate before trusting one confirmation"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "block_tag": tag,
            "required_confirmations": depth,
            "wait_seconds": round(float(slot_s * depth), 2),
            "risk_ratio": round(float(ratio), 4),
            "basis": basis,
            "findings": findings,
        }

    return respond(payload, _safe(work))


def l2_withdrawal_window(payload: dict[str, Any]) -> dict[str, Any]:
    """Withdrawal timeline of a rollup: batch posting, proof, challenge window and L1 finality against a policy.

    Delays are rounded up to whole seconds and the ready time up to the next second. The 24 h floor for an optimistic
    challenge window is this tool's policy, not a protocol rule; the 7 day reference is OP Mainnet (Arbitrum One: about 6.4 days)."""

    def work() -> dict[str, Any]:
        started, as_of = _ts("initiated_at", payload["initiated_at"]), _ts("as_of", payload["as_of"])
        if as_of < started:
            raise _Bad("as_of", "as_of precedes initiated_at")
        optimistic = payload["rollup_type"] == "optimistic"
        window_h = _dec(payload["challenge_window_hours"])
        if optimistic and window_h <= 0:
            raise _Bad("challenge_window_hours", "an optimistic rollup needs a challenge window above zero")
        post = int((_dec(payload["batch_post_delay_min"]) * 60).to_integral_value(rounding=ROUND_CEILING))
        proof = int((_dec(payload["proof_delay_hours"]) * 3600).to_integral_value(rounding=ROUND_CEILING))
        window = int((window_h * 3600).to_integral_value(rounding=ROUND_CEILING))
        finality = int((_dec(payload["l1_finality_min"]) * 60).to_integral_value(rounding=ROUND_CEILING))
        total = post + proof + window + finality
        elapsed = _secs(as_of - started)
        try:
            ready_at = started + timedelta(seconds=total)
            ready_text = _iso(ready_at)
        except OverflowError:
            raise _Bad("initiated_at", "initiated_at plus the withdrawal delays leaves the representable date range") from None
        if elapsed < post:
            stage = "batch-posting"
        elif elapsed < post + proof:
            stage = "proof-pending"
        elif elapsed < post + proof + window:
            stage = "challenge-window" if optimistic else "execution-delay"
        elif elapsed < total:
            stage = "l1-finality"
        else:
            stage = "claimable"
        findings = []
        if optimistic and window < 24 * 3600:
            findings.append(_finding("challenge-window-too-short", "major", "a challenge window under 24 hours is below this tool's policy floor and leaves fraud-proof participants little time to detect and dispute"))
        elif optimistic and window < 168 * 3600:
            findings.append(_finding("challenge-window-below-7-days", "info", "the window is shorter than 7 days (OP Mainnet uses 7 days, Arbitrum One about 6.4 days)"))
        if not optimistic and window > 0:
            findings.append(_finding("validity-exit-delay", "info", "a validity rollup with an execution delay: the delay is a security time-lock, not a proof wait"))
        if _dec(total) > _dec(payload["max_wait_hours"]) * 3600:
            findings.append(_finding("wait-exceeds-policy", "minor", f"the {total / 3600:.2f} hour wait exceeds the {payload['max_wait_hours']} hour policy: use a liquidity-provider fast exit and count its trust assumptions"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "withdrawable_at": ready_text,
            "total_wait_hours": round(total / 3600, 4),
            "remaining_hours": round(float(max(Decimal(0), total - elapsed) / 3600), 4),
            "ready": elapsed >= total,
            "stage": stage,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def indexer_lag(payload: dict[str, Any]) -> dict[str, Any]:
    """Head versus indexed block over time: lag in blocks and seconds, trend, catch-up ETA and freshness SLO breach.

    Rates use the first and last sample. The 1x (major) and 3x (blocker) SLO tiers are this tool's policy."""

    def work() -> dict[str, Any]:
        samples = payload["samples"]
        stamps = [_ts("samples", s["observed_at"]) for s in samples]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise _Bad("samples", "samples must be listed with strictly increasing observed_at")
        for s in samples:
            if s["indexed_block"] > s["head_block"]:
                raise _Bad("samples", "indexed_block exceeds head_block in a sample: the head source is behind the indexer")
        if any(b["head_block"] < a["head_block"] for a, b in zip(samples, samples[1:])):
            raise _Bad("samples", "head_block decreased between samples")
        first, last = samples[0], samples[-1]
        span = _secs(stamps[-1] - stamps[0])
        head_adv, idx_adv = last["head_block"] - first["head_block"], last["indexed_block"] - first["indexed_block"]
        head_rate, idx_rate = Decimal(head_adv) / span, Decimal(idx_adv) / span
        lag = last["head_block"] - last["indexed_block"]
        lag_s = Decimal(lag) * _dec(payload["block_time_s"])
        slo = _dec(payload["freshness_slo_s"])
        if lag == 0:
            trend, eta = "caught-up", Decimal(0)
        elif idx_rate > head_rate:
            trend, eta = "closing", Decimal(lag) / (idx_rate - head_rate)
        elif idx_rate == head_rate:
            trend, eta = "steady", Decimal(-1)
        else:
            trend, eta = "growing", Decimal(-1)
        findings = []
        if lag_s > 3 * slo:
            findings.append(_finding("slo-breach-severe", "blocker", f"indexed data is {float(lag_s):.0f} s behind, more than 3x the {float(slo):.0f} s freshness SLO"))
        elif lag_s > slo:
            findings.append(_finding("slo-breach", "major", f"indexed data is {float(lag_s):.0f} s behind the {float(slo):.0f} s freshness SLO"))
        if lag > 0 and idx_adv <= 0:
            findings.append(_finding("indexer-stalled", "blocker", "the indexed block did not advance across the samples while behind the head"))
        if head_adv == 0:
            findings.append(_finding("head-stalled", "major", "the chain head did not advance across the samples: the chain or the head source is stalled"))
        if trend == "growing" and idx_adv > 0:
            findings.append(_finding("lag-growing", "major" if lag_s > slo else "minor", "the chain grows faster than the indexer processes: lag keeps increasing"))
        if idx_adv < 0:
            findings.append(_finding("indexer-rewound", "info", "the indexed block moved backwards: a reorg rollback or a reindex"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "lag_blocks": lag,
            "lag_seconds": round(float(lag_s), 2),
            "head_rate_bps": round(float(head_rate), 4),
            "index_rate_bps": round(float(idx_rate), 4),
            "trend": trend,
            "eta_catch_up_s": round(float(eta), 1),
            "within_slo": lag_s <= slo,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def backfill_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Size eth_getLogs windows to provider limits, then duration, catch-up against the moving head and bisect depth.

    A window is planned to fill at most half of the provider's response cap (this tool's safety factor of 2), so a busier
    block than the average still fits; the average logs per block is the caller's measurement."""

    def work() -> dict[str, Any]:
        start, end = payload["from_block"], payload["to_block"]
        if end < start:
            raise _Bad("to_block", "to_block must not precede from_block")
        per_block, cap = _dec(payload["est_logs_per_block"]), payload["max_logs_per_response"]
        findings = []
        if per_block > cap:
            findings.append(_finding("block-exceeds-response-cap", "blocker", "one block alone is expected to return more logs than the response cap: filter by address and topic or read receipts instead"))
            chunk = 1
        elif per_block > 0:
            chunk = max(1, min(payload["max_block_range"], int(Decimal(cap) / (per_block * LOGS_SAFETY))))
            if per_block * LOGS_SAFETY > cap:
                findings.append(_finding("no-log-margin", "major", f"one average block returns {per_block} logs against a response cap of {cap}: a busier block exceeds the cap and the window has no margin; filter by address or topic to shrink the average"))
        else:
            chunk = payload["max_block_range"]
        blocks = end - start + 1
        chunks = -(-blocks // chunk)
        requests = chunks * payload["requests_per_chunk"]
        worker_rps = Decimal(payload["workers"]) * 1000 / _dec(payload["request_latency_ms"])
        limit = _dec(payload["provider_rate_limit_rps"])
        effective = min(worker_rps, limit)
        limited_by = "rate-limit" if limit < worker_rps else "workers"
        needed = math.ceil(limit * _dec(payload["request_latency_ms"]) / 1000)
        if limited_by == "rate-limit" and payload["workers"] > needed:
            findings.append(_finding("workers-exceed-rate-limit", "info", f"{payload['workers']} workers exceed what the rate limit feeds: {needed} saturate it and extra workers only trigger 429s"))
        processing = effective / payload["requests_per_chunk"] * chunk
        head = Decimal(1) / _dec(payload["block_time_s"])
        catches = processing > head
        duration = Decimal(blocks) / processing
        to_head = Decimal(blocks) / (processing - head) if catches else Decimal(-1)
        if not catches:
            findings.append(_finding("never-catches-up", "blocker", "the backfill processes fewer blocks per second than the chain produces: raise workers, the rate limit or the window size"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "blocks": blocks,
            "chunk_size_blocks": chunk,
            "chunks": chunks,
            "requests": requests,
            "expected_logs_per_chunk": round(float(per_block * chunk), 2),
            "effective_rps": round(float(effective), 4),
            "limited_by": limited_by,
            "workers_needed": needed,
            "duration_s": round(float(duration), 1),
            "catches_up": catches,
            "time_to_head_s": round(float(to_head), 1),
            "bisect_depth": (chunk - 1).bit_length(),
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def _next_base_fee(base: int, gas_used: int, gas_limit: int, elasticity: int, denominator: int) -> int:
    """EIP-1559 base-fee update with the spec's integer arithmetic."""
    target = gas_limit // elasticity
    if gas_used == target:
        return base
    if gas_used > target:
        return base + max(base * (gas_used - target) // target // denominator, 1)
    return max(base - base * (target - gas_used) // target // denominator, 0)


def fee_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """EIP-1559 fee policy: exact next base fee, projections, capped max fee and priority fee, runway in full blocks."""

    def work() -> dict[str, Any]:
        limit, elasticity, denominator = payload["gas_limit"], payload["elasticity_multiplier"], payload["base_fee_change_denominator"]
        if limit // elasticity < 1:
            raise _Bad("gas_limit", "gas_limit divided by elasticity_multiplier must be at least 1")
        if payload["latest_gas_used"] > limit:
            raise _Bad("latest_gas_used", "latest_gas_used exceeds gas_limit")
        if _dec(payload["priority_cap_gwei"]) > _dec(payload["max_fee_cap_gwei"]):
            raise _Bad("priority_cap_gwei", "the priority cap must not exceed the max-fee cap (a transaction needs maxPriorityFeePerGas <= maxFeePerGas)")
        horizon = payload["horizon_blocks"]
        latest = _gwei_to_wei(payload["latest_base_fee_gwei"])
        next_fee = _next_base_fee(latest, payload["latest_gas_used"], limit, elasticity, denominator)
        assumed_used = int(_dec(payload["assumed_fullness"]) * limit)
        projected = worst = next_fee
        for _ in range(horizon - 1):
            projected = _next_base_fee(projected, assumed_used, limit, elasticity, denominator)
            worst = _next_base_fee(worst, limit, limit, elasticity, denominator)
        priority_cap, fee_cap = _gwei_to_wei(payload["priority_cap_gwei"]), _gwei_to_wei(payload["max_fee_cap_gwei"])
        wanted = _gwei_to_wei(payload["priority_fee_gwei"])
        priority = min(wanted, priority_cap)
        uncapped = worst + priority
        max_fee = min(uncapped, fee_cap)
        survived, base = 0, next_fee
        while survived < 200 and base <= max_fee:
            survived += 1
            base = _next_base_fee(base, limit, limit, elasticity, denominator)
        effective_tip = max(0, min(priority, max_fee - next_fee))
        findings = []
        if next_fee > fee_cap:
            findings.append(_finding("cap-below-next-base-fee", "blocker", "the max-fee cap is below the next block's base fee: no transaction under this policy can be included"))
        elif worst > fee_cap:
            findings.append(_finding("max-fee-capped", "major", f"the cap binds: it covers the base fee for {survived} consecutive blocks (the next block, then full blocks), the horizon asks for {horizon}"))
        elif uncapped > fee_cap:
            findings.append(_finding("max-fee-trims-tip", "minor", f"the cap covers the worst-case base fee over the {horizon}-block horizon but leaves only {_wei_to_gwei(fee_cap - worst)} gwei of the {_wei_to_gwei(priority)} gwei tip at its end"))
        if wanted > priority_cap:
            findings.append(_finding("priority-capped", "info", "the requested priority fee was cut to the priority cap"))
        if payload["latest_gas_used"] > limit // elasticity:
            findings.append(_finding("base-fee-rising", "info", "the latest block was above its gas target: the base fee rises in the next block"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "next_base_fee_gwei": _wei_to_gwei(next_fee),
            "projected_base_fee_gwei": _wei_to_gwei(projected),
            "worst_case_base_fee_gwei": _wei_to_gwei(worst),
            "priority_fee_gwei": _wei_to_gwei(priority),
            "max_fee_gwei": _wei_to_gwei(max_fee),
            "capped": uncapped > fee_cap,
            "full_blocks_survived": survived,
            "effective_tip_next_gwei": _wei_to_gwei(effective_tip),
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def _gap_text(gaps: list[tuple[int, int]]) -> str:
    shown = [str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in gaps[:5]]
    return ",".join(shown) + (",..." if len(gaps) > 5 else "")


def stuck_transactions(payload: dict[str, Any]) -> dict[str, Any]:
    """Per account: nonce gaps, queued transactions behind them, underpriced or aged transactions, replacement bids."""

    def work() -> dict[str, Any]:
        as_of, base_wei = _ts("as_of", payload["as_of"]), _gwei_to_wei(payload["base_fee_gwei"])
        limit_s = payload["stuck_after_minutes"] * 60
        accounts = payload["accounts"]
        labels = [_label("accounts", a["account"]).strip().lower() for a in accounts]
        if len(set(labels)) != len(labels):
            raise _Bad("accounts", "account labels must be unique ignoring case")
        findings, replacements = [], []
        gap_accounts = missing_total = stuck_total = queued_total = 0
        for label, account in sorted(zip(labels, accounts), key=lambda pair: pair[0]):
            confirmed = account["confirmed_nonce"]
            pending = sorted(account["pending"], key=lambda t: t["nonce"])
            nonces = [t["nonce"] for t in pending]
            if len(set(nonces)) != len(nonces):
                raise _Bad("accounts", f"account {label} lists one nonce twice: a node holds one transaction per nonce")
            if nonces and nonces[0] < confirmed:
                raise _Bad("accounts", f"account {label} lists a pending nonce below its confirmed nonce")
            for tx in pending:
                seen = _ts("accounts", tx["first_seen_at"])
                if seen > as_of:
                    raise _Bad("accounts", f"account {label} nonce {tx['nonce']} was first seen after as_of")
                if _gwei_to_wei(tx["max_priority_fee_gwei"]) > _gwei_to_wei(tx["max_fee_gwei"]):
                    raise _Bad("accounts", f"account {label} nonce {tx['nonce']} has a priority fee above its max fee")
            expected, executable = confirmed, []
            for tx in pending:
                if tx["nonce"] != expected:
                    break
                executable.append(tx)
                expected += 1
            queued = pending[len(executable):]
            if queued:
                gaps, cursor = [], confirmed
                for n in nonces:
                    if n > cursor:
                        gaps.append((cursor, n - 1))
                    cursor = n + 1
                missing = sum(hi - lo + 1 for lo, hi in gaps)
                gap_accounts += 1
                missing_total += missing
                queued_total += len(queued)
                findings.append(_finding(f"nonce-gap:{label}", "major", f"account {label} misses {missing} nonce(s) (gaps {_gap_text(gaps)}): {len(queued)} transaction(s) wait unexecutable; fill each gap with a replacement or a 0-value self-transfer"))
            for tx in executable:
                max_fee, prio = _gwei_to_wei(tx["max_fee_gwei"]), _gwei_to_wei(tx["max_priority_fee_gwei"])
                age = int((as_of - _ts("accounts", tx["first_seen_at"])).total_seconds())
                reasons = []
                if max_fee < base_wei:
                    reasons.append("underpriced")
                if age >= limit_s:
                    reasons.append("aged")
                if not reasons:
                    continue
                stuck_total += 1
                # go-ethereum's txpool (legacypool list.Add) refuses a replacement unless BOTH fee fields are strictly higher
                # and at least pricebump (default 10) percent higher: so a zero tip needs at least 1 wei, not a 0 bump
                bump_prio = max(-(-prio * PRICE_BUMP_PERCENT // 100), prio + 1)
                bump_fee = max(-(-max_fee * PRICE_BUMP_PERCENT // 100), max_fee + 1, base_wei + bump_prio)
                replacements.append({"account": label, "nonce": tx["nonce"], "min_max_fee_gwei": _wei_to_gwei(bump_fee), "min_priority_fee_gwei": _wei_to_gwei(bump_prio)})
                severity = "major" if "underpriced" in reasons else "minor"
                findings.append(_finding(f"stuck:{label}:{tx['nonce']}", severity, f"account {label} nonce {tx['nonce']} is {'+'.join(reasons)} ({age // 60} min old): replace with max fee >= {_wei_to_gwei(bump_fee)} and priority >= {_wei_to_gwei(bump_prio)} gwei"))
        findings = _ordered(findings)
        replacements.sort(key=lambda r: (r["account"], r["nonce"]))
        return {
            "state": "SUCCESS",
            "accounts_checked": len(accounts),
            "gap_accounts": gap_accounts,
            "missing_nonces": missing_total,
            "queued_count": queued_total,
            "stuck_count": stuck_total,
            "replacements": replacements,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def _key_identity(ref: str) -> str:
    """A key reference compares as one identity: hex is 0x plus lowercase hex (optional prefix, any case); any other label is lowercase."""
    body = ref[2:] if ref[:2].lower() == "0x" else ref
    if re.fullmatch(r"[0-9A-Fa-f]+", body):
        return "0x" + body.lower()
    return ref.lower()


def validator_slashing_risk(payload: dict[str, Any]) -> dict[str, Any]:
    """Validator keys by label: the same key active twice, missing slashing protection, unsafe standby failover."""

    def work() -> dict[str, Any]:
        instances = payload["instances"]
        seen = set()
        for inst in instances:
            _label("instances", inst["key_ref"])
            _label("instances", inst["host"])
            pair = (_key_identity(inst["key_ref"]), inst["host"])
            if pair in seen:
                raise _Bad("instances", "one key_ref appears twice on the same host")
            seen.add(pair)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for inst in instances:
            by_key.setdefault(_key_identity(inst["key_ref"]), []).append(inst)
        findings, double_keys, unprotected = [], [], 0
        for key in sorted(by_key):
            group = sorted(by_key[key], key=lambda i: i["host"])
            active = [i for i in group if i["role"] == "active"]
            if len(active) >= 2:
                hosts = ",".join(i["host"] for i in active)
                if all(i["slashing_protection"] == "shared_signer" for i in active):
                    findings.append(_finding(f"multi-active-shared-signer:{key}", "info", f"key {key} is active on {hosts} behind one shared signer database: duties duplicate but conflicting messages are refused"))
                else:
                    double_keys.append(key)
                    findings.append(_finding(f"double-sign:{key}", "blocker", f"key {key} is active on {hosts}: two signers for one key produce slashable double votes or proposals"))
            elif not active:
                findings.append(_finding(f"no-active-instance:{key}", "info", f"key {key} has standby instances only: nothing is validating with it"))
            for inst in group:
                guard, host = inst["slashing_protection"], inst["host"]
                if inst["role"] == "active":
                    if guard == "none":
                        unprotected += 1
                        findings.append(_finding(f"no-slashing-db:{key}:{host}", "major", f"active key {key} on {host} has no slashing-protection database"))
                    continue
                if guard == "none":
                    unprotected += 1
                    findings.append(_finding(f"standby-unprotected:{key}:{host}", "major", f"standby key {key} on {host} has no slashing protection: a failover can re-sign what the active host signed"))
                elif guard == "local_db":
                    unprotected += 1
                    findings.append(_finding(f"standby-stale-history:{key}:{host}", "major", f"standby key {key} on {host} has an empty local database: import the active host's EIP-3076 interchange file before activation"))
                if guard != "shared_signer":
                    if not inst["doppelganger_check"]:
                        findings.append(_finding(f"standby-no-doppelganger:{key}:{host}", "minor", f"standby key {key} on {host} starts without a doppelganger check"))
                    if inst["failover_wait_epochs"] < 2:
                        findings.append(_finding(f"failover-too-fast:{key}:{host}", "major", f"standby key {key} on {host} activates after {inst['failover_wait_epochs']} epoch(s): wait at least 2 epochs after the active host stops (this tool's policy; Lighthouse doppelganger protection itself waits 2-3 epochs)"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "key_count": len(by_key),
            "instance_count": len(instances),
            "double_sign_keys": double_keys,
            "unprotected_count": unprotected,
            "slashing_exposed": bool(double_keys),
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def _amount(field: str, text: str) -> Decimal:
    try:
        value = Decimal(text)
    except Exception:  # noqa: BLE001 - decimal.InvalidOperation is the only expected failure
        raise _Bad(field, f"{field} must be a decimal number written as text") from None
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT:
        raise _Bad(field, f"{field} must be an amount between 0 and 10^30")
    return value


def _exact_number(value: Decimal) -> int | float:
    """An integral amount as an exact int; any other amount as a float to 6 places (the exact text goes in a finding when they differ)."""
    return int(value) if value == value.to_integral_value() else round(float(value), 6)


def _surplus(locked: Decimal, minted: Decimal) -> Decimal:
    """The exact difference: each amount is at most 40 characters, so 120 digits of working precision cannot round it."""
    with localcontext() as ctx:
        ctx.prec = 120
        return locked - minted


def _exact_text(value: Decimal) -> str:
    """The exact decimal text of an amount, without exponent and without trailing zeros."""
    with localcontext() as ctx:
        ctx.prec = 120
        return format(value.normalize(), "f")


def bridge_monitor(payload: dict[str, Any]) -> dict[str, Any]:
    """Lock-and-mint bridge health: message lag, overdue in-flight messages, collateral invariant, pause advice."""

    def work() -> dict[str, Any]:
        as_of = _ts("as_of", payload["as_of"])
        limit_s = _dec(payload["max_lag_minutes"]) * 60
        locked, minted = _amount("locked_amount", payload["locked_amount"]), _amount("minted_amount", payload["minted_amount"])
        messages = payload["messages"]
        if len({m["id"] for m in messages}) != len(messages):
            raise _Bad("messages", "message ids must be unique")
        delivered_lags, pending_ages = [], []
        for index, m in sorted(enumerate(messages), key=lambda pair: pair[1]["id"]):
            sent = _ts("messages", m["sent_at"])
            if sent > as_of:
                raise _Bad("messages", f"message #{index} was sent after as_of")
            if m.get("delivered_at"):
                done = _ts("messages", m["delivered_at"])
                if done < sent or done > as_of:
                    raise _Bad("messages", f"message #{index} needs sent_at <= delivered_at <= as_of")
                delivered_lags.append(_secs(done - sent))
            else:
                pending_ages.append(_secs(as_of - sent))
        overdue = sum(1 for a in pending_ages if a > limit_s)
        late = sum(1 for lag in delivered_lags if lag > limit_s)
        delivered_lags.sort()
        p95 = delivered_lags[math.ceil(0.95 * len(delivered_lags)) - 1] if delivered_lags else Decimal(0)
        overdue_ratio = Decimal(overdue) / len(pending_ages) if pending_ages else Decimal(0)
        under = minted > locked
        pause = under or (bool(pending_ages) and overdue_ratio >= _dec(payload["pause_overdue_ratio"]))
        findings = []
        if under:
            findings.append(_finding("undercollateralized", "blocker", f"{format(minted - locked, 'f')} more is minted than locked: the bridge is insolvent, the classic signature of a forged-message exploit"))
        if pause and not payload["paused"]:
            findings.append(_finding("pause-recommended", "blocker", "pause conditions are met and the bridge is not paused: escalate for a human pause decision"))
        if payload["paused"] and not pause:
            findings.append(_finding("paused-without-trigger", "info", "the bridge is paused while collateral and lag are healthy: review whether to resume"))
        surplus = _surplus(locked, minted)
        surplus_number = _exact_number(surplus)
        if isinstance(surplus_number, float) and Decimal(repr(surplus_number)) != surplus:
            findings.append(_finding("surplus-not-exact-in-number", "info", f"the collateral surplus is exactly {_exact_text(surplus)}; collateral_surplus holds the nearest float, which does not carry every digit"))
        if overdue:
            findings.append(_finding("messages-overdue", "major", f"{overdue} of {len(pending_ages)} in-flight message(s) are older than {payload['max_lag_minutes']} minutes"))
        if late:
            findings.append(_finding("late-deliveries", "minor", f"{late} delivered message(s) took longer than {payload['max_lag_minutes']} minutes"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "message_count": len(messages),
            "delivered_count": len(delivered_lags),
            "pending_count": len(pending_ages),
            "overdue_count": overdue,
            "late_delivered_count": late,
            "p95_delivery_lag_min": round(float(p95 / 60), 2),
            "max_pending_age_min": round(float(max(pending_ages, default=Decimal(0)) / 60), 2),
            "collateral_surplus": surplus_number,
            "collateral_surplus_exact": _exact_text(surplus),
            "undercollateralized": under,
            "pause_recommended": pause,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))


def network_guard(payload: dict[str, Any]) -> dict[str, Any]:
    """Chain id versus environment and the autonomy level an operation needs; unknown chain ids count as mainnet.

    Policy, not a protocol rule: a non-production environment may not target a mainnet-grade chain even for a read, and the
    production environment may not target a testnet or local chain."""

    def work() -> dict[str, Any]:
        chain_id, env, op = payload["target_chain_id"], payload["environment"], payload["operation"]
        known = CHAINS.get(chain_id)
        net_class = known[1] if known else "unknown"
        mainnet_grade = net_class in ("mainnet", "unknown")
        if op == "validator_key_op":
            required = "L4"
        elif op == "node_change":
            required = "L3" if mainnet_grade else "L2"
        else:
            required = "L0"
        approved = LEVELS.index(payload["approved_level"])
        findings, approval_only = [], True
        if known is None:
            findings.append(_finding("unknown-chain-id", "minor", f"chain id {chain_id} is not in the reference table: it is treated as mainnet-grade"))
        if env != "production" and mainnet_grade:
            approval_only = False
            findings.append(_finding("environment-mismatch", "blocker", f"a {env} environment targets {'a mainnet' if known else 'an unknown'} chain (id {chain_id}): point it at a testnet or a local chain"))
        if known and env == "production" and net_class in ("testnet", "local"):
            approval_only = False
            findings.append(_finding("production-on-testnet", "major", f"the production environment targets {known[0]} (id {chain_id}), a {net_class} chain"))
        rpc = payload.get("rpc_chain_id")
        if rpc is not None and rpc != chain_id:
            approval_only = False
            findings.append(_finding("rpc-chain-mismatch", "blocker", f"the endpoint reports chain id {rpc} but the target is {chain_id}: signatures and replay protection (EIP-155) would bind to the wrong chain"))
        if op != "read" and mainnet_grade and not payload["mainnet_approved"]:
            findings.append(_finding("mainnet-not-approved", "blocker", "a mainnet-grade target needs an explicit mainnet approval before any change"))
        if approved < LEVELS.index(required):
            findings.append(_finding("insufficient-approval", "blocker", f"{op} needs {required} approval, {payload['approved_level']} granted"))
        findings = _ordered(findings)
        verdict = _verdict(findings)
        decision = "allow" if verdict != "fail" else "needs_approval" if approval_only else "block"
        return {
            "state": "SUCCESS",
            "network_class": net_class,
            "chain_name": known[0] if known else "",
            "required_level": required,
            "decision": decision,
            "verdict": verdict,
            "findings": findings,
        }

    return respond(payload, _safe(work))


def mempool_privacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a transaction through the public mempool, a private relay or a sequencer by its MEV exposure."""

    def work() -> dict[str, Any]:
        kind, network = payload["tx_kind"], payload["network_kind"]
        value, tolerance = _dec(payload["value_usd"]), _dec(payload["max_acceptable_loss_usd"])
        sandwich = kind in SANDWICH_KINDS
        # a sandwich can push the price to the slippage limit: loss is bounded by value x slippage tolerance
        max_loss = value * _dec(payload["slippage_bps"]) / 10000 if sandwich else Decimal(0)
        cap_bps = (min(Decimal(10000), tolerance * 10000 / value) if value > 0 else Decimal(10000)) if sandwich else Decimal(-1)
        findings = []
        if network == "l2_sequencer":
            route = "sequencer_native"
            exposure = "low" if sandwich and max_loss > tolerance else "none"
            findings.append(_finding("no-public-mempool", "info", "transactions go to the sequencer, not a public mempool: ordering is the sequencer's policy and a trust assumption"))
        else:
            if sandwich and max_loss > tolerance:
                exposure = "high"
            elif kind in FRONTRUN_KINDS:
                exposure = "medium"
            else:
                exposure = "low" if sandwich and max_loss > 0 else "none"
            exposed = exposure in ("high", "medium")
            route = "private_relay" if exposed and payload["private_route_available"] else "public_mempool"
            if exposed and not payload["private_route_available"]:
                findings.append(_finding("no-private-route", "major", "the transaction is MEV-exposed but no private route exists on this chain: " + ("lower the slippage to the cap" if sandwich else "accept the exposure or batch through a protocol-level auction")))
            if route == "private_relay" and not payload["revert_protection"]:
                findings.append(_finding("no-revert-protection", "minor", "the private route may still land a reverting transaction and charge its gas"))
        if sandwich and not payload["has_deadline"]:
            findings.append(_finding("no-deadline", "major", "a swap without a deadline can be held by a block builder and executed at a later, worse price"))
        findings = _ordered(findings)
        changed = network == "l1_public_mempool" and route == "private_relay" and payload["submitted_route"] != "private"
        return {
            "state": "SUCCESS",
            "route": route,
            "exposure": exposure,
            "max_sandwich_loss_usd": round(float(max_loss), 2),
            "slippage_bps_cap": int(cap_bps) if cap_bps >= 0 else -1,
            "route_change_required": changed,
            "verdict": _verdict(findings),
            "findings": findings,
        }

    return respond(payload, _safe(work))
