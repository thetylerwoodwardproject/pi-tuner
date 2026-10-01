import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

PATH = os.path.join(os.path.dirname(__file__), "..", "zabbix_template.xml")
UUID4 = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(PATH).getroot()
        self.template = self.root.find("templates/template")

    def test_modern_export_format(self):
        self.assertTrue(self.root.findtext("version").startswith("6."))
        self.assertEqual(self.template.findtext("template"), "Pi-Tuner")

    def test_no_template_level_triggers(self):
        # Zabbix 5.4+ rejects <triggers> directly under <template>
        self.assertIsNone(self.template.find("triggers"))
        self.assertEqual([c.tag for c in self.template
                          if c.tag not in ("uuid", "template", "name", "description",
                                           "groups", "items")], [])

    def test_triggers_live_in_their_own_item_with_new_expression_syntax(self):
        count = 0
        for item in self.template.findall("items/item"):
            key = item.findtext("key")
            for trig in item.findall("triggers/trigger"):
                count += 1
                expr = trig.findtext("expression")
                self.assertIn(f"/Pi-Tuner/{key}", expr)
                self.assertNotIn("{Pi-Tuner:", expr)   # pre-5.4 syntax
                self.assertTrue(trig.findtext("name"))
        self.assertEqual(count, 5)

    def test_uuids_are_valid_and_unique(self):
        uuids = [e.text for e in self.root.iter("uuid")]
        self.assertGreaterEqual(len(uuids), 12)
        self.assertEqual(len(uuids), len(set(uuids)))
        for value in uuids:
            self.assertRegex(value, UUID4)

    def test_item_keys_match_what_tuner_sends(self):
        keys = {i.findtext("key") for i in self.template.findall("items/item")}
        z = tuner.ZabbixSender({})
        self.assertEqual(keys, {z.key_event, z.key_status, z.key_active,
                                z.key_heartbeat, z.key_eas})
        for item in self.template.findall("items/item"):
            self.assertEqual(item.findtext("type"), "TRAP")


if __name__ == "__main__":
    unittest.main()
