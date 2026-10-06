"""Private, bounded JSON-lines transport used by the Harness plugin.

The parent supplies trusted configuration once. Role requests go back to the
parent's llm service; credentials never enter this Python process.
"""
from __future__ import annotations

import json
from pathlib import Path
import queue
import re
import sys
import threading

from .cancellation import RunCancelled, check_cancelled
from .cli import _run_id, load_config
from .orchestrator import AgentOrchestrator
from .providers import JsonRoleProvider, ProviderError, _json_object, role_messages
from .storage import write_json
from .workspace import GitWorkspace

MAX_MESSAGE_BYTES = 32_000_000


def read_message(stream):
    line = stream.readline(MAX_MESSAGE_BYTES + 1)
    if not line or len(line.encode("utf-8")) > MAX_MESSAGE_BYTES or not line.endswith("\n"):
        raise ProviderError("Missing or oversized Harness bridge message")
    return _json_object(line)


def send_message(stream, message):
    line = json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n"
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProviderError("Harness bridge message exceeded the size limit")
    stream.write(line)
    stream.flush()


class HarnessProvider(JsonRoleProvider):
    name = "deepseek-harness-llm"
    is_fixture = False

    def __init__(self, responses, output, cancelled):
        self.responses = responses
        self.output = output
        self.cancelled = cancelled
        self.sequence = 0

    def _call(self, role, human_goal, untrusted_inputs, parser):
        check_cancelled(self.cancelled)
        self.sequence += 1
        system, prompt = role_messages(role, human_goal, untrusted_inputs)
        send_message(self.output, {"type": "model_request", "id": self.sequence,
                                  "role": role, "system": system, "prompt": prompt})
        while True:
            check_cancelled(self.cancelled)
            try:
                response = self.responses.get(timeout=0.1)
                break
            except queue.Empty:
                continue
        check_cancelled(self.cancelled)
        if response.get("id") != self.sequence or response.get("type") != "model_response":
            raise ProviderError("Unexpected Harness model response")
        if response.get("error"):
            raise ProviderError("Harness LLM request failed; check the Harness provider configuration")
        try:
            return parser(_json_object(response["text"]))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ProviderError("Harness response failed local JSON/schema validation") from None


def inspect_run(config_path, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[a-f0-9]{8}", run_id):
        raise ValueError("Invalid run_id")
    runs = config_path.parent / "runs"
    path = runs / run_id / "result.json"
    # Reject symlinks/junctions leading out of the configured state directory.
    if not path.resolve().is_relative_to(runs.resolve()) or runs.resolve() != runs:
        raise ValueError("Report path escapes the configured state directory")
    result = _json_object(path.read_text(encoding="utf-8"))
    result["run_id"] = run_id
    return result


def serve(input_stream, output_stream):
    request = read_message(input_stream)
    operation = request.get("type")
    fields = ({"type", "config_path", "goal", "allow_execution"} if operation == "run"
              else {"type", "config_path", "run_id"})
    if operation not in {"run", "inspect"} or set(request) != fields:
        raise ValueError("Invalid Harness bridge operation")
    config_path = Path(request["config_path"])
    if not config_path.is_absolute():
        raise ValueError("config_path must be absolute")
    config_path = config_path.resolve()
    config, commands = load_config(config_path)
    if config["provider"] != {"type": "harness"}:
        raise ValueError('Harness projects require provider: {"type": "harness"}')
    if operation == "inspect":
        send_message(output_stream, {"type": "result", "value": inspect_run(config_path, request["run_id"])})
        return
    if type(request["allow_execution"]) is not bool:
        raise ValueError("allow_execution must be a boolean")
    if not isinstance(request["goal"], str) or not request["goal"].strip():
        raise ValueError("A nonempty goal is required")

    cancelled = threading.Event()
    responses = queue.Queue(maxsize=1)

    def receive():
        try:
            while True:
                message = read_message(input_stream)
                if message.get("type") == "cancel":
                    cancelled.set()
                    return
                if message.get("type") != "model_response":
                    raise ValueError("Unexpected bridge message")
                responses.put_nowait(message)
        except (ValueError, OSError, ProviderError, queue.Full):
            cancelled.set()

    threading.Thread(target=receive, daemon=True).start()
    run_dir = config_path.parent / "runs" / _run_id()
    provider = HarnessProvider(responses, output_stream, cancelled)
    workspace = GitWorkspace(Path(config["repo"]), run_dir, config["editable_paths"],
                             config["protected_paths"], commands,
                             allow_execution=request["allow_execution"], cancel_event=cancelled)
    runner = AgentOrchestrator(provider, workspace, config_path.parent / "system_record.json",
                               run_dir, max_rounds=config["max_rounds"])
    result = runner.run(request["goal"])
    result["run_id"] = run_dir.name
    write_json(run_dir / "result.json", result)
    send_message(output_stream, {"type": "result", "value": result})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        serve(sys.stdin, sys.stdout)
        return 0
    except (Exception, RunCancelled) as exc:
        # Do not echo model output, credentials, or tracebacks over the protocol.
        send_message(sys.stdout, {"type": "error", "message": str(exc) if isinstance(
            exc, (ValueError, ProviderError)) else "Harness bridge failed: " + type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
