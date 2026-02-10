"""
tether - Maestro E2E Test Authoring CLI

A CLI for AI agents to write and debug Maestro e2e tests for mobile apps.
Optimized for stability, visibility, and fast iteration.

COMMANDS:
    tether doctor              Validate and fix the test environment
    tether status              Quick emulator state check (fast)
    tether boot                Start emulator if not running
    tether screen [path]       Screenshot current screen
    tether elements [--json]   List visible UI elements (with @refs)
    tether flow <file>         Run a Maestro flow (with logcat)
    tether smoke <dir>         Run all flows in directory
    tether progress            Show test history
    tether inspect             Screenshot + elements + logcat (recommended)
    tether watch               Watch for UI changes, auto-capture
    tether logcat [--follow]   Show filtered logcat (crashes, errors, RN)
    tether last-error          Show most recent failure

EXAMPLES:
    tether doctor              # First: ensure everything works
    tether screen              # See what's on screen
    tether elements            # Find selectors to use
    tether flow flows/login.yaml   # Run a test

WORKFLOW:
    1. tether doctor           # Validate environment
    2. tether screen           # See current app state
    3. tether elements         # Find element selectors
    4. Write flow YAML        # Create test file
    5. tether flow <file>      # Run and iterate

ENVIRONMENT:
    TETHER_AVD                 Android Virtual Device name
                              Default: Pixel_XL_API_29
    TETHER_SIMULATOR           iOS Simulator UDID or name
    ANDROID_HOME              Android SDK path
                              Default: ~/Library/Android/sdk

CONFIG FILE:
    tether.json                Project config (searched in cwd, then parent dirs)
                              Priority: env vars > tether.json > defaults

FILES:
    ~/.tether/progress.json    Test history
    /tmp/tether-screen.png     Latest screenshot
    /tmp/tether-failure.png    Screenshot on test failure
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Static paths (not config-dependent)
PROGRESS_FILE = Path.home() / ".tether" / "progress.json"
SCREEN_PATH = Path("/tmp/tether-screen.png")
WATCH_MANIFEST = Path("/tmp/tether-watch.json")
WATCH_ELEMENTS = Path("/tmp/tether-elements.json")

# Hardcoded defaults
_DEFAULTS = {
    "platform": "android",
    "avd": "Pixel_XL_API_29",
    "appId": "",
    "android_home": os.path.expanduser("~/Library/Android/sdk"),
    "timeouts": {
        "boot": 90,
        "flow": 180,
        "screenshot": 10,
    },
}


@dataclass
class Config:
    platform: str
    avd: str
    app_id: str
    android_home: str
    emulator_bin: str
    simulator: str  # iOS simulator UDID or name
    timeout_boot: int
    timeout_flow: int
    timeout_screenshot: int

    @property
    def default_timeout(self) -> int:
        return self.timeout_screenshot


# Classes that are layout/container noise -- skip unless they have content or interactivity
NOISE_CLASSES = frozenset({
    "android.view.View",
    "android.view.ViewGroup",
    "android.widget.FrameLayout",
    "android.widget.LinearLayout",
    "android.widget.RelativeLayout",
    "androidx.compose.ui.platform.ComposeView",
    "android.widget.ScrollView",
    "android.widget.HorizontalScrollView",
    "androidx.recyclerview.widget.RecyclerView",
    "androidx.viewpager2.widget.ViewPager2",
    "androidx.constraintlayout.widget.ConstraintLayout",
    "androidx.coordinatorlayout.widget.CoordinatorLayout",
    "androidx.appcompat.widget.ActionBarOverlayLayout",
    "androidx.appcompat.widget.ContentFrameLayout",
    "androidx.appcompat.widget.FitWindowsLinearLayout",
    "android.widget.ContentFrameLayout",
})

# System resource IDs to always skip
SYSTEM_RES_IDS = frozenset({
    "android:id/statusBarBackground",
    "android:id/navigationBarBackground",
    "android:id/content",
    "android:id/action_bar_container",
})

# Logcat filter patterns (compiled once)
_LOGCAT_PATTERNS = [
    re.compile(r"ReactNativeJS", re.IGNORECASE),
    re.compile(r"FATAL|ANR|CRASH", re.IGNORECASE),
    re.compile(r"AndroidRuntime.*Exception", re.IGNORECASE),
    re.compile(r"maestro", re.IGNORECASE),
    re.compile(r"E/\S+\s*:\s*(?:Error|Exception|Fatal|Crash)", re.IGNORECASE),
]

LOGCAT_FILE = Path("/tmp/tether-logcat.json")


class LogcatCollector:
    """Background logcat collector. Spawns adb logcat and buffers filtered lines."""

    def __init__(self, max_lines: int = 200, app_id: str = ""):
        self._max_lines = max_lines
        self._app_id = app_id
        self._lines: list[dict] = []
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            # Clear logcat buffer, then start streaming
            subprocess.run(["adb", "logcat", "-c"], timeout=3,
                           capture_output=True)
            self._proc = subprocess.Popen(
                ["adb", "logcat", "-v", "time"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
        except Exception:
            self._proc = None

    def _reader(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if not self._matches(line):
                continue
            entry = {
                "line": line,
                "ts": time.strftime("%H:%M:%S"),
            }
            # Classify severity
            if re.search(r"FATAL|ANR|CRASH|AndroidRuntime", line, re.IGNORECASE):
                entry["severity"] = "crash"
            elif re.search(r"Error|Exception|E/", line, re.IGNORECASE):
                entry["severity"] = "error"
            else:
                entry["severity"] = "info"
            with self._lock:
                self._lines.append(entry)
                if len(self._lines) > self._max_lines:
                    self._lines = self._lines[-self._max_lines:]

    def _matches(self, line: str) -> bool:
        if self._app_id and self._app_id in line:
            return True
        return any(p.search(line) for p in _LOGCAT_PATTERNS)

    def drain(self) -> list[dict]:
        """Return and clear buffered lines."""
        with self._lock:
            lines = self._lines[:]
            self._lines.clear()
        return lines

    def recent(self, n: int = 50) -> list[dict]:
        """Return last n lines without clearing."""
        with self._lock:
            return self._lines[-n:]

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                if self._proc:
                    self._proc.kill()
            self._proc = None

    def save(self, path: Path | None = None) -> None:
        """Save recent lines to file."""
        dest = path or LOGCAT_FILE
        lines = self.recent()
        dest.write_text(json.dumps(lines, indent=2))


# Module-level logcat collector
_logcat: LogcatCollector | None = None


def get_logcat() -> LogcatCollector:
    """Get or create the module-level logcat collector."""
    global _logcat
    if _logcat is None:
        app_id = cfg.app_id if cfg else ""
        _logcat = LogcatCollector(app_id=app_id)
    return _logcat


# === Platform Abstraction ===

# iOS noise roles to filter (equivalent of Android NOISE_CLASSES)
IOS_NOISE_ROLES = frozenset({
    "AXWindow", "AXGroup", "AXScrollArea", "AXLayoutArea",
    "AXSplitGroup", "AXList", "AXTable", "AXOutline",
    "AXRow", "AXColumn", "AXCell",
})


class Platform:
    """Base platform interface. Subclassed per OS."""

    def is_device_running(self) -> CheckResult:
        raise NotImplementedError

    def boot_device(self) -> None:
        raise NotImplementedError

    def screenshot(self, output: Path) -> bool:
        raise NotImplementedError

    def dump_elements_raw(self) -> str:
        """Return raw element data (XML for Android, JSON for iOS)."""
        raise NotImplementedError

    def parse_elements(self, raw: str, assign_refs: bool = True) -> list[dict]:
        raise NotImplementedError

    def run_checks(self, auto_fix: bool = False) -> DoctorReport:
        raise NotImplementedError

    def start_log_collector(self) -> LogcatCollector:
        raise NotImplementedError

    def stream_logs_oneshot(self, lines: int = 50) -> str:
        """Return filtered log output (one-shot, not streaming)."""
        raise NotImplementedError

    def watch_events_cmd(self) -> list[str] | None:
        """Return command to watch UI events, or None if not supported."""
        return None


class AndroidPlatform(Platform):
    """Android platform using adb + uiautomator."""

    def is_device_running(self) -> CheckResult:
        return check_emulator_running()

    def boot_device(self) -> None:
        print(f"Booting {cfg.avd}...")
        subprocess.Popen(
            [cfg.emulator_bin, "-avd", cfg.avd, "-no-snapshot-load", "-no-audio", "-no-window"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        start = time.perf_counter()
        deadline = time.time() + cfg.timeout_boot
        while time.time() < deadline:
            code, out, _ = run_cmd(["adb", "shell", "getprop", "sys.boot_completed"], timeout=5)
            if code == 0 and "1" in out:
                ms = int((time.perf_counter() - start) * 1000)
                print(f"Booted in {ms}ms")
                return
            time.sleep(2)
        print(f"Boot timeout after {cfg.timeout_boot}s")
        sys.exit(1)

    def screenshot(self, output: Path) -> bool:
        result = subprocess.run(
            ["adb", "exec-out", "screencap", "-p"],
            capture_output=True, timeout=cfg.timeout_screenshot,
        )
        if result.returncode == 0 and len(result.stdout) > 1000:
            output.write_bytes(result.stdout)
            return True
        return False

    def dump_elements_raw(self) -> str:
        run_cmd(["adb", "shell", "uiautomator", "dump", "/sdcard/ui.xml"], timeout=10)
        code, out, _ = run_cmd(["adb", "shell", "cat", "/sdcard/ui.xml"], timeout=5)
        return out if code == 0 else ""

    def parse_elements(self, raw: str, assign_refs: bool = True) -> list[dict]:
        return parse_ui_tree(raw, assign_refs)

    def run_checks(self, auto_fix: bool = False) -> DoctorReport:
        report = DoctorReport()
        adb_check = check_adb_installed()
        report.add(adb_check)
        if not adb_check.passed:
            return report
        server_check = check_adb_server()
        if not server_check.passed and auto_fix:
            print("Starting adb server...")
            run_cmd(["adb", "start-server"], timeout=10)
            server_check = check_adb_server()
        report.add(server_check)
        report.add(check_avd_exists())
        report.add(check_maestro_installed())
        emu_check = check_emulator_running()
        if not emu_check.passed and auto_fix:
            self.boot_device()
            emu_check = check_emulator_running()
        report.add(emu_check)
        if emu_check.passed:
            report.add(check_adb_connection())
            report.add(check_screenshot())
            report.add(check_ui_dump())
        return report

    def start_log_collector(self) -> LogcatCollector:
        lc = get_logcat()
        lc.start()
        return lc

    def stream_logs_oneshot(self, lines: int = 50) -> str:
        code, out, _ = run_cmd(
            ["adb", "logcat", "-d", "-v", "time", "-t", str(lines * 10)],
            timeout=10,
        )
        return out if code == 0 else ""

    def watch_events_cmd(self) -> list[str] | None:
        return ["adb", "shell", "uiautomator", "events"]


class IOSLogCollector(LogcatCollector):
    """iOS log collector using xcrun simctl spawn log stream."""

    def __init__(self, max_lines: int = 200, app_id: str = "", simulator: str = "booted"):
        super().__init__(max_lines=max_lines, app_id=app_id)
        self._simulator = simulator

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                ["xcrun", "simctl", "spawn", self._simulator, "log", "stream",
                 "--style", "compact", "--predicate",
                 f'subsystem == "com.apple.UIKit" OR '
                 f'messageType == 21 OR '  # fault/crash
                 f'subsystem CONTAINS "ReactNative" OR '
                 f'process == "maestro" OR '
                 f'(processImagePath CONTAINS "{self._app_id}" AND messageType >= 16)'
                 if self._app_id else
                 'subsystem == "com.apple.UIKit" OR '
                 'messageType == 21 OR '
                 'subsystem CONTAINS "ReactNative" OR '
                 'process == "maestro"'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
        except Exception:
            self._proc = None

    def _reader(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # Skip the log stream filter confirmation line
            if line.startswith("Filtering the log data"):
                continue
            entry = {"line": line, "ts": time.strftime("%H:%M:%S")}
            if re.search(r"fault|crash|SIGABRT|EXC_BAD_ACCESS", line, re.IGNORECASE):
                entry["severity"] = "crash"
            elif re.search(r"error|exception", line, re.IGNORECASE):
                entry["severity"] = "error"
            else:
                entry["severity"] = "info"
            with self._lock:
                self._lines.append(entry)
                if len(self._lines) > self._max_lines:
                    self._lines = self._lines[-self._max_lines:]


# Module-level iOS log collector
_ios_logcat: IOSLogCollector | None = None


class IOSPlatform(Platform):
    """iOS platform using axe + xcrun simctl."""

    def _sim_id(self) -> str:
        """Get simulator identifier (UDID or 'booted')."""
        return cfg.simulator or "booted"

    def _resolve_booted_udid(self) -> str:
        """Resolve 'booted' to actual UDID."""
        code, out, _ = run_cmd(["xcrun", "simctl", "list", "devices", "booted", "-j"], timeout=5)
        if code != 0:
            return ""
        try:
            data = json.loads(out)
            for runtime, devices in data.get("devices", {}).items():
                for dev in devices:
                    if dev.get("state") == "Booted":
                        return dev.get("udid", "")
        except (json.JSONDecodeError, KeyError):
            pass
        return ""

    def is_device_running(self) -> CheckResult:
        start = time.perf_counter()
        code, out, _ = run_cmd(["xcrun", "simctl", "list", "devices", "booted"], timeout=5)
        ms = int((time.perf_counter() - start) * 1000)
        if code != 0:
            return CheckResult("simulator running", False, "simctl failed", ms)
        sim_id = self._sim_id()
        if sim_id == "booted":
            if "Booted" in out:
                return CheckResult("simulator running", True, "yes", ms)
        else:
            for line in out.split("\n"):
                if sim_id in line and "Booted" in line:
                    return CheckResult("simulator running", True, sim_id, ms)
        return CheckResult("simulator running", False, "not running", ms)

    def boot_device(self) -> None:
        sim_id = self._sim_id()
        if sim_id == "booted":
            print("No simulator specified. Set 'simulator' in tether.json or TETHER_SIMULATOR env var.")
            sys.exit(1)
        print(f"Booting {sim_id}...")
        code, _, err = run_cmd(["xcrun", "simctl", "boot", sim_id], timeout=cfg.timeout_boot)
        if code != 0 and "current state: Booted" not in err:
            print(f"Boot failed: {err.strip()[:100]}")
            sys.exit(1)
        # Wait for boot to complete
        start = time.perf_counter()
        deadline = time.time() + cfg.timeout_boot
        while time.time() < deadline:
            code, out, _ = run_cmd(
                ["xcrun", "simctl", "spawn", sim_id, "launchctl", "print", "system"],
                timeout=5,
            )
            if code == 0:
                ms = int((time.perf_counter() - start) * 1000)
                print(f"Booted in {ms}ms")
                return
            time.sleep(2)
        print(f"Boot timeout after {cfg.timeout_boot}s")
        sys.exit(1)

    def screenshot(self, output: Path) -> bool:
        sim_id = self._sim_id()
        # Try axe first (higher quality, consistent with element dump)
        if shutil.which("axe"):
            udid = sim_id if sim_id != "booted" else self._resolve_booted_udid()
            if udid:
                code, _, _ = run_cmd(
                    ["axe", "screenshot", "--output", str(output), "--udid", udid],
                    timeout=cfg.timeout_screenshot,
                )
                if code == 0 and output.exists() and output.stat().st_size > 1000:
                    return True
        # Fallback to simctl
        code, _, _ = run_cmd(
            ["xcrun", "simctl", "io", sim_id, "screenshot", str(output)],
            timeout=cfg.timeout_screenshot,
        )
        return code == 0 and output.exists() and output.stat().st_size > 1000

    def dump_elements_raw(self) -> str:
        if not shutil.which("axe"):
            return ""
        sim_id = self._sim_id()
        udid = sim_id if sim_id != "booted" else self._resolve_booted_udid()
        if not udid:
            return ""
        code, out, _ = run_cmd(["axe", "describe-ui", "--udid", udid], timeout=10)
        return out if code == 0 else ""

    def parse_elements(self, raw: str, assign_refs: bool = True) -> list[dict]:
        return parse_axe_tree(raw, assign_refs)

    def run_checks(self, auto_fix: bool = False) -> DoctorReport:
        report = DoctorReport()
        # Check xcrun simctl
        start = time.perf_counter()
        path = shutil.which("xcrun")
        ms = int((time.perf_counter() - start) * 1000)
        if path:
            report.add(CheckResult("xcrun simctl", True, path, ms))
        else:
            report.add(CheckResult("xcrun simctl", False, "Xcode tools not installed", ms))
            return report

        # Check axe installed
        start = time.perf_counter()
        axe_path = shutil.which("axe")
        ms = int((time.perf_counter() - start) * 1000)
        if axe_path:
            report.add(CheckResult("axe installed", True, axe_path, ms))
        else:
            report.add(CheckResult("axe installed", False,
                                   "not found. Install: brew install cameroncooke/axe/axe", ms))

        # Check Maestro
        report.add(check_maestro_installed())

        # Check simulator
        sim_check = self.is_device_running()
        if not sim_check.passed and auto_fix:
            self.boot_device()
            sim_check = self.is_device_running()
        report.add(sim_check)

        if sim_check.passed:
            # Check screenshot
            start = time.perf_counter()
            test_path = Path("/tmp/tether-test-screenshot.png")
            ok = self.screenshot(test_path)
            ms = int((time.perf_counter() - start) * 1000)
            if ok:
                size = test_path.stat().st_size
                report.add(CheckResult("screenshot", True, f"{size} bytes", ms))
                test_path.unlink(missing_ok=True)
            else:
                report.add(CheckResult("screenshot", False, "capture failed", ms))

            # Check element dump (non-critical without axe)
            if axe_path:
                start = time.perf_counter()
                raw = self.dump_elements_raw()
                ms = int((time.perf_counter() - start) * 1000)
                if raw:
                    report.add(CheckResult("element dump", True, "works (axe)", ms))
                else:
                    report.add(CheckResult("element dump", False,
                                           "axe describe-ui failed", ms, critical=False))
            else:
                report.add(CheckResult("element dump", False,
                                       "axe not installed (non-critical)", 0, critical=False))

        return report

    def start_log_collector(self) -> LogcatCollector:
        global _ios_logcat
        if _ios_logcat is None:
            _ios_logcat = IOSLogCollector(
                app_id=cfg.app_id, simulator=self._sim_id())
        _ios_logcat.start()
        return _ios_logcat

    def stream_logs_oneshot(self, lines: int = 50) -> str:
        sim_id = self._sim_id()
        try:
            result = subprocess.run(
                ["xcrun", "simctl", "spawn", sim_id, "log", "show",
                 "--style", "compact", "--last", "30s"],
                capture_output=True, text=True, timeout=20,
            )
            return result.stdout if result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, Exception):
            return ""

    def watch_events_cmd(self) -> list[str] | None:
        # iOS doesn't have uiautomator events -- use poll-based approach
        return None


def parse_axe_tree(raw: str, assign_refs: bool = True) -> list[dict]:
    """Parse AXe describe-ui JSON into structured elements.

    AXe outputs a tree with: type, frame, AXLabel, AXUniqueId, children.
    The raw JSON from idb's accessibility APIs may also include:
    role, role_description, value, title, enabled, traits, custom_actions.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    # Can be a single root dict or array of roots
    roots = data if isinstance(data, list) else [data]

    elements: list[dict] = []
    ref_counter = 0

    def walk(node: dict) -> None:
        nonlocal ref_counter
        if not isinstance(node, dict):
            return

        el_type = node.get("type", "")
        label = node.get("AXLabel", "") or ""
        unique_id = node.get("AXUniqueId", "") or ""
        role = node.get("role", "") or node.get("role_description", "") or ""
        value = node.get("value", "") or node.get("AXValue", "") or ""
        title = node.get("title", "") or ""
        frame = node.get("frame", {})
        enabled = node.get("enabled", True)
        traits = node.get("traits", 0)
        children = node.get("children", []) or []

        # Filter: skip noise roles with no content
        has_content = bool(label or unique_id or title or value)
        is_button = "Button" in el_type or "Button" in role
        is_interactive = is_button or "TextField" in el_type or "SecureTextField" in el_type
        if el_type in IOS_NOISE_ROLES and not has_content and not is_interactive:
            # Still walk children
            for child in children:
                walk(child)
            return

        # Skip zero-area
        if frame:
            w = frame.get("width", 0)
            h = frame.get("height", 0)
            if w <= 0 or h <= 0:
                for child in children:
                    walk(child)
                return

        # Skip elements with no content and not interactive
        if not has_content and not is_interactive:
            for child in children:
                walk(child)
            return

        el: dict = {}
        if assign_refs:
            ref_counter += 1
            el["ref"] = f"@e{ref_counter}"

        # Type
        type_short = el_type.replace("AX", "") if el_type.startswith("AX") else el_type
        if type_short:
            el["type"] = type_short

        # Content
        if label:
            el["text"] = label.strip()
        if title and title != label:
            el["title"] = title.strip()
        if unique_id:
            el["id"] = unique_id.strip()
        if value:
            el["value"] = str(value).strip()

        # State
        if is_button or is_interactive:
            el["clickable"] = True
        if enabled is False:
            el["enabled"] = False

        # Bounds
        if frame:
            x = int(frame.get("x", 0))
            y = int(frame.get("y", 0))
            w = int(frame.get("width", 0))
            h = int(frame.get("height", 0))
            el["bounds"] = f"[{x},{y}][{x+w},{y+h}]"

        elements.append(el)

        # Walk children
        for child in children:
            walk(child)

    for root in roots:
        walk(root)
    return elements


def get_platform() -> Platform:
    """Return the platform instance based on config."""
    if cfg.platform == "ios":
        return IOSPlatform()
    return AndroidPlatform()


# Module-level platform, initialized in main()
platform: Platform = None  # type: ignore[assignment]


def find_config_file() -> Path | None:
    """Walk up from cwd looking for tether.json. Returns path or None."""
    current = Path.cwd()
    while True:
        candidate = current / "tether.json"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config() -> Config:
    """Load config with priority: env vars > tether.json > defaults."""
    avd = _DEFAULTS["avd"]
    platform = _DEFAULTS["platform"]
    app_id = _DEFAULTS["appId"]
    android_home = _DEFAULTS["android_home"]
    simulator = ""
    timeout_boot = _DEFAULTS["timeouts"]["boot"]
    timeout_flow = _DEFAULTS["timeouts"]["flow"]
    timeout_screenshot = _DEFAULTS["timeouts"]["screenshot"]

    # Layer on tether.json if found
    config_path = find_config_file()
    if config_path:
        try:
            data = json.loads(config_path.read_text())
            if "avd" in data:
                avd = data["avd"]
            if "platform" in data:
                platform = data["platform"]
            if "appId" in data:
                app_id = data["appId"]
            if "android_home" in data:
                android_home = data["android_home"]
            if "simulator" in data:
                simulator = data["simulator"]
            timeouts = data.get("timeouts", {})
            if "boot" in timeouts:
                timeout_boot = int(timeouts["boot"])
            if "flow" in timeouts:
                timeout_flow = int(timeouts["flow"])
            if "screenshot" in timeouts:
                timeout_screenshot = int(timeouts["screenshot"])
        except (json.JSONDecodeError, OSError):
            pass

    # Env vars override everything
    if env_avd := os.environ.get("TETHER_AVD"):
        avd = env_avd
    if env_home := os.environ.get("ANDROID_HOME"):
        android_home = env_home
    if env_sim := os.environ.get("TETHER_SIMULATOR"):
        simulator = env_sim

    emulator_bin = f"{android_home}/emulator/emulator"

    return Config(
        platform=platform,
        avd=avd,
        app_id=app_id,
        android_home=android_home,
        emulator_bin=emulator_bin,
        simulator=simulator,
        timeout_boot=timeout_boot,
        timeout_flow=timeout_flow,
        timeout_screenshot=timeout_screenshot,
    )


# Module-level config, initialized in main()
cfg: Config = None  # type: ignore[assignment]


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    duration_ms: int = 0
    critical: bool = True  # Non-critical checks don't fail doctor


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def critical_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.critical)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    def print(self) -> None:
        for check in self.checks:
            if check.passed:
                icon = "✓"
            elif check.critical:
                icon = "✗"
            else:
                icon = "⚠"
            print(f"{icon} {check.name}: {check.message}")
        print()
        if self.all_passed:
            print("All checks passed.")
        elif self.critical_passed:
            warnings = [c.name for c in self.checks if not c.passed]
            print(f"Warnings: {', '.join(warnings)}")
            print("Core functionality ready.")
        else:
            failed = [c.name for c in self.checks if not c.passed and c.critical]
            print(f"Failed: {', '.join(failed)}")


def run_cmd(args: list[str], timeout: int = 0) -> tuple[int, str, str]:
    """Run command with timeout. Returns (exit_code, stdout, stderr).

    If timeout is 0 (default), uses cfg.default_timeout.
    """
    if timeout == 0:
        timeout = cfg.default_timeout if cfg else 10
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return -1, "", "command not found"
    except Exception as e:
        return -1, "", str(e)


# === Health Checks ===

def check_adb_installed() -> CheckResult:
    """Check if adb is in PATH."""
    start = time.perf_counter()
    path = shutil.which("adb")
    ms = int((time.perf_counter() - start) * 1000)
    if path:
        return CheckResult("adb installed", True, path, ms)
    return CheckResult("adb installed", False, "not found in PATH", ms)


def check_adb_server() -> CheckResult:
    """Check if adb server is running and responsive."""
    start = time.perf_counter()
    code, out, err = run_cmd(["adb", "devices"], timeout=5)
    ms = int((time.perf_counter() - start) * 1000)
    if code == 0:
        return CheckResult("adb server", True, "running", ms)
    return CheckResult("adb server", False, err.strip()[:50] or "failed", ms)


def check_avd_exists() -> CheckResult:
    """Check if configured AVD exists."""
    start = time.perf_counter()
    code, out, err = run_cmd([cfg.emulator_bin, "-list-avds"], timeout=10)
    ms = int((time.perf_counter() - start) * 1000)
    if code != 0:
        return CheckResult("avd exists", False, "emulator command failed", ms)
    avds = [line.strip() for line in out.strip().split("\n") if line.strip()]
    if cfg.avd in avds:
        return CheckResult("avd exists", True, cfg.avd, ms)
    return CheckResult("avd exists", False, f"{cfg.avd} not found. Available: {avds}", ms)


def check_emulator_running() -> CheckResult:
    """Check if emulator is running."""
    start = time.perf_counter()
    code, out, _ = run_cmd(["adb", "devices"], timeout=5)
    ms = int((time.perf_counter() - start) * 1000)
    if code != 0:
        return CheckResult("emulator running", False, "adb failed", ms)
    for line in out.strip().split("\n")[1:]:
        if line.strip() and "emulator" in line and "device" in line:
            return CheckResult("emulator running", True, "yes", ms)
    return CheckResult("emulator running", False, "not running", ms)


def check_adb_connection() -> CheckResult:
    """Check if we can run shell commands on device."""
    start = time.perf_counter()
    code, out, err = run_cmd(["adb", "shell", "echo", "ok"], timeout=5)
    ms = int((time.perf_counter() - start) * 1000)
    if code == 0 and "ok" in out:
        return CheckResult("adb connection", True, "connected", ms)
    return CheckResult("adb connection", False, err.strip()[:50] or "failed", ms)


def check_maestro_installed() -> CheckResult:
    """Check if Maestro CLI is available."""
    start = time.perf_counter()
    path = shutil.which("maestro")
    if not path:
        ms = int((time.perf_counter() - start) * 1000)
        return CheckResult("maestro installed", False, "not found in PATH", ms)
    code, out, err = run_cmd(["maestro", "--version"], timeout=30)
    ms = int((time.perf_counter() - start) * 1000)
    # Maestro may output version to stdout or stderr, and may return non-zero
    combined = (out + err).strip()
    for line in combined.split("\n"):
        if re.search(r"\d+\.\d+", line):
            return CheckResult("maestro installed", True, line.strip(), ms)
    if code == 0:
        return CheckResult("maestro installed", True, path, ms)
    return CheckResult("maestro installed", False, "version check failed", ms)


def check_screenshot() -> CheckResult:
    """Check if we can take screenshots."""
    start = time.perf_counter()
    result = subprocess.run(
        ["adb", "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=cfg.timeout_screenshot,
    )
    ms = int((time.perf_counter() - start) * 1000)
    if result.returncode == 0 and len(result.stdout) > 1000:
        return CheckResult("screenshot", True, f"{len(result.stdout)} bytes", ms)
    return CheckResult("screenshot", False, "capture failed", ms)


def check_ui_dump() -> CheckResult:
    """Check if we can dump UI hierarchy. Non-critical - screenshots are primary."""
    start = time.perf_counter()
    # uiautomator can hang - use short timeout
    code1, _, _ = run_cmd(["adb", "shell", "uiautomator", "dump", "/sdcard/ui.xml"], timeout=5)
    if code1 != 0:
        ms = int((time.perf_counter() - start) * 1000)
        return CheckResult("ui dump", False, "timeout (non-critical)", ms, critical=False)
    code, out, _ = run_cmd(["adb", "shell", "cat", "/sdcard/ui.xml"], timeout=3)
    ms = int((time.perf_counter() - start) * 1000)
    if code == 0 and "hierarchy" in out:
        return CheckResult("ui dump", True, "works", ms, critical=False)
    return CheckResult("ui dump", False, "dump failed (non-critical)", ms, critical=False)


# === Commands ===

def cmd_doctor(auto_fix: bool = False) -> None:
    """Run all health checks, optionally auto-fixing issues."""
    report = platform.run_checks(auto_fix)
    report.print()
    sys.exit(0 if report.critical_passed else 1)


def cmd_status() -> None:
    """Quick status check - optimized for speed."""
    check = platform.is_device_running()
    if cfg.platform == "ios":
        sim_id = cfg.simulator or "booted"
        print(f"Platform: ios")
        print(f"Simulator: {sim_id}")
    else:
        print(f"AVD: {cfg.avd}")
    print(f"Device: {'running' if check.passed else 'stopped'}")


def cmd_boot() -> None:
    """Ensure device/simulator is running."""
    check = platform.is_device_running()
    if check.passed:
        print("Device already running.")
        return
    platform.boot_device()


def cmd_screen(output_path: str | None = None) -> None:
    """Take screenshot."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    output = Path(output_path) if output_path else SCREEN_PATH
    if not platform.screenshot(output):
        print("Screenshot failed")
        sys.exit(1)
    print(output)


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2)."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _resolve_element_name(node: ET.Element) -> str:
    """Walk children to build a readable name for compound widgets.
    Returns only text content (not content-desc, which goes in 'id' field).
    Stops at actionable boundaries (clickable children)."""
    parts = []
    text = node.get("text", "")
    if text:
        parts.append(text)
    for child in node:
        if child.get("clickable") == "true":
            continue  # don't cross actionable boundaries
        child_text = child.get("text", "")
        if child_text and child_text not in parts:
            parts.append(child_text)
    return " | ".join(parts) if parts else ""


def parse_ui_tree(xml_str: str, assign_refs: bool = True) -> list[dict]:
    """Parse uiautomator XML into structured elements.

    Two-layer filter:
      1. Class-based: skip known layout/container noise classes
      2. Attribute-based: skip invisible, zero-area, system elements

    When assign_refs=True, each element gets a ref like @e1, @e2.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []

    elements = []
    ref_counter = 0

    for node in root.iter("node"):
        cls = node.get("class", "")
        text = node.get("text", "")
        desc = node.get("content-desc", "")
        res_id = node.get("resource-id", "")
        enabled = node.get("enabled") == "true"
        clickable = node.get("clickable") == "true"
        focusable = node.get("focusable") == "true"
        checked = node.get("checked") == "true"
        selected = node.get("selected") == "true"
        scrollable = node.get("scrollable") == "true"
        bounds_str = node.get("bounds", "")

        # Layer 1: class-based filter
        has_content = text or desc or res_id
        is_interactive = clickable or scrollable
        if cls in NOISE_CLASSES and not has_content and not is_interactive:
            continue

        # Layer 2: attribute-based filter
        if res_id in SYSTEM_RES_IDS:
            continue
        # Skip invisible (not displayed on screen)
        if node.get("displayed") == "false":
            continue
        # Skip zero-area elements
        if bounds_str:
            bounds = _parse_bounds(bounds_str)
            if bounds:
                x1, y1, x2, y2 = bounds
                if x2 <= x1 or y2 <= y1:
                    continue
        # Skip elements with no content and not interactive
        if not has_content and not is_interactive:
            continue

        # Resolve name for compound widgets
        name = _resolve_element_name(node)
        cls_short = cls.rsplit(".", 1)[-1] if cls else ""

        el: dict = {}
        if assign_refs:
            ref_counter += 1
            el["ref"] = f"@e{ref_counter}"
        if cls_short:
            el["type"] = cls_short
        # Use "name" for compound text (multiple children), "text" for simple
        if name and name != text and "|" in name:
            el["name"] = name
        elif text:
            el["text"] = text
        if desc:
            el["id"] = desc
        if res_id:
            el["resourceId"] = res_id
        if clickable:
            el["clickable"] = True
        if not enabled:
            el["enabled"] = False
        if checked:
            el["checked"] = True
        if selected:
            el["selected"] = True
        if scrollable:
            el["scrollable"] = True
        if bounds_str:
            el["bounds"] = bounds_str

        elements.append(el)
    return elements


def _format_element_line(el: dict) -> str:
    """Format a single element as a compact human-readable line."""
    ref = el.get("ref", "")
    parts = []
    if "name" in el:
        parts.append(f'"{el["name"]}"')
    elif "text" in el:
        parts.append(f'"{el["text"]}"')
    if "id" in el:
        parts.append(f'id="{el["id"]}"')
    if "resourceId" in el:
        parts.append(f'res={el["resourceId"]}')
    if not parts:
        parts.append(el.get("type", "element"))

    flags = []
    if el.get("clickable"):
        flags.append("clickable")
    if el.get("enabled") is False:
        flags.append("DISABLED")
    if el.get("checked"):
        flags.append("checked")
    if el.get("selected"):
        flags.append("selected")
    if el.get("scrollable"):
        flags.append("scrollable")

    line = f"{ref:5s} " if ref else ""
    line += " ".join(parts)
    if flags:
        line += f"  [{', '.join(flags)}]"
    return line


def cmd_elements(as_json: bool = False) -> None:
    """Dump visible UI elements."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    raw = platform.dump_elements_raw()
    if not raw:
        print("UI dump failed")
        sys.exit(1)

    elements = platform.parse_elements(raw)

    if as_json:
        print(json.dumps(elements, indent=2))
    else:
        for el in elements:
            print(_format_element_line(el))


def cmd_inspect() -> None:
    """Screenshot + elements + logs in one call. The primary agent command."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    # Start log collector
    lc = platform.start_log_collector()
    time.sleep(0.3)

    # Screenshot
    if not platform.screenshot(SCREEN_PATH):
        print("Screenshot failed", file=sys.stderr)

    # Elements
    raw = platform.dump_elements_raw()
    elements = platform.parse_elements(raw) if raw else []

    # Logs - drain recent entries
    log_entries = lc.drain()
    crashes = [e for e in log_entries if e.get("severity") == "crash"]
    errors = [e for e in log_entries if e.get("severity") == "error"]

    output: dict = {
        "screenshot": str(SCREEN_PATH),
        "elements": elements,
    }
    if crashes:
        output["crashes"] = [e["line"] for e in crashes]
    if errors:
        output["errors"] = [e["line"] for e in errors[-10:]]
    if log_entries and not crashes and not errors:
        output["log_lines"] = len(log_entries)

    print(json.dumps(output, indent=2))


def _parse_event_line(line: str) -> str | None:
    """Extract meaningful event type from uiautomator events output.
    Returns event type string or None if the event should be ignored."""
    for event_type in ("TYPE_WINDOW_STATE_CHANGED", "TYPE_WINDOW_CONTENT_CHANGED"):
        if event_type in line:
            return event_type
    return None


WATCH_DIR = Path("/tmp/tether-watch")

_last_elements_hash: str = ""
_watch_manifest_log: list[dict] = []


def _screen_summary(elements: list[dict]) -> dict:
    """Extract a short summary from elements for the manifest."""
    selected_tab = ""
    screen_title = ""
    clickable_count = 0
    for el in elements:
        if el.get("selected") and el.get("type") == "View":
            selected_tab = el.get("id", "")
        if not screen_title and el.get("type") == "TextView":
            text = el.get("text", "")
            if len(text) > 1 and text[0].isupper():
                screen_title = text
        if el.get("clickable"):
            clickable_count += 1
    return {
        "screen_title": screen_title,
        "selected_tab": selected_tab,
        "clickable_count": clickable_count,
    }


def _take_snapshot(event_type: str, snapshot_num: int, json_output: bool) -> bool:
    """Capture screenshot + elements and write manifest.
    Returns True if snapshot was written, False if skipped (duplicate)."""
    global _last_elements_hash
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    elements_count = -1
    elements: list[dict] = []
    screenshot_bytes: bytes = b""

    # Screenshot
    try:
        if platform.screenshot(SCREEN_PATH):
            screenshot_bytes = SCREEN_PATH.read_bytes()
    except Exception as e:
        print(f"screenshot failed: {e}", file=sys.stderr)

    # Elements
    elements_json = ""
    try:
        raw = platform.dump_elements_raw()
        if raw:
            elements = platform.parse_elements(raw)
            elements_count = len(elements)
            elements_json = json.dumps(elements, indent=2)
            WATCH_ELEMENTS.write_text(elements_json)
        else:
            print("ui dump skipped", file=sys.stderr)
    except Exception as e:
        print(f"element dump failed: {e}", file=sys.stderr)

    # Deduplicate: skip if elements unchanged (unless INITIAL or WINDOW_STATE_CHANGED)
    elements_hash = str(hash(elements_json)) if elements_json else ""
    if event_type not in ("INITIAL", "TYPE_WINDOW_STATE_CHANGED"):
        if elements_hash and elements_hash == _last_elements_hash:
            return False
    if elements_hash:
        _last_elements_hash = elements_hash

    # Save per-snapshot files
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"{snapshot_num:03d}"
    if screenshot_bytes:
        (WATCH_DIR / f"{prefix}-screen.png").write_bytes(screenshot_bytes)
    if elements_json:
        (WATCH_DIR / f"{prefix}-elements.json").write_text(elements_json)

    # Logcat - drain since last snapshot
    log_entries = []
    if _logcat:
        log_entries = _logcat.drain()
        if log_entries:
            (WATCH_DIR / f"{prefix}-logcat.json").write_text(
                json.dumps(log_entries, indent=2))

    # Build manifest entry with screen context
    summary = _screen_summary(elements) if elements else {}
    crashes = [e["line"] for e in log_entries if e.get("severity") == "crash"]
    entry = {
        "snapshot": snapshot_num,
        "timestamp": ts,
        "event_type": event_type,
        "elements_count": elements_count,
        "screen_title": summary.get("screen_title", ""),
        "selected_tab": summary.get("selected_tab", ""),
        "clickable_count": summary.get("clickable_count", 0),
        "log_lines": len(log_entries),
        "files": {
            "screen": str(WATCH_DIR / f"{prefix}-screen.png"),
            "elements": str(WATCH_DIR / f"{prefix}-elements.json"),
        },
    }
    if crashes:
        entry["crashes"] = crashes
    if log_entries:
        entry["files"]["logcat"] = str(WATCH_DIR / f"{prefix}-logcat.json")
    _watch_manifest_log.append(entry)

    # Write manifest atomically
    tmp_path = WATCH_MANIFEST.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(_watch_manifest_log, indent=2))
    os.replace(str(tmp_path), str(WATCH_MANIFEST))

    # Output
    tab_info = f" [{summary['selected_tab']}]" if summary.get("selected_tab") else ""
    title_info = f" {summary['screen_title']}" if summary.get("screen_title") else ""
    if json_output:
        print(json.dumps(entry), flush=True)
    else:
        short_ts = time.strftime("%H:%M:%S")
        print(f"[{short_ts}] #{snapshot_num} ({event_type}) {elements_count} elements{tab_info}{title_info}", flush=True)
    return True


def _kill_proc(proc: subprocess.Popen) -> None:
    """Terminate a process and wait for it to die."""
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _watch_poll_mode(timeout: float | None, debounce: float, json_output: bool) -> None:
    """Poll-based watch for platforms without event streams (iOS)."""
    snapshot_num = 1
    _take_snapshot("INITIAL", snapshot_num, json_output)
    deadline = time.time() + timeout if timeout else None
    poll_interval = max(debounce, 2.0)
    print(f"poll mode (every {poll_interval}s)", file=sys.stderr)

    try:
        while True:
            if deadline and time.time() >= deadline:
                print("timeout reached", file=sys.stderr)
                break
            time.sleep(poll_interval)
            snapshot_num += 1
            _take_snapshot("POLL", snapshot_num, json_output)
    except KeyboardInterrupt:
        raise


def _watch_event_mode(timeout: float | None, debounce: float, json_output: bool,
                      events_cmd: list[str]) -> None:
    """Event-based watch using a UI event stream (Android uiautomator events)."""
    snapshot_num = 1
    _take_snapshot("INITIAL", snapshot_num, json_output)
    deadline = time.time() + timeout if timeout else None
    max_retries = 3
    retry_count = 0

    lock = threading.Lock()
    last_event_type: str | None = None
    last_event_time: float = 0.0
    reader_alive = True

    def read_events(proc: subprocess.Popen) -> None:
        nonlocal last_event_type, last_event_time, reader_alive
        try:
            for line in proc.stdout:
                evt = _parse_event_line(line)
                if evt:
                    with lock:
                        last_event_type = evt
                        last_event_time = time.time()
        except Exception:
            pass
        finally:
            reader_alive = False

    while retry_count < max_retries:
        if deadline and time.time() >= deadline:
            print("timeout reached", file=sys.stderr)
            break

        try:
            proc = subprocess.Popen(
                events_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except Exception as e:
            print(f"failed to start events: {e}", file=sys.stderr)
            retry_count += 1
            if retry_count < max_retries:
                time.sleep(2)
            continue

        print("events connected", file=sys.stderr)
        retry_count = 0
        reader_alive = True
        reader = threading.Thread(target=read_events, args=(proc,), daemon=True)
        reader.start()

        intentional_restart = False
        try:
            while reader_alive:
                if deadline and time.time() >= deadline:
                    break
                with lock:
                    evt = last_event_type
                    evt_time = last_event_time
                if evt and (time.time() - evt_time) >= debounce:
                    with lock:
                        last_event_type = None
                    _kill_proc(proc)
                    reader.join(timeout=2)
                    snapshot_num += 1
                    _take_snapshot(evt, snapshot_num, json_output)
                    intentional_restart = True
                    break
                else:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            _kill_proc(proc)
            raise

        _kill_proc(proc)
        if deadline and time.time() >= deadline:
            break
        if not intentional_restart:
            retry_count += 1
            if retry_count < max_retries:
                print(f"reconnecting ({retry_count}/{max_retries})...", file=sys.stderr)
                time.sleep(2)

    if retry_count >= max_retries:
        print("max retries reached, exiting", file=sys.stderr)
        sys.exit(1)


def cmd_watch(timeout: float | None, debounce: float, json_output: bool) -> None:
    """Watch for UI changes and auto-capture snapshots."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    _watch_manifest_log.clear()
    if WATCH_DIR.exists():
        shutil.rmtree(WATCH_DIR)
    WATCH_DIR.mkdir(parents=True, exist_ok=True)

    platform.start_log_collector()

    print("watching for UI changes...", file=sys.stderr)
    if timeout:
        print(f"timeout: {timeout}s", file=sys.stderr)
    print(f"debounce: {debounce}s", file=sys.stderr)

    events_cmd = platform.watch_events_cmd()
    if events_cmd:
        _watch_event_mode(timeout, debounce, json_output, events_cmd)
    else:
        _watch_poll_mode(timeout, debounce, json_output)


def cmd_flow(flow_path: str) -> None:
    """Run a Maestro flow."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    if not Path(flow_path).exists():
        print(f"Flow not found: {flow_path}")
        sys.exit(1)

    # Start log collector to capture errors during flow execution
    lc = platform.start_log_collector()
    lc.drain()  # clear buffer before run

    print(f"Running: {flow_path}")
    start = time.perf_counter()
    code, out, err = run_cmd(
        ["maestro", "test", "-p", cfg.platform, flow_path],
        timeout=cfg.timeout_flow,
    )
    ms = int((time.perf_counter() - start) * 1000)

    # Collect logcat from during the run
    log_entries = lc.drain()
    crashes = [e for e in log_entries if e.get("severity") == "crash"]
    errors = [e for e in log_entries if e.get("severity") == "error"]

    if code == 0:
        print(f"PASS ({ms}ms)")
        if crashes:
            print(f"  WARNING: {len(crashes)} crash(es) in logcat during run")
            for c in crashes[:3]:
                print(f"    {c['line'][:120]}")
        save_progress(flow_path, True, None)
    else:
        print(f"FAIL ({ms}ms)")
        # Extract error from maestro output
        combined = out + err
        error_line = None
        for line in combined.split("\n"):
            if "assert" in line.lower() or "error" in line.lower() or "failed" in line.lower():
                error_line = line.strip()[:200]
                print(f"  {error_line[:100]}")
                break
        # Show logcat context -- this is the "why"
        if crashes:
            print(f"  CRASHES ({len(crashes)}):")
            for c in crashes[:5]:
                print(f"    {c['line'][:120]}")
        if errors:
            print(f"  ERRORS ({len(errors)}):")
            for e in errors[-5:]:
                print(f"    {e['line'][:120]}")
        # Save logcat alongside failure
        if log_entries:
            lc_path = Path("/tmp/tether-failure-logcat.json")
            lc_path.write_text(json.dumps(log_entries, indent=2))
            print(f"  Logcat: {lc_path}")
        save_progress(flow_path, False, error_line)
        # Capture failure state
        cmd_screen(str(SCREEN_PATH.parent / "tether-failure.png"))
        print(f"  Screenshot: {SCREEN_PATH.parent / 'tether-failure.png'}")
        sys.exit(1)


def save_progress(flow: str, passed: bool, error: str | None) -> None:
    """Save flow result to progress file."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    progress = {}
    if PROGRESS_FILE.exists():
        try:
            progress = json.loads(PROGRESS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    if "flows" not in progress:
        progress["flows"] = {}
    progress["flows"][flow] = {
        "passed": passed,
        "error": error,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


def cmd_smoke(dir_path: str, resume: bool = False) -> None:
    """Run all .yaml flows in a directory."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    flow_dir = Path(dir_path)
    if not flow_dir.is_dir():
        print(f"Directory not found: {dir_path}")
        sys.exit(1)

    flows = sorted(flow_dir.glob("*.yaml"))
    if not flows:
        print(f"No .yaml files in {dir_path}")
        sys.exit(1)

    # Load existing progress for --resume
    passed_flows: set[str] = set()
    if resume and PROGRESS_FILE.exists():
        try:
            progress = json.loads(PROGRESS_FILE.read_text())
            for name, data in progress.get("flows", {}).items():
                if data.get("passed"):
                    passed_flows.add(name)
        except json.JSONDecodeError:
            pass

    passed = 0
    failed = 0
    skipped = 0

    for flow in flows:
        flow_str = str(flow)

        # Skip already-passed flows in resume mode
        if resume and flow_str in passed_flows:
            print(f"SKIP {flow_str} (already passed)")
            skipped += 1
            continue

        print(f"Running: {flow_str}")
        start = time.perf_counter()
        code, out, err = run_cmd(
            ["maestro", "test", "-p", cfg.platform, flow_str],
            timeout=cfg.timeout_flow,
        )
        ms = int((time.perf_counter() - start) * 1000)

        if code == 0:
            print(f"  PASS ({ms}ms)")
            save_progress(flow_str, True, None)
            passed += 1
        else:
            print(f"  FAIL ({ms}ms)")
            combined = out + err
            error_line = None
            for line in combined.split("\n"):
                if "assert" in line.lower() or "error" in line.lower() or "failed" in line.lower():
                    error_line = line.strip()[:200]
                    print(f"    {error_line[:100]}")
                    break
            save_progress(flow_str, False, error_line)
            failed += 1

    # Summary
    total = passed + failed + skipped
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped (of {total})")
    if failed > 0:
        sys.exit(1)


def cmd_progress() -> None:
    """Show flow progress."""
    if not PROGRESS_FILE.exists():
        print("No progress recorded yet.")
        return
    progress = json.loads(PROGRESS_FILE.read_text())
    flows = progress.get("flows", {})
    if not flows:
        print("No flows recorded.")
        return
    for name, data in flows.items():
        icon = "✓" if data.get("passed") else "✗"
        print(f"{icon} {name}")
        if data.get("error"):
            print(f"    {data['error'][:80]}")


def cmd_last_error() -> None:
    """Show the most recent failure."""
    if not PROGRESS_FILE.exists():
        print("No failures recorded.")
        return
    progress = json.loads(PROGRESS_FILE.read_text())
    flows = progress.get("flows", {})

    # Find most recent failure by timestamp
    latest_name = None
    latest_ts = ""
    for name, data in flows.items():
        if not data.get("passed") and data.get("timestamp", "") > latest_ts:
            latest_name = name
            latest_ts = data["timestamp"]

    if not latest_name:
        print("No failures recorded.")
        return

    data = flows[latest_name]
    print(f"Flow: {latest_name}")
    print(f"Error: {data.get('error', 'unknown')}")
    print(f"Time: {latest_ts}")

    failure_png = Path("/tmp/tether-failure.png")
    if failure_png.exists():
        print(f"Screenshot: {failure_png}")


def cmd_logcat(lines: int = 50, follow: bool = False) -> None:
    """Show filtered log output. With --follow, streams continuously."""
    check = platform.is_device_running()
    if not check.passed:
        print("Device not running. Run: tether boot")
        sys.exit(1)

    if follow:
        lc = platform.start_log_collector()
        print("streaming logs (Ctrl+C to stop)...", file=sys.stderr)
        try:
            while True:
                entries = lc.drain()
                for e in entries:
                    sev = e.get("severity", "info")
                    prefix = {"crash": "!!!", "error": "ERR"}.get(sev, "   ")
                    print(f"{prefix} {e['line']}")
                time.sleep(0.5)
        except KeyboardInterrupt:
            lc.stop()
            print("\nstopped", file=sys.stderr)
    else:
        out = platform.stream_logs_oneshot(lines)
        if not out:
            print("log retrieval failed")
            sys.exit(1)
        app_id = cfg.app_id if cfg else ""
        for line in out.strip().split("\n"):
            if not line:
                continue
            matches = False
            if app_id and app_id in line:
                matches = True
            elif any(p.search(line) for p in _LOGCAT_PATTERNS):
                matches = True
            if matches:
                if re.search(r"FATAL|ANR|CRASH|AndroidRuntime|fault|EXC_BAD", line, re.IGNORECASE):
                    print(f"!!! {line}")
                elif re.search(r"Error|Exception|E/", line, re.IGNORECASE):
                    print(f"ERR {line}")
                else:
                    print(f"    {line}")


def cmd_progress_clear() -> None:
    """Clear progress history."""
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("Progress cleared.")
    else:
        print("No progress to clear.")


COMMAND_HELP = {
    "doctor": """
tether doctor [--fix]

Validate the test environment. Checks:
  - adb installed and server running
  - Android emulator exists and running
  - Maestro CLI installed
  - Can take screenshots
  - Can dump UI elements

Options:
  --fix    Auto-fix issues (start adb server, boot emulator)

Exit codes:
  0  All critical checks passed
  1  Critical check failed
""",
    "status": """
tether status

Quick emulator state check. Fast, no side effects.

Output:
  AVD name and running/stopped state
""",
    "boot": """
tether boot

Start the emulator if not already running.
Waits up to 90s for boot to complete.
""",
    "screen": """
tether screen [path]

Take a screenshot of the current emulator screen.

Arguments:
  path    Output path (default: /tmp/tether-screen.png)

Output:
  Prints the path to the saved screenshot.
  The image can be viewed by multimodal AI agents.
""",
    "elements": """
tether elements [--json]

Dump visible UI elements from the current screen.

Options:
  --json    Output as JSON (for programmatic use)

Output:
  Lists text content, content descriptions, and resource IDs
  that can be used as Maestro selectors.

Note: May timeout on some Android versions. Use 'screen' as fallback.
""",
    "flow": """
tether flow <file>

Run a Maestro flow file.

Arguments:
  file    Path to .yaml flow file

On success:
  Prints PASS with timing

On failure:
  Prints FAIL with error details
  Saves screenshot to /tmp/tether-failure.png
  Records result in progress history
""",
    "progress": """
tether progress [--clear]

Show test history.

Options:
  --clear    Clear all history

Output:
  List of flows with pass/fail status and any error messages.
""",
    "smoke": """
tether smoke <dir> [--resume]

Run all .yaml flow files in a directory.

Arguments:
  dir       Directory containing .yaml flow files

Options:
  --resume  Skip flows that already passed (from progress.json)

Output:
  Per-flow PASS/FAIL/SKIP with timing.
  Summary line: X passed, Y failed, Z skipped.

On failure, continues running remaining flows.
Results saved to progress history.
""",
    "last-error": """
tether last-error

Show the most recent test failure.

Output:
  Flow name, error message, and timestamp.
  If a failure screenshot exists, prints its path.
""",
    "logcat": """
tether logcat [--lines N] [--follow]

Show filtered logcat output. Only shows lines matching:
  - ReactNativeJS (JS errors, warnings, console.log)
  - FATAL / ANR / CRASH (app crashes)
  - AndroidRuntime exceptions
  - Maestro-related output
  - Your app's package ID (from tether.json appId)

Options:
  --lines N    Number of raw lines to search (default: 500)
  --follow     Stream continuously (Ctrl+C to stop)

Output prefixes:
  !!!  Crash (FATAL, ANR)
  ERR  Error/exception
       Info (ReactNativeJS log, etc.)
""",
    "watch": """
tether watch [--timeout N] [--debounce N] [--json]

Watch for UI changes and auto-capture screenshots + elements.

Listens to uiautomator events and takes a snapshot when the UI settles.
Takes an initial snapshot immediately on start.

Options:
  --timeout N    Stop after N seconds (default: run forever)
  --debounce N   Settle time in seconds (default: 1.0)
  --json         JSON-per-line output instead of human-readable

Output files:
  /tmp/tether-screen.png          Latest screenshot (overwritten)
  /tmp/tether-elements.json       Latest element dump (overwritten)
  /tmp/tether-watch.json          Manifest with all snapshots (timeline)
  /tmp/tether-watch/NNN-screen.png     Per-snapshot screenshot
  /tmp/tether-watch/NNN-elements.json  Per-snapshot elements

Stdout (human):
  [23:04:37] snapshot #3 (WINDOW_STATE_CHANGED) 42 elements

Stdout (--json):
  {"timestamp":"...","event_type":"...","elements_count":42,"snapshot_number":3}

Diagnostics print to stderr. Ctrl+C to stop.
""",
}


def print_help(cmd: str | None = None) -> None:
    """Print help for a command or general help."""
    if cmd and cmd in COMMAND_HELP:
        print(COMMAND_HELP[cmd])
    else:
        print(__doc__)


def main() -> None:
    global cfg, platform
    cfg = load_config()
    platform = get_platform()

    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_help(args[1] if len(args) > 1 else None)
        sys.exit(0)

    cmd = args[0]

    # Check for command-specific help
    if "--help" in args or "-h" in args:
        print_help(cmd)
        sys.exit(0)

    if cmd == "doctor":
        auto_fix = "--fix" in args
        cmd_doctor(auto_fix)
    elif cmd == "status":
        cmd_status()
    elif cmd == "boot":
        cmd_boot()
    elif cmd == "screen":
        output = args[1] if len(args) > 1 and not args[1].startswith("-") else None
        cmd_screen(output)
    elif cmd == "elements":
        as_json = "--json" in args
        cmd_elements(as_json)
    elif cmd == "flow":
        flow_args = [a for a in args[1:] if not a.startswith("-")]
        if not flow_args:
            print("Usage: tether flow <path>")
            print("Run 'tether help flow' for details.")
            sys.exit(1)
        cmd_flow(flow_args[0])
    elif cmd == "smoke":
        smoke_args = [a for a in args[1:] if not a.startswith("-")]
        if not smoke_args:
            print("Usage: tether smoke <dir>")
            print("Run 'tether help smoke' for details.")
            sys.exit(1)
        resume = "--resume" in args
        cmd_smoke(smoke_args[0], resume)
    elif cmd == "progress":
        if "--clear" in args:
            cmd_progress_clear()
        else:
            cmd_progress()
    elif cmd == "last-error":
        cmd_last_error()
    elif cmd == "inspect":
        cmd_inspect()
    elif cmd == "logcat":
        lc_follow = "--follow" in args
        lc_lines = 50
        for i, a in enumerate(args):
            if a == "--lines" and i + 1 < len(args):
                try:
                    lc_lines = int(args[i + 1])
                except ValueError:
                    print("--lines requires a number")
                    sys.exit(1)
        cmd_logcat(lines=lc_lines, follow=lc_follow)
    elif cmd == "watch":
        w_timeout = None
        w_debounce = 1.0
        w_json = "--json" in args
        for i, a in enumerate(args):
            if a == "--timeout" and i + 1 < len(args):
                try:
                    w_timeout = float(args[i + 1])
                except ValueError:
                    print("--timeout requires a number")
                    sys.exit(1)
            if a == "--debounce" and i + 1 < len(args):
                try:
                    w_debounce = float(args[i + 1])
                except ValueError:
                    print("--debounce requires a number")
                    sys.exit(1)
        try:
            cmd_watch(w_timeout, w_debounce, w_json)
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'tether --help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    main()
