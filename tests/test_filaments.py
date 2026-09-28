import configparser

from jogger import filaments
from jogger.config import Store, profile


def test_parse_lanes_matches_the_plugin_format():
    value = {
        "tool1": {"lane": "1", "material": "petg", "color": "#00ff00", "nozzle_temp": 245},
        "tool0": {"lane": "0", "material": "PLA", "color": "FF0000"},
        "tool2": {"lane": "2", "material": "UNSET", "color": "123456"},
        "tool40": {"material": "PLA"},        # beyond the plugin's 32-tool limit
        "extra": {"material": "PLA"},         # not a lane
        "tool3": "garbage",
    }
    assert filaments.parse_lanes(value) == [
        {"tool": 0, "material": "PLA", "color": "FF0000"},
        {"tool": 1, "material": "PETG", "color": "00FF00"},
        {"tool": 2, "material": "", "color": ""},
    ]
    assert filaments.parse_lanes(None) == []


def test_normalize_color_like_the_plugin():
    assert filaments.normalize_color("#ff8800") == "FF8800"
    assert filaments.normalize_color("0xFF8800CC") == "FF8800"
    assert filaments.normalize_color("orange") == "000000"


def test_summary():
    lane = lambda tool, material: {"tool": tool, "material": material, "color": "FFFFFF"}
    assert filaments.summary([]) == ""
    assert filaments.summary([lane(0, "PLA")]) == "PLA"
    assert filaments.summary([lane(0, "PLA"), lane(1, "")]) == "PLA · Empty"
    assert filaments.summary([lane(0, ""), lane(1, "")]) == "Empty"
    assert filaments.summary([lane(i, "PLA" if i < 3 else "") for i in range(4)]) == "3/4 loaded"


def test_generated_config_has_the_gated_menu_entry(tmp_path):
    store = Store(tmp_path)
    store.put(profile("Voron", "http://voron.local:7125"))
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(store.generate())
    section = cfg[filaments.MENU_SECTION]
    assert section["panel"] == "maeve_filaments"
    assert section["icon"] == "spool"
    assert "set_tool_filament" in section["enable"]


def test_menu_condition_renders_with_jinja():
    jinja2 = __import__("pytest").importorskip("jinja2")
    template = jinja2.Environment().from_string(filaments.MENU_ITEM["enable"])
    with_plugin = {"gcode_macros": {"list": ["PRINT_START", "SET_TOOL_FILAMENT"]}}
    lower_case = {"gcode_macros": {"list": ["set_tool_filament"]}}
    without = {"gcode_macros": {"list": ["PRINT_START"]}}
    assert template.render(printer=with_plugin) == "True"
    assert template.render(printer=lower_case) == "True"
    assert template.render(printer=without) == "False"
