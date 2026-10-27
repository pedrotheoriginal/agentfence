"""AgentFence core engine.

Loads the YAML policy, validates it with Pydantic, evaluates a command string
against the rules and decides whether to ALLOW, ask for CONFIRMation or BLOCK.
Every decision is written to the SQLite audit log.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from rich.box import HEAVY, ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.text import Text

from .database import AuditDatabase, DatabaseError, Status

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

CONFIG_FILENAME = "agentfence.yaml"
CONFIG_ENV_VAR = "AGENTFENCE_CONFIG"
GLOBAL_CONFIG_PATH = Path("~/.agentfence").expanduser() / CONFIG_FILENAME

EXIT_OK = 0
EXIT_USAGE = 64        # empty / invalid command string
EXIT_TIMEOUT = 124     # command exceeded the configured timeout
EXIT_DENIED = 125      # human refused (or no TTY available for confirmation)
EXIT_BLOCKED = 126     # blocked by a rule
EXIT_NOT_FOUND = 127   # shell could not be started
EXIT_CONFIRM = 10      # used by `agentfence check` only
EXIT_CONFIG_ERROR = 78
EXIT_INTERRUPTED = 130

# Splits a command line into independently evaluated segments.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||(?<![<>])&(?![>&])|\n|`|\$\(")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ConfigError(Exception):
    """Invalid or unreadable configuration."""


class ConfigNotFoundError(ConfigError):
    """No configuration file could be located."""


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class Action(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


_SEVERITY = {Action.ALLOW: 0, Action.CONFIRM: 1, Action.BLOCK: 2}


class Rule(BaseModel):
    """A single deterministic rule: a regex bound to an action."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    action: Action
    pattern: str = Field(min_length=1)
    description: str = ""

    _regex: "re.Pattern[str]" = PrivateAttr()

    @field_validator("pattern")
    @classmethod
    def _pattern_must_compile(cls, value: str) -> str:
        try:
            re.compile(value, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return value

    def model_post_init(self, __context: object) -> None:
        self._regex = re.compile(self.pattern, re.IGNORECASE)

    def matches(self, text: str) -> bool:
        return self._regex.search(text) is not None


class AuditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    db_path: str = "~/.agentfence/audit.db"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default_action: Action = Action.CONFIRM
    timeout: Optional[int] = Field(default=None, gt=0)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    rules: List[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rule_names_unique(self) -> "Config":
        seen = set()
        for rule in self.rules:
            if rule.name in seen:
                raise ValueError(f"duplicate rule name: {rule.name!r}")
            seen.add(rule.name)
        return self


@dataclass(frozen=True)
class Decision:
    """Result of evaluating a command."""

    action: Action
    rule_name: Optional[str]
    reason: str
    matched_text: str


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

def find_config(explicit: Optional[Path] = None) -> Path:
    """Locate the configuration file.

    Order: explicit path > $AGENTFENCE_CONFIG > ./agentfence.yaml >
    ~/.agentfence/agentfence.yaml
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigNotFoundError(f"Config file not found: {path}")
        return path

    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        path = Path(env_value).expanduser()
        if not path.is_file():
            raise ConfigNotFoundError(f"${CONFIG_ENV_VAR} points to a missing file: {path}")
        return path

    for candidate in (Path.cwd() / CONFIG_FILENAME, GLOBAL_CONFIG_PATH):
        if candidate.is_file():
            return candidate

    raise ConfigNotFoundError(
        f"No {CONFIG_FILENAME} found. Run `agentfence init` to create one."
    )


def load_config(path: Path) -> Config:
    """Read, parse and validate a YAML policy file."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level YAML element must be a mapping.")

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        details = "\n".join(
            f"  - {'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration in {path}:\n{details}") from exc


def split_command(command: str) -> List[str]:
    """Split a shell command line into its individual segments."""
    return [part.strip() for part in _SEGMENT_SPLIT.split(command) if part.strip()]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class AgentFence:
    """Evaluates, audits and (when permitted) executes shell commands."""

    def __init__(
        self,
        config: Config,
        console: Optional[Console] = None,
        db: Optional[AuditDatabase] = None,
    ) -> None:
        self.config = config
        # UI goes to stderr so the wrapped command's stdout stays clean/parsable.
        self.console = console or Console(stderr=True)
        self.db: Optional[AuditDatabase] = db
        if self.db is None and config.audit.enabled:
            try:
                self.db = AuditDatabase(config.audit.db_path)
            except DatabaseError as exc:
                self.console.print(f"[yellow]⚠ Audit log disabled:[/yellow] {exc}")

    # ------------------------------------------------------------ evaluation
    def evaluate(self, command: str) -> Decision:
        """Pure function: decide what to do with `command` (no side effects)."""
        default = self.config.default_action
        candidates: List[Tuple[Action, Optional[Rule], str]] = []

        for segment in split_command(command):
            matched = [rule for rule in self.config.rules if rule.matches(segment)]
            if matched:
                winner = max(matched, key=lambda rule: _SEVERITY[rule.action])
                candidates.append((winner.action, winner, segment))
            else:
                candidates.append((default, None, segment))

        # Whole-string check for restrictive rules (e.g. `curl ... | sh`).
        for rule in self.config.rules:
            if rule.action is not Action.ALLOW and rule.matches(command):
                candidates.append((rule.action, rule, command))

        if not candidates:
            candidates.append((default, None, command))

        action, rule, text = max(candidates, key=lambda item: _SEVERITY[item[0]])
        if rule is not None:
            reason = rule.description or f"Matched rule '{rule.name}'."
            return Decision(action, rule.name, reason, text)
        return Decision(
            action,
            None,
            f"No rule matched; default_action is '{default.value}'.",
            text,
        )

    # -------------------------------------------------------------- execution
    def run(self, command: str) -> int:
        """Evaluate `command`, enforce the decision and return an exit code."""
        command = command.strip()
        if not command:
            self.console.print("[bold red]✖ Empty command.[/bold red]")
            return EXIT_USAGE

        decision = self.evaluate(command)

        if decision.action is Action.BLOCK:
            self._render(command, decision)
            self._audit(command, Status.BLOCKED, decision)
            return EXIT_BLOCKED

        if decision.action is Action.CONFIRM:
            self._render(command, decision)
            if not self._ask_human():
                self._audit(command, Status.DENIED, decision)
                self.console.print("[bold yellow]✖ Denied. Command was not executed.[/bold yellow]")
                return EXIT_DENIED
            status = Status.CONFIRMED
        else:
            self._render(command, decision)
            status = Status.ALLOWED

        entry_id = self._audit(command, status, decision)
        exit_code = self._execute(command)
        self._record_exit_code(entry_id, exit_code)
        return exit_code

    def _ask_human(self) -> bool:
        """Interactive confirmation. Fails closed when no TTY is available."""
        if not sys.stdin.isatty():
            self.console.print(
                "[bold yellow]No interactive terminal available for approval - "
                "refusing to run (fail-closed).[/bold yellow]"
            )
            return False
        try:
            return Confirm.ask(
                "[bold yellow]Do you approve this command?[/bold yellow]",
                console=self.console,
                default=False,
            )
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return False

    def _execute(self, command: str) -> int:
        try:
            completed = subprocess.run(command, shell=True, timeout=self.config.timeout)
        except subprocess.TimeoutExpired:
            self.console.print(
                f"[bold red]✖ Timed out after {self.config.timeout}s.[/bold red]"
            )
            return EXIT_TIMEOUT
        except KeyboardInterrupt:
            self.console.print("\n[bold yellow]Interrupted.[/bold yellow]")
            return EXIT_INTERRUPTED
        except OSError as exc:
            self.console.print(f"[bold red]✖ Could not start the shell:[/bold red] {exc}")
            return EXIT_NOT_FOUND

        code = completed.returncode
        # POSIX: negative return code means "killed by signal N" -> shell style 128+N.
        return 128 - code if code < 0 else code

    # ----------------------------------------------------------------- audit
    def _audit(self, command: str, status: Status, decision: Decision) -> Optional[int]:
        if self.db is None:
            return None
        try:
            return self.db.log(
                command=command,
                status=status,
                rule_name=decision.rule_name,
                reason=decision.reason,
            )
        except DatabaseError as exc:
            self.console.print(f"[yellow]⚠ Audit write failed:[/yellow] {exc}")
            return None

    def _record_exit_code(self, entry_id: Optional[int], exit_code: int) -> None:
        if self.db is None or entry_id is None:
            return
        try:
            self.db.set_exit_code(entry_id, exit_code)
        except DatabaseError as exc:
            self.console.print(f"[yellow]⚠ Audit update failed:[/yellow] {exc}")

    # -------------------------------------------------------------------- UI
    def _render(self, command: str, decision: Decision) -> None:
        rule_label = decision.rule_name or "default"

        if decision.action is Action.ALLOW:
            line = Text()
            line.append("✔ ALLOWED ", style="bold green")
            line.append(f"[{rule_label}] ", style="green")
            line.append(command, style="dim")
            self.console.print(line)
            return

        style, title, box = {
            Action.BLOCK: ("bold red", "⛔  BLOCKED BY AGENTFENCE", HEAVY),
            Action.CONFIRM: ("bold yellow", "⚠  HUMAN APPROVAL REQUIRED", ROUNDED),
        }[decision.action]

        body = Text()
        body.append("Command : ", style="bold")
        body.append(command + "\n", style=style)
        body.append("Rule    : ", style="bold")
        body.append(rule_label + "\n")
        body.append("Reason  : ", style="bold")
        body.append(decision.reason)
        if decision.matched_text != command:
            body.append("\nMatched : ", style="bold")
            body.append(decision.matched_text, style=style)

        self.console.print(
            Panel(body, title=f"[{style}]{title}[/{style}]", border_style=style, box=box, expand=False)
        )