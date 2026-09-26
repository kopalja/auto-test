"""Small, strict JSON configuration. Paths are relative to the config file."""
import copy
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Failure(Exception):
    """An actionable operational failure, safe to show after redaction."""


STAGES = ("investigation", "fixing", "verification")
EFFORTS = {
    "codex": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"},
    "claude": {"low", "medium", "high", "xhigh", "max"},
}
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def repository_name(value):
    if not isinstance(value, str) or not REPO.fullmatch(value) or ".." in value:
        raise Failure("Expected a GitHub owner/repository name")
    return value.lower()


def keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise Failure(f"Invalid {label} fields; allowed: {', '.join(sorted(allowed))}")


def positive(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise Failure(f"{label} must be a positive finite number")


def text(value, label):
    if not isinstance(value, str) or "\x00" in value:
        raise Failure(f"{label} must be text without NUL characters")


def validate_agents(agents):
    keys(agents, STAGES, "agents")
    for stage in STAGES:
        agent = agents.get(stage, {})
        keys(agent, ("provider", "model", "reasoning_effort"), f"{stage} agent")
        if not isinstance(agent.get("provider"), str) or agent["provider"] not in EFFORTS:
            raise Failure(f"{stage}: provider must be codex or claude")
        model = agent.get("model")
        if not isinstance(model, str) or not model.strip() or model.startswith(("-", "YOUR_")) or "\n" in model:
            raise Failure(f"{stage}: configure a real model identifier")
        if not isinstance(agent.get("reasoning_effort"), str) or agent["reasoning_effort"] not in EFFORTS[agent["provider"]]:
            raise Failure(f"{stage}: unsupported {agent['provider']} reasoning_effort")


def validate_environment(env):
    keys(env, ("description", "allowed_targets", "credential_environment_variables", "instructions"), "test_environment")
    for field in ("description", "instructions"):
        text(env.get(field, ""), field)
    for field in ("allowed_targets", "credential_environment_variables"):
        values = env.get(field, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise Failure(f"{field} must be a list of nonempty strings")
    for name in env.get("credential_environment_variables", []):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise Failure("Invalid credential environment variable name")
        if name.startswith(("OPENAI_", "ANTHROPIC_", "CLAUDE_CODE_", "CODEX_")) or name in {"HOME", "PATH", "BASH_ENV", "ENV", "PYTHONPATH", "LD_PRELOAD"}:
            raise Failure("Test credentials cannot override provider authentication or process configuration")


def load(path):
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Failure(f"Cannot read JSON configuration: {path}") from exc
    keys(data, ("auto_test_repository", "monitored_directory", "state_directory", "timezone", "soft_budget_minutes", "retention_days", "agents", "repositories", "instructions", "test_environment"), "configuration")
    data["auto_test_repository"] = repository_name(data.get("auto_test_repository"))
    for key, default in (("monitored_directory", "monitored-repos"), ("state_directory", "var")):
        text(data.get(key, default), key)
        data[key] = (path.parent / data.get(key, default)).resolve()
    # State retention must never be pointed at an owner's checkout or project root.
    state, monitored = data["state_directory"], data["monitored_directory"]
    if state == path.parent or state in path.parents or state == monitored or state in monitored.parents or monitored in state.parents:
        raise Failure("State must be a dedicated directory separate from configuration and monitored checkouts")
    data.setdefault("soft_budget_minutes", 20)
    data.setdefault("retention_days", 30)
    data.setdefault("timezone", "Europe/Bratislava")
    for key in ("soft_budget_minutes", "retention_days"):
        positive(data[key], key)
    try:
        ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise Failure("Unknown timezone; install the host's timezone database") from exc
    data.setdefault("instructions", "")
    text(data["instructions"], "instructions")
    data.setdefault("test_environment", {})
    validate_environment(data["test_environment"])
    validate_agents(data.get("agents", {}))
    entries = data.get("repositories", [])
    if not isinstance(entries, list):
        raise Failure("repositories must be a list")
    overrides = {}
    for entry in entries:
        keys(entry, ("name", "enabled", "soft_budget_minutes", "instructions", "test_environment", "agents"), "repository")
        name = repository_name(entry.get("name"))
        if name in overrides:
            raise Failure(f"Duplicate repository override: {name}")
        if type(entry.get("enabled", True)) is not bool:
            raise Failure("enabled must be boolean")
        merged = repository_settings(data, entry)
        validate_agents(merged["agents"])
        validate_environment(merged["test_environment"])
        positive(merged["soft_budget_minutes"], "soft_budget_minutes")
        text(merged["instructions"], "instructions")
        overrides[name] = entry
    data["repositories"] = overrides
    return data


def repository_settings(config, override):
    result = {key: copy.deepcopy(config[key]) for key in ("agents", "soft_budget_minutes", "instructions", "test_environment")}
    for key in ("soft_budget_minutes", "instructions", "test_environment"):
        if key in override:
            result[key] = copy.deepcopy(override[key])
    stages = override.get("agents", {})
    keys(stages, STAGES, "repository agents")
    for stage, changes in stages.items():
        keys(changes, ("provider", "model", "reasoning_effort"), f"{stage} override")
        result["agents"][stage].update(changes)
    result["enabled"] = override.get("enabled", True)
    return result
