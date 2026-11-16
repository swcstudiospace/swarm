"""Tests of scripts/cloud_readiness_scan.py (A16): fixture trees in tmp_path, findings, exit codes, JSON shape, bounds and the dry-run contract.

The scan is run as a child process for the CLI contract (exit codes, stdout is one JSON document) and in-process
through the script's `run` for the finding logic, so the PyYAML-missing path can be forced by clearing the module's
`yaml` attribute.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cloud_readiness_scan.py"
TWIN = ROOT / "scripts" / "ts" / "cloud_readiness_scan.ts"
BUN = shutil.which("bun") or "/root/.local/share/reflex/bun/bin/bun"

DEPLOYMENT_GOOD = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  labels:
    app: api
spec:
  replicas: 3
  template:
    spec:
      terminationGracePeriodSeconds: 45
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
      containers:
        - name: api
          image: example/api:1.0.0
          readinessProbe:
            httpGet: {path: /ready, port: 8080}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
"""
HPA = "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: api\nspec:\n  maxReplicas: 6\n"
TF_GOOD = """\
resource "aws_db_instance" "db" {
  instance_class          = "db.t4g.medium"
  multi_az                = true
  storage_encrypted       = true
  backup_retention_period = 7
  tags = {
    owner       = "payments"
    environment = "prod"
    cost-center = "cc-42"
  }
}
"""
DOCKERFILE_GOOD = "FROM python:3.12-slim\nCOPY . /app\nHEALTHCHECK CMD curl -f http://localhost:8080/health\nENTRYPOINT [\"python\", \"/app/app.py\"]\n"
APP_GOOD = "import os, signal\nPORT = int(os.environ['PORT'])\nsignal.signal(signal.SIGTERM, lambda *a: None)\n"


def write(base: Path, rel: str, text: str = "") -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def good_repo(base: Path) -> Path:
    write(base, ".github/workflows/ci.yml", "name: ci\non: push\n")
    write(base, "app.py", APP_GOOD)
    write(base, "Dockerfile", DOCKERFILE_GOOD)
    write(base, "main.tf", TF_GOOD)
    write(base, "k8s/deployment.yaml", DEPLOYMENT_GOOD)
    write(base, "k8s/hpa.yaml", HPA)
    return base


def load_script():
    spec = importlib.util.spec_from_file_location("_cloud_readiness_scan_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan(base: Path, path: str = ".", module=None) -> dict:
    module = module or load_script()
    return module.run(argparse.Namespace(path=path), SimpleNamespace(dry_run=False, root=base))


def brief(result: dict) -> list[tuple]:
    return [(f["id"], f["severity"], f.get("location", "")) for f in result["findings"]]


def cli(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SWARM_DIR": str(tmp_path / ".swarm-state"),
           "SWARM_PURE_PYTHON": "1"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=ROOT)


# --- dry run and CLI contract -----------------------------------------------------------------------------------


def test_dry_run_prints_one_json_document_without_reading_the_root(tmp_path):
    result = cli("--dry-run", "--json", "--root", str(tmp_path / "does-not-exist"), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["agent"] == "A16" and data["script"] == "cloud_readiness_scan" and data["status"] == "ok"
    assert data["dry_run"] is True and data["findings"] == [] and data["pillars"] == [] and data["files_scanned"] == 0
    assert not (tmp_path / "does-not-exist").exists()  # the root was neither read nor created


def test_dry_run_output_is_deterministic(tmp_path):
    first = cli("--dry-run", "--json", tmp_path=tmp_path).stdout
    assert first == cli("--dry-run", "--json", tmp_path=tmp_path).stdout


def test_real_run_json_shape_and_exit_code_zero_on_a_ready_repo(tmp_path):
    pytest.importorskip("yaml")  # the ready repository's Kubernetes manifests are only judged when PyYAML is present
    repo = good_repo(tmp_path / "repo")
    result = cli("--json", "--root", str(repo), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["agent"] == "A16" and data["script"] == "cloud_readiness_scan" and data["status"] == "ok"
    assert set(data) >= {"summary", "files_scanned", "files_skipped", "truncated", "signals", "pillars", "overall_score_pct", "skipped", "findings"}


def test_cli_exits_1_when_a_major_finding_exists_and_2_for_bad_paths(tmp_path):
    repo = tmp_path / "repo"
    write(repo, ".env", "TOKEN=abc\n")
    failing = cli("--json", "--root", str(repo), tmp_path=tmp_path)
    assert failing.returncode == 1 and json.loads(failing.stdout)["status"] == "fail"
    escaping = cli("--json", "--root", str(repo), "--path", "..", tmp_path=tmp_path)
    assert escaping.returncode == 2 and json.loads(escaping.stdout)["error"]["code"] == "E-INPUT"
    missing = cli("--json", "--root", str(repo), "--path", "nope", tmp_path=tmp_path)
    assert missing.returncode == 2 and json.loads(missing.stdout)["status"] == "error"


@pytest.mark.skipif(not Path(BUN).exists(), reason="bun not installed")
def test_bun_twin_dry_run_matches_the_python_script(tmp_path):
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm-state")}
    twin = subprocess.run([BUN, str(TWIN), "--dry-run", "--json"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert twin.returncode == 0, twin.stderr
    assert json.loads(twin.stdout) == json.loads(cli("--dry-run", "--json", tmp_path=tmp_path).stdout)


# --- finding logic ----------------------------------------------------------------------------------------------


def test_ready_repository_has_no_findings_and_scores_100_in_every_pillar(tmp_path):
    pytest.importorskip("yaml")
    result = scan(good_repo(tmp_path))
    assert result["findings"] == [] and result["status"] == "ok" and result["skipped"] == []
    assert result["overall_score_pct"] == 100.0 and [p["score_pct"] for p in result["pillars"]] == [100.0] * 6
    assert [p["pillar"] for p in result["pillars"]] == ["operational_excellence", "security", "reliability", "performance_efficiency",
                                                       "cost_optimization", "sustainability"]
    assert result["signals"] == {"ci_files": 1, "dockerfiles": 1, "kubernetes_workloads": 1, "iac_files": 1, "source_files": 1}
    assert result["files_scanned"] == 6 and result["files_skipped"] == 0 and result["truncated"] is False


def test_empty_repository_is_incomplete_and_fails_and_scores_each_pillar_from_its_always_applicable_question(tmp_path):
    result = scan(tmp_path)
    assert result["status"] == "fail" and "no in-scope input was analysed" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert brief(result) == [
        ("scan/incomplete", "major", ""),
        ("cloud.autoscaling-missing", "minor", ""), ("cloud.ci-pipeline-missing", "minor", ""), ("cloud.efficiency-signals-missing", "minor", ""),
        ("cloud.health-probe-missing", "minor", ""), ("cloud.tags-missing", "minor", "")]
    assert {p["pillar"]: p["score_pct"] for p in result["pillars"]} == {
        "operational_excellence": 50.0, "security": 100.0, "reliability": 50.0, "performance_efficiency": 50.0,
        "cost_optimization": 50.0, "sustainability": 50.0}
    assert result["overall_score_pct"] == 58.33


def test_unready_repository_findings_severities_locations_and_pillar_scores(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, ".env", "DB_PASSWORD=hunter2\n")
    write(tmp_path, ".env.example", "DB_PASSWORD=changeme\n")
    write(tmp_path, "Dockerfile", "FROM python:3.12\nCMD python app.py\n")
    write(tmp_path, "app.py", "print('hi')\n")
    write(tmp_path, "k8s/deploy.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 1\n"
                                      "  template:\n    spec:\n      containers:\n        - name: web\n          image: web:1\n")
    write(tmp_path, "infra/main.tf", 'resource "aws_instance" "web" {}\n')
    write(tmp_path, "infra/db.tf", 'resource "aws_db_instance" "d" {\n  multi_az = false\n}\n')
    result = scan(tmp_path)
    assert result["status"] == "fail"
    assert brief(result) == [
        ("cloud.env-file-committed", "major", ".env"), ("cloud.k8s-probes-missing", "major", "k8s/deploy.yaml"),
        ("cloud.multi-az-disabled", "major", "infra/db.tf"), ("cloud.multi-az-missing", "major", "infra/db.tf"),
        ("cloud.autoscaling-missing", "minor", ""), ("cloud.backup-not-declared", "minor", "infra/db.tf"),
        ("cloud.ci-pipeline-missing", "minor", ""), ("cloud.config-not-from-environment", "minor", ""),
        ("cloud.docker-shell-form-entrypoint", "minor", "Dockerfile"), ("cloud.efficiency-signals-missing", "minor", ""),
        ("cloud.encryption-not-declared", "minor", "infra/db.tf"), ("cloud.graceful-shutdown-missing", "minor", ""),
        ("cloud.k8s-no-resource-requests", "minor", "k8s/deploy.yaml"), ("cloud.k8s-single-replica", "minor", "k8s/deploy.yaml"),
        ("cloud.tags-missing", "minor", "infra/db.tf")]
    scores = {p["pillar"]: p["score_pct"] for p in result["pillars"]}
    # security: env-file high (5) + encryption medium (3): 100 * (1 - 6.5/8); reliability: 13 of 17 weight at risk
    assert scores["security"] == 18.75 and scores["reliability"] == 23.53 and scores["operational_excellence"] == 50.0
    assert result["overall_score_pct"] == 40.38


def test_env_file_variants_only_values_count_and_templates_are_ignored(tmp_path):
    write(tmp_path, ".env", "# comment only\nEMPTY=\n")
    write(tmp_path, ".env.production", "export API_KEY=abc\n")
    write(tmp_path, ".env.sample", "API_KEY=abc\n")
    write(tmp_path, ".env.template", "API_KEY=abc\n")
    result = scan(tmp_path)
    assert [(f["id"], f.get("location")) for f in result["findings"] if f["id"] == "cloud.env-file-committed"] == [("cloud.env-file-committed", ".env.production")]


def test_sources_without_env_reads_are_flagged_and_test_files_do_not_count_as_sources(tmp_path):
    write(tmp_path, "tests/test_x.py", "import os\nos.environ['A']\n")
    write(tmp_path, "src/service.py", "print('x')\n")
    result = scan(tmp_path)
    assert "cloud.config-not-from-environment" in [f["id"] for f in result["findings"]]
    assert result["signals"]["source_files"] == 1
    write(tmp_path, "src/service.go", 'package main\nfunc main() { _ = os.Getenv("PORT") }\n')
    assert "cloud.config-not-from-environment" not in [f["id"] for f in scan(tmp_path)["findings"]]


@pytest.mark.parametrize("name,text", [
    ("app.py", "import signal\nsignal.signal(signal.SIGTERM, h)\n"),
    ("main.go", "signal.Notify(c, syscall.SIGTERM)\n"),
    ("server.js", "process.on('SIGTERM', stop)\n"),
    ("App.java", "Runtime.getRuntime().addShutdownHook(t);\n"),
])
def test_graceful_shutdown_signals_in_source_clear_the_finding(tmp_path, name, text):
    write(tmp_path, name, text)
    assert "cloud.graceful-shutdown-missing" not in [f["id"] for f in scan(tmp_path)["findings"]]
    write(tmp_path, name, "print('no handler')\n")
    assert "cloud.graceful-shutdown-missing" in [f["id"] for f in scan(tmp_path)["findings"]]


def test_dockerfile_only_the_final_stage_counts_and_none_disables_the_healthcheck(tmp_path):
    write(tmp_path, "Dockerfile", "FROM golang AS build\nHEALTHCHECK CMD true\nFROM scratch\nCOPY --from=build /app /app\nENTRYPOINT [\"/app\"]\n")
    assert "cloud.health-probe-missing" in [f["id"] for f in scan(tmp_path)["findings"]]
    write(tmp_path, "Dockerfile", "FROM alpine\nHEALTHCHECK NONE\nCMD [\"/app\"]\n")
    assert "cloud.health-probe-missing" in [f["id"] for f in scan(tmp_path)["findings"]]
    write(tmp_path, "Dockerfile", "FROM alpine\nHEALTHCHECK --interval=30s \\\n  CMD wget -q http://localhost/ || exit 1\nCMD [\"/app\"]\n")
    assert "cloud.health-probe-missing" not in [f["id"] for f in scan(tmp_path)["findings"]]


def test_dockerfile_shell_form_launcher_is_flagged_unless_it_execs(tmp_path):
    write(tmp_path, "Dockerfile", "FROM alpine\nENTRYPOINT /app --serve\n")
    flagged = [f for f in scan(tmp_path)["findings"] if f["id"] == "cloud.docker-shell-form-entrypoint"]
    assert [(f["severity"], f["location"]) for f in flagged] == [("minor", "Dockerfile")] and flagged[0]["summary"].startswith("ENTRYPOINT runs through")
    write(tmp_path, "Dockerfile", "FROM alpine\nCMD exec /app --serve\n")
    assert "cloud.docker-shell-form-entrypoint" not in [f["id"] for f in scan(tmp_path)["findings"]]
    write(tmp_path, "Dockerfile.prod", "FROM alpine\nENTRYPOINT [\"/app\"]\nCMD /bin/echo\n")  # exec-form ENTRYPOINT launches the process
    assert "cloud.docker-shell-form-entrypoint" not in [f["id"] for f in scan(tmp_path)["findings"]]


def test_compose_healthcheck_counts_as_a_health_signal(tmp_path):
    write(tmp_path, "docker-compose.yml", "services:\n  web:\n    image: web\n    healthcheck:\n      test: [CMD, curl, -f, http://localhost]\n")
    assert "cloud.health-probe-missing" not in [f["id"] for f in scan(tmp_path)["findings"]]


@pytest.mark.parametrize("text,present", [
    ('resource "aws_instance" "a" {}\nvariable "azs" { default = ["a", "b"] }\nlocals { availability_zones = ["eu-1a", "eu-1b"] }\n', True),
    ('resource "aws_instance" "a" {}\nlocals { availability_zones = ["eu-1a"] }\n', False),
    ('resource "aws_db_instance" "a" {\n  multi_az = true\n}\n', True),
    ('resource "azurerm_postgresql_flexible_server" "a" {\n  zone_redundant = true\n}\n', True),
    ('resource "aws_instance" "a" {}\n', False),
])
def test_multi_az_declarations_in_terraform(tmp_path, text, present):
    write(tmp_path, "main.tf", text)
    ids = [f["id"] for f in scan(tmp_path)["findings"]]
    assert ("cloud.multi-az-missing" not in ids) is present


def test_cloudformation_multi_az_and_backup_are_read_from_text(tmp_path):
    write(tmp_path, "db.yaml", "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Db:\n    Type: AWS::RDS::DBInstance\n"
                               "    Properties:\n      MultiAZ: true\n      BackupRetentionPeriod: 7\n      StorageEncrypted: true\n")
    ids = [f["id"] for f in scan(tmp_path)["findings"]]
    assert not {"cloud.multi-az-missing", "cloud.multi-az-disabled", "cloud.backup-not-declared", "cloud.encryption-not-declared"} & set(ids)
    write(tmp_path, "db.yaml", "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Db:\n    Type: AWS::RDS::DBInstance\n    Properties:\n      MultiAZ: false\n"
                               "      BackupRetentionPeriod: 0\n")
    result = scan(tmp_path)
    assert {("cloud.multi-az-disabled", "major"), ("cloud.backup-not-declared", "minor"), ("cloud.encryption-not-declared", "minor")} <= {
        (f["id"], f["severity"]) for f in result["findings"]}


def test_iac_without_compute_or_data_resources_is_not_judged_for_multi_az(tmp_path):
    write(tmp_path, "iam.tf", 'resource "aws_iam_role" "r" {}\n')
    assert not {"cloud.multi-az-missing", "cloud.backup-not-declared"} & {f["id"] for f in scan(tmp_path)["findings"]}


def test_tag_keys_must_cover_owner_environment_and_cost_center(tmp_path):
    write(tmp_path, "main.tf", 'resource "aws_instance" "a" {\n  multi_az = true\n  tags = {\n    Owner = "x"\n  }\n}\n')
    finding = next(f for f in scan(tmp_path)["findings"] if f["id"] == "cloud.tag-keys-missing")
    assert finding["summary"] == "allocation tag keys not found in IaC: cost-center, environment" and finding["location"] == "main.tf"
    write(tmp_path, "stack.yaml", "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  A:\n    Type: AWS::EC2::Instance\n    Properties:\n"
                                  "      Tags:\n        - Key: Environment\n          Value: prod\n        - Key: cost_center\n          Value: c1\n")
    assert "cloud.tag-keys-missing" not in [f["id"] for f in scan(tmp_path)["findings"]]


def test_kubernetes_findings_per_workload_zone_spread_and_daemonset_exemptions(tmp_path):
    pytest.importorskip("yaml")
    spec = ("apiVersion: apps/v1\nkind: {kind}\nmetadata:\n  name: {name}\nspec:\n{replicas}  template:\n    spec:\n      containers:\n"
            "        - name: c\n          image: i\n          livenessProbe: {{httpGet: {{path: /, port: 80}}}}\n          resources:\n            requests: {{cpu: 1m}}\n")
    write(tmp_path, "a.yaml", spec.format(kind="Deployment", name="three", replicas="  replicas: 3\n"))
    write(tmp_path, "b.yaml", spec.format(kind="StatefulSet", name="unset", replicas=""))
    write(tmp_path, "c.yaml", spec.format(kind="DaemonSet", name="agent", replicas=""))
    write(tmp_path, "d.yaml", spec.format(kind="StatefulSet", name="solo", replicas="  replicas: 1\n"))
    result = scan(tmp_path)
    kube = [(f["id"], f["location"]) for f in result["findings"] if "k8s" in f["id"]]
    # an unset StatefulSet replicas field defaults to one replica (the apps/v1 API default), so b.yaml is a single-replica workload
    assert kube == [("cloud.k8s-no-zone-spread", "a.yaml"), ("cloud.k8s-single-replica", "b.yaml"),
                    ("cloud.k8s-single-replica", "d.yaml")]
    assert result["signals"]["kubernetes_workloads"] == 4


def test_kubernetes_list_kind_and_multi_document_files_are_flattened(tmp_path):
    pytest.importorskip("yaml")
    body = "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: %s}\nspec:\n  template:\n    spec:\n      containers: [{name: c}]\n"
    write(tmp_path, "multi.yaml", body % "one" + "---\n" + body % "two")
    write(tmp_path, "list.json", json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "three"}, "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}}]}))
    result = scan(tmp_path)
    assert result["signals"]["kubernetes_workloads"] == 3
    assert [f["location"] for f in result["findings"] if f["id"] == "cloud.k8s-probes-missing"] == ["list.json", "multi.yaml", "multi.yaml"]


def test_helm_templates_are_judged_from_text_without_a_yaml_parser(tmp_path):
    module = load_script()
    module.yaml = None
    write(tmp_path, "chart/templates/deployment.yaml",
          "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\nspec:\n  replicas: 1\n  template:\n    spec:\n"
          "      containers:\n        - name: app\n          image: {{ .Values.image }}\n")
    result = scan(tmp_path, module=module)
    assert ("cloud.k8s-probes-missing", "major", "chart/templates/deployment.yaml") in brief(result)
    assert "cloud.yaml-checks-skipped" not in [f["id"] for f in result["findings"]] and result["skipped"] == []
    write(tmp_path, "chart/templates/deployment.yaml",
          "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: {{ .Values.replicas }}\n  template:\n    spec:\n      terminationGracePeriodSeconds: 30\n"
          "      containers:\n        - name: app\n          readinessProbe: {}\n          resources:\n            requests: {cpu: 1m}\n")
    ids = [f["id"] for f in scan(tmp_path, module=module)["findings"]]
    assert not {"cloud.k8s-probes-missing", "cloud.k8s-no-resource-requests", "cloud.k8s-single-replica", "cloud.graceful-shutdown-missing"} & set(ids)


def test_missing_pyyaml_reports_skipped_tool_missing_and_keeps_json_manifests(tmp_path):
    module = load_script()
    module.yaml = None
    write(tmp_path, "k8s/deploy.yaml", DEPLOYMENT_GOOD)
    write(tmp_path, "k8s/other.yml", HPA)
    write(tmp_path, "k8s/bad.json", json.dumps({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "j"},
                                               "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}}))
    result = scan(tmp_path, module=module)
    assert result["skipped"] == ["skipped:tool-missing: PyYAML (Kubernetes YAML manifests not analysed)"]
    incomplete = next(f for f in result["findings"] if f["id"] == "scan/incomplete")
    assert incomplete["severity"] == "major" and "k8s/deploy.yaml" in incomplete["summary"] and "k8s/other.yml" in incomplete["summary"]
    assert "cloud.yaml-checks-skipped" not in [f["id"] for f in result["findings"]]
    assert ("cloud.k8s-probes-missing", "major", "k8s/bad.json") in brief(result)  # JSON is parsed natively
    assert result["signals"]["kubernetes_workloads"] == 1 and result["status"] == "fail"


def test_unparseable_manifests_are_incomplete_reasons_and_fail_the_scan(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "broken.yaml", "apiVersion: v1\nkind: Deployment\n  bad: [unclosed\n")
    write(tmp_path, "broken.json", '{"apiVersion": "v1", "kind": ')
    result = scan(tmp_path)
    incomplete = next(f for f in result["findings"] if f["id"] == "scan/incomplete")
    assert incomplete["severity"] == "major" and result["status"] == "fail"
    assert "skipped:unparseable-manifest: broken.json" in incomplete["summary"]
    assert "skipped:unparseable-manifest: broken.yaml" in incomplete["summary"]
    assert "cloud.manifest-unparseable" not in [f["id"] for f in result["findings"]]


def test_a_yaml_manifest_nested_past_the_parser_limit_is_unparseable_and_the_good_file_is_still_judged(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/deep.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: deep}\nspec: {junk: " + "[" * 600 + "]" * 600 + "}\n")
    write(tmp_path, "k8s/good.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: bare}\nspec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n")
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 1
    summary = next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert "skipped:unparseable-manifest: k8s/deep.yaml" in summary
    assert ("cloud.k8s-probes-missing", "major", "k8s/good.yaml") in brief(result)


# --- bounds -----------------------------------------------------------------------------------------------------


def test_oversized_and_binary_files_are_skipped_and_counted(tmp_path):
    write(tmp_path, "big.py", "import os\nos.environ\n" + "#" * (1024 * 1024))
    (tmp_path / "blob.py").write_bytes(b"\x00\x01os.environ")
    write(tmp_path, "ok.py", "import os\nos.environ\n")
    result = scan(tmp_path)
    assert result["files_scanned"] == 1 and result["files_skipped"] == 2 and result["signals"]["source_files"] == 1 and result["status"] == "fail"
    summary = next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert "skipped:too-large: big.py" in summary and "skipped:binary: blob.py" in summary


def test_a_file_of_exactly_one_mebibyte_is_still_read(tmp_path):
    text = "import os\nos.environ\n"
    write(tmp_path, "edge.py", text + "#" * (1024 * 1024 - len(text)))
    assert (tmp_path / "edge.py").stat().st_size == 1024 * 1024
    assert scan(tmp_path)["files_scanned"] == 1


def test_symlinks_are_never_followed_out_of_the_root(tmp_path):
    outside = tmp_path / "outside"
    write(outside, ".env", "SECRET=1\n")
    write(outside, "leak.py", "import os\nos.environ\n")
    repo = tmp_path / "repo"
    write(repo, "app.py", "print('x')\n")
    (repo / "linked-dir").symlink_to(outside, target_is_directory=True)
    (repo / "linked.py").symlink_to(outside / "leak.py")
    (repo / ".env").symlink_to(outside / ".env")
    result = scan(repo)
    assert "cloud.env-file-committed" not in [f["id"] for f in result["findings"]]
    assert result["signals"]["source_files"] == 1 and result["files_skipped"] >= 2 and result["status"] == "fail"
    summary = next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert "skipped:symlink-directory: linked-dir" in summary and "skipped:symlink: linked.py" in summary and "skipped:symlink: .env" in summary


def test_scan_stops_after_2000_files_and_says_so(tmp_path):
    for i in range(2000):
        (tmp_path / f"f{i:04d}.py").write_text("print(1)\n")
    exact = scan(tmp_path)
    assert exact["status"] == "ok" and exact["truncated"] is False and "skipped:file-limit" not in json.dumps(exact["findings"])
    (tmp_path / "f2000.py").write_text("print(1)\n")
    result = scan(tmp_path)
    assert result["truncated"] is True and result["status"] == "fail"
    assert "skipped:file-limit" in next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_skipped_directories_are_not_scanned(tmp_path):
    write(tmp_path, "node_modules/pkg/.env", "A=1\n")
    write(tmp_path, ".git/.env", "A=1\n")
    write(tmp_path, "vendor/x/main.tf", 'resource "aws_instance" "a" {}\n')
    assert scan(tmp_path)["files_scanned"] == 0


def test_path_narrows_the_scan_to_a_subdirectory_or_one_file(tmp_path):
    write(tmp_path, "a/.env", "A=1\n")
    write(tmp_path, "b/app.py", "print(1)\n")
    assert [f["location"] for f in scan(tmp_path, "a")["findings"] if f["id"] == "cloud.env-file-committed"] == ["a/.env"]
    assert "cloud.env-file-committed" not in [f["id"] for f in scan(tmp_path, "b")["findings"]]
    assert scan(tmp_path, "b/app.py")["signals"]["source_files"] == 1


def test_run_refuses_paths_outside_or_missing_under_the_root(tmp_path):
    module = load_script()
    from swarm.errors import SwarmError
    with pytest.raises(SwarmError):
        scan(tmp_path, "../elsewhere", module)
    with pytest.raises(SwarmError):
        scan(tmp_path, "missing", module)


# --- secret safety ----------------------------------------------------------------------------------------------

SECRETS = ("AKIAIOSFODNN7EXAMPLE", "ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
           "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about", "hunter2pass")


def test_no_scanned_value_or_credential_shaped_name_reaches_the_output(tmp_path):
    pytest.importorskip("yaml")
    repo = tmp_path / "repo"
    write(repo, ".env", f"AWS_ACCESS_KEY_ID={SECRETS[0]}\nGITHUB_TOKEN={SECRETS[1]}\nPRIVATE_KEY={SECRETS[2]}\nMNEMONIC={SECRETS[3]}\n")
    write(repo, "app.py", f"TOKEN = '{SECRETS[1]}'\nimport os\nos.environ\n")
    write(repo, "Dockerfile", f"FROM python:3.12\nENV DB_URL=https://user:{SECRETS[4]}@db.example/x\nCMD run.sh\n")
    write(repo, "main.tf", f'resource "aws_db_instance" "db" {{\n  password = "{SECRETS[4]}"\n  username = "{SECRETS[0]}"\n}}\n')
    write(repo, "k8s/d.yaml", f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {SECRETS[1]}\nspec:\n  template:\n    spec:\n      containers:\n"
                              f"        - name: {SECRETS[0]}\n          env:\n            - {{name: T, value: {SECRETS[2]}}}\n")
    write(repo, f".env.{SECRETS[2]}", "A=1\n")
    for text in (json.dumps(scan(repo)), cli("--json", "--root", str(repo), tmp_path=tmp_path).stdout):
        assert text and not any(secret in text for secret in SECRETS)
    names = [f["summary"] for f in scan(repo)["findings"] if f["id"] == "cloud.k8s-probes-missing"]
    assert names and names[0].startswith("Deployment [redacted:credential:40:")


def test_a_path_argument_is_never_echoed_in_the_error(tmp_path):
    module = load_script()
    from swarm.errors import SwarmError
    for bad in ("https://user:hunter2pass@host.example/x", "../hunter2pass"):
        with pytest.raises(SwarmError) as caught:
            scan(tmp_path, bad, module)
        assert "hunter2pass" not in str(caught.value) and "hunter2pass" not in json.dumps(getattr(caught.value, "details", {}))
    out = cli("--json", "--root", str(tmp_path), "--path", "../hunter2pass", tmp_path=tmp_path)
    assert out.returncode == 2 and "hunter2pass" not in out.stdout + out.stderr


def test_env_file_finding_does_not_claim_the_file_is_tracked(tmp_path):
    write(tmp_path, ".env", "TOKEN=abc\n")
    finding = next(f for f in scan(tmp_path)["findings"] if f["id"] == "cloud.env-file-committed")
    assert finding["severity"] == "major" and finding["location"] == ".env"
    assert "committed unless version control ignores it" in finding["summary"]


def test_tab_separated_healthcheck_and_exec_form_are_read_like_the_space_form():
    module = load_script()
    assert module._dockerfile_facts("FROM a\nHEALTHCHECK\tCMD true\n")["healthcheck"] is True
    assert module._dockerfile_facts("FROM a\nHEALTHCHECK\tNONE\n")["healthcheck"] is False
    assert module._dockerfile_facts("FROM a\nCMD\tnode app.js\n") == {"healthcheck": False, "shell_form": True, "launcher": "CMD"}


def test_string_typed_workload_metadata_is_skipped_and_counted_and_the_scan_completes(tmp_path):
    pytest.importorskip("yaml")
    repo = tmp_path / "repo"
    write(repo, "k8s/broken.json", json.dumps({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": "a", "spec": {"replicas": 2}}))
    write(repo, "k8s/deploy.yaml", DEPLOYMENT_GOOD)
    result = cli("--json", "--root", str(repo), tmp_path=tmp_path)
    assert result.returncode in (0, 1), result.stderr  # the scan completes; 2 would mean it aborted
    data = json.loads(result.stdout)
    assert data["signals"]["kubernetes_workloads"] == 1
    assert "skipped:malformed-workload: k8s/broken.json" in next(f for f in data["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert data["status"] == "fail"


# --- QA-03 regressions ------------------------------------------------------------------------------------------


def test_unset_replicas_default_to_one_so_a_single_replica_workload_is_flagged(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/web.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n"
                                    "      containers:\n        - name: web\n          readinessProbe: {httpGet: {path: /, port: 80}}\n"
                                    "          resources:\n            requests: {cpu: 1m}\n")
    result = scan(tmp_path)
    assert [(f["id"], f["location"]) for f in result["findings"] if "k8s-single" in f["id"]] == [("cloud.k8s-single-replica", "k8s/web.yaml")]
    assert result["signals"]["kubernetes_workloads"] == 1


def test_unparsed_manifest_makes_absence_rules_unknown_not_missing(tmp_path):
    module = load_script()
    module.yaml = None
    write(tmp_path, "app.py", "print('no signals')\n")
    write(tmp_path, "k8s/deploy.yaml", DEPLOYMENT_GOOD)  # may hold the probes, grace period, labels and autoscaler the scan cannot read
    result = scan(tmp_path, module=module)
    ids = {f["id"] for f in result["findings"]}
    assert {"cloud.health-probe-unknown", "cloud.autoscaling-unknown", "cloud.tags-unknown", "cloud.graceful-shutdown-unknown"} <= ids
    assert not {"cloud.health-probe-missing", "cloud.autoscaling-missing", "cloud.tags-missing", "cloud.graceful-shutdown-missing"} & ids
    unknown = next(f for f in result["findings"] if f["id"] == "cloud.health-probe-unknown")
    assert unknown["severity"] == "minor" and unknown["summary"].startswith("unknown, not missing: no health check signal found")
    assert unknown["summary"].endswith("Kubernetes YAML was not parsed (PyYAML missing)")


def test_a_tree_with_no_in_scope_input_fails_as_incomplete(tmp_path):
    write(tmp_path, "README.md", "# notes\n")
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["files_scanned"] == 0
    assert [f["id"] for f in result["findings"] if f["severity"] == "major"] == ["scan/incomplete"]


def test_unreadable_subdirectory_is_an_incomplete_reason_not_a_silent_drop(tmp_path, monkeypatch):
    write(tmp_path, "app.py", "import os\nos.environ\n")
    write(tmp_path, "locked/.env", "TOKEN=abc\n")
    real_scandir = os.scandir

    def scandir_denying_locked(path="."):
        if os.fspath(path).endswith("locked"):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir_denying_locked)
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["files_scanned"] == 1
    assert "skipped:unreadable-directory: locked" in next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert "cloud.env-file-committed" not in [f["id"] for f in result["findings"]]


def test_helm_template_replicas_and_spread_are_judged_per_kind_not_from_the_first_kind(tmp_path):
    body = ("apiVersion: apps/v1\nkind: {kind}\nmetadata:\n  name: {name}\nspec:\n  replicas: {replicas}\n  template:\n    spec:\n"
            "      containers:\n        - name: app\n          image: {{{{ .Values.image }}}}\n          readinessProbe: {{}}\n"
            "          resources:\n            requests: {{cpu: 1m}}\n")
    write(tmp_path, "chart/templates/all.yaml", body.format(kind="Deployment", name="web", replicas=1)
          + "---\n" + body.format(kind="StatefulSet", name="db", replicas=3))
    result = scan(tmp_path)
    assert [f["summary"] for f in result["findings"] if f["id"] == "cloud.k8s-single-replica"] == [
        "Deployment (helm template) runs a single replica"]
    assert [f["summary"] for f in result["findings"] if f["id"] == "cloud.k8s-no-zone-spread"] == [
        "StatefulSet (helm template) has 3 replicas but no topology spread or pod anti-affinity"]


def test_yaml_top_level_sequence_of_objects_is_an_unparseable_reason_not_a_silent_drop(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "seq.yaml", "- apiVersion: apps/v1\n  kind: Deployment\n  metadata: {name: a}\n"
                                "  spec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n")
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 0
    assert "skipped:unparseable-manifest: seq.yaml" in next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_json_top_level_array_of_objects_is_an_unparseable_reason_not_a_silent_drop(tmp_path):
    write(tmp_path, "arr.json", json.dumps([{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "a"},
                                            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "c"}]}}}}]))
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 0
    assert "skipped:unparseable-manifest: arr.json" in next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_workload_without_a_spec_mapping_is_a_malformed_reason_naming_path_and_kind(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "nospec.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: nospec\n")
    write(tmp_path, "nullspec.yaml", "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: nullspec\nspec:\n")
    result = scan(tmp_path)
    summary = next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 0
    assert "skipped:malformed-workload: nospec.yaml (Deployment)" in summary
    assert "skipped:malformed-workload: nullspec.yaml (StatefulSet)" in summary


# --- QA-03 round two: non-mapping YAML documents and List entries --------------------------------------------------------


def test_a_scalar_document_before_a_deployment_is_a_reason_naming_the_document_and_fails_the_scan(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/mixed.yaml", "just text\n---\n" + DEPLOYMENT_GOOD)
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 1
    assert "skipped:non-mapping-document: k8s/mixed.yaml (document 0)" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_a_yaml_file_of_only_scalars_is_not_kubernetes_and_stays_silent(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "notes.yaml", "# apiVersion and kind are only words in this comment\njust text\n---\n42\n")
    result = scan(tmp_path)
    assert result["status"] == "ok" and result["files_scanned"] == 1 and result["signals"]["kubernetes_workloads"] == 0
    assert "scan/incomplete" not in [f["id"] for f in result["findings"]]


def test_a_list_with_a_bare_string_item_is_a_reason_naming_the_list_and_the_item_index(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/list.yaml", "apiVersion: v1\nkind: List\nitems:\n  - just-a-string\n  - apiVersion: apps/v1\n"
                                     "    kind: Deployment\n    metadata: {name: ok}\n"
                                     "    spec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n")
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 1
    assert "skipped:non-mapping-document: k8s/list.yaml (document 0, List items[0])" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


# --- QA-03 round three: a List whose items is not a list --------------------------------------------------------------


def test_a_list_whose_items_is_a_mapping_is_a_reason_naming_the_list_items_and_fails(tmp_path):
    pytest.importorskip("yaml")
    bare = ("  apiVersion: apps/v1\n  kind: Deployment\n  metadata: {name: bare}\n"
            "  spec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n")
    write(tmp_path, "k8s/list.yaml", "apiVersion: v1\nkind: List\nitems:\n" + bare)
    write(tmp_path, "k8s/deploylist.yaml", "apiVersion: apps/v1\nkind: DeploymentList\nitems:\n" + bare)
    result = scan(tmp_path)
    summary = next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 0
    assert "skipped:non-list-items: k8s/list.yaml (document 0, List items)" in summary
    assert "skipped:non-list-items: k8s/deploylist.yaml (document 0, List items)" in summary


def test_a_list_whose_items_is_a_scalar_is_a_reason_and_fails(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/scalar.yaml", "apiVersion: v1\nkind: List\nitems: just-text\n")
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 0
    assert "skipped:non-list-items: k8s/scalar.yaml (document 0, List items)" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_a_valid_list_of_items_still_gets_per_item_treatment(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/list.yaml", "apiVersion: v1\nkind: List\nitems:\n"
          "  - apiVersion: apps/v1\n    kind: Deployment\n    metadata: {name: good}\n"
          "    spec: {replicas: 3, template: {spec: {terminationGracePeriodSeconds: 30, containers: [{name: c, "
          "readinessProbe: {httpGet: {path: /, port: 80}}, resources: {requests: {cpu: 1m}}}]}}}\n"
          "  - apiVersion: apps/v1\n    kind: Deployment\n    metadata: {name: bare}\n"
          "    spec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n")
    result = scan(tmp_path)
    assert result["signals"]["kubernetes_workloads"] == 2
    assert ("cloud.k8s-probes-missing", "major", "k8s/list.yaml") in brief(result)  # the bare item is judged on its own
    assert "scan/incomplete" not in [f["id"] for f in result["findings"]]  # every item is analysed, so no input is incomplete


# --- QA-03 round four: a stray items field does not change the kind of a document -----------------------------------------


def test_a_deployment_with_a_stray_items_list_is_still_judged_as_a_deployment(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/web.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web}\n"
                                    "spec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n"
                                    "items:\n  - apiVersion: v1\n    kind: Pod\n    metadata: {name: p}\n")
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 1
    assert ("cloud.k8s-probes-missing", "major", "k8s/web.yaml") in brief(result)


def test_a_kind_less_mapping_with_a_list_of_items_still_gets_per_item_treatment(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/kindless.yaml", "items:\n  - apiVersion: apps/v1\n    kind: Deployment\n    metadata: {name: bare}\n"
                                         "    spec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n")
    result = scan(tmp_path)
    assert result["signals"]["kubernetes_workloads"] == 1
    assert ("cloud.k8s-probes-missing", "major", "k8s/kindless.yaml") in brief(result)


# --- QA-03 round five: a custom resource whose kind ends in List is not a Kubernetes List --------------------------------------


def test_a_custom_todolist_resource_with_a_spec_and_no_items_is_ok_with_no_reason(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "crd/todo.yaml", "apiVersion: example.com/v1\nkind: TodoList\nmetadata: {name: t}\nspec: {owner: alice}\n")
    result = scan(tmp_path)
    assert result["status"] == "ok" and result["signals"]["kubernetes_workloads"] == 0
    assert "scan/incomplete" not in [f["id"] for f in result["findings"]]


def test_a_deployment_list_with_no_items_key_is_a_reason_and_fails_since_it_is_a_strict_kind(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/dl.yaml", "apiVersion: apps/v1\nkind: DeploymentList\nmetadata: {name: d}\n")
    result = scan(tmp_path)
    assert result["status"] == "fail"
    assert "skipped:non-list-items: k8s/dl.yaml (document 0, List items)" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_a_service_list_with_a_mapping_items_is_a_reason_since_its_items_key_is_present(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/sl.yaml", "apiVersion: v1\nkind: ServiceList\nitems:\n  key: value\n")
    result = scan(tmp_path)
    assert result["status"] == "fail"
    assert "skipped:non-list-items: k8s/sl.yaml (document 0, List items)" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


# --- QA-03 round six: a kind-less entry inside a List is reported, not dropped --------------------------------------------


KIND_LESS_ENTRY = "  - spec:\n      template:\n        spec:\n          containers:\n            - name: web\n"
VALID_ENTRY = ("  - apiVersion: apps/v1\n    kind: Deployment\n    metadata: {name: ok}\n"
               "    spec: {replicas: 3, template: {spec: {terminationGracePeriodSeconds: 30, containers: [{name: c, "
               "readinessProbe: {httpGet: {path: /, port: 80}}, resources: {requests: {cpu: 1m}}}]}}}\n")


def test_a_list_whose_only_item_is_kind_less_is_a_reason_and_fails_with_no_workloads(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/list.yaml", "apiVersion: v1\nkind: List\nitems:\n" + KIND_LESS_ENTRY)
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 0
    assert "skipped:unkinded-entry: k8s/list.yaml (document 0, List items[0])" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_a_deployment_list_with_a_kind_less_item_and_a_valid_item_reports_the_item_and_judges_the_valid_one(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/dl.yaml", "apiVersion: apps/v1\nkind: DeploymentList\nitems:\n" + KIND_LESS_ENTRY + VALID_ENTRY)
    result = scan(tmp_path)
    assert result["status"] == "fail" and result["signals"]["kubernetes_workloads"] == 1
    assert "skipped:unkinded-entry: k8s/dl.yaml (document 0, List items[0])" in next(
        f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]


def test_a_list_of_valid_items_still_gives_ok_with_no_reason(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/list.yaml", "apiVersion: v1\nkind: List\nitems:\n" + VALID_ENTRY)
    result = scan(tmp_path)
    assert result["status"] == "ok" and result["signals"]["kubernetes_workloads"] == 1
    assert "scan/incomplete" not in [f["id"] for f in result["findings"]]


# --- QA-03 round seven: a list kind nested inside a List is judged per item, under a depth limit -------------------------


NESTED_VALID = ("apiVersion: v1\nkind: List\nitems:\n  - apiVersion: apps/v1\n    kind: DeploymentList\n    items:\n"
                "      - apiVersion: apps/v1\n        kind: Deployment\n        metadata: {name: ok}\n"
                "        spec: {replicas: 3, template: {spec: {terminationGracePeriodSeconds: 30, containers: [{name: c, "
                "readinessProbe: {httpGet: {path: /, port: 80}}, resources: {requests: {cpu: 1m}}}]}}}\n")


def test_a_list_whose_only_entry_is_a_deployment_list_with_one_valid_deployment_is_ok_with_one_workload(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/nested.yaml", NESTED_VALID)
    result = scan(tmp_path)
    assert result["status"] == "ok" and result["signals"]["kubernetes_workloads"] == 1
    assert "scan/incomplete" not in [f["id"] for f in result["findings"]]


def test_an_invalid_deployment_inside_a_nested_deployment_list_gives_the_probes_finding(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "k8s/nested.yaml", NESTED_VALID.replace("readinessProbe: {httpGet: {path: /, port: 80}}, resources: {requests: {cpu: 1m}}",
                                                             "resources: {requests: {cpu: 1m}}"))
    result = scan(tmp_path)
    assert result["signals"]["kubernetes_workloads"] == 1
    assert ("cloud.k8s-probes-missing", "major", "k8s/nested.yaml") in brief(result)
    assert result["status"] == "fail"


def test_a_list_nested_beyond_the_depth_limit_is_a_reason_and_the_rest_of_the_file_is_still_judged(tmp_path):
    pytest.importorskip("yaml")
    bare = "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: deep}\nspec: {replicas: 1, template: {spec: {containers: [{name: c}]}}}\n"
    write(tmp_path, "k8s/deep.yaml",
          "apiVersion: v1\nkind: List\nitems:\n"
          "  - apiVersion: apps/v1\n    kind: DeploymentList\n    items:\n"          # list level 2
          "      - apiVersion: apps/v1\n        kind: DeploymentList\n        items:\n"  # list level 3
          "          - apiVersion: apps/v1\n            kind: DeploymentList\n            items:\n"  # list level 4: beyond the limit
          "              - " + bare.replace("\n", "\n                ").rstrip() + "\n"
          "  - " + bare.replace("\n", "\n    ").rstrip() + "\n")
    result = scan(tmp_path)
    assert result["status"] == "fail"
    summary = next(f for f in result["findings"] if f["id"] == "scan/incomplete")["summary"]
    assert "skipped:list-too-deep: k8s/deep.yaml (document 0, List items[0], List items[0], List items[0])" in summary
    assert ("cloud.k8s-probes-missing", "major", "k8s/deep.yaml") in brief(result)  # the top-level sibling Deployment is still judged
