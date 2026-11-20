"""Settings loaded from ``~/.mcp-guardian/config.toml`` (all keys optional).

See ``examples/config.toml`` for a documented example.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_guardian import paths
from mcp_guardian.policy import VALID_ACTIONS, Rule

DEFAULT_GATED_METHODS: tuple[str, ...] = ("tools/call", "resources/read", "prompts/get")


class ConfigError(ValueError):
    pass


@dataclass
class UpstreamConfig:
    """A remote MCP server exposed to claude.ai through the gateway."""

    name: str
    url: str
    # Extra headers sent upstream (e.g. an API key). They never leave this machine
    # except towards the upstream, so claude.ai never sees the secret.
    headers: dict[str, str] = field(default_factory=dict)
    # Forward the client's Authorization header upstream (ignored if headers set one).
    forward_authorization: bool = True


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    # Path secret ("capability URL"). None => generated once and kept in gateway.secret.
    secret: str | None = None
    max_body_bytes: int = 16 * 1024 * 1024
    upstreams: dict[str, UpstreamConfig] = field(default_factory=dict)


@dataclass
class Settings:
    gated_methods: tuple[str, ...] = DEFAULT_GATED_METHODS
    # What to do with gated requests no rule matches: "ask" (popup), "allow" or "block".
    default_action: str = "ask"
    # Seconds to wait for the user before auto-blocking. 0 = wait forever.
    approval_timeout: float = 300.0
    # When the daemon cannot be reached: "closed" blocks the request, "open" lets it through.
    fail_mode: str = "closed"
    # Shims/gateway start the daemon on demand if it is not running.
    auto_start_daemon: bool = True
    # How a blocked tools/call is reported: "error" (JSON-RPC error) or "result" (isError tool result).
    block_style: str = "error"
    max_pending: int = 32
    max_pending_per_session: int = 8
    # Allow/Block stay disabled this long after a popup appears (anti mis-click / anti focus-steal).
    arm_delay: float = 0.8
    notify_new_sessions: bool = True
    ui: str = "auto"  # auto | tk | osascript | console | none
    audit_arguments: str = "redacted"  # full | redacted | none
    rules: tuple[Rule, ...] = ()
    gateway: GatewayConfig = field(default_factory=GatewayConfig)

    def public_view(self) -> dict[str, Any]:
        """Subset sent to interceptors during the IPC handshake."""
        return {
            "gated_methods": list(self.gated_methods),
            "block_style": self.block_style,
            "fail_mode": self.fail_mode,
            "approval_timeout": self.approval_timeout,
        }


def _expect(value: Any, typ: type | tuple[type, ...], key: str) -> Any:
    # bool is a subclass of int in Python; never accept it where a number/string/list is expected.
    if isinstance(value, bool) or not isinstance(value, typ):
        raise ConfigError(f"config key {key!r} has an invalid value: {value!r}")
    return value


def _choice(value: Any, choices: set[str], key: str) -> str:
    if value not in choices:
        raise ConfigError(f"config key {key!r} must be one of {sorted(choices)}, got {value!r}")
    return value


def _parse_rules(raw: Any) -> tuple[Rule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'rules' must be an array of tables ([[rules]])")
    rules = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"rules[{i}] must be a table")
        unknown = set(item) - {"action", "server", "tool", "method", "transport", "note"}
        if unknown:
            raise ConfigError(f"rules[{i}] has unknown keys: {sorted(unknown)}")
        action = _choice(item.get("action"), VALID_ACTIONS, f"rules[{i}].action")
        rules.append(
            Rule(
                action=action,
                server=str(item.get("server", "*")),
                tool=str(item.get("tool", "*")),
                method=str(item.get("method", "*")),
                transport=str(item.get("transport", "*")),
                note=str(item.get("note", "")),
            )
        )
    return tuple(rules)


def _parse_gateway(raw: Any) -> GatewayConfig:
    if raw is None:
        return GatewayConfig()
    if not isinstance(raw, dict):
        raise ConfigError("[gateway] must be a table")
    gw = GatewayConfig()
    if "host" in raw:
        gw.host = _expect(raw["host"], str, "gateway.host")
    if "port" in raw:
        gw.port = _expect(raw["port"], int, "gateway.port")
    if "secret" in raw:
        secret = _expect(raw["secret"], str, "gateway.secret")
        if len(secret) < 16:
            raise ConfigError("gateway.secret must be at least 16 characters")
        gw.secret = secret
    if "max_body_bytes" in raw:
        gw.max_body_bytes = _expect(raw["max_body_bytes"], int, "gateway.max_body_bytes")
    for name, up in (raw.get("upstreams") or {}).items():
        if not isinstance(up, dict) or not isinstance(up.get("url"), str):
            raise ConfigError(f"gateway.upstreams.{name} needs a 'url'")
        if not up["url"].startswith(("http://", "https://")):
            raise ConfigError(f"gateway.upstreams.{name}.url must be http(s)")
        headers = up.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise ConfigError(f"gateway.upstreams.{name}.headers must be a table of strings")
        gw.upstreams[name] = UpstreamConfig(
            name=name,
            url=up["url"],
            headers=dict(headers),
            forward_authorization=bool(up.get("forward_authorization", True)),
        )
    return gw


def parse_settings(raw: dict[str, Any]) -> Settings:
    s = Settings()
    known = {
        "gated_methods", "default_action", "approval_timeout", "fail_mode", "auto_start_daemon",
        "block_style", "max_pending", "max_pending_per_session", "arm_delay", "notify_new_sessions",
        "ui", "audit_arguments", "rules", "gateway",
    }  # fmt: skip
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    if "gated_methods" in raw:
        methods = _expect(raw["gated_methods"], list, "gated_methods")
        s.gated_methods = tuple(str(m).strip() for m in methods)
    if "default_action" in raw:
        s.default_action = _choice(raw["default_action"], VALID_ACTIONS, "default_action")
    if "approval_timeout" in raw:
        s.approval_timeout = float(_expect(raw["approval_timeout"], (int, float), "approval_timeout"))
        if s.approval_timeout < 0:
            raise ConfigError("approval_timeout must be >= 0")
    if "fail_mode" in raw:
        s.fail_mode = _choice(raw["fail_mode"], {"closed", "open"}, "fail_mode")
    if "auto_start_daemon" in raw:
        s.auto_start_daemon = bool(raw["auto_start_daemon"])
    if "block_style" in raw:
        s.block_style = _choice(raw["block_style"], {"error", "result"}, "block_style")
    if "max_pending" in raw:
        s.max_pending = max(1, int(_expect(raw["max_pending"], int, "max_pending")))
    if "max_pending_per_session" in raw:
        s.max_pending_per_session = max(1, int(_expect(raw["max_pending_per_session"], int, "max_pending_per_session")))
    if "arm_delay" in raw:
        s.arm_delay = max(0.0, float(_expect(raw["arm_delay"], (int, float), "arm_delay")))
    if "notify_new_sessions" in raw:
        s.notify_new_sessions = bool(raw["notify_new_sessions"])
    if "ui" in raw:
        s.ui = _choice(raw["ui"], {"auto", "tk", "osascript", "console", "none"}, "ui")
    if "audit_arguments" in raw:
        s.audit_arguments = _choice(raw["audit_arguments"], {"full", "redacted", "none"}, "audit_arguments")
    s.rules = _parse_rules(raw.get("rules"))
    s.gateway = _parse_gateway(raw.get("gateway"))
    return s


def load_settings(home: Path | None = None, path: Path | None = None) -> Settings:
    """Load settings; a missing file yields defaults. Raises ConfigError on bad config."""
    home = home or paths.guardian_home()
    path = path or paths.config_path(home)
    if not path.exists():
        return Settings()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_settings(raw)
