import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

PATH = os.path.join(os.path.dirname(__file__), "..", "zabbix_template.xml")
UUID4 = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")
VALUE_TYPE = {"char": "CHAR", "text": "TEXT", "uint": "UNSIGNED", "float": "FLOAT"}
KEY_RE = re.compile(r"/Pi-Tuner/([A-Za-z0-9_.]+(?:\[[^\]]*\])?)")


def proto_key(metric):
    return tuner.ZabbixSender.tuner_key(metric, "{#TUNER}")


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(PATH).getroot()
        self.template = self.root.find("templates/template")
        self.rule = self.template.find("discovery_rules/discovery_rule")

    def test_modern_export_format(self):
        self.assertTrue(self.root.findtext("version").startswith("6."))
        self.assertEqual(self.template.findtext("template"), "Pi-Tuner")

    def test_only_known_template_children_and_no_template_level_triggers(self):
        # Zabbix 5.4+ rejects <triggers> directly under <template>
        self.assertIsNone(self.template.find("triggers"))
        self.assertEqual({c.tag for c in self.template} - {
            "uuid", "template", "name", "description", "groups", "items",
            "discovery_rules", "macros", "valuemaps"}, set())

    def test_global_items_match_the_sender_defaults(self):
        z = tuner.ZabbixSender({})
        keys = {i.findtext("key") for i in self.template.findall("items/item")}
        self.assertEqual(keys, {z.key_event, z.key_active, z.key_heartbeat, z.key_eas})
        self.assertNotIn("pituner.status", keys)     # the old JSON blob is gone
        for item in self.template.findall("items/item"):
            self.assertEqual(item.findtext("type"), "TRAP")

    def test_discovery_rule(self):
        self.assertEqual(self.rule.findtext("key"), tuner.ZabbixSender.key_discovery)
        self.assertEqual(self.rule.findtext("type"), "TRAP")
        self.assertTrue(self.rule.findtext("lifetime"))

    def test_every_metric_has_exactly_one_matching_item_prototype(self):
        protos = self.rule.findall("item_prototypes/item_prototype")
        self.assertEqual(sorted(p.findtext("key") for p in protos),
                         sorted(proto_key(m) for m in tuner.TUNER_METRICS))
        for p in protos:
            metric = re.match(r"pituner\.tuner\.(.+)\[\{#TUNER\}\]", p.findtext("key")).group(1)
            self.assertEqual(p.findtext("value_type"), VALUE_TYPE[tuner.TUNER_METRICS[metric][0]], metric)
            self.assertEqual(p.findtext("type"), "TRAP")
            self.assertIn("{#NAME}", p.findtext("name"))

    def test_trigger_prototypes_reference_real_keys_and_defined_macros(self):
        protos = {p.findtext("key"): p for p in self.rule.findall("item_prototypes/item_prototype")}
        macros = {m.findtext("macro") for m in self.template.findall("macros/macro")}
        names = []
        # simple triggers sit under the one item they reference
        for key, item in protos.items():
            for trig in item.findall("trigger_prototypes/trigger_prototype"):
                refs = set(KEY_RE.findall(trig.findtext("expression")))
                self.assertEqual(refs, {key}, trig.findtext("name"))
                names.append(trig.findtext("name"))
        # multi-item triggers live at the discovery rule
        complex_ = self.rule.findall("trigger_prototypes/trigger_prototype")
        self.assertGreaterEqual(len(complex_), 2)
        for trig in complex_:
            refs = set(KEY_RE.findall(trig.findtext("expression")))
            self.assertGreaterEqual(len(refs), 2, trig.findtext("name"))
            names.append(trig.findtext("name"))
        for trig in self.rule.iter("trigger_prototype"):
            for ref in KEY_RE.findall(trig.findtext("expression")):
                self.assertIn(ref, protos)
            for macro in re.findall(r"\{\$[A-Z0-9_.]+\}", trig.findtext("expression")):
                self.assertIn(macro, macros)
            self.assertNotIn("{Pi-Tuner:", trig.findtext("expression"))   # pre-5.4 syntax
        for name in names:
            self.assertIn("{#NAME}", name)
        self.assertEqual(len(names), len(set(names)))

    def test_expected_alerts_exist(self):
        names = " | ".join(t.findtext("name") for t in self.root.iter("trigger_prototype"))
        for wanted in ("is down", "serial not found", "EAS attention tone", "restarting repeatedly",
                       "Dead air", "recording stalled"):
            self.assertIn(wanted, names)
        globals_ = " | ".join(t.findtext("name") for t in self.template.findall("items/item/triggers/trigger"))
        self.assertIn("heartbeat lost", globals_)
        self.assertIn("no stations streaming", globals_)

    def test_macros_have_defaults(self):
        values = {m.findtext("macro"): m.findtext("value") for m in self.template.findall("macros/macro")}
        self.assertEqual(values["{$PITUNER.SILENCE.DB}"], "-60")
        self.assertIn("{$PITUNER.SILENCE.TIME}", values)
        self.assertIn("{$PITUNER.REC.STALE}", values)

    def test_value_map_used_by_up_exists(self):
        up = [p for p in self.rule.findall("item_prototypes/item_prototype")
              if p.findtext("key") == proto_key("up")][0]
        name = up.findtext("valuemap/name")
        self.assertIn(name, {v.findtext("name") for v in self.template.findall("valuemaps/valuemap")})

    def test_uuids_from_earlier_versions_are_kept_so_a_reimport_updates_in_place(self):
        golden = os.path.join(os.path.dirname(__file__), "template_uuids.txt")
        with open(golden) as f:
            earlier = set(f.read().split())
        now = {e.text for e in self.root.iter("uuid")}
        self.assertLessEqual(earlier, now)

    def test_uuids_are_valid_and_unique(self):
        uuids = [e.text for e in self.root.iter("uuid")]
        self.assertGreaterEqual(len(uuids), 25)
        self.assertEqual(len(uuids), len(set(uuids)))
        for value in uuids:
            self.assertRegex(value, UUID4)


if __name__ == "__main__":
    unittest.main()
