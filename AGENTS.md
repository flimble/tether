# tether - Maestro E2E Test Authoring CLI

A CLI tool that enables AI agents to write and debug Maestro e2e tests for mobile apps (Android + iOS).

## Architecture

Single Python file (`tether`), zero dependencies, runs via `uv run --script`.

Key classes:
- `Config` -- loaded from tether.json + env vars + defaults
- `Platform` (base) -> `AndroidPlatform`, `IOSPlatform`
- `LogcatCollector` / `IOSLogCollector` -- background log streaming
- `DoctorReport` / `CheckResult` -- health check system
- `parse_ui_tree()` -- Android XML element parsing
- `parse_axe_tree()` -- iOS AXe JSON element parsing

## Commands

```bash
tether doctor [--fix]       # validate environment
tether status               # quick device state
tether boot                 # start emulator/simulator
tether screen [path]        # screenshot
tether elements [--json]    # element dump with @refs
tether inspect              # screenshot + elements + logs (JSON)
tether flow <file>          # run Maestro flow
tether smoke <dir>          # run all flows
tether progress [--clear]   # test history
tether last-error           # most recent failure
tether logcat [--follow]    # filtered device logs
tether watch                # auto-capture on UI changes
```

## Testing

```bash
python3 test_tether.py -v   # 58 unit tests
```

## Releasing

To create a new release, just tag and push:

```bash
git tag v0.2.0
git push origin v0.2.0
```

This automatically:
1. Runs tests
2. Bumps the version in `pyproject.toml` on main
3. Creates a GitHub Release with auto-generated notes
4. Updates the Homebrew formula in `flimble/homebrew-tap` with the new sha256

Do NOT manually edit the version in `pyproject.toml` -- the release workflow handles it.

## Development Rules

- Keep it as a single file (`src/tether/cli.py`) with zero dependencies
- All commands must work on both Android and iOS via the Platform abstraction
- Element parsing must filter noise (NOISE_CLASSES, IOS_NOISE_ROLES, SYSTEM_RES_IDS)
- Never hang -- all subprocess calls must have timeouts
- Tests use exec() to load the module (not importlib) since cli.py is loaded dynamically
