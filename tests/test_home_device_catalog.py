import unittest
from unittest.mock import Mock

from core.home.device_catalog import DeviceCatalog, SmartThingsReader, display_name, handle_device_read, state_text


class DeviceCatalogTests(unittest.TestCase):
    def primary(self, available=True):
        primary = Mock()
        primary.catalog.return_value = {"bridge": {"available": available,
            "permission": "GRANTED", "bridge_state": "READY"}, "devices": [
            {"name": "TV", "state": [{"trait": "OnOff", "on": False}],
             "type_candidates": [{"device_type": "GoogleTVDevice"}], "commands": {"on": False}},
            {"name": "환풍기", "state": [{"trait": "OnOff", "on": True}]},
            {"name": "Mi Robot Vacuum", "state": []},
            {"name": "x" * 60, "state": []}]}
        return primary

    def secondary(self):
        secondary = Mock()
        secondary.catalog.return_value = [{"label": "움직임 센서", "type": "ZIGBEE", "components": [
            {"id": "main", "capabilities": [{"id": "motionSensor"}, {"id": "battery"},
                                          {"id": "samsungim.samsungaccount"}]}]}]
        secondary.status.return_value = {"components": {"main": {
            "motionSensor": {"motion": {"value": "inactive", "timestamp": "2026-10-13T11:00:00Z"}},
            "battery": {"battery": {"value": 88, "unit": "%"}},
            "samsungim.samsungaccount": {"account": {"value": "secret-account"}}}}}
        return secondary

    def test_actual_types_and_reports_do_not_authorize_power(self):
        result = DeviceCatalog(self.primary(), self.secondary()).inspect()
        tv = next(d for d in result["devices"] if d["name"] == "TV")
        self.assertEqual(tv["device_types"], ["GoogleTVDevice"])
        self.assertEqual(tv["enabled_power_commands"], {"on": False})
        self.assertFalse(result["physical_state_confirmed"])

    def test_fan_excluded_and_opaque_name_hidden(self):
        result = DeviceCatalog(self.primary(), self.secondary()).inspect()
        self.assertNotIn("환풍기", [d["name"] for d in result["devices"]])
        self.assertNotIn("Mi Robot Vacuum", [d["name"] for d in result["devices"]])
        self.assertIn("이름 미확인 기기", [d["name"] for d in result["devices"]])

    def test_secondary_state_filters_account_payload(self):
        result = DeviceCatalog(self.primary(), self.secondary()).inspect()
        sensor = next(d for d in result["devices"] if d["name"] == "움직임 센서")
        self.assertEqual(sensor["state"]["motionSensor"]["value"], "inactive")
        self.assertNotIn("secret-account", repr(result))
        self.assertEqual(sensor["enabled_power_commands"], {})

    def test_primary_unavailable_does_not_reuse_cached_devices(self):
        result = DeviceCatalog(self.primary(False), self.secondary()).inspect()
        self.assertEqual(result["providers"]["Google Home"], "UNAVAILABLE")
        self.assertFalse(any(d["provider"] == "Google Home" for d in result["devices"]))

    def test_unavailable_secondary_does_not_invent_status(self):
        secondary = self.secondary()
        secondary.status.side_effect = TimeoutError("credential must not escape")
        result = DeviceCatalog(self.primary(), secondary).inspect()
        sensor = next(d for d in result["devices"] if d["provider"] == "SmartThings")
        self.assertEqual(sensor["query_status"], "UNAVAILABLE")
        self.assertEqual(sensor["state"], {})
        self.assertNotIn("credential", repr(result))

    def test_read_route_does_not_interpret_actions(self):
        factory = Mock()
        self.assertIsNone(handle_device_read("/home TV on", factory))
        factory.assert_not_called()
        factory.return_value.inspect.return_value = {"devices": [{"name": "TV", "provider": "Google Home",
            "device_types": ["GoogleTVDevice"], "state": []}]}
        self.assertIn("TV · Google Home", handle_device_read("/home TV status", factory))

    def test_multiword_display_name_and_lamp_read_alias(self):
        factory = Mock()
        factory.return_value.inspect.return_value = {"devices": [{"name": "Mi LED 탁상 램프", "provider": "Google Home", "state": []}]}
        self.assertIn("Mi LED 탁상 램프", handle_device_read("/home Mi LED 탁상 램프 status", factory))
        self.assertIn("Mi LED 탁상 램프", handle_device_read("/home 탁상등 status", factory))

    def test_unavailable_primary_is_explicit_in_inventory_output(self):
        factory = Mock()
        factory.return_value.inspect.return_value = {"providers": {"Google Home": "UNAVAILABLE"},
            "devices": [{"name": "lens", "provider": "SmartThings", "state": {}}]}
        self.assertIn("보조 상태만", handle_device_read("/home devices", factory))

    def test_fixed_api_pagination_rejects_external_url(self):
        reader = SmartThingsReader()
        reader.get = Mock(return_value={"items": [], "_links": {"next": {
            "href": "https://evil.example/v1/devices?token=secret"}}})
        with self.assertRaises(ValueError):
            reader.catalog()
        reader.get.assert_called_once_with("/v1/devices")

    def test_invalid_discovered_id_never_queries_api(self):
        reader = SmartThingsReader()
        reader.get = Mock()
        with self.assertRaises(ValueError):
            reader.status({"deviceId": "../../keys"})
        reader.get.assert_not_called()

    def test_offline_device_does_not_present_cached_attributes_as_current_state(self):
        row = {"capabilities": [{"device_type": "RoboticVacuumCleanerDevice", "connectivity": "OFFLINE"},
                                {"device_type": "RootNodeDevice", "connectivity": "ONLINE"}],
               "state": [{"trait": "OnOff", "on": True},
                         {"trait": "RvcOperationalState", "operational_state_raw": 1}]}
        self.assertEqual("오프라인 · 현재 동작 상태 미확인", state_text(row))

    def test_null_media_and_vacuum_values_do_not_claim_state_was_observed(self):
        row = {"state": [{"trait": "MediaPlayback", "playback_state": None},
                         {"trait": "RvcOperationalState", "operational_state_raw": None}]}
        self.assertNotIn("조회됨", state_text(row))
        self.assertEqual(state_text({"state": []}), state_text(row))

    def test_online_media_state_is_retained(self):
        self.assertEqual("재생 중", state_text({"capabilities": [{"device_type": "SpeakerDevice", "connectivity": "ONLINE"}],
                                             "state": [{"trait": "MediaPlayback", "playback_state": "Playing"}]}))


if __name__ == "__main__":
    unittest.main()
