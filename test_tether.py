#!/usr/bin/env python3
"""Unit tests for tether CLI."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Load tether module by exec'ing everything before main()
_cli_src = Path(__file__).parent / "src" / "tether" / "cli.py"
_code = _cli_src.read_text().split("\ndef main")[0]
_ns: dict = {}
exec(compile(_code, str(_cli_src), "exec"), _ns)

# Pull functions/classes into module scope
parse_ui_tree = _ns["parse_ui_tree"]
parse_axe_tree = _ns["parse_axe_tree"]
_parse_bounds = _ns["_parse_bounds"]
_resolve_element_name = _ns["_resolve_element_name"]
_format_element_line = _ns["_format_element_line"]
find_config_file = _ns["find_config_file"]
load_config = _ns["load_config"]
get_platform = _ns["get_platform"]
Config = _ns["Config"]
CheckResult = _ns["CheckResult"]
DoctorReport = _ns["DoctorReport"]
Platform = _ns["Platform"]
AndroidPlatform = _ns["AndroidPlatform"]
IOSPlatform = _ns["IOSPlatform"]
NOISE_CLASSES = _ns["NOISE_CLASSES"]
SYSTEM_RES_IDS = _ns["SYSTEM_RES_IDS"]
IOS_NOISE_ROLES = _ns["IOS_NOISE_ROLES"]
ET = _ns["ET"]
parse_sniff_output = _ns["parse_sniff_output"]
redact_sniff_entry = _ns["redact_sniff_entry"]
_redact_header = _ns["_redact_header"]
SniffCapture = _ns["SniffCapture"]
CAPTURES_DIR = _ns["CAPTURES_DIR"]
SNIFF_LAST = _ns["SNIFF_LAST"]
_MITM_ADDON_TEMPLATE = _ns["_MITM_ADDON_TEMPLATE"]
check_mitmproxy_installed = _ns["check_mitmproxy_installed"]
check_mitmproxy_ca = _ns["check_mitmproxy_ca"]
read_audit_events = _ns["read_audit_events"]
cmd_open_url = _ns["cmd_open_url"]
_maestro_tap_flow_yaml = _ns["_maestro_tap_flow_yaml"]
_element_center_point = _ns["_element_center_point"]


# === Android Element Parsing ===

SAMPLE_ANDROID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]"
        enabled="true" clickable="false" text="" content-desc="" resource-id="">
    <node class="android.widget.TextView" bounds="[50,100][500,150]"
          enabled="true" clickable="false" text="Welcome" content-desc=""
          resource-id="com.app:id/title" />
    <node class="android.widget.Button" bounds="[50,200][300,260]"
          enabled="true" clickable="true" text="Login"
          content-desc="login-button" resource-id="com.app:id/login_btn" />
    <node class="android.widget.EditText" bounds="[50,300][500,360]"
          enabled="true" clickable="true" focusable="true" text=""
          content-desc="email-input" resource-id="com.app:id/email" />
    <node class="android.view.View" bounds="[0,0][0,0]"
          enabled="true" clickable="false" text="" content-desc="" resource-id="" />
    <node class="android.widget.FrameLayout" bounds="[0,1800][1080,1920]"
          enabled="true" clickable="false" text="" content-desc=""
          resource-id="android:id/navigationBarBackground" />
    <node class="android.widget.LinearLayout" bounds="[0,0][1080,100]"
          enabled="true" clickable="false" text="" content-desc="" resource-id="" />
    <node class="android.widget.CheckBox" bounds="[50,400][100,450]"
          enabled="true" clickable="true" checked="true" text="Remember me"
          content-desc="" resource-id="" />
    <node class="android.widget.ScrollView" bounds="[0,500][1080,1800]"
          enabled="true" clickable="false" scrollable="true" text=""
          content-desc="" resource-id="" />
    <node class="android.widget.Button" bounds="[50,600][300,660]"
          enabled="false" clickable="true" text="Submit"
          content-desc="" resource-id="" />
  </node>
</hierarchy>"""


class TestParseBounds(unittest.TestCase):
    def test_valid_bounds(self):
        self.assertEqual(_parse_bounds("[0,0][1080,1920]"), (0, 0, 1080, 1920))

    def test_single_digit(self):
        self.assertEqual(_parse_bounds("[5,3][9,7]"), (5, 3, 9, 7))

    def test_invalid(self):
        self.assertIsNone(_parse_bounds(""))
        self.assertIsNone(_parse_bounds("invalid"))
        self.assertIsNone(_parse_bounds("[0,0]"))


class TestResolveElementName(unittest.TestCase):
    def test_simple_text(self):
        node = ET.fromstring('<node text="Hello" content-desc="" />')
        self.assertEqual(_resolve_element_name(node), "Hello")

    def test_compound_text(self):
        xml = '<node text="Title" content-desc=""><child text="Subtitle" clickable="false" /></node>'
        node = ET.fromstring(xml)
        self.assertEqual(_resolve_element_name(node), "Title | Subtitle")

    def test_skips_clickable_children(self):
        xml = '<node text="Parent" content-desc=""><child text="Skip" clickable="true" /></node>'
        node = ET.fromstring(xml)
        self.assertEqual(_resolve_element_name(node), "Parent")

    def test_no_duplicates(self):
        xml = '<node text="Same" content-desc=""><child text="Same" clickable="false" /></node>'
        node = ET.fromstring(xml)
        self.assertEqual(_resolve_element_name(node), "Same")

    def test_empty(self):
        node = ET.fromstring('<node text="" content-desc="" />')
        self.assertEqual(_resolve_element_name(node), "")


class TestParseUiTree(unittest.TestCase):
    def test_filters_noise_containers(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        types = [e.get("type", "") for e in elements]
        self.assertNotIn("FrameLayout", types)
        self.assertNotIn("LinearLayout", types)

    def test_filters_system_resource_ids(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        res_ids = [e.get("resourceId", "") for e in elements]
        for sys_id in SYSTEM_RES_IDS:
            self.assertNotIn(sys_id, res_ids)

    def test_filters_zero_area(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        for el in elements:
            if "bounds" in el:
                b = _parse_bounds(el["bounds"])
                self.assertIsNotNone(b)
                x1, y1, x2, y2 = b
                self.assertGreater(x2, x1)
                self.assertGreater(y2, y1)

    def test_keeps_meaningful_elements(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        texts = [e.get("text", "") for e in elements]
        self.assertIn("Welcome", texts)
        self.assertIn("Login", texts)
        self.assertIn("Remember me", texts)
        self.assertIn("Submit", texts)

    def test_keeps_interactive_without_content(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        scrollables = [e for e in elements if e.get("scrollable")]
        self.assertTrue(len(scrollables) >= 1)

    def test_assigns_refs(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML, assign_refs=True)
        for i, el in enumerate(elements):
            self.assertEqual(el["ref"], f"@e{i+1}")

    def test_no_refs_when_disabled(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML, assign_refs=False)
        for el in elements:
            self.assertNotIn("ref", el)

    def test_clickable_flag(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        login_btn = [e for e in elements if e.get("text") == "Login"][0]
        self.assertTrue(login_btn.get("clickable"))

    def test_disabled_flag(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        submit_btn = [e for e in elements if e.get("text") == "Submit"][0]
        self.assertFalse(submit_btn.get("enabled", True))

    def test_checked_flag(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        checkbox = [e for e in elements if e.get("text") == "Remember me"][0]
        self.assertTrue(checkbox.get("checked"))

    def test_content_desc_as_id(self):
        elements = parse_ui_tree(SAMPLE_ANDROID_XML)
        login_btn = [e for e in elements if e.get("text") == "Login"][0]
        self.assertEqual(login_btn.get("id"), "login-button")

    def test_empty_xml(self):
        self.assertEqual(parse_ui_tree(""), [])

    def test_invalid_xml(self):
        self.assertEqual(parse_ui_tree("<broken"), [])

    def test_minimal_hierarchy(self):
        xml = '<hierarchy><node class="android.widget.TextView" text="Hi" bounds="[0,0][100,50]" enabled="true" clickable="false" content-desc="" resource-id="" /></hierarchy>'
        elements = parse_ui_tree(xml)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["text"], "Hi")


# === iOS Element Parsing ===

SAMPLE_AXE_JSON = json.dumps([
    {
        "type": "AXWindow",
        "frame": {"x": 0, "y": 0, "width": 390, "height": 844},
        "children": [
            {
                "type": "AXGroup",
                "frame": {"x": 0, "y": 0, "width": 390, "height": 844},
                "children": [
                    {
                        "type": "AXStaticText",
                        "AXLabel": "Welcome to MyApp",
                        "frame": {"x": 50, "y": 100, "width": 290, "height": 30},
                    },
                    {
                        "type": "AXButton",
                        "AXLabel": "Get Started",
                        "AXUniqueId": "get-started-btn",
                        "frame": {"x": 50, "y": 200, "width": 290, "height": 44},
                    },
                    {
                        "type": "AXTextField",
                        "AXLabel": "Email",
                        "AXUniqueId": "email-field",
                        "value": "",
                        "frame": {"x": 50, "y": 300, "width": 290, "height": 44},
                    },
                    {
                        "type": "AXGroup",
                        "frame": {"x": 0, "y": 0, "width": 0, "height": 0},
                    },
                    {
                        "type": "AXImage",
                        "frame": {"x": 50, "y": 400, "width": 100, "height": 100},
                    },
                    {
                        "type": "AXCell",
                        "AXLabel": "Sleep Tracker",
                        "AXUniqueId": "sleep-cell",
                        "frame": {"x": 0, "y": 500, "width": 390, "height": 60},
                    },
                    {
                        "type": "AXButton",
                        "AXLabel": "",
                        "AXUniqueId": "",
                        "frame": {"x": 300, "y": 700, "width": 44, "height": 44},
                    },
                ],
            },
        ],
    }
])


class TestParseAxeTree(unittest.TestCase):
    def test_filters_noise_roles(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        types = [e.get("type", "") for e in elements]
        self.assertNotIn("Window", types)

    def test_filters_zero_area(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        for el in elements:
            if "bounds" in el:
                b = _parse_bounds(el["bounds"])
                self.assertIsNotNone(b)
                x1, y1, x2, y2 = b
                self.assertGreater(x2, x1)
                self.assertGreater(y2, y1)

    def test_filters_no_content_non_interactive(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        types = [e.get("type", "") for e in elements]
        self.assertNotIn("Image", types)

    def test_keeps_meaningful_elements(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        texts = [e.get("text", "") for e in elements]
        self.assertIn("Welcome to MyApp", texts)
        self.assertIn("Get Started", texts)
        self.assertIn("Email", texts)
        self.assertIn("Sleep Tracker", texts)

    def test_keeps_interactive_button_without_label(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        buttons = [e for e in elements if e.get("type") == "Button"]
        self.assertTrue(len(buttons) >= 2)

    def test_assigns_refs(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON, assign_refs=True)
        for i, el in enumerate(elements):
            self.assertEqual(el["ref"], f"@e{i+1}")

    def test_no_refs_when_disabled(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON, assign_refs=False)
        for el in elements:
            self.assertNotIn("ref", el)

    def test_strips_ax_prefix(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        for el in elements:
            t = el.get("type", "")
            self.assertFalse(t.startswith("AX"), f"Type still has AX prefix: {t}")

    def test_unique_id_as_id(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        btn = [e for e in elements if e.get("text") == "Get Started"][0]
        self.assertEqual(btn.get("id"), "get-started-btn")

    def test_clickable_on_buttons_and_textfields(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        btn = [e for e in elements if e.get("text") == "Get Started"][0]
        self.assertTrue(btn.get("clickable"))
        tf = [e for e in elements if e.get("text") == "Email"][0]
        self.assertTrue(tf.get("clickable"))

    def test_bounds_format(self):
        elements = parse_axe_tree(SAMPLE_AXE_JSON)
        btn = [e for e in elements if e.get("text") == "Get Started"][0]
        self.assertEqual(btn["bounds"], "[50,200][340,244]")

    def test_empty_json(self):
        self.assertEqual(parse_axe_tree(""), [])

    def test_invalid_json(self):
        self.assertEqual(parse_axe_tree("{broken"), [])

    def test_single_root_dict(self):
        single = json.dumps({
            "type": "AXStaticText",
            "AXLabel": "Hello",
            "frame": {"x": 0, "y": 0, "width": 100, "height": 30},
        })
        elements = parse_axe_tree(single)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["text"], "Hello")


# === Format Element Line ===

class TestFormatElementLine(unittest.TestCase):
    def test_with_ref_and_text(self):
        line = _format_element_line({"ref": "@e1", "text": "Hello"})
        self.assertIn("@e1", line)
        self.assertIn('"Hello"', line)

    def test_with_id(self):
        line = _format_element_line({"ref": "@e2", "id": "my-btn"})
        self.assertIn('id="my-btn"', line)

    def test_with_resource_id(self):
        line = _format_element_line({"ref": "@e3", "resourceId": "com.app:id/btn"})
        self.assertIn("res=com.app:id/btn", line)

    def test_clickable_flag(self):
        line = _format_element_line({"ref": "@e4", "text": "X", "clickable": True})
        self.assertIn("[clickable]", line)

    def test_disabled_flag(self):
        line = _format_element_line({"ref": "@e5", "text": "X", "enabled": False})
        self.assertIn("DISABLED", line)

    def test_scrollable_flag(self):
        line = _format_element_line({"ref": "@e6", "type": "ScrollView", "scrollable": True})
        self.assertIn("[scrollable]", line)

    def test_selected_flag(self):
        line = _format_element_line({"ref": "@e7", "text": "Tab", "selected": True})
        self.assertIn("selected", line)

    def test_no_ref(self):
        line = _format_element_line({"text": "Hello"})
        self.assertNotIn("@e", line)
        self.assertIn('"Hello"', line)

    def test_name_over_text(self):
        line = _format_element_line({"ref": "@e1", "name": "A | B"})
        self.assertIn('"A | B"', line)

    def test_fallback_to_type(self):
        line = _format_element_line({"ref": "@e1", "type": "View"})
        self.assertIn("View", line)


# === Config Loading ===

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                # Patch cfg global so load_config works
                _ns["cfg"] = None
                c = load_config()
                self.assertEqual(c.platform, "android")
                self.assertEqual(c.avd, "Pixel_XL_API_29")
                self.assertEqual(c.simulator, "")
                self.assertEqual(c.timeout_boot, 90)
            finally:
                os.chdir(old_cwd)

    def test_loads_json(self):
        with tempfile.TemporaryDirectory() as d:
            config = {
                "platform": "ios",
                "simulator": "ABC-123",
                "appId": "com.test.app",
                "timeouts": {"boot": 60},
            }
            (Path(d) / "tether.json").write_text(json.dumps(config))
            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                _ns["cfg"] = None
                c = load_config()
                self.assertEqual(c.platform, "ios")
                self.assertEqual(c.simulator, "ABC-123")
                self.assertEqual(c.app_id, "com.test.app")
                self.assertEqual(c.timeout_boot, 60)
                self.assertEqual(c.timeout_flow, 180)  # default preserved
            finally:
                os.chdir(old_cwd)

    def test_env_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                _ns["cfg"] = None
                with patch.dict(os.environ, {"TETHER_AVD": "MyAVD", "TETHER_SIMULATOR": "SIM-456"}):
                    c = load_config()
                    self.assertEqual(c.avd, "MyAVD")
                    self.assertEqual(c.simulator, "SIM-456")
            finally:
                os.chdir(old_cwd)


# === Platform Selection ===

class TestAndroidLaunch(unittest.TestCase):
    def setUp(self):
        _ns["cfg"] = Config(
            platform="android", avd="test", app_id="com.test.app", android_home="/tmp",
            emulator_bin="/tmp/emulator", simulator="",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )

    def test_resolve_launch_activity_uses_package_resolver(self):
        p = AndroidPlatform()
        out = "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\ncom.test.app/com.example.MainActivity\n"
        with patch.dict(p._resolve_launch_activity.__globals__, {"run_cmd": lambda *a, **k: (0, out, "")}):
            self.assertEqual(p._resolve_launch_activity(), "com.test.app/com.example.MainActivity")

    def test_resolve_launch_activity_falls_back_to_dot_main_activity(self):
        p = AndroidPlatform()
        with patch.dict(p._resolve_launch_activity.__globals__, {"run_cmd": lambda *a, **k: (1, "", "error")}):
            self.assertEqual(p._resolve_launch_activity(), "com.test.app/.MainActivity")

    def test_launch_accepts_explicit_app_id(self):
        p = AndroidPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            if "resolve-activity" in args:
                return (0, "com.other.app/.MainActivity\n", "")
            return (0, "Starting", "")

        with patch.dict(p.launch_app.__globals__, {"run_cmd": fake_run_cmd}):
            p.launch_app("com.other.app")

        self.assertEqual(calls[0][-1], "com.other.app")
        self.assertEqual(calls[1], ["adb", "shell", "am", "start", "-W", "-n", "com.other.app/.MainActivity"])

    def test_close_accepts_explicit_app_id(self):
        p = AndroidPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch.dict(p.close_app.__globals__, {"run_cmd": fake_run_cmd}):
            p.close_app("com.other.app")

        self.assertEqual(calls[0], ["adb", "shell", "am", "force-stop", "com.other.app"])

    def test_open_url_uses_android_view_intent(self):
        p = AndroidPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch.dict(p.open_url.__globals__, {"run_cmd": fake_run_cmd}):
            p.open_url("example://path?x=1")

        self.assertEqual(
            calls[0],
            [
                "adb", "shell", "am", "start", "-W", "-a",
                "android.intent.action.VIEW", "-d", "example://path?x=1",
            ],
        )


class TestIOSLaunch(unittest.TestCase):
    def setUp(self):
        _ns["cfg"] = Config(
            platform="ios", avd="", app_id="com.test.app", android_home="/tmp",
            emulator_bin="/tmp/emulator", simulator="SIM-123",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )

    def test_launch_accepts_explicit_app_id(self):
        p = IOSPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch.dict(p.launch_app.__globals__, {"run_cmd": fake_run_cmd}):
            p.launch_app("com.other.app")

        self.assertEqual(calls[0], ["xcrun", "simctl", "launch", "SIM-123", "com.other.app"])

    def test_close_terminates_app(self):
        p = IOSPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch.dict(p.close_app.__globals__, {"run_cmd": fake_run_cmd}):
            p.close_app("com.other.app")

        self.assertEqual(calls[0], ["xcrun", "simctl", "terminate", "SIM-123", "com.other.app"])

    def test_open_url_uses_maestro_when_app_id_is_configured(self):
        p = IOSPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch.dict(p.open_url.__globals__, {"run_cmd": fake_run_cmd}):
            with patch("shutil.which", return_value="/usr/local/bin/maestro"):
                p.open_url("example://path?x=1")

        self.assertEqual(calls[0][:5], ["maestro", "--platform=ios", "--device", "SIM-123", "test"])

    def test_open_url_falls_back_to_simctl_without_app_id(self):
        _ns["cfg"] = Config(
            platform="ios", avd="", app_id="", android_home="/tmp",
            emulator_bin="/tmp/emulator", simulator="SIM-123",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )
        p = IOSPlatform()
        calls = []

        def fake_run_cmd(args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch.dict(p.open_url.__globals__, {"run_cmd": fake_run_cmd}):
            p.open_url("example://path?x=1")

        self.assertEqual(calls[0], ["xcrun", "simctl", "openurl", "SIM-123", "example://path?x=1"])


class TestPlatformSelection(unittest.TestCase):
    def test_android_default(self):
        _ns["cfg"] = Config(
            platform="android", avd="test", app_id="", android_home="/tmp",
            emulator_bin="/tmp/emulator", simulator="",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )
        p = get_platform()
        self.assertIsInstance(p, AndroidPlatform)

    def test_ios_selection(self):
        _ns["cfg"] = Config(
            platform="ios", avd="", app_id="", android_home="/tmp",
            emulator_bin="/tmp/emulator", simulator="ABC-123",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )
        p = get_platform()
        self.assertIsInstance(p, IOSPlatform)


# === DoctorReport ===

class TestDoctorReport(unittest.TestCase):
    def test_all_passed(self):
        r = DoctorReport()
        r.add(CheckResult("a", True, "ok"))
        r.add(CheckResult("b", True, "ok"))
        self.assertTrue(r.all_passed)
        self.assertTrue(r.critical_passed)

    def test_critical_failure(self):
        r = DoctorReport()
        r.add(CheckResult("a", True, "ok"))
        r.add(CheckResult("b", False, "fail", critical=True))
        self.assertFalse(r.all_passed)
        self.assertFalse(r.critical_passed)

    def test_non_critical_warning(self):
        r = DoctorReport()
        r.add(CheckResult("a", True, "ok"))
        r.add(CheckResult("b", False, "warn", critical=False))
        self.assertFalse(r.all_passed)
        self.assertTrue(r.critical_passed)


# === Cross-Platform Consistency ===

class TestCrossPlatformConsistency(unittest.TestCase):
    """Verify Android and iOS parsers produce the same output structure."""

    def test_same_keys_available(self):
        android_el = parse_ui_tree(SAMPLE_ANDROID_XML)[0]
        ios_el = parse_axe_tree(SAMPLE_AXE_JSON)[0]
        # Both should have ref, type, bounds at minimum
        for key in ("ref", "type", "bounds"):
            self.assertIn(key, android_el, f"Android element missing '{key}'")
            self.assertIn(key, ios_el, f"iOS element missing '{key}'")

    def test_bounds_format_consistent(self):
        """Both platforms should use [x1,y1][x2,y2] format."""
        import re
        pattern = r"^\[\d+,\d+\]\[\d+,\d+\]$"
        for el in parse_ui_tree(SAMPLE_ANDROID_XML):
            if "bounds" in el:
                self.assertRegex(el["bounds"], pattern)
        for el in parse_axe_tree(SAMPLE_AXE_JSON):
            if "bounds" in el:
                self.assertRegex(el["bounds"], pattern)

    def test_ref_format_consistent(self):
        """Both platforms should use @eN format."""
        import re
        pattern = r"^@e\d+$"
        for el in parse_ui_tree(SAMPLE_ANDROID_XML):
            self.assertRegex(el["ref"], pattern)
        for el in parse_axe_tree(SAMPLE_AXE_JSON):
            self.assertRegex(el["ref"], pattern)

    def test_format_element_line_works_for_both(self):
        """_format_element_line should work with elements from either platform."""
        for el in parse_ui_tree(SAMPLE_ANDROID_XML):
            line = _format_element_line(el)
            self.assertIsInstance(line, str)
            self.assertTrue(len(line) > 0)
        for el in parse_axe_tree(SAMPLE_AXE_JSON):
            line = _format_element_line(el)
            self.assertIsInstance(line, str)
            self.assertTrue(len(line) > 0)


# === Tap ===


class TestTapCommand(unittest.TestCase):
    def test_generates_text_tap_flow(self):
        cfg = Config(
            platform="ios", avd="Pixel_XL_API_29", app_id="com.example.app",
            android_home="/tmp/android", emulator_bin="emulator", simulator="booted",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )
        with patch.dict(_ns, {"cfg": cfg}):
            self.assertEqual(
                _maestro_tap_flow_yaml({"text": "Cancel"}),
                'appId: com.example.app\n---\n- tapOn:\n    text: "Cancel"\n',
            )

    def test_generates_id_tap_flow(self):
        cfg = Config(
            platform="android", avd="Pixel_XL_API_29", app_id="com.example.app",
            android_home="/tmp/android", emulator_bin="emulator", simulator="booted",
            timeout_boot=90, timeout_flow=180, timeout_screenshot=10,
        )
        with patch.dict(_ns, {"cfg": cfg}):
            self.assertEqual(
                _maestro_tap_flow_yaml({"id": "Storybook.ListView.SearchBar"}),
                'appId: com.example.app\n---\n- tapOn:\n    id: "Storybook.ListView.SearchBar"\n',
            )

    def test_calculates_center_point_from_android_bounds(self):
        self.assertEqual(
            _element_center_point({"bounds": "[10,20][30,60]"}),
            "20,40",
        )

    def test_calculates_center_point_from_ios_frame(self):
        self.assertEqual(
            _element_center_point({"frame": {"x": 10, "y": 20, "width": 30, "height": 40}}),
            "25,40",
        )


# === Sniff ===

SAMPLE_SNIFF_NDJSON = (
    '{"timestamp":"2026-03-10T09:30:00Z","method":"POST","url":"https://api.purchasely.io/v3/subscriptions",'
    '"request_headers":{"Authorization":"Bearer eyJhbGciOiJS...token","Content-Type":"application/json"},'
    '"request_body":{"product_id":"premium_monthly"},"status_code":403,'
    '"response_headers":{"Content-Type":"application/json"},'
    '"response_body":{"error":"token_expired"},"duration_ms":230}\n'
    '{"timestamp":"2026-03-10T09:30:01Z","method":"GET","url":"https://googleapis.com/auth/token",'
    '"request_headers":{"Cookie":"session=abc123def456"},'
    '"request_body":null,"status_code":200,'
    '"response_headers":{},"response_body":{"token":"new"},"duration_ms":50}\n'
)


class TestRedactHeader(unittest.TestCase):

    def test_short_value_unchanged(self):
        self.assertEqual(_redact_header("abc"), "abc")

    def test_exactly_ten_unchanged(self):
        self.assertEqual(_redact_header("0123456789"), "0123456789")

    def test_long_value_redacted(self):
        result = _redact_header("Bearer eyJhbGciOiJS")
        self.assertEqual(result, "Bearer eyJ...redacted")
        self.assertNotIn("OiJS", result)


class TestRedactSniffEntry(unittest.TestCase):

    def test_redacts_authorization(self):
        entry = {
            "request_headers": {"Authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"},
            "status_code": 200,
        }
        result = redact_sniff_entry(entry)
        self.assertTrue(result["request_headers"]["Authorization"].endswith("...redacted"))

    def test_redacts_cookie(self):
        entry = {
            "request_headers": {"Cookie": "session=abc123def456ghijk"},
            "status_code": 200,
        }
        result = redact_sniff_entry(entry)
        self.assertTrue(result["request_headers"]["Cookie"].endswith("...redacted"))

    def test_preserves_other_headers(self):
        entry = {
            "request_headers": {"Content-Type": "application/json", "Authorization": "Bearer longtokenvalue"},
            "status_code": 200,
        }
        result = redact_sniff_entry(entry)
        self.assertEqual(result["request_headers"]["Content-Type"], "application/json")

    def test_no_headers_key(self):
        entry = {"status_code": 200}
        result = redact_sniff_entry(entry)
        self.assertEqual(result["status_code"], 200)

    def test_does_not_mutate_original(self):
        entry = {
            "request_headers": {"Authorization": "Bearer longtokenvalue"},
        }
        original_val = entry["request_headers"]["Authorization"]
        redact_sniff_entry(entry)
        self.assertEqual(entry["request_headers"]["Authorization"], original_val)


class TestParseSniffOutput(unittest.TestCase):

    def test_parses_ndjson(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            f.write(SAMPLE_SNIFF_NDJSON)
            f.flush()
            entries = parse_sniff_output(f.name)
        os.unlink(f.name)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["method"], "POST")
        self.assertEqual(entries[0]["status_code"], 403)
        self.assertEqual(entries[1]["method"], "GET")

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            f.write("")
            f.flush()
            entries = parse_sniff_output(f.name)
        os.unlink(f.name)
        self.assertEqual(entries, [])

    def test_missing_file(self):
        entries = parse_sniff_output("/tmp/nonexistent-tether-test.ndjson")
        self.assertEqual(entries, [])

    def test_ignores_bad_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            f.write("not json\n")
            f.write('{"valid": true}\n')
            f.write("\n")
            f.write("also bad\n")
            f.flush()
            entries = parse_sniff_output(f.name)
        os.unlink(f.name)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["valid"])

    def test_preserves_all_fields(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            f.write(SAMPLE_SNIFF_NDJSON)
            f.flush()
            entries = parse_sniff_output(f.name)
        os.unlink(f.name)
        first = entries[0]
        for key in ("timestamp", "method", "url", "request_headers",
                     "request_body", "status_code", "response_headers",
                     "response_body", "duration_ms"):
            self.assertIn(key, first)


class TestSniffCapture(unittest.TestCase):

    def test_init_defaults(self):
        cap = SniffCapture()
        self.assertEqual(cap.filter_domains, [])
        self.assertEqual(cap.port, 8080)
        self.assertIsNone(cap._proc)

    def test_init_custom(self):
        cap = SniffCapture(filter_domains=["purchasely", "googleapis"], port=9090)
        self.assertEqual(cap.filter_domains, ["purchasely", "googleapis"])
        self.assertEqual(cap.port, 9090)

    def test_write_addon_creates_file(self):
        cap = SniffCapture(filter_domains=["test.com"])
        with tempfile.NamedTemporaryFile(suffix=".ndjson", delete=False) as out:
            addon_path = cap._write_addon(out.name)
        self.assertTrue(os.path.exists(addon_path))
        with open(addon_path) as f:
            content = f.read()
        self.assertIn("test.com", content)
        self.assertIn("TetherAddon", content)
        self.assertIn("def response", content)
        self.assertIn("_redact", content)
        os.unlink(addon_path)
        os.unlink(out.name)


class TestMitmAddonTemplate(unittest.TestCase):

    def test_template_is_valid_python(self):
        rendered = _MITM_ADDON_TEMPLATE.format(
            filter_domains=["example.com"],
            output_file="/tmp/test.ndjson",
        )
        compile(rendered, "<addon>", "exec")

    def test_template_with_empty_filters(self):
        rendered = _MITM_ADDON_TEMPLATE.format(
            filter_domains=[],
            output_file="/tmp/test.ndjson",
        )
        compile(rendered, "<addon>", "exec")

    def test_template_contains_required_hooks(self):
        rendered = _MITM_ADDON_TEMPLATE.format(
            filter_domains=[],
            output_file="/tmp/test.ndjson",
        )
        self.assertIn("def request(self, flow", rendered)
        self.assertIn("def response(self, flow", rendered)
        self.assertIn("addons = [TetherAddon()]", rendered)


class TestAuditCommands(unittest.TestCase):

    def test_read_audit_events_filters_by_run_surface_and_name(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(json.dumps({"runId": "run-1", "surface": "navigation", "name": "received"}) + "\n")
            f.write(json.dumps({"runId": "run-2", "surface": "navigation", "name": "received"}) + "\n")
            f.write("not-json\n")
            audit_path = Path(f.name)
        previous = _ns["AUDIT_FILE"]
        _ns["AUDIT_FILE"] = audit_path
        try:
            self.assertEqual(
                read_audit_events(run_id="run-1", surface="navigation", name="received"),
                [{"runId": "run-1", "surface": "navigation", "name": "received"}],
            )
        finally:
            _ns["AUDIT_FILE"] = previous
            audit_path.unlink(missing_ok=True)

    def test_open_url_json_collects_audit_events_and_logs(self):
        class FakeCollector:
            def drain(self):
                return [{"line": "log", "severity": "info"}]

        class FakePlatform:
            def is_device_running(self):
                return CheckResult("device", True, "ok", 1)

            def start_log_collector(self):
                return FakeCollector()

            def open_url(self, url):
                print(f"opened {url}")

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(json.dumps({"runId": "run-1", "surface": "navigation", "name": "agentScreen.received"}) + "\n")
            audit_path = Path(f.name)
        previous_platform = _ns.get("platform")
        previous_audit = _ns["AUDIT_FILE"]
        _ns["platform"] = FakePlatform()
        _ns["AUDIT_FILE"] = audit_path
        try:
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                cmd_open_url("app://test", audit_run_id="run-1", json_output=True)
            output = json.loads(stdout.getvalue())
            self.assertTrue(output["opened"])
            self.assertEqual(output["message"], "opened app://test")
            self.assertEqual(output["auditEvents"][0]["runId"], "run-1")
            self.assertTrue(Path(output["logsPath"]).exists())
        finally:
            _ns["platform"] = previous_platform
            _ns["AUDIT_FILE"] = previous_audit
            audit_path.unlink(missing_ok=True)


class TestSniffDoctorChecks(unittest.TestCase):

    def test_check_mitmproxy_returns_check_result(self):
        result = check_mitmproxy_installed()
        self.assertIsInstance(result, CheckResult)
        self.assertEqual(result.name, "mitmproxy installed")
        self.assertFalse(result.critical)

    def test_check_ca_returns_check_result(self):
        result = check_mitmproxy_ca()
        self.assertIsInstance(result, CheckResult)
        self.assertEqual(result.name, "mitmproxy CA cert")
        self.assertFalse(result.critical)


if __name__ == "__main__":
    unittest.main()
