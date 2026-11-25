"""Behaviour tests for scripts/chain_config_check.py (A28): fixture trees in tmp_path, findings, exit codes, JSON shape, bounds."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "chain_config_check.py"
TWIN = ROOT / "scripts" / "ts" / "chain_config_check.ts"
BUN = shutil.which("bun") or "/root/.local/share/reflex/bun/bin/bun"
KEYS = {"status", "files_scanned", "skipped", "checks", "truncated", "counts", "findings", "summary", "agent", "script"}


def module():
    spec = importlib.util.spec_from_file_location("chain_config_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(mod)
    return mod


def cli(tmp_path: Path, *args: str, root: Path | None = None) -> tuple[int, dict]:
    env = {**os.environ, "SWARM_DIR": str(tmp_path.parent / f"{tmp_path.name}-state")}  # event log stays outside the scanned tree
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json", "--root", str(root or tmp_path), *args], capture_output=True, text=True, env=env, cwd=ROOT)
    assert "Traceback" not in proc.stderr, proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def write(base: Path, rel: str, content) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def rules(tmp_path: Path, *args: str) -> dict[str, str]:
    """{rule: severity} for the whole tree (the highest severity per rule)."""
    code, out = cli(tmp_path, *args)
    order = ["info", "minor", "major", "blocker"]
    found: dict[str, str] = {}
    for f in out["findings"]:
        if f["rule"] not in found or order.index(f["severity"]) > order.index(found[f["rule"]]):
            found[f["rule"]] = f["severity"]
    assert code == (1 if out["status"] == "fail" else 0)
    return found


def net(chain_id=11155111, url="https://rpc.example.org/", name=None, **extra) -> dict:
    body = {"chainId": chain_id, "rpcUrl": url, **extra}
    if name:
        body["name"] = name
    return body


# --- contract -----------------------------------------------------------------------------------------------


def test_dry_run_is_canned_json_and_reads_nothing(tmp_path):
    write(tmp_path, "chains.json", net(1, "http://user:pw@x.example/"))  # would fail in real mode
    code, out = cli(tmp_path, "--dry-run")
    assert code == 0 and out["status"] == "ok" and out["dry_run"] is True and out["findings"] == [] and out["files_scanned"] == 0
    assert out["agent"] == "A28" and out["script"] == "chain_config_check" and KEYS <= set(out)
    assert cli(tmp_path, "--dry-run") == (code, out)


def test_ts_twin_passes_through_to_the_python_script():
    text = TWIN.read_text(encoding="utf-8")
    assert 'passthrough("chain_config_check")' in text


@pytest.mark.skipif(not Path(BUN).exists(), reason="bun not installed")
def test_ts_twin_resolves_a_relative_root_against_the_callers_cwd(tmp_path):
    """passthrough runs Python in the caller's directory, so a relative --root names the same tree for both."""
    work = tmp_path / "work"
    write(work, "qa/chains.json", net(1, "http://user:pw@x.example/"))
    argv = ["--json", "--root", "qa"]
    env = {**os.environ, "SWARM_DIR": str(tmp_path / "state")}  # event log stays outside the scanned tree
    py = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, env=env, cwd=work)
    ts = subprocess.run([BUN, str(TWIN), *argv], capture_output=True, text=True, env=env, cwd=work)
    assert py.returncode == 1 and ts.returncode == py.returncode, ts.stderr[-400:]
    assert json.loads(ts.stdout) == json.loads(py.stdout)
    assert json.loads(ts.stdout)["files_scanned"] == 1


def test_real_mode_output_shape_on_an_empty_tree(tmp_path):
    code, out = cli(tmp_path)
    assert code == 1 and KEYS <= set(out) and out["status"] == "fail" and out["files_scanned"] == 0
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("scan-incomplete", "major")] and out["truncated"] is False
    assert out["counts"] == {"info": 0, "minor": 0, "major": 1, "blocker": 0}
    assert out["checks"] == {"json": "no-files", "toml": "no-files", "yaml": "no-files", "env": "no-files"}
    assert out["summary"] == "1 finding(s): 0 blocker, 1 major, 0 minor, 0 info"


def test_clean_testnet_and_opted_in_mainnet_configs_pass(tmp_path):
    write(tmp_path, "networks.json", {
        "sepolia": net(11155111, "https://sepolia.example.org/${RPC_KEY}", "sepolia"),
        "mainnet": net(1, "wss://mainnet.example.org/ws?token=${RPC_TOKEN}", "Ethereum Mainnet", allowMainnet=True),
    })
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"], out["files_scanned"]) == (0, "ok", [], 1)


def test_path_must_stay_inside_the_root_and_exist(tmp_path):
    code, out = cli(tmp_path, "--path", "../outside")
    assert code == 2 and out["error"]["code"] == "E-INPUT"
    code, out = cli(tmp_path, "--path", "missing.json")
    assert code == 2 and out["error"]["code"] == "E-INPUT"


def test_path_can_name_a_single_file(tmp_path):
    write(tmp_path, "a/bad.json", net(1))
    write(tmp_path, "b/other.json", net(1))
    code, out = cli(tmp_path, "--path", "a/bad.json")
    assert code == 1 and out["files_scanned"] == 1 and {f["file"] for f in out["findings"]} == {"a/bad.json"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes need os.mkfifo")
def test_a_named_pipe_named_like_a_chain_config_is_refused_and_never_opened(tmp_path):
    os.mkfifo(tmp_path / "chains.json")
    write(tmp_path, "ok.json", net(1, allowMainnet=True))
    env = {**os.environ, "SWARM_DIR": str(tmp_path.parent / f"{tmp_path.name}-state")}
    # a read of the FIFO would block forever; the timeout turns that regression into a failure instead of a hang
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json", "--root", str(tmp_path)], capture_output=True, text=True, env=env,
                          cwd=ROOT, timeout=60)
    out = json.loads(proc.stdout)
    assert proc.returncode == 1 and out["status"] == "fail"
    assert out["skipped"]["unparsed"] == 1 and out["files_scanned"] == 1
    assert [f["rule"] for f in out["findings"]] == ["scan-incomplete"]


def test_output_is_deterministic_and_sorted_by_severity_then_file(tmp_path):
    write(tmp_path, "b.json", net(1, "https://u:p@x.example/"))
    write(tmp_path, "a.json", net(1))
    first = cli(tmp_path)
    assert first == cli(tmp_path)
    order = ["info", "minor", "major", "blocker"]
    keys = [(-order.index(f["severity"]), f["file"], f["rule"], f["location"]) for f in first[1]["findings"]]
    assert keys == sorted(keys) and first[1]["findings"][0]["severity"] == "blocker"


# --- URL rules ------------------------------------------------------------------------------------------------


def test_credentials_in_a_url_are_a_blocker_and_never_printed(tmp_path):
    write(tmp_path, "chains.json", net(11155111, "https://alice:hunter2@rpc.example.org/"))
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    finding = next(f for f in out["findings"] if f["rule"] == "rpc-url-credentials")
    assert (finding["severity"], finding["file"], finding["location"]) == ("blocker", "chains.json", "chains.json:rpcUrl")
    assert "hunter2" not in json.dumps(out) and "alice" not in json.dumps(out)


def test_username_only_userinfo_is_also_a_credential(tmp_path):
    write(tmp_path, "c.json", net(11155111, "https://token123@rpc.example.org/"))
    assert rules(tmp_path)["rpc-url-credentials"] == "blocker"


def test_api_keys_in_query_or_path_are_blockers_but_placeholders_are_not(tmp_path):
    write(tmp_path, "q.json", {"rpcUrls": ["https://x.example/rpc?apikey=abc123def", "https://x.example/rpc?KEY=abc", "https://x.example/rpc?apikey=${KEY}", "https://x.example/rpc?apikey=", "https://x.example/rpc?chain=eth"]})
    code, out = cli(tmp_path)
    flagged = sorted(f["location"] for f in out["findings"] if f["rule"] == "rpc-url-embedded-key")
    assert flagged == ["q.json:rpcUrls[0]", "q.json:rpcUrls[1]"] and code == 1


def test_long_token_like_path_segments_are_blockers(tmp_path):
    write(tmp_path, "p.json", {"rpcUrls": [
        "https://mainnet.example.io/v3/0123456789abcdef0123456789abcdef",  # infura-style
        "https://eth.example.com/v2/Ab3_dE-fGh1JkLmN0pQrStUvWxYz",  # alchemy-style, one hyphen
        "https://rpc.example.com/ethereum-mainnet-archive-node-v2-endpoint",  # words, many hyphens
        "https://rpc.example.com/v1/mainnet",
        "https://rpc.example.com/abcdefghijklmnopqrstuvwxyz",  # long but no digit
        "https://rpc.example.com/v3/${INFURA_API_KEY_FROM_ENV_00}",
    ]})
    _, out = cli(tmp_path)
    assert sorted(f["location"] for f in out["findings"] if f["rule"] == "rpc-url-embedded-key") == ["p.json:rpcUrls[0]", "p.json:rpcUrls[1]"]


def test_plaintext_schemes_are_major_unless_loopback(tmp_path):
    write(tmp_path, "s.json", {"rpcUrls": ["http://rpc.example.org", "ws://rpc.example.org/ws"]})
    code, out = cli(tmp_path)
    assert code == 1 and [(f["rule"], f["severity"]) for f in out["findings"]] == [("rpc-url-insecure-scheme", "major")] * 2
    assert [f["summary"] for f in out["findings"]] == ["the endpoint uses plaintext http: use https", "the endpoint uses plaintext ws: use wss"]


@pytest.mark.parametrize("url", ["http://localhost:8545", "http://127.0.0.1:8545", "ws://[::1]:8546"])
def test_loopback_plaintext_is_info_only(tmp_path, url):
    write(tmp_path, "l.json", net(31337, url, "hardhat"))
    code, out = cli(tmp_path)
    assert code == 0 and out["status"] == "ok" and [(f["rule"], f["severity"]) for f in out["findings"]] == [("rpc-url-local-plaintext", "info")]


def test_other_schemes_are_minor_and_malformed_urls_are_reported(tmp_path):
    write(tmp_path, "o.json", {"rpcUrls": ["ipc:///var/run/geth.ipc", "https://host:notaport/"]})
    found = rules(tmp_path)
    assert found == {"rpc-url-scheme": "minor", "rpc-url-malformed": "minor"}


def test_only_rpc_like_keys_are_treated_as_endpoints(tmp_path):
    write(tmp_path, "w.json", {"chainId": 11155111, "rpcUrl": "https://ok.example.org", "blockExplorerUrls": ["http://explorer.example.org"],
                               "faucetUrl": "http://faucet.example.org", "homepage": "http://example.org", "docs": {"url": "http://docs.example.org"}})
    code, out = cli(tmp_path)
    assert (code, out["findings"]) == (0, [])


def test_plain_url_key_counts_only_next_to_a_chain_id(tmp_path):
    write(tmp_path, "u.json", {"service": {"url": "http://example.org/"}, "net": {"chainId": 11155111, "url": "http://node.example.org/"}})
    _, out = cli(tmp_path)
    assert [f["location"] for f in out["findings"]] == ["u.json:net.url"]


def test_eip3085_and_viem_shaped_chain_objects(tmp_path):
    write(tmp_path, "add.json", {"chainId": "0xaa36a7", "chainName": "Sepolia", "rpcUrls": ["http://rpc.example.org"], "blockExplorerUrls": ["http://e.example.org"],
                                 "nativeCurrency": {"name": "Sepolia Ether", "symbol": "ETH", "decimals": 18}})
    write(tmp_path, "viem.json", {"id": 1, "name": "Ethereum", "nativeCurrency": {"symbol": "ETH"}, "rpcUrls": {"default": {"http": ["https://u:p@rpc.example.org"]}}})
    _, out = cli(tmp_path)
    assert sorted((f["file"], f["rule"]) for f in out["findings"]) == [("add.json", "rpc-url-insecure-scheme"), ("viem.json", "mainnet-without-opt-in"), ("viem.json", "rpc-url-credentials")]


# --- chain-id rules ---------------------------------------------------------------------------------------------


def test_mainnet_grade_chain_ids_need_an_opt_in_in_the_same_or_an_enclosing_object(tmp_path):
    write(tmp_path, "none.json", net(1))
    write(tmp_path, "same.json", net(1, allowMainnet=True))
    write(tmp_path, "text.json", net(137, mainnetOptIn="yes"))
    write(tmp_path, "outer.json", {"mainnet_approved": True, "networks": {"eth": net(1), "base": net(8453)}})
    write(tmp_path, "false.json", net(1, allowMainnet=False))
    _, out = cli(tmp_path)
    flagged = sorted(f["file"] for f in out["findings"] if f["rule"] == "mainnet-without-opt-in")
    assert flagged == ["false.json", "none.json"]
    assert all(f["severity"] == "major" for f in out["findings"] if f["rule"] == "mainnet-without-opt-in")


def test_unknown_chain_id_is_minor_and_treated_as_mainnet(tmp_path):
    write(tmp_path, "x.json", net(424242))
    assert rules(tmp_path) == {"mainnet-without-opt-in": "major", "chain-id-unknown": "minor"}
    write(tmp_path, "x.json", net(424242, allowMainnet=True))
    assert rules(tmp_path) == {"chain-id-unknown": "minor"}


@pytest.mark.parametrize("value,expected", [(1, "mainnet-without-opt-in"), ("1", "mainnet-without-opt-in"), ("0x1", "mainnet-without-opt-in"), ("0X1", "mainnet-without-opt-in"),
                                            (11155111, None), ("0xaa36a7", None)])
def test_chain_ids_parse_as_integers_decimal_text_and_hex_text(tmp_path, value, expected):
    write(tmp_path, "c.json", {"chain_id": value, "rpc_url": "https://x.example.org/"})
    assert set(rules(tmp_path)) == ({expected} if expected else set())


@pytest.mark.parametrize("value", [0, -5, True, "abc", "0xzz", 2**53, 1.5, None, ""])
def test_unparseable_chain_ids_are_minor_and_fail_the_scan(tmp_path, value):
    write(tmp_path, "c.json", {"chainId": value})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert {f["rule"]: f["severity"] for f in out["findings"]} == {"chain-id-unparseable": "minor", "scan-incomplete": "major"}


@pytest.mark.parametrize("name,chain_id,rule,severity", [
    ("sepolia", 1, "chain-id-name-mismatch", "blocker"),
    ("ethereum-sepolia", 1, "chain-id-name-mismatch", "blocker"),
    ("Ethereum Mainnet", 11155111, "chain-id-name-mismatch", "blocker"),
    ("Base", 8453, None, None),
    ("base-sepolia", 8453, "chain-id-name-mismatch", "blocker"),
    ("my-testnet", 1, "chain-id-name-mismatch", "blocker"),
    ("staging-devnet", 137, "chain-id-name-mismatch", "blocker"),
    ("custom-mainnet", 11155111, "chain-id-name-mismatch", "major"),
    ("my-private-chain", 11155111, None, None),
    ("anvil", 31337, None, None),
])
def test_network_name_must_agree_with_the_chain_id(tmp_path, name, chain_id, rule, severity):
    write(tmp_path, "n.json", net(chain_id, name=name, allowMainnet=True))
    found = rules(tmp_path)
    assert found.get("chain-id-name-mismatch") == severity and (rule is None) == ("chain-id-name-mismatch" not in found)


def test_a_name_that_only_contains_a_testnet_word_is_not_a_testnet(tmp_path):
    write(tmp_path, "fujitsu.json", net(1, name="Fujitsu Gateway", allowMainnet=True))
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_a_host_that_only_contains_a_testnet_word_is_not_a_testnet(tmp_path):
    write(tmp_path, "host.json", net(1, "https://fujitsu-rpc.example.com/", allowMainnet=True))
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_a_bare_alias_of_another_chain_is_a_minor_note_not_a_blocker(tmp_path):
    write(tmp_path, "base.json", net(84532, name="base"))
    _, out = cli(tmp_path)
    assert out["status"] == "ok" and [(f["rule"], f["severity"]) for f in out["findings"]] == [("chain-id-name-mismatch", "minor")]


def test_unambiguous_testnet_alias_on_a_mainnet_chain_id_blocks(tmp_path):
    write(tmp_path, "spoof.json", net(1, name="ethereum-sepolia", allowMainnet=True))
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


def test_endpoint_host_must_agree_with_the_chain_id_class(tmp_path):
    write(tmp_path, "h1.json", net(1, "https://sepolia.example.org/", allowMainnet=True))
    write(tmp_path, "h2.json", net(11155111, "https://mainnet.example.org/"))
    write(tmp_path, "h3.json", net(1, "https://mainnet.example.org/", allowMainnet=True))
    _, out = cli(tmp_path)
    assert sorted(f["file"] for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch") == ["h1.json", "h2.json"]


def test_one_network_name_with_two_chain_ids_conflicts_across_files(tmp_path):
    write(tmp_path, "a.json", net(11155111, name="staging"))
    write(tmp_path, "b.json", net(80002, name="Staging"))
    write(tmp_path, "c.json", net(80002, name="other"))
    _, out = cli(tmp_path)
    conflicts = [f for f in out["findings"] if f["rule"] == "chain-id-conflict"]
    assert len(conflicts) == 1 and conflicts[0]["file"] == "b.json" and conflicts[0]["severity"] == "major" and "a.json" in conflicts[0]["summary"]


# --- file kinds -------------------------------------------------------------------------------------------------


def test_env_files_are_read_as_flat_key_values(tmp_path):
    write(tmp_path, ".env", "# comment\nexport CHAIN_ID=1\nRPC_URL=\"https://user:pw@rpc.example.org\"\nNETWORK=mainnet\nALLOW_MAINNET=true\n")
    write(tmp_path, ".env.staging", "CHAIN_ID=11155111\nRPC_URL=http://rpc.example.org\n")
    _, out = cli(tmp_path)
    assert sorted((f["file"], f["rule"]) for f in out["findings"]) == [(".env", "rpc-url-credentials"), (".env.staging", "rpc-url-insecure-scheme")]
    assert out["checks"]["env"] == "ran" and out["files_scanned"] == 2


def test_toml_files_are_parsed_with_the_standard_library(tmp_path):
    write(tmp_path, "foundry.toml", '[rpc_endpoints]\nmain = "https://u:p@rpc.example.org"\n\n[profile.default]\nchain_id = 1\n')
    code, out = cli(tmp_path)
    assert out["checks"]["toml"] == "ran" and code == 1
    assert {(f["rule"], f["location"]) for f in out["findings"]} == {("rpc-url-credentials", "foundry.toml:rpc_endpoints.main"), ("mainnet-without-opt-in", "foundry.toml:profile.default.chain_id")}


def test_yaml_files_are_parsed_when_pyyaml_is_available(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "c.yaml", "networks:\n  main:\n    chain_id: 1\n    rpc_url: http://rpc.example.org\n---\nchainId: 11155111\nrpcUrl: https://u:p@x.example.org\n")
    code, out = cli(tmp_path)
    assert code == 1 and out["checks"]["yaml"] == "ran"
    assert sorted(f["rule"] for f in out["findings"]) == ["mainnet-without-opt-in", "rpc-url-credentials", "rpc-url-insecure-scheme"]


def test_missing_pyyaml_is_reported_as_skipped_and_json_still_runs(tmp_path, monkeypatch):
    write(tmp_path, "c.yaml", "chainId: 1\n")
    write(tmp_path, "c.json", net(1))
    monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError
    out = module().scan(tmp_path, tmp_path)
    assert out["checks"] == {"json": "ran", "toml": "no-files", "yaml": "skipped:tool-missing", "env": "no-files"}
    assert out["files_scanned"] == 1 and {f["file"] for f in out["findings"]} == {"c.json", "."} and out["status"] == "fail"


def test_unparseable_and_unrelated_files_never_crash_the_scan(tmp_path):
    write(tmp_path, "broken.json", "{not json")
    write(tmp_path, "list.json", [1, 2, {"chainId": 11155111}])
    write(tmp_path, "scalar.json", "42")
    write(tmp_path, "settings.toml", "= broken")
    code, out = cli(tmp_path)
    assert code == 1 and {f["rule"] for f in out["findings"]} == {"scan-incomplete"} and out["skipped"]["unparsed"] >= 2


def test_deeply_nested_json_does_not_crash(tmp_path):
    write(tmp_path, "deep.json", "[" * 5000 + "]" * 5000)
    write(tmp_path, "deep2.json", '{"a":' * 100 + "1" + "}" * 100)
    code, out = cli(tmp_path)
    assert code == 1 and {f["rule"] for f in out["findings"]} == {"scan-incomplete"} and "deep2.json: nested deeper than 30 levels" in out["findings"][0]["summary"]


# --- bounds -----------------------------------------------------------------------------------------------------


def test_files_over_1_mib_binaries_symlinks_and_vendored_dirs_are_skipped(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(json.dumps(net(1)) + " " * (1024 * 1024), encoding="utf-8")
    (tmp_path / "bin.json").write_bytes(b'{"chainId": 1}\0\0')
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    write(outside, "secret.json", net(1))
    (tmp_path / "link.json").symlink_to(outside / "secret.json")
    (tmp_path / "linkdir").symlink_to(outside, target_is_directory=True)
    write(tmp_path, "node_modules/pkg/c.json", net(1))
    write(tmp_path, ".git/c.json", net(1))
    write(tmp_path, "package-lock.json", net(1))
    write(tmp_path, "ok/c.json", net(11155111))
    code, out = cli(tmp_path)
    assert code == 1 and {f["rule"] for f in out["findings"]} == {"scan-incomplete"} and out["files_scanned"] == 1
    assert all(part in out["findings"][0]["summary"] for part in ("big.json: larger than 1 MiB", "bin.json: binary", "link.json: symlink", "linkdir: symlinked directory"))
    assert out["skipped"] == {"too_large": 1, "binary": 1, "symlink": 1, "unparsed": 0}


def test_exactly_one_mib_is_still_scanned(tmp_path):
    body = json.dumps(net(1))
    (tmp_path / "edge.json").write_text(body + " " * (1024 * 1024 - len(body)), encoding="utf-8")
    code, out = cli(tmp_path)
    assert code == 1 and out["skipped"]["too_large"] == 0 and out["files_scanned"] == 1


def test_scan_stops_after_2000_candidate_files(tmp_path):
    for index in range(2005):
        write(tmp_path, f"d{index % 7}/f{index:04d}.json", {"x": index})
    code, out = cli(tmp_path)
    assert code == 1 and out["truncated"] is True and out["files_scanned"] == 2000


def test_finding_count_is_capped(tmp_path):
    write(tmp_path, "many.json", {"rpcUrls": [f"http://h{i}.example.org" for i in range(600)]})
    code, out = cli(tmp_path)
    assert code == 1 and len(out["findings"]) == 500 and out["truncated"] is True


def test_script_does_not_write_into_the_scanned_tree(tmp_path):
    write(tmp_path, "c.json", net(1))
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    cli(tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_tool_and_script_share_one_chain_table():
    mod = module()
    tools = sys.modules["swarm.tools.a28_tools"]
    assert mod.CHAINS is tools.CHAINS
    assert mod.NAME_TO_ID["sepolia"] == 11155111 and mod.NAME_TO_ID["ethereum-mainnet"] == 1 and mod.NAME_TO_ID["op-sepolia"] == 11155420


# --- independent review: secrets never echoed, severity past the cap, bombs, templates --------------------------

CANARIES = ["ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "AKIAIOSFODNN7EXAMPLE", "https://user:hunter2pass@rpc.example.org/v2/abc", "password=hunter2"]


@pytest.mark.parametrize("canary", CANARIES)
def test_secret_looking_network_names_and_keys_are_never_echoed(tmp_path, canary):
    write(tmp_path, "a.json", {canary: {"chainId": 1, "name": canary, "rpcUrl": f"https://{canary.split('//')[-1]}"}})
    write(tmp_path, "b.json", {"chainId": 11155111, "name": canary})
    write(tmp_path, "c.json", {"chainId": 80002, "name": canary})
    code, out = cli(tmp_path)
    assert code == 1 and out["findings"]
    blob = json.dumps(out)
    assert canary not in blob and "hunter2" not in blob and "a1B2c3D4" not in blob and "AKIAIOSFODNN7" not in blob


def test_a_plain_network_name_is_still_shown_in_the_message(tmp_path):
    write(tmp_path, "c.json", net(1, name="Sepolia"))
    _, out = cli(tmp_path)
    assert any("'Sepolia'" in f["summary"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch")


def test_a_blocker_past_the_finding_cap_still_fails_the_scan_and_is_listed_first(tmp_path):
    write(tmp_path, "a.json", {"rpcUrls": [f"http://localhost:{8000 + i}" for i in range(600)]})  # 600 loopback notes (info)
    write(tmp_path, "z.json", net(11155111, "https://alice:pw@rpc.example.org/"))  # sorts after every a.json finding
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail" and out["truncated"] is True
    assert len(out["findings"]) == 500 and out["findings"][0]["rule"] == "rpc-url-credentials"
    assert out["counts"] == {"info": 600, "minor": 0, "major": 0, "blocker": 1}
    assert "601 finding(s)" in out["summary"] and "(101 not listed)" in out["summary"]


def test_userinfo_made_only_of_templates_is_not_a_credential_but_literal_text_is(tmp_path):
    write(tmp_path, "t.json", {"rpcUrls": ["https://${RPC_USER}:${RPC_PASS}@rpc.example.org/", "https://<user>:<pass>@rpc.example.org/", "https://%RPC_USER%:%RPC_PASS%@rpc.example.org/"]})
    code, out = cli(tmp_path)
    assert code == 0 and out["findings"] == []
    write(tmp_path, "t.json", {"rpcUrls": ["https://${RPC_USER}:realpassword@rpc.example.org/", "https://user:p%40ss@rpc.example.org/", "https://u:@rpc.example.org/"]})
    _, out = cli(tmp_path)
    assert [f["rule"] for f in out["findings"]] == ["rpc-url-credentials"] * 3


def test_literal_secrets_that_merely_contain_a_dollar_or_percent_are_not_templates(tmp_path):
    write(tmp_path, "q.json", {"rpcUrls": ["https://x.example/rpc?apikey=abc$def", "https://x.example/rpc?token=ab%25cd", "https://x.example/rpc?apikey=$KEY", "https://x.example/rpc?token=<TOKEN>"]})
    _, out = cli(tmp_path)
    assert sorted(f["location"] for f in out["findings"] if f["rule"] == "rpc-url-embedded-key") == ["q.json:rpcUrls[0]", "q.json:rpcUrls[1]"]


@pytest.mark.parametrize("param", ["password", "api-key", "jwt", "client_secret", "PRIVATE_KEY"])
def test_more_secret_query_parameters_are_blockers(tmp_path, param):
    write(tmp_path, "q.json", {"rpcUrls": [f"https://x.example/rpc?{param}=s3cret"]})
    code, out = cli(tmp_path)
    assert code == 1 and [f["rule"] for f in out["findings"]] == ["rpc-url-embedded-key"] and "s3cret" not in json.dumps(out)


def test_any_true_opt_in_key_in_the_object_counts_even_after_a_false_one(tmp_path):
    write(tmp_path, "c.json", {"chainId": 1, "allowMainnet": False, "mainnetApproved": True})
    code, out = cli(tmp_path)
    assert code == 0 and out["findings"] == []
    write(tmp_path, "d.json", {"chainId": 10, "allowMainnet": False, "enableMainnet": "no"})
    _, out = cli(tmp_path)
    assert [f["rule"] for f in out["findings"]] == ["mainnet-without-opt-in"]


def test_a_yaml_alias_bomb_is_cut_off_at_the_node_budget_and_reported(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 10):
        lines.append(f"l{level}: &l{level} [" + ", ".join([f"*l{level - 1}"] * 9) + "]")  # 9^9 nodes when expanded
    write(tmp_path, "bomb.yaml", "\n".join(lines) + "\n")
    write(tmp_path, "ok.json", net(1))
    mod = module()
    walks = [0]
    real_walk = mod._Scan.walk

    def counted_walk(self, *args, **kwargs):
        walks[0] += 1
        return real_walk(self, *args, **kwargs)

    monkeypatch.setattr(mod._Scan, "walk", counted_walk)
    out = mod.scan(tmp_path, tmp_path)
    # counted work, not wall time: a visited node makes at most one walk call per child (ten keys at most, nine items per list), so
    # the calls stay within a small multiple of MAX_NODES. Without the node budget the walk would expand the shared graph to 9^9 nodes.
    assert 0 < walks[0] <= 20 * mod.MAX_NODES, walks[0]
    assert out["status"] == "fail"
    assert {f["rule"] for f in out["findings"]} == {"scan-truncated", "mainnet-without-opt-in", "scan-incomplete"}
    assert [f for f in out["findings"] if f["rule"] == "scan-truncated"][0]["file"] == "bomb.yaml"


def test_exceeding_the_file_cap_adds_a_visible_note_instead_of_a_silent_ok(tmp_path):
    for index in range(2001):
        write(tmp_path, f"d{index % 7}/f{index:04d}.json", {"x": index})
    code, out = cli(tmp_path)
    assert code == 1 and out["truncated"] is True
    assert [(f["rule"], f["severity"], f["file"]) for f in out["findings"]] == [("scan-incomplete", "major", "."), ("scan-truncated", "minor", ".")]


def test_dotenv_inline_comments_and_quoted_values_do_not_hide_a_mainnet_chain_id(tmp_path):
    write(tmp_path, ".env", "CHAIN_ID=1 # production chain\nRPC_URL='https://alice:pw@rpc.example.org/' # primary\nNETWORK=\"Ethereum Mainnet\"  # label\n")
    _, out = cli(tmp_path)
    assert sorted(f["rule"] for f in out["findings"]) == ["mainnet-without-opt-in", "rpc-url-credentials"]
    assert not any(f["rule"] == "chain-id-unparseable" for f in out["findings"])
    assert "alice" not in json.dumps(out) and "primary" not in json.dumps(out)


# --- QA-03 remediation: test-network names, aliases, unknown ids, fail-closed scan reasons ----------------------------


@pytest.mark.parametrize("name,chain_id", [("SepoliaTestnet", 1), ("BaseSepolia", 8453), ("GoerliDevnet", 137)])
def test_a_single_token_testnet_name_on_a_mainnet_id_is_a_blocker(tmp_path, name, chain_id):
    write(tmp_path, "n.json", net(chain_id, name=name, allowMainnet=True))
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


@pytest.mark.parametrize("name,chain_id", [("Mumbai", 137), ("Ropsten", 1), ("Rinkeby", 1), ("Kovan", 1)])
def test_deprecated_testnet_names_on_a_mainnet_id_are_blockers(tmp_path, name, chain_id):
    write(tmp_path, "n.json", net(chain_id, name=name, allowMainnet=True))
    assert rules(tmp_path)["chain-id-name-mismatch"] == "blocker"


def test_deprecated_testnets_on_their_own_ids_pass(tmp_path):
    write(tmp_path, "a.json", net(80001, name="Mumbai"))
    write(tmp_path, "b.json", net(3, name="Ropsten"))
    code, out = cli(tmp_path)
    assert code == 0 and out["findings"] == []


def test_an_unknown_chain_id_is_checked_for_test_network_names_and_hosts_like_a_mainnet(tmp_path):
    write(tmp_path, "name.json", net(999999, name="Sepolia Testnet", allowMainnet=True))
    write(tmp_path, "host.json", net(999999, "https://rpc.sepolia.org", allowMainnet=True))
    found = rules(tmp_path)
    assert found["chain-id-name-mismatch"] == "blocker" and found["rpc-host-chain-mismatch"] == "blocker" and found["chain-id-unknown"] == "minor"


def test_a_test_network_host_on_a_mainnet_id_is_a_blocker_like_the_name(tmp_path):
    write(tmp_path, "h.json", net(1, "https://rpc.sepolia.org", allowMainnet=True))
    assert rules(tmp_path)["rpc-host-chain-mismatch"] == "blocker"


@pytest.mark.parametrize("name,chain_id", [("optimism", 11155420), ("bsc", 97)])
def test_family_aliases_on_a_testnet_id_stay_minor_like_base(tmp_path, name, chain_id):
    write(tmp_path, "f.json", net(chain_id, name=name, allowMainnet=True))
    code, out = cli(tmp_path)
    assert code == 0 and out["status"] == "ok"
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("chain-id-name-mismatch", "minor")]


def test_ambiguity_counts_aliases_as_well_as_canonical_names():
    mod = module()
    assert mod._family("optimism") == {10, 11155420} and mod._family("bsc") == {56, 97} and mod._family("base") == {8453, 84532}
    assert mod._family("sepolia") == {11155111}
    assert mod._family("celo") == {42220} and mod._family("no-such-network") == frozenset()


def test_inputs_the_scan_could_not_read_fail_with_labelled_reasons_only(tmp_path):
    write(tmp_path, "broken.json", "{not json")
    write(tmp_path, "ok.json", net(11155111))
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    write(outside, "s.json", net(1))
    (tmp_path / "linkdir").symlink_to(outside, target_is_directory=True)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("scan-incomplete", "major")]
    assert "broken.json: could not be parsed as json" in out["findings"][0]["summary"] and "linkdir: symlinked directory" in out["findings"][0]["summary"]
    assert "not json" not in json.dumps(out) and "s.json" not in out["findings"][0]["summary"]


def test_a_document_nested_past_the_depth_limit_is_reported_not_skipped_silently(tmp_path):
    deep: dict = {}
    node = deep
    for _ in range(40):
        node["a"] = {}
        node = node["a"]
    node["chainId"] = 1  # a mainnet id no one checked
    write(tmp_path, "deep.json", deep)
    code, out = cli(tmp_path)
    assert code == 1 and [f["rule"] for f in out["findings"]] == ["scan-incomplete"]
    assert "deep.json: nested deeper than 30 levels" in out["findings"][0]["summary"]


def test_an_empty_tree_is_incomplete_not_clean(tmp_path):
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("scan-incomplete", "major")]
    assert "no in-scope input was analysed" in out["findings"][0]["summary"]


def test_every_name_key_in_one_object_is_checked_in_either_order(tmp_path):
    write(tmp_path, "forward.json", {"chainId": 1, "name": "Ethereum Mainnet", "network": "sepolia", "allowMainnet": True})
    write(tmp_path, "reverse.json", {"network": "sepolia", "name": "Ethereum Mainnet", "chainId": 1, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert sorted(f["file"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch" and f["severity"] == "blocker") == ["forward.json", "reverse.json"]


def test_chain_id_keys_named_by_pattern_are_checked_with_their_rpc_host(tmp_path):
    write(tmp_path, ".env", "NEXT_PUBLIC_CHAIN_ID=1\nRPC_URL=https://rpc.sepolia.org\nALLOW_MAINNET=true\n")
    write(tmp_path, "pair.json", {"chain": 1, "rpcUrl": "https://rpc.sepolia.org", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert sorted(f["file"] for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch") == [".env", "pair.json"]


def test_a_bare_chain_holding_a_name_is_a_name_not_an_id(tmp_path):
    write(tmp_path, "c.json", {"chain": "sepolia", "chainId": 11155111, "rpcUrl": "https://rpc.sepolia.org"})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_two_chain_ids_in_one_object_that_disagree_fail_the_scan(tmp_path):
    write(tmp_path, "c.json", {"chainId": 1, "chain_id": 11155111, "allowMainnet": True})
    write(tmp_path, "same.json", {"chainId": 11155111, "chain_id": "0xaa36a7"})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["file"], f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-keys-disagree"] == [("c.json", "chain-id-keys-disagree", "major")]


def test_an_explicit_file_the_scan_does_not_read_is_incomplete_not_clean(tmp_path):
    write(tmp_path, "x.cfg", "chainId=1\n")
    code, out = cli(tmp_path, "--path", "x.cfg")
    assert code == 1 and out["status"] == "fail" and out["files_scanned"] == 0
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("scan-incomplete", "major")]
    assert "x.cfg: not a chain-config file type the scan reads" in out["findings"][0]["summary"]


def test_an_rpc_value_without_a_scheme_is_a_minor_note_not_a_silent_skip(tmp_path):
    write(tmp_path, "c.json", {"chainId": 11155111, "rpcUrl": "rpc.sepolia.org"})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("rpc-url-not-a-url", "minor")]


def test_an_unparseable_chain_id_fails_the_scan_and_names_only_its_location(tmp_path):
    write(tmp_path, "c.json", {"chainId": "sepolia", "rpcUrl": "https://rpc.sepolia.org"})
    code, out = cli(tmp_path)
    incomplete = [f for f in out["findings"] if f["rule"] == "scan-incomplete"]
    assert code == 1 and out["status"] == "fail" and len(incomplete) == 1 and incomplete[0]["severity"] == "major"
    assert "c.json:chainId" in incomplete[0]["summary"] and "sepolia" not in json.dumps(out)


def test_the_visit_cap_sets_truncated_and_stays_incomplete(tmp_path, monkeypatch):
    mod = module()
    monkeypatch.setattr(mod, "MAX_VISITED", 2)
    for name in ("a.json", "b.json", "c.json"):
        write(tmp_path, name, net(1, allowMainnet=True))
    out = mod.scan(tmp_path, tmp_path)
    assert out["truncated"] is True and out["status"] == "fail"
    assert [f["rule"] for f in out["findings"]] == ["scan-incomplete"]


def test_a_source_and_destination_chain_id_pair_is_not_a_conflict(tmp_path):
    write(tmp_path, "bridge.json", {"source_chain_id": 1, "destination_chain_id": 8453, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_two_non_directional_chain_ids_that_disagree_still_fail(tmp_path):
    write(tmp_path, "c.json", {"chainId": 1, "chain_id": 11155111, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-keys-disagree"] == [("chain-id-keys-disagree", "major")]


def test_n_a_a_directional_name_key_is_a_name_and_is_checked(tmp_path):
    write(tmp_path, "src.json", {"source_chain_id": 1, "source_network": "Sepolia", "allowMainnet": True})
    write(tmp_path, "dst.json", {"destinationChainId": 8453, "destinationNetwork": "Base Sepolia", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert sorted(f["file"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch" and f["severity"] == "blocker") == ["dst.json", "src.json"]


def test_n_b_an_undirected_test_network_name_beside_a_mainnet_leg_is_a_blocker(tmp_path):
    write(tmp_path, "bridge.json", {"source_chain_id": 11155111, "destination_chain_id": 8453, "network": "Sepolia", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_n_c_two_ids_in_one_direction_that_disagree_are_a_major_conflict(tmp_path):
    write(tmp_path, "flat.json", {"source_chain_id": 11155111, "sourceChainId": 1, "allowMainnet": True})
    write(tmp_path, "nested.json", {"source": {"chainId": 1}, "source_chain_id": 11155111, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert sorted(f["file"] for f in out["findings"] if f["rule"] == "chain-id-keys-disagree" and f["severity"] == "major") == ["flat.json", "nested.json"]


def test_n_d_a_mainnet_leg_beside_a_testnet_leg_is_one_minor_note_with_status_ok(tmp_path):
    write(tmp_path, "nested.json", {"source": {"chainId": 1, "rpcUrl": "https://eth.llamarpc.com"}, "destination": {"chainId": 11155111, "rpcUrl": "https://rpc.sepolia.org"}, "allowMainnet": True})
    write(tmp_path, "flat.json", {"source_chain_id": 1, "destination_chain_id": 11155111, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["file"], f["rule"], f["severity"]) for f in out["findings"]] == [("flat.json", "chain-id-mixed-networks", "minor"), ("nested.json", "chain-id-mixed-networks", "minor")]


def test_n_e_a_testnet_host_in_one_leg_is_checked_against_the_other_legs_chain_id(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"rpcUrl": "https://rpc.sepolia.org"}, "allowMainnet": True})
    write(tmp_path, "b.json", {"source": {"rpcUrl": "https://rpc.sepolia.org"}, "destination": {"chainId": 1}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert sorted(f["file"] for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch" and f["severity"] == "blocker") == ["a.json", "b.json"]


def test_n_f_a_no_dot_rpc_value_is_a_minor_note_not_a_silent_skip(tmp_path):
    write(tmp_path, "c.json", {"chainId": 1, "rpcUrl": "sepolia", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["rule"], f["severity"]) for f in out["findings"]] == [("rpc-url-not-a-url", "minor")]


def test_a_direction_less_chain_id_beside_a_directed_one_is_a_minor_note_not_a_comparison(tmp_path):
    write(tmp_path, "mixed.json", {"chainId": 1, "source_chain_id": 11155111, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["file"], f["rule"], f["severity"]) for f in out["findings"]] == [("mixed.json", "chain-id-mixed-networks", "minor")]


def test_an_undirected_base_name_beside_legs_1_and_8453_is_consistent_and_ok(tmp_path):
    write(tmp_path, "bridge.json", {"source_chain_id": 1, "destination_chain_id": 8453, "name": "Base", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_an_undirected_base_name_beside_legs_1_and_10_gives_no_finding_because_no_leg_matches_its_family(tmp_path):
    write(tmp_path, "bridge.json", {"source_chain_id": 1, "destination_chain_id": 10, "name": "Base", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_an_undirected_testnet_name_on_directed_mainnet_ids_is_a_blocker(tmp_path):
    write(tmp_path, "bridge.json", {"source_chain_id": 1, "destination_chain_id": 8453, "name": "SepoliaTestnet", "allowMainnet": True})
    code, out = cli(tmp_path)
    blockers = [f for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]
    assert code == 1 and out["status"] == "fail"
    assert blockers and all(f["severity"] == "blocker" for f in blockers)


def test_a_host_under_a_directed_key_is_compared_only_with_that_direction(tmp_path):
    write(tmp_path, "scoped.json", {"source_chain_id": 11155111, "destination_chain_id": 8453, "source_rpc_url": "https://rpc.sepolia.org", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and [f for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch"] == []


def test_an_endpoint_under_its_own_testnet_id_is_not_judged_by_an_inherited_mainnet_id(tmp_path):
    write(tmp_path, "nested.json", {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "rpcUrl": "https://rpc.sepolia.org"}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and [f for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch"] == []


def test_a_nested_endpoint_of_a_testnet_item_is_judged_by_that_item_not_an_inherited_mainnet_id(tmp_path):
    write(tmp_path, "nested.json", {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "rpc": {"url": "https://rpc.sepolia.org"}, "endpoints": [{"url": "https://rpc.sepolia.org"}]}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and [f for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch"] == []


SEPOLIA = "https://rpc.sepolia.org"


@pytest.mark.parametrize("doc", [
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "rpcUrls": [SEPOLIA]}]},
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "rpcs": [SEPOLIA]}]},
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "endpoints": [SEPOLIA]}]},
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "urls": [SEPOLIA]}]},
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "rpc": {"urls": [SEPOLIA]}}]},
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [{"chainId": 11155111, "rpcUrl": [SEPOLIA]}]},
    {"allowMainnet": True, "source": {"chainId": 1}, "networks": [
        {"chainId": 11155111, "rpcUrl": SEPOLIA}, {"chainId": 1, "rpcUrl": "https://eth.llamarpc.com"}, {"chainId": 84532, "rpcUrl": "https://sepolia.base.org"}]},
], ids=["rpcUrls", "rpcs", "endpoints", "urls", "rpc-urls", "rpcUrl-list", "several-networks"])
def test_valid_testnet_endpoints_under_their_own_network_items_are_not_blocked(tmp_path, doc):
    write(tmp_path, "networks.json", doc)
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and [f for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch"] == []


def test_credentialed_urls_used_as_mapping_keys_are_never_printed(tmp_path):
    write(tmp_path, "cfg.json", {"https://user:password@rpc.example": {"chainId": 1, "rpcUrl": "https://user:password@rpc.example/?apikey=abcd1234"},
                                 "networks": [{"https://alice:hunter2pass@rpc.sepolia.org": {"name": "Sepolia", "chainId": 11155111}}]})
    code, out = cli(tmp_path)
    assert code == 1 and out["findings"]
    text = json.dumps(out)
    for secret in ("password@", "hunter2pass", "abcd1234"):  # the fixed rule text "embeds user:password credentials" names no value
        assert secret not in text, secret


def test_a_host_under_a_directed_key_still_blocks_on_its_own_mainnet_source(tmp_path):
    write(tmp_path, "scoped.json", {"source_chain_id": 1, "destination_chain_id": 8453, "source_rpc_url": "https://rpc.sepolia.org", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch"] == [("rpc-host-chain-mismatch", "blocker")]


def test_a_host_under_an_undirected_key_is_still_compared_with_every_id(tmp_path):
    write(tmp_path, "every.json", {"source_chain_id": 11155111, "destination_chain_id": 8453, "rpcUrl": "https://rpc.sepolia.org", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch"] == [("rpc-host-chain-mismatch", "blocker")]


@pytest.mark.parametrize("doc", [
    {"name": "Bridge", "source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"name": "Bridge", "source": {"chainId": 1, "rpcUrl": "https://eth.llamarpc.com"}, "destination": {"chainId": 8453, "rpcUrl": "https://mainnet.base.org"}, "allowMainnet": True},
    {"name": "Lock Bridge", "source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"name": "Testnet Bridge", "source": {"chainId": 11155111}, "destination": {"chainId": 84532}},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "rpcUrls": [{"name": "Optimism", "url": "https://mainnet.optimism.io"}], "allowMainnet": True},
], ids=["bridge", "bridge-with-urls-and-opt-in", "lock-bridge", "testnet-bridge", "optimism-rpc-label"])
def test_nd_1_a_name_matching_no_leg_of_a_bridge_gives_ok_with_no_conflict(tmp_path, doc):
    write(tmp_path, "bridge.json", doc)
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


@pytest.mark.parametrize("doc,expected", [
    ({"source_chain_id": 1, "destination_chain_id": 8453, "source_network": "Base", "allowMainnet": True}, "blocker"),
    ({"source_chain_id": 1, "destination_chain_id": 8453, "destination_network": "Ethereum", "allowMainnet": True}, "blocker"),
    ({"source_chain_id": 1, "destination_chain_id": 8453, "source_network": "Celo", "allowMainnet": True}, "blocker"),
    ({"source_chain_id": 1, "destination_chain_id": 8453, "destination_network": "Base", "allowMainnet": True}, "ok"),
], ids=["base-on-1", "ethereum-on-8453", "celo-on-1", "base-on-8453"])
def test_nd_2_a_name_is_judged_by_the_family_of_the_chain_ids_it_can_denote(tmp_path, doc, expected):
    write(tmp_path, "bridge.json", doc)
    code, out = cli(tmp_path)
    blockers = [f for f in out["findings"] if f["rule"] == "chain-id-name-mismatch" and f["severity"] == "blocker"]
    if expected == "blocker":
        assert code == 1 and out["status"] == "fail" and len(blockers) == 1
    else:
        assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_a_name_label_inside_an_rpc_list_entry_is_not_a_note(tmp_path):
    write(tmp_path, "rpc.json", {"chainId": 11155111, "rpcUrls": [{"name": "Alchemy", "url": "https://sepolia.example.org"}]})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_nd_a_a_name_in_an_object_with_its_own_chain_id_is_judged_by_that_id_not_an_ancestor_leg(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 8453}, "x": {"source_network": "Celo", "source_chain_id": 1}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


def test_nd_b_a_name_on_a_list_item_with_its_own_chain_id_is_not_silenced_by_ancestor_legs(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 8453}, "destination": {"chainId": 1}, "networks": [{"name": "Celo", "chainId": 1}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


def test_nd_c_an_undirected_name_in_an_object_with_its_own_chain_id_is_a_minor_note_despite_ancestor_legs(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "x": {"name": "Base", "chainId": 1}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "minor")]


def test_nd_d_two_legs_with_the_same_rpc_provider_label_are_not_a_network_conflict(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "rpcUrls": [{"name": "Alchemy", "url": "https://eth.example.org"}]},
                               "destination": {"chainId": 8453, "rpcUrls": [{"name": "Alchemy", "url": "https://base.example.org"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_nd_d_one_file_per_network_with_the_same_rpc_label_is_not_a_conflict(tmp_path):
    write(tmp_path, "ethereum.json", {"chainId": 1, "rpcUrls": [{"name": "Alchemy", "url": "https://eth.example.org"}], "allowMainnet": True})
    write(tmp_path, "base.json", {"chainId": 8453, "rpcUrls": [{"name": "Alchemy", "url": "https://base.example.org"}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_nd_e_a_known_network_rpc_label_under_the_wrong_leg_is_a_minor_note_never_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "rpcUrls": [{"name": "Optimism", "url": "https://eth.example.org"}]}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "minor")]


def test_nd_e_a_provider_label_that_names_no_network_is_silent(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "rpcUrls": [{"name": "Alchemy", "url": "https://eth.example.org"}]}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_ndd_1_providers_and_nodes_labels_with_a_url_sibling_are_endpoint_labels_not_network_names(tmp_path):
    write(tmp_path, "providers.json", {"source": {"chainId": 1, "providers": [{"name": "Alchemy", "url": "https://eth.example.org"}]},
                                       "destination": {"chainId": 8453, "providers": [{"name": "Alchemy", "url": "https://base.example.org"}]}, "allowMainnet": True})
    write(tmp_path, "nodes.json", {"source": {"chainId": 1, "nodes": [{"name": "Public Node", "url": "https://eth.example.org"}]},
                                   "destination": {"chainId": 8453, "nodes": [{"name": "Infura", "url": "https://base.example.org"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_ndd_1_a_name_with_no_url_sibling_still_counts_as_a_network_name(tmp_path):
    write(tmp_path, "networks.json", {"chainId": 1, "networks": [{"name": "Sepolia"}]})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


@pytest.mark.parametrize("key", ["providers", "rpcUrls"])
def test_ndd_2_a_known_network_label_under_any_endpoint_list_is_at_most_a_minor_note(tmp_path, key):
    write(tmp_path, "a.json", {"source": {"chainId": 1, key: [{"name": "Base", "url": "https://eth.example.org"}]},
                               "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "minor")]


def test_ndd_3_a_directional_name_matching_neither_same_direction_leg_is_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "y": {"source": {"chainId": 8453}, "z": {"source_network": "Celo"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] and all(
        f["severity"] == "blocker" for f in out["findings"] if f["rule"] == "chain-id-name-mismatch")


def test_ndd_4_a_directional_name_with_no_same_direction_id_is_judged_by_the_own_ids_and_blocks(tmp_path):
    write(tmp_path, "a.json", {"chainId": 1, "destination_chain_id": 8453, "source_network": "Celo", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] and all(
        f["severity"] == "blocker" for f in out["findings"] if f["rule"] == "chain-id-name-mismatch")


def test_a_directional_base_name_on_the_wrong_source_leg_keeps_its_blocker_beside_the_matching_destination(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "source_network": "Base", "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1, "token": {"name": "Celo", "address": "0x" + "ab" * 20}}, "destination": {"chainId": 42220}, "allowMainnet": True},
    {"source": {"chainId": 1, "token": {"name": "Optimism", "address": "0x" + "ab" * 20}}, "destination": {"chainId": 10}, "allowMainnet": True},
    {"source": {"chainId": 8453}, "destination": {"chainId": 1, "token": {"name": "Base", "address": "0x" + "ab" * 20}}, "allowMainnet": True},
], ids=["celo-token-under-1", "optimism-token-under-1", "base-token-under-destination-1"])
def test_nd5_1_a_token_name_is_metadata_and_at_most_a_minor_note(tmp_path, doc):
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd5_2_a_testnet_name_in_a_network_list_entry_with_a_url_sibling_is_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"chainId": 1, "allowMainnet": True, "networks": [{"name": "Sepolia", "rpcUrl": "https://example.com/rpc"}]})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


def test_nd5_2_a_testnet_name_on_a_source_leg_with_no_chain_id_is_judged_against_that_leg(tmp_path):
    write(tmp_path, "a.json", {"source": {"name": "Sepolia", "rpcUrl": "https://example.com/rpc"}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_nd5_3_a_provider_entry_with_its_own_chain_id_is_a_label_checked_by_its_chain_id_only(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "providers": [{"chainId": 8453, "name": "Optimism", "url": "https://base.example.org"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd5_3_a_base_label_under_rpc_urls_on_leg_1_is_at_most_a_minor_note(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "rpcUrls": [{"chainId": 1, "name": "Base", "url": "https://eth.example.org"}]}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_a_provider_brand_beside_a_url_under_another_key_is_a_label_and_names_no_network(tmp_path):
    write(tmp_path, "a.json", {"chainId": 1, "networks": [{"name": "Alchemy", "url": "https://eth.example.org"}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "l2Token": {"name": "Optimism"}}, "allowMainnet": True},
    {"source": {"chainId": 1, "wrappedToken": {"name": "Celo"}}, "destination": {"chainId": 42220}, "allowMainnet": True},
], ids=["l2Token", "wrappedToken"])
def test_nd6_1_a_token_under_a_key_containing_token_is_metadata_and_at_most_a_minor_note(tmp_path, doc):
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd6_2_a_network_object_with_its_own_chain_id_and_an_address_is_not_metadata_so_its_testnet_name_blocks(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "name": "Sepolia", "address": "0x" + "22" * 20}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["rule"], f["severity"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("chain-id-name-mismatch", "blocker")]


def test_nd6_3_a_testnet_label_under_a_mainnet_leg_is_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "providers": [{"name": "Sepolia", "url": "https://example.com/rpc"}]}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_nd6_3_a_testnet_label_with_its_own_mainnet_chain_id_is_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "providers": [{"chainId": 1, "name": "Sepolia", "url": "https://example.com/rpc"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch" and f["location"].endswith("providers[0].chainId")] == ["blocker"]


def test_nd6_3_a_testnet_label_under_a_testnet_leg_is_ok(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 11155111, "providers": [{"name": "Sepolia", "url": "https://example.com/rpc"}]}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd7_1_a_scope_summary_is_computed_once_per_direction_set_and_reused():
    mod = module()
    entries = {(1, frozenset({"source"})): "source.chainId", (11155111, frozenset({"source"})): "source2.chainId", (8453, frozenset({"destination"})): "destination.chainId"}
    scope = mod._Scope(entries, None)
    source = scope.extremes(frozenset({"source"}))
    assert source == ((1, "source.chainId"), (11155111, "source2.chainId"), (1, "source.chainId"), (11155111, "source2.chainId"))
    assert scope.extremes(frozenset({"destination"})) == ((8453, "destination.chainId"), None, (1, "source.chainId"), (11155111, "source2.chainId"))
    assert scope.extremes(frozenset({"source"})) is source and len(scope._extremes) == 2


def test_nd7_1_many_urls_beside_many_legs_still_block_on_the_lowest_matching_chain_id(tmp_path):
    write(tmp_path, "a.json", {"allowMainnet": True, "source": [{"chainId": i} for i in range(1, 301)],
                               "destination": [{"rpcUrl": f"https://h{i}.example.org/rpc"} for i in range(5)] + [{"rpcUrl": "https://rpc.sepolia.org"}]})
    code, out = cli(tmp_path)
    blockers = [f for f in out["findings"] if f["rule"] == "rpc-host-chain-mismatch" and f["severity"] == "blocker"]
    assert code == 1 and out["status"] == "fail" and len(blockers) == 1 and "chain id 1 is a mainnet" in blockers[0]["summary"]


@pytest.mark.parametrize("holder", ["tokenBridge", "contract"])
def test_nd7_2_a_network_object_with_its_own_chain_id_under_a_metadata_word_is_judged_as_a_network(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1, holder: {"chainId": 8453, "name": "Ethereum"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_nd7_2_an_asset_list_entry_with_its_own_chain_id_is_judged_exactly_as_the_plain_key_equivalent(tmp_path):
    write(tmp_path, "asset.json", {"assetChains": [{"chainId": 1, "name": "Base"}], "allowMainnet": True})
    write(tmp_path, "plain.json", {"chains": [{"chainId": 1, "name": "Base"}], "allowMainnet": True})
    code, out = cli(tmp_path)
    found = {file: [(f["rule"], f["severity"]) for f in out["findings"] if f["file"] == file and f["rule"] != "mainnet-without-opt-in"] for file in ("asset.json", "plain.json")}
    assert found["asset.json"] == found["plain.json"] == [("chain-id-name-mismatch", "minor")]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1, "coin": {"name": "Celo"}}, "destination": {"chainId": 42220}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "erc20": {"name": "Optimism"}}, "allowMainnet": True},
], ids=["coin-celo", "erc20-optimism"])
def test_nd7_3_a_token_name_under_an_unlisted_key_is_metadata_and_at_most_a_minor_note(tmp_path, doc):
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd8_1_a_name_summary_is_computed_once_per_scope_and_direction_set():
    mod = module()
    entries = {(1, frozenset({"source"})): "source.chainId", (8453, frozenset({"destination"})): "destination.chainId"}
    scope = mod._Scope(entries, None)
    first = scope.view(frozenset({"destination"}))
    assert list(first.use_iter()) == [(8453, "destination.chainId")]
    assert scope.view(frozenset({"destination"})).index is first.index and scope._index is first.index
    assert list(scope.view(frozenset()).use_iter()) == [(1, "source.chainId"), (8453, "destination.chainId")]


def test_nd8_1_plain_names_on_many_legs_stay_silent_so_the_name_path_is_one_lookup_per_name(tmp_path):
    write(tmp_path, "a.json", {"allowMainnet": True, "source": [{"chainId": i} for i in range(1, 301)], "destination": [{"name": f"dst{i}"} for i in range(300)]})
    code, out = cli(tmp_path)
    assert [f for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == []


def test_nd8_2_a_name_with_many_offending_legs_lists_five_locations_and_states_the_total(tmp_path):
    write(tmp_path, "a.json", {"allowMainnet": True, "source": [{"chainId": i} for i in range(1, 301)], "destination": [{"name": "Celo"}]})
    code, out = cli(tmp_path)
    blockers = [f for f in out["findings"] if f["rule"] == "chain-id-name-mismatch" and f["severity"] == "blocker"]
    assert code == 1 and out["status"] == "fail"
    assert len(blockers) == 5 and len({f["location"] for f in blockers}) == 5
    assert all("300 chain ids of this kind in scope; the first 5 are listed" in f["summary"] for f in blockers)


@pytest.mark.parametrize("holder", ["destinationNetwork", "destination_network", "destinationLegs", "destination"])
def test_nd8_3_a_directional_holder_in_any_spelling_is_network_context_and_blocks_like_the_plain_key(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 8453}, holder: {"name": "Celo"}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_nd8_3_an_unrelated_holder_still_gives_a_label_note(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 8453}, "notes": {"name": "Celo"}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["minor"]


@pytest.mark.parametrize("entry", [
    {"chainId": 1, "name": "Celo", "symbol": "CELO", "address": "0x" + "22" * 20, "decimals": 18},
    {"chainId": 1, "name": "Sepolia Token", "symbol": "STK", "address": "0x" + "22" * 20, "decimals": 18},
], ids=["celo-entry", "sepolia-token-entry"])
def test_nd8_4_a_token_entry_with_its_own_chain_id_is_metadata_and_at_most_a_minor_note(tmp_path, entry):
    write(tmp_path, "a.json", {"tokens": [entry], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd8_4_a_leg_with_its_own_chain_id_and_an_address_under_source_stays_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "name": "Sepolia", "address": "0x" + "22" * 20}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_a_testnet_label_under_an_endpoint_container_with_metadata_keys_is_still_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1, "providers": [{"name": "Sepolia", "url": "https://example.com/rpc", "address": "0x" + "22" * 20}]}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


def test_nd8_5_a_testnet_network_declared_under_a_chain_holder_on_a_mainnet_leg_is_a_blocker(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetChains": [{"name": "Sepolia"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert [(f["severity"], f["location"]) for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == [("blocker", "a.json:destination.chainId")]


@pytest.mark.parametrize("holder", ["assetChains", "chains", "networks", "networkChains", "legs"])
def test_nd8_5_every_chain_or_network_holder_keeps_the_testnet_blocker(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, holder: [{"name": "Sepolia"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["blocker"]


@pytest.mark.parametrize("holder", ["coin", "erc20", "token", "l2Token"])
def test_nd8_5_a_testnet_name_under_a_token_holder_that_is_not_a_chain_holder_is_at_most_a_minor_note(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, holder: {"name": "Sepolia"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd8_5_the_coin_form_of_the_chain_holder_document_gives_at_most_a_minor_note(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "coin": {"name": "Sepolia"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok")
    assert [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"] == ["minor"]


@pytest.mark.parametrize("holder", ["assetChains", "chains", "chain", "networks", "network", "networkChains", "legs", "leg", "sourceLegs"])
def test_nd8_6_a_chain_or_network_holder_word_keeps_the_testnet_blocker(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, holder: [{"name": "Sepolia"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("holder", ["legal", "legacy", "tokenHolder", "unknownHolder"])
def test_nd8_6_a_look_alike_or_unknown_holder_is_not_a_chain_holder_so_its_testnet_name_is_at_most_a_minor_note(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, holder: {"name": "Sepolia"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("holder", ["blockchain", "subchains", "networkName"])
def test_nd8_7_a_chain_or_network_word_anywhere_in_the_holder_keeps_the_testnet_blocker_on_a_mainnet_leg(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1, holder: {"name": "Sepolia"}}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd10_2_a_chainlink_holder_with_shape_keys_declares_no_network(tmp_path):
    """chainlink is a product name, not a chain (R4): a record under it with shape keys and no chain id of its own is metadata, ok."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, "chainlinkFeeds": [{"name": "Sepolia ETH/USD", "address": "0x" + "22" * 20}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd8_7_a_substring_chain_holder_list_under_the_destination_also_blocks(tmp_path):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "subchains": [{"name": "Sepolia"}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("holder", ["legal", "legacy", "legend", "tokenHolder"])
def test_nd8_7_leg_is_a_whole_word_so_look_alike_holders_stay_at_most_a_minor_note(tmp_path, holder):
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, holder: {"name": "Sepolia"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert out["status"] == "ok" and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("holder", ["legal", "legacy", "legend", "unknownHolder"])
def test_nd9_3_a_look_alike_key_under_a_chain_bearing_leg_stays_at_most_a_minor_note_even_one_level_down(tmp_path, holder):
    """ND9-3: a holder's context passes only through an object with no chain id of its own. The destination declares 8453, so its look-alike
    key is not a keyed-map entry, and the object under it does not inherit either."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, holder: {"x": {"name": "Sepolia"}}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 8453}, "destination": {"x": {"name": "Celo"}}, "allowMainnet": True},
    {"chainId": 1, "allowMainnet": True, "networks": {"sepolia": {"name": "Sepolia"}}},
    {"source": {"chainId": 1, "legs": {"a": {"name": "Sepolia"}}}, "destination": {"chainId": 8453}, "allowMainnet": True},
], ids=["destination-keyed", "networks-keyed", "legs-keyed"])
def test_nd9_3_a_keyed_map_under_a_holder_blocks_like_the_list_form(tmp_path, doc):
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd9_3_a_keyed_map_under_an_unknown_root_key_stays_at_most_a_minor_note(tmp_path):
    write(tmp_path, "a.json", {"notes": {"x": {"name": "Celo"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("holder", ["tokenNetworks", "chainlinkFeeds", "tokenChains"])
def test_nd9_2_a_metadata_record_under_a_chain_or_network_named_holder_is_at_most_a_minor_note(tmp_path, holder):
    """A metadata-shape record with no chain id of its own is metadata; its holder's chain or network word does not override that."""
    record = {"name": "Sepolia Token", "symbol": "STK", "address": "0x" + "22" * 20, "decimals": 18}
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, holder: [record]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd9_1_many_name_and_chain_keys_in_one_object_summarise_once(tmp_path, monkeypatch):
    """ND9-1 and ND10-1: the own-chain-id candidates of an object are indexed once, not once per name key."""
    mod = module()
    builds = []
    real = mod._Index

    class Counting(real):
        def __init__(self, entries):
            builds.append(1)
            super().__init__(entries)

    monkeypatch.setattr(mod, "_Index", Counting)
    doc = {"allowMainnet": True}
    for i in range(200):
        tail = "".join("_" if (i >> bit) & 1 else "-" for bit in range(8))
        doc["n" + tail + "ame"] = "Sepolia"
        doc["x" + tail + "chainId"] = [1, 8453][i % 2]
    write(tmp_path, "a.json", doc)
    out = mod.scan(tmp_path, tmp_path)
    assert len(builds) == 1
    assert out["status"] == "fail"


def test_nd9_4_the_counts_hold_every_offender_of_a_name_and_the_findings_list_five(tmp_path):
    write(tmp_path, "a.json", {"allowMainnet": True, "source": [{"chainId": i} for i in range(1, 301)], "destination": [{"name": "Celo"}]})
    code, out = cli(tmp_path)
    assert out["status"] == "fail"
    blockers = [f for f in out["findings"] if f["rule"] == "chain-id-name-mismatch" and f["severity"] == "blocker"]
    assert len(blockers) == 5 and out["counts"]["blocker"] == 300 and out["truncated"] is True


def test_nd10_4_a_single_offender_is_not_truncated(tmp_path):
    write(tmp_path, "a.json", {"allowMainnet": True, "source": {"chainId": 1}, "destination": {"chainId": 8453, "name": "Sepolia"}})
    code, out = cli(tmp_path)
    assert out["status"] == "fail" and out["counts"]["blocker"] == 1 and out["truncated"] is False


def test_nd10_1_names_with_distinct_direction_sets_share_one_index_and_keep_their_verdicts(tmp_path, monkeypatch):
    mod = module()
    builds = []
    real = mod._Index

    class Counting(real):
        def __init__(self, entries):
            builds.append(1)
            super().__init__(entries)

    monkeypatch.setattr(mod, "_Index", Counting)
    words = ["source", "destination", "from", "to", "l1", "l2"]
    doc = {"allowMainnet": True}
    for i in range(64):
        ws = [w for bit, w in enumerate(words) if (i >> bit) & 1]
        pre = "".join(w.capitalize() if j else w for j, w in enumerate(ws))
        doc[(pre + "Name") if ws else "name"] = "Sepolia"
        doc[(pre + "ChainId") if ws else "chainId"] = [1, 8453][i % 2]
    write(tmp_path, "a.json", doc)
    out = mod.scan(tmp_path, tmp_path)
    assert len(builds) == 1
    assert out["status"] == "fail" and out["counts"]["blocker"] == 127


@pytest.mark.parametrize("doc", [
    {"chainId": 1, "allowMainnet": True, "networks": [{"name": "Sepolia", "decimals": 18}]},
    {"chainId": 1, "allowMainnet": True, "networks": [{"name": "Sepolia", "symbol": "ETH"}]},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "legs": [{"name": "Sepolia", "address": "0x" + "22" * 20}]}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetChains": [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True},
], ids=["networks-decimals", "networks-symbol", "legs-address", "assetchains-decimals"])
def test_nd10_2_a_testnet_name_under_a_network_holder_with_shape_keys_is_a_blocker(tmp_path, doc):
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1, "url": [{"name": "Sepolia", "url": "https://example.com/rpc"}]}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"source": {"chainId": 1, "endpoint": [{"name": "Sepolia", "url": "https://example.com/rpc"}]}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"source": {"chainId": 1, "url": {"name": "Sepolia", "url": "https://example.com/rpc"}}, "destination": {"chainId": 8453}, "allowMainnet": True},
], ids=["url-list", "endpoint-list", "url-object"])
def test_nd10_3_a_testnet_label_beside_a_url_under_any_key_on_a_mainnet_leg_is_a_blocker(tmp_path, doc):
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("holder", ["tokenChains", "tokenNetworks"])
def test_nd10_2_a_token_record_with_its_own_chain_id_under_a_chain_named_token_holder_stays_ok(tmp_path, holder):
    record = {"chainId": 8453, "name": "Sepolia Token", "symbol": "STK", "address": "0x" + "22" * 20, "decimals": 18}
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, holder: [record]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


ADDRESS = "0x" + "22" * 20


@pytest.mark.parametrize("shape", [{"address": ADDRESS}, {"decimals": 18}], ids=["address", "decimals"])
def test_nd11_1_a_root_shape_record_is_not_metadata_so_a_testnet_name_on_it_blocks(tmp_path, shape):
    """R1: the root object is never a metadata record. A root with shape keys declares its network, so a testnet name blocks on the mainnet legs."""
    write(tmp_path, "a.json", {"name": "Sepolia Bridge", **shape, "source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1, "assets": [{"chainId": 1, "name": "Sepolia Token", "symbol": "STK", "address": ADDRESS, "decimals": 18}]}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"source": {"chainId": 1, "assetBalance": {"chainId": 1, "name": "Celo", "symbol": "CELO", "address": ADDRESS}}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetInfo": {"chainId": 8453, "name": "Sepolia", "address": ADDRESS}}, "allowMainnet": True},
], ids=["assets-list", "asset-balance", "asset-info"])
def test_nd11_2_a_asset_word_beside_an_own_chain_id_and_shape_keys_is_metadata_as_the_tokens_form_is(tmp_path, doc):
    """R2: an owned token word (asset, assets included) with a chain id of the record and shape keys makes it token metadata: ok."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "blockchains": [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetChains": [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True},
], ids=["blockchains", "asset-chains"])
def test_nd11_3_a_chainless_shape_record_under_a_chain_or_network_holder_declares_its_network_and_blocks(tmp_path, doc):
    """R3: a record with no chain id of its own declares a network when its holder names a chain or network and carries no token word.
    asset is not a token word for this rule, so assetChains blocks too."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd11_4_a_chain_substring_holder_with_shape_keys_declares_its_network_and_blocks(tmp_path):
    """R4: blockchain is a chain holder by substring, so a testnet name in a shape record under it blocks, unlike chainlink (see above)."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, "blockchainFeeds": [{"name": "Sepolia ETH/USD", "address": ADDRESS}]})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd11_5_a_shape_record_with_a_url_is_metadata_and_not_an_endpoint_entry(tmp_path):
    """R5 and ND12-2: a record with shape keys and no chain id is metadata under a token holder, even with a URL key (ND11-5); under any other
    holder the URL makes it an endpoint entry (test_nd12_2)."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True,
                               "tokens": [{"name": "Sepolia Token", "symbol": "STK", "address": ADDRESS, "decimals": 18, "url": "https://example.com/rpc"}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "blockchainLink": {"name": "Sepolia"}}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "blockchainLinks": [{"name": "Sepolia", "decimals": 18}], "allowMainnet": True},
], ids=["blockchainLink-leg", "blockchainLinks-shape"])
def test_nd12_1_a_chain_word_inside_a_longer_word_next_to_chainlink_still_blocks(tmp_path, doc):
    """ND12-1, ND14-1: a chain word inside a longer word next to chainlink still names a chain; a chainlink directly after block is not a product name."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd12_1_a_whole_word_chainlink_before_a_chain_word_still_blocks(tmp_path):
    """ND12-1, ND13-1: a leading chainlink is stripped, so the chain word after it (chainlinkChains) still names a chain and blocks."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "chainlinkChains": [{"name": "Sepolia", "decimals": 18}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1, "endpoint": [{"name": "Sepolia", "url": "https://example.com/rpc", "address": ADDRESS}]}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"source": {"chainId": 1, "endpoint": [{"name": "Sepolia", "url": "https://example.com/rpc", "decimals": 18}]}, "destination": {"chainId": 8453}, "allowMainnet": True},
    {"source": {"chainId": 1, "url": [{"name": "Sepolia", "url": "https://example.com/rpc"}]}, "destination": {"chainId": 8453}, "allowMainnet": True},
], ids=["endpoint-url-address", "endpoint-url-decimals", "url-list"])
def test_nd12_2_an_endpoint_entry_with_a_url_is_checked_as_a_label_whatever_its_shape_keys(tmp_path, doc):
    """ND12-2: shape keys make an entry metadata only under a token holder. Under any other holder a URL makes it an endpoint, so its
    testnet label is checked as ND6-3 requires."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd12_2_a_token_entry_with_an_address_and_url_under_a_chain_1_root_stays_ok(tmp_path):
    """ND12-2 and R5: tokens is a token holder, so an address and a URL on a token entry make it metadata: ok."""
    write(tmp_path, "a.json", {"chainId": 1, "allowMainnet": True, "tokens": [{"name": "Sepolia Token", "address": ADDRESS, "url": "https://example.com/rpc"}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


def test_nd12_3_an_asset_list_with_its_own_chain_id_under_a_chain_holder_keeps_the_testnet_blocker(tmp_path):
    """ND12-3, ND13-2: asset is a token word for a record with its own chain id only when its holder declares no network. assetChains names one, so
    the testnet name blocks (the keys-disagree major may also be listed)."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetChains": [{"chainId": 1, "name": "Sepolia", "decimals": 18}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("key", ["chainlinkFeeds", "chainlinkfeeds", "chainlink"])
def test_nd13_1_a_leading_chainlink_product_name_names_no_chain_whatever_its_case(tmp_path, key):
    """ND13-1: chainlink at the start of a key is a product name, in camel case, lower case or on its own."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, key: [{"name": "Sepolia ETH/USD", "address": ADDRESS}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("key", ["chainlinkchain"])
def test_nd13_1_a_chain_word_right_after_a_leading_chainlink_still_blocks(tmp_path, key):
    """ND13-1: chainlinkchain is the product name followed by a chain word; the chain word counts."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, key: [{"name": "Sepolia", "decimals": 18}]})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd13_1_a_chainlink_later_in_a_key_is_not_a_product_name_so_blockchainlink_style_keys_keep_their_chain(tmp_path):
    """ND13-1, ND14-1: blockChainLink has chainlink directly after block, which is blockchain plus link, so its chain word counts."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, "blockChainLink": {"name": "Sepolia"}}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetLegs": [{"chainId": 1, "name": "Sepolia", "decimals": 18}]}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "assetLeg": {"chainId": 1, "name": "Sepolia", "address": ADDRESS}}, "allowMainnet": True},
], ids=["asset-legs-list", "asset-leg-object"])
def test_nd13_2_an_asset_word_under_a_leg_holder_keeps_the_testnet_blocker(tmp_path, doc):
    """ND13-2: asset counts as a token word for a record with its own chain id only when its holder declares no network. A leg declares one,
    so the testnet name on the record's own chain id blocks (the keys-disagree major may also be listed)."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("doc", [
    {"rpcUrl": "https://example.com/rpc", "address": ADDRESS, "source_network": "Celo", "source": {"chainId": 1}, "allowMainnet": True},
    {"rpcUrl": "https://example.com/rpc", "address": ADDRESS, "destination_network": "Celo", "destination": {"chainId": 8453}, "allowMainnet": True},
], ids=["source-network", "destination-network"])
def test_nd13_3_the_document_root_is_not_an_endpoint_entry_so_its_directional_name_is_judged_as_a_network_name(tmp_path, doc):
    """ND13-3: the root object is never an endpoint entry; it keeps its network context, so a directional network name that matches no
    candidate in its own direction is a blocker."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd13_3_a_url_only_root_gives_no_finding(tmp_path):
    """ND13-3: a root with only a URL key gives no finding, as before the root stopped being an endpoint entry."""
    write(tmp_path, "a.json", {"rpcUrl": "https://example.com/rpc", "source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


@pytest.mark.parametrize("key", ["ethChainlinkFeeds", "primaryChainlinkFeeds", "feedsChainlink"])
def test_nd14_1_a_product_name_chainlink_anywhere_in_a_feed_key_names_no_chain(tmp_path, key):
    """ND14-1, ND14-2: the product name chainlink in its exact spelling is removed wherever it occurs in a key, so a feed list with the name in the middle or at the end is ok."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True, key: [{"name": "Sepolia ETH/USD", "address": ADDRESS}]})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("doc", [
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "blockChainLink": {"name": "Sepolia"}}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453, "blockchainLink": {"name": "Sepolia"}}, "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "blockchainLinks": [{"name": "Sepolia", "decimals": 18}], "allowMainnet": True},
    {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "blockchainFeeds": [{"name": "Sepolia ETH/USD", "address": ADDRESS}], "allowMainnet": True},
], ids=["blockChainLink-leg", "blockchainLink-leg", "blockchainLinks-list", "blockchainFeeds-list"])
def test_nd14_1_a_chainlink_directly_after_block_keeps_its_chain_word_and_blocks(tmp_path, doc):
    """ND14-1: blockchain plus link keeps its chain word, so a testnet name under it on a mainnet leg blocks."""
    write(tmp_path, "a.json", doc)
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd14_1_a_chain_dash_link_and_chain_underscore_link_are_not_the_product_name_and_block(tmp_path):
    """ND14-1 (fail closed): chain-link and chain_link are not the product name, so their chain word counts and a testnet name blocks."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453}, "allowMainnet": True,
                               "chain-link-feeds": [{"name": "Sepolia ETH/USD", "address": ADDRESS}], "chain_link_feeds": [{"name": "Sepolia ETH/USD", "address": ADDRESS}]})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("key", ["ethChainlinkFeeds", "primaryChainlinkFeeds", "feedsChainlink", "chainlinkFeeds", "chainlinkfeeds", "chainlink"])
def test_nd14_2_the_product_name_in_its_exact_spelling_names_no_chain_wherever_it_sits(tmp_path, key):
    """ND14-2: chainlink, Chainlink and CHAINLINK are the product name wherever they occur; a feed list under any of them with a name and an address is ok."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia ETH/USD", "address": ADDRESS}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("key", [
    "crossChainLink", "crosschainlink", "multichainLink", "sidechainLink", "subchainLink", "parachainLink", "metachainLink", "ethChainLink",
    "blockChainLink", "blockchainLink", "chainLinkNetworks", "chainlinkChains", "chainlinkchain", "chain-link-feeds", "chain_link_feeds",
])
def test_nd14_2_a_camel_or_compound_chain_link_keeps_its_chain_word_and_its_testnet_name_blocks(tmp_path, key):
    """ND14-2: ChainLink and chainLink are two words, and a chain compound prefix (block, cross, multi, side, sub, para, meta) before the
    product name keeps its chain word, so a testnet name in such a holder under destination 8453 blocks: fail closed."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("key", ["outsideChainlinkFeeds", "insideChainlinkFeeds", "tradesideChainlinkFeeds", "supershardChainlinkFeeds",
                                 "subscriptionChainlinkFeeds", "polygonChainlinkFeeds", "sidebarChainlinkFeeds"])
def test_nd14_3_a_feed_key_whose_word_before_the_product_name_only_ends_in_a_prefix_names_no_chain(tmp_path, key):
    """ND14-3: a compound prefix counts only as the whole segment before the product name. outside, inside, subscription and the like
    are not one of the prefixes, so the feed list is ok."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia ETH/USD", "address": ADDRESS}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("key", ["crossChainLink", "multiChainlinkFeeds", "shardChainlinkFeeds", "crosschainlink"])
def test_nd14_3_a_compound_prefix_that_is_a_whole_segment_keeps_its_chain_word_and_blocks(tmp_path, key):
    """ND14-3: multi, shard and cross are whole segments before the product name (or all lower case, which fails closed), so the chain word
    counts and a testnet name blocks."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("key", ["crossChainLink", "sidechainLink", "PolyChainlink", "primaryChainLink", "blockchainLink", "chainlinkChains"])
def test_nd14_4_a_name_under_a_holder_that_declares_a_chain_is_checked_against_the_leg_item_chain_id(tmp_path, key):
    """ND14-4: a name under a holder that declares a chain, in an object that carries a chain id in scope, is a network name, so the
    mismatch with chainId 8453 blocks. Before this the name was a label and the item passed."""
    write(tmp_path, "a.json", {"source": [{key: {"name": "Ethereum"}, "chainId": 8453}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("key", ["metaChainlinkFeeds", "multiChainlinkFeeds"])
def test_nd14_4_a_feed_list_under_a_chain_declaring_holder_in_a_leg_item_is_checked_against_the_leg_chain_id(tmp_path, key):
    """ND14-4: the list form of the same rule, a feed list named Ethereum in a leg item whose chain id is 8453, blocks."""
    write(tmp_path, "a.json", {"source": [{key: [{"name": "Ethereum"}], "chainId": 8453}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


def test_nd14_4_a_chain_id_in_a_sibling_child_of_the_leg_item_is_checked_against_the_name(tmp_path):
    """ND14-4: the chain id sits in a sibling child (x) of the leg item, and the name under the chain-declaring holder is still checked."""
    write(tmp_path, "a.json", {"source": [{"crossChainLink": {"name": "Ethereum"}, "x": {"chainId": 8453}}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("chain_id, name", [(1, "Ethereum"), (11155111, "Sepolia")], ids=["ethereum-1", "sepolia-11155111"])
def test_nd14_4_a_matching_name_under_a_chain_declaring_holder_in_a_leg_item_stays_ok(tmp_path, chain_id, name):
    """ND14-4: the same leg item with a matching name and chain id is ok, so the rule adds no false finding."""
    write(tmp_path, "a.json", {"source": [{"crossChainLink": {"name": name}, "chainId": chain_id}], "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("key", ["XMultiChainlink", "ETHSubChainlinkFeeds", "ABCMultiChainlink", "XBlockChainlink", "ETHmultiChainlink"])
def test_nd15_1_a_compound_prefix_after_an_uppercase_run_or_a_capital_keeps_its_chain_word_and_blocks(tmp_path, key):
    """ND15-1: in the boundary regime a prefix is recognised as a suffix that starts outside a lowercase word, so XMulti, ETHSub, ABCMulti,
    XBlock and ETHmulti keep their chain word and a testnet name under them blocks."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]


@pytest.mark.parametrize("key", ["Outside_ChainlinkFeeds", "multi2Chainlink", "multiiChainlink", "xsubChainlink", "tradesideChainlinkFeeds",
                                 "outsideChainlinkFeeds", "polygonChainlinkFeeds", "sidebarChainlinkFeeds"])
def test_nd15_1_a_word_that_only_ends_in_a_prefix_after_a_lowercase_letter_names_no_chain(tmp_path, key):
    """ND15-1: side after a lowercase e, sub after a lowercase x, multi with a digit or an i after it, and the other look-alike words do not
    recognise a compound prefix, so the feed list is ok."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia ETH/USD", "address": ADDRESS}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert (code, out["status"]) == (0, "ok") and not [f for f in out["findings"] if f["severity"] in ("blocker", "major")]


@pytest.mark.parametrize("key", ["xMultiChainlink", "ethMultiChainlink", "ethSubChainlinkFeeds", "ethSideChainlinkFeeds"])
def test_nd15_2_a_compound_prefix_at_a_camel_word_start_keeps_its_chain_word_even_after_a_lowercase_letter(tmp_path, key):
    """ND15-2: a capital starts a camel word, so Multi, Sub and Side after a lowercase letter are a prefix at a word start and keep their chain
    word; the testnet name in such a holder under destination 8453 blocks."""
    write(tmp_path, "a.json", {"source": {"chainId": 1}, "destination": {"chainId": 8453, key: [{"name": "Sepolia", "decimals": 18}]}, "allowMainnet": True})
    code, out = cli(tmp_path)
    assert code == 1 and out["status"] == "fail"
    assert "blocker" in [f["severity"] for f in out["findings"] if f["rule"] == "chain-id-name-mismatch"]
