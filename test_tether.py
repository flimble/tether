#!/usr/bin/env python3
"""Unit tests for tether CLI."""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
