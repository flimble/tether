<p align="center">
  <img src="assets/logo.svg" alt="tether" width="480" />
</p>

<p align="center">
  <strong>Mobile emulator automation CLI for AI agents.</strong><br>
  Like <a href="https://github.com/vercel-labs/agent-browser">agent-browser</a>, but for Android emulators and iOS simulators.<br>
  Screenshots, element trees with @refs, log streaming, and Maestro test execution.
</p>

<p align="center">
  <a href="https://github.com/flimble/tether/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/flimble/tether/ci.yml?style=for-the-badge&label=CI" alt="CI"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="#"><img src="https://img.shields.io/badge/platforms-macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Platforms"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License"></a>
</p>

---

## Why

[agent-browser](https://github.com/vercel-labs/agent-browser) gave AI agents eyes into the browser. tether does the same for mobile -- your agent can see the screen, read the element tree, stream device logs, and run Maestro e2e tests, all from a single CLI.

Without it, agents writing mobile tests are flying blind: they can't see the emulator, don't know what elements exist, wait 20s per iteration to discover failures, and can't tell if the device is hung vs. loading.

```
tether doctor            # validate the whole stack
tether inspect           # screenshot + elements + logs in one call
tether flow login.yaml   # run a test, get pass/fail + crash context
```

One Python file. Zero dependencies. Works with Android emulators and iOS simulators.

## Installation

### Homebrew (macOS)

```bash
brew install flimble/tap/tether
```

### uv (any platform)

[uv](https://docs.astral.sh/uv/) manages Python for you -- no need to install Python first.

```bash
# Install globally
uv tool install git+https://github.com/flimble/tether

# Or just run it once (npx-style)
uvx --from git+https://github.com/flimble/tether tether doctor
```

### From source

```bash
git clone https://github.com/flimble/tether.git
cd tether
uv tool install .
```

### Verify

```bash
tether --help
tether doctor    # checks your environment
```

## Prerequisites

**Android:**
- [Android SDK](https://developer.android.com/studio) with an AVD configured
- `adb` in your PATH
- [Maestro](https://maestro.mobile.dev/) CLI installed

**iOS:**
- Xcode with Simulator
- [AXe](https://github.com/cameroncooke/AXe) for element dumps (`brew install cameroncooke/axe/axe`)
- [Maestro](https://maestro.mobile.dev/) CLI installed

## Quick Start

```bash
# 1. Validate your environment
tether doctor

# 2. See what's on screen
tether screen              # saves screenshot to /tmp/tether-screen.png

# 3. Find element selectors
tether elements            # human-readable list with @refs
tether elements --json     # machine-readable

# 4. Do it all at once (recommended for agents)
tether inspect             # screenshot + elements + logs as JSON

# 5. Run a test
tether flow flows/login.yaml
```

## Commands

### Environment

| Command | Description |
|---------|-------------|
| `tether doctor` | Validate entire stack (adb, emulator, Maestro, screenshots, elements) |
| `tether doctor --fix` | Auto-fix issues (start adb server, boot emulator) |
| `tether status` | Quick device state check |
| `tether boot` | Start emulator/simulator if not running |

### Visibility

| Command | Description |
|---------|-------------|
| `tether screen [path]` | Screenshot current screen |
| `tether elements` | List visible UI elements with `@ref` handles |
| `tether elements --json` | Machine-readable element dump |
| `tether inspect` | Screenshot + elements + logs in one JSON call |
| `tether watch` | Auto-capture on UI changes |

### Test Execution

| Command | Description |
|---------|-------------|
| `tether flow <file>` | Run a Maestro flow with crash/error context |
| `tether smoke <dir>` | Run all flows in a directory |
| `tether smoke <dir> --resume` | Skip already-passed flows |
| `tether progress` | Show pass/fail history |
| `tether last-error` | Details of most recent failure |

### Logs

| Command | Description |
|---------|-------------|
| `tether logcat` | Show filtered device logs (crashes, RN errors, Maestro) |
| `tether logcat --follow` | Stream logs continuously |

## Agent Workflow

The typical AI agent loop:

```
1. tether doctor --fix      # ensure environment is ready
2. tether inspect            # see screen state + elements + logs
3. Write/edit flow YAML      # using element refs from inspect
4. tether flow <file>        # run the test
5. If FAIL: tether inspect   # see what went wrong, iterate
```

The `inspect` command returns JSON that includes:
- Path to the screenshot (viewable by multimodal models)
- Full element tree with `@e1`, `@e2` ref handles
- Any crash or error logs captured during the session

## Element Refs

tether assigns stable `@ref` handles to every visible element:

```
@e1   "Welcome" TextView
@e2   "Login" Button  [clickable]
@e3   id="email-input" EditText  [clickable]
@e4   "Remember me" CheckBox  [clickable, checked]
```

Use these refs to quickly identify elements when writing Maestro selectors. The refs reset on each `elements` or `inspect` call.

## Configuration

Create `tether.json` in your project root:

```json
{
  "platform": "android",
  "avd": "Pixel_XL_API_29",
  "appId": "com.example.myapp",
  "timeouts": {
    "boot": 90,
    "flow": 180,
    "screenshot": 10
  }
}
```

For iOS:

```json
{
  "platform": "ios",
  "simulator": "iPhone-15-Pro",
  "appId": "com.example.myapp"
}
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TETHER_AVD` | Android Virtual Device name | `Pixel_XL_API_29` |
| `TETHER_SIMULATOR` | iOS Simulator UDID or name | (none) |
| `ANDROID_HOME` | Android SDK path | `~/Library/Android/sdk` |

Config priority: env vars > `tether.json` > defaults.

## Cross-Platform Support

tether works with both Android and iOS:

| Feature | Android | iOS |
|---------|---------|-----|
| Screenshots | `adb exec-out screencap` | `xcrun simctl io` / AXe |
| Element dumps | `uiautomator dump` | AXe `describe-ui` |
| Log streaming | `adb logcat` | `xcrun simctl spawn log stream` |
| Device management | `emulator` / `adb` | `xcrun simctl` |

Set `"platform": "ios"` in your config to switch.

## Development

```bash
git clone https://github.com/flimble/tether.git
cd tether

# Run tests
python3 test_tether.py -v

# Run the CLI
./tether doctor
./tether --help
```

## How It Works

tether is a single Python file (~2000 lines) with zero dependencies. It shells out to platform tools (`adb`, `xcrun simctl`, `uiautomator`, AXe, Maestro) and parses their output into clean, structured data that AI agents can consume.

Key design decisions:
- **Single file, zero deps** -- `uv run --script` handles the Python runtime. No virtualenv, no pip install.
- **Platform abstraction** -- `AndroidPlatform` and `IOSPlatform` classes share the same interface, so all commands work on both.
- **Two-layer element filtering** -- Noise containers (FrameLayout, ViewGroup, AXGroup, etc.) and system elements are stripped out. Only meaningful, interactable elements are shown.
- **Logcat integration** -- Background thread collects filtered logs (crashes, RN errors, Maestro output). On test failure, crash context is shown alongside the error.

## Acknowledgments

Inspired by [agent-browser](https://github.com/vercel-labs/agent-browser) -- the browser automation CLI for AI agents. tether applies the same philosophy (screenshots, element refs, structured output for agents) to the mobile emulator world.

## License

MIT -- see [LICENSE](LICENSE).
