"""A22 GitOps & Progressive Delivery Engineer tools: pure analyses of GitOps manifests, rollouts and promotions.

Every handler works on JSON the caller already parsed (manifests, specs, metric samples, file lists); none talks to a
cluster, a repository or the clock. Findings are {id, severity, summary} with severity blocker|major|minor|info.
Lint-style tools answer `verdict` = fail (a blocker or major finding exists), warn (only minor findings) or pass; the
gate-style tools answer ready|waiting|blocked, the canary tool Successful|Failed|Inconclusive and the drift tool
clean|drifted|dangerous. Every result also carries `severity_counts` and at most 200 findings (worst first).

Tools:
  gitops_kustomize_check   kustomize overlay integrity: duplicate ids, namespace collisions, patch targets, patch ops
  gitops_helm_values_drift Helm values per environment: required keys, invariant keys, type and key drift
  gitops_appset_check      Argo CD ApplicationSet: generators, cluster selection, template keys, sync and rollout
  gitops_app_sync_policy   Argo CD Application / Flux Kustomization / HelmRelease sync policy and revision pinning
  gitops_sync_wave_check   Argo CD sync order: phases, waves, namespaces, CRDs before custom resources, dependencies
  gitops_prune_risk        what an automated or manual prune would delete (PVC, Namespace, CRD, ...) and the autonomy level
  gitops_rollout_check     Argo Rollouts canary / blue-green strategy validation and effective canary weights
  gitops_canary_verdict    canary analysis verdict from metric samples versus thresholds and baseline regression
  gitops_promotion_gate    promotion PR preconditions: stage order, checks, soak time, approvals, change freeze
  gitops_drift_classify    live-versus-git field diffs classified benign, drift or dangerous
  gitops_image_policy      image reference pinning and Flux-style semver / alphabetical / numerical update policy
  gitops_secret_scan       plaintext Secrets and credential patterns versus Sealed/External/SOPS secret references
  gitops_repo_layout       GitOps repository layout: environments, cluster registration, CODEOWNERS ownership

Public references (the small reference tables below follow them):
  Argo CD sync phases, waves and hooks: https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/
  Argo CD sync options and automated sync: https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/ and
    https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/
  ApplicationSet generators and templates: https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/
  Argo Rollouts: https://argo-rollouts.readthedocs.io/en/stable/features/specification/
  Flux Kustomization / HelmRelease / ImagePolicy: https://fluxcd.io/flux/components/kustomize/kustomizations/,
    https://fluxcd.io/flux/components/helm/helmreleases/, https://fluxcd.io/flux/components/image/imagepolicies/
  Kustomize: https://kubectl.docs.kubernetes.io/references/kustomize/kustomization/
  GitHub CODEOWNERS syntax: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners
"""
from __future__ import annotations

import json
import math
import posixpath
import re
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeGuard, TypedDict

from swarm.tools.support import respond

SEVERITIES = ("blocker", "major", "minor", "info")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
MAX_FINDINGS = 200  # findings listed; the counts always cover every finding
MAX_JSON_NODES = 100_000  # free-form inputs (manifests) are bounded in node count, depth and string length
MAX_JSON_DEPTH = 40
MAX_JSON_STRING = 65_536
MAX_SELECTOR_PATTERN = 256  # a Kubernetes name is at most 253 characters, so a longer pattern is not evaluated
MAX_PATCH_RESOURCE_PAIRS = 20_000  # patches x resources a kustomize check evaluates

# `kubectl api-resources --namespaced=false` (Kubernetes 1.34 API reference) for the built-in kinds plus Gateway API's GatewayClass;
# the extra_cluster_scoped_kinds input adds custom ones.
CLUSTER_SCOPED_KINDS = frozenset({
    "APIService", "CSIDriver", "CSINode", "CertificateSigningRequest", "ClusterRole", "ClusterRoleBinding", "ClusterTrustBundle",
    "ComponentStatus", "CustomResourceDefinition", "DeviceClass", "FlowSchema", "GatewayClass", "IPAddress", "IngressClass",
    "MutatingAdmissionPolicy", "MutatingAdmissionPolicyBinding", "MutatingWebhookConfiguration", "Namespace", "Node",
    "PersistentVolume", "PodSecurityPolicy", "PriorityClass", "PriorityLevelConfiguration", "ResourceSlice", "RuntimeClass",
    "SelfSubjectAccessReview", "SelfSubjectReview", "SelfSubjectRulesReview", "ServiceCIDR", "StorageClass", "StorageVersionMigration",
    "SubjectAccessReview", "TokenReview", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding",
    "ValidatingWebhookConfiguration", "VolumeAttachment", "VolumeAttributesClass",
})
WORKLOAD_KINDS = frozenset({"DaemonSet", "Deployment", "Job", "ReplicaSet", "ReplicationController", "StatefulSet"})

ARGO_API = "argoproj.io/v1alpha1"
WAVE_ANNOTATION = "argocd.argoproj.io/sync-wave"
HOOK_ANNOTATION = "argocd.argoproj.io/hook"
HOOK_DELETE_ANNOTATION = "argocd.argoproj.io/hook-delete-policy"
SYNC_OPTIONS_ANNOTATION = "argocd.argoproj.io/sync-options"
RESOURCES_FINALIZER = "resources-finalizer.argocd.argoproj.io"
FLUX_PRUNE_KEY = "kustomize.toolkit.fluxcd.io/prune"


def _invalid(field: str, message: str) -> dict[str, str]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finding(fid: str, severity: str, summary: str) -> dict[str, str]:
    """Every finding passes through _scrub, so no handler can quote a credential-shaped input value by accident."""
    return {"id": _scrub(fid), "severity": severity, "summary": _scrub(summary)}


def _counts(findings: list[dict[str, str]]) -> dict[str, int]:
    return {name: sum(1 for f in findings if f["severity"] == name) for name in SEVERITIES}


def _report(findings: list[dict[str, str]]) -> dict[str, Any]:
    """verdict, severity_counts and the worst MAX_FINDINGS findings (worst first, then by id) of a lint-style result."""
    ordered = sorted(findings, key=lambda f: (_SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))
    counts = _counts(ordered)
    verdict = "fail" if counts["blocker"] or counts["major"] else "warn" if counts["minor"] else "pass"
    return {"verdict": verdict, "severity_counts": counts, "findings": ordered[:MAX_FINDINGS]}


def _dig(value: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _list(value: object) -> list[Any]:
    """The value when it is a list, else an empty list: free-form manifests may hold any JSON type under any key."""
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict[str, Any]:
    """The value when it is a JSON object, else an empty object."""
    return value if isinstance(value, dict) else {}


def _dict_or_none(value: object) -> dict[str, Any] | None:
    """The value when it is a JSON object, else None."""
    return value if isinstance(value, dict) else None


def _tree_problem(*values: object) -> str | None:
    """Reason a free-form input is too large or deep to analyse (bounded work), else None."""
    stack = [(value, 1) for value in values]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            return f"more than {MAX_JSON_NODES} JSON nodes"
        if depth > MAX_JSON_DEPTH:
            return f"nested deeper than {MAX_JSON_DEPTH} levels"
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and len(item) > MAX_JSON_STRING:
            return f"contains a string longer than {MAX_JSON_STRING} characters"
    return None


def _bounded(field: str, *values: object) -> dict[str, str] | None:
    problem = _tree_problem(*values)
    return _invalid(field, f"{field}: {problem}") if problem else None


def _name_of(manifest: object) -> str:
    """The object's name: metadata.name, or metadata.generateName when only that is set (the API server creates the name on apply); "" when neither."""
    metadata = manifest.get("metadata") if isinstance(manifest, dict) else None
    if isinstance(metadata, dict):
        for key in ("name", "generateName"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _manifest_problem(items: list[Any], field: str) -> dict[str, str] | None:
    """INVALID_INPUT unless every item is a Kubernetes object with string apiVersion, kind and metadata.name or generateName."""
    for index, item in enumerate(items):
        if not (isinstance(item, dict) and isinstance(item.get("apiVersion"), str) and item["apiVersion"]
                and isinstance(item.get("kind"), str) and item["kind"] and isinstance(item.get("metadata"), dict) and _name_of(item)):
            return _invalid(field, f"{field}[{index}] must be an object with string apiVersion, kind and metadata.name or generateName")
    return None


def _group(api_version: str) -> str:
    return api_version.split("/", 1)[0] if "/" in api_version else ""


def _version(api_version: str) -> str:
    return api_version.split("/", 1)[1] if "/" in api_version else api_version


def _namespace(manifest: dict[str, Any]) -> str:
    return _str(_dig(manifest, "metadata", "namespace"))


def _label(manifest: dict[str, Any]) -> str:
    """Kind/namespace/name of a manifest, namespace omitted when absent."""
    ns = _namespace(manifest)
    return f"{manifest['kind']}/{ns}/{_name_of(manifest)}" if ns else f"{manifest['kind']}/{_name_of(manifest)}"


def _annotations(manifest: dict[str, Any]) -> dict[str, str]:
    raw = _dig(manifest, "metadata", "annotations")
    return {k: v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an ISO-8601 timestamp with an explicit UTC offset") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must carry an explicit UTC offset (for example 2026-01-31T10:00:00+00:00 or ...Z)")
    return parsed


_GO_DURATION_UNITS = {"ns": Decimal("1e-9"), "us": Decimal("1e-6"), "\u00b5s": Decimal("1e-6"), "ms": Decimal("0.001"),
                      "s": Decimal(1), "m": Decimal(60), "h": Decimal(3600)}
_GO_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|\u00b5s|ms|s|m|h)")


def _duration_seconds(text: str) -> Decimal | None:
    """Seconds of a Go duration such as 30s, 5m, 1h30m or 1.5h (what Flux intervals and Rollout pauses use); None if malformed."""
    if not text or len(text) > 64:
        return None
    position, total = 0, Decimal(0)
    while position < len(text):
        match = _GO_DURATION_PART.match(text, position)
        if not match:
            return None
        total += Decimal(match.group(1)) * _GO_DURATION_UNITS[match.group(2)]
        position = match.end()
    return total


def _templated(value: object) -> bool:
    return isinstance(value, str) and "{{" in value


# --- gitops_kustomize_check ------------------------------------------------------------------------------------------

_JSON_PATCH_OPS = frozenset({"add", "copy", "move", "remove", "replace", "test"})
# Fields Kustomize itself warns about (api/types/kustomization.go CheckDeprecatedFields; helmChartInflationGenerator is auto-converted),
# with the field `kustomize edit fix` moves them to (https://kubectl.docs.kubernetes.io/references/kustomize/kustomization/).
_DEPRECATED_KUSTOMIZE_FIELDS = {
    "bases": "resources", "commonLabels": "labels", "helmChartInflationGenerator": "helmCharts", "imageTags": "images",
    "patchesJson6902": "patches", "patchesStrategicMerge": "patches", "vars": "replacements",
}
_REGEX_META = frozenset("[](){}|?\\^$*+")


def _name_tokens(pattern: str) -> list[tuple[str, str]] | None:
    """Tokens of the literal / `.` / `.*` / `.+` subset of a Kustomize target regex; None for any other construct."""
    body = pattern[1:] if pattern.startswith("^") else pattern
    body = body[:-1] if body.endswith("$") else body
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char in _REGEX_META:
            return None
        if char == ".":
            following = body[index + 1] if index + 1 < len(body) else ""
            if following in ("*", "+"):
                tokens.append(("star" if following == "*" else "plus", ""))
                index += 2
                continue
            tokens.append(("any", ""))
        else:
            tokens.append(("lit", char))
        index += 1
    return tokens


def _tokens_match(tokens: list[tuple[str, str]], text: str) -> bool:
    states = {0}
    for kind, char in tokens:
        following: set[int] = set()
        if kind in ("star", "plus"):
            start = min(states) + (1 if kind == "plus" else 0)
            following.update(range(start, len(text) + 1))
        else:
            for state in states:
                if state < len(text) and (kind == "any" or text[state] == char):
                    following.add(state + 1)
        states = following
        if not states:
            return False
    return len(text) in states


def _selector_matches(pattern: object, value: str) -> bool | None:
    """Whole-value match of a Kustomize target field (a regex); None when the pattern is outside the evaluated subset."""
    if pattern in (None, ""):
        return True
    if not isinstance(pattern, str):
        return False
    if len(pattern) > MAX_SELECTOR_PATTERN:
        return None
    tokens = _name_tokens(pattern)
    return None if tokens is None else _tokens_match(tokens, value)


def _target_hit(target: dict[str, Any], literal: bool, manifest: dict[str, Any]) -> bool | None:
    """Whether a patch target selects a manifest: True, False, or None when only an unevaluated regular expression is undecided.
    A target derived from a strategic merge patch must equal the resource id exactly: group, version, kind, name and namespace,
    an absent namespace meaning none (a patch without a namespace does not select a namespaced object, a patch without
    apiVersion selects nothing); an explicit `target` treats every field as a regular expression over the whole value."""
    api = manifest["apiVersion"]
    fields = (("group", _group(api)), ("version", _version(api)), ("kind", manifest["kind"]),
              ("name", manifest["metadata"].get("name", "")), ("namespace", _namespace(manifest)))
    undecided = False
    for key, value in fields:
        wanted = target.get(key)
        if literal:
            if (wanted or "") != value:
                return False
            continue
        outcome = _selector_matches(wanted, value)
        if outcome is None:
            undecided = True
        elif not outcome:
            return False
    return None if undecided else True


def _kustomize_patches(kust: dict[str, Any], findings: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Every patch of a kustomization as {label, target, literal, unresolved}; structural problems become findings."""
    patches: list[dict[str, Any]] = []
    sources = (("patches", kust.get("patches")), ("patchesStrategicMerge", kust.get("patchesStrategicMerge")),
               ("patchesJson6902", kust.get("patchesJson6902")))
    for field, entries in sources:
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            label = f"{field}[{index}]"
            target = entry.get("target") if isinstance(entry, dict) and field != "patchesStrategicMerge" else None
            content = entry.get("patch") if isinstance(entry, dict) and field != "patchesStrategicMerge" else entry
            if field == "patchesJson6902" and isinstance(entry, dict):
                content = entry.get("patch", entry.get("path"))
            if isinstance(content, str) and content.lstrip()[:1] in ("{", "["):
                try:
                    content = json.loads(content)
                except ValueError:
                    pass
            literal, unresolved = False, False
            if isinstance(content, list):
                for op_index, op in enumerate(content):
                    problem = _json_patch_op_problem(op)
                    if problem:
                        findings.append(_finding(f"patch-op-invalid:{label}[{op_index}]", "major", f"{label} operation {op_index}: {problem}"))
                if not isinstance(target, dict):
                    findings.append(_finding(f"patch-target-missing:{label}", "blocker",
                                             f"{label} is a JSON 6902 patch without a target; Kustomize cannot tell what to patch"))
                    continue
            elif isinstance(content, dict) and not isinstance(target, dict):
                name = _dig(content, "metadata", "name")
                if not (isinstance(content.get("kind"), str) and isinstance(name, str)):
                    findings.append(_finding(f"patch-target-missing:{label}", "blocker",
                                             f"{label} has no target and no kind and metadata.name to derive one from"))
                    continue
                api = _str(content.get("apiVersion"))
                target = {"group": _group(api) if api else "", "version": _version(api) if api else "", "kind": content["kind"],
                          "name": name, "namespace": _str(_dig(content, "metadata", "namespace"))}
                literal = True
            elif not isinstance(target, dict):
                unresolved = True
            patches.append({"label": label, "target": target, "literal": literal, "unresolved": unresolved})
    return patches


def _json_patch_op_problem(op: object) -> str | None:
    if not isinstance(op, dict) or not isinstance(op.get("op"), str) or op["op"] not in _JSON_PATCH_OPS:
        return "op must be one of add, copy, move, remove, replace, test"
    if not isinstance(op.get("path"), str) or (op["path"] and not op["path"].startswith("/")):
        return "path must be a JSON pointer starting with /"
    if op["op"] in ("add", "replace", "test") and "value" not in op:
        return f"{op['op']} requires a value"
    if op["op"] in ("copy", "move") and not isinstance(op.get("from"), str):
        return f"{op['op']} requires a from pointer"
    return None


def kustomize_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Overlay integrity: duplicate ids, namespace collisions after the `namespace:` transformer, patch targets that
    match nothing, JSON 6902 operations and deprecated kustomization fields.

    `resources` are the objects the kustomization renders before patches and transformers. Kustomize registers a resource by
    group+version+kind+name+namespace and refuses a second one with the same id (blocker); the same object under two API versions
    builds, but the cluster sees one object, so it is reported as a conflict (major). The `namespace:` transformer sets the
    namespace of every namespaced object and renames every v1 Namespace object to it (two Namespaces then collide)."""

    def work() -> dict[str, Any]:
        kust, resources = payload["kustomization"], payload["resources"]
        extra = set(payload.get("extra_cluster_scoped_kinds", []))
        problem = _bounded("kustomization", kust) or _bounded("resources", resources) or _manifest_problem(resources, "resources")
        if problem:
            return problem
        findings: list[dict[str, str]] = []
        override = kust.get("namespace") if isinstance(kust.get("namespace"), str) else ""
        scoped = CLUSTER_SCOPED_KINDS | extra

        def identity(manifest: dict[str, Any], namespace: str, name: str) -> str:
            group = _group(manifest["apiVersion"])
            return f"{group + '/' if group else ''}{manifest['kind']}/{namespace + '/' if namespace else ''}{name}"

        before_full: Counter[tuple[str, str]] = Counter()  # (version, id) -> objects
        before_plain: Counter[str] = Counter()  # id -> objects
        after_full: Counter[tuple[str, str]] = Counter()
        after_plain: Counter[str] = Counter()
        moved: dict[tuple[str, str], int] = {}
        renamed: list[str] = []
        for manifest in resources:
            cluster = manifest["kind"] in scoped
            name, version = _name_of(manifest), _version(manifest["apiVersion"])
            original = "" if cluster else _namespace(manifest)
            effective = override if override and not cluster else original
            new_name = override if override and manifest["apiVersion"] == "v1" and manifest["kind"] == "Namespace" else name
            if new_name != name:
                renamed.append(name)
            old_id, new_id = identity(manifest, original, name), identity(manifest, effective, new_name)
            before_full[(version, old_id)] += 1
            before_plain[old_id] += 1
            after_full[(version, new_id)] += 1
            after_plain[new_id] += 1
            if override and not cluster and original and original != override:
                moved[(original, override)] = moved.get((original, override), 0) + 1
        exact = {ident for (_, ident), count in before_full.items() if count > 1}
        for ident in sorted(exact):
            findings.append(_finding(f"duplicate-resource:{ident}", "blocker",
                                     f"{ident} is rendered {before_plain[ident]} times; Kustomize refuses to add a resource with an already registered id"))
        for ident, count in sorted(before_plain.items()):
            if count > 1 and ident not in exact:
                findings.append(_finding(f"duplicate-object-versions:{ident}", "major",
                                         f"{ident} is rendered in {count} API versions; Kustomize builds it, but the cluster has one object, so the versions fight over it"))
        exact_after = {ident for (_, ident), count in after_full.items() if count > 1 and before_plain.get(ident, 0) < count}
        for ident in sorted(exact_after):
            findings.append(_finding(f"namespace-collision:{ident}", "blocker", f"namespace: {_redact(override)} folds {after_plain[ident]} distinct resources into {ident}"))
        for ident, count in sorted(after_plain.items()):
            if count > 1 and before_plain.get(ident, 0) < count and ident not in exact_after:
                findings.append(_finding(f"duplicate-object-versions:{ident}", "major",
                                         f"namespace: {_redact(override)} folds {count} API versions of one object into {ident}; the cluster has one object, so the versions fight over it"))
        for (original, target_ns), count in sorted(moved.items()):
            findings.append(_finding(f"namespace-overridden:{original}", "minor",
                                     f"{count} resource(s) declared in namespace {original} are moved to {target_ns} by the kustomization namespace"))
        for name in sorted(set(renamed)):
            findings.append(_finding(f"namespace-renamed:{name}", "info",
                                     f"the kustomization namespace renames Namespace {name} to {_redact(override)}"))

        listed = [posixpath.normpath(entry) for entry in _list(kust.get("resources")) if isinstance(entry, str)]
        for entry in sorted(name for name, count in Counter(listed).items() if count > 1):
            findings.append(_finding(f"duplicate-resource-entry:{entry}", "major",
                                     f"resources lists {entry} more than once; Kustomize fails with an already registered id"))
        for old, new in sorted(_DEPRECATED_KUSTOMIZE_FIELDS.items()):
            if old in kust:
                how = ("`kustomize edit fix` only migrates it experimentally, so convert it by hand and check the build" if old == "vars"
                       else "`kustomize edit fix` migrates it")
                findings.append(_finding(f"deprecated-field:{old}", "minor", f"{old} is deprecated in favour of {new}; {how}"))

        patches = _kustomize_patches(kust, findings)
        if len(patches) * len(resources) > MAX_PATCH_RESOURCE_PAIRS:
            return _invalid("kustomization", f"{len(patches)} patches x {len(resources)} resources is more than {MAX_PATCH_RESOURCE_PAIRS} combinations to check")
        for patch in patches:
            target = patch["target"]
            if patch["unresolved"]:
                findings.append(_finding(f"patch-unresolved:{patch['label']}", "info",
                                         f"{patch['label']} has no inline content or target, so its target cannot be checked"))
                continue
            if not isinstance(target, dict):
                continue
            if target.get("labelSelector") or target.get("annotationSelector"):
                findings.append(_finding(f"patch-selector-unverified:{patch['label']}", "info",
                                         f"{patch['label']} selects by label or annotation; the resources carry no labels here, so it is assumed to match"))
                continue
            outcomes = [_target_hit(target, patch["literal"], manifest) for manifest in resources]
            if True in outcomes:
                continue
            if None in outcomes:
                findings.append(_finding(f"patch-regex-unverified:{patch['label']}", "info",
                                         f"{patch['label']} uses a regular expression beyond literals, . and .*; it is assumed to match"))
            else:
                described = "/".join(str(target.get(k)) for k in ("group", "kind", "namespace", "name") if target.get(k))
                findings.append(_finding(f"patch-target-not-found:{patch['label']}", "major",
                                         f"{patch['label']} targets {described or 'an empty selector'} but no rendered resource matches; "
                                         "Kustomize fails for derived targets and silently skips explicit selectors"))
        return {
            "state": "SUCCESS", "resource_count": len(resources), "patch_count": len(patches),
            "unresolved_patch_count": sum(1 for p in patches if p["unresolved"]), **_report(findings),
        }

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_helm_values_drift ----------------------------------------------------------------------------------------


_EMPTY_KEY = "\\e"  # an empty key is one path segment, spelled as a backslash and e; an escaped backslash never reads as it


def _segment(key: str) -> str:
    """One path segment of a key: an empty key is _EMPTY_KEY; each backslash or dot inside a key gets a backslash in front, so the
    literal key a.b never collides with a nested a -> b, and the empty key never collides with a parent or a sibling."""
    if key == "":
        return _EMPTY_KEY
    return key.replace("\\", "\\\\").replace(".", "\\.")


def _flatten(values: object) -> dict[str, object]:
    """Dotted-path leaves of a values tree; scalars, lists and empty maps are leaves. Keys are escaped by _segment, so every leaf
    has its own path and no leaf is dropped."""
    flat: dict[str, object] = {}
    stack: list[tuple[str | None, object]] = [(None, values)]
    while stack:
        prefix, node = stack.pop()
        if isinstance(node, dict) and node:
            for key, child in node.items():
                segment = _segment(key)
                stack.append((segment if prefix is None else f"{prefix}.{segment}", child))
        elif prefix is not None:
            flat[prefix] = node
    return flat


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _same_value(left: object, right: object) -> bool:
    if _value_type(left) != _value_type(right):
        return False
    if _value_type(left) == "number":
        return _dec(left) == _dec(right)
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _key_problem(key: str) -> bool:
    return not key or key.startswith(".") or key.endswith(".") or ".." in key


def _preview(keys: list[str], limit: int = 8) -> str:
    shown = ", ".join(keys[:limit])
    return shown + (f" (+{len(keys) - limit} more)" if len(keys) > limit else "")


def helm_values_drift(payload: dict[str, Any]) -> dict[str, Any]:
    """Compare Helm values across environments: `required_keys` must exist and be non-empty (null and "" count as empty,
    because Helm treats a null override as an unset key), `invariant_keys` must be identical everywhere, and every
    environment is diffed against `reference` (default: the first) for missing keys, extra keys and type changes.
    A key matches a leaf exactly or as a prefix of leaves (`resources` covers `resources.limits.cpu`); a dot that belongs to one key is written with a backslash in front."""

    def work() -> dict[str, Any]:
        envs, required, invariant = payload["environments"], payload["required_keys"], payload["invariant_keys"]
        problem = _bounded("environments", envs)
        if problem:
            return problem
        names = [env["name"] for env in envs]
        if len(set(names)) != len(names):
            return _invalid("environments", "environment names must be unique")
        for env in envs:
            if not isinstance(env["values"], dict):
                return _invalid("environments", f"environment {env['name']}: values must be an object")
        for field, keys in (("required_keys", required), ("invariant_keys", invariant)):
            if any(_key_problem(key) for key in keys):
                return _invalid(field, f"{field} must be dotted paths without empty segments")
        reference = payload.get("reference", names[0])
        if reference not in names:
            return _invalid("reference", f"reference {reference!r} is not one of the environments")

        flat = {env["name"]: _flatten(env["values"]) for env in envs}
        findings: list[dict[str, str]] = []

        def covered(env: str, key: str) -> list[str]:
            return sorted(k for k in flat[env] if k == key or k.startswith(key + "."))

        for env in names:
            for key in sorted(set(required)):
                leaves = covered(env, key)
                if not leaves:
                    findings.append(_finding(f"required-key-missing:{env}:{key}", "major", f"{env} does not set required key {key}"))
                elif all(flat[env][leaf] is None or flat[env][leaf] == "" for leaf in leaves):
                    findings.append(_finding(f"required-key-empty:{env}:{key}", "major", f"{env} sets required key {key} to null or an empty string"))
        missing = object()
        for key in sorted(set(invariant)):
            leaves = sorted({leaf for env in names for leaf in covered(env, key)})
            for leaf in leaves:
                groups: list[tuple[object, list[str]]] = []  # (value, environments holding it), in order of first appearance
                for env in names:
                    value = flat[env].get(leaf, missing)
                    for entry in groups:
                        other = entry[0]
                        if (value is missing and other is missing) or (value is not missing and other is not missing and _same_value(value, other)):
                            entry[1].append(env)
                            break
                    else:
                        groups.append((value, [env]))
                if len(groups) > 1:
                    # Values are never echoed, whatever the key name: each distinct value is named by its type and group, and a missing one as <unset>.
                    parts = []
                    for position, (value, holders) in enumerate(groups, 1):
                        shown = "<unset>" if value is missing else f"<{_value_type(value)} #{position}>"
                        parts.append(f"{', '.join(holders)}: {shown}")
                    findings.append(_finding(f"invariant-key-differs:{_redact(leaf)}", "major",
                                             f"{_redact(leaf)} must be identical in every environment but differs: {'; '.join(parts)}"))
        diverging = 0
        ref = flat[reference]
        for env in names:
            if env == reference:
                continue
            current = flat[env]
            lacking = sorted(k for k in ref if k not in current)
            extra = sorted(k for k in current if k not in ref)
            if lacking:
                findings.append(_finding(f"key-missing-in-env:{env}", "minor", f"{env} lacks {len(lacking)} key(s) set in {reference}: {_preview(lacking)}"))
            if extra:
                findings.append(_finding(f"key-only-in-env:{env}", "info", f"{env} sets {len(extra)} key(s) absent from {reference}: {_preview(extra)}"))
            retyped = sorted(k for k in ref if k in current and _value_type(ref[k]) != _value_type(current[k]))
            for key in retyped:
                findings.append(_finding(f"type-mismatch:{env}:{key}", "major",
                                         f"{key} is {_value_type(ref[key])} in {reference} but {_value_type(current[key])} in {env}"))
            diverging += sum(1 for k in ref if k in current and _value_type(ref[k]) == _value_type(current[k]) and not _same_value(ref[k], current[k]))
        return {
            "state": "SUCCESS", "environment_count": len(envs), "compared_key_count": len({k for env in names for k in flat[env]}),
            "reference": reference, "diverging_value_count": diverging, **_report(findings),
        }

    result: dict[str, Any] = respond(payload, work)
    return result


# --- shared Argo CD / Flux application checks -------------------------------------------------------------------------

_SHA_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VERSION_TAG = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.+-]+)?$")
_WILDCARD_VERSION = re.compile(r"(?:^|[.])[xX*](?:[.]|$)")
_ARGO_BOOL_SYNC_OPTIONS = frozenset({
    "ApplyOutOfSyncOnly", "ClientSideApplyMigration", "CreateNamespace", "FailOnSharedResource", "Force", "PruneLast", "Replace",
    "RespectIgnoreDifferences", "ServerSideApply", "SkipDryRunOnMissingResource", "Validate",
})
_ARGO_ENUM_SYNC_OPTIONS = {"PrunePropagationPolicy": frozenset({"background", "foreground", "orphan"}),
                           "Prune": frozenset({"true", "false", "confirm"}), "Delete": frozenset({"true", "false", "confirm"})}


def _revision_class(revision: object, chart: bool) -> str:
    """pinned (commit sha, exact chart version), tag, branch, range (chart wildcard or constraint), templated or floating
    (empty or HEAD, which follows the default branch)."""
    if not isinstance(revision, str) or not revision.strip():
        return "floating"
    text = revision.strip()
    if _templated(text):
        return "templated"
    if text == "HEAD":
        return "floating"
    if _SHA_REVISION.match(text):
        return "pinned"
    if chart:
        if any(char in text for char in "*^~<>|") or _WILDCARD_VERSION.search(text):
            return "range"
        return "pinned" if _VERSION_TAG.match(text) else "unknown"
    return "tag" if _VERSION_TAG.match(text) else "branch"


def _revision_findings(add: Callable[[str, str, str], None], label: str, klass: str, prod: bool) -> None:
    if klass in ("floating", "range"):
        add(f"floating-revision:{label}", "major" if prod else "info",
            f"{label} is {klass}: the deployed content changes without a Git change to this application")
    elif klass == "branch" and prod:
        add(f"branch-revision:{label}", "minor", f"{label} tracks a branch; a production rollout follows every merge to it")


def _argo_app_findings(spec: dict[str, Any], metadata: dict[str, Any], prod: bool, prefix: str = "") -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Findings for the spec of an Argo CD Application (or an ApplicationSet template); also {auto_sync, prune, revision_class}."""
    findings: list[dict[str, str]] = []

    def add(rule: str, severity: str, message: str) -> None:
        findings.append(_finding(prefix + rule, severity, message))

    sources_field = spec.get("sources")
    sources = sources_field if isinstance(sources_field, list) else (
        [spec["source"]] if isinstance(spec.get("source"), dict) else [])
    if not sources:
        add("no-source", "blocker", "spec has neither source nor sources")
    first_class = "unknown"
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            add(f"source-invalid:{index}", "blocker", f"source {index} is not an object")
            continue
        if not _str(source.get("repoURL")).strip():
            add(f"no-repo-url:{index}", "blocker", f"source {index} has no repoURL")
        chart, path, ref = bool(source.get("chart")), bool(source.get("path")), bool(source.get("ref"))
        if chart and path:
            add(f"path-and-chart:{index}", "major", f"source {index} sets both path and chart; a source is a Git path or a Helm chart")
        if not (chart or path or ref):
            add(f"no-path-or-chart:{index}", "blocker", f"source {index} has no path, chart or ref")
        klass = _revision_class(source.get("targetRevision"), chart)
        if index == 0:
            first_class = klass
        _revision_findings(add, f"source {index} targetRevision {_echo(source.get('targetRevision'))}", klass, prod)
    destination = spec.get("destination")
    if not isinstance(destination, dict) or not (destination.get("server") or destination.get("name")):
        add("no-destination", "blocker", "destination needs a server or a name")
    elif destination.get("server") and destination.get("name"):
        add("destination-server-and-name", "blocker", "destination sets both server and name; Argo CD rejects the application")
    project = spec.get("project")
    if not project:
        add("no-project", "minor", "spec.project is empty; the application falls into the default project")
    elif project == "default" and prod:
        add("default-project", "minor", "a production application in the default project is not restricted by an AppProject")

    policy = _dict(spec.get("syncPolicy"))
    automated = _dict_or_none(policy.get("automated"))
    if automated is not None and automated.get("enabled") is False:
        automated = None  # `enabled: false` turns automated sync off whatever prune, selfHeal and allowEmpty say (Argo CD automated sync docs)
    prune = bool(automated and automated.get("prune") is True)
    if automated is not None:
        if prod:
            add("prod-auto-sync", "major", "automated sync in production: production sync is L3 and needs a human or a reviewed promotion")
        if prune:
            add("prod-auto-prune" if prod else "auto-prune", "blocker" if prod else "info",
                "automated prune deletes live resources that leave Git" + ("; production prune is L4" if prod else ""))
        if automated.get("allowEmpty") is True and prune:
            add("allow-empty-prune", "blocker" if prod else "major",
                "allowEmpty with prune lets an empty render delete every resource of the application")
        if not isinstance(policy.get("retry"), dict):
            add("no-retry", "minor", "automated sync without a retry policy stops after the first failed attempt")
    options = _list(policy.get("syncOptions"))
    for option in options:
        if not isinstance(option, str) or "=" not in option:
            add(f"sync-option-malformed:{_redact(str(option))}", "minor", f"sync option {_echo(option)} is not Key=value")
            continue
        key, value = option.split("=", 1)
        if key in _ARGO_ENUM_SYNC_OPTIONS:
            if value not in _ARGO_ENUM_SYNC_OPTIONS[key]:
                add(f"sync-option-value:{_redact(option)}", "major", f"{key} must be one of {', '.join(sorted(_ARGO_ENUM_SYNC_OPTIONS[key]))}, not {_echo(value)}")
        elif key in _ARGO_BOOL_SYNC_OPTIONS:
            if value not in ("true", "false"):
                add(f"sync-option-value:{_redact(option)}", "major", f"{key} must be true or false, not {_echo(value)}")
            elif (key, value) in (("Force", "true"), ("Replace", "true")):
                add(f"sync-option:{key}", "major" if prod else "minor",
                    "Force=true deletes and recreates resources that cannot be updated in place (kubectl delete/create, with Replace=true or client-side apply), "
                    "which can cause downtime" if key == "Force"
                    else "Replace=true uses kubectl replace/create instead of apply, overwriting whole objects and possibly destroying them")
            elif (key, value) == ("Validate", "false"):
                add("sync-option:Validate", "minor", "Validate=false skips kubectl schema validation")
        else:
            add(f"sync-option-unknown:{_redact(key)}", "info", f"sync option {_redact(key)} is not one of the documented options")
    ignores = _list(spec.get("ignoreDifferences"))
    for index, entry in enumerate(ignores):
        pointers = _list(entry.get("jsonPointers")) if isinstance(entry, dict) else []
        queries = _list(entry.get("jqPathExpressions")) if isinstance(entry, dict) else []
        if any(p in ("", "/") for p in pointers) or any(q.strip() == "." for q in queries if isinstance(q, str)):
            add(f"ignore-everything:{index}", "minor", f"ignoreDifferences[{index}] ignores the whole object, so drift can never be seen")
    finalizers = _list(metadata.get("finalizers"))
    if any(isinstance(f, str) and f.startswith(RESOURCES_FINALIZER) for f in finalizers):
        add("cascade-delete", "info", "the resources finalizer makes deleting the application delete its workloads")
    return findings, {"auto_sync": automated is not None, "prune": prune, "revision_class": first_class}


# Flux promoted Kustomization to kustomize.toolkit.fluxcd.io/v1 in Flux 2.0 and HelmRelease to helm.toolkit.fluxcd.io/v2 in Flux 2.3; the older
# versions are deprecated (https://fluxcd.io/flux/components/kustomize/kustomizations/ and https://fluxcd.io/flux/components/helm/helmreleases/).
_FLUX_DEPRECATED_API = {
    ("kustomize.toolkit.fluxcd.io", "v1beta1"), ("kustomize.toolkit.fluxcd.io", "v1beta2"),
    ("helm.toolkit.fluxcd.io", "v2beta1"), ("helm.toolkit.fluxcd.io", "v2beta2"),
}
MIN_FLUX_INTERVAL_SECONDS = 60  # policy of this check (sub-minute polling hammers the source), not an API limit


def _flux_findings(manifest: dict[str, Any], prod: bool) -> tuple[list[dict[str, str]], dict[str, Any]]:
    findings: list[dict[str, str]] = []

    def add(rule: str, severity: str, message: str) -> None:
        findings.append(_finding(rule, severity, message))

    kind, spec = manifest["kind"], _dict(manifest.get("spec"))
    group, version = _group(manifest["apiVersion"]), _version(manifest["apiVersion"])
    if (group, version) in _FLUX_DEPRECATED_API:
        add("deprecated-api-version", "minor", f"{manifest['apiVersion']} is deprecated; Flux 2 serves {group}/{'v1' if kind == 'Kustomization' else 'v2'}")
    interval = spec.get("interval")
    seconds = _duration_seconds(interval) if isinstance(interval, str) else None
    if interval is None:
        add("no-interval", "blocker", "spec.interval is required")
    elif seconds is None:
        add("invalid-interval", "blocker", f"spec.interval {_echo(interval)} is not a duration such as 10m or 1h30m")
    elif seconds < MIN_FLUX_INTERVAL_SECONDS:
        add("interval-under-1m", "minor", f"spec.interval {interval} is shorter than {MIN_FLUX_INTERVAL_SECONDS}s")
    suspended = spec.get("suspend") is True
    if suspended:
        add("suspended", "minor", "spec.suspend is true: nothing is reconciled and drift accumulates")
    prune, klass = False, "unknown"
    if kind == "Kustomization":
        source_ref = spec.get("sourceRef")
        if not isinstance(source_ref, dict) or not source_ref.get("name"):
            add("no-source-ref", "blocker", "spec.sourceRef needs a kind and a name")
        elif source_ref.get("kind") not in ("GitRepository", "OCIRepository", "Bucket", "ExternalArtifact"):
            add("source-ref-kind", "blocker", f"sourceRef.kind {_echo(source_ref.get('kind'))} must be GitRepository, OCIRepository, Bucket or ExternalArtifact")
        if not isinstance(spec.get("prune"), bool):
            add("prune-unspecified", "blocker", "spec.prune is required and must be true or false")
        prune = spec.get("prune") is True
        if prune:
            add("prod-prune" if prod else "prune-enabled", "blocker" if prod else "info",
                "prune deletes cluster objects that leave Git" + ("; production prune is L4" if prod else ""))
        if spec.get("force") is True:
            add("force", "major" if prod else "minor", "force recreates objects whose immutable fields change, which can cause downtime")
        if prod and not spec.get("serviceAccountName"):
            add("no-impersonation", "minor", "no serviceAccountName: the controller applies this Kustomization with cluster-admin rights")
        decryption = spec.get("decryption")
        if isinstance(decryption, dict) and decryption.get("provider") != "sops":
            add("decryption-provider", "blocker", f"decryption.provider {_echo(decryption.get('provider'))} must be sops")
    else:
        chart_spec = _dig(spec, "chart", "spec")
        chart_ref = spec.get("chartRef")
        if not isinstance(chart_spec, dict) and not isinstance(chart_ref, dict):
            add("no-chart", "blocker", "spec needs chart.spec or chartRef")
        elif isinstance(chart_spec, dict) and isinstance(chart_ref, dict):
            add("chart-and-chartref", "blocker", "spec sets both chart and chartRef; Flux requires exactly one of them")
            klass = _revision_class(chart_spec.get("version"), True)
        elif isinstance(chart_spec, dict):
            klass = _revision_class(chart_spec.get("version"), True)
            _revision_findings(add, f"chart version {_echo(chart_spec.get('version'))}", klass, prod)
        if not any(isinstance(_dig(spec, section, "remediation", "retries"), int) for section in ("install", "upgrade")):
            add("no-remediation", "minor", "no install/upgrade remediation retries: a failed release stays failed until someone intervenes")
        if _dig(spec, "upgrade", "force") is True:
            add("upgrade-force", "major" if prod else "minor", "upgrade.force recreates resources and can cause downtime")
        if _dig(spec, "driftDetection", "mode") != "enabled":
            add("drift-detection-off", "info", "driftDetection.mode is not enabled: manual changes to the release are not reverted")
    return findings, {"auto_sync": not suspended, "prune": prune, "revision_class": klass}


def app_sync_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Sync-policy lint of one Argo CD Application, Flux Kustomization or Flux HelmRelease: production auto-sync and
    prune (L3 / L4 for this agent), revision pinning, destination, sync options, retry, intervals and deprecated Flux API versions."""

    def work() -> dict[str, Any]:
        resource, prod = payload["resource"], payload["environment"] == "prod"
        if not isinstance(resource, dict):
            return _invalid("resource", "resource must be an object")
        problem = _bounded("resource", resource) or _manifest_problem([resource], "resource")
        if problem:
            return problem
        group, kind = _group(resource["apiVersion"]), resource["kind"]
        if (resource["apiVersion"], kind) == (ARGO_API, "Application"):
            spec = resource.get("spec")
            if not isinstance(spec, dict):
                return _invalid("resource", "resource.spec must be an object")
            findings, info = _argo_app_findings(spec, resource["metadata"], prod)
            engine = "argocd"
        elif (group, kind) in (("kustomize.toolkit.fluxcd.io", "Kustomization"), ("helm.toolkit.fluxcd.io", "HelmRelease")):
            findings, info = _flux_findings(resource, prod)
            engine = "flux"
        else:
            return _invalid("resource", "resource must be an Argo CD Application (argoproj.io/v1alpha1), a Flux Kustomization or a Flux HelmRelease")
        return {"state": "SUCCESS", "engine": engine, "kind": kind, "auto_sync": info["auto_sync"], "prune_enabled": info["prune"],
                "revision_class": info["revision_class"], **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_appset_check ---------------------------------------------------------------------------------------------

_GENERATOR_TYPES = ("list", "clusters", "git", "scmProvider", "clusterDecisionResource", "pullRequest", "matrix", "merge", "plugin")


def _go_actions(text: str) -> Iterator[str]:
    """The text of every {{ ... }} action in a Go template string (linear; a regex with a lazy `.*?` rescans on every unclosed `{{`)."""
    position = 0
    while True:
        start = text.find("{{", position)
        if start < 0:
            return
        end = text.find("}}", start + 2)
        if end < 0:
            return
        yield text[start + 2:end]
        position = end + 2


_GO_FIELD = re.compile(r"(?<![\w.$])\.([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")
_FAST_FIELD = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
_PATH_INDEX = re.compile(r"^path\[\d+\]$")
_MAX_GENERATOR_DEPTH = 4
_MAX_ELEMENT_DEPTH = 6


def _nested_keys(element: dict[str, Any], prefix: str = "", depth: int = 1) -> set[str]:
    """Parameter names a list element offers: its keys and, for a nested object, the dotted keys inside it (`{{ .cluster.name }}`)."""
    keys: set[str] = set()
    for key, value in element.items():
        name = f"{prefix}.{key}" if prefix else key
        keys.add(name)
        if isinstance(value, dict) and depth < _MAX_ELEMENT_DEPTH:
            keys |= _nested_keys(value, name, depth + 1)
    return keys


def _selector_hit(selector: dict[str, Any], labels: dict[str, Any]) -> bool:
    """Kubernetes label-selector semantics: matchLabels all equal, every matchExpression satisfied."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if operator == "In":
            ok = key in labels and labels[key] in values
        elif operator == "NotIn":
            ok = key not in labels or labels[key] not in values
        elif operator == "Exists":
            ok = key in labels
        elif operator == "DoesNotExist":
            ok = key not in labels
        else:
            raise ValueError(f"unknown matchExpressions operator {_echo(operator)}; use In, NotIn, Exists or DoesNotExist")
        if not ok:
            return False
    return True


def _appset_generator(gen: object, ctx: dict[str, Any], path: str, depth: int) -> tuple[set[str], bool, int | None]:
    """(parameter keys, keys-are-open, upper bound on elements or None) for one generator; problems go to ctx findings."""
    findings, go = ctx["findings"], ctx["go"]
    ctx["count"] += 1
    if not isinstance(gen, dict):
        findings.append(_finding(f"generator-invalid:{path}", "blocker", f"{path} is not an object"))
        return set(), True, None
    kinds = [k for k in _GENERATOR_TYPES if k in gen]
    if len(kinds) != 1 or not isinstance(gen[kinds[0]], dict):
        findings.append(_finding(f"generator-type:{path}", "blocker",
                                 f"{path} must set exactly one generator type (one of {', '.join(_GENERATOR_TYPES)}), found {kinds or 'none'}"))
        return set(), True, None
    kind, body = kinds[0], gen[kinds[0]]
    if depth > _MAX_GENERATOR_DEPTH:
        findings.append(_finding(f"generators-too-deep:{path}", "blocker", f"{path} nests generators deeper than {_MAX_GENERATOR_DEPTH}"))
        return set(), True, None
    if kind == "list":
        elements = body.get("elements")
        if isinstance(body.get("elementsYaml"), str):
            return set(), True, None
        if not isinstance(elements, list) or not elements:
            findings.append(_finding(f"list-empty:{path}", "major", f"{path} lists no elements, so it generates no Applications"))
            return set(), False, 0
        sets = [set(e) for e in elements if isinstance(e, dict)]
        top = set().union(*sets) if sets else set()
        keys = set().union(*(_nested_keys(e) for e in elements if isinstance(e, dict))) if sets else set()
        if any(s != top for s in sets):
            findings.append(_finding(f"list-elements-inconsistent:{path}", "minor", f"{path} elements do not all define the same keys"))
        return keys, False, len(elements)
    if kind == "clusters":
        ctx["cluster_generators"] += 1
        selector = body.get("selector")
        if selector is not None and not isinstance(selector, dict):
            findings.append(_finding(f"selector-invalid:{path}", "blocker", f"{path}.selector is not an object"))
            selector = {}
        selector = selector or {}
        match_all = not (selector.get("matchLabels") or selector.get("matchExpressions"))
        keys = {"name", "nameNormalized", "server"}
        if isinstance(body.get("values"), dict):
            keys |= {f"values.{k}" for k in body["values"]}
        if not ctx["clusters"]:
            ctx["cluster_unknown"] = True
            if match_all:
                findings.append(_finding(f"all-clusters-selected:{path}", "major",
                                         f"{path} has no selector: it selects every cluster registered in Argo CD (the cluster list was not supplied)"))
            return keys, True, None
        try:
            matched = [c for c in ctx["clusters"] if match_all or _selector_hit(selector, c["labels"])]
        except (TypeError, AttributeError, ValueError) as exc:
            findings.append(_finding(f"selector-invalid:{path}", "blocker", f"{path}.selector: {exc}"))
            return keys, True, None
        ctx["matched"].update(c["name"] for c in matched)
        ctx["matched_labels"].extend(c["labels"] for c in matched)
        for cluster in matched:
            keys |= {f"metadata.labels.{k}" for k in cluster["labels"]}
        if match_all:
            findings.append(_finding(f"all-clusters-selected:{path}", "major",
                                     f"{path} has no selector: it selects every cluster registered in Argo CD ({len(matched)})"))
        elif not matched:
            findings.append(_finding(f"selector-matches-no-cluster:{path}", "major",
                                     f"{path} matches no registered cluster; if it matched before, the generated Applications (and with the "
                                     "resources finalizer their workloads) are deleted"))
        return keys, False, len(matched)
    if kind == "git":
        if not _str(body.get("repoURL")).strip():
            findings.append(_finding(f"git-no-repo:{path}", "blocker", f"{path} has no repoURL"))
        directories, files = body.get("directories"), body.get("files")
        if not (isinstance(directories, list) and directories) and not (isinstance(files, list) and files):
            findings.append(_finding(f"git-no-paths:{path}", "blocker", f"{path} needs directories or files"))
        if go:
            keys = {"path.path", "path.basename", "path.basenameNormalized", "path.segments"}
            if isinstance(files, list) and files:
                keys |= {"path.filename", "path.filenameNormalized"}
        else:
            keys = {"path", "path.basename", "path.basenameNormalized"}
            if isinstance(files, list) and files:
                keys |= {"path.filename", "path.filenameNormalized"}
        return keys, bool(isinstance(files, list) and files), None
    if kind in ("matrix", "merge"):
        children = body.get("generators")
        if not isinstance(children, list):
            findings.append(_finding(f"generators-missing:{path}", "blocker", f"{path}.generators must be a list"))
            return set(), True, None
        if kind == "matrix" and len(children) != 2:
            findings.append(_finding(f"matrix-arity:{path}", "blocker", f"{path} needs exactly 2 child generators, has {len(children)}"))
        results = [_appset_generator(child, ctx, f"{path}.{kind}.generators[{i}]", depth + 1) for i, child in enumerate(children)]
        keys = set().union(*(r[0] for r in results)) if results else set()
        open_ = any(r[1] for r in results)
        if kind == "matrix":
            bounds = [r[2] for r in results]
            bound = math.prod(b for b in bounds if b is not None) if bounds and all(b is not None for b in bounds) else None
            return keys, open_, bound
        merge_keys = body.get("mergeKeys")
        if not isinstance(merge_keys, list) or not merge_keys or not all(isinstance(k, str) for k in merge_keys):
            findings.append(_finding(f"merge-keys:{path}", "blocker", f"{path}.mergeKeys must list at least one key"))
        elif results and not results[0][1]:
            for key in merge_keys:
                if key not in results[0][0]:
                    findings.append(_finding(f"merge-key-missing:{path}:{key}", "major", f"mergeKey {key} is not produced by the base generator"))
        if len(children) < 2:
            findings.append(_finding(f"merge-single:{path}", "minor", f"{path} merges fewer than 2 generators"))
        return keys, open_, results[0][2] if results else 0
    return set(), True, None  # scmProvider, pullRequest, clusterDecisionResource, plugin: parameters come from the remote system


def _template_refs(template: object, go: bool) -> list[str]:
    refs: set[str] = set()
    stack = [template]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and "{{" in node:
            if go:
                for action in _go_actions(node):
                    refs.update(_GO_FIELD.findall(action))
            else:
                refs.update(_FAST_FIELD.findall(node))
    return sorted(refs)


def _known_ref(ref: str, keys: set[str], prefixes: set[str], open_: bool, go: bool) -> bool:
    if open_ or ref in keys or ref in prefixes:
        return True
    if ref in ("metadata", "metadata.labels", "metadata.annotations", "values") or ref.startswith("metadata.annotations."):
        return True
    return not go and bool(_PATH_INDEX.match(ref))


def appset_check(payload: dict[str, Any]) -> dict[str, Any]:
    """ApplicationSet lint: generator shape (exactly one type; matrix has 2 children; merge keys), cluster selection
    against the registered clusters (empty selector = every cluster, no match = Applications deleted), template keys that
    the generators actually produce (fasttemplate `{{key}}` or Go template `{{ .key }}`), a unique templated Application
    name and destination per element, RollingSync steps, plus every Application-spec rule of gitops_app_sync_policy."""

    def work() -> dict[str, Any]:
        appset, clusters, prod = payload["applicationset"], payload["clusters"], payload["environment"] == "prod"
        problem = _bounded("applicationset", appset) or _bounded("clusters", clusters)
        if problem:
            return problem
        if appset.get("apiVersion") != ARGO_API or appset.get("kind") != "ApplicationSet" or not isinstance(appset.get("spec"), dict):
            return _invalid("applicationset", "applicationset must be an argoproj.io/v1alpha1 ApplicationSet with a spec")
        names = [c["name"] for c in clusters]
        if len(set(names)) != len(names):
            return _invalid("clusters", "cluster names must be unique")
        for cluster in clusters:
            labels = cluster.get("labels", {})
            if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
                return _invalid("clusters", f"cluster {cluster['name']}: labels must be an object of strings")
        spec = appset["spec"]
        go = spec.get("goTemplate") is True
        findings: list[dict[str, str]] = []
        ctx: dict[str, Any] = {"findings": findings, "go": go, "clusters": [{"name": c["name"], "labels": c.get("labels", {})} for c in clusters],
               "matched": set(), "matched_labels": [], "count": 0, "cluster_unknown": False, "cluster_generators": 0}
        generators = spec.get("generators")
        if not isinstance(generators, list) or not generators:
            findings.append(_finding("no-generators", "blocker", "spec.generators must list at least one generator"))
            generators = []
        keys: set[str] = set()
        open_: bool = False
        bound: int | None = 0
        for index, gen in enumerate(generators):
            gen_keys, gen_open, gen_bound = _appset_generator(gen, ctx, f"generators[{index}]", 1)
            keys |= gen_keys
            open_ = open_ or gen_open
            bound = None if bound is None or gen_bound is None else bound + gen_bound
        many = bound is None or bound > 1
        matched_count = -1 if not ctx["cluster_generators"] or ctx["cluster_unknown"] else len(ctx["matched"])
        any_cluster_generator = ctx["cluster_generators"] > 0
        if ctx["cluster_unknown"]:
            findings.append(_finding("clusters-not-supplied", "info", "no registered clusters were supplied, so cluster selection and label keys are not verified"))
        if go and "missingkey=error" not in _list(spec.get("goTemplateOptions")):
            findings.append(_finding("go-template-missingkey", "minor",
                                     "goTemplate without goTemplateOptions [missingkey=error] renders a missing key as <no value> instead of failing"))

        template = spec.get("template")
        if not isinstance(template, dict):
            findings.append(_finding("no-template", "blocker", "spec.template is required"))
        else:
            metadata = _dict(template.get("metadata"))
            app_spec = _dict(template.get("spec"))
            name = metadata.get("name")
            if not isinstance(name, str) or not name:
                findings.append(_finding("template-no-name", "blocker", "template.metadata.name is required"))
            elif "{{" not in name and many:
                findings.append(_finding("template-name-static", "blocker",
                                         f"template name {name!r} has no template key, so every generated element produces the same Application"))
            destination = _dict(app_spec.get("destination"))
            if any_cluster_generator and matched_count > 1 and destination and not any(_templated(destination.get(k)) for k in ("server", "name")):
                findings.append(_finding("destination-static", "major",
                                         f"{matched_count} clusters are selected but the destination is not templated: every Application targets the same cluster"))
            prefixes = {".".join(parts[:i]) for parts in (k.split(".") for k in keys) for i in range(1, len(parts))}
            for ref in _template_refs(template, go):
                if not _known_ref(ref, keys, prefixes, open_, go):
                    findings.append(_finding(f"unknown-template-key:{ref}", "major",
                                             f"template references {ref}, which no generator produces"))
                elif ref.startswith("metadata.labels.") and ctx["matched_labels"]:
                    label = ref[len("metadata.labels."):]
                    lacking = sum(1 for labels in ctx["matched_labels"] if label not in labels)
                    if lacking:
                        findings.append(_finding(f"label-missing-on-cluster:{label}", "major",
                                                 f"{lacking} selected cluster(s) lack label {label} used by the template"))
            strategy = spec.get("strategy")
            kind = strategy.get("type") if isinstance(strategy, dict) else None
            sync_policy = _dict(app_spec.get("syncPolicy"))
            if kind == "RollingSync" and isinstance(sync_policy.get("automated"), dict):
                findings.append(_finding("template-auto-sync-ignored-by-rolling-sync", "minor",
                                         "RollingSync forces automated sync off on every generated Application (the controller logs a warning), so the template's "
                                         "syncPolicy.automated has no effect"))
                app_spec = {**app_spec, "syncPolicy": {k: v for k, v in sync_policy.items() if k != "automated"}}
            app_findings, _ = _argo_app_findings(app_spec, metadata, prod, "template-")
            findings.extend(app_findings)
            labels_in_template = _dict(metadata.get("labels"))
            if kind not in (None, "AllAtOnce", "RollingSync"):
                findings.append(_finding("strategy-type", "blocker", f"strategy.type {_echo(kind)} must be AllAtOnce or RollingSync"))
            if kind == "RollingSync":
                steps = _dig(strategy, "rollingSync", "steps")
                if not isinstance(steps, list) or not steps:
                    findings.append(_finding("rolling-sync-empty", "blocker", "RollingSync needs rollingSync.steps"))
                else:
                    for index, step in enumerate(steps):
                        for expression in _list(step.get("matchExpressions")) if isinstance(step, dict) else []:
                            key = expression.get("key") if isinstance(expression, dict) else None
                            if isinstance(key, str) and not _templated(key) and key not in labels_in_template:
                                findings.append(_finding(f"rolling-sync-label-missing:{index}:{key}", "major",
                                                         f"rollingSync step {index} matches label {key}, which the template does not set; Applications cannot be grouped into steps"))
            elif prod and many and (matched_count != 1):
                findings.append(_finding("no-progressive-sync", "minor",
                                         "a production ApplicationSet that can generate several Applications syncs them all at once; consider strategy RollingSync"))
            sync = _dict(spec.get("syncPolicy"))
            if sync.get("applicationsSync") not in (None, "create-only", "create-update", "create-delete", "sync"):
                findings.append(_finding("applications-sync", "major", f"syncPolicy.applicationsSync {_echo(sync.get('applicationsSync'))} is not a valid policy"))
            cascades = any(isinstance(f, str) and f.startswith(RESOURCES_FINALIZER) for f in _list(metadata.get("finalizers")))
            if prod and cascades and sync.get("preserveResourcesOnDeletion") is not True:
                findings.append(_finding("appset-cascade-delete", "minor",
                                         "deleting this ApplicationSet (or an element leaving a generator) deletes the Applications and, via the finalizer, their workloads; "
                                         "preserveResourcesOnDeletion is not set"))
        return {"state": "SUCCESS", "generator_count": ctx["count"], "matched_cluster_count": matched_count, **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_sync_wave_check ------------------------------------------------------------------------------------------

_PHASE_ORDER = {"PreSync": 0, "Sync": 1, "PostSync": 2, "SyncFail": 3, "PreDelete": 4, "PostDelete": 5}  # the delete phases run on Application deletion, not in a sync
_DELETE_PHASES = frozenset({"PreDelete", "PostDelete"})
_HOOK_TYPES = frozenset(_PHASE_ORDER) | {"Skip"}
_HOOK_DELETE_POLICIES = frozenset({"HookSucceeded", "HookFailed", "BeforeHookCreation"})
_WAVE_VALUE = re.compile(r"^[+-]?[0-9]+$")
_INT64_DIGITS = 19  # an int64 has at most 19 significant digits: longer runs are refused before int() can convert them


def _wave_number(raw: object) -> int | None:
    """The int64 that a sync-wave annotation written as a string holds, None when it holds none. Leading zeros are not significant
    (Argo CD parses the value as an integer), so only the digits after them are converted, and only when there are at most 19."""
    if not isinstance(raw, str) or not _WAVE_VALUE.fullmatch(raw):
        return None
    digits = raw.lstrip("+-").lstrip("0") or "0"
    if len(digits) > _INT64_DIGITS:
        return None
    value = -int(digits) if raw.startswith("-") else int(digits)
    return value if -(1 << 63) <= value < (1 << 63) else None


_POD_OWNERS = frozenset(WORKLOAD_KINDS | {"CronJob", "Pod", "Rollout"})


def _pod_spec(manifest: dict[str, Any]) -> dict[str, Any] | None:
    kind, spec = manifest["kind"], manifest.get("spec")
    if kind not in _POD_OWNERS or not isinstance(spec, dict):
        return None
    pod = spec if kind == "Pod" else _dig(spec, "jobTemplate", "spec", "template", "spec") if kind == "CronJob" else _dig(spec, "template", "spec")
    return pod if isinstance(pod, dict) else None


def _pod_refs(pod: dict[str, Any]) -> set[tuple[str, str]]:
    """(kind, name) of the ConfigMaps, Secrets, ServiceAccount and PVCs a pod spec needs to start (optional references excluded)."""
    refs: set[tuple[str, str]] = set()

    def add(kind: str, holder: object, name_key: str = "name") -> None:
        if isinstance(holder, dict) and isinstance(holder.get(name_key), str) and holder[name_key] and holder.get("optional") is not True:
            refs.add((kind, holder[name_key]))

    for volume in _list(pod.get("volumes")):
        if not isinstance(volume, dict):
            continue
        add("ConfigMap", volume.get("configMap"))
        add("Secret", volume.get("secret"), "secretName")
        add("PersistentVolumeClaim", volume.get("persistentVolumeClaim"), "claimName")
        for source in _list(_dig(volume, "projected", "sources")):
            if isinstance(source, dict):
                add("ConfigMap", source.get("configMap"))
                add("Secret", source.get("secret"))
    for section in ("containers", "initContainers"):
        for container in _list(pod.get(section)):
            if not isinstance(container, dict):
                continue
            for env in _list(container.get("env")):
                source = env.get("valueFrom") if isinstance(env, dict) else None
                if isinstance(source, dict):
                    add("ConfigMap", source.get("configMapKeyRef"))
                    add("Secret", source.get("secretKeyRef"))
            for env_from in _list(container.get("envFrom")):
                if isinstance(env_from, dict):
                    add("ConfigMap", env_from.get("configMapRef"))
                    add("Secret", env_from.get("secretRef"))
    for secret in _list(pod.get("imagePullSecrets")):
        add("Secret", secret)
    account = pod.get("serviceAccountName") or pod.get("serviceAccount")
    if isinstance(account, str) and account:
        refs.add(("ServiceAccount", account))
    return refs


def sync_wave_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Argo CD sync ordering (sync-waves guide): resources run by phase (PreSync, Sync, PostSync), then ascending
    `argocd.argoproj.io/sync-wave` (default 0, negative allowed), then Namespaces, other built-in kinds and custom
    resources, then name. Flags unparsable waves (Argo CD silently uses 0), invalid hook types and delete policies,
    Namespaces after their contents, custom resources before or in the wave of their CRD, and workloads that run before
    the ConfigMap, Secret, ServiceAccount or PVC they need (the wave never turns healthy, so the sync stalls)."""

    def work() -> dict[str, Any]:
        manifests = payload["manifests"]
        problem = _bounded("manifests", manifests) or _manifest_problem(manifests, "manifests")
        if problem:
            return problem
        findings: list[dict[str, str]] = []
        entries: list[dict[str, Any]] = []
        skipped_keys: set[tuple[str, str, str]] = set()  # objects with hook Skip: never applied, so nothing can depend on them
        skipped_namespaces: set[str] = set()
        for manifest in manifests:
            label, notes = _label(manifest), _annotations(manifest)
            wave = 0
            raw_notes = _dig(manifest, "metadata", "annotations")
            raw = raw_notes.get(WAVE_ANNOTATION) if isinstance(raw_notes, dict) else None
            if raw is not None:
                number = _wave_number(raw)
                if number is not None:
                    wave = number
                else:
                    findings.append(_finding(f"wave-invalid:{label}", "major",
                                             f"{label} has sync-wave {_echo(raw)}, which is not an int64 written as a string; Argo CD treats it as 0 "
                                             "(and the Kubernetes API refuses a non-string annotation value)"))
            phases = ["Sync"]
            if HOOK_ANNOTATION in notes:
                tokens = [t.strip() for t in notes[HOOK_ANNOTATION].split(",") if t.strip()]
                unknown = [t for t in tokens if t not in _HOOK_TYPES]
                for token in unknown:
                    findings.append(_finding(f"hook-unknown-type:{label}:{_redact(token)}", "major",
                                             f"{label} has hook type {_echo(token)}; use PreSync, Sync, PostSync, SyncFail, PreDelete, PostDelete or Skip"))
                if "Skip" in tokens:
                    skipped_keys.add((manifest["kind"], _namespace(manifest), _name_of(manifest)))
                    if manifest["kind"] == "Namespace":
                        skipped_namespaces.add(_name_of(manifest))
                    continue
                valid = sorted({t for t in tokens if t in _PHASE_ORDER}, key=_PHASE_ORDER.__getitem__)
                phases = valid or ["Sync"]
            for policy in [p.strip() for p in notes.get(HOOK_DELETE_ANNOTATION, "").split(",") if p.strip()]:
                if policy not in _HOOK_DELETE_POLICIES:
                    findings.append(_finding(f"hook-delete-policy:{label}:{_redact(policy)}", "major",
                                             f"{label} has hook-delete-policy {_echo(policy)}; use HookSucceeded, HookFailed or BeforeHookCreation"))
            entries.append({"manifest": manifest, "label": label, "wave": wave, "phases": phases,
                            "position": (_PHASE_ORDER[phases[0]], wave)})

        namespaces = {_name_of(e["manifest"]): e for e in entries if e["manifest"]["kind"] == "Namespace"}
        crds: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in entries:
            spec = entry["manifest"].get("spec")
            if entry["manifest"]["kind"] == "CustomResourceDefinition" and isinstance(spec, dict):
                group, kind = spec.get("group"), _dig(spec, "names", "kind")
                if isinstance(group, str) and isinstance(kind, str):
                    crds[(group, kind)] = entry
        dependencies = {(e["manifest"]["kind"], _namespace(e["manifest"]), _name_of(e["manifest"])): e
                        for e in entries if e["manifest"]["kind"] in ("ConfigMap", "Secret", "ServiceAccount", "PersistentVolumeClaim")}
        for entry in entries:
            if entry["phases"][0] in _DELETE_PHASES:
                continue  # runs when the Application is deleted: no ordering against the sync
            manifest, label, position = entry["manifest"], entry["label"], entry["position"]
            ns = _namespace(manifest)
            if manifest["kind"] != "Namespace" and manifest["kind"] not in CLUSTER_SCOPED_KINDS and ns in namespaces:
                if position < namespaces[ns]["position"]:
                    findings.append(_finding(f"namespace-after-use:{label}", "blocker",
                                             f"{label} syncs (phase/wave {position}) before Namespace {ns} (phase/wave {namespaces[ns]['position']})"))
            if manifest["kind"] != "Namespace" and manifest["kind"] not in CLUSTER_SCOPED_KINDS and ns in skipped_namespaces and ns not in namespaces:
                findings.append(_finding(f"namespace-skipped:{label}", "major",
                                         f"{label} lives in Namespace {ns}, which carries hook Skip and is never applied by this sync; it must already exist"))
            crd = crds.get((_group(manifest["apiVersion"]), manifest["kind"]))
            if crd is not None and manifest["kind"] != "CustomResourceDefinition":
                if position < crd["position"]:
                    findings.append(_finding(f"cr-before-crd:{label}", "blocker",
                                             f"{label} syncs before the CustomResourceDefinition {crd['label']} that defines its kind"))
                elif position == crd["position"]:
                    findings.append(_finding(f"cr-with-crd-same-wave:{label}", "major",
                                             f"{label} syncs in the same wave as its CRD {crd['label']}; the API may not be established yet "
                                             "(put the CRD in an earlier wave or set SkipDryRunOnMissingResource=true)"))
            pod = _pod_spec(manifest)
            for kind, name in sorted(_pod_refs(pod)) if pod else []:
                target = dependencies.get((kind, ns, name))
                if target is not None and position < target["position"]:
                    findings.append(_finding(f"dependency-after-workload:{label}:{kind}/{name}", "blocker",
                                             f"{label} syncs (phase/wave {position}) before the {kind} {name} it needs (phase/wave {target['position']}); "
                                             "it cannot become healthy before that object exists, so the sync stalls or fails"))
                elif target is None and (kind, ns, name) in skipped_keys:
                    findings.append(_finding(f"dependency-skipped:{label}:{kind}/{name}", "major",
                                             f"{label} needs the {kind} {name}, which carries hook Skip and is never applied by this sync; it must already exist in the cluster"))
        groups: dict[tuple[int, str, int], list[str]] = {}
        for entry in entries:
            for phase in entry["phases"]:
                groups.setdefault((_PHASE_ORDER[phase], phase, entry["wave"]), []).append(entry["manifest"]["kind"])
        plan = [{"phase": phase, "wave": wave, "resource_count": len(kinds), "kinds": sorted(set(kinds))}
                for (_, phase, wave), kinds in sorted(groups.items())]
        return {"state": "SUCCESS", "resource_count": len(entries), "wave_count": len({(k[1], k[2]) for k in groups}),
                "plan": plan, **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_prune_risk -----------------------------------------------------------------------------------------------

_GUARDRAIL_KINDS = frozenset({
    "ClusterRole", "ClusterRoleBinding", "MutatingWebhookConfiguration", "NetworkPolicy", "Role", "RoleBinding",
    "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding", "ValidatingWebhookConfiguration",
})
_CROSSPLANE_CONFIG_GROUPS = frozenset({"apiextensions.crossplane.io", "pkg.crossplane.io"})


def prune_risk(payload: dict[str, Any]) -> dict[str, Any]:
    """What a prune would delete. `pruned` are the live objects that are no longer in Git. Namespaces and CRDs cascade
    to everything inside them (blocker); PVCs lose data (blocker in production), StatefulSets whose
    persistentVolumeClaimRetentionPolicy.whenDeleted is Delete take their PVCs along; Secrets, access-control objects,
    Crossplane managed resources (upbound and crossplane groups) and any non-config object with deletionPolicy Delete in any API group
    (claims, composites, other external-resource kinds) and Argo CD Applications or ApplicationSets (their finalizer cascades to
    the workloads) are flagged. `argocd.argoproj.io/sync-options: Prune=false` or the Flux annotation or label
    `kustomize.toolkit.fluxcd.io/prune: disabled` protects an object and Prune=confirm makes the sync wait for a human.
    Autonomy: non-production L2, production sync L3, production prune L4 (any unprotected object). An empty desired state with
    allowEmpty wipes the application."""

    def work() -> dict[str, Any]:
        pruned, prod, automated = payload["pruned"], payload["environment"] == "prod", payload["automated"]
        problem = _bounded("pruned", pruned) or _manifest_problem(pruned, "pruned")
        if problem:
            return problem
        findings: list[dict[str, str]] = []
        protected = 0

        def severity(production: str, other: str) -> str:
            return production if prod else other

        for manifest in pruned:
            label, kind = _label(manifest), manifest["kind"]
            options = {t.strip() for t in _annotations(manifest).get(SYNC_OPTIONS_ANNOTATION, "").split(",")}
            flux_off = any(isinstance(raw, dict) and isinstance(raw.get(FLUX_PRUNE_KEY), str) and raw[FLUX_PRUNE_KEY].lower() == "disabled"
                           for raw in (_dig(manifest, "metadata", "annotations"), _dig(manifest, "metadata", "labels")))
            if flux_off:
                options.add("Prune=false")
            if "Prune=false" in options or "Prune=confirm" in options:
                protected += 1
                findings.append(_finding(f"protected:{label}", "info",
                                         f"{label} is " + ("skipped by prune (Prune=false)" if "Prune=false" in options
                                                           else "pruned only after manual confirmation (Prune=confirm)")))
                continue
            group, spec = _group(manifest["apiVersion"]), _dict(manifest.get("spec"))
            if kind == "Namespace":
                findings.append(_finding(f"prune-namespace:{label}", "blocker", f"pruning Namespace {label} deletes every object inside it"))
            elif kind == "CustomResourceDefinition":
                findings.append(_finding(f"prune-crd:{label}", "blocker", f"pruning CRD {label} deletes every custom resource of that kind in the cluster"))
            elif kind == "PersistentVolumeClaim":
                findings.append(_finding(f"prune-pvc:{label}", severity("blocker", "major"),
                                         f"pruning PVC {label} deletes its data unless the PersistentVolume reclaim policy is Retain"))
            elif kind == "PersistentVolume":
                findings.append(_finding(f"prune-pv:{label}", "major", f"pruning PersistentVolume {label} can destroy the volume depending on its reclaim policy"))
            elif kind == "StatefulSet":
                if _dig(spec, "persistentVolumeClaimRetentionPolicy", "whenDeleted") == "Delete":
                    findings.append(_finding(f"prune-statefulset-data:{label}", "blocker",
                                             f"StatefulSet {label} has persistentVolumeClaimRetentionPolicy.whenDeleted=Delete: pruning it deletes its PVCs"))
                else:
                    findings.append(_finding(f"prune-statefulset:{label}", severity("major", "minor"),
                                             f"pruning StatefulSet {label} removes the workload (its PVCs are retained)"))
            elif kind == "Secret":
                findings.append(_finding(f"prune-secret:{label}", severity("major", "minor"),
                                         f"Secret {label} is not in Git and may be the only copy of that material"))
            elif kind in _GUARDRAIL_KINDS:
                findings.append(_finding(f"prune-guardrail:{label}", severity("major", "minor"),
                                         f"pruning {label} removes an access-control or admission guardrail"))
            elif manifest["apiVersion"].startswith("argoproj.io/") and kind == "Application":
                cascading = any(isinstance(f, str) and f.startswith(RESOURCES_FINALIZER) for f in _list(_dig(manifest, "metadata", "finalizers")))
                findings.append(_finding(f"prune-application:{label}", severity("blocker", "major") if cascading else "minor",
                                         f"pruning Application {label} deletes it and, through the resources finalizer, every workload it manages" if cascading
                                         else f"pruning Application {label} stops managing its workloads (they keep running unmanaged)"))
            elif manifest["apiVersion"].startswith("argoproj.io/") and kind == "ApplicationSet":
                preserved = _dig(spec, "syncPolicy", "preserveResourcesOnDeletion") is True
                findings.append(_finding(f"prune-applicationset:{label}", "minor" if preserved else severity("blocker", "major"),
                                         f"pruning ApplicationSet {label} keeps the Applications it generated (preserveResourcesOnDeletion)" if preserved
                                         else f"pruning ApplicationSet {label} deletes every Application it generated and, through their finalizers, their workloads"))
            # Crossplane-suffixed groups match regardless of deletionPolicy. The explicit deletionPolicy Delete rule applies to any non-config group.
            elif group not in _CROSSPLANE_CONFIG_GROUPS and (group.endswith((".upbound.io", ".crossplane.io")) or spec.get("deletionPolicy") == "Delete"):
                if spec.get("deletionPolicy") == "Orphan":
                    findings.append(_finding(f"prune-managed-orphan:{label}", "info", f"{label} is a managed resource with deletionPolicy Orphan: the external resource survives"))
                else:
                    crossplane = group.endswith((".upbound.io", ".crossplane.io"))
                    findings.append(_finding(f"prune-managed-resource:{label}", severity("blocker", "major"),
                                             f"{label} is a Crossplane resource with deletionPolicy Delete: pruning deletes the external resource it manages" if crossplane
                                             else f"{label} has deletionPolicy Delete: pruning deletes the external resource it manages"))
        unprotected = len(pruned) - protected
        desired = payload["desired_count"]
        if desired == 0 and unprotected:
            if automated and payload["allow_empty"]:
                findings.append(_finding("empty-desired-state", "blocker", "the desired state is empty and allowEmpty is true: automated prune deletes the whole application"))
            elif automated:
                findings.append(_finding("empty-desired-state-refused", "info", "the desired state is empty: Argo CD refuses an automated sync without allowEmpty"))
            else:
                findings.append(_finding("empty-desired-state-manual", "major", "the desired state is empty: a manual prune sync deletes the whole application"))
        if automated and prod and unprotected:
            findings.append(_finding("prod-auto-prune", "major", f"automated production sync would prune {unprotected} object(s); production prune is L4"))
        if prod:
            autonomy = "L4" if unprotected else "L3"
        else:
            autonomy = "L2"
        return {"state": "SUCCESS", "pruned_count": len(pruned), "protected_count": protected, "unprotected_count": unprotected,
                "autonomy_required": autonomy, "human_approval_required": autonomy != "L2", **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_rollout_check --------------------------------------------------------------------------------------------

_ROLLOUT_STEPS = ("analysis", "experiment", "pause", "plugin", "setCanaryScale", "setHeaderRoute", "setMirrorRoute", "setWeight")
DEFAULT_MAX_FIRST_STEP_WEIGHT = 25  # policy of this check: a first canary step above 25% is a large blast radius
WEIGHT_GRANULARITY_POINTS = 10  # policy: replica rounding that moves a step by 10 points or more is reported
MAX_ROLLOUT_STEPS = 1000  # keeps effective_weights far below the 256 KiB output limit
_PERCENT = re.compile(r"\d{1,6}%")
_DIGITS = re.compile(r"\d{1,9}")


def _int_or_percent(value: object) -> tuple[str, int] | None:
    """("int", n) or ("pct", n) for a Kubernetes IntOrString of a non-negative integer or a percentage; None when invalid."""
    if _is_int(value):
        return ("int", value) if value >= 0 else None
    if isinstance(value, str) and _PERCENT.fullmatch(value):
        return ("pct", int(value[:-1]))
    return None


def _analysis_names(block: object, label: str, known: set[str], findings: list[dict[str, str]]) -> int:
    """Check the AnalysisTemplate references of one analysis block; returns how many templates it references."""
    templates = block.get("templates") if isinstance(block, dict) else None
    if not isinstance(templates, list) or not templates:
        findings.append(_finding(f"analysis-no-templates:{label}", "blocker", f"{label} references no analysis templates"))
        return 0
    for entry in templates:
        name = entry.get("templateName") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            findings.append(_finding(f"analysis-template-name:{label}", "blocker", f"{label} has a template reference without templateName"))
        elif name not in known:
            findings.append(_finding(f"analysis-template-missing:{name}", "blocker",
                                     f"{label} references AnalysisTemplate {name}, which is not among the supplied templates"))
    return len(templates)


def _surge_pods(replicas: int, value: object) -> int:
    """maxSurge as a pod count the way Argo Rollouts resolves it: an integer as is, a percentage of the replicas rounded up, 25% when
    unset; an invalid value resolves to 0 there (the validation error is ignored by the replica math)."""
    parsed = ("pct", 25) if value is None else _int_or_percent(value)
    if parsed is None or replicas == 0:
        return 0
    return parsed[1] if parsed[0] == "int" else -(-parsed[1] * replicas // 100)


def _basic_canary_weight(replicas: int, weight: int, surge: int) -> int:
    """Whole percent of pods that run the canary without traffic routing: a port of Argo Rollouts v1.10
    `approximateWeightedCanaryStableReplicaCounts` (utils/replicaset/canary.go). Candidates are the ceiling and floor of
    weight% of the replicas (the floor only when the two are not equidistant, never 0 for weights 1-99 unless a single
    replica has no surge) at `replicas` pods and, when maxSurge allows, at `replicas + 1` pods; the candidate whose pod ratio is
    closest to the weight wins, the first one on a tie. No candidate at all (for example 5 replicas, 90%, maxSurge 0) leaves
    zero canary pods, as upstream does."""
    if replicas == 0:
        return 0
    exact = replicas * weight / 100
    ceil_pods, floor_pods = math.ceil(exact), math.floor(exact)

    def tied(total: int) -> bool:
        return math.modf(total * (weight / 100))[0] == 0.5

    zero_allowed = weight in (0, 100) or (replicas == 1 and surge == 0)
    options: list[tuple[int, int]] = []
    if ceil_pods < replicas or zero_allowed:
        options.append((ceil_pods, replicas))
    if not tied(replicas) and (floor_pods != 0 or zero_allowed):
        options.append((floor_pods, replicas))
    if surge > 0:
        options.append((ceil_pods, replicas + 1))
        if not tied(replicas + 1) and (floor_pods != 0 or zero_allowed):
            options.append((floor_pods, replicas + 1))
    if not options:
        return 0
    canary, total = min(options, key=lambda option: abs(option[0] * 100 / option[1] - weight))
    return (canary * 200 + total) // (2 * total)


def rollout_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Argo Rollouts validation. Exactly one of canary / blueGreen; canary steps (one action each, setWeight 0-100 and not
    decreasing, pause durations, analysis templates that exist, background analysis), maxSurge / maxUnavailable (both
    zero is invalid; maxUnavailable above 100% is invalid; defaults are 25%), traffic routing services, and a blast
    radius check on the first step (house policy, default 25%, not an Argo Rollouts limit). Without traffic routing Argo
    Rollouts only approximates a weight with whole pods, and maxSurge takes part in the choice, so `effective_weights` shows
    what each step really exposes (see _basic_canary_weight; a step that lands 10 points or more from its weight is reported,
    also a house threshold). With traffic routing the weight is the traffic weight. blueGreen needs distinct active and
    preview services and a prePromotionAnalysis when promotion is automatic."""

    def work() -> dict[str, Any]:
        rollout, known = payload["rollout"], set(payload["analysis_templates"])
        max_first = payload.get("max_first_step_weight", DEFAULT_MAX_FIRST_STEP_WEIGHT)
        problem = _bounded("rollout", rollout)
        if problem:
            return problem
        if rollout.get("apiVersion") != ARGO_API or rollout.get("kind") != "Rollout" or not isinstance(rollout.get("spec"), dict):
            return _invalid("rollout", "rollout must be an argoproj.io/v1alpha1 Rollout with a spec")
        spec, findings = rollout["spec"], []
        if not isinstance(spec.get("selector"), dict):
            findings.append(_finding("no-selector", "blocker", "spec.selector is required"))
        if not isinstance(spec.get("template"), dict) and not isinstance(spec.get("workloadRef"), dict):
            findings.append(_finding("no-template", "blocker", "spec needs a pod template or a workloadRef"))
        replicas = spec.get("replicas", 1)
        if not _is_int(replicas) or replicas < 0:
            findings.append(_finding("replicas-invalid", "blocker", f"spec.replicas {_echo(replicas)} must be a non-negative integer"))
            replicas = 1
        deadline = spec.get("progressDeadlineSeconds")
        if deadline is not None and (not _is_int(deadline) or deadline <= 0):
            findings.append(_finding("progress-deadline", "blocker", "progressDeadlineSeconds must be a positive integer"))
        strategy = _dict(spec.get("strategy"))
        canary, blue_green = strategy.get("canary"), strategy.get("blueGreen")
        effective: list[int] = []
        gates = pause_total = analyses = steps_count = 0
        name = "invalid"
        if canary is not None and blue_green is not None:
            findings.append(_finding("strategy-ambiguous", "blocker", "strategy sets both canary and blueGreen"))
        elif canary is None and blue_green is None:
            findings.append(_finding("strategy-missing", "blocker", "strategy needs canary or blueGreen"))
        elif canary is not None:
            name = "canary"
            if not isinstance(canary, dict):
                findings.append(_finding("canary-invalid", "blocker", "strategy.canary must be an object"))
                canary = {}
            routing = _dict(canary.get("trafficRouting"))
            routed = bool(routing)
            if routed and not _dig(routing, "istio", "destinationRule") and not (canary.get("stableService") and canary.get("canaryService")):
                findings.append(_finding("traffic-routing-services", "blocker", "trafficRouting needs stableService and canaryService (or an Istio destinationRule with subsets)"))
            if canary.get("stableService") and canary.get("stableService") == canary.get("canaryService"):
                findings.append(_finding("canary-equals-stable-service", "blocker", "stableService and canaryService name the same Service; Argo Rollouts requires two"))
            if not routed and canary.get("scaleDownDelaySeconds") is not None:
                findings.append(_finding("scale-down-delay-needs-traffic-routing", "blocker", "canary.scaleDownDelaySeconds can only be used with traffic routing"))
            if not routed and canary.get("dynamicStableScale"):
                findings.append(_finding("dynamic-stable-scale-needs-traffic-routing", "blocker", "canary.dynamicStableScale can only be used with traffic routing"))
            surge_pods = _surge_pods(replicas, canary.get("maxSurge"))
            steps = canary.get("steps")
            if steps is None:
                steps = []
            if not isinstance(steps, list):
                findings.append(_finding("steps-invalid", "blocker", "canary.steps must be a list"))
                steps = []
            if len(steps) > MAX_ROLLOUT_STEPS:
                return _invalid("rollout", f"canary.steps has {len(steps)} entries; at most {MAX_ROLLOUT_STEPS} are analysed")
            steps_count = len(steps)
            if not steps:
                findings.append(_finding("no-steps", "major", "a canary without steps replaces every pod at once like a plain rolling update"))
            previous = None
            for index, step in enumerate(steps):
                actions = [k for k in _ROLLOUT_STEPS if isinstance(step, dict) and k in step]
                unknown = sorted(k for k in step if k not in _ROLLOUT_STEPS) if isinstance(step, dict) else []
                if len(actions) != 1 or unknown:
                    findings.append(_finding(f"step-invalid:{index}", "blocker",
                                             f"step {index} must contain exactly one of {', '.join(_ROLLOUT_STEPS)}" + (f" (unknown: {', '.join(unknown)})" if unknown else "")))
                    continue
                action = actions[0]
                if action == "setWeight":
                    weight = step["setWeight"]
                    if not _is_int(weight) or not 0 <= weight <= 100:
                        findings.append(_finding(f"weight-invalid:{index}", "blocker", f"step {index} setWeight {_echo(weight)} must be an integer from 0 to 100"))
                        continue
                    if previous is not None and weight < previous:
                        findings.append(_finding(f"weight-decrease:{index}", "major", f"step {index} lowers the weight from {previous} to {weight}"))
                    if previous is None and weight > max_first:
                        findings.append(_finding("large-first-step", "minor", f"the first canary step exposes {weight}% (policy limit {max_first}%)"))
                    previous = weight
                    real = weight if routed else _basic_canary_weight(replicas, weight, surge_pods)
                    effective.append(real)
                    if abs(real - weight) >= WEIGHT_GRANULARITY_POINTS:
                        findings.append(_finding(f"weight-granularity:{index}", "minor",
                                                 f"step {index} asks for {weight}% but {replicas} replica(s) without traffic routing expose {real}% "
                                                 f"(whole pods, maxSurge {surge_pods} pod(s))"))
                elif action == "pause":
                    pause = _dict(step["pause"])
                    duration = pause.get("duration")
                    if duration is None:
                        gates += 1
                    elif _is_int(duration) and duration >= 0:
                        pause_total += duration
                    elif isinstance(duration, str) and _DIGITS.fullmatch(duration):
                        pause_total += int(duration)
                    elif isinstance(duration, str) and (seconds := _duration_seconds(duration)) is not None:
                        pause_total += int(seconds)
                    else:
                        findings.append(_finding(f"pause-invalid:{index}", "blocker", f"step {index} pause duration {_echo(duration)} is not seconds or a duration such as 30s, 5m, 1h"))
                elif action == "analysis":
                    analyses += _analysis_names(step["analysis"], f"step {index} analysis", known, findings)
                elif action in ("setHeaderRoute", "setMirrorRoute") and not routed:
                    findings.append(_finding(f"step-needs-traffic-routing:{index}", "blocker", f"step {index} {action} requires canary.trafficRouting"))
                elif action == "setCanaryScale" and not routed:
                    findings.append(_finding(f"step-needs-traffic-routing:{index}", "major", f"step {index} setCanaryScale only decouples scale from weight with canary.trafficRouting"))
            managed = routing.get("managedRoutes")
            if routed and any(isinstance(s, dict) and ("setHeaderRoute" in s or "setMirrorRoute" in s) for s in steps) and not managed:
                findings.append(_finding("managed-routes-missing", "blocker", "setHeaderRoute and setMirrorRoute steps need trafficRouting.managedRoutes"))
            background = canary.get("analysis")
            if background is not None:
                analyses += _analysis_names(background, "background analysis", known, findings)
                start = background.get("startingStep") if isinstance(background, dict) else None
                if _is_int(start) and start >= len(steps) and steps:
                    findings.append(_finding("background-never-starts", "minor", f"background analysis starts at step {start} but there are only {len(steps)} steps"))
            if not analyses:
                findings.append(_finding("no-analysis", "major", "no analysis step or background analysis: nothing verifies the canary before it is promoted"))
            surge, unavailable = canary.get("maxSurge"), canary.get("maxUnavailable")
            parsed: dict[str, tuple[str, int] | None] = {}
            for label, value in (("maxSurge", surge), ("maxUnavailable", unavailable)):
                if value is None:
                    continue
                parsed[label] = _int_or_percent(value)
                if parsed[label] is None:
                    findings.append(_finding(f"{label}-invalid", "blocker", f"{label} {_echo(value)} must be a non-negative integer or a percentage such as 25%"))
            unavailable_parsed = parsed.get("maxUnavailable")
            if unavailable_parsed and unavailable_parsed[0] == "pct" and unavailable_parsed[1] > 100:
                findings.append(_finding("maxUnavailable-over-100", "major",
                                         "maxUnavailable above 100% lets more pods be unavailable than exist; Deployments reject it, Argo Rollouts does not validate it"))
            if "maxSurge" in parsed and "maxUnavailable" in parsed and all(p is not None and p[1] == 0 for p in (parsed["maxSurge"], parsed["maxUnavailable"])):
                findings.append(_finding("surge-and-unavailable-zero", "blocker", "maxSurge and maxUnavailable cannot both be zero"))
        else:
            name = "blueGreen"
            if not isinstance(blue_green, dict):
                findings.append(_finding("blue-green-invalid", "blocker", "strategy.blueGreen must be an object"))
                blue_green = {}
            active, preview = blue_green.get("activeService"), blue_green.get("previewService")
            if not isinstance(active, str) or not active:
                findings.append(_finding("no-active-service", "blocker", "blueGreen.activeService is required"))
            if not preview:
                findings.append(_finding("no-preview-service", "minor", "no previewService: the new version cannot be verified before promotion"))
            elif preview == active:
                findings.append(_finding("preview-equals-active", "blocker", "previewService and activeService are the same Service"))
            pre = blue_green.get("prePromotionAnalysis")
            if pre is not None:
                analyses += _analysis_names(pre, "prePromotionAnalysis", known, findings)
            post = blue_green.get("postPromotionAnalysis")
            if post is not None:
                analyses += _analysis_names(post, "postPromotionAnalysis", known, findings)
            if blue_green.get("autoPromotionEnabled", True) is False:
                gates += 1
            elif pre is None:
                findings.append(_finding("auto-promote-unverified", "major", "automatic promotion without prePromotionAnalysis switches traffic without verification"))
            delay = blue_green.get("scaleDownDelaySeconds")
            if delay is not None and (not _is_int(delay) or delay < 0):
                findings.append(_finding("scale-down-delay-invalid", "blocker", "scaleDownDelaySeconds must be a non-negative integer"))
            elif delay == 0:
                findings.append(_finding("scale-down-immediately", "minor", "scaleDownDelaySeconds 0 removes the old ReplicaSet before endpoints and load balancers have drained (default 30)"))
        return {"state": "SUCCESS", "strategy": name, "step_count": steps_count, "effective_weights": effective,
                "manual_gate_count": gates, "total_pause_seconds": pause_total, "analysis_count": analyses, **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_canary_verdict -------------------------------------------------------------------------------------------

_PERCENTILES = {"p50": 50, "p90": 90, "p95": 95, "p99": 99}


def _aggregate(samples: list[Decimal], how: str) -> Decimal:
    """mean, min, max or a nearest-rank percentile (the value at rank ceil(p * n / 100) of the sorted samples)."""
    ordered = sorted(samples)
    if how == "mean":
        return sum(ordered, Decimal(0)) / len(ordered)
    if how == "max":
        return ordered[-1]
    if how == "min":
        return ordered[0]
    rank = -(-_PERCENTILES[how] * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


def _significant(value: Decimal) -> float:
    """The number for the output: six decimals from 1 upward, six significant digits below 1 (a 1.5e-7 limit stays 1.5e-07)."""
    number = float(value)
    return round(number, 6) if abs(number) >= 1 else float(f"{number:.6g}")


def canary_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    """AnalysisRun-style verdict. Per metric the canary samples are aggregated and compared with a limit: the absolute
    `threshold`, the baseline aggregate widened by `max_regression_pct` (of its magnitude, in the worse direction: up for
    lower-is-better, down for higher-is-better, so a negative baseline of -10 with 10% allows -9 and -11 respectively), or the
    tighter of both. lower-is-better fails when a value is above the limit, higher-is-better when below it (equal passes).
    Individual samples beyond the limit count as failures, and more than `failure_limit` of them fails the metric (Argo
    Rollouts failureLimit). A metric with fewer than `min_samples` canary samples, or one compared with a baseline of fewer than
    `min_samples` samples, is Inconclusive unless it already failed. Any failed metric makes the run Failed (abort), else any
    inconclusive one makes it Inconclusive (pause), else Successful (promote). Non-finite numbers are refused."""

    def work() -> dict[str, Any]:
        metrics, min_samples = payload["metrics"], payload["min_samples"]
        names = [m["name"] for m in metrics]
        if len(set(names)) != len(names):
            return _invalid("metrics", "metric names must be unique")
        for metric in metrics:
            numbers = [metric.get("threshold"), metric.get("max_regression_pct"), *metric["canary_samples"], *metric.get("baseline_samples", [])]
            if not all(value is None or _is_number(value) for value in numbers):
                return _invalid("metrics", f"metric {metric['name']}: thresholds, percentages and samples must be finite numbers")
        results: list[dict[str, Any]] = []
        counts = {"Failed": 0, "Inconclusive": 0, "Successful": 0}
        for metric in metrics:
            name = metric["name"]
            threshold, pct = metric.get("threshold"), metric.get("max_regression_pct")
            baseline = metric.get("baseline_samples", [])
            if threshold is None and pct is None:
                return _invalid("metrics", f"metric {name}: set threshold or max_regression_pct")
            if pct is not None and not baseline:
                return _invalid("metrics", f"metric {name}: max_regression_pct needs baseline_samples")
            lower = metric["direction"] == "lower-is-better"
            if pct is not None and not lower and pct > 100:
                return _invalid("metrics", f"metric {name}: a higher-is-better metric cannot regress by more than 100%")
            limits: list[Decimal] = []
            if threshold is not None:
                limits.append(_dec(threshold))
            if pct is not None:
                base = _aggregate([_dec(v) for v in baseline], metric["aggregation"])
                slack = abs(base) * _dec(pct) / 100
                limits.append(base + slack if lower else base - slack)
            limit = min(limits) if lower else max(limits)
            samples = [_dec(v) for v in metric["canary_samples"]]
            failures = sum(1 for v in samples if (v > limit if lower else v < limit))
            aggregate = _aggregate(samples, metric["aggregation"]) if samples else Decimal(0)
            violated = bool(samples) and (aggregate > limit if lower else aggregate < limit)
            thin_baseline = pct is not None and len(baseline) < min_samples
            if violated or failures > metric["failure_limit"]:
                status = "Failed"
            elif len(samples) < min_samples or thin_baseline:
                status = "Inconclusive"
            else:
                status = "Successful"
            counts[status] += 1
            results.append({"name": name, "status": status, "samples": len(samples), "aggregate": _significant(aggregate),
                            "limit": _significant(limit), "sample_failures": failures})
        verdict = "Failed" if counts["Failed"] else "Inconclusive" if counts["Inconclusive"] else "Successful"
        action = {"Failed": "abort", "Inconclusive": "pause", "Successful": "promote"}[verdict]
        return {"state": "SUCCESS", "verdict": verdict, "action": action, "failed_count": counts["Failed"],
                "inconclusive_count": counts["Inconclusive"], "passed_count": counts["Successful"], "metrics": results}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_promotion_gate -------------------------------------------------------------------------------------------

_CHECK_RANK = {"failure": 0, "pending": 1, "neutral": 2, "skipped": 2, "success": 2}  # GitHub counts neutral and skipped as passing


def _same_commit(left: str, right: str) -> bool:
    """Whether two commit ids (7 to 64 characters, abbreviated or full, any case) name the same commit: one is a prefix of the other."""
    a, b = left.lower(), right.lower()
    return a.startswith(b) or b.startswith(a)


def promotion_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Preconditions of a promotion PR from `source_env` to `target_env`: the target must be the next stage of `env_order`,
    required checks must pass (neutral and skipped count as passing, like GitHub required checks; a required check that
    never reported is waiting), the source must have soaked for `min_soak_minutes` (elapsed >= required passes), enough
    valid approvals (latest non-comment review per user; approved on the head commit, abbreviated ids allowed; the PR author
    cannot approve; a later changes_requested blocks) and no change freeze (start inclusive, end exclusive). blocked > waiting
    > ready. Production promotion is L3 and needs at least one approval. This tool decides readiness; it never merges."""

    def work() -> dict[str, Any]:
        try:
            now = _timestamp(payload["now"], "now")
            deployed = _timestamp(payload["deployed_at"], "deployed_at")
        except ValueError as exc:
            return _invalid("now" if str(exc).startswith("now ") else "deployed_at", str(exc))
        order, source, target = payload["env_order"], payload["source_env"], payload["target_env"]
        if len(set(order)) != len(order):
            return _invalid("env_order", "env_order must not repeat an environment")
        for field in ("source_env", "target_env"):
            if payload[field] not in order:
                return _invalid(field, f"{field} {payload[field]!r} is not in env_order")
        gap = order.index(target) - order.index(source)
        if gap <= 0:
            return _invalid("target_env", "target_env must come after source_env in env_order")
        if deployed > now:
            return _invalid("deployed_at", "deployed_at is later than now")
        freeze_start, freeze_end = payload.get("freeze_start"), payload.get("freeze_end")
        if (freeze_start is None) != (freeze_end is None):
            return _invalid("freeze_start", "freeze_start and freeze_end must be given together")
        freeze = None
        if freeze_start is not None and freeze_end is not None:
            try:
                freeze = (_timestamp(freeze_start, "freeze_start"), _timestamp(freeze_end, "freeze_end"))
            except ValueError as exc:
                return _invalid("freeze_start" if "freeze_start" in str(exc) else "freeze_end", str(exc))
            if freeze[0] >= freeze[1]:
                return _invalid("freeze_end", "freeze_end must be after freeze_start")
        latest: dict[str, tuple[tuple[datetime, int], Any]] = {}
        for index, review in enumerate(payload["approvals"]):
            try:
                when = _timestamp(review["at"], f"approvals[{index}].at")
            except ValueError as exc:
                return _invalid("approvals", str(exc))
            if review["state"] == "commented":
                continue  # a comment neither approves nor withdraws an approval (GitHub reports the latest non-comment review)
            user = review["user"].lower()
            if user not in latest or (when, index) >= latest[user][0]:
                latest[user] = ((when, index), review)

        findings: list[dict[str, str]] = []
        if gap > 1:
            findings.append(_finding("stage-skipped", "blocker", f"{target} is not the next stage after {source}; {order[order.index(source) + 1]} comes first"))
        statuses: dict[str, int] = {}
        for check in payload["checks"]:
            statuses[check["name"]] = min(statuses.get(check["name"], 9), _CHECK_RANK[check["status"]])
        required = sorted(set(payload["required_checks"]))
        for name in required:
            if name not in statuses:
                findings.append(_finding(f"required-check-missing:{name}", "major", f"required check {name} has not reported"))
            elif statuses[name] == 0:
                findings.append(_finding(f"required-check-failed:{name}", "blocker", f"required check {name} failed"))
            elif statuses[name] == 1:
                findings.append(_finding(f"required-check-pending:{name}", "major", f"required check {name} is still running"))
        for name in sorted(set(statuses) - set(required)):
            if statuses[name] == 0:
                findings.append(_finding(f"optional-check-failed:{name}", "minor", f"non-required check {name} failed"))
        elapsed, needed = now - deployed, timedelta(minutes=payload["min_soak_minutes"])
        remaining_minutes = 0
        if elapsed < needed:
            seconds = (needed - elapsed).days * 86400 + (needed - elapsed).seconds + (1 if (needed - elapsed).microseconds else 0)
            remaining_minutes = -(-seconds // 60)
            findings.append(_finding("soak-incomplete", "major", f"{source} has soaked {int(elapsed.total_seconds() // 60)} of {payload['min_soak_minutes']} minutes"))
        valid = 0
        for user, (_, review) in sorted(latest.items()):
            if review["state"] == "changes_requested":
                findings.append(_finding(f"changes-requested:{user}", "blocker", f"{user} requested changes"))
            elif review["state"] == "approved":
                if user == payload["pr_author"].lower():
                    findings.append(_finding(f"self-approval:{user}", "info", f"{user} authored the PR; the approval does not count"))
                elif not _same_commit(review["commit_sha"], payload["head_sha"]):
                    findings.append(_finding(f"stale-approval:{user}", "info", f"{user} approved {review['commit_sha'][:12]}, not the head commit"))
                else:
                    valid += 1
        needed_approvals = payload["required_approvals"]
        if valid < needed_approvals:
            findings.append(_finding("approvals-missing", "major", f"{valid} valid approval(s), {needed_approvals} required"))
        production = payload["target_is_production"]
        if production and needed_approvals < 1:
            findings.append(_finding("prod-without-approval", "major", "a production promotion must require at least one approval (L3)"))
        if freeze is not None and freeze[0] <= now < freeze[1]:
            findings.append(_finding("change-freeze", "blocker", f"change freeze until {freeze_end}"))
        counts = _counts(findings)
        verdict = "blocked" if counts["blocker"] else "waiting" if counts["major"] else "ready"
        ordered = _report(findings)
        return {"state": "SUCCESS", "verdict": verdict, "soak_elapsed_minutes": round(elapsed.total_seconds() / 60, 2),
                "soak_remaining_minutes": remaining_minutes, "valid_approval_count": valid,
                "autonomy_level": "L3" if production else "L2", "severity_counts": ordered["severity_counts"],
                "findings": ordered["findings"]}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_drift_classify -------------------------------------------------------------------------------------------

_BRACKETS = re.compile(r"\[[^\]]*\]")
_SERVER_SET_FIELDS = ("metadata.resourceVersion", "metadata.uid", "metadata.generation", "metadata.creationTimestamp",
                      "metadata.selfLink", "metadata.managedFields")
_SERVER_SET_ANNOTATIONS = ("metadata.annotations.kubectl.kubernetes.io/last-applied-configuration",
                           "metadata.annotations.deployment.kubernetes.io/revision",
                           "metadata.annotations.deprecated.daemonset.template.generation")
# Fields the API server fills in when a manifest omits them (defaulting), matched as a path suffix and only when the
# live object has the field and Git does not (change == added).
_API_DEFAULTED_SUFFIXES = (
    "terminationMessagePath", "terminationMessagePolicy", "imagePullPolicy", "protocol", "spec.dnsPolicy", "spec.restartPolicy",
    "spec.schedulerName", "spec.terminationGracePeriodSeconds", "spec.revisionHistoryLimit",
    "spec.progressDeadlineSeconds", "spec.strategy", "spec.clusterIP", "spec.clusterIPs", "spec.ipFamilies",
    "spec.ipFamilyPolicy", "spec.internalTrafficPolicy", "spec.sessionAffinity",
)
_DANGEROUS_PATH_FRAGMENTS = (
    "securityContext", "hostNetwork", "hostPID", "hostIPC", "hostPath", "privileged", "capabilities", "serviceAccountName",
    "automountServiceAccountToken", "imagePullSecrets", "externalIPs", "loadBalancerSourceRanges",
)
_IMAGE_PATH = re.compile(r"(?:^|\.)image$")
_REPLICA_OWNERS = frozenset({"Deployment", "ReplicaSet", "StatefulSet"})


def _normal_path(path: str) -> str:
    text = path.strip()
    for lead in ("$.", "."):
        if text.startswith(lead):
            text = text[len(lead):]
    return "" if text == "$" else _BRACKETS.sub("[*]", text)


def _path_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "[")


def _classify_diff(diff: dict[str, Any], ignored: list[str]) -> tuple[str, str]:
    kind, path, change, manager = diff["kind"], _normal_path(diff["path"]), diff["change"], diff.get("field_manager", "")
    if any(_path_under(path, prefix) for prefix in ignored):
        return "benign", "covered by ignore_differences"
    if path == "":
        if kind in _GUARDRAIL_KINDS or kind == "Secret":  # a whole Secret is secret material: a self-heal would overwrite its only live copy
            return "dangerous", f"{kind} {change} outside Git"
        return "drift", "resource " + ("exists only in the cluster" if change == "added" else "is missing from the cluster" if change == "removed" else "differs")
    if path == "status" or path.startswith("status.") or any(_path_under(path, f) for f in _SERVER_SET_FIELDS + _SERVER_SET_ANNOTATIONS):
        return "benign", "server-populated field"
    if path == "spec.replicas" and kind in _REPLICA_OWNERS and manager == "kube-controller-manager":
        return "benign", "replica count written by the HPA through the scale subresource"
    if kind in _GUARDRAIL_KINDS:  # before the defaults: a guardrail added outside Git is never a default
        return "dangerous", f"{kind} (access control, admission or network guardrail) changed outside Git"
    if change == "added" and any(path == s or path.endswith("." + s) for s in _API_DEFAULTED_SUFFIXES):
        return "benign", "default applied by the API server"
    if kind == "Secret" and (path == "data" or path == "stringData" or path.startswith(("data.", "stringData."))):
        return "dangerous", "secret material changed outside Git"
    if kind == "Service" and path in ("spec.type", "spec.externalIPs", "spec.loadBalancerSourceRanges"):
        return "dangerous", "service exposure changed outside Git"
    for fragment in _DANGEROUS_PATH_FRAGMENTS:
        if fragment in path:
            return "dangerous", f"security-relevant field {fragment} changed outside Git"
    if _IMAGE_PATH.search(path):
        return "dangerous", "the running image differs from the reviewed one"
    return "drift", "field differs from Git"


def drift_classify(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify field-level differences between live objects and Git (path in dotted form, `[n]` or `[name=x]` for list
    items; `$` or empty for a whole object). benign: server-populated fields (resourceVersion, uid, generation,
    managedFields, status, last-applied annotation), the HPA-written replica count (field manager kube-controller-manager),
    defaults the API server adds, and `ignore_differences` prefixes. dangerous: RBAC, NetworkPolicy and admission objects,
    Secret data, Service exposure, security contexts, host namespaces and mounts, service accounts and images. drift:
    everything else. clean -> ignore; drifted -> self-heal to Git; dangerous -> investigate before any sync."""

    def work() -> dict[str, Any]:
        diffs, ignored = payload["diffs"], [_normal_path(p) for p in payload["ignore_differences"]]
        classified: list[dict[str, str]] = []
        counts: dict[str, int] = {"benign": 0, "drift": 0, "dangerous": 0}
        for diff in diffs:
            klass, reason = _classify_diff(diff, ignored)
            counts[klass] += 1
            namespace = diff.get("namespace", "")
            classified.append({"resource": _scrub(f"{diff['kind']}/{namespace + '/' if namespace else ''}{diff['name']}"),
                               "path": _scrub(diff["path"]), "class": klass, "reason": reason})
        verdict = "dangerous" if counts["dangerous"] else "drifted" if counts["drift"] else "clean"
        action = {"dangerous": "investigate", "drifted": "self-heal", "clean": "ignore"}[verdict]
        return {"state": "SUCCESS", "benign_count": counts["benign"], "drift_count": counts["drift"],
                "dangerous_count": counts["dangerous"], "verdict": verdict, "recommended_action": action,
                "classified": sorted(classified, key=lambda c: ({"dangerous": 0, "drift": 1, "benign": 2}[c["class"]], c["resource"], c["path"]))}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_image_policy ---------------------------------------------------------------------------------------------

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")  # OCI distribution spec tag grammar
MUTABLE_TAGS = frozenset({"latest", "edge", "nightly", "main", "master", "dev", "develop", "stable"})  # convention, not a registry fact
_NUMERIC_TAG = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,30})?")

# Semver: a port of github.com/Masterminds/semver/v3 v3.5.0 (constraints.go, version.go), the library behind Flux ImagePolicy
# `semver.range` (image-reflector-controller go.mod). Its quirks are kept on purpose (`~0.0.0` and `*` accept everything,
# `>*` accepts every non-zero version, `!=1.0.0` accepts prereleases, `1.x.3` parses); tests pin it against the Go library.
# A prerelease version only matches an AND group in which one constraint names a prerelease (3.4.0 and later).
_WS = r"[ \t\n\f\r]"
_SEMVER_MAX_VERSION = 256
_SEMVER_MAX_CONSTRAINT = 512
_SEMVER_MAX_GROUPS = 32
_UINT64 = 1 << 64
_LOOSE_VERSION = re.compile(r"v?([0-9]+)(\.[0-9]+)?(\.[0-9]+)?(-([0-9A-Za-z\-]+(\.[0-9A-Za-z\-]+)*))?(\+([0-9A-Za-z\-]+(\.[0-9A-Za-z\-]+)*))?", re.ASCII)
_CV = (r"v?([0-9|xX*]+)(\.[0-9|xX*]+)?(\.[0-9|xX*]+)?(-([0-9A-Za-z\-]+(\.[0-9A-Za-z\-]+)*))?(\+([0-9A-Za-z\-]+(\.[0-9A-Za-z\-]+)*))?")
_OPS = r"=|!=|>|<|>=|=>|<=|=<|~|~>|\^"
_CONSTRAINT = re.compile(rf"{_WS}*((?:{_OPS})?){_WS}*({_CV}){_WS}*", re.ASCII)
_FIND_CONSTRAINT = re.compile(rf"((?:{_OPS})?){_WS}*({_CV})", re.ASCII)
_RANGE = re.compile(rf"{_WS}*({_CV}){_WS}+-{_WS}+({_CV}){_WS}*", re.ASCII)
# Masterminds validates a group as `token (sep token)*`. A token is an optional operator and its version, and whitespace may
# follow an operator only: a whitespace run between two tokens can then be read in one way, so the match cannot backtrack exponentially.
_TOKEN = rf"(?:(?:{_OPS}){_WS}*)?{_CV}"
_VALID_GROUP = re.compile(rf"{_WS}*{_TOKEN}(?:(?:{_WS}+|{_WS}*,{_WS}*){_TOKEN})*{_WS}*", re.ASCII)


def _parse_image(ref: str) -> tuple[str, str, str]:
    """(repository, tag, digest) of an image reference; ValueError when it is not a valid reference."""
    if not ref or any(char.isspace() for char in ref):
        raise ValueError("image must be a non-empty reference without whitespace")
    name, separator, digest = ref.partition("@")
    if separator and not _DIGEST.fullmatch(digest):
        raise ValueError("the digest must be sha256:<64 hex> or sha512:<128 hex>")
    tag = ""
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
        if not _TAG.fullmatch(tag):
            raise ValueError("the tag is not a valid OCI tag")
    if not name or name.startswith("/") or name.endswith("/"):
        raise ValueError("image has no repository")
    return name, tag, digest


def _version_key(version: tuple[int, int, int, str]) -> tuple[object, ...]:
    """Semver 2.0 precedence key of (major, minor, patch, prerelease or ""); build metadata never takes part. A numeric
    identifier sorts below an alphanumeric one; a digits-only identifier above uint64 counts as text, as in Masterminds."""
    major, minor, patch, pre = version
    identifiers = tuple((0, int(i), "") if i.isascii() and i.isdigit() and int(i) < _UINT64 else (1, 0, i) for i in pre.split(".")) if pre else ()
    return (major, minor, patch, 0 if pre else 1, identifiers)


def _compare(left: tuple[int, int, int, str], right: tuple[int, int, int, str]) -> int:
    a, b = _version_key(left), _version_key(right)
    return (a > b) - (a < b)


def _semver_parse(text: str) -> tuple[int, int, int, str] | None:
    """(major, minor, patch, prerelease or "") of a version as Masterminds NewVersion reads it (optional v, missing minor and
    patch are 0, leading zeros allowed in the numbers); None when it is not a version."""
    if len(text) > _SEMVER_MAX_VERSION:
        return None
    match = _LOOSE_VERSION.fullmatch(text)
    if not match:
        return None
    numbers = [int(match.group(1)), int((match.group(2) or ".0")[1:]), int((match.group(3) or ".0")[1:])]
    pre = match.group(5) or ""
    if any(number >= _UINT64 for number in numbers):
        return None
    if any(part.isdigit() and len(part) > 1 and part[0] == "0" for part in pre.split(".")) if pre else False:
        return None
    major, minor, patch = numbers
    return major, minor, patch, pre


def _release_version(tag: str) -> tuple[int, int, int, str] | None:
    """(major, minor, patch, prerelease or "") of a version tag (v1.2, 1.2.3-rc.1); None for anything else, wildcards included."""
    return _semver_parse(tag)


def _is_x(text: str) -> bool:
    return text in ("x", "X", "*")


class _Comparator(TypedDict):
    op: str
    con: tuple[int, int, int, str]
    dirty: bool
    minor_dirty: bool
    patch_dirty: bool


def _parse_comparator(token: str) -> _Comparator:
    match = _CONSTRAINT.fullmatch(token)
    if not match:
        raise ValueError("improper constraint")
    operator, major, minor, patch, pre = match.group(1), match.group(3), match.group(4) or "", match.group(5) or "", match.group(6) or ""
    version, dirty, minor_dirty, patch_dirty = match.group(2), False, False, False
    if _is_x(major):
        version, dirty = "0.0.0" + pre, True
    elif _is_x(minor[1:]) or minor == "":
        version, dirty, minor_dirty = f"{major}.0.0{pre}", True, True
    elif _is_x(patch[1:]) or patch == "":
        version, dirty, patch_dirty = f"{major}{minor}.0{pre}", True, True
    parsed = _semver_parse(version)
    if parsed is None:
        raise ValueError("improper constraint")
    return {"op": operator, "con": parsed, "dirty": dirty, "minor_dirty": minor_dirty, "patch_dirty": patch_dirty}


def _tilde(v: tuple[int, int, int, str], c: _Comparator) -> bool:
    con = c["con"]
    if _compare(v, con) < 0:
        return False
    if con[:3] == (0, 0, 0) and not c["minor_dirty"] and not c["patch_dirty"]:
        return True  # ~0.0.0 is a special case where every version is accepted
    if v[0] != con[0]:
        return False
    return v[1] == con[1] or c["minor_dirty"]


def _caret(v: tuple[int, int, int, str], c: _Comparator) -> bool:
    con = c["con"]
    if _compare(v, con) < 0:
        return False
    if con[0] > 0 or c["minor_dirty"]:
        return v[0] == con[0]
    if v[0] > 0:
        return False
    if con[1] > 0 or c["patch_dirty"]:
        return v[1] == con[1]
    if v[1] > 0:
        return False
    return v[2] == con[2]


def _not_equal(v: tuple[int, int, int, str], c: _Comparator) -> bool:
    con = c["con"]
    if c["dirty"]:
        if con[0] != v[0]:
            return True
        if con[1] != v[1] and not c["minor_dirty"]:
            return True
        if c["minor_dirty"]:
            return False
        if con[2] != v[2] and not c["patch_dirty"]:
            return True
        if c["patch_dirty"]:
            return (v[3] != "" or con[3] != "") and _version_key((0, 0, 0, v[3])) != _version_key((0, 0, 0, con[3]))
    return _compare(v, con) != 0


def _greater_than(v: tuple[int, int, int, str], c: _Comparator) -> bool:
    con = c["con"]
    if not c["dirty"]:
        return _compare(v, con) > 0
    if v[0] != con[0]:
        return v[0] > con[0]
    if c["minor_dirty"]:
        return False
    if c["patch_dirty"]:
        return v[1] > con[1]
    return _compare(v, con) > 0


def _less_equal(v: tuple[int, int, int, str], c: _Comparator) -> bool:
    con = c["con"]
    if not c["dirty"]:
        return _compare(v, con) <= 0
    if v[0] > con[0]:
        return False
    return not (v[0] == con[0] and v[1] > con[1] and not c["minor_dirty"])


def _check_comparator(c: _Comparator, v: tuple[int, int, int, str], include_pre: bool) -> bool:
    if v[3] != "" and not include_pre:
        return False
    operator, con = c["op"], c["con"]
    if operator in ("", "="):
        return _tilde(v, c) if c["dirty"] else _compare(v, con) == 0
    if operator == "!=":
        return _not_equal(v, c)
    if operator == ">":
        return _greater_than(v, c)
    if operator in (">=", "=>"):
        return _compare(v, con) >= 0
    if operator == "<":
        return _compare(v, con) < 0
    if operator in ("<=", "=<"):
        return _less_equal(v, c)
    if operator in ("~", "~>"):
        return _tilde(v, c)
    return _caret(v, c)


def _parse_constraint(text: str) -> Callable[[tuple[int, int, int, str]], bool]:
    """Predicate for a Masterminds semver constraint (=, !=, >, >=, <, <=, ~, ~>, ^, x wildcards, `A - B` ranges, `||`, comma or
    space AND); ValueError when Masterminds rejects it."""
    if len(text) > _SEMVER_MAX_CONSTRAINT:
        raise ValueError("constraint string is too long (max 512 bytes)")
    text = _RANGE.sub(lambda match: f">= {match.group(1)}, <= {match.group(11)} ", text)
    segments = text.split("||")
    if len(segments) > _SEMVER_MAX_GROUPS:
        raise ValueError("too many constraint groups (max 32)")
    groups: list[tuple[list[_Comparator], bool]] = []
    for segment in segments:
        if not _VALID_GROUP.fullmatch(segment):
            raise ValueError("improper constraint")
        comparators = [_parse_comparator(found.group(0)) for found in _FIND_CONSTRAINT.finditer(segment)]
        groups.append((comparators, any(c["con"][3] != "" for c in comparators)))
    return lambda v: any(all(_check_comparator(c, v, include_pre) for c in comparators) for comparators, include_pre in groups)


def image_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Image reference hygiene plus a Flux ImagePolicy-style selection. The reference is pinned only by a digest; no tag
    or one of MUTABLE_TAGS (latest, main, ...: a convention, not a registry fact) is floating. Policies: semver (a port of
    Masterminds/semver v3.5.0, the library Flux uses: =, !=, >, >=, <, <=, ~, ^, x wildcards, `A - B` ranges, `||` and comma
    or space AND; a prerelease matches only an AND group that names a prerelease; tags that are not versions are ignored,
    as Flux ignores them), alphabetical and numerical (Flux picks the last tag in the sorted order: asc selects the
    greatest, desc the smallest). Flux rejects the whole candidate list when a numerical tag is not a number, so filter the
    tags first; here such tags are ignored. Anything credential-shaped in the reference is redacted in every output."""

    def work() -> dict[str, Any]:
        try:
            repository, tag, digest = _parse_image(payload["image"])
        except ValueError as exc:
            return _invalid("image", str(exc))
        candidates, policy = payload["candidate_tags"], payload.get("policy")
        for index, item in enumerate(candidates):
            if not _TAG.fullmatch(item):
                return _invalid("candidate_tags", f"candidate_tags[{index}] is not a valid OCI tag")
        findings: list[dict[str, str]] = []
        pinned = bool(digest)
        floating = not pinned and (not tag or tag in MUTABLE_TAGS)
        shown, shown_tag = _redact(payload["image"]), _redact(tag)
        if floating:
            findings.append(_finding("floating-tag", "major", f"{shown} resolves to {shown_tag or 'the implicit latest tag'}: the content moves without a Git change"))
        elif not pinned and payload["require_digest"]:
            findings.append(_finding("digest-required", "major", f"{shown} is pinned by tag only; policy requires a sha256 digest"))
        elif not pinned:
            findings.append(_finding("tag-not-immutable", "info", f"tag {shown_tag} can be re-pushed; a digest pins the content"))
        if pinned and tag:
            findings.append(_finding("tag-ignored-with-digest", "info", f"the runtime resolves {digest[:19]}...; the tag {shown_tag} is informational"))
        selected, eligible, in_range = "", 0, False
        if policy is not None:
            kind, order = policy["type"], policy.get("order", "asc")
            if kind == "semver":
                try:
                    allows = _parse_constraint(policy.get("range", "*"))
                except ValueError as exc:
                    return _invalid("policy", f"policy.range: {exc}")
                versions: dict[str, tuple[int, int, int, str]] = {}
                for item in candidates:
                    version = _release_version(item)
                    if version is not None and allows(version):
                        versions[item] = version
                eligible = len(versions)
                if versions:
                    selected = max(versions, key=lambda t: (_version_key(versions[t]), t))
                current = _release_version(tag) if tag else None
                if current is None:
                    findings.append(_finding("current-tag-not-semver", "info", f"current tag {shown_tag or '<none>'} is not a semantic version"))
                else:
                    in_range = allows(current)
                    if selected and _version_key(versions[selected]) < _version_key(current):
                        findings.append(_finding("selected-older-than-current", "minor", f"the policy selects {_redact(selected)}, older than the current {shown_tag}"))
                if allows((999_999, 0, 0, "")):
                    findings.append(_finding("range-unbounded-major", "minor", f"range {_echo(policy.get('range', '*'))} also accepts future major versions"))
            elif kind == "alphabetical":
                eligible = len(candidates)
                if candidates:
                    selected = max(candidates) if order == "asc" else min(candidates)
                in_range = bool(tag)
            else:
                numbers = {item: Decimal(item) for item in candidates if _NUMERIC_TAG.fullmatch(item)}
                eligible = len(numbers)
                if numbers:
                    pick = max if order == "asc" else min
                    selected = pick(numbers, key=lambda t: (numbers[t], t))
                in_range = bool(_NUMERIC_TAG.fullmatch(tag))
            if candidates and not selected:
                findings.append(_finding("no-eligible-candidate", "major", f"none of the {len(candidates)} candidate tags satisfies the {kind} policy"))
        return {"state": "SUCCESS", "repository": _redact(repository), "tag": shown_tag, "digest": digest, "digest_pinned": pinned,
                "floating": floating, "policy_applied": policy is not None, "current_in_range": in_range,
                "selected_tag": _redact(selected), "update_available": bool(selected) and selected != tag, "eligible_count": eligible,
                **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_secret_scan ----------------------------------------------------------------------------------------------

_SECRET_WORDS = ("password", "passwd", "passphrase", "pwd", "secret", "token", "apikey", "api_key", "api-key", "private_key", "privatekey",
                 "private-key", "access_key", "accesskey", "credential")
_NOT_A_SECRET_SUFFIX = ("name", "file", "path", "ref", "id", "url", "enabled", "arn", "namespace")
_SECRET_PATTERNS = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
_URL_PASSWORD_AT = re.compile(r"[^:]*:(.+)")  # the userinfo of a URL: the user, then the password, which may itself hold colons
_URL_USERINFO_AT = re.compile(r"[^/\s@]+@")
_EXTERNAL_SECRET_GROUPS = ("external-secrets.io",)

# What a finding may repeat of a caller-supplied value. Findings name keys, paths and kinds, never values, and every finding passes
# through _scrub; the few places that quote a field (a revision, an interval, a tag, a hook type, an owner) use _echo, which also
# caps the length. What is blanked: well-known token formats, user[:password]@ in URLs, long hex or token-like runs and mnemonic
# phrases. Everything here is linear in the text: the inputs are free-form and up to 64 KiB per string.
_REDACTED = "<redacted>"
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+_=]{40,}")
_MNEMONIC = re.compile(r"\b(?:[a-z]{3,8} ){11,23}[a-z]{3,8}\b")
_MAX_ECHO = 120


def _userinfo_spans(text: str) -> Iterator[tuple[int, int]]:
    """(start, end) of the `user[:password]` part of every `scheme://user[:password]@host` in text (end is the index of the @)."""
    position = 0
    while True:
        index = text.find("://", position)
        if index < 0:
            return
        start = position = index + 3
        if index == 0 or not (text[index - 1].isalnum() or text[index - 1] in "+.-"):
            continue
        match = _URL_USERINFO_AT.match(text, start)
        if match:
            yield start, match.end() - 1
            position = match.end()


def _bare_token(userinfo: str) -> bool:
    """A userinfo with no colon that looks like a token: 16 or more characters with letters and digits (a plain user name such as git is not)."""
    return ":" not in userinfo and len(userinfo) >= 16 and any(c.isdigit() for c in userinfo) and any(c.isalpha() for c in userinfo)


def _url_passwords(text: str) -> Iterator[str]:
    """The password of every `scheme://user[:password]@host` in text, or the whole userinfo when it is a bare token."""
    for start, end in _userinfo_spans(text):
        userinfo = text[start:end]
        match = _URL_PASSWORD_AT.fullmatch(userinfo)
        if match:
            yield match.group(1)
        elif _bare_token(userinfo):
            yield userinfo


def _blank_token_run(match: re.Match[str]) -> str:
    run = match.group(0)
    return _REDACTED if any(char.isdigit() for char in run) and any(char.isalpha() for char in run) else run


def _scrub(text: str) -> str:
    """text with every credential-shaped part replaced by <redacted> (no length cap)."""
    pieces: list[str] = []
    last = 0
    for start, end in _userinfo_spans(text):
        pieces += [text[last:start], _REDACTED]
        last = end
    out = "".join(pieces) + text[last:] if pieces else text
    for _, pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    out = _MNEMONIC.sub(_REDACTED, _LONG_HEX.sub(_REDACTED, out))
    return _TOKEN_RUN.sub(_blank_token_run, out)


def _redact(text: str) -> str:
    """_scrub, capped at _MAX_ECHO characters: for a quoted field."""
    out = _scrub(text)
    return out if len(out) <= _MAX_ECHO else out[:_MAX_ECHO] + "..."


def _echo(value: object) -> str:
    """repr of a caller-supplied value for a finding, with credential-shaped parts redacted."""
    if isinstance(value, str):
        return repr(_redact(value))
    return repr(value) if value is None or isinstance(value, (bool, int, float)) else f"<{type(value).__name__}>"


_NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")  # words of a name: split on _ . - and camelCase boundaries
_WHOLE_WORD_SECRETS = frozenset({"pass", "key"})  # matched as a whole word only: DB_PASS, dbPass and ENCRYPTION_KEY are secrets, passport and monkey are not


def _secret_like_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith("existing") or lowered.endswith(_NOT_A_SECRET_SUFFIX):
        return False
    return any(word in lowered for word in _SECRET_WORDS) or not _WHOLE_WORD_SECRETS.isdisjoint(found.lower() for found in _NAME_WORD.findall(name))


_PLACEHOLDER_WORD = re.compile(r"^\s*(?:CHANGE_?ME|REPLACE_?ME|xxx+|dummy)\s*$", re.IGNORECASE)
_TEMPLATE_TOKENS = (("${", "}"), ("$(", ")"), ("{{", "}}"))


def _strip_templates(text: str) -> str:
    """text without its ${...}, $(...), {{...}} and <...> tokens; linear (a closer that is not found is remembered as absent)."""
    out: list[str] = []
    index, size, absent = 0, len(text), set[str]()
    while index < size:
        for opener, closer in _TEMPLATE_TOKENS:
            if closer not in absent and text.startswith(opener, index):
                end = text.find(closer, index + len(opener))
                if end < 0:
                    absent.add(closer)
                else:
                    index = end + len(closer)
                    break
        else:
            if text[index] == "<":
                end = index + 1
                while end < size and text[end] not in "<>":
                    end += 1
                if end < size and text[end] == ">":
                    index = end + 1
                    continue
            out.append(text[index])
            index += 1
    return "".join(out)


def _real_value(value: object) -> bool:
    """A string that is a literal value. Empty text and values made *entirely* of template or placeholder tokens (<token>, ${TOKEN},
    $(TOKEN), {{ .Values.t }}, CHANGEME) are not: `hunter2${x}` and `Bearer ${T}` still hold literal text."""
    if not isinstance(value, str) or _PLACEHOLDER_WORD.match(value):
        return False
    return any(char.isalnum() for char in _strip_templates(value))


def _leaves(node: object, path: str = "") -> Iterator[tuple[str, str]]:
    """(path, value) of every scalar string leaf, depth first with dict keys sorted."""
    stack = [(path, node)]
    while stack:
        current, item = stack.pop()
        if isinstance(item, dict):
            stack.extend((f"{current}.{key}" if current else key, item[key]) for key in sorted(item, reverse=True))
        elif isinstance(item, list):
            stack.extend((f"{current}[{index}]", item[index]) for index in range(len(item) - 1, -1, -1))
        elif isinstance(item, str):
            yield current, item


def _expand_lists(manifests: list[Any]) -> list[Any]:
    """The manifests with every `kind: List` (or *List) replaced by its items, nested lists included; kubectl get -o yaml emits those
    and their own metadata has no name."""
    out, pending = [], list(reversed(manifests))
    while pending:
        item = pending.pop()
        if isinstance(item, dict) and isinstance(item.get("kind"), str) and item["kind"].endswith("List") and isinstance(item.get("items"), list):
            pending.extend(reversed(item["items"]))
        else:
            out.append(item)
    return out


def secret_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Plaintext secrets in manifests versus secret references. A v1 Secret with data or stringData is plaintext (base64 is
    an encoding) unless it is SOPS-encrypted (top-level `sops` key and every value ENC[...]); a SealedSecret
    (bitnami.com), ExternalSecret (external-secrets.io) or SecretProviderClass is a reference. Also flagged: env vars and
    ConfigMap keys whose names look like secrets (password, token, apiKey, ...) holding literal values, well-known
    credential formats (AWS access key id, PEM private key, GitHub and Slack tokens, Google API key) and user:password@
    in URLs. A `kind: List` is scanned item by item. Values made entirely of placeholders (empty, <...>, ${...}, $(...),
    {{...}}, CHANGEME) are ignored. A finding names the object, the key or path and the kind of credential, never a value
    (key names and paths pass through the same redaction as every quoted field)."""

    def work() -> dict[str, Any]:
        manifests = _expand_lists(payload["manifests"])
        problem = _bounded("manifests", manifests) or _manifest_problem(manifests, "manifests")
        if problem:
            return problem
        findings: list[dict[str, str]] = []
        exposures = references = 0
        for manifest in manifests:
            label, kind, api = _label(manifest), manifest["kind"], manifest["apiVersion"]
            group = _group(api)
            skip_patterns = False
            if kind == "Secret" and api == "v1":
                values: dict[str, object] = {}
                for section in ("data", "stringData"):
                    block = manifest.get(section)
                    values.update({f"{section}.{k}": v for k, v in (block.items() if isinstance(block, dict) else [])})
                real = sorted(k for k, v in values.items() if _real_value(v))
                if isinstance(manifest.get("sops"), dict) and values:
                    clear = sorted(k for k, v in values.items() if not (isinstance(v, str) and v.startswith("ENC[")))
                    if clear:
                        exposures += 1
                        findings.append(_finding(f"sops-partially-encrypted:{label}", "blocker", f"{label} has a sops block but {_preview([_redact(k) for k in clear])} are not ENC[...] values"))
                    else:
                        references += 1
                elif real:
                    exposures += 1
                    findings.append(_finding(f"plaintext-secret:{label}", "blocker",
                                             f"{label} commits {len(real)} secret value(s) in clear (base64 is not encryption): {_preview([_redact(k) for k in real])}"))
                skip_patterns = True
            elif kind == "SealedSecret" and group == "bitnami.com":
                encrypted = _dig(manifest, "spec", "encryptedData")
                if isinstance(encrypted, dict) and encrypted:
                    references += 1
                else:
                    findings.append(_finding(f"empty-sealed-secret:{label}", "major", f"{label} has no spec.encryptedData"))
            elif kind == "ExternalSecret" and group in _EXTERNAL_SECRET_GROUPS:
                references += 1
                if not _dig(manifest, "spec", "secretStoreRef", "name"):
                    findings.append(_finding(f"external-secret-no-store:{label}", "major", f"{label} has no spec.secretStoreRef.name"))
                if not _dig(manifest, "spec", "data") and not _dig(manifest, "spec", "dataFrom"):
                    findings.append(_finding(f"external-secret-empty:{label}", "minor", f"{label} maps no keys (spec.data and spec.dataFrom are empty)"))
            elif kind == "SecretProviderClass" and group == "secrets-store.csi.x-k8s.io":
                references += 1
            elif kind == "ConfigMap":
                data = manifest.get("data")
                for key in sorted(data) if isinstance(data, dict) else []:
                    value = data[key]
                    if _secret_like_name(key) and _real_value(value) and len(value) >= 8:
                        exposures += 1
                        findings.append(_finding(f"secret-in-configmap:{label}:{_redact(key)}", "major",
                                                 f"ConfigMap {label} key {_redact(key)} looks like a secret and holds a literal value; ConfigMaps are not encrypted"))
            stack = [("", manifest)]
            while stack:
                path, node = stack.pop()
                if isinstance(node, dict):
                    stack.extend((f"{path}.{k}" if path else k, v) for k, v in node.items())
                elif isinstance(node, list):
                    for index, item in enumerate(node):
                        stack.append((f"{path}[{index}]", item))
                        if path.endswith(("env", "parameters")) and isinstance(item, dict) and isinstance(item.get("name"), str) \
                                and "value" in item and "valueFrom" not in item and _secret_like_name(item["name"]) and _real_value(item["value"]):
                            exposures += 1
                            if path.endswith("env"):
                                findings.append(_finding(f"plaintext-env:{label}:{_redact(path)}[{index}]", "major",
                                                         f"{label} sets env {_redact(item['name'])} to a literal value; use secretKeyRef or an external secret"))
                            else:
                                findings.append(_finding(f"plaintext-parameter:{label}:{_redact(path)}[{index}]", "major",
                                                         f"{label} sets Helm parameter {_redact(item['name'])} to a literal value; take it from a secret reference"))
            for path, text in _leaves(manifest):
                if skip_patterns and path.split(".", 1)[0] in ("data", "stringData"):
                    continue  # base64 data is not literal text; the rest of a Secret (annotations, labels, type) is still scanned
                for name, pattern in _SECRET_PATTERNS:
                    if pattern.search(text):
                        exposures += 1
                        findings.append(_finding(f"secret-pattern:{name}:{label}:{_redact(path)}", "blocker", f"{label} {_redact(path)} contains a credential of type {name} (value not shown)"))
                for password in _url_passwords(text):
                    if _real_value(password):
                        exposures += 1
                        findings.append(_finding(f"credentials-in-url:{label}:{_redact(path)}", "blocker", f"{label} {_redact(path)} embeds user:password in a URL (value not shown)"))
        return {"state": "SUCCESS", "manifest_count": len(manifests), "exposure_count": exposures, "reference_count": references, **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result


# --- gitops_repo_layout ----------------------------------------------------------------------------------------------

_ENV_ROOTS = frozenset({"overlays", "environments", "envs", "env"})
_KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
_PRIVATE_FILE_SUFFIXES = (".key", ".pfx", ".p12", ".jks", ".keystore", ".p8", ".ppk", ".env")
_PRIVATE_FILE_NAMES = frozenset({".netrc", ".pgpass"})  # dotfiles with no suffix; .env is covered by _PRIVATE_FILE_SUFFIXES
_PRIVATE_FILE_STEMS = frozenset({"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "kubeconfig"})  # the name up to its first dot: kubeconfig.yaml, id_dsa
_OWNER = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_.-]+)?|[^@\s]+@[^@\s]+\.[^@\s]+)$")
_MAX_OWNER_LENGTH = 254
_MAX_DOUBLE_STAR = 2
MAX_LISTED = 200  # environments and cluster directories listed in the result; keeps it far below the 256 KiB output limit


def _codeowners_pattern(pattern: str) -> tuple[bool, tuple[str, ...], str] | None:
    """(anchored, segments, mode) of a CODEOWNERS pattern (gitignore-like; no negation, no character classes), or None when
    unsupported. mode is `dir` (trailing slash: needs something inside), `open` (a plain name: the path itself or anything under
    it) or `exact` (a wildcard in the last segment: that depth only)."""
    if pattern.startswith("!") or "[" in pattern or "\\" in pattern:
        return None
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    directory = body.endswith("/")
    body = body.rstrip("/")
    if not body:
        return None
    segments = body.split("/")
    if sum(1 for s in segments if s == "**") > _MAX_DOUBLE_STAR:
        return None
    last = segments[-1]
    mode = "dir" if directory else "open" if last != "**" and "*" not in last and "?" not in last else "exact"
    return anchored or len(segments) > 1, tuple(segments), mode


def _glob(pattern: str, text: str) -> bool:
    """`*` (any characters) and `?` (one character) over one path segment; O(len(pattern) * len(text)), never exponential."""
    p = t = mark = 0
    star = -1
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, mark, p = p, t, p + 1
        elif p < len(pattern) and (pattern[p] == "?" or pattern[p] == text[t]):
            p, t = p + 1, t + 1
        elif star >= 0:
            mark += 1
            p, t = star + 1, mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _pattern_matches(rule: tuple[bool, tuple[str, ...], str], path_segments: list[str]) -> bool:
    anchored, segments, mode = rule
    size, last = len(path_segments), len(segments) - 1
    memo: dict[tuple[int, int], bool] = {}

    def walk(si: int, pi: int) -> bool:
        key = (si, pi)
        if key not in memo:
            if si == len(segments):
                memo[key] = pi < size if mode == "dir" else mode == "open" or pi == size
            elif segments[si] == "**":
                if si == last:
                    memo[key] = size - pi >= (2 if mode == "dir" else 1 if si else 0)
                else:
                    memo[key] = any(walk(si + 1, k) for k in range(pi, size + 1))
            else:
                memo[key] = pi < size and _glob(segments[si], path_segments[pi]) and walk(si + 1, pi + 1)
        return memo[key]

    return any(walk(0, start) for start in ([0] if anchored else range(size)))


def _codeowners_rules(text: str, findings: list[dict[str, str]]) -> list[tuple[tuple[bool, tuple[str, ...], str], list[str]]]:
    rules: list[tuple[tuple[bool, tuple[str, ...], str], list[str]]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith(("[", "^[")):
            continue
        parts = line.split()
        rule = _codeowners_pattern(parts[0])
        if rule is None:
            findings.append(_finding(f"codeowners-pattern-unsupported:{number}", "minor",
                                     f"CODEOWNERS line {number}: pattern {_echo(parts[0])} is not supported (no negation, character classes or escapes; at most 2 **)"))
            continue
        owners = parts[1:]
        valid = [o for o in owners if len(o) <= _MAX_OWNER_LENGTH and _OWNER.match(o)]
        for owner in owners:
            if owner not in valid:
                findings.append(_finding(f"invalid-owner:{number}:{_redact(owner)}", "minor",
                                         f"CODEOWNERS line {number}: {_echo(owner)} is not @user, @org/team or an email"))
        rules.append((rule, valid))
    return rules


def _owners_of(rules: list[tuple[tuple[bool, tuple[str, ...], str], list[str]]], path: str) -> list[str]:
    """Owners of the last pattern that matches the path (the last matching pattern wins), none when no pattern matches."""
    segments = path.split("/")
    for rule, rule_owners in reversed(rules):
        if _pattern_matches(rule, segments):
            return rule_owners
    return []


def repo_layout(payload: dict[str, Any]) -> dict[str, Any]:
    """GitOps repository layout from a file list. An environment exists when a path runs through overlays/<env>/,
    environments/<env>/, envs/<env>/, env/<env>/ or is values-<env>.y(a)ml or values/<env>.y(a)ml. Checks: expected
    environments present, overlays with a kustomization file and a sibling base/, every application having every expected
    overlay, registered clusters having clusters/<name>/ (Flux: flux-system/gotk-sync.yaml and gotk-components.yaml),
    unregistered cluster directories, private-key-looking files, and CODEOWNERS: present at a location GitHub or GitLab
    reads and, when its text is supplied, owning every production environment (last matching pattern wins)."""

    def work() -> dict[str, Any]:
        paths = payload["paths"]
        expected, production = payload["expected_envs"], payload["production_envs"]
        for index, path in enumerate(paths):
            if path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
                return _invalid("paths", f"paths[{index}] must be a clean repo-relative POSIX path")
        if any(not env or "/" in env for env in expected + payload["clusters"]):
            return _invalid("expected_envs", "environment and cluster names must be non-empty and contain no /")
        if not set(production) <= set(expected):
            return _invalid("production_envs", "production_envs must be a subset of expected_envs")
        findings: list[dict[str, str]] = []
        files = set(paths)
        env_paths: dict[str, list[str]] = {}
        overlay_dirs: dict[str, set[str]] = {}  # application prefix -> envs with an overlay dir
        overlay_roots: set[str] = set()
        for path in sorted(files):
            parts = path.split("/")
            for index, segment in enumerate(parts[:-2]):
                if segment in _ENV_ROOTS:
                    env = parts[index + 1]
                    env_paths.setdefault(env, []).append(path)
                    if segment == "overlays":
                        prefix = "/".join(parts[:index])
                        overlay_dirs.setdefault(prefix, set()).add(env)
                        overlay_roots.add("/".join(parts[:index + 2]))
            match = re.fullmatch(r"values-(.+)\.ya?ml", parts[-1])
            if match:
                env_paths.setdefault(match.group(1), []).append(path)
            elif len(parts) > 1 and parts[-2] == "values" and re.fullmatch(r".+\.ya?ml", parts[-1]):
                env_paths.setdefault(parts[-1].rsplit(".", 1)[0], []).append(path)
        found = sorted(env_paths)
        missing = [env for env in expected if env not in env_paths]
        for env in missing:
            findings.append(_finding(f"env-missing:{env}", "major", f"no overlay, environment directory or values file for {env}"))
        for env in found:
            if env not in expected:
                findings.append(_finding(f"env-unexpected:{env}", "info", f"environment {env} is present but not in expected_envs"))
        for directory in sorted(overlay_roots):
            if not any(f"{directory}/{name}" in files for name in _KUSTOMIZATION_FILES):
                findings.append(_finding(f"overlay-no-kustomization:{directory}", "minor", f"{directory} has no kustomization file"))
        for prefix, envs in sorted(overlay_dirs.items()):
            base = f"{prefix}/base/" if prefix else "base/"
            if not any(p.startswith(base) for p in files):
                findings.append(_finding(f"overlay-without-base:{prefix or '.'}", "minor", f"{prefix or '.'} has overlays but no base/ directory"))
            for env in expected:
                if env not in envs:
                    findings.append(_finding(f"app-env-gap:{prefix or '.'}:{env}", "minor", f"{prefix or '.'} has no overlay for {env}"))
        cluster_dirs = sorted({p.split("/")[1] for p in files if p.startswith("clusters/") and p.count("/") >= 2})
        for cluster in sorted(set(payload["clusters"])):
            if cluster not in cluster_dirs:
                findings.append(_finding(f"cluster-dir-missing:{cluster}", "major", f"registered cluster {cluster} has no clusters/{cluster}/ directory"))
            elif payload["engine"] == "flux":
                for name in ("gotk-sync.yaml", "gotk-components.yaml"):
                    if f"clusters/{cluster}/flux-system/{name}" not in files:
                        findings.append(_finding(f"flux-not-bootstrapped:{cluster}:{name}", "major", f"clusters/{cluster}/flux-system/{name} is missing"))
        for cluster in cluster_dirs:
            if cluster not in payload["clusters"]:
                findings.append(_finding(f"cluster-dir-unregistered:{cluster}", "minor", f"clusters/{cluster}/ exists but {cluster} is not a registered cluster"))
        for path in sorted(files):
            name = path.rsplit("/", 1)[-1]
            shown = _redact(path)
            stem = name.split(".", 1)[0]
            if name in _PRIVATE_FILE_NAMES or name.endswith(_PRIVATE_FILE_SUFFIXES) or (stem in _PRIVATE_FILE_STEMS and not name.endswith(".pub")):
                findings.append(_finding(f"secret-like-file:{shown}", "major", f"{shown} looks like a private key or credential file"))
            elif name.endswith(".pem"):
                findings.append(_finding(f"pem-file:{shown}", "minor", f"{shown} is a PEM file; make sure it holds no private key"))
        file_present = [p for p in _CODEOWNERS_LOCATIONS if p in files]
        text = payload.get("codeowners")
        if not file_present and text is None:
            findings.append(_finding("codeowners-missing", "major", "no CODEOWNERS file at .github/, the root, docs/ or .gitlab/"))
        evaluated = text is not None
        if text is not None:
            rules = _codeowners_rules(text, findings)
            for env in production:
                env_files = sorted(set(env_paths.get(env, [])))
                unowned = [p for p in env_files if not _owners_of(rules, p)]
                if unowned:
                    findings.append(_finding(f"prod-env-unowned:{env}", "major",
                                             f"CODEOWNERS assigns no owner to {len(unowned)} of {len(env_files)} path(s) of production environment {env}, for example "
                                             f"{_redact(unowned[0])}, so those changes need no review from owners"))
            cluster_files = sorted(p for p in files if p.startswith("clusters/"))
            unowned = [p for p in cluster_files if not _owners_of(rules, p)]
            if unowned:
                findings.append(_finding("clusters-unowned", "minor",
                                         f"CODEOWNERS assigns no owner to {len(unowned)} of {len(cluster_files)} path(s) under clusters/, for example {_redact(unowned[0])}"))
        listed_envs, listed_clusters = [_scrub(e) for e in found[:MAX_LISTED]], [_scrub(c) for c in cluster_dirs[:MAX_LISTED]]
        if len(found) > MAX_LISTED or len(cluster_dirs) > MAX_LISTED:
            findings.append(_finding("lists-truncated", "info", f"{len(found)} environments and {len(cluster_dirs)} cluster directories found; "
                                                              f"envs_found and cluster_dirs list the first {MAX_LISTED} of each (sorted)"))
        return {"state": "SUCCESS", "path_count": len(files), "envs_found": listed_envs, "missing_envs": [_scrub(e) for e in sorted(missing)],
                "cluster_dirs": listed_clusters, "codeowners_evaluated": evaluated, **_report(findings)}

    result: dict[str, Any] = respond(payload, work)
    return result
