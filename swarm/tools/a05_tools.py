"""A05 backend tools: HTTP contract, dependency, and test-coverage checks over supplied data.

Tools:
- be_openapi_path_params: extract {name} path parameters from an OpenAPI route template.
- be_route_collision: find same-method routes whose templated shapes collide.
- be_method_status_check: check an HTTP method and status code against allowlists.
- be_semver_range_class: classify a semver range and whether it has an upper bound.
- be_license_allowlist_check: find packages whose license expression is not allowed.
- be_coverage_delta: compute the percentage-point coverage change between two runs.
- be_acceptance_coverage: compute acceptance-criteria coverage for a test plan.
- be_perf_regression: flag benchmarks slower than a percentage budget against their baseline.
- be_dependency_class: classify each proposed dependency change as L2 (allowlisted, same major) or L3.
- be_route_auth_coverage: list routes that have no auth marker and are not declared public.
- be_patch_shape_check: validate a code.patch JSON document and derive whether it is ready for review.

All tools are pure: every result is computed from the payload.
"""

from __future__ import annotations

import math
import re
from typing import Any, TypeGuard

from swarm.tools.support import respond

_PARAM_FORM = re.compile(r"\{[^{}/]*\}|:[A-Za-z_][A-Za-z0-9_]*")
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_METHOD = re.compile(r"[A-Za-z]{1,10}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}\Z")
_EXACT = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_COMPARATORS = (">=", "<=", ">", "<", "=")
_OR = re.compile(r"\s+OR\s+", re.IGNORECASE)
_AND = re.compile(r"\s+AND\s+", re.IGNORECASE)


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def openapi_path_params(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        template = payload["template"]
        if not template.startswith("/"):
            return _invalid("template", "template must start with '/'")
        names: list[str] = []
        current: list[str] | None = None
        for char in template:
            if char == "{":
                if current is not None:
                    return _invalid("template", "nested '{' in path template")
                current = []
            elif char == "}":
                if current is None:
                    return _invalid("template", "unmatched '}' in path template")
                name = "".join(current)
                if not _PARAM_NAME.match(name):
                    return _invalid("template", f"invalid parameter name {name!r}")
                names.append(name)
                current = None
            elif current is not None:
                current.append(char)
        if current is not None:
            return _invalid("template", "unclosed '{' in path template")
        duplicates = sorted({name for name in names if names.count(name) > 1})
        return {"state": "SUCCESS", "params": names, "param_count": len(names), "duplicates": duplicates}

    return respond(payload, work)


def _route_shape(path: str) -> str:
    shaped = _PARAM_FORM.sub("{}", path)
    return shaped.rstrip("/") or "/"


def route_collision(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        groups: dict[tuple[str, str], list[str]] = {}
        for index, route in enumerate(payload["routes"]):
            method = route["method"].strip().upper()
            path = route["path"].strip()
            if not method or not path.startswith("/"):
                return _invalid("routes", f"route {index} needs a method and a path starting with '/'")
            groups.setdefault((method, _route_shape(path)), []).append(path)
        collisions = [
            f"{method} {shape} <- {', '.join(paths)}"
            for (method, shape), paths in groups.items()
            if len(paths) > 1
        ]
        return {
            "state": "SUCCESS",
            "routes_checked": len(payload["routes"]),
            "collision_count": len(collisions),
            "collisions": collisions,
        }

    return respond(payload, work)


def method_status_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        method = payload["method"].strip()
        if not _METHOD.match(method):
            return _invalid("method", "method must be 1-10 ASCII letters")
        status = payload["status"]
        if not 100 <= status <= 599:
            return _invalid("status", "status must be between 100 and 599")
        method_upper = method.upper()
        allowed_methods = {item.strip().upper() for item in payload["allowed_methods"]}
        method_ok = method_upper in allowed_methods
        status_ok = status in set(payload["allowed_statuses"])
        return {
            "state": "SUCCESS",
            "method": method_upper,
            "status_class": status // 100,
            "method_allowed": method_ok,
            "status_allowed": status_ok,
            "verdict": "PASS" if method_ok and status_ok else "FAIL",
        }

    return respond(payload, work)


def semver_range_class(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        text = payload["range"].strip()
        if text in ("", "*", "x", "X"):
            kind, bounded = "wildcard", False
        elif "||" in text:
            return _invalid("range", "union ranges ('||') are not supported")
        elif " - " in text:
            left, _, right = text.partition(" - ")
            if not (_VERSION.match(left.strip()) and _VERSION.match(right.strip())):
                return _invalid("range", "hyphen range needs two versions")
            kind, bounded = "hyphen", True
        elif text[0] in "^~":
            if not _VERSION.match(text[1:]):
                return _invalid("range", f"{text[0]} range needs a version")
            kind, bounded = ("caret" if text[0] == "^" else "tilde"), True
        elif text[0] in "<>=":
            operators: list[str] = []
            for token in text.split():
                operator = next((op for op in _COMPARATORS if token.startswith(op)), None)
                if operator is None or not _VERSION.match(token[len(operator):]):
                    return _invalid("range", f"invalid comparator token {token!r}")
                operators.append(operator)
            kind = "comparator"
            bounded = any(op in ("<", "<=", "=") for op in operators)
        elif _EXACT.match(text):
            kind, bounded = "exact", True
        else:
            return _invalid("range", f"unrecognized semver range {text!r}")
        return {"state": "SUCCESS", "kind": kind, "upper_bounded": bounded}

    return respond(payload, work)


def _license_allowed(expression: str, allowed: set[str]) -> bool:
    # Parentheses are not evaluated; such expressions fail closed.
    if "(" in expression or ")" in expression:
        return False
    cleaned = expression.strip()
    if not cleaned:
        return False
    return any(
        all(term.strip().lower() in allowed for term in _AND.split(clause.strip()))
        for clause in _OR.split(cleaned)
    )


def license_allowlist_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        allowed = {item.strip().lower() for item in payload["allowlist"]}
        packages = payload["packages"]
        violations = [
            f"{package['name']} ({package['license'] or 'none'})"
            for package in packages
            if not _license_allowed(package["license"], allowed)
        ]
        return {
            "state": "SUCCESS",
            "checked_count": len(packages),
            "violation_count": len(violations),
            "violations": violations,
        }

    return respond(payload, work)


def coverage_delta(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        before = payload["before"]
        after = payload["after"]
        for field, value in (("before", before), ("after", after)):
            if not 0 <= value <= 100:
                return _invalid(field, "coverage must be between 0 and 100")
        # direction and regressed come from the same comparison; delta keeps full precision (only float
        # noise below 1e-12 is rounded away) so a tiny drop is never reported as flat.
        direction = "up" if after > before else "down" if after < before else "flat"
        raw = after - before
        delta = (round(raw, 12) or raw) + 0.0
        return {"state": "SUCCESS", "delta": delta, "direction": direction, "regressed": direction == "down"}

    return respond(payload, work)


def acceptance_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        criteria = list(dict.fromkeys(payload["criteria"]))
        if not criteria:
            return _invalid("criteria", "at least one acceptance criterion is required")
        criteria_set = set(criteria)
        covered = set(payload["covered"])
        uncovered = [item for item in criteria if item not in covered]
        return {
            "state": "SUCCESS",
            "total_count": len(criteria),
            "covered_count": len(criteria) - len(uncovered),
            "uncovered": uncovered,
            "unmapped": sorted(covered - criteria_set),
            "ratio": round((len(criteria) - len(uncovered)) / len(criteria), 4),
        }

    return respond(payload, work)


def _finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def perf_regression(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        benchmarks = payload["benchmarks"]
        limit = payload.get("max_regression_pct", 10)
        if not _finite_number(limit) or not 0 <= limit <= 1000:
            return _invalid("max_regression_pct", "max_regression_pct must be a number between 0 and 1000")
        if not isinstance(benchmarks, list) or not benchmarks:
            return _invalid("benchmarks", "at least one benchmark is required")
        seen: set[str] = set()
        regressions: list[dict[str, Any]] = []
        worst: float | None = None
        for index, item in enumerate(benchmarks):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                return _invalid("benchmarks", f"benchmark {index} needs a non-empty name")
            name = item["name"].strip()
            if name in seen:
                return _invalid("benchmarks", f"benchmark name {name!r} appears twice")
            seen.add(name)
            before, after = item.get("before_ms"), item.get("after_ms")
            if not _finite_number(before) or before <= 0:
                return _invalid("benchmarks", f"benchmark {name!r} needs before_ms above 0 (the baseline)")
            if not _finite_number(after) or after < 0:
                return _invalid("benchmarks", f"benchmark {name!r} needs after_ms of 0 or more")
            pct = (after - before) * 100.0 / before
            if not math.isfinite(pct):
                return _invalid("benchmarks", f"benchmark {name!r}: after_ms is too large relative to before_ms to compare")
            pct = round(pct, 4) + 0.0
            worst = pct if worst is None or pct > worst else worst
            if pct > limit:
                regressions.append({"name": name, "pct": pct})
        return {
            "state": "SUCCESS",
            "checked": len(benchmarks),
            "regressions": regressions,
            "worst_pct": worst,
            "regressed": bool(regressions),
        }

    return respond(payload, work)


_DEP_VERSION = re.compile(r"(?:==|[\^~=v])?([0-9]+)\.([0-9]+)\.([0-9]+)\Z")


def _dep_version(text: object) -> tuple[int, int, int] | None:
    match = _DEP_VERSION.match(text.strip()) if isinstance(text, str) else None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def _dep_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return name if name and len(name) <= 214 and not any(ch.isspace() for ch in name) else None


def dependency_class(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        manifest: dict[str, tuple[int, int, int]] = {}
        spelled: dict[str, str] = {}
        for package, version in payload["manifest"].items():
            name = _dep_name(package)
            parsed = _dep_version(version)
            if name is None or parsed is None:
                return _invalid("manifest", f"manifest entry {package!r} needs a package name and a MAJOR.MINOR.PATCH version")
            if name in spelled:
                return _invalid("manifest", f"manifest entries {spelled[name]!r} and {package!r} name the same package (names compare lower-cased)")
            spelled[name] = package
            manifest[name] = parsed
        allowlist = set()
        for entry in payload["allowlist"]:
            name = _dep_name(entry)
            if name is None:
                return _invalid("allowlist", "allowlist entries must be package names")
            allowlist.add(name)
        proposed = payload["proposed"]
        if not isinstance(proposed, list) or not proposed:
            return _invalid("proposed", "at least one proposed dependency is required")
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for index, entry in enumerate(proposed):
            name = _dep_name(entry.get("package")) if isinstance(entry, dict) else None
            target = _dep_version(entry.get("version")) if isinstance(entry, dict) else None
            if name is None:
                return _invalid("proposed", f"proposed {index} needs a package name")
            if target is None:
                return _invalid("proposed", f"proposed {name!r} needs a MAJOR.MINOR.PATCH version")
            if name in seen:
                return _invalid("proposed", f"package {name!r} is proposed twice")
            seen.add(name)
            current = manifest.get(name)
            if current is None:
                change, level, reason = "new", "L3", "not in the manifest: a new dependency needs a dependency.request"
            else:
                if target == current:
                    change = "unchanged"
                elif target < current:
                    change = "downgrade"
                elif target[0] != current[0] or (current[0] == 0 and target[1] != current[1]):
                    change = "major"
                elif target[1] != current[1]:
                    change = "minor"
                else:
                    change = "patch"
                if change == "downgrade":
                    level, reason = "L3", "downgrade is not a semver-compatible update"
                elif change == "major" and target[0] == current[0]:
                    level, reason = "L3", "a 0.x minor change may break callers (semver: 0.y.z is unstable); not semver-compatible"
                elif change == "major":
                    level, reason = "L3", "major version change is not semver-compatible"
                elif name not in allowlist:
                    level, reason = "L3", "package is not on the allowlist"
                else:
                    level, reason = "L2", "allowlisted and semver-compatible"
            items.append({"package": name, "change": change, "level": level, "reason": reason})
        return {"state": "SUCCESS", "items": items, "l3_count": sum(1 for item in items if item["level"] == "L3")}

    return respond(payload, work)


def route_auth_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        public: set[tuple[str, str]] = set()
        for index, entry in enumerate(payload["public"]):
            method = entry["method"].strip().upper()
            path = entry["path"].strip()
            if not method or not path.startswith("/"):
                return _invalid("public", f"public route {index} needs a method and a path starting with '/'")
            public.add((method, _route_shape(path)))
        unprotected: list[str] = []
        public_used = 0
        routes = payload["routes"]
        for index, route in enumerate(routes):
            method = route["method"].strip().upper()
            path = route["path"].strip()
            if not method or not path.startswith("/"):
                return _invalid("routes", f"route {index} needs a method and a path starting with '/'")
            if route["auth"]:
                continue
            if (method, _route_shape(path)) in public:
                public_used += 1
            else:
                unprotected.append(f"{method} {path}")
        return {
            "state": "SUCCESS",
            "checked": len(routes),
            "unprotected": unprotected,
            "public_used": public_used,
            "covered": not unprotected,
        }

    return respond(payload, work)


_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SEAT_PREFIX = re.compile(r"bot-0[0-6]-[a-z0-9-]+/")  # Programming Desk: gates.yml requires ^bot-0[0-6]-[a-z0-9-]+$ for the seat part
_CONTRACT_PIN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*@[0-9]+\.[0-9]+\.[0-9]+\Z")
_PATCH_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CHECK_VALUE = re.compile(r"(?:pass|fail|skipped(?::[^\s:][^\n]*)?)\Z")


def _is_branch_for(branch: object, task_id: str) -> bool:
    if not isinstance(branch, str):
        return False
    if branch == f"swarm/{task_id}":
        return True
    prefix = _SEAT_PREFIX.match(branch)
    return prefix is not None and branch[prefix.end():] == task_id


def patch_shape_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        patch = payload["patch"]
        if not isinstance(patch, dict):
            return _invalid("patch", "patch must be an object")
        problems: list[dict[str, Any]] = []

        def flag(field: str, message: str) -> None:
            problems.append({"field": field, "msg": message})

        task_id = patch.get("task_id")
        if not isinstance(task_id, str) or not _TASK_ID.match(task_id) or ".." in task_id:
            flag("task_id", "task_id must be a safe id: [A-Za-z0-9._-], starting alphanumeric, at most 128 chars")
        branch = patch.get("branch")
        if isinstance(task_id, str):
            if not _is_branch_for(branch, task_id):
                flag("branch", f"branch must be swarm/{task_id} or, in a Programming Desk repo, bot-0N-<seat>/{task_id} (N 0-6, seat lowercase a-z 0-9 -)")
        elif not isinstance(branch, str) or not (branch.startswith("swarm/") or _SEAT_PREFIX.match(branch)):
            flag("branch", "branch is required and must be swarm/<task_id> or bot-0N-<seat>/<task_id>")
        signed = patch.get("commits_signed")
        if not isinstance(signed, bool):
            flag("commits_signed", "commits_signed must be true or false")
        elif not signed:
            flag("commits_signed", "commits must be signed before review")
        pins = patch.get("contracts_bound")
        if not isinstance(pins, list) or not pins:
            flag("contracts_bound", "bind at least one contract as Name@MAJOR.MINOR.PATCH")
        else:
            for pin in pins:
                if not isinstance(pin, str) or not _CONTRACT_PIN.match(pin):
                    flag("contracts_bound", f"{pin!r} is not Name@MAJOR.MINOR.PATCH")
        tests = patch.get("tests_added")
        if isinstance(tests, bool) or not isinstance(tests, int):
            flag("tests_added", "tests_added must be an integer")
        elif tests <= 0:
            flag("tests_added", "tests_added must be above 0: a task without tests cannot enter review")
        digest = patch.get("digest")
        if not isinstance(digest, str) or not _PATCH_DIGEST.match(digest):
            flag("digest", "digest must be sha256: followed by 64 lowercase hex characters")
        checks = patch.get("checks")
        failed = False
        if not isinstance(checks, dict) or not checks:
            flag("checks", "checks must be a non-empty object of check name to pass, fail or skipped")
        else:
            for name, value in checks.items():
                if not isinstance(value, str) or not _CHECK_VALUE.match(value):
                    flag(f"checks.{name}", "value must be pass, fail or skipped[:reason]")
                elif value == "fail":
                    failed = True
        valid = not problems
        return {
            "state": "SUCCESS",
            "valid": valid,
            "problems": problems,
            "ready_for_review": valid and not failed,
        }

    return respond(payload, work)
