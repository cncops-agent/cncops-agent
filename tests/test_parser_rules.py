"""口语解析、点位目录与阈值规则的单元测试。"""

from cncops import catalog, datasource, parser, rules
from cncops.simulator import Simulator


def test_alias_resolution():
    """设备别名解析：一号机/二号机/空压机都要认得出来。"""
    assert catalog.resolve_device("二号机声音不对").id == "CNC-02"
    assert catalog.resolve_device("一号机发烫").id == "CNC-01"
    assert catalog.resolve_device("空压机没劲儿").id == "AIR-01"
    assert catalog.resolve_device("气泵不打压").id == "AIR-01"
    assert catalog.resolve_device("今天天气不错") is None


def test_point_namespace_no_overlap():
    """空压机不得借用 CNC 点位：不同设备之间不允许出现同名测点。"""
    overlap = catalog.point_namespace_overlap()
    # status / alarm_code 是派生字段，三台设备共用属设计内的例外
    assert set(overlap) <= {"status", "alarm_code"}, f"出现跨类型点位借用：{overlap}"


def test_symptom_expansion():
    """口语症状映射到测点，并做同义词扩展。"""
    p = parser.parse("二号机声音不对还发烫")
    assert p.device_id == "CNC-02"
    assert "spindle_vib" in p.symptom_points
    assert "spindle_temp" in p.symptom_points
    assert p.expanded_terms, "同义词扩展不应为空"


def test_vague_and_danger_detection():
    p = parser.parse("机器不太对劲")
    assert p.vague is True
    p2 = parser.parse("一号机冒烟还有焦糊味")
    assert p2.danger_terms, "应识别危险现象词"


def test_threshold_evaluation():
    """阈值方向与偏差判定。"""
    table = rules.thresholds_for("cnc") if hasattr(rules, "thresholds_for") else None
    from cncops.config import thresholds_for
    table = thresholds_for("cnc")
    assert rules.evaluate(92.0, table["spindle_temp"]) == "alarm"
    assert rules.evaluate(70.0, table["spindle_temp"]) == "warn"
    assert rules.evaluate(45.0, table["spindle_temp"]) == "normal"
    assert rules.evaluate(0.40, table["inlet_air_pressure"]) == "alarm"
    assert rules.evaluate(300.0, table["feed_rate"]) == "alarm"


def test_alarm_code_derived_from_data():
    """报警码必须由数据派生：注入故障后出现对应码，恢复后不得残留。"""
    for _ in range(8):
        datasource.read_snapshot("CNC-02")
    assert datasource.read_snapshot("CNC-02")["alarm_codes"] == []

    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    codes = set()
    for _ in range(12):
        codes |= set(datasource.read_snapshot("CNC-02")["alarm_codes"])
    assert {"ALM-101", "ALM-102"} <= codes, f"应派生出主轴过热与振动报警码，实际：{codes}"
    assert "ALM-201" not in codes, "不得派生空压机专用报警码"

    datasource.clear_faults()
    for _ in range(15):
        datasource.read_snapshot("CNC-02")
    assert datasource.read_snapshot("CNC-02")["alarm_codes"] == [], "恢复后不应残留报警码"


def test_simulation_thresholds_reachable():
    """阈值可达：每个故障剧本的必越限测点都能在注入后越限。"""
    sim = Simulator()
    cfg_seen = False
    for fault in sim.list_faults():
        device_type = fault["device_types"][0]
        for point in fault.get("must_trigger", []):
            cfg_seen = True
            from cncops.config import load_yaml
            from cncops.paths import THRESHOLDS_YAML
            table = load_yaml(str(THRESHOLDS_YAML))[device_type]
            assert rules.evaluate(float(fault["target"][point]), table[point]) != "normal", \
                f"{fault['id']} 的 {point} 无法越限（阈值不可达）"
    assert cfg_seen
