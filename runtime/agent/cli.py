"""Command line entrypoints: init, run, demo and inspect."""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .orchestrator import AgentOrchestrator
from .providers import OpenAICompatibleProvider, ScriptedProvider
from .schema import VerificationCommand
from .storage import empty_record, write_json
from .workspace import GitWorkspace


def _run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]


def _paths(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{name} must be a list of nonempty glob strings")
    return value


def load_config(path: Path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    fields = {"schema_version", "repo", "provider", "editable_paths", "protected_paths", "verification", "max_rounds"}
    if not isinstance(config, dict) or set(config) != fields or config["schema_version"] != 1:
        raise ValueError("Invalid configuration fields or schema_version")
    if not isinstance(config["repo"], str) or not config["repo"].strip():
        raise ValueError("repo must be a path")
    repo = (path.parent / config["repo"]).resolve()
    if path.parent.resolve().is_relative_to(repo):
        raise ValueError("State/config directory must be outside the target repository")
    config["repo"] = str(repo)
    if not _paths(config["editable_paths"], "editable_paths"):
        raise ValueError("At least one editable path is required")
    _paths(config["protected_paths"], "protected_paths")
    if type(config["max_rounds"]) is not int or not 1 <= config["max_rounds"] <= 10:
        raise ValueError("max_rounds must be 1..10")
    if not isinstance(config["verification"], list):
        raise ValueError("verification must be a list")
    commands = []
    for item in config["verification"]:
        if not isinstance(item, dict) or set(item) != {"name", "argv", "timeout_seconds"}:
            raise ValueError("Each verification command needs name, argv, timeout_seconds")
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ValueError("Command name cannot be empty")
        argv = _paths(item["argv"], "argv")
        if not argv:
            raise ValueError("Command argv cannot be empty")
        timeout = item["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError("timeout_seconds must be > 0 and <= 3600")
        commands.append(VerificationCommand(item["name"], [sys.executable if v == "{python}" else v for v in argv], timeout))
    if len({command.name for command in commands}) != len(commands):
        raise ValueError("Verification command names must be unique")
    provider = config["provider"]
    if (not isinstance(provider, dict) or not isinstance(provider.get("type"), str)
            or provider["type"] not in {"openai", "scripted", "harness"}):
        raise ValueError("provider.type must be openai, scripted or harness")
    allowed = ({"type", "model", "base_url", "api_key_env", "timeout_seconds"}
               if provider["type"] == "openai" else ({"type", "scenario"} if provider["type"] == "scripted" else {"type"}))
    if set(provider) != allowed:
        raise ValueError("Invalid provider configuration fields")
    if provider["type"] == "scripted" and (not isinstance(provider["scenario"], str)
                                           or not provider["scenario"].strip()):
        raise ValueError("provider.scenario must be a nonempty path string")
    return config, commands


def init_state(repo, state, model="", editable=None, provider_type="openai"):
    repo, state = Path(repo).resolve(), Path(state).resolve()
    if not repo.is_dir():
        raise ValueError("Target repository does not exist")
    if state.is_relative_to(repo):
        raise ValueError("State directory must be outside the target repository")
    if (state / "agent.json").exists() or (state / "system_record.json").exists():
        raise ValueError("State already exists; edit its agent.json instead of overwriting")
    config = {
        "schema_version": 1, "repo": str(repo),
        "provider": {"type": "openai", "model": model,
                     "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
                     "timeout_seconds": 60},
        "editable_paths": editable or ["src/**"],
        "protected_paths": ["tests/**", "test_*.py", "agent.json", "system_record.json"],
        "verification": [{"name": "tests", "argv": ["{python}", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                          "timeout_seconds": 60}],
        "max_rounds": 3,
    }
    if provider_type == "harness":
        config["provider"] = {"type": "harness"}
    elif provider_type != "openai":
        raise ValueError("Initialization provider must be openai or harness")
    write_json(state / "agent.json", config)
    write_json(state / "system_record.json", empty_record())
    return {"status": "INITIALIZED", "config": str(state / "agent.json"),
            "next_step": ("Review editable_paths and verification, then register this config in the Harness plugin."
                          if provider_type == "harness" else
                          "Set model, editable_paths and verification commands in agent.json; then run with your goal.")}


def run_config(config_path, goal, allow_execution=False):
    path = Path(config_path).resolve()
    config, commands = load_config(path)
    settings = dict(config["provider"])
    kind = settings.pop("type")
    if kind == "harness":
        raise ValueError("Harness provider requires the dsh-ai-native-agent plugin; call ai_native_run there")
    if kind == "scripted":
        scenario_path = path.parent / settings["scenario"]
        provider = ScriptedProvider(json.loads(scenario_path.read_text(encoding="utf-8-sig")))
    else:
        provider = OpenAICompatibleProvider(**settings)
    run_dir = path.parent / "runs" / _run_id()
    workspace = GitWorkspace(Path(config["repo"]), run_dir, config["editable_paths"],
                             config["protected_paths"], commands, allow_execution=allow_execution)
    return AgentOrchestrator(provider, workspace, path.parent / "system_record.json",
                             run_dir, max_rounds=config["max_rounds"]).run(goal)


def demo(output):
    """Create a harmless, known local fixture and demonstrate a real failing test loop."""
    root = Path(output).resolve()
    if root.exists():
        raise ValueError("Demo output already exists; choose a new directory to preserve previous evidence")
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "clamp.py").write_text("def clamp(value, lower=0, upper=100):\n    return value\n", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_clamp.py").write_text(
        "import unittest\nfrom clamp import clamp\n\nclass ClampContract(unittest.TestCase):\n"
        "    def test_lower_bound(self):\n        self.assertEqual(clamp(-4), 0)\n"
        "    def test_upper_bound(self):\n        self.assertEqual(clamp(140), 100)\n"
        "    def test_preserves_in_range(self):\n        self.assertEqual(clamp(45), 45)\n"
        "    def test_custom_bounds(self):\n        self.assertEqual(clamp(0, 10, 20), 10)\n",
        encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8", newline="\n")
    hooks = root / "empty-hooks"
    hooks.mkdir()
    for argv in (["init", "--quiet"], ["config", "core.autocrlf", "false"], ["add", "."],
                 ["-c", "user.name=Agent Demo", "-c", "user.email=demo@localhost",
                  "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={hooks}",
                  "commit", "--quiet", "-m", "Initial bounded clamp fixture"]):
        subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True, text=True)
    state = root / "state"
    init_state(repo, state, editable=["clamp.py"])
    config = json.loads((state / "agent.json").read_text(encoding="utf-8"))
    config["provider"] = {"type": "scripted", "scenario": "scenario.json"}
    write_json(state / "agent.json", config)
    proposal = {
        "intent": "Ensure clamp keeps values inside the requested interval",
        "observed_facts": ["The supplied four contract tests define expected clamp behavior"],
        "proposed_change": ["Replace only the clamp implementation"],
        "semantic_delta": ["Out-of-range values become the nearest bound"],
        "new_concepts": [], "risks_unknowns": ["Behavior when lower exceeds upper is unspecified and untested"],
        "verification_plan": ["tests"], "reversibility_notes": ["Original repository is unchanged; discard candidate clone"],
        "side_effects": ["local_files"],
    }
    scenario = {
        "proposals": [
            {**proposal, "changes": [{"path": "clamp.py", "content": "def clamp(value, lower=0, upper=100):\n    return min(value, upper)\n"}]},
            {**proposal, "changes": [{"path": "clamp.py", "content": "def clamp(value, lower=0, upper=100):\n    return max(lower, min(value, upper))\n"}]},
        ],
        "claims": [{"statement": "The candidate passes four clamp boundary and preservation contract examples",
                    "source": "Configured tests command output", "confidence": "high", "scope": "clamp with lower <= upper",
                    "kind": "fact", "validity": "Only the tested examples on this candidate; no proof for untested inputs",
                    "evidence_ids": ["$verification"]}],
    }
    write_json(state / "scenario.json", scenario)
    result = run_config(state / "agent.json", "Fix clamp so values stay between lower and upper; preserve in-range values.", True)
    result["demo"] = "Scripted roles; actual file changes, Git diff, subprocess tests and persistence"
    result["demo_root"] = str(root)
    result["config"] = str(state / "agent.json")
    write_json(root / "demo-result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI Native Agent — Constructor, Critic, Guardian")
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init", help="Create a project configuration and System Record outside the target repo")
    initialize.add_argument("--repo", required=True)
    initialize.add_argument("--state-dir", required=True)
    initialize.add_argument("--model", default="")
    initialize.add_argument("--provider", choices=["openai", "harness"], default="openai")
    initialize.add_argument("--editable", nargs="+", default=None)
    run = sub.add_parser("run", help="Build and verify a candidate patch")
    run.add_argument("--config", required=True)
    run.add_argument("--goal", required=True)
    run.add_argument("--allow-execution", action="store_true", help="Authorize configured commands to execute candidate code on this host; not an OS sandbox")
    demonstration = sub.add_parser("demo", help="Offline scripted fixture with real tests and a revision cycle")
    demonstration.add_argument("--output", default=".agent-demo")
    inspect = sub.add_parser("inspect", help="Read a saved run report")
    inspect.add_argument("run_dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_state(args.repo, args.state_dir, args.model, args.editable, args.provider)
        elif args.command == "run":
            result = run_config(args.config, args.goal, args.allow_execution)
        elif args.command == "demo":
            result = demo(args.output)
        else:
            result = json.loads((Path(args.run_dir) / "result.json").read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"INITIALIZED", "ACCEPTED"} else (2 if result["status"] == "HUMAN_REQUIRED" else 1)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__, "reasons": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
        return 1
