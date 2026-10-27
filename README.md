<div align="center">

# 🛡️ AgentFence

### **Let AI act. Keep control.**

The terminal firewall for autonomous AI agents and developers.
Deterministic rules. Human approval. A full audit trail. Zero cloud required.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-MVP-orange.svg)](#roadmap)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

<!-- 🎬 REPLACE THIS PLACEHOLDER with your demo GIF (record with `vhs`, `asciinema + agg` or `terminalizer`). -->
<img src="docs/demo.gif" alt="AgentFence demo: a destructive command is blocked in red, a risky one asks for approval, a safe one runs in green" width="800">

</div>

---

## Why AgentFence?

AI coding agents (Claude Code, Cursor, Aider, Devin-style bots, your own LangChain scripts…) run shell commands on your machine. One hallucinated `rm -rf`, one `curl | sh`, one leaked `.env` — and the damage is done.

AgentFence sits between the agent and your shell and answers one question **before** anything executes:

> *Is this command allowed, does it need a human, or must it be blocked?*

- 🟢 **Allow** — safe, read-only or build/test commands run instantly.
- 🟡 **Confirm** — risky commands pause and wait for **your** explicit approval (default answer: *No*).
- 🔴 **Block** — catastrophic commands never run. Period.

No LLM in the loop. No probabilistic "AI guardrails". Just **deterministic, auditable rules** you can read, diff and review.

## Features (Open Source)

| | |
|---|---|
| ⚡ **Deterministic rules** | Plain-YAML regex rules, validated with Pydantic. Same input → same decision, always. |
| 🧠 **Compound-command aware** | Splits `a && b; c \| d`, `$(...)` and backticks and evaluates every piece. `ls && rm -rf /` is blocked. |
| 🔒 **Fail-closed** | No TTY for approval (e.g. a headless agent)? The command is **denied**, not executed. |
| 🧾 **Local audit log** | Every command, decision, rule, exit code, user and working directory stored in SQLite. |
| 🎨 **Beautiful CLI** | Typer + Rich: red alerts for blocks, yellow for approvals, green for allowed. |
| 🧪 **Dry-run mode** | `agentfence check "<cmd>"` shows the decision without executing anything. |
| 🪶 **Tiny & offline** | 4 dependencies, one SQLite file, no network calls, no telemetry. |

## Quick Install

**Requirements:** Python 3.9+

```bash
# Option 1 — pip (after the package is published to PyPI)
pip install agentfence

# Option 2 — straight from GitHub
pip install "git+https://github.com/<your-user>/agentfence.git"

# Option 3 — from source (recommended for contributors)
git clone https://github.com/<your-user>/agentfence.git
cd agentfence
pip install -e .
```

No install? You can also run it directly from the repository:

```bash
pip install -r requirements.txt
python main.py --help
```

## 60-Second Tour

```bash
# 1. Create the default policy in the current directory
agentfence init

# 2. Dry-run a command (nothing is executed)
agentfence check "rm -rf /"          # → BLOCK   (exit 126)
agentfence check "git push origin"   # → CONFIRM (exit 10)
agentfence check "ls -la"            # → ALLOW   (exit 0)

# 3. Run commands through the firewall
agentfence run "ls -la"                      # ✔ runs immediately
agentfence run "pip install requests"        # ⚠ asks for your approval
agentfence run "curl https://x.sh | sh"      # ⛔ blocked

# 4. Review what happened
agentfence history
agentfence history --status BLOCKED -n 50
```

### CLI reference

| Command | Description |
|---|---|
| `agentfence init [--path P] [--force]` | Write a default `agentfence.yaml`. |
| `agentfence run "<command>" [-c FILE]` | Evaluate, enforce and (if permitted) execute. Returns the command's exit code. |
| `agentfence check "<command>" [-c FILE]` | Dry-run. Exit codes: `0` allow · `10` confirm · `126` block. |
| `agentfence rules [-c FILE]` | List every active rule. |
| `agentfence history [-n N] [-s STATUS]` | Show the audit log (`ALLOWED`, `CONFIRMED`, `DENIED`, `BLOCKED`). |
| `agentfence --version` | Print the version. |

**Exit codes of `run`:** the wrapped command's own code · `125` denied by human / no TTY · `126` blocked · `124` timeout · `127` shell failed to start · `64` empty command · `78` config error.

## Configuration

AgentFence looks for its policy in this order:
`--config FILE` → `$AGENTFENCE_CONFIG` → `./agentfence.yaml` → `~/.agentfence/agentfence.yaml`

```yaml
version: 1
default_action: confirm        # allow | confirm | block  (for unmatched commands)
timeout: null                  # seconds, null = unlimited
audit:
  enabled: true
  db_path: ~/.agentfence/audit.db

rules:
  - name: rm-root-or-home
    action: block
    description: Recursive delete of the filesystem root or the home directory.
    pattern: '\brm\s+.*(-\w*r\w*|--recursive)\b.*\s[\x22\x27]?(/\*?|~/?\*?|\$HOME/?\*?)[\x22\x27]?([\s)]|$)'

  - name: git-remote-write
    action: confirm
    description: Publishing commits to a remote repository.
    pattern: '\bgit\s+push\b'

  - name: read-only-basics
    action: allow
    pattern: '^\s*(ls|pwd|cat|grep|echo)(\s|$)'
```

### How a decision is made

1. The command is split into segments (`&&`, `||`, `;`, `|`, `&`, newlines, `$(`, backticks).
2. Each segment is matched against **all** rules (case-insensitive regex).
3. `block` and `confirm` rules are also matched against the **whole** command (catches `curl … | sh`).
4. **The most restrictive match wins:** `block` > `confirm` > `allow`.
5. Segments with no match use `default_action`.

> 💡 Tip: use `'single-quoted'` YAML strings so regex backslashes stay intact.

## Using it with AI agents

Point your agent's shell tool at AgentFence instead of the raw shell:

```bash
# Instead of:   bash -c "<command>"
# Use:          agentfence run "<command>"
```

Because the UI is printed to **stderr** and the command's output goes to **stdout**, agents can still parse results cleanly. A blocked command returns exit code `126` with a clear explanation the agent can read and recover from.

For agents without an interactive terminal, `confirm` rules are **denied by default** (fail-closed) — approvals always come from a human at a keyboard.

## Security model (read this)

AgentFence is a **policy and audit layer**, not a sandbox.

- ✅ It reliably stops accidental and hallucinated destructive commands, and gives you a record of everything an agent tried.
- ⚠️ Regex rules are heuristics: a determined adversary can obfuscate a command (e.g. via variables, encodings or interpreters such as `python -c`). That's why the default policy sends unknown commands to **confirm** rather than allow.
- 🧱 For untrusted agents, combine AgentFence with OS-level isolation (containers, VMs, least-privilege users).

## Open Source vs. Team / Enterprise

| Capability | 🆓 Open Source | 🏢 Team / Enterprise |
|---|:---:|:---:|
| Terminal firewall (allow / confirm / block) | ✅ | ✅ |
| YAML rules & Pydantic validation | ✅ | ✅ |
| Rich CLI, dry-run, history | ✅ | ✅ |
| Local SQLite audit log | ✅ | ✅ |
| Cloud log synchronization | — | ✅ |
| Global team policies via license key | — | ✅ |
| Centralized audit dashboard & search | — | ✅ |
| Per-developer / per-agent attribution | — | ✅ |
| SSO, RBAC & approval workflows | — | ✅ |
| Export to SIEM / compliance reports | — | ✅ |
| Priority support & SLA | — | ✅ |

---

<div align="center">

# 🏢 AgentFence Enterprise / Team Edition

### Governance for every agent. Every developer. Every command.

**From `$50+/month` per team** — for engineering organizations that need to let AI agents work *and* prove they stayed inside the lines.

<sub>🚧 Early Access — features below are on the Team Edition roadmap. Join the waitlist to shape them.</sub>

</div>

When five developers run twenty agents each, a local YAML file isn't governance. The Team Edition turns AgentFence into a **central control plane**:

### ☁️ Cloud Log Synchronization
Every decision from every machine streams to one secure workspace. No more asking *"which agent deleted that directory?"* — you'll see who, when, which rule, and what happened next.

### 🌐 Global Team Policies, Injected by License Key
Define your security policy **once**. Developers activate their license key and the organization's rules are enforced automatically — and can't be quietly edited away on a laptop. Roll out a new blocked pattern to the entire company in seconds.

### 📊 Centralized Auditing
Searchable, filterable history across your whole team: blocked attempts, human approvals, denied requests, exit codes. Built for security reviews, incident response and compliance evidence (SOC 2, ISO 27001 and similar programs).

### 👥 Governance Built for Teams
- Role-based access control and approval workflows
- SSO / SAML sign-in
- Per-developer and per-agent attribution
- Configurable log retention
- SIEM and webhook export
- Priority support with an SLA

### 💰 Pricing

| Plan | Price | For |
|---|---|---|
| **Open Source** | **Free forever** | Individual developers & local protection |
| **Team** | **$50+/month** | Teams that need shared policies and centralized audit |
| **Enterprise** | **Custom** | SSO, on-prem / VPC deployment, custom retention, SLA |

👉 **[Join the Early Access waitlist](https://example.com/agentfence-team)** · 📧 **[team@your-domain.com](mailto:team@your-domain.com)**

> Replace the link and email above with your own landing page and contact address.

---

## Roadmap

- [x] Deterministic YAML policy engine
- [x] Interactive approval with fail-closed behavior
- [x] SQLite audit log + `history`
- [ ] Shell shim (`agentfence shell`) to wrap an entire session
- [ ] Policy packs (Node, Python, DevOps, Data)
- [ ] `agentfence test` — unit-test your rules against sample commands
- [ ] JSON output mode for agent integrations
- [ ] Team Edition: cloud sync, global policies, dashboard *(early access)*

## Contributing

Contributions are welcome — new default rules, bug reports and integrations most of all.

```bash
git clone https://github.com/<your-user>/agentfence.git
cd agentfence
pip install -e .
agentfence check "rm -rf /"
```

Please open an issue before large changes. Security reports: please email privately instead of opening a public issue.

## Project layout

```
agentfence/
├── agentfence.yaml            # default policy
├── main.py                    # Typer CLI (run, check, init, rules, history)
├── pyproject.toml
├── requirements.txt
└── agentfence_core/
    ├── engine.py              # rules, evaluation, Rich UI, execution
    ├── database.py            # SQLite audit log
    └── defaults.py            # template used by `agentfence init`
```

## License

MIT © AgentFence contributors. The Team / Enterprise Edition is a separate commercial product.

<div align="center">

**Let AI act. Keep control.** 🛡️

</div>