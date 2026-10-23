#!/usr/bin/env python3
"""
The Forge v2.4.0 — Comprehensive End-to-End Verification Suite
==============================================================
Tests all 16 core modules in `forge/`, CLI subcommands (`forge_cli.py`),
live HTTP server (`forge_server.py`) + security gates, and MCP server
(`forge_mcp.py`) JSON-RPC 2.0 protocol.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestForgeCoreSuite(unittest.TestCase):
    """Verify all 16 core modules in `forge/`, CLI, Server, and MCP."""

    def test_01_sdk_exports_and_version(self):
        import forge
        self.assertEqual(forge.__version__, "2.5.0")
        required_exports = [
            "Arena", "ArenaConfig", "Challenge", "Submission", "FitnessScore",
            "CompilationGate", "Provider", "CompilationResult", "Tournament",
            "compile_and_run", "type_check", "lex", "parse_ast", "validate_safety",
            "factory_build", "factory_deploy", "ARTIFACT_TYPES",
            "multi_agent_research", "consensus", "auto_route", "MODELS",
            "fitness_score", "get_memory", "get_provenance", "get_policy_engine",
            "get_marketplace", "get_tracer", "get_tracker", "BudgetConfig",
            "BudgetTracker", "CostTracker", "create_replay_config", "run_replay",
            "run_ab_test", "kls_spectral_diagnostics", "StochasticLocalizationState",
            "stochastic_localization_select",
        ]
        for sym in required_exports:
            self.assertTrue(hasattr(forge, sym), f"Missing export in forge.__init__: {sym}")

    def test_02_compiler_7_gate_and_fallback_interpreter(self):
        from forge.compiler import (
            compile_and_run, type_check, lex, parse_ast, validate_safety,
            _py_run, hotpath_mean, hotpath_stddev, hotpath_cosine_similarity,
            hotpath_boltzmann_select, hotpath_bayesian_ucb, hotpath_pareto_front,
        )
        from forge.models import CompilationGate

        # Basic compilation & execution
        res = compile_and_run("fn main() -> i64 { 40 + 2 }")
        self.assertTrue(res.success, f"Compile failed: {res.error}")
        self.assertEqual(res.output.strip(), "42")
        self.assertEqual(res.gate_reached, CompilationGate.EXECUTE)

        # Pure-Python .sl interpreter fallback tests (arrays, loops, recursion)
        sort_sl = """
        fn sort(arr: [i64]) -> [i64] {
            let n = arr.len();
            if n <= 1 { return arr; }
            insertion_sort(arr)
        }
        fn main() -> [i64] {
            sort([5, 3, 8, 1, 2])
        }
        """
        r_sort = _py_run(sort_sl)
        self.assertTrue(r_sort.success, f"Fallback sort failed: {r_sort.error}")
        self.assertEqual(r_sort.output.strip(), "[1, 2, 3, 5, 8]")

        fib_sl = """
        fn fib(n: i64) -> i64 {
            if n <= 1 { return n; }
            fib(n - 1) + fib(n - 2)
        }
        fn main() -> i64 {
            fib(10)
        }
        """
        r_fib = _py_run(fib_sl)
        self.assertTrue(r_fib.success, f"Fallback fib failed: {r_fib.error}")
        self.assertEqual(r_fib.output.strip(), "55")

        # Type check returns CompilationResult
        tc_res = type_check("fn main() -> i64 { 42 }")
        self.assertTrue(tc_res.success)
        self.assertEqual(tc_res.error, "")

        # Safety gate blocks dangerous capabilities in compile_and_run
        unsafe_res = compile_and_run('fn main() -> i64 { process_exec("rm -rf /"); 0 }')
        self.assertFalse(unsafe_res.success)
        self.assertEqual(unsafe_res.gate_reached, CompilationGate.CAPABILITY)

        # Mathematical hotpaths
        self.assertAlmostEqual(hotpath_mean([10.0, 20.0, 30.0]), 20.0, places=5)
        self.assertAlmostEqual(hotpath_stddev([10.0, 20.0, 30.0]), 10.0, places=5)
        self.assertAlmostEqual(hotpath_cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0, places=5)
        probs = hotpath_boltzmann_select([10.0, 50.0, 90.0], 1.0)
        self.assertEqual(len(probs), 3)
        self.assertAlmostEqual(sum(probs), 1.0, places=5)
        ucb = hotpath_bayesian_ucb(0.9, 5, 20, 1.414)
        self.assertGreater(ucb, 0.9)
        pf = hotpath_pareto_front([[10.0, 10.0], [5.0, 5.0], [12.0, 8.0]])
        self.assertIn(0, pf)
        self.assertIn(2, pf)
        self.assertNotIn(1, pf)

    def test_03_statistical_benchmark(self):
        from forge.benchmark import (
            welford_stats, compute_benchmark, compute_ci_95, welch_t_test,
            detect_outliers, compute_pareto_front, FitnessTrend,
        )
        samples = [10.0, 12.0, 11.0, 13.0, 9.0, 11.5]
        mean, std = welford_stats(samples)
        self.assertAlmostEqual(mean, sum(samples) / len(samples), places=6)
        self.assertGreater(std, 0.0)

        bench = compute_benchmark(samples)
        self.assertEqual(bench.min_ms, 9.0)
        self.assertEqual(bench.max_ms, 13.0)

        ci_lo, ci_hi = compute_ci_95(samples)
        self.assertLess(ci_lo, mean)
        self.assertGreater(ci_hi, mean)

        # Zero-variance Welch's t-test
        b_same_1 = compute_benchmark([5.0, 5.0, 5.0])
        b_same_2 = compute_benchmark([5.0, 5.0, 5.0])
        res_same = welch_t_test(b_same_1, b_same_2)
        self.assertFalse(res_same["significant"])
        self.assertEqual(res_same["winner"], "tie")

        b_diff = compute_benchmark([10.0, 10.0, 10.0])
        res_diff = welch_t_test(b_same_1, b_diff)
        self.assertTrue(res_diff["significant"])
        self.assertEqual(res_diff["winner"], "a")

        # MAD == 0 outlier detection (majority identical + 1 spike)
        outliers = detect_outliers([1.0, 1.0, 1.0, 1.0, 100.0])
        self.assertIn(4, outliers)

        # FitnessTrend seeded EMA
        trend = FitnessTrend()
        ema_val = trend.update(80.0)
        self.assertAlmostEqual(ema_val, 80.0, places=5)

    def test_04_evolution_operators_preserve_syntax(self):
        from forge.evolution import (
            _split_function, crossover_single_point, crossover_uniform,
            crossover_ast_aware, mutate_swap_lines, mutate_insert_guard,
            mutate_change_constant, mutate_rename_variable, mutate_add_comment,
            code_novelty, evolve_generation, EvolutionConfig,
        )
        from forge.models import Submission, FitnessScore

        fn_a = "fn solve(n: i64) -> i64 {\n    let x = n + 1;\n    let y = x * 2;\n    y\n}"
        fn_b = "fn solve(n: i64) -> i64 {\n    let a = n * 3;\n    let b = a - 1;\n    b\n}"

        hdr, body, ftr = _split_function(fn_a)
        self.assertIn("fn solve(n: i64) -> i64 {", hdr)
        self.assertEqual(ftr, ["}"])

        for xover in (crossover_single_point, crossover_uniform, crossover_ast_aware):
            child = xover(fn_a, fn_b)
            self.assertTrue(child.startswith("fn solve(n: i64) -> i64 {"))
            self.assertTrue(child.rstrip().endswith("}"))

        for mut in (mutate_swap_lines, mutate_insert_guard, mutate_change_constant, mutate_rename_variable, mutate_add_comment):
            mutated, desc = mut(fn_a)
            self.assertTrue(mutated.startswith("fn solve(n: i64) -> i64 {"))
            self.assertTrue(mutated.rstrip().endswith("}"))

        # Deterministic novelty score
        nb1 = code_novelty(fn_a, [fn_b])
        nb2 = code_novelty(fn_a, [fn_b])
        self.assertAlmostEqual(nb1, nb2, places=9)

        # Evolve generation
        s1 = Submission(source_code=fn_a, fitness=FitnessScore(correctness=0.9, performance=0.85))
        s2 = Submission(source_code=fn_b, fitness=FitnessScore(correctness=0.8, performance=0.75))
        cfg = EvolutionConfig(population_size=6, elite_count=1, seed=42)
        next_gen = evolve_generation([s1, s2], challenge_id="c1", generation_number=1, config=cfg)
        self.assertEqual(len(next_gen), 6)

    def test_05_arena_and_replay_engine(self):
        from forge import Arena, ArenaConfig, Challenge, create_replay_config, run_replay, run_ab_test
        from forge.arena import mock_provider

        challenge_data = json.loads((ROOT / "challenges" / "sort_integers.json").read_text(encoding="utf-8"))
        challenge = Challenge(
            name=challenge_data["name"],
            description=challenge_data["description"],
            function_signature=challenge_data["function_signature"],
            test_cases=challenge_data["test_cases"],
            constraints=challenge_data["constraints"],
            difficulty=challenge_data["difficulty"],
        )

        arena = Arena(config=ArenaConfig(max_generations=2, population_size=6, elite_count=2, verbose=False))
        for name in ("claude", "gpt", "gemini"):
            arena.register_provider(name, mock_provider(name))

        tournament = arena.run(challenge)
        self.assertIsNotNone(tournament.champion)
        self.assertGreater(tournament.champion.fitness.total, 80.0)
        self.assertAlmostEqual(tournament.champion.fitness.correctness, 1.0, places=5)

        # Replay config & A/B test
        rcfg = create_replay_config(tournament, arena.config, seed=42)
        rep_t = run_replay(rcfg)
        self.assertIsNotNone(rep_t.champion)
        self.assertGreater(rep_t.champion.fitness.total, 75.0)
        ab = run_ab_test(
            challenge,
            config_a=ArenaConfig(max_generations=1, population_size=4, verbose=False),
            config_b=ArenaConfig(max_generations=2, population_size=6, verbose=False),
            seed=42,
        )
        self.assertIn("A/B Test:", ab.summary())

    def test_06_polyglot_fitness_engine(self):
        from forge.fitness_engine import score

        # TypeScript with ?? nullish coalescing (previously crashed with re.error)
        ts_code = """
        export interface UserConfig {
            timeoutMs?: number;
            retries?: number;
        }
        export function resolveTimeout(cfg?: UserConfig): number {
            const timeout = cfg?.timeoutMs ?? 5000;
            const retries = cfg?.retries ?? 3;
            if (timeout <= 0) {
                throw new Error("Invalid timeout");
            }
            return timeout * retries;
        }
        """
        ts_report = score(ts_code, "typescript")
        self.assertGreater(ts_report.total, 65.0)

        # Vitalis .sl
        sl_code = "fn add(a: i64, b: i64) -> i64 {\n    a + b\n}\nfn main() -> i64 {\n    add(20, 22)\n}"
        sl_report = score(sl_code, "vitalis")
        self.assertGreater(sl_report.total, 70.0)

        # Terraform HCL, YAML, Dockerfile
        hcl_report = score('variable "env" {\n  type = string\n  description = "Environment"\n}\nresource "aws_s3_bucket" "b" {\n  bucket = var.env\n}', "hcl")
        self.assertGreater(hcl_report.total, 70.0)

        yaml_report = score("name: CI\non:\n  push:\n    branches: [main]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n", "yaml")
        self.assertGreater(yaml_report.total, 70.0)

        docker_report = score("FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nUSER nobody\nHEALTHCHECK CMD python -c 'print(1)'\nCMD [\"python\", \"app.py\"]\n", "dockerfile")
        self.assertGreater(docker_report.total, 75.0)

    def test_07_factory_and_deployer_all_17_types(self):
        from forge.factory import _extract_code, _detect_type, Artifact, FactoryResult, ARTIFACT_TYPES, LANG_MAP
        from forge.deployer import deploy

        self.assertEqual(len(ARTIFACT_TYPES), 18)
        self.assertIn("lean4_proof", ARTIFACT_TYPES)

        # Verify fence extraction with hyphenated language tag
        raw_md = "Here is the code:\n```objective-c\nint main() { return 0; }\n```\nDone."
        self.assertEqual(_extract_code(raw_md, "objective-c"), "int main() { return 0; }")

        # Verify _detect_type supports all new types including lean4_proof
        self.assertEqual(_detect_type("Build a Slack integration bot"), "integration")
        self.assertEqual(_detect_type("Create a VS Code extension for linting"), "extension")
        self.assertEqual(_detect_type("Write a multi-stage Dockerfile for FastAPI"), "docker")
        self.assertEqual(_detect_type("Build a full stack web app with React and FastAPI"), "full_project")
        self.assertEqual(_detect_type("Formalize a Lean 4 proof of the KLS conjecture Poincaré bound"), "lean4_proof")

        # Verify leaderboard() method on FactoryResult
        a1 = Artifact(provider_name="claude", source="def main():\n    return 1\n", score_total=91.5, heuristic_total=22.0)
        fr = FactoryResult(run_id="test_run", artifact_type="agent", description="test", artifacts=[a1], winner=a1)
        self.assertIn("Factory Result: test_run", fr.leaderboard())
        self.assertIn("claude", fr.leaderboard())

        # Verify deploy() handles every artifact type safely in a temp dir
        with tempfile.TemporaryDirectory() as tmp:
            sample_code_by_lang = {
                "python": '"""Sample module."""\ndef main() -> int:\n    return 42\nif __name__ == "__main__":\n    main()\n',
                "rust": "pub fn add(a: i64, b: i64) -> i64 { a + b }\nfn main() { println!(\"{}\", add(1, 2)); }\n",
                "go": "package main\nimport \"fmt\"\nfunc main() { fmt.Println(42) }\n",
                "typescript": "export function main(): number { return 42; }\n",
                "hcl": 'resource "null_resource" "main" {}\n',
                "yaml": "name: test\non: [push]\njobs:\n  run:\n    runs-on: ubuntu-latest\n",
                "dockerfile": "FROM python:3.12-slim\nCMD [\"python\", \"-V\"]\n",
                "vitalis": "fn main() -> i64 { 42 }\n",
            }
            for atype in ARTIFACT_TYPES:
                lang = LANG_MAP.get(atype, "python")
                code = sample_code_by_lang.get(lang, sample_code_by_lang["python"])
                out_dir = Path(tmp) / f"run_{atype}"
                out_dir.mkdir(parents=True, exist_ok=True)
                art = Artifact(artifact_type=atype, language=lang, description=f"Test {atype}", provider_name="claude", source=code, score_total=88.0)
                f_res = FactoryResult(run_id=f"run_{atype}", artifact_type=atype, description=f"Test {atype}", artifacts=[art], winner=art, output_path=out_dir)
                d_res = deploy(f_res)
                self.assertTrue(len(d_res.files_created) >= 1, f"Deployer created no files for {atype}")

    def test_08_governance_memory_skills_marketplace_tracer(self):
        from forge.governance import ProvenanceChain, PolicyEngine, CircuitBreaker
        from forge.memory import MemoryEngine
        from forge import skills
        from forge.marketplace import Marketplace
        from forge.tracer import ExecutionTracer

        # Provenance chain + tamper detection
        chain = ProvenanceChain()
        chain.record("test_event", "entity_1", "actor_1", {"k": "v"})
        chain.record("test_event_2", "entity_2", "actor_2", {"k": "v2"})
        valid, count = chain.verify_integrity()
        self.assertTrue(valid)
        self.assertEqual(count, 2)

        # Tamper with parent_hash and verify detection
        orig_parent = chain._entries[1].parent_hash
        chain._entries[1].parent_hash = "deadbeef" * 8
        valid_tampered, _ = chain.verify_integrity()
        self.assertFalse(valid_tampered)
        chain._entries[1].parent_hash = orig_parent

        # PolicyEngine AST call extraction: normal methods allowed, aliased subprocess blocked
        pe = PolicyEngine()
        safe_violations = pe.check_all({
            "source_code": "def f(d: dict, items: list):\n    items.remove(1)\n    return d.get('x', 0)\n",
            "compile_time_ms": 10.0,
            "type_check_passed": True,
        })
        self.assertEqual([v for v in safe_violations if v["action"] == "block"], [])

        unsafe_violations = pe.check_all({
            "source_code": "import subprocess as sp\nsp.run(['whoami'])\n",
        })
        self.assertTrue(any(v["policy"] == "no_process_exec" for v in unsafe_violations))

        # CircuitBreaker half-open single-probe guard
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "open")
        time.sleep(0.06)
        self.assertEqual(cb.state, "half-open")
        self.assertTrue(cb.can_proceed())
        self.assertFalse(cb.can_proceed())  # second concurrent probe blocked while first is in flight
        cb.record_success()
        self.assertEqual(cb.state, "closed")

        # Memory 3-tier engine
        mem = MemoryEngine()
        mem.remember("user", "Implement quicksort in Vitalis", session="test_s1", persist=False)
        recalled = mem.recall("quicksort Vitalis", session="test_s1", k=5)
        self.assertGreaterEqual(len(recalled), 1)
        skills_found = mem.get_skills("Review Python code for security")
        self.assertIsInstance(skills_found, list)

        # Skill DNA certification + integrity + Marketplace in temp dir
        with tempfile.TemporaryDirectory() as tmp:
            dna = skills.certify_from_compilation(
                name="FastSort",
                description="Sorts integers",
                source_code="fn main() -> i64 { 42 }",
                signature="fn main() -> i64",
                fitness_score=90.0,
                correctness=1.0,
                performance_p95_us=12.0,
                champion_provider="claude",
                generation=1,
            )
            self.assertTrue(dna.verify_integrity())
            loaded_dna = skills.get(dna.skill_id)
            self.assertIsNotNone(loaded_dna)

            # Marketplace relative paths + publish_artifact + load_skill integrity check
            mp = Marketplace(root=Path(tmp) / "market")
            mp.publish_skill(dna)
            self.assertIsNotNone(mp.load_skill(dna.skill_id))
            entry = mp.publish_artifact(
                name="WebhookHandler",
                description="GitHub webhook verifier",
                artifact_type="webhook",
                language="python",
                source_code="def handle(): pass\n",
                fitness=88.5,
                provider="claude",
            )
            self.assertFalse(Path(entry.path).is_absolute())
            self.assertEqual(len(mp.search(query="Webhook")), 1)

        # Tracer step duration
        tracer = ExecutionTracer()
        t = tracer.start_trace("s1", "Test trace")
        tracer.add_step(t.id, "thought", "Step 1")
        time.sleep(0.015)
        tracer.add_step(t.id, "action", "Step 2")
        tracer.finish_trace(t.id, "completed")
        self.assertGreaterEqual(t.steps[1].duration_ms, 5.0)

    def test_09_cli_subcommands(self):
        # Test compile
        r_comp = subprocess.run(
            [sys.executable, str(ROOT / "forge_cli.py"), "compile", "fn main() -> i64 { 21 * 2 }"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(r_comp.returncode, 0, r_comp.stderr)
        self.assertIn("42", r_comp.stdout)

        # Test check (previously crashed on tuple unpacking)
        r_chk = subprocess.run(
            [sys.executable, str(ROOT / "forge_cli.py"), "check", "fn main() -> i64 { 42 }"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(r_chk.returncode, 0, r_chk.stderr)
        self.assertIn("OK", r_chk.stdout)

        # Test demo
        r_demo = subprocess.run(
            [sys.executable, str(ROOT / "forge_cli.py"), "demo", "--generations", "1", "--population", "4"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        )
        self.assertEqual(r_demo.returncode, 0, r_demo.stderr)
        self.assertIn("Status: completed", r_demo.stdout)

    def test_10_live_http_server_and_security_gates(self):
        import forge_server

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), forge_server.ForgeHandler)
        port = httpd.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_thread.start()

        try:
            # 1. Public /api/health and /api/auth-bootstrap
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=5) as resp:
                health = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["artifact_types"], 18)

            with urllib.request.urlopen(f"{base_url}/api/auth-bootstrap", timeout=5) as resp:
                boot = json.loads(resp.read().decode("utf-8"))
                api_key = boot["key"]
                self.assertTrue(len(api_key) >= 16)

            # 2. Unauthenticated request to protected route must return 401
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base_url}/api/models", timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            headers = {"X-Forge-Key": api_key, "Content-Type": "application/json"}

            # 3. Authenticated GET endpoints
            for ep in ("/api/models", "/api/vendor-health", "/api/compute", "/api/budget",
                       "/api/provenance", "/api/memory", "/api/traces", "/api/system",
                       "/api/artifact-types", "/api/marketplace"):
                req = urllib.request.Request(f"{base_url}{ep}", headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(data, dict)
                    if ep == "/api/models":
                        local_models = [m for m in data["models"] if m["name"].endswith("-local")]
                        self.assertTrue(all(m["vendor"] == "ollama" for m in local_models))

            # 4. POST /api/compile & /api/score
            comp_req = urllib.request.Request(
                f"{base_url}/api/compile",
                data=json.dumps({"source": "fn main() -> i64 { 6 * 7 }"}).encode("utf-8"),
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(comp_req, timeout=10) as resp:
                comp_data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(comp_data["success"])
                self.assertEqual(comp_data["output"].strip(), "42")

            score_req = urllib.request.Request(
                f"{base_url}/api/score",
                data=json.dumps({"code": "def add(a: int, b: int) -> int:\n    return a + b\n", "language": "python"}).encode("utf-8"),
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(score_req, timeout=10) as resp:
                score_data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("total", score_data)
                self.assertGreater(score_data["total"], 70.0)

            # 5. Security Gate: /api/self-improve must reject .forge_key and outside-repo files with 403
            for forbidden_target in (".forge_key", ".env", "../outside.py"):
                si_req = urllib.request.Request(
                    f"{base_url}/api/self-improve",
                    data=json.dumps({"instruction": "noop", "file": forbidden_target}).encode("utf-8"),
                    headers=headers, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as si_ctx:
                    urllib.request.urlopen(si_req, timeout=5)
                self.assertEqual(si_ctx.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_11_mcp_server_jsonrpc(self):
        import forge_mcp

        # Initialize
        init_resp = forge_mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init_resp["result"]["serverInfo"]["version"], "2.5.0")

        # Tools list
        list_resp = forge_mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        self.assertIn("vitalis_score", tool_names)
        self.assertIn("forge_compile", tool_names)
        self.assertIn("forge_benchmark", tool_names)
        self.assertIn("forge_market_publish", tool_names)
        self.assertEqual(len(tool_names), 13)

        # Call forge_compile and forge_benchmark via MCP
        comp_call = forge_mcp.handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "forge_compile", "arguments": {"code": "fn main() -> i64 { 99 + 1 }"}},
        })
        comp_payload = json.loads(comp_call["result"]["content"][0]["text"])
        self.assertTrue(comp_payload["success"])
        self.assertEqual(comp_payload["output"].strip(), "100")

        bench_call = forge_mcp.handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "forge_benchmark", "arguments": {"code": "fn main() -> i64 { 10 * 10 }", "runs": 3}},
        })
        bench_payload = json.loads(bench_call["result"]["content"][0]["text"])
        self.assertTrue(bench_payload["success"])
        self.assertEqual(bench_payload["runs"], 3)
        self.assertEqual(len(bench_payload["ci_95_ms"]), 2)

    def test_12_kls_stochastic_localization_and_lean4_proof(self):
        from forge import (
            Arena, ArenaConfig, Challenge, Submission, FitnessScore,
            StochasticLocalizationState, stochastic_localization_select,
            kls_spectral_diagnostics, fitness_score,
        )
        from forge.evolution import _ngram_vector
        from forge.arena import mock_provider

        # 1. Test KLS spectral diagnostics & KLS500 Poincaré bound on synthetic population
        sources = [
            "fn sort(a: [i64]) -> [i64] { insertion_sort(a) }",
            "fn sort(a: [i64]) -> [i64] { if a.len() < 2 { return a; } merge_sorted(a.slice(0, 1), a.slice(1, a.len())) }",
            "fn sort(a: [i64]) -> [i64] { let mut r: [i64] = a; r }",
            "fn sort(a: [i64]) -> [i64] { if a.len() == 0 { return []; } insertion_sort(a) }",
        ]
        scores = [92.5, 95.0, 58.0, 93.8]
        vecs = [_ngram_vector(s, dim=64) for s in sources]
        diag = kls_spectral_diagnostics(vecs, scores)
        self.assertEqual(diag["dimension"], 64)
        self.assertEqual(diag["sample_size"], 4)
        self.assertGreater(diag["fitness_variance"], 0.0)
        self.assertGreater(diag["dirichlet_gradient_energy"], 0.0)
        self.assertGreater(diag["poincare_ratio"], 0.0)
        self.assertTrue(diag["kls500_bound_satisfied"])

        # 2. Test Stochastic Localization Tilt Selection
        subs = [
            Submission(source_code=s, fitness=FitnessScore(correctness=sc / 100.0, performance=sc / 100.0))
            for s, sc in zip(sources, scores)
        ]
        state = StochasticLocalizationState(dim=64)
        picked = stochastic_localization_select(subs, state=state, temperature=0.8, dt=0.2, advance_step=True)
        self.assertIsNotNone(picked)
        self.assertEqual(state.steps, 1)
        self.assertAlmostEqual(state.t, 0.2, places=6)
        self.assertTrue(any(abs(v) > 0.0 for v in state.tilt_vector))

        # 3. Test Arena trajectory pruning + director_hook + kls_history
        steered_calls = []
        def director(gen, kls_d):
            steered_calls.append((gen.number, kls_d["poincare_ratio"]))
            return None  # delegate to automatic trajectory pruning

        challenge = Challenge(
            name="Sort KLS",
            description="Sort integers in ascending order",
            function_signature="fn sort(arr: [i64]) -> [i64]",
            test_cases=[{"input": "[3, 1, 2]", "expected": "[1, 2, 3]"}],
        )
        cfg = ArenaConfig(
            max_generations=3,
            population_size=8,
            elite_count=2,
            enable_trajectory_pruning=True,
            trajectory_prune_patience=1,
            trajectory_prune_gap=5.0,
            director_hook=director,
            verbose=False,
        )
        arena = Arena(config=cfg)
        for name in ("claude", "gpt", "gemini", "deepseek"):
            arena.register_provider(name, mock_provider(name))
        t = arena.run(challenge)
        self.assertGreaterEqual(len(arena.kls_history), 2)
        self.assertGreaterEqual(len(steered_calls), 2)
        self.assertTrue(all(d["kls500_bound_satisfied"] for d in arena.kls_history))
        self.assertIsNotNone(t.champion)

        # 4. Test Lean 4 formal proof scorer (kernel trust level zero vs `sorry` penalty)
        valid_lean4 = """
        structure IsotropicMeasure (n : Nat) where
          poincare_const : Nat
          bound_kls500 : poincare_const <= 500

        lemma kls500_refl (m : IsotropicMeasure n) : m.poincare_const <= 500 := by
          exact m.bound_kls500

        theorem song_zhang_kls_bound (m : IsotropicMeasure n) : m.poincare_const <= 500 := by
          have h := kls500_refl m
          exact h

        #print axioms song_zhang_kls_bound
        """
        sorry_lean4 = """
        theorem broken_kls_bound (n : Nat) : n <= 500 := by
          sorry
        """
        rep_valid = fitness_score(valid_lean4, "lean4")
        rep_sorry = fitness_score(sorry_lean4, "lean4")
        self.assertEqual(rep_valid.language, "lean4")
        self.assertGreater(rep_valid.correctness.score, 0.90)
        self.assertLess(rep_sorry.correctness.score, 0.50)
        self.assertGreater(rep_valid.total, rep_sorry.total + 20.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
