import io
import json
import re
import subprocess
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

from adb_fps import SampleRecord, _align_next_tick, format_sample_line, write_report
from perfpilot.android_power import (
    AndroidPowerMonitor, BATTERY_COMMAND, POWER_FIELDS,
    package_uid, parse_battery, parse_batterystats, parse_thermal,
)
from perfpilot.collectors import collector_command, parse_collector_line
from perfpilot.devices import capabilities
from perfpilot.events import sample_event
from perfpilot.report import power_report_charts, power_report_section, render_report
from perfpilot.session import SessionManager
from perfpilot.store import RunStore
from perfpilot.summary import summarize


BATTERY = """Current Battery Service state:
  AC powered: false
  USB powered: false
  status: 3
  level: 70
  scale: 100
  voltage: 4000
  temperature: 350
PERFPILOT_NODE_temp
351
PERFPILOT_NODE_current_now
-1800000
PERFPILOT_NODE_voltage_now
4000000
"""
THERMAL = """Cached temperatures:
  Temperature{mValue=99.0, mType=0, mName=cpu, mStatus=3}
Thermal Status: 2
Current temperatures from HAL:
  Temperature{mValue=42.5, mType=0, mName=cpu0, mStatus=0}
  Temperature{mValue=46.5, mType=0, mName=cpu1, mStatus=0}
  Temperature{mValue=44.0, mType=1, mName=gpu, mStatus=0}
  Temperature{mValue=36.0, mType=3, mName=skin, mStatus=0}
  Temperature{mValue=150.0, mType=2, mName=battery, mStatus=0}
Current cooling devices from HAL:
  Temperature{mValue=199.0, mType=0, mName=not-a-reading}
"""
STATS = """Statistics since last charge:
Estimated power use (mAh):
  Capacity: 5000, Computed drain: 123.4, actual drain: 100-150
  Screen: 30
  Uid u0a123: 12.5 ( cpu=8.5 wifi=4 )
  Uid u0a124: 90.0

Per-app mobile ms per packet:
"""


class ReportChartParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.power_parents = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article" and "power-chart" in attrs.get("class", "").split():
            self.power_parents.append(list(self.stack))
        if tag in ("div", "section", "article"):
            self.stack.append((tag, attrs.get("class", "")))

    def handle_endtag(self, tag):
        if tag in ("div", "section", "article"):
            if not self.stack or self.stack[-1][0] != tag:
                raise AssertionError(f"Unbalanced report structure at {tag}: {self.stack}")
            self.stack.pop()


class PowerParserTests(unittest.TestCase):
    def test_battery_units_and_power(self):
        values = parse_battery(BATTERY)
        self.assertEqual(values["batteryTemperatureC"], 35)
        self.assertEqual(values["batteryLevelPct"], 70)
        self.assertEqual(values["batteryCurrentMa"], -1800)
        self.assertEqual(values["batteryVoltageV"], 4)
        self.assertEqual(values["batteryPowerMw"], 7200)
        self.assertEqual(values["batteryStatus"], 3)

    def test_scale_and_zero_are_valid(self):
        values = parse_battery("level: 0\nscale: 200\ntemperature: 0\nstatus: 5")
        self.assertEqual(values["batteryLevelPct"], 0)
        self.assertEqual(values["batteryTemperatureC"], 0)
        self.assertEqual(parse_battery("level: 50\nscale: 200")["batteryLevelPct"], 25)

    def test_missing_nodes_fallback_without_fake_power(self):
        values = parse_battery(
            "level: 50\nvoltage: 4100\nPERFPILOT_NODE_temp\n320\n"
            "PERFPILOT_NODE_current_now\ncat: Permission denied\n"
            "PERFPILOT_NODE_voltage_now\nunavailable"
        )
        self.assertEqual(values["batteryTemperatureC"], 32)
        self.assertEqual(values["batteryVoltageV"], 4.1)
        self.assertIsNone(values["batteryCurrentMa"])
        self.assertIsNone(values["batteryPowerMw"])
        self.assertIsNone(values["batteryStatus"])

    def test_invalid_nonfinite_and_zero_voltage(self):
        values = parse_battery(
            "level: 101\nscale: 100\ntemperature: 5000\nstatus: 10\n"
            "PERFPILOT_NODE_current_now\nNaN\nPERFPILOT_NODE_voltage_now\n0"
        )
        self.assertTrue(all(value is None for value in values.values()))
        self.assertIsNone(parse_battery("level: 10\nscale: 0")["batteryLevelPct"])
        self.assertIsNone(parse_battery("PERFPILOT_NODE_current_now\ninf")["batteryCurrentMa"])

    def test_current_thermal_zones_only(self):
        values = parse_thermal(THERMAL)
        self.assertEqual(values, {
            "cpuTemperatureC": 46.5, "gpuTemperatureC": 44,
            "skinTemperatureC": 36, "thermalStatus": 2,
        })
        self.assertEqual(parse_thermal(THERMAL.replace(" from HAL", "")), values)

    def test_missing_thermal_and_bad_values(self):
        self.assertTrue(all(value is None for value in parse_thermal("Can't find service").values()))
        values = parse_thermal(
            "Thermal Status: 99\nCurrent temperatures:\n"
            "Temperature{mValue=NaN, mType=0}\nTemperature{mValue=999, mType=1}"
        )
        self.assertTrue(all(value is None for value in values.values()))
        self.assertIsNone(parse_thermal("Cached temperatures:\nTemperature{mValue=50, mType=0}")["cpuTemperatureC"])

    def test_uid_exact_and_unambiguous(self):
        output = "package:com.test.more uid:10124\npackage:com.test uid:10123\n"
        self.assertEqual(package_uid(output, "com.test"), 10123)
        self.assertIsNone(package_uid(output, "com.missing"))
        self.assertIsNone(package_uid(output + "package:com.test uid:110123", "com.test"))

    def test_batterystats_uid_and_numeric_uid(self):
        self.assertEqual(parse_batterystats(STATS, 10123), {
            "deviceEstimatedMah": 123.4, "appEstimatedMah": 12.5,
        })
        self.assertEqual(parse_batterystats(STATS.replace("u0a123", "10123"), 10123)["appEstimatedMah"], 12.5)
        self.assertEqual(parse_batterystats(STATS.replace("u0a123", "u10a123"), 1010123)["appEstimatedMah"], 12.5)
        self.assertIsNone(parse_batterystats(STATS, 1012)["appEstimatedMah"])
        self.assertIsNone(parse_batterystats(STATS, None)["appEstimatedMah"])
        self.assertIsNone(parse_batterystats("Uid u0a123: 100", 10123)["appEstimatedMah"])
        self.assertIsNone(parse_batterystats(STATS.replace("12.5", "-1"), 10123)["appEstimatedMah"])


class PowerMonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = AndroidPowerMonitor(Mock(return_value=BATTERY), "com.test")

    def read_at(self, seconds, output=BATTERY):
        self.monitor.shell.return_value = output
        with patch("perfpilot.android_power.time.monotonic", return_value=seconds):
            self.monitor._read("battery", (), parse_battery)
            return self.monitor.snapshot()

    def test_energy_trapezoid_and_level_drop(self):
        first = self.read_at(0)
        self.assertIsNone(first["dischargeEnergyMwh"])
        second = self.read_at(30, BATTERY.replace("level: 70", "level: 69"))
        self.assertEqual(second["batteryDropPct"], 1)
        self.assertAlmostEqual(second["dischargeEnergyMwh"], 60)
        self.assertEqual(second["energyCoverageSec"], 30)

    def test_energy_uses_30_second_trapezoid_with_varying_power(self):
        self.read_at(0)
        values = self.read_at(30, BATTERY.replace("-1800000", "-900000"))
        self.assertAlmostEqual(values["dischargeEnergyMwh"], 45)
        self.assertEqual(values["energyCoverageSec"], 30)

    def test_charge_gaps_and_failures_not_integrated(self):
        self.read_at(0)
        self.read_at(30)
        charged = self.read_at(60, BATTERY.replace("status: 3", "status: 2").replace("level: 70", "level: 71"))
        self.assertEqual(charged["batteryDropPct"], -1)
        self.assertEqual(charged["dischargeEnergyMwh"], 60)
        self.read_at(90)
        self.read_at(150)
        failed = self.read_at(180, "Permission denied")
        self.assertIsNone(failed["batteryLevelPct"])
        self.read_at(210)
        final = self.read_at(240)
        self.assertEqual(final["dischargeEnergyMwh"], 120)
        self.assertEqual(final["energyCoverageSec"], 60)

    def test_zero_power_and_exact_gap_threshold(self):
        self.read_at(0, BATTERY.replace("-1800000", "0"))
        values = self.read_at(45, BATTERY.replace("-1800000", "0"))
        self.assertEqual(values["batteryPowerMw"], 0)
        self.assertEqual(values["dischargeEnergyMwh"], 0)
        self.assertEqual(values["energyCoverageSec"], 45)
        values = self.read_at(91, BATTERY.replace("-1800000", "0"))
        self.assertEqual(values["energyCoverageSec"], 45)

    def test_stale_readings_are_removed(self):
        self.read_at(0)
        with patch("perfpilot.android_power.time.monotonic", return_value=0):
            self.monitor.shell.return_value = THERMAL
            self.monitor._read("thermal", (), parse_thermal)
            self.monitor.shell.return_value = STATS
            self.monitor._read("stats", (), lambda out: parse_batterystats(out, 10123))
        with patch("perfpilot.android_power.time.monotonic", return_value=31):
            values = self.monitor.snapshot()
            self.assertEqual(values["batteryPowerMw"], 7200)
            self.assertEqual(values["cpuTemperatureC"], 46.5)
            self.assertEqual(values["batterySampleAgeSec"], 31)
        with patch("perfpilot.android_power.time.monotonic", return_value=91):
            values = self.monitor.snapshot()
            self.assertIsNone(values["batteryPowerMw"])
            self.assertIsNone(values["cpuTemperatureC"])
            self.assertIsNone(values["appEstimatedMah"])

    def test_reading_is_valid_at_90_second_boundary(self):
        self.read_at(0)
        with patch("perfpilot.android_power.time.monotonic", return_value=90):
            self.assertEqual(self.monitor.snapshot()["batteryPowerMw"], 7200)
        with patch("perfpilot.android_power.time.monotonic", return_value=90.01):
            self.assertIsNone(self.monitor.snapshot()["batteryPowerMw"])

    def test_timeout_clears_previous_values_and_logs(self):
        self.read_at(0)
        self.monitor.shell.side_effect = subprocess.TimeoutExpired("adb", 20)
        with patch("builtins.print") as log:
            values = self.read_at(5)
        self.assertIsNone(values["batteryPowerMw"])
        self.assertTrue(log.called)
        self.assertEqual(log.call_count, 1)

    def test_all_power_metrics_poll_every_30_seconds(self):
        clock = [0.0]
        commands = []
        monitor = AndroidPowerMonitor(Mock(), "com.test")

        def shell(*args):
            commands.append((clock[0], args))
            if args[:2] == ("sh", "-c"):
                return BATTERY
            if args == ("dumpsys", "thermalservice"):
                return THERMAL
            if args[:2] == ("cmd", "package"):
                return "package:com.test uid:10123"
            return STATS

        def wait(seconds):
            clock[0] += seconds
            if clock[0] > 60:
                monitor._stop.set()

        monitor.shell.side_effect = shell
        with patch("perfpilot.android_power.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(monitor._stop, "wait", side_effect=wait):
            monitor._run()
        battery_times = [at for at, args in commands if args[:2] == ("sh", "-c")]
        stats_times = [at for at, args in commands if args[:2] == ("dumpsys", "batterystats")]
        thermal_times = [at for at, args in commands if args == ("dumpsys", "thermalservice")]
        self.assertEqual(battery_times, [0, 30, 60])
        self.assertEqual(thermal_times, [0, 30, 60])
        self.assertEqual(stats_times, [0, 30, 60])

    def test_worker_lifecycle_read_only_commands(self):
        completed = threading.Event()

        def shell(*args):
            if args[:2] == ("sh", "-c"):
                return BATTERY
            if args == ("dumpsys", "thermalservice"):
                return THERMAL
            if args[:2] == ("cmd", "package"):
                return "package:com.test uid:10123"
            completed.set()
            return STATS

        monitor = AndroidPowerMonitor(Mock(side_effect=shell), "com.test")
        monitor.start()
        try:
            self.assertTrue(completed.wait(2))
        finally:
            monitor.close()
        self.assertFalse(monitor._thread.is_alive())
        commands = monitor.shell.call_args_list
        self.assertEqual(len(commands), 4)
        self.assertIn("--user", commands[2].args)
        self.assertIn("--charged", commands[3].args)
        self.assertNotIn("--reset", " ".join(str(call) for call in commands))
        self.assertIn("|| echo unavailable", BATTERY_COMMAND)


class PowerPipelineTests(unittest.TestCase):
    def test_existing_fps_cpu_memory_sampling_stays_one_second(self):
        command = collector_command(
            {"platform": "android", "id": "test-device"}, "com.test",
            Path("report.html"), Path("stop.flag"),
        )
        self.assertEqual(command[command.index("--interval") + 1], "1")
        with patch("adb_fps.time.monotonic", return_value=0), patch("adb_fps.time.sleep") as sleep:
            self.assertEqual(_align_next_tick(0, 1), 1)
            sleep.assert_called_once_with(1)

    def sample(self):
        parsed = parse_collector_line(format_sample_line(60, 12, 100, power=parse_battery(BATTERY)))
        self.assertIsNotNone(parsed)
        return sample_event("test", 0, parsed["fps"], parsed["cpu"], parsed["memory"],
                            extra_metrics={key: parsed[key] for key in POWER_FIELDS})

    def test_protocol_events_summary_and_reports(self):
        event = self.sample()
        self.assertEqual(event["metrics"]["batteryPowerMw"], 7200)
        self.assertEqual(event["batteryTemperatureC"], 35)
        self.assertEqual(summarize([event])["power"]["batteryPowerMw"]["peak"], 7200)
        document = render_report({"device": {"platform": "android"}}, [event])
        self.assertIn("Android 温度 / 电池 / 功耗", document)
        self.assertIn("7200.0 mW", document)
        self.assertIn("不是本次会话增量", document)
        self.assertEqual(power_report_section([{"fps": 60}]), "")
        self.assertNotIn("power", summarize([{"fps": 60}]))

    def test_five_power_charts_are_in_main_report_grid_without_duplicates(self):
        samples = []
        for index in range(3):
            event = self.sample()
            event.update({
                "elapsedMs": index * 30000,
                "dischargeEnergyMwh": index * 60,
                "deviceEstimatedMah": 100 + index * 2,
                "appEstimatedMah": 10 + index,
            })
            samples.append(event)
        document = render_report({"device": {"platform": "android"}}, samples)
        parser = ReportChartParser()
        parser.feed(document)
        self.assertEqual(parser.stack, [])
        self.assertEqual(len(parser.power_parents), 5)
        for parents in parser.power_parents:
            self.assertIn(("div", "chart-grid"), parents)
            self.assertNotIn(("section", "table-card"), parents)
        for title in ("温度趋势", "电量趋势", "整机电池侧功率", "会话放电能量", "系统估算累计耗电"):
            self.assertIn(f"<h3>{title}</h3>", document)
        self.assertIn("均约 30s 采样", document)
        self.assertNotIn("电池约 5s", document)
        self.assertLess(document.index('class="chart-card power-chart"'), document.index("<h2>Android 温度"))
        payload = json.loads(re.search(r"const REPORT_DATA = (.*?);\n", document, re.S)[1])
        self.assertEqual(payload["times"], [0, 30000, 60000])
        power = {item["id"]: item for item in payload["power"]}
        self.assertEqual(power["inspect-battery-energy"]["values"], [0, 60, 120])
        self.assertEqual(power["inspect-battery-estimated"]["values"], [10, 11, 12])

    def test_power_charts_handle_missing_values_without_affecting_ios_reports(self):
        empty = dict.fromkeys(POWER_FIELDS)
        charts = power_report_charts([empty])
        self.assertEqual(charts.count('class="chart-card power-chart"'), 5)
        self.assertEqual(charts.count("无可读数据"), 5)
        self.assertNotIn('class="chart-card power-chart"', power_report_section([empty], include_charts=False))
        ios = render_report({"device": {"platform": "ios"}}, [{"fps": 60, "cpu": 10, "memory": 100}])
        self.assertNotIn('class="chart-card power-chart"', ios)
        self.assertNotIn('id="inspect-battery-power"', ios)
        self.assertEqual(power_report_charts([]), "")

    def test_stale_report_does_not_reuse_last_valid_reading(self):
        latest = {key: None for key in POWER_FIELDS}
        document = power_report_section([self.sample(), latest])
        self.assertIn("整机电池侧功率（绝对值）</div><strong>-</strong>", document)
        self.assertIn("峰值 7200.0 mW", document)
        self.assertIsNone(summarize([self.sample(), latest])["power"]["batteryPowerMw"]["last"])

    def test_power_protocol_preserves_small_values(self):
        parsed = parse_collector_line(format_sample_line(
            60, 12, 100, power={"batteryVoltageV": 4.123456, "dischargeEnergyMwh": 0.004321},
        ))
        self.assertEqual(parsed["batteryVoltageV"], 4.123456)
        self.assertEqual(parsed["dischargeEnergyMwh"], 0.004321)

    def test_cli_report_contains_power(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.html"
            write_report(str(path), "com.test", [SampleRecord(1, 60, 60, 12, 100, 0, parse_battery(BATTERY))],
                         60, 1, 12, 1, 12, 100, 1, 100, 1, 1)
            self.assertIn("7200.0 mW", path.read_text(encoding="utf-8"))

    def test_persisted_samples_summary_and_report_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("perfpilot.store.runs_root", return_value=Path(folder)):
            store = RunStore("power-test")
            event = self.sample()
            store.append_sample(event)
            samples = RunStore("power-test").read_samples()
            self.assertEqual(samples, [event])
            meta = {"device": {"platform": "android"}, "summary": summarize(samples)}
            store.write_meta(meta)
            self.assertEqual(store.read_meta()["summary"]["power"]["batteryPowerMw"]["peak"], 7200)
            store.write_report(render_report(meta, samples))
            self.assertIn("7200.0 mW", store.report_path.read_text(encoding="utf-8"))

    def test_session_stream_preserves_and_stores_power(self):
        line = format_sample_line(60, 12, 100, power=parse_battery(BATTERY))
        process = Mock(pid=123, stdout=io.BytesIO((line + "\n").encode()), returncode=0)
        process.wait.return_value = 0
        manager = SessionManager.__new__(SessionManager)
        manager._lock = threading.Lock()
        manager.device_locks = {}
        with tempfile.TemporaryDirectory() as folder:
            store = Mock(spec=RunStore)
            store.directory = Path(folder)
            store.pid_path = Path(folder) / "pid"
            store.logcat_pid_path = Path(folder) / "logcat-pid"
            store.stop_path = Path(folder) / "stop"
            store.read_meta.return_value = {}
            manager.runs = {"test": {
                "store": store, "device": {"platform": "android"}, "startedAtMs": 0,
                "samples": [], "status": "running", "runId": "test",
                "wantReport": False,
            }}
            with patch("perfpilot.session.start_collector", return_value=process), \
                    patch.object(manager, "_publish"), \
                    patch.object(manager, "_stop_logcat"):
                manager._run_collector("test", ["collector"])
            event = store.append_sample.call_args.args[0]
            self.assertEqual(event["batteryPowerMw"], 7200)
            self.assertEqual(event["metrics"]["batteryTemperatureC"], 35)

    def test_android_capability_only_and_existing_protocol_unchanged(self):
        android = capabilities({"platform": "android"}, "com.test")["indicators"]
        ios = capabilities({"platform": "ios"}, "com.test")["indicators"]
        self.assertTrue(any(item["id"] == "battery" for item in android))
        self.assertFalse(any(item["id"] == "battery" for item in ios))
        self.assertEqual(format_sample_line(60, 12, 100), "PERFPILOT_SAMPLE\tfps=60.0\tcpu=12.0\tmemory=100.0")


if __name__ == "__main__":
    unittest.main()
