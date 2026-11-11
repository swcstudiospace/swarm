"""Behaviour tests for scripts/dapp_wallet_check.py (A29) and its Bun twin.

Real-mode tests build fixture trees in tmp_path and assert findings (id, severity, location), exit codes and the JSON
shape; the dry-run contract is asserted separately. No fixture value that looks like a secret is ever expected in the
output: findings carry locations and lengths only.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, run_script

SCRIPT = "dapp_wallet_check.py"
BUN = shutil.which("bun") or "/root/.local/share/reflex/bun/bin/bun"
HAS_BUN = Path(BUN).exists()
INFURA_KEY = "9aa3d95b3bc440fa88ea12eaa4456161"
FAKE_PRIVATE_KEY = "0x" + "ab" * 32
MNEMONIC = "test test test test test test test test test test test junk"


def module():
    spec = importlib.util.spec_from_file_location("dapp_wallet_check_under_test", ROOT / "scripts" / SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def scan(root: Path, *args: str) -> tuple[int, dict, str]:
    result = run_script(SCRIPT, "--root", str(root), "--json", *args)
    return result.returncode, json.loads(result.stdout), result.stdout


def found(report: dict) -> set[tuple[str, str, str]]:
    return {(f["id"], f["severity"], f["location"]) for f in report["findings"]}


# --- dry run ---------------------------------------------------------------------------------------------------


def test_dry_run_is_canned_deterministic_and_reads_nothing(tmp_path):
    missing = tmp_path / "does-not-exist"
    first = run_script(SCRIPT, "--dry-run", "--json", "--root", str(missing))
    second = run_script(SCRIPT, "--dry-run", "--json", "--root", str(missing))
    assert first.returncode == 0 and first.stdout == second.stdout and first.stderr == ""
    report = json.loads(first.stdout)
    assert report["status"] == "ok" and report["dry_run"] is True and report["agent"] == "A29" and report["script"] == "dapp_wallet_check"
    assert report["findings"] == [] and report["counts"] == {"info": 0, "minor": 0, "major": 0, "blocker": 0}
    assert (report["files_scanned"], report["files_skipped"], report["truncated"], report["skipped"]) == (0, 0, False, [])
    assert not missing.exists()
    assert run_script(SCRIPT, "--dry-run", "--json").returncode == 0  # no other argument is needed


# --- real mode: shape, status and exit codes -------------------------------------------------------------------


def test_clean_tree_is_ok_with_exit_zero_and_stable_shape(tmp_path):
    write(tmp_path, "src/app.ts", 'const chainId = await provider.getChainId();\nawait simulateContract(client, request);\nawait writeContract(client, request);\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == []
    assert set(report) == {"findings", "counts", "files_scanned", "files_skipped", "truncated", "skipped", "summary", "status", "agent", "script"}
    assert report["files_scanned"] == 1 and report["counts"] == {"info": 0, "minor": 0, "major": 0, "blocker": 0}
    assert report["summary"] == "1 file(s) scanned, 0 finding(s) (0 major or worse)"


def test_status_fails_on_major_or_worse_but_not_on_minor_or_info(tmp_path):
    write(tmp_path, "a.ts", 'const p = window.ethereum;\nawait signer.sendTransaction(tx);\nconst chainId = 1;\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok"
    assert found(report) == {("DAPP-WINDOW-ETHEREUM", "info", "a.ts:1"), ("DAPP-NO-SIMULATION", "minor", "a.ts:2")}
    write(tmp_path, "b.ts", 'await ethereum.request({ method: "eth_sign" });\n')
    code, report, _ = scan(tmp_path)
    assert code == 1 and report["status"] == "fail" and report["counts"] == {"info": 1, "minor": 1, "major": 0, "blocker": 1}
    assert report["findings"][0]["id"] == "DAPP-ETH-SIGN"  # most severe first


def test_findings_are_sorted_and_output_is_deterministic(tmp_path):
    write(tmp_path, "z.ts", 'x.request({ method: "eth_sign" });\n')
    write(tmp_path, "a.ts", 'x.request({ method: "eth_sign" });\ny.approve(s, MaxUint256);\n')
    _, first, text = scan(tmp_path)
    assert [f["location"] for f in first["findings"]] == ["a.ts:1", "z.ts:1", "a.ts:2"]
    assert scan(tmp_path)[2] == text


# --- real mode: one test per rule family -----------------------------------------------------------------------


def test_eth_sign_and_legacy_typed_data(tmp_path):
    write(tmp_path, "src/sign.ts", 'const a = 1;\nawait provider.request({ method: "eth_sign", params: [account, hash] });\n'
                                   "await provider.request({ method: 'eth_signTypedData', params: [] });\n"
                                   'await provider.request({ method: "eth_signTypedData_v4", params: [] });\n'
                                   'await provider.request({ method: "personal_sign", params: [] });\n')
    _, report, _ = scan(tmp_path)
    assert found(report) >= {("DAPP-ETH-SIGN", "blocker", "src/sign.ts:2"), ("DAPP-TYPED-DATA-LEGACY", "minor", "src/sign.ts:3")}
    assert sum(1 for f in report["findings"] if f["id"] == "DAPP-TYPED-DATA-LEGACY") == 1  # v4 is not the legacy format
    assert sum(1 for f in report["findings"] if f["id"] == "DAPP-ETH-SIGN") == 1  # personal_sign and v4 are fine


def test_unlimited_approval_variants(tmp_path):
    write(tmp_path, "src/approve.ts",
          "await token.approve(spender, ethers.constants.MaxUint256);\n"                       # line 1: ethers v5
          "await token.approve(spender, 1000n);\n"                                              # line 2: exact amount
          "await token.approve(\n  spender,\n  MaxUint256\n);\n"                                # lines 3-6: multi-line
          "await token.increaseAllowance(spender, 2n ** 256n - 1n);\n"                          # line 7
          "await writeContract({ functionName: 'approve', args: [spender, maxUint256] });\n"    # line 8: object style
          "await token.approve(spender, '0x" + "f" * 64 + "');\n"                              # line 9: max as hex
          "const MaxUint256 = 5;\n"                                                             # line 10: no approve call nearby
          "await token.approve(spender, amount);\n"                                             # line 11: the max above is another call
          "const unrelated = MaxUint256;\n")
    _, report, _ = scan(tmp_path)
    flagged = {f["location"] for f in report["findings"] if f["id"] == "DAPP-UNLIMITED-APPROVAL"}
    assert flagged == {"src/approve.ts:1", "src/approve.ts:3", "src/approve.ts:7", "src/approve.ts:8", "src/approve.ts:9"}
    assert {f["severity"] for f in report["findings"] if f["id"] == "DAPP-UNLIMITED-APPROVAL"} == {"major"}


def test_set_approval_for_all_is_a_minor_prompt_to_review(tmp_path):
    write(tmp_path, "nft.tsx", "await nft.setApprovalForAll(operator, true);\n")
    _, report, _ = scan(tmp_path)
    assert found(report) == {("DAPP-APPROVAL-FOR-ALL", "minor", "nft.tsx:1")}


def test_rpc_keys_credentials_and_cleartext_transport(tmp_path):
    write(tmp_path, "src/rpc.ts",
          f'const infura = "https://mainnet.infura.io/v3/{INFURA_KEY}";\n'                                  # 1
          'const alchemy = createPublicClient({ transport: http("https://eth-mainnet.g.alchemy.com/v2/abcDEF1234567890abcdEF1234567890") });\n'  # 2
          'const basic = new JsonRpcProvider("https://user:hunter2@rpc.example.com");\n'                       # 3
          'const clear = new JsonRpcProvider("http://rpc.example.com");\n'                                    # 4
          'const local = new JsonRpcProvider("http://localhost:8545");\n'                                     # 5
          'const query = new JsonRpcProvider("https://rpc.example.com/?apikey=abcdef123456");\n'               # 6
          'const image = "https://cdn.example.com/assets/9aa3d95b3bc440fa88ea12eaa4456161/logo.png";\n'      # 7: not an RPC line
          'const ok = new JsonRpcProvider("https://rpc.ankr.com/eth");\n')                                    # 8
    code, report, text = scan(tmp_path)
    assert found(report) == {
        ("DAPP-RPC-KEY", "major", "src/rpc.ts:1"), ("DAPP-RPC-KEY", "major", "src/rpc.ts:2"), ("DAPP-RPC-CREDENTIALS", "blocker", "src/rpc.ts:3"),
        ("DAPP-RPC-INSECURE", "major", "src/rpc.ts:4"), ("DAPP-RPC-KEY", "major", "src/rpc.ts:6"),
    }
    assert code == 1
    for secret in (INFURA_KEY, "abcDEF1234567890abcdEF1234567890", "hunter2", "abcdef123456"):
        assert secret not in text  # locations and lengths only, never the value
    key_finding = next(f for f in report["findings"] if f["location"] == "src/rpc.ts:1")
    assert "32 chars" in key_finding["summary"]


def test_key_material_storage_and_logging_never_echo_the_value(tmp_path):
    write(tmp_path, "src/keys.ts",
          f'const privateKey = "{FAKE_PRIVATE_KEY}";\n'                       # 1
          f'const cfg = {{ mnemonic: "{MNEMONIC}" }};\n'                       # 2
          "const fromEnv = process.env.PRIVATE_KEY;\n"                         # 3: a reference, not a literal
          "const privateKey2 = loadKey();\n"                                   # 4
          'localStorage.setItem("mnemonic", phrase);\n'                        # 5
          'console.log("debug", privateKey);\n'                                # 6
          'console.log("connected", account);\n'                               # 7
          "const w = new ethers.Wallet(key, provider);\n"                      # 8
          "const acct = privateKeyToAccount(k);\n")                            # 9
    _, report, text = scan(tmp_path)
    assert found(report) == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/keys.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/keys.ts:2"),
        ("DAPP-KEY-STORAGE", "blocker", "src/keys.ts:5"), ("DAPP-KEY-LOG", "blocker", "src/keys.ts:6"),
        ("DAPP-CLIENT-SIGNER", "major", "src/keys.ts:8"), ("DAPP-CLIENT-SIGNER", "major", "src/keys.ts:9"),
    }
    assert FAKE_PRIVATE_KEY not in text and MNEMONIC not in text and "ab" * 32 not in text
    assert "66 chars" in next(f for f in report["findings"] if f["location"] == "src/keys.ts:1")["summary"]


def test_blind_signing_prompts(tmp_path):
    write(tmp_path, "src/login.ts",
          "await signer.signMessage(ethers.utils.arrayify(hash));\n"            # 1
          "await signer.signMessage(getBytes(digest));\n"                       # 2
          "await walletClient.signMessage({ account, message: { raw: hash } });\n"  # 3
          'await signer.signMessage("Sign in to Example");\n'                  # 4: readable text
          "await signer.signMessage(message);\n")                              # 5
    _, report, _ = scan(tmp_path)
    assert found(report) == {("DAPP-BLIND-SIGN", "major", "src/login.ts:1"), ("DAPP-BLIND-SIGN", "major", "src/login.ts:2"),
                             ("DAPP-BLIND-SIGN", "major", "src/login.ts:3")}


def test_missing_chain_check_and_missing_simulation(tmp_path):
    write(tmp_path, "src/send.ts", "const tx = build();\nawait signer.sendTransaction(tx);\nawait signer.sendTransaction(tx2);\n")
    write(tmp_path, "src/typed.ts", "await signer._signTypedData(domain, types, value);\n")
    write(tmp_path, "src/guarded.ts", "const { chainId } = await provider.getNetwork();\nawait signer.sendTransaction(tx);\nawait estimateGas(tx);\n")
    write(tmp_path, "src/viem.ts", "await publicClient.simulateContract(req);\nawait walletClient.writeContract(req);\n")
    _, report, _ = scan(tmp_path)
    assert found(report) == {
        ("DAPP-NO-CHAIN-CHECK", "major", "src/send.ts:2"), ("DAPP-NO-SIMULATION", "minor", "src/send.ts:2"),
        ("DAPP-NO-CHAIN-CHECK", "major", "src/typed.ts:1"), ("DAPP-NO-SIMULATION", "minor", "src/typed.ts:1"),
        ("DAPP-NO-CHAIN-CHECK", "major", "src/viem.ts:2"),
    }


def test_comment_lines_and_declaration_files_are_ignored(tmp_path):
    write(tmp_path, "src/notes.ts", '// await provider.request({ method: "eth_sign" });\n/* token.approve(s, MaxUint256)\n * eth_sign "eth_sign"\n */\n')  # the middle line is inside an open block comment
    write(tmp_path, "src/types.d.ts", 'declare const x: "eth_sign";\nawait y.request({ method: "eth_sign" });\n')
    _, report, _ = scan(tmp_path)
    assert report["findings"] == [] and report["files_scanned"] == 1


# --- real mode: .env, JSON and YAML ----------------------------------------------------------------------------


def test_env_files_flag_only_client_exposed_variables(tmp_path):
    write(tmp_path, ".env.local",
          "NEXT_PUBLIC_PRIVATE_KEY=0xabc123abc123abc123\n"                                           # 1
          f"NEXT_PUBLIC_RPC_URL=https://mainnet.infura.io/v3/{INFURA_KEY}\n"                         # 2
          f"RPC_URL=https://mainnet.infura.io/v3/{INFURA_KEY}\n"                                     # 3: server side, fine
          "VITE_MNEMONIC=\n"                                                                          # 4: empty
          "NEXT_PUBLIC_CHAIN_ID=1\n"                                                                  # 5
          "# NEXT_PUBLIC_SECRET=x\n"                                                                  # 6
          'export VITE_API_SECRET="topsecretvalue"\n')                                                # 7
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-PUBLIC-SECRET-ENV", "blocker", ".env.local:1"), ("DAPP-RPC-KEY", "major", ".env.local:2"),
                             ("DAPP-PUBLIC-SECRET-ENV", "blocker", ".env.local:7")}
    assert "abc123abc123" not in text and "topsecretvalue" not in text and INFURA_KEY not in text
    write(tmp_path, ".env.example", "NEXT_PUBLIC_SEED=word\n")
    assert (".env.example:1" in {f["location"] for f in scan(tmp_path)[1]["findings"]})


def test_json_configuration_is_parsed_by_key_and_value(tmp_path):
    write(tmp_path, "config/chains.json", json.dumps({
        "chains": [{"id": 1, "rpcUrls": [f"https://rpc.example.com/v3/{INFURA_KEY}", "https://u:pw@rpc.example.com", "https://rpc.ankr.com/eth"]}],
        "wallet": {"privateKey": "abcdef0123456789", "mnemonic": "${MNEMONIC}", "seedPhrase": ""},
        "docs": "https://cdn.example.com/9aa3d95b3bc440fa88ea12eaa4456161",
    }))
    write(tmp_path, "config/broken.json", "{ not json")
    _, report, text = scan(tmp_path)
    assert found(report) == {
        ("DAPP-RPC-KEY", "major", "config/chains.json#chains[0].rpcUrls[0]"), ("DAPP-RPC-CREDENTIALS", "blocker", "config/chains.json#chains[0].rpcUrls[1]"),
        ("DAPP-KEY-MATERIAL", "blocker", "config/chains.json"), ("DAPP-CONFIG-PARSE", "info", "config/broken.json"), ("scan/incomplete", "major", "."),
    }
    assert "abcdef0123456789" not in text and INFURA_KEY not in text and "pw@" not in text
    assert "16 chars" in next(f for f in report["findings"] if f["id"] == "DAPP-KEY-MATERIAL")["summary"]


def test_yaml_configuration_when_pyyaml_is_available(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "config.yaml", "rpc_url: https://rpc.example.com/?apikey=abcdef123456\nsigner:\n  private_key: deadbeefdeadbeef\nnote: plain\n")
    write(tmp_path, "bad.yml", "key: [unclosed\n")
    code, report, text = scan(tmp_path)
    assert code == 1 and len(report["skipped"]) == 1 and report["skipped"][0].startswith("bad.yml: skipped:unparsable yml")  # old: skipped == []; the unparsed file is now a reason
    assert found(report) == {("DAPP-RPC-KEY", "major", "config.yaml#rpc_url"), ("DAPP-KEY-MATERIAL", "blocker", "config.yaml"),
                             ("DAPP-CONFIG-PARSE", "info", "bad.yml"), ("scan/incomplete", "major", ".")}
    assert "abcdef123456" not in text and "deadbeefdeadbeef" not in text


def test_yaml_checks_report_tool_missing_when_pyyaml_is_absent(tmp_path, monkeypatch):
    loaded = module()
    monkeypatch.setattr(loaded, "_load_yaml", lambda: None)
    write(tmp_path, "config.yaml", "rpc_url: https://rpc.example.com/?apikey=abcdef123456\n")
    write(tmp_path, "src/a.ts", 'x.request({ method: "eth_sign" });\n')
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["skipped"] == ["yaml-config-structure: skipped:tool-missing (PyYAML is not installed)"]
    assert (report["files_scanned"], report["files_skipped"]) == (1, 1)
    assert [f["id"] for f in report["findings"]] == ["DAPP-ETH-SIGN", "scan/incomplete"] and report["status"] == "fail"  # JSON and code checks stay native
    only_yaml = tmp_path / "only"
    write(only_yaml, "c.yml", "a: b\n")
    clean = loaded.scan_tree(only_yaml, only_yaml)
    assert clean["status"] == "fail" and clean["skipped"] != []  # old: ok; the only in-scope file was not analysed, so the scan is incomplete


# --- real mode: bounds and traversal ----------------------------------------------------------------------------


def test_large_binary_and_ignored_directories_are_skipped(tmp_path):
    hazard = 'x.request({ method: "eth_sign" });\n'
    write(tmp_path, "big.ts", hazard + "// " + "x" * (1 << 20) + "\n")
    (tmp_path / "bin.js").write_bytes(b"\x00\x01" + hazard.encode())
    write(tmp_path, "node_modules/pkg/index.js", hazard)
    write(tmp_path, "dist/bundle.js", hazard)
    write(tmp_path, ".git/hooks/x.js", hazard)
    write(tmp_path, "src/real.ts", hazard)
    write(tmp_path, "notes.txt", hazard)
    _, report, _ = scan(tmp_path)
    assert found(report) == {("DAPP-ETH-SIGN", "blocker", "src/real.ts:1"), ("scan/incomplete", "major", ".")}  # the skipped files make the scan incomplete
    assert (report["files_scanned"], report["files_skipped"]) == (1, 2)  # big.ts and bin.js


def test_file_at_exactly_one_mebibyte_is_scanned(tmp_path):
    line = 'x.request({ method: "eth_sign" });\n'
    body = line + "/" * ((1 << 20) - len(line))
    write(tmp_path, "edge.ts", body)
    assert len(body.encode()) == 1 << 20
    _, report, _ = scan(tmp_path)
    assert report["files_scanned"] == 1 and found(report) == {("DAPP-ETH-SIGN", "blocker", "edge.ts:1")}


def test_scan_stops_at_the_file_limit(tmp_path, monkeypatch):
    loaded = module()
    monkeypatch.setattr(loaded, "MAX_FILES", 3)
    for index in range(5):
        write(tmp_path, f"f{index}.ts", "const a = 1;\n")
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["files_scanned"] == 3 and report["truncated"] is True and "stopped at the 3-file limit" in report["summary"]
    monkeypatch.setattr(loaded, "MAX_FILES", 5)
    assert loaded.scan_tree(tmp_path, tmp_path)["truncated"] is False  # exactly at the limit is not truncated


def test_symlinks_leaving_the_root_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    write(outside, "secret.ts", 'x.request({ method: "eth_sign" });\n')
    write(outside, "dir/inner.ts", 'x.request({ method: "eth_sign" });\n')
    write(root, "src/ok.ts", "const a = 1;\n")
    os.symlink(outside / "secret.ts", root / "src" / "link.ts")
    os.symlink(outside / "dir", root / "linked-dir")
    os.symlink(root / "src" / "ok.ts", root / "src" / "inside.ts")  # a link that stays inside the root is read
    code, report, _ = scan(root)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"] and report["status"] == "fail"  # old: code 0; a skipped link is not ok
    assert report["files_scanned"] == 2 and report["files_skipped"] == 1


def test_path_option_scopes_the_scan_and_rejects_escapes(tmp_path):
    write(tmp_path, "a/x.ts", 'x.request({ method: "eth_sign" });\n')
    write(tmp_path, "b/y.ts", "const ok = 1;\n")
    code, report, _ = scan(tmp_path, "--path", "b")
    assert code == 0 and report["findings"] == [] and report["files_scanned"] == 1
    code, report, _ = scan(tmp_path, "--path", "a/x.ts")
    assert code == 1 and found(report) == {("DAPP-ETH-SIGN", "blocker", "a/x.ts:1")}
    for bad in ("../elsewhere", "missing"):
        code, report, _ = scan(tmp_path, "--path", bad)
        assert code == 2 and report["status"] == "error" and report["error"]["code"] == "E-INPUT"


# --- the shared URL rule ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("url,codes", [
    ("https://rpc.ankr.com/eth", []),
    ("https://eth.llamarpc.com", []),
    (f"https://mainnet.infura.io/v3/{INFURA_KEY}", ["path-key"]),
    ("https://eth-mainnet.g.alchemy.com/v2/abcDEF1234567890abcdEF1234567890", ["path-key"]),
    ("https://nd-123-456-789.p2pify.com/350670db-19fa-4704-a166-e52e178b59d2", ["path-key"]),
    ("https://rpc.example.com/ethereum-mainnet-archive-node", []),
    ("https://rpc.example.com/?apikey=abcdef123456", ["query-key"]),
    ("https://rpc.example.com/?APIKEY=abcdef123456", ["query-key"]),
    ("https://rpc.example.com/?key=short", []),
    ("https://rpc.example.com/?chain=mainnet", []),
    ("https://user:pw@rpc.example.com", ["credentials"]),
    ("https://token@rpc.example.com", ["credentials"]),
    ("http://rpc.example.com", ["insecure-scheme"]),
    ("ws://rpc.example.com", ["insecure-scheme"]),
    ("wss://rpc.example.com", []),
    ("http://localhost:8545", ["local-endpoint"]),
    ("http://127.0.0.1:8545", ["local-endpoint"]),
    ("http://[::1]:8545", ["local-endpoint"]),
    ("ftp://rpc.example.com", ["scheme"]),
])
def test_rpc_url_inspect_rows(url, codes):
    assert [issue["code"] for issue in module().rpc_url_inspect(url)["issues"]] == codes


def test_rpc_url_inspect_malformed_and_details_never_contain_the_secret():
    inspect = module().rpc_url_inspect
    for bad in ("not a url", "https://", "rpc.example.com", "https://rpc.example.com:notaport", "https://exa\nmple.com", ""):
        result = inspect(bad)
        assert result["valid"] is False and [i["code"] for i in result["issues"]] == ["malformed"], bad
    result = inspect(f"https://user:hunter2@mainnet.infura.io/v3/{INFURA_KEY}?apikey=abcdef123456")
    assert [i["code"] for i in result["issues"]] == ["credentials", "path-key", "query-key"]
    assert result["host"] == "mainnet.infura.io" and result["scheme"] == "https"
    blob = json.dumps(result)
    assert "hunter2" not in blob and INFURA_KEY not in blob and "abcdef123456" not in blob
    assert "7 chars" in result["issues"][0]["detail"]  # the password length
    assert inspect("HTTPS://RPC.Example.COM")["host"] == "rpc.example.com"


# --- Bun twin -------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_BUN, reason="bun not installed")
def test_bun_twin_matches_python_in_dry_run_and_real_mode(tmp_path):
    write(tmp_path, "src/a.ts", 'x.request({ method: "eth_sign" });\n')
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    for args in (["--dry-run", "--json"], ["--root", str(tmp_path), "--json"]):
        py = subprocess.run(["python3", str(ROOT / "scripts" / SCRIPT), *args], capture_output=True, text=True, env=env, cwd=ROOT)
        ts = subprocess.run([BUN, str(ROOT / "scripts" / "ts" / "dapp_wallet_check.ts"), *args], capture_output=True, text=True, env=env, cwd=ROOT)
        assert ts.returncode == py.returncode and json.loads(ts.stdout) == json.loads(py.stdout)
    assert json.loads(ts.stdout)["findings"][0]["id"] == "DAPP-ETH-SIGN" and ts.returncode == 1


# --- independent review (A29 reviewer-fixer): regressions with exact values ------------------------------------------

ALCHEMY_STYLE_KEY = "Zu8mQ-aB3x9_QzPLkd0e1f2g3h4i5j6k"


@pytest.mark.parametrize("url,codes", [
    ("http://127.0.0.2:8545", ["local-endpoint"]),
    ("http://127.255.255.254", ["local-endpoint"]),
    ("http://192.168.1.5:8545", ["local-endpoint"]),
    ("http://10.0.0.1", ["local-endpoint"]),
    ("http://172.16.0.9", ["local-endpoint"]),
    ("http://169.254.169.254/latest", ["local-endpoint"]),
    ("http://0.0.0.0:8545", ["local-endpoint"]),
    ("http://[fe80::1]:8545", ["local-endpoint"]),
    ("http://rpc.localhost:8545", ["local-endpoint"]),
    ("http://node:8545", ["local-endpoint"]),
    ("http://printer.local", ["local-endpoint"]),
    ("http://1.1.1.1", ["insecure-scheme"]),
    ("https://8.8.8.8", []),
    ("https://rpc.example.com./eth", []),
    (f"https://eth.example.net/v2/{ALCHEMY_STYLE_KEY}", ["path-key"]),
    ("https://eth.example.net/v2/Zu8mQ_aB3x9-QzPLkd0e1f2g3h4i5j6k7l8m9n0", ["path-key"]),
    ("https://rpc.example.com/ethereum-mainnet-archive", []),
    ("https://rpc.example.com/polygon-mainnet-fullnode-2", []),
    ("https://rpc.example.com/eth_mainnet_archive_node", []),
    ("https://rpc.example.com/v3/eth-sepolia", []),
])
def test_rpc_url_inspect_local_ranges_and_hyphenated_keys(url, codes):
    assert [issue["code"] for issue in module().rpc_url_inspect(url)["issues"]] == codes, url


def test_rpc_url_inspect_trailing_dot_host_is_normalized():
    assert module().rpc_url_inspect("https://RPC.Example.com./eth")["host"] == "rpc.example.com"
    assert module().rpc_url_inspect("https://./x")["valid"] is False


def test_decimal_max_uint256_literal_is_an_unlimited_approval(tmp_path):
    write(tmp_path, "src/a.ts", 'await token.approve(spender, "115792089237316195423570985008687907853269984665640564039457584007913129639935");\n'
                               'await token.approve(spender, 115792089237316195423570985008687907853269984665640564039457584007913129639935n);\n'
                               'await token.approve(spender, "115792089237316195423570985008687907853269984665640564039457584007913129639934");\n'
                               'await token.approve(spender, parseUnits("100", 6));\n')
    _, report, _ = scan(tmp_path)
    assert {(f["id"], f["location"]) for f in report["findings"] if f["id"] == "DAPP-UNLIMITED-APPROVAL"} == {
        ("DAPP-UNLIMITED-APPROVAL", "src/a.ts:1"), ("DAPP-UNLIMITED-APPROVAL", "src/a.ts:2")}


def test_env_inline_comments_do_not_hide_an_rpc_key_or_a_secret_variable(tmp_path):
    write(tmp_path, ".env.production",
          f"VITE_RPC_URL=https://mainnet.infura.io/v3/{INFURA_KEY} # prod\n"                       # 1
          f'NEXT_PUBLIC_RPC="https://eth.example.net/v2/{ALCHEMY_STYLE_KEY}"   # quoted\n'       # 2
          f"VITE_WS_URL=wss://eth.example.net/ws/{ALCHEMY_STYLE_KEY}\n"                            # 3
          f"VITE_SECRET_KEY=abcdef0123456789 # {INFURA_KEY}\n"                                     # 4
          "VITE_BASE=https://rpc.example.com/path#anchor\n"                                       # 5: a # without a space is part of the value
          "VITE_NOTE=plain # comment\n")                                                          # 6
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-RPC-KEY", "major", ".env.production:1"), ("DAPP-RPC-KEY", "major", ".env.production:2"),
                             ("DAPP-RPC-KEY", "major", ".env.production:3"), ("DAPP-PUBLIC-SECRET-ENV", "blocker", ".env.production:4")}
    for secret in (INFURA_KEY, ALCHEMY_STYLE_KEY, "abcdef0123456789"):
        assert secret not in text
    assert "16 chars" in next(f for f in report["findings"] if f["location"].endswith(":4"))["summary"]  # the value without its comment


def test_findings_output_is_capped_but_counts_stay_complete(tmp_path):
    for index in range(600):
        write(tmp_path, f"src/f{index:04d}.ts", 'await provider.request({ method: "eth_sign" });\n')
    code, report, text = scan(tmp_path)
    assert code == 1 and report["counts"]["blocker"] == 600
    assert len(report["findings"]) == 500 and report["truncated"] is True
    assert report["skipped"] == ["findings: output capped at 500 of 600 (counts are complete)"]
    assert len(text) < 256 * 1024
    assert report["summary"] == "600 file(s) scanned, 600 finding(s) (600 major or worse); only the 500 most severe are listed"


def test_script_never_echoes_secrets_from_any_source_kind(tmp_path):
    canary_url = f"https://user:hunter2Zq8@node.example.org/v2/{ALCHEMY_STYLE_KEY}?apikey=AKIAZ7Q2X5LMN3B8T6YR"
    canary_key = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
    write(tmp_path, "src/a.ts", f'const rpc = new JsonRpcProvider("{canary_url}");\nconst privateKey = "0x{canary_key}";\nconst mnemonic = "{MNEMONIC}";\n'
                                f'localStorage.setItem("privateKey", "{canary_key}");\n')
    write(tmp_path, ".env", f"NEXT_PUBLIC_PRIVATE_KEY={canary_key}\nVITE_RPC={canary_url}\n")
    write(tmp_path, "c.json", json.dumps({"rpcUrl": canary_url, "wallet": {"privateKey": canary_key, "mnemonic": MNEMONIC}}))
    _, report, text = scan(tmp_path)
    assert report["counts"]["blocker"] >= 6
    for secret in ("hunter2Zq8", ALCHEMY_STYLE_KEY, "AKIAZ7Q2X5LMN3B8T6YR", canary_key, MNEMONIC):
        assert secret not in text, secret


def test_long_minified_lines_scan_in_linear_time(tmp_path):
    # unbounded `[^)]*` rules were quadratic: one 1 MiB line of repeated calls took minutes
    line = ("localStorage.setItem(x," * 45000)[:300_000] + "\nconsole.log(x," * 1 + ("console.log(x," * 60000)[:100_000] + "\n"
    write(tmp_path, "public/vendor.min.js", line + ("signMessage({" * 60000)[:100_000] + "\n")
    import time
    start = time.monotonic()
    code, report, _ = scan(tmp_path)
    assert time.monotonic() - start < 20 and code == 0 and report["files_scanned"] == 1
    write(tmp_path, "public/hit.js", "console.log('privateKey', k); localStorage.setItem('mnemonic', m); signMessage({ raw: h });\n")
    ids = {f["id"] for f in scan(tmp_path)[1]["findings"]}
    assert {"DAPP-KEY-LOG", "DAPP-KEY-STORAGE", "DAPP-BLIND-SIGN"} <= ids


def test_secret_looking_mapping_keys_are_never_echoed_in_finding_locations(tmp_path):
    token_key, hex_key = "AKIAZ7Q2X5LMN3B8T6YR", "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
    write(tmp_path, "c.json", json.dumps({"keys": {token_key: {"privateKey": "abcdef0123456789"}, hex_key: {"rpcUrl": f"https://rpc.example.com/v3/{INFURA_KEY}"}}}))
    _, report, text = scan(tmp_path)
    assert {f["id"] for f in report["findings"]} == {"DAPP-KEY-MATERIAL", "DAPP-RPC-KEY"}
    for secret in (token_key, hex_key, INFURA_KEY, "abcdef0123456789"):
        assert secret not in text
    assert "keys.<key 20 chars>.privateKey" in text


CREDENTIALED_KEYS = [
    ("body", {"https://user:password@rpc.example": {"rpcUrl": "https://rpc.example/?apikey=abcd1234"}}),
    ("url-value", {"https://alice:hunter2pass@rpc.example.org": {"url": "https://alice:hunter2pass@rpc.example.org/x"}}),
    ("key-material", {"https://alice:hunter2pass@rpc.example.org": {"privateKey": "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"}}),
]


@pytest.mark.parametrize("label,doc", CREDENTIALED_KEYS, ids=[label for label, _ in CREDENTIALED_KEYS])
def test_credentialed_urls_used_as_json_mapping_keys_are_never_printed(tmp_path, label, doc):
    write(tmp_path, "cfg.json", json.dumps(doc))
    code, report, text = scan(tmp_path)
    assert code == 1 and report["findings"]
    for secret in ("hunter2pass", "password@", "abcd1234", "4c0883a6"):
        assert secret not in text, secret
    assert any("<key " in f["location"] + f["summary"] for f in report["findings"])


def test_credentialed_url_key_in_yaml_is_never_printed(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "cfg.yml", '"https://user:password@rpc.example":\n  rpc: "https://rpc.example/?apikey=abcd1234"\n')
    code, report, text = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["DAPP-RPC-KEY"]
    assert "password@" not in text and "abcd1234" not in text
    assert report["findings"][0]["location"] == "cfg.yml#<key 33 chars>.rpc"


def test_a_plain_url_mapping_key_is_still_shown_in_its_location(tmp_path):
    write(tmp_path, "cfg.json", json.dumps({"https://rpc.example.org": {"rpcUrl": "https://rpc.example.org/?apikey=abcd1234"}}))
    _, report, _ = scan(tmp_path)
    assert {f["location"] for f in report["findings"] if f["id"] == "DAPP-RPC-KEY"} == {"cfg.json#https://rpc.example.org.rpcUrl"}


def test_yaml_alias_cycle_does_not_stop_the_scan(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "config.yaml", "loop: &x {again: *x}\nrpc_url: https://rpc.example.com/?apikey=abcdef123456\n")
    code, report, text = scan(tmp_path)
    assert code == 1 and report["status"] == "fail"
    assert found(report) == {("DAPP-RPC-KEY", "major", "config.yaml#rpc_url")}
    assert "abcdef123456" not in text


def test_alias_reached_under_two_keys_is_checked_under_each_key(tmp_path):
    pytest.importorskip("yaml")
    write(tmp_path, "config.yaml", "shared: &cfg {primary: 'https://rpc.example.com/?apikey=abcdef123456'}\nrpc: {ethereum: *cfg}\n")
    _, report, text = scan(tmp_path)
    assert ("DAPP-RPC-KEY", "major", "config.yaml#rpc.ethereum.primary") in found(report)
    assert "abcdef123456" not in text


def test_values_shared_through_many_aliases_stop_at_the_budget_and_findings_stay_capped(tmp_path):
    pytest.importorskip("yaml")
    shared = "".join(f'  privateKey{i}: "abcdef0123456789"\n' for i in range(10_000))
    fanout = "".join("- *shared\n" for _ in range(2_000))
    write(tmp_path, "config.yaml", f"shared: &shared\n{shared}items:\n{fanout}")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / "swarm-state")}
    # Unbounded work would stall here; the timeout turns that regression into a failure instead of a hung suite.
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / SCRIPT), "--root", str(tmp_path), "--json"], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=60)
    report = json.loads(proc.stdout)
    assert proc.returncode == 1 and report["status"] == "fail" and report["truncated"] is True
    assert len(report["findings"]) == 500 and report["counts"]["blocker"] > 500
    assert report["counts"]["info"] == 1  # the DAPP-CONFIG-PARSE that stopped the walk
    assert "abcdef0123456789" not in proc.stdout


# --- one-number IPv4 hosts ---------------------------------------------------------------------------------------------


def test_decimal_and_hex_ipv4_hosts_are_classed_as_addresses_not_single_label_names():
    inspect = module().rpc_url_inspect
    assert [i["code"] for i in inspect("http://134744072/rpc")["issues"]] == ["insecure-scheme"]  # decimal 8.8.8.8 is public
    assert [i["code"] for i in inspect("https://134744072/rpc")["issues"]] == []
    assert [i["code"] for i in inspect("http://0x08080808/rpc")["issues"]] == ["insecure-scheme"]  # hex 8.8.8.8
    assert [i["code"] for i in inspect("http://127.0.0.1:8545")["issues"]] == ["local-endpoint"]
    assert [i["code"] for i in inspect("http://2130706433:8545")["issues"]] == ["local-endpoint"]  # decimal 127.0.0.1
    assert [i["code"] for i in inspect("http://4294967296/rpc")["issues"]] == ["insecure-scheme"]  # beyond 32 bits: not a local address


# --- QA-03 remediation: regressions for the blocker and major fixes ----------------------------------------------------


def test_hash_prefixed_and_camel_case_key_literals_are_reported(tmp_path):
    key = "0x" + "ab" * 32
    write(tmp_path, "src/signer.ts", f'class Signer {{\n  #privateKey = "{key}";\n}}\n')
    write(tmp_path, "src/a.ts", f'const deployerPrivateKey = "{key}";\nconst MY_PRIVATE_KEY = "{key}";\nconst signerSecretKey = "{key}";\n')
    write(tmp_path, "src/m.ts", f'const deployerMnemonic = "{MNEMONIC}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/signer.ts:2"), ("DAPP-KEY-MATERIAL", "blocker", "src/a.ts:1"),
        ("DAPP-KEY-MATERIAL", "blocker", "src/a.ts:2"), ("DAPP-KEY-MATERIAL", "blocker", "src/a.ts:3"),
        ("DAPP-KEY-MATERIAL", "blocker", "src/m.ts:1"),
    }
    assert key not in text and MNEMONIC not in text


def test_every_key_literal_on_one_line_is_counted(tmp_path):
    key = "0x" + "ab" * 32
    write(tmp_path, "src/cfg.ts", f'const cfg = {{ privateKey: "{key}", mnemonic: "{MNEMONIC}" }};\n')
    _, report, text = scan(tmp_path)
    assert [f["id"] for f in report["findings"]] == ["DAPP-KEY-MATERIAL", "DAPP-KEY-MATERIAL"] and report["counts"]["blocker"] == 2
    assert key not in text and MNEMONIC not in text


def test_url_named_public_variables_are_not_secret_findings(tmp_path):
    write(tmp_path, ".env.local", "REACT_APP_SEED_PHRASE_URL=https://ok.example.org\nREACT_APP_SEED=abcdef0123456789\n")
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-PUBLIC-SECRET-ENV", "blocker", ".env.local:2")} and "abcdef0123456789" not in text


def test_url_scheme_is_read_when_urlsplit_refuses_the_url():
    loaded = module()
    assert loaded.url_scheme_userinfo("javascript://[%0aalert(document.cookie)") == ("javascript", "")
    assert loaded.url_scheme_userinfo("\x01 VBS\tcript://[x") == ("vbscript", "")
    assert loaded.url_scheme_userinfo("https://user@rpc.example.com") == ("https", "plain")


def test_rpc_scheme_detail_never_echoes_the_scheme_text():
    token = "sk1a2b3c4d5e6f7g8h9i0jKLMNOP"
    issues = module().rpc_url_inspect(f"{token}://polygon-rpc.com")["issues"]
    assert [i["code"] for i in issues] == ["scheme"] and token.lower() not in json.dumps(issues)


def test_unparsable_config_makes_the_scan_incomplete(tmp_path):
    write(tmp_path, "config/broken.json", "{ not json")
    code, report, _ = scan(tmp_path)
    assert code == 1 and report["status"] == "fail"
    assert found(report) == {("DAPP-CONFIG-PARSE", "info", "config/broken.json"), ("scan/incomplete", "major", ".")}
    assert report["skipped"] == ["config/broken.json: skipped:unparsable json (JSONDecodeError)"]


def test_scan_with_no_analysed_input_is_not_ok(tmp_path):
    write(tmp_path, "README.md", "# nothing in scope\n")
    code, report, _ = scan(tmp_path)
    assert code == 1 and report["status"] == "fail" and report["files_scanned"] == 0
    assert found(report) == {("scan/incomplete", "major", ".")}


def test_joined_string_literals_are_one_key_literal(tmp_path):
    write(tmp_path, "src/join.ts", 'const privateKey = "0x" + "ab12ab12ab12ab12";\nconst short = "0x" + "ab12";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/join.ts:1")}  # "0x" + "ab12" is 6 chars, below the key length
    assert "18 chars" in report["findings"][0]["summary"] and "ab12ab12" not in text


def test_key_named_value_that_is_not_a_plain_literal_makes_the_scan_incomplete(tmp_path):
    write(tmp_path, "src/deploy.ts", 'const deployerPrivateKey = "0x" + "ab12"*16;\n')  # the report's exact shape
    code, report, text = scan(tmp_path)
    assert code == 1 and report["status"] == "fail"
    incomplete = [f for f in report["findings"] if f["id"] == "scan/incomplete"]
    assert len(incomplete) == 1 and incomplete[0]["severity"] == "major"
    assert "src/deploy.ts:1" in incomplete[0]["summary"]  # file and line named, never the value
    assert "ab12" not in text


def test_env_references_type_names_and_nullish_values_are_not_incomplete(tmp_path):
    write(tmp_path, "src/ok.ts", "const privateKey = process.env.PRIVATE_KEY;\nconst mnemonic = null;\ninterface W { privateKey: string; mnemonic?: string }\n")
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == []


# --- QA-03 re-review: N1 to N8 ----------------------------------------------------------------------------------------------

KEY_HEX = "0x" + "ab" * 32


def test_key_literal_after_a_leading_block_or_html_comment_is_scanned(tmp_path):  # N1
    write(tmp_path, "src/block.ts", f'/*! lib */ var cfg = {{privateKey: "{KEY_HEX}"}};\n')
    write(tmp_path, "src/page.html", f'<!-- note --> <script>const privateKey = "{KEY_HEX}";</script>\n')
    write(tmp_path, "src/only-comment.ts", f'/* const privateKey = "{KEY_HEX}"; */\n')  # still inside the comment: not code
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/block.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/page.html:1")}
    assert KEY_HEX not in text


def test_json_key_with_digits_and_dotted_key_name_are_checked(tmp_path):  # N2 and N3
    write(tmp_path, "config/keys.json", json.dumps({"deployerPrivateKey2026": KEY_HEX, "privateKey.dev": KEY_HEX}))
    write(tmp_path, "src/keys.ts", f'const deployerPrivateKey2026 = "{KEY_HEX}";\n')  # the source path agrees with the JSON path
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "config/keys.json"), ("DAPP-KEY-MATERIAL", "blocker", "src/keys.ts:1")}
    assert report["counts"]["blocker"] == 3 and report["status"] == "fail"
    assert KEY_HEX not in text and "deployerPrivateKey2026" not in text  # the rendered path shows the length, not the name


@pytest.mark.parametrize("source", [
    "export function make(seed: number) {}\n",  # N4: a type annotation
    "const seed = 42;\n",  # N4: a number
    "const secret = getSecret();\n",  # N4: a call
])
def test_key_named_non_literal_values_give_no_reason(tmp_path, source):  # N4
    write(tmp_path, "src/a.ts", source)
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == []


def test_seed_named_phrase_is_a_mnemonic_blocker(tmp_path):  # N5
    write(tmp_path, "src/seed.ts", f'const seed = "{MNEMONIC}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/seed.ts:1")} and MNEMONIC not in text


def test_secret_named_hex_key_is_a_key_blocker(tmp_path):  # N5
    write(tmp_path, "src/secret.ts", f'const secret = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/secret.ts:1")} and KEY_HEX not in text


def test_unlistable_directory_is_recorded_as_incomplete(tmp_path, monkeypatch):  # N6
    loaded = module()
    write(tmp_path, "src/ok.ts", "const a = 1;\n")
    write(tmp_path, "locked/k.ts", f'const privateKey = "{KEY_HEX}";\n')
    locked = str(tmp_path / "locked")
    real_scandir = os.scandir

    def refusing_scandir(path="."):
        if os.fspath(path) == locked:
            raise PermissionError(13, "Permission denied", locked)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", refusing_scandir)  # in-process: the subprocess CLI would not see the patch
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["status"] == "fail" and found(report) == {("scan/incomplete", "major", ".")}
    assert "locked: skipped:unlistable-directory (PermissionError)" in report["skipped"]
    assert KEY_HEX not in json.dumps(report)


def test_symlinked_directory_is_recorded_as_skipped(tmp_path):  # N7
    outside, root = tmp_path / "outside", tmp_path / "root"
    write(outside, "s.ts", f'const privateKey = "{KEY_HEX}";\n')
    write(root, "src/ok.ts", "const a = 1;\n")
    os.symlink(outside, root / "linked")
    code, report, text = scan(root)
    assert code == 1 and report["status"] == "fail" and found(report) == {("scan/incomplete", "major", ".")}
    assert "linked: skipped:symlink-directory" in report["skipped"]
    assert KEY_HEX not in text


def test_symlink_loop_is_recorded_and_the_other_files_are_scanned(tmp_path):  # N8
    write(tmp_path, "src/real.ts", 'x.request({ method: "eth_sign" });\n')
    os.symlink(tmp_path / "src" / "b.ts", tmp_path / "src" / "a.ts")
    os.symlink(tmp_path / "src" / "a.ts", tmp_path / "src" / "b.ts")
    code, report, _ = scan(tmp_path)  # exit 1 with a report: a loop is not an E-error exit 2
    assert code == 1 and report["status"] == "fail" and report["files_scanned"] == 1
    assert found(report) == {("DAPP-ETH-SIGN", "blocker", "src/real.ts:1"), ("scan/incomplete", "major", ".")}
    assert {"src/a.ts: skipped:symlink-loop", "src/b.ts: skipped:symlink-loop"} <= set(report["skipped"])


# --- JSON and source agree on key names and length rules ------------------------------------------------------------------

TWELVE_WORD_PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def test_json_seed_holding_a_twelve_word_phrase_is_a_mnemonic_blocker(tmp_path):
    write(tmp_path, "config/seed.json", json.dumps({"seed": TWELVE_WORD_PHRASE}))
    write(tmp_path, "config/numbers.json", json.dumps({"seed": 42, "secret": None}))  # non-string values are not key material
    code, report, text = scan(tmp_path)
    assert code == 1 and found(report) == {("DAPP-KEY-MATERIAL", "blocker", "config/seed.json")}
    assert f"({len(TWELVE_WORD_PHRASE)} chars" in report["findings"][0]["summary"] and TWELVE_WORD_PHRASE not in text
    assert report["skipped"] == [] and report["status"] == "fail"


def test_json_secret_holding_a_hex_key_is_a_key_blocker(tmp_path):
    write(tmp_path, "config/secret.json", json.dumps({"secret": KEY_HEX}))
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "config/secret.json")} and KEY_HEX not in text


# --- re-review: ND1 to ND4 and ND6 ----------------------------------------------------------------------------------------------


def test_key_looking_locators_and_collections_are_not_key_material(tmp_path):  # ND1
    locators = ["privateKeyPath", "seedNodes", "secretName", "seedUrls", "PRIVATE_KEY_URL", "seedUrl"]
    write(tmp_path, "src/locators.ts", "".join(f'const {name} = "{KEY_HEX}";\n' for name in locators))
    write(tmp_path, "config/locators.json", json.dumps({name: KEY_HEX for name in locators}))
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == []
    flagged = {"privateKey": KEY_HEX, "deployerPrivateKey2026": KEY_HEX, "MY_PRIVATE_KEY": KEY_HEX, "seed": KEY_HEX, "secret": KEY_HEX,
               "seedPhrase": MNEMONIC}
    write(tmp_path, "src/flagged.ts", "".join(f'const {name} = "{value}";\n' for name, value in flagged.items()))
    write(tmp_path, "config/flagged.json", json.dumps(flagged))
    _, report, text = scan(tmp_path)
    assert report["counts"]["blocker"] == 2 * len(flagged) and report["status"] == "fail"
    assert KEY_HEX not in text and MNEMONIC not in text


def test_string_under_a_nested_object_is_checked_against_its_own_key_only(tmp_path):  # ND2
    client = {"clientId": "a1b2c3d4e5f6a7b8c9", "redirectUri": "https://app.example.com/callback"}
    write(tmp_path, "config/oauth.json", json.dumps({"secret": client}))
    write(tmp_path, "src/oauth.ts", f"const secret = {json.dumps(client)};\n")  # the source shape of the same object
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_generator_method_line_is_code_not_a_comment_continuation(tmp_path):  # ND3
    write(tmp_path, "src/gen.ts", f'class Store {{\n  *gen() {{ const privateKey = "{KEY_HEX}"; }}\n}}\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/gen.ts:2")} and KEY_HEX not in text


def test_call_argument_fragments_are_not_part_of_the_value(tmp_path):  # ND4
    write(tmp_path, "src/call.ts", 'const secret = getSecret("api-" + env);\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["skipped"] == [] and report["findings"] == []


def test_top_level_fragment_still_gives_the_reason(tmp_path):  # ND4
    write(tmp_path, "src/top.ts", 'const secret = "api-" + env;\n')
    code, report, _ = scan(tmp_path)
    assert code == 1 and report["status"] == "fail" and [f["id"] for f in report["findings"]] == ["scan/incomplete"]
    assert "src/top.ts:1" in report["findings"][0]["summary"]


def test_path_to_a_symlink_loop_is_recorded_as_not_analysed(tmp_path):  # ND6
    os.symlink(tmp_path / "a.ts", tmp_path / "b.ts")
    os.symlink(tmp_path / "b.ts", tmp_path / "a.ts")
    code, report, _ = scan(tmp_path, "--path", "a.ts")  # exit 1 with a report, not an internal error (exit 2)
    assert code == 1 and report["status"] == "fail" and "error" not in report
    assert found(report) == {("scan/incomplete", "major", ".")} and report["skipped"] == ["a.ts: skipped:symlink-loop"]


# --- ND1 follow-up: plural, Key-suffixed and underscore names are key material; locator and collection names are not -------------
LITERAL_16 = "abcdef0123456789"  # 16 characters, no whitespace: a key-length literal under a bare seed or secret name

def test_plural_key_suffixed_and_underscore_names_with_a_literal_are_flagged(tmp_path):
    write(tmp_path, "src/suffix.ts", f'const privateKeys = "{KEY_HEX}";\nconst seedKey = "{LITERAL_16}";\n'
                                     f'const secrets = "{LITERAL_16}";\nconst seed_key = "{LITERAL_16}";\n')
    write(tmp_path, "config/suffix.json", json.dumps({"privateKeys": [KEY_HEX], "seedKey": LITERAL_16, "secrets": LITERAL_16, "seed_key": LITERAL_16}))
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", f"src/suffix.ts:{n}") for n in (1, 2, 3, 4)} | {
        ("DAPP-KEY-MATERIAL", "blocker", "config/suffix.json")}
    assert report["counts"]["blocker"] == 8 and report["status"] == "fail"  # four source lines, four JSON keys
    assert KEY_HEX not in text and LITERAL_16 not in text


def test_locator_and_collection_names_are_not_flagged(tmp_path):
    names = ["seedNodes", "seedUrls", "secretName", "privateKeyPath"]
    write(tmp_path, "src/locators.ts", "".join(f'const {name} = "{KEY_HEX}";\n' for name in names))
    write(tmp_path, "config/locators.json", json.dumps({name: KEY_HEX for name in names}))
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_plain_key_names_stay_flagged(tmp_path):
    names = {"privateKey": KEY_HEX, "deployerPrivateKey2026": KEY_HEX, "MY_PRIVATE_KEY": KEY_HEX, "seed": KEY_HEX}
    write(tmp_path, "src/plain.ts", "".join(f'const {name} = "{value}";\n' for name, value in names.items()))
    write(tmp_path, "config/plain.json", json.dumps(names))
    _, report, text = scan(tmp_path)
    assert report["counts"]["blocker"] == 2 * len(names) and report["status"] == "fail"
    assert KEY_HEX not in text


# --- source lists under key names: each element gets the scalar rule ---------------------------------------------------------------


def test_key_named_source_list_of_hex_literals_is_flagged(tmp_path):
    write(tmp_path, "src/list.ts", f'const privateKeys = ["{KEY_HEX}"];\n')
    write(tmp_path, "src/typed.ts", f'const privateKeys: string[] = ["{KEY_HEX}"];\n')  # a TypeScript annotation before the list
    write(tmp_path, "src/multi.ts", f'const privateKeys = [\n  // "{KEY_HEX}" retired\n  "{KEY_HEX}", // primary\n];\n')  # multi-line, comments
    write(tmp_path, "config/list.json", json.dumps({"privateKeys": [KEY_HEX]}))  # the JSON array form, unchanged
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/list.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/typed.ts:1"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/multi.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "config/list.json")}
    assert report["counts"]["blocker"] == 4 and report["status"] == "fail" and KEY_HEX not in text


def test_key_named_list_of_calls_stays_ok(tmp_path):
    write(tmp_path, "src/calls.ts", "const privateKeys = [getKey(1)];\n")  # non-literal elements: no finding, no reason
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_string_list_under_a_non_key_name_is_not_checked(tmp_path):
    write(tmp_path, "src/nodes.ts", 'const seedNodes = ["https://node.example.org/abcdef0123456789abcdef"];\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_joinable_fragment_element_gives_the_scalar_reason(tmp_path):  # the element is a concatenation, not a plain literal
    write(tmp_path, "src/fragment.ts", 'const privateKeys = ["0x" + "ab"*32];\n')
    code, report, _ = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"] and "src/fragment.ts:1" in report["skipped"][0]


def test_key_named_list_not_closed_within_the_window_gives_a_reason(tmp_path):  # fail closed rather than read past the window
    write(tmp_path, "src/long.ts", "const privateKeys = [\n" + "".join(f"  getKey({index}),\n" for index in range(210)) + "];\n")
    code, report, _ = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"]
    assert "src/long.ts:1: key-named list is not closed within 200 lines" in report["skipped"][0]


# --- round three: NX1 to NX7 ------------------------------------------------------------------------------------------------------


def test_list_element_type_assertions_are_stripped_before_the_literal_is_read(tmp_path):  # NX1
    write(tmp_path, "src/as-hex.ts", f'const privateKeys = ["{KEY_HEX}" as Hex];\n')
    write(tmp_path, "src/as-const.ts", f'const privateKeys = ["{KEY_HEX}" as const];\n')
    write(tmp_path, "src/junk.ts", f'const privateKeys = ["{KEY_HEX}" junk];\n')  # anything else after the literal: a reason, never silence
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/as-hex.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/as-const.ts:1"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/junk.ts:1"), ("scan/incomplete", "major", ".")}  # round six: the key literal blocks even before junk
    assert [s.split(":")[0] for s in report["skipped"]] == ["src/junk.ts"] and KEY_HEX not in text


def test_block_comment_between_separator_and_value_is_skipped(tmp_path):  # NX2, both forms
    write(tmp_path, "src/list.ts", f'const privateKeys = /* prod */ ["{KEY_HEX}"];\n')
    write(tmp_path, "src/scalar.ts", f'const secret = /* x */ "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/list.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/scalar.ts:1")}
    assert KEY_HEX not in text


def test_value_after_a_closed_multi_line_comment_or_on_the_next_line_is_flagged(tmp_path):  # decision 2: the value is read, not refused
    write(tmp_path, "src/open.ts", f'const secret = /* x\n */ "{KEY_HEX}";\n')  # the comment is closed on the next line
    write(tmp_path, "src/next.ts", f'const secret =\n  "{KEY_HEX}";\n')
    write(tmp_path, "src/slashes.ts", f'const secret = // note\n  "{KEY_HEX}";\n')  # a // comment does not end the search
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/open.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/next.ts:1"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/slashes.ts:1")}
    assert KEY_HEX not in text


def test_list_on_the_next_line_is_flagged(tmp_path):  # decision 2
    write(tmp_path, "src/list.ts", f'const secrets =\n  ["{KEY_HEX}"];\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/list.ts:1")} and KEY_HEX not in text


def test_value_after_an_unclosed_block_comment_gives_a_reason(tmp_path):  # decision 2: where the comment ends is unknown
    write(tmp_path, "src/unclosed.ts", f'const secret = /* open\n  "{KEY_HEX}";\n')
    code, report, text = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"]
    assert "src/unclosed.ts:1: key-named assignment's value follows a block comment that is not closed" in report["skipped"][0]
    assert KEY_HEX not in text


def test_url_values_are_locators_whatever_the_name(tmp_path):  # NX3
    url = "https://example.org/some/long/path/value"
    write(tmp_path, "src/urls.ts", f'const secrets = ["{url}"];\n')
    write(tmp_path, "src/seeds.ts", f'const seeds = ["{url}", "{KEY_HEX}"];\n')  # the hex key in the same list still gives one
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/seeds.ts:1")}
    assert url not in text and KEY_HEX not in text


def test_dotted_keys_are_judged_whole_in_source_and_json(tmp_path):  # NX4
    write(tmp_path, "src/path.ts", 'const x = { "privateKey.path": "./keys/deployer.json" };\n')  # a locator suffix: not key material
    write(tmp_path, "src/dev.ts", f'const y = {{ "privateKey.dev": "{KEY_HEX}" }};\n')  # a non-locator segment: key material
    write(tmp_path, "config/path.json", json.dumps({"privateKey.path": "./keys/deployer.json"}))
    write(tmp_path, "config/dev.json", json.dumps({"privateKey.dev": KEY_HEX}))
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/dev.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "config/dev.json")}
    assert KEY_HEX not in text


def test_hyphenated_key_names_match_in_source_and_json(tmp_path):  # NX5
    write(tmp_path, "src/hyphen.ts", f"const x = {{ 'private-key': \"{KEY_HEX}\" }};\n")
    write(tmp_path, "config/hyphen.json", json.dumps({"private-key": KEY_HEX}))
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/hyphen.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "config/hyphen.json")}
    assert KEY_HEX not in text


def test_clean_link_with_a_trailing_newline_passes_and_an_inner_newline_is_flagged(tmp_path):  # NX6 (tool-level, spec-based)
    tools = importlib.import_module("swarm.tools.a29_tools")

    def links(*values):
        return tools.signing_request_scan({"requests": [], "known_contracts": [], "expected_domains": ["app.example.com"],
                                           "link_urls": list(values), "hardcoded_addresses": []})

    clean = links("https://app.example.com/login\n")
    assert clean["verdict"] == "pass" and clean["findings"] == []
    inner = links("https://app.example.com\n.evil.io/x")
    assert inner["verdict"] == "block" and {(f["id"], f["severity"]) for f in inner["findings"]} == {
        ("LINK-SPOOF", "blocker"), ("LINK-CONTROL-CHARACTER", "major")}


def test_nested_key_named_list_element_is_read_as_a_list(tmp_path):  # NX7
    write(tmp_path, "src/nested.ts", f'const privateKeys = [["{KEY_HEX}"]];\n')
    write(tmp_path, "src/nested-call.ts", "const privateKeys = [[getKey(1)]];\n")  # a call inside a nested list: nothing
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/nested.ts:1")} and KEY_HEX not in text
    assert report["skipped"] == []


def test_scalar_type_assertions_are_stripped_before_the_literal_is_read(tmp_path):  # decision 1
    write(tmp_path, "src/label.ts", 'const secret = "a plain label" as const;\n')  # a plain label: nothing
    write(tmp_path, "src/hex.ts", f'const privateKey = "{KEY_HEX}" as Hex;\n')  # a hex key: the blocker
    write(tmp_path, "src/junk.ts", 'const secret = "abc" junk;\n')  # anything else after the literal: the reason, never silence
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/hex.ts:1"), ("scan/incomplete", "major", ".")}
    assert [s.split(":")[0] for s in report["skipped"]] == ["src/junk.ts"] and KEY_HEX not in text


def test_typed_scalar_value_is_read(tmp_path):  # `privateKey: string = "…"` was silent before the value search followed annotations
    write(tmp_path, "src/typed.ts", f'const privateKey: string = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/typed.ts:1")} and KEY_HEX not in text


# --- round four: NV1 to NV6 ----------------------------------------------------------------------------------------------------


def test_key_split_across_lines_by_plus_is_read_as_one_literal(tmp_path):  # NV1: a continuation is part of the expression
    write(tmp_path, "src/end.ts", f'const privateKey = "0x" +\n  "{KEY_HEX[2:]}";\n')
    write(tmp_path, "src/start.ts", f'const privateKey = "0x"\n  + "{KEY_HEX[2:]}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/end.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/start.ts:1")}
    assert report["skipped"] == [] and KEY_HEX[2:] not in text


def test_split_expression_containing_a_call_gives_the_reason(tmp_path):  # NV1: a non-literal part gives the reason
    write(tmp_path, "src/split-call.ts", 'const privateKey = "0x" +\n  getKey();\n')
    code, report, _ = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"]
    assert "src/split-call.ts:1: key-named assignment is not a plain string literal" in report["skipped"][0]


def test_concatenation_with_a_call_across_lines_gives_the_reason(tmp_path):  # NV2: the whole top-level expression is read
    write(tmp_path, "src/concat.ts", 'const secret = getKey() +\n  "api-";\n')
    code, report, _ = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"]
    assert "src/concat.ts:1" in report["skipped"][0]


def test_typed_declarations_with_complex_types_are_read(tmp_path):  # NV3
    write(tmp_path, "src/newline.ts", f'const privateKey: Hex\n  = "{KEY_HEX}";\n')  # the `=` is on the next line
    write(tmp_path, "src/parens.ts", f'const privateKey: (Hex) = "{KEY_HEX}";\n')
    write(tmp_path, "src/generic.ts", f"const privateKey: Foo<'x'> = \"{KEY_HEX}\";\n")
    write(tmp_path, "src/member.ts", "interface Wallet {\n  privateKey: string;\n}\n")  # an interface member has no value: silent
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/newline.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/parens.ts:1"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/generic.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_sentences_and_lowercase_placeholders_are_not_key_material(tmp_path):  # NV4
    write(tmp_path, "src/sentence.ts", 'const secret = "the quick brown fox jumps over";\n')  # six words: not a mnemonic
    write(tmp_path, "src/label.ts", 'const secret = "Your secret is safe with us here";\n')
    write(tmp_path, "src/placeholder.ts", 'const secret = "your-private-key-here-placeholder";\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_mnemonic_hex_and_mixed_case_keys_are_still_blockers(tmp_path):  # NV4
    mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    write(tmp_path, "src/seed.ts", f'const seed = "{mnemonic}";\n')  # twelve lowercase words under a bare name
    write(tmp_path, "src/hex.ts", f'const secret = "{KEY_HEX}";\n')
    write(tmp_path, "src/mixed.ts", 'const secret = "Qz8fLk3mNp0aRt7vXy2bWc9dEf4gHj1k";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/seed.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/hex.ts:1"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/mixed.ts:1")}
    assert mnemonic not in text and KEY_HEX not in text


def test_literal_followed_by_a_method_call_gives_nothing(tmp_path):  # NV6: a non-literal expression, as a call is
    write(tmp_path, "src/trim.ts", 'const privateKeys = ["abc".trim()];\n')
    write(tmp_path, "src/junk.ts", 'const privateKeys = ["abc" junk];\n')  # junk after the literal still gives the reason
    code, report, _ = scan(tmp_path)
    assert code == 1 and [f["id"] for f in report["findings"]] == ["scan/incomplete"]
    assert [s.split(":")[0] for s in report["skipped"]] == ["src/junk.ts"]


def test_unclosed_list_openers_each_give_one_reason_within_the_window(tmp_path):  # NV5: the window bound holds per opener
    write(tmp_path, "src/openers.ts", "".join(f"const privateKeys{index} = [\n" for index in range(40)))
    code, report, _ = scan(tmp_path)
    assert code == 1 and report["status"] == "fail"
    assert len(report["skipped"]) == 40
    assert all("is not closed within 200 lines" in reason for reason in report["skipped"])


# --- round five: decision 3, the scan-wide window-line budget -----------------------------------------------------------------------


def test_scan_line_budget_stops_the_key_line_readers_and_skips_later_files(tmp_path, monkeypatch):  # decision 3
    loaded = module()
    monkeypatch.setattr(loaded, "SCAN_LINE_BUDGET", 200)  # round six: units are characters read plus window lines; a.ts costs 78 of them
    write(tmp_path, "a.ts", f'const privateKeys = [\n  "{KEY_HEX}",\n];\n')  # two window lines: fits, and is analysed
    write(tmp_path, "b.ts", "const privateKeys = [\n" + "".join('  "x",\n' for _ in range(100)) + "];\n")  # needs 100 lines: runs out
    write(tmp_path, "c.ts", f'const secret = "{KEY_HEX}";\n')  # read after the budget ran out: not read
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "a.ts:1"), ("scan/incomplete", "major", ".")}
    assert report["skipped"] == ["scan-line-budget-spent:b.ts", "not-read-after-scan-line-budget:1-files"]
    assert report["files_scanned"] == 2 and report["status"] == "fail" and KEY_HEX not in json.dumps(report)


def test_skipped_reasons_are_listed_to_fifty_with_an_exact_remainder(tmp_path):  # the listing cap; the status counts every reason
    write(tmp_path, "src/many.ts", "".join(f"const privateKeys{index} = [\n" for index in range(120)))
    code, report, _ = scan(tmp_path)
    assert code == 1 and report["status"] == "fail"
    assert len(report["skipped"]) == 51
    assert all("key-named list is not closed within 200 lines" in reason for reason in report["skipped"][:50])
    assert report["skipped"][50] == "… and 70 more not listed"
    assert report["summary"].startswith("1 file(s) scanned")


# --- round six: NW1 to NW6 -----------------------------------------------------------------------------------------------------


def test_separator_on_a_following_line_is_matched_in_both_forms(tmp_path):  # NW1
    write(tmp_path, "src/scalar.ts", f'const secret\n  = "{KEY_HEX}";\n')
    write(tmp_path, "src/object.ts", f'const cfg = {{\n  secret\n  : "{KEY_HEX}"\n}};\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/scalar.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/object.ts:2")}
    assert KEY_HEX not in text


def test_key_literal_as_fallback_in_parentheses_after_a_cast_or_before_an_operator_is_key_material(tmp_path):  # NW2
    write(tmp_path, "src/or.ts", f'const privateKey = "{KEY_HEX}" || process.env.PK;\n')
    write(tmp_path, "src/nullish.ts", f'const privateKey = "{KEY_HEX}" ?? "";\n')
    write(tmp_path, "src/paren.ts", f'const privateKey = ("{KEY_HEX}");\n')
    write(tmp_path, "src/cast.ts", f'const privateKey = <Hex>"{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", f"src/{name}.ts:1") for name in ("or", "nullish", "paren", "cast")}
    assert KEY_HEX not in text


def test_key_literal_followed_by_a_member_call_is_key_material_and_a_plain_string_is_not(tmp_path):  # NW3
    write(tmp_path, "src/slice.ts", f'const privateKey = "{KEY_HEX}".slice(2);\n')
    write(tmp_path, "src/trim.ts", 'const privateKeys = ["abc".trim()];\n')  # NV6: an ordinary string and a method call give nothing
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/slice.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_key_names_with_value_suffixes_are_matched_and_locators_are_not(tmp_path):  # NW4
    write(tmp_path, "src/suffix.ts", "".join(f'const {name} = "{KEY_HEX}";\n' for name in ("privateKeyHex", "privateKeyBytes", "secretHex")))
    write(tmp_path, "config/suffix.json", json.dumps({"privateKeyHex": KEY_HEX, "privateKeyBytes": KEY_HEX, "secretHex": KEY_HEX}))
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/suffix.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/suffix.ts:2"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/suffix.ts:3"), ("DAPP-KEY-MATERIAL", "blocker", "config/suffix.json")}
    assert KEY_HEX not in text
    locators = tmp_path / "locators"
    names = ["seedNodes", "seedUrls", "secretName", "privateKeyPath", "PRIVATE_KEY_URL", "seedUrl"]
    write(locators, "src/locators.ts", "".join(f'const {name} = "{KEY_HEX}";\n' for name in names))
    write(locators, "config/locators.json", json.dumps({name: KEY_HEX for name in names}))
    code, report, _ = scan(locators)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_mnemonic_shape_needs_a_seed_mnemonic_or_phrase_name(tmp_path):  # NW5
    sentence = "please do not share this value with anyone at all day long"
    mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    write(tmp_path, "src/secret.ts", f'const secret = "{sentence}";\n')  # twelve lowercase words under a bare name: nothing
    write(tmp_path, "src/seed.ts", f'const seed = "{mnemonic}";\n')  # the same shape under a seed name: a blocker
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/seed.ts:1")} and mnemonic not in text


def test_line_with_twenty_thousand_key_matches_is_read_once_and_the_budget_bounds_it(tmp_path, monkeypatch):  # NW6
    loaded = module()
    line = "secret=a " * 20000 + "\n"
    write(tmp_path, "src/many.ts", line)
    # counted: a linear reader spends about a fifth of a unit per character on this line (40,002 units for 180,001 characters at HEAD)
    monkeypatch.setattr(loaded, "SCAN_LINE_BUDGET", 2 * len(line))
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["status"] == "ok" and report["skipped"] == []
    monkeypatch.setattr(loaded, "SCAN_LINE_BUDGET", 1000)  # the budget bounds the work: a low budget gives the budget reason
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["status"] == "fail" and report["skipped"] == ["scan-line-budget-spent:src/many.ts"]


# --- round seven: ND1 to ND5 ---------------------------------------------------------------------------------------------------------


def test_key_names_inside_template_literals_are_not_occurrences(tmp_path):  # ND1: a template is a string, its key names open nothing
    write(tmp_path, "src/templates.ts", "const hint = `Enter your secret:`;\n"
                                        "const msg = `Your secret: ${value} is ready`;\n"
                                        "const s = `privateKey: stored locally`;\n")
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []
    write(tmp_path, "src/real.ts", "const hint = `Enter your secret:`;\n" f'const secret = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/real.ts:2")} and KEY_HEX not in text


def test_trailing_comment_does_not_hide_a_separator_on_the_next_line(tmp_path):  # ND2: the comment is stripped before the line ends
    write(tmp_path, "src/line.ts", f'const secret // note\n  = "{KEY_HEX}";\n')
    write(tmp_path, "src/block.ts", f'const secret /* note */\n  = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/line.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/block.ts:1")}
    assert KEY_HEX not in text


def test_commented_out_assignments_after_code_are_silent(tmp_path):  # ND3: text inside a comment is never code
    write(tmp_path, "src/comments.ts", f'const a = 1; // privateKey: "{KEY_HEX}";\n'
                                       f'const a = 1; /* privateKey: "{KEY_HEX}" */\n'
                                       f'const privateKey = "{KEY_HEX}";\n')  # the same assignment outside a comment still blocks
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/comments.ts:3")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_a_mnemonic_is_key_material_under_key_kind_names_but_a_bare_sentence_is_not(tmp_path):  # ND4
    mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    write(tmp_path, "src/key.ts", f'const privateKey = "{mnemonic}";\n')
    write(tmp_path, "src/secret-key.ts", f'const secretKey = "{mnemonic}";\n')
    write(tmp_path, "src/list.ts", f'const privateKeys = ["{mnemonic}"];\n')
    write(tmp_path, "src/sentence.ts", 'const secret = "please do not share this value with anyone at all day long";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/key.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/secret-key.ts:1"),
                             ("DAPP-KEY-MATERIAL", "blocker", "src/list.ts:1")}
    assert mnemonic not in text and report["skipped"] == []


def test_plural_key_with_a_value_suffix_matches_like_the_singular(tmp_path):  # ND5
    write(tmp_path, "src/plural.ts", f'const privateKeysHex = "{KEY_HEX[2:]}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/plural.ts:1")} and KEY_HEX[2:] not in text
    locators = tmp_path / "locators"
    names = ["seedNodes", "seedUrls", "secretName", "privateKeyPath", "PRIVATE_KEY_URL", "seedUrl"]
    write(locators, "src/locators.ts", "".join(f'const {name} = "{KEY_HEX}";\n' for name in names))
    code, report, _ = scan(locators)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_an_unclosed_apostrophe_in_prose_does_not_mask_a_key_assignment_on_its_line(tmp_path):  # the residual: an unclosed quote is text
    write(tmp_path, "src/prose.html", f'<p>Don\'t</p> const secret = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/prose.html:1")} and KEY_HEX not in text


def test_a_closed_string_containing_an_apostrophe_still_masks_its_own_contents(tmp_path):  # a closed string is a string
    write(tmp_path, "src/closed.ts", 'const msg = "Don\'t share your secret: now";\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


# --- round seven: D1 to D8 -----------------------------------------------------------------------------------------------------------


def test_quotes_in_prose_do_not_mask_a_key_assignment_after_them(tmp_path):  # D1: a quote after a word is text, not a literal
    write(tmp_path, "src/prose.html", f'<p>Don\'t</p> const privateKey = "{KEY_HEX}";\n'
                                      f'<p>He said "hi</p> const privateKey2 = "{KEY_HEX}";\n')
    write(tmp_path, "src/tail.ts", f'const privateKey = "{KEY_HEX}" and it\'s fine\n')
    write(tmp_path, "src/press.html", "<p>Press ` to copy</p>\n" + "".join(f'const privateKey{i} = "{KEY_HEX}";\n' for i in range(3)))
    _, report, text = scan(tmp_path)
    blockers = {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"}  # the tail's reason is a scan/incomplete finding, not a blocker
    assert blockers == {("DAPP-KEY-MATERIAL", "blocker", "src/prose.html:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/prose.html:2"),
                        ("DAPP-KEY-MATERIAL", "blocker", "src/tail.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/press.html:2"),
                        ("DAPP-KEY-MATERIAL", "blocker", "src/press.html:3"), ("DAPP-KEY-MATERIAL", "blocker", "src/press.html:4")}
    assert KEY_HEX not in text


def test_a_backtick_after_an_equals_sign_still_opens_a_template(tmp_path):  # D1: the key inside the template is text, not an assignment
    write(tmp_path, "src/tpl.ts", f'const x = `\nprivateKey: "{KEY_HEX}"\n`;\n')
    code, report, text = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == [] and KEY_HEX not in text  # no markup: the key is template text


def test_a_slash_slash_after_a_colon_is_a_url_and_a_comment_after_code_is_still_one(tmp_path):  # D2
    write(tmp_path, "src/page.html", f'<p>See https://docs.example.com</p><script>const privateKey = "{KEY_HEX}";</script>\n')
    write(tmp_path, "src/comment.ts", f'const a = 1; // privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/page.html:1")} and KEY_HEX not in text


def test_a_key_assignment_inside_a_template_substitution_is_code(tmp_path):  # D4: the braces of ${...} are balanced and read as code
    write(tmp_path, "src/sub.ts", f'const x = `${{ (privateKey = "{KEY_HEX}") }}`;\n'
                                  f'const y = `${{ {{ a: 1 }}.a }}`;\n'
                                  f'const privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/sub.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/sub.ts:3")}
    assert KEY_HEX not in text


def test_the_trailing_type_assertion_is_stripped_in_one_pass(tmp_path, monkeypatch):  # D6: a strip that does not backtrack
    loaded = module()
    assert loaded._strip_type_assertion('"0x" as const') == '"0x"'
    assert loaded._strip_type_assertion('"0x" satisfies Hex') == '"0x"'
    assert loaded._strip_type_assertion('"0x" as a as b!') == '"0x" as a as b!'
    assert loaded._strip_type_assertion('"0x" as ') == '"0x" as '


def test_a_list_nested_to_depth_eight_is_read_and_its_reads_are_charged(tmp_path, monkeypatch):  # D7
    loaded = module()
    write(tmp_path, "src/nested.ts", "const privateKeys = " + "[" * 8 + f'"{KEY_HEX}"' + "]" * 8 + ";\n")
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/nested.ts:1")} and report["skipped"] == []
    monkeypatch.setattr(loaded, "SCAN_LINE_BUDGET", 300)  # the flat read costs 71 units, the nested reads 617 in all: the budget runs out
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["status"] == "fail" and report["skipped"] == ["scan-line-budget-spent:src/nested.ts"]


def test_an_html_comment_is_a_comment_only_in_markup_files(tmp_path):  # D8
    line = f'const a = b <!-- c; const privateKey = "{KEY_HEX}";\n'
    write(tmp_path, "src/code.ts", line)
    write(tmp_path, "src/page.html", line)
    _, report, text = scan(tmp_path)
    assert found(report) == {("DAPP-KEY-MATERIAL", "blocker", "src/code.ts:1")} and KEY_HEX not in text


def test_a_jsx_tag_close_does_not_open_a_literal_so_a_key_between_quotes_still_blocks(tmp_path):  # bare > is not an opener
    write(tmp_path, "src/jsx.html", f'<p>"a</p> <p>"b</p> const privateKey = "{KEY_HEX}";\n')
    write(tmp_path, "src/jsx-between.html", f'<p>"a</p> const privateKey = "{KEY_HEX}"; <p>"b</p>\n')
    _, report, text = scan(tmp_path)
    blockers = {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"}
    assert blockers == {("DAPP-KEY-MATERIAL", "blocker", "src/jsx.html:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/jsx-between.html:1")}
    assert KEY_HEX not in text


def test_an_arrow_before_a_quote_still_opens_a_literal(tmp_path):  # the arrow => is an opener; a key-named arrow value keeps its result
    write(tmp_path, "src/get.ts", f'const get = () => "secret: {KEY_HEX}";\n')  # the literal is masked: no key occurrence inside it
    write(tmp_path, "src/arrow.ts", f'const privateKey = () => "{KEY_HEX}";\n')  # unchanged by the opener rule: still a blocker
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {("DAPP-KEY-MATERIAL", "blocker", "src/arrow.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_a_key_assignment_inside_a_quote_that_prose_opened_is_reported_not_hidden(tmp_path):  # NX-M1: fail closed, no value shown
    write(tmp_path, "src/prose.html", f'<p>x = "y</p> const privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set()
    assert "src/prose.html:1: possible key assignment inside a quoted span at line 1: not analysed" in report["skipped"]
    assert KEY_HEX not in text


def test_ui_text_that_names_a_key_word_still_passes(tmp_path):  # NX-M1: the text after the key name is not a quoted key-shaped literal
    write(tmp_path, "src/ui.ts", 'const msg = "Missing seed: set SEED";\nconst s = "privateKey: stored locally";\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_a_slash_slash_after_a_colon_that_is_not_a_scheme_starts_a_comment(tmp_path):  # NX-M2: `c://` is a comment, `https://` a URL
    write(tmp_path, "src/label.ts", f'const label = b ? c:// note privateKey = "{KEY_HEX}";\n')
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_a_regex_literal_hides_its_slashes_from_the_comment_rule(tmp_path):  # NX-M3: the // and /* in a regex are not comments
    write(tmp_path, "src/url.ts", f'const isUrl = /^https?:\\/\\//.test(s); const privateKey = "{KEY_HEX}";\n')
    write(tmp_path, "src/star.ts", f'const r = /a\\/*b/; const privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/url.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/star.ts:1")}
    assert KEY_HEX not in text


def test_a_division_is_not_a_regex_and_a_comment_after_code_is_still_blanked(tmp_path):  # NX-M3: the slash after a word divides
    write(tmp_path, "src/half.ts", f'const half = total / 2; const privateKey = "{KEY_HEX}";\n')
    write(tmp_path, "src/note.ts", f'const a = 1; // note privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {("DAPP-KEY-MATERIAL", "blocker", "src/half.ts:1")}
    assert KEY_HEX not in text


def test_a_plain_string_or_template_that_names_a_key_is_not_reported(tmp_path):  # NM1b: no markup inside the literal, so no reason
    write(tmp_path, "src/plain.ts", f"const s = \"privateKey: '{KEY_HEX}'\";\n"
                                    f'const doc = `{{"privateKey": "{KEY_HEX}"}}`;\n'
                                    f'const note = `Your secret = "{KEY_HEX}"\n`;\n')
    code, report, text = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == [] and KEY_HEX not in text


def test_a_spaced_less_than_before_a_slash_starts_a_regex_and_a_closing_tag_does_not(tmp_path):  # NM3b
    write(tmp_path, "src/ok.ts", f'const ok = x < /https?:\\/\\//.test(s); const privateKey = "{KEY_HEX}";\n')
    write(tmp_path, "src/tags.html", f'<p>a</p> const privateKey = "{KEY_HEX}"; <p>b</p>\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/ok.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/tags.html:1")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_a_slash_after_an_increment_or_decrement_divides(tmp_path):  # NM3c: ++ and -- end an operand, so the / that follows is division
    write(tmp_path, "src/inc.ts", f'let n = count++ / 2; const privateKey = "{KEY_HEX}"; const r = a / b;\n')
    write(tmp_path, "src/dec.ts", f'let m = count-- / 2; const privateKey = "{KEY_HEX}"; const q = a / b;\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/inc.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/dec.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_a_stray_quote_with_markup_elsewhere_on_the_line_is_reported_not_hidden(tmp_path):  # NX-M1c: markup anywhere on the line counts
    write(tmp_path, "src/tag.html", f'<p>Note</p>: "hi const privateKey = "{KEY_HEX}";\n')
    write(tmp_path, "src/fragment.tsx", f'<>Note: "hi const privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set()
    assert "src/tag.html:1: possible key assignment inside a quoted span at line 1: not analysed" in report["skipped"]
    assert "src/fragment.tsx:1: possible key assignment inside a quoted span at line 1: not analysed" in report["skipped"]
    assert KEY_HEX not in text


def test_the_suspect_reason_fires_once_per_markup_line_and_a_plain_line_gives_none(tmp_path):  # NX-M1d: the line's markup is read once
    write(tmp_path, "src/tag.html", f'<p>x = "y</p> const privateKey = "{KEY_HEX}";\n' f'<p>x = "y</p> const privateKey = "{KEY_HEX}";\n')
    write(tmp_path, "src/plain.ts", f'const s = "privateKey: \'{KEY_HEX}\'";\n')
    _, report, text = scan(tmp_path)
    assert sum(s.startswith("src/tag.html:1:") for s in report["skipped"]) == 1
    assert sum(s.startswith("src/tag.html:2:") for s in report["skipped"]) == 1
    assert not any(s.startswith("src/plain.ts:") for s in report["skipped"]) and KEY_HEX not in text


def test_a_long_line_of_keys_inside_one_literal_is_scanned_in_linear_time(tmp_path, monkeypatch):  # NX-M1d: was quadratic
    loaded = module()
    keys = 32000
    write(tmp_path, "src/many.ts", 'const s = "' + "secret: a " * keys + '"\n')
    classified = []
    real_name_kinds = loaded._name_kinds

    def counted(word):
        classified.append(word)
        return real_name_kinds(word)

    monkeypatch.setattr(loaded, "_name_kinds", counted)
    report = loaded.scan_tree(tmp_path, tmp_path)
    assert report["status"] == "ok" and report["skipped"] == []
    # counted instead of timed: each key occurrence is classified once (about 32,000 calls); a rescan per key adds calls
    assert keys <= len(classified) <= keys + 2, len(classified)


def test_a_value_ending_in_a_closing_angle_at_end_of_file_is_closed(tmp_path):  # NX-M1e: > is not a binary operator, so the value closes
    write(tmp_path, "src/cast.ts", f'const privateKey = "{KEY_HEX}" as Hex >\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {("DAPP-KEY-MATERIAL", "blocker", "src/cast.ts:1")}
    assert not any("not closed" in s for s in report["skipped"]) and KEY_HEX not in text


def test_a_value_ending_in_a_binary_operator_at_end_of_file_stays_open(tmp_path):  # NX-M1e: fail closed, as before
    write(tmp_path, "src/plus.ts", f'const privateKey = "{KEY_HEX}" +\n')
    _, report, text = scan(tmp_path)
    assert [s for s in report["skipped"] if s.startswith("src/plus.ts:1:")] == [
        "src/plus.ts:1: key-named assignment's value is not closed within 200 lines (value not checked)"]
    assert KEY_HEX not in text


def test_a_complete_jsx_last_line_gives_no_reason(tmp_path):  # NX-M1e: a closing /> at end of file is complete
    write(tmp_path, "src/view.tsx", "<Input secret={s} />\n")
    code, report, _ = scan(tmp_path)
    assert code == 0 and report["status"] == "ok" and report["findings"] == [] and report["skipped"] == []


def test_a_jsx_self_closing_tag_at_a_line_break_does_not_join_the_next_line(tmp_path):  # NX-M1f: the blocker is on the literal's own line
    write(tmp_path, "src/view.tsx", f'<Input secret={{s}} />\nconst privateKey = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {("DAPP-KEY-MATERIAL", "blocker", "src/view.tsx:2")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_a_bare_greater_than_at_a_line_break_ends_the_value(tmp_path):  # NX-M1f: one blocker per literal, each at its own line
    write(tmp_path, "src/gt.ts", f'const privateKey = "{KEY_HEX}" >\nconst secret = "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/gt.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/gt.ts:2")}
    assert KEY_HEX not in text


def test_an_arrow_at_a_line_break_still_joins_the_next_literal(tmp_path):  # NX-M1f: the arrow continues, so the value is judged once
    write(tmp_path, "src/arrow.ts", f'const privateKey = () =>\n  "{KEY_HEX}";\n')
    _, report, text = scan(tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {("DAPP-KEY-MATERIAL", "blocker", "src/arrow.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in text


def test_a_cast_before_a_literal_on_the_next_line_is_judged_on_line_one(tmp_path):  # NX-M1g: <Hex> continues into the literal
    write(tmp_path, "src/cast.ts", f'const privateKey = <Hex>\n"{KEY_HEX}";\n')
    write(tmp_path, "src/spaced.ts", f'const privateKey = <Hex> \n"{KEY_HEX}" ;\n')
    write(tmp_path, "src/nested.ts", f'const privateKey = <Array<Hex>>\n"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/cast.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/spaced.ts:1"),
        ("DAPP-KEY-MATERIAL", "blocker", "src/nested.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_a_generic_call_before_a_parenthesised_literal_stays_silent(tmp_path):  # NX-M1g: call arguments are not read by design
    write(tmp_path, "src/call.ts", f'const privateKey = Foo<Bar>\n("{KEY_HEX}");\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert report["findings"] == [] and report["skipped"] == []


def test_an_attribute_tag_before_a_literal_still_ends_the_value(tmp_path):  # NX-M1g: an attribute tag is not a cast
    write(tmp_path, "src/tag.tsx", f'const privateKey = <Input secret={{s}}>\n"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert report["findings"] == [] and report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_an_angle_value_of_another_shape_before_a_literal_is_reported_not_silent(tmp_path):  # NX-M1g: fail closed, no value shown
    write(tmp_path, "src/union.ts", f'const privateKey = <Hex | Foo>\n"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set()
    assert report["skipped"] == ["src/union.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"]
    assert KEY_HEX not in json.dumps(report)


def test_generic_close_forms_before_a_literal_on_the_next_line_are_unchanged(tmp_path):  # NX-M1g: (a) to (d) keep their round-fourteen results
    index = 0
    for generic in ("Map<string, string>", "Hex<T>", "Array<Hex>"):
        for text in (f'const privateKey = {generic}\n"{KEY_HEX}";\n',
                     f'const privateKey = {generic} as Hex\n"{KEY_HEX}";\n',
                     f'const privateKey = {generic} satisfies Hex\n"{KEY_HEX}";\n',
                     f'const privateKey = {generic}\n("{KEY_HEX}");\n',
                     f'const privateKey = {generic}\n("{KEY_HEX}" as Hex);\n'):
            index += 1
            write(tmp_path, f"src/generic{index}.ts", text)
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set()  # the literal after a generic type is a statement, not the value
    assert report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_a_double_cast_on_the_literal_line_is_judged_on_line_one(tmp_path):  # NX-M1h: a second cast operand joins the first
    write(tmp_path, "src/double.ts", f'const privateKey = <Hex>\n<Hex>"{KEY_HEX}" as Hex;\n')
    write(tmp_path, "src/indented.ts", f'const privateKey = <Hex>\n  <Hex>"{KEY_HEX}" as Hex;\n')
    write(tmp_path, "src/semi.ts", f'const privateKey = <Hex>\n<Hex>"{KEY_HEX}";\n')
    write(tmp_path, "src/spaced.ts", f'const privateKey = <Hex>\n"{KEY_HEX}" as Hex ;\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/double.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/indented.ts:1"),
        ("DAPP-KEY-MATERIAL", "blocker", "src/semi.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/spaced.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_a_jsx_element_then_a_self_closing_tag_holds_no_literal_and_gives_no_reason(tmp_path):  # NX-M1h: the joined value has no literal
    write(tmp_path, "src/div.tsx", "const privateKey = <div>\n  <span/>\n")
    report = module().scan_tree(tmp_path, tmp_path)
    assert report["findings"] == [] and report["skipped"] == []


def test_an_angle_value_of_another_shape_before_a_double_cast_is_reported_not_silent(tmp_path):  # NX-M1h: fail closed, no value shown
    write(tmp_path, "src/union.ts", f'const privateKey = <Hex | Foo>\n<Hex>"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set()
    assert report["skipped"] == ["src/union.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"]
    assert KEY_HEX not in json.dumps(report)


def test_an_identifier_led_continuation_of_a_cast_is_reported_not_silent(tmp_path):  # NX-M1i: getKey() + "0x…" is part of the value
    write(tmp_path, "src/cont.ts", f'const privateKey = <Hex>\n  getKey() + "{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set() and report["status"] == "fail"
    assert report["skipped"] == ["src/cont.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"]
    assert KEY_HEX not in json.dumps(report)


def test_a_triple_cast_before_a_literal_is_judged_on_line_one(tmp_path):  # NX-M1i: two joins give <Hex> <Hex>, which is still a cast chain
    write(tmp_path, "src/triple.ts", f'const privateKey = <Hex>\n<Hex>\n<Hex>"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {("DAPP-KEY-MATERIAL", "blocker", "src/triple.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_repeated_casts_across_blank_lines_are_judged_on_line_one(tmp_path):  # NX-M1i: blank lines between the casts and before the literal do not stop the join
    write(tmp_path, "src/blank.ts", f'const privateKey = <Hex>\n<Hex>\n\n"{KEY_HEX}";\n')
    write(tmp_path, "src/between.ts", f'const privateKey = <Hex>\n\n<Hex>\n"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/blank.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/between.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_a_cast_then_a_keyword_led_line_ends_the_value(tmp_path):  # NX-M1i: a statement keyword ends the value, so nothing is reported
    write(tmp_path, "src/kw.ts", "const privateKey = <Hex>\nconst other = 1;\n")
    report = module().scan_tree(tmp_path, tmp_path)
    assert report["findings"] == [] and report["skipped"] == []


def test_a_cast_then_a_digit_led_line_is_reported_not_silent(tmp_path):  # NX-M1i: 1 + 2 continues the value
    write(tmp_path, "src/digit.ts", "const privateKey = <Hex>\n1 + 2;\n")
    report = module().scan_tree(tmp_path, tmp_path)
    assert report["status"] == "fail" and report["skipped"] == ["src/digit.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"]


def test_an_angle_union_then_an_identifier_led_line_is_reported_not_silent(tmp_path):  # NX-M1i: <Hex | Foo>getKey() is an assertion too
    write(tmp_path, "src/union.ts", f'const privateKey = <Hex | Foo>\ngetKey() + "{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set()
    assert report["skipped"] == ["src/union.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"]
    assert KEY_HEX not in json.dumps(report)


def test_a_contextual_word_starting_the_next_line_after_a_cast_is_an_operand(tmp_path):  # NX-M1j: type, declare, namespace, import are not statements
    lines = {"type": f'  type + "{KEY_HEX}";\n', "declare": f'  declare + "{KEY_HEX}";\n', "namespace": f'  namespace + "{KEY_HEX}";\n',
             "importmeta": f'  import.meta.env.X + "{KEY_HEX}";\n'}
    for name, tail in lines.items():
        write(tmp_path, f"src/{name}.ts", "const privateKey = <Hex>\n" + tail)
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set() and report["status"] == "fail"
    assert sorted(report["skipped"]) == [f"src/{name}.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"
                                         for name in sorted(lines)]
    assert KEY_HEX not in json.dumps(report)


def test_a_leading_operator_after_a_cast_is_reported_not_silent(tmp_path):  # NX-M1j: the value continues with an operator, so it is not analysed
    lines = {"plus": f'  + "{KEY_HEX}";\n', "minus": f'  - "{KEY_HEX}";\n', "and": f'  && "{KEY_HEX}";\n',
             "ternary": f'  ? "{KEY_HEX}" : "a";\n', "concat": '  .concat("x");\n'}
    for name, tail in lines.items():
        write(tmp_path, f"src/{name}.ts", "const privateKey = <Hex>\n" + tail)
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set() and report["status"] == "fail"
    assert sorted(report["skipped"]) == [f"src/{name}.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"
                                         for name in sorted(lines)]
    assert KEY_HEX not in json.dumps(report)


def test_a_statement_word_starting_the_next_line_after_a_cast_ends_the_value(tmp_path):  # NX-M1j: a statement word is not an operand
    lines = {"const": "const other = 1;\n", "var": "var other = 1;\n", "return": "return 1;\n",
             "if": "if (x) y();\n", "export": "export const z = 1;\n", "function": "function f() {}\n", "class": "class A {}\n",
             "interface": "interface I {}\n", "enum": "enum E {}\n", "break": "break;\n", "continue": "continue;\n"}
    for name, tail in lines.items():
        write(tmp_path, f"src/{name}.ts", "const privateKey = <Hex>\n" + tail)
    report = module().scan_tree(tmp_path, tmp_path)
    assert report["findings"] == [] and report["skipped"] == []


def test_a_comment_line_between_a_cast_and_a_literal_is_skipped(tmp_path):  # NX-M1j: a comment-only line does not end the value
    write(tmp_path, "src/line.ts", f'const privateKey = <Hex>\n// note\n<Hex>"{KEY_HEX}";\n')
    write(tmp_path, "src/block.ts", f'const privateKey = <Hex>\n/* note */\n<Hex>"{KEY_HEX}";\n')
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == {
        ("DAPP-KEY-MATERIAL", "blocker", "src/line.ts:1"), ("DAPP-KEY-MATERIAL", "blocker", "src/block.ts:1")}
    assert report["skipped"] == [] and KEY_HEX not in json.dumps(report)


def test_a_let_line_after_a_cast_is_reported_not_silently_ended(tmp_path):  # NX-M1k: let is not a statement word, so the value is not silently ended
    write(tmp_path, "src/operand.ts", f'const privateKey = <Hex>\n  let + "{KEY_HEX}";\n')
    write(tmp_path, "src/declaration.ts", "const privateKey = <Hex>\nlet x = 1;\n")
    report = module().scan_tree(tmp_path, tmp_path)
    assert {item for item in found(report) if item[0] == "DAPP-KEY-MATERIAL"} == set() and report["status"] == "fail"
    assert sorted(report["skipped"]) == ["src/declaration.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed",
                                         "src/operand.ts:1: key-named assignment's angle-bracket value continues into a literal on the next line: not analysed"]
    assert KEY_HEX not in json.dumps(report)
