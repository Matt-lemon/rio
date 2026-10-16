import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.home.tools import CommandLedger, HomeToolRegistry, LocalHomeTransport, NoRedirects, parse_light_request, user_message, validate_targets
from core.home.chat import run_chat


ALIAS = "dev_" + "a" * 24
TARGETS = {"거실": {"alias": ALIAS, "device_type": "OnOffPluginUnitDevice", "confirmed_physical_light": True}}


class FakeTransport:
    def __init__(self):
        self.writes = []
        self.response = {"status": "DISPATCHED"}
        self.job_response = {"status": "SUCCEEDED"}
        self.bridge = {"bridge_state": "READY", "available": True, "permission": "GRANTED", "controls_enabled": True}
        self.devices = [{"alias": ALIAS, "lighting": True, "commands": {"on": True, "off": True},
                         "control_binding": {"status": "SELECTED_EXPLICIT_TYPE", "device_type": "OnOffPluginUnitDevice"}}]

    def catalog(self):
        return {"bridge": self.bridge, "devices": self.devices}

    def status(self):
        return {**self.bridge, "private_secret": "must-not-leak"}

    def set_power(self, alias, on, key):
        self.writes.append((alias, on, key))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def result(self, key):
        return self.job_response


class HomeToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = FakeTransport()
        self.registry = HomeToolRegistry(self.transport, TARGETS, self.root / "commands.sqlite", allow_execution=True)
        self.addCleanup(self.registry.close)

    def call(self, on=True, key="test-key-0001"):
        return self.registry.call("home_set_light", {"target": "거실", "on": on, "idempotency_key": key})

    def test_default_preview_has_no_write_or_ledger(self):
        registry = HomeToolRegistry(self.transport, TARGETS, self.root / "unused.sqlite")
        self.assertEqual("PREVIEW", registry.call("home_set_light", {"target": "거실", "on": True})["status"])
        self.assertEqual([], self.transport.writes)
        self.assertFalse((self.root / "unused.sqlite").exists())

    def test_locked_controls_cannot_write_even_with_execution_flag(self):
        self.transport.bridge["controls_enabled"] = False
        self.assertIn("CONTROLS_LOCKED", self.call()["blockers"])
        self.assertEqual([], self.transport.writes)

    def test_unready_or_revoked_bridge_cannot_write(self):
        for field, value in (("available", False), ("permission", "REVOKED"), ("bridge_state", "STALE")):
            with self.subTest(field=field):
                self.transport = FakeTransport(); self.registry.transport = self.transport
                self.transport.bridge[field] = value
                self.assertEqual("BRIDGE_NOT_READY", self.call()["code"])
                self.assertEqual([], self.transport.writes)

    def test_same_type_binding_and_supported_command_required(self):
        for change in ({"control_binding": {}}, {"lighting": False}, {"commands": {"on": False}},
                       {"control_binding": {"status": "SELECTED_EXPLICIT_TYPE", "device_type": "DimmableLightDevice"}}):
            with self.subTest(change=change):
                self.transport.devices[0] = {**FakeTransport().devices[0], **change}
                self.assertEqual("EXECUTION_NOT_READY", self.call()["code"])
        self.assertEqual([], self.transport.writes)

    def test_missing_or_ambiguous_catalog_target_rejected(self):
        original = self.transport.devices[0]
        for devices in ([], [original, original]):
            self.transport.devices = devices
            self.assertEqual("TARGET_NOT_UNIQUE_IN_CURRENT_CATALOG", self.call()["code"])
        self.assertEqual([], self.transport.writes)

    def test_missing_unknown_target_and_non_boolean_on_rejected(self):
        for args in ({"target": None, "on": True}, {"target": "없는 방", "on": True},
                     {"target": "거실", "on": "false"}, {"target": "거실", "on": 1}):
            self.assertIn(self.registry.call("home_set_light", args)["status"], {"NEED_INPUT", "REJECTED"})
        self.assertEqual([], self.transport.writes)

    def test_same_key_replay_does_not_resend(self):
        first = self.call()
        self.assertEqual("DISPATCHED", first["status"])
        self.assertEqual(first, self.call())
        self.assertEqual(1, len(self.transport.writes))

    def test_same_key_different_request_rejected(self):
        self.call()
        self.assertEqual("IDEMPOTENCY_CONFLICT", self.call(on=False)["code"])
        self.assertEqual(1, len(self.transport.writes))

    def test_timeout_is_unknown_and_new_key_cannot_bypass(self):
        self.transport.response = TimeoutError()
        self.assertEqual("UNKNOWN", self.call()["status"])
        self.assertEqual("PREVIOUS_RESULT_UNRESOLVED", self.call(key="new-key-0002")["code"])
        self.assertEqual(1, len(self.transport.writes))

    def test_uncertain_write_survives_process_restart(self):
        self.transport.response = TimeoutError()
        self.call(); self.registry.close(); self.registry._ledger = None
        self.assertEqual("UNKNOWN", self.call()["status"])
        self.assertEqual("PREVIOUS_RESULT_UNRESOLVED", self.call(key="new-key-0002")["code"])
        self.assertEqual(1, len(self.transport.writes))

    def test_human_review_preserves_unknown_and_never_replays_original_request(self):
        self.transport.response = TimeoutError()
        original = self.call()
        ledger = self.registry.ledger
        before = ledger.db.execute('SELECT * FROM light_commands').fetchall()
        ledger.review_not_applied('test-key-0001', '사용자가 실제로 켜지지 않았다고 확인함',
                                 gateway_result={'status': 'UNKNOWN'},
                                 gateway_status={**self.transport.bridge, 'inflight': False})
        self.assertEqual(before, ledger.db.execute('SELECT * FROM light_commands').fetchall())
        self.assertEqual(original, self.call())
        self.assertEqual(1, len(self.transport.writes))
        self.transport.response = {'status': 'DISPATCHED'}
        self.assertEqual('DISPATCHED', self.call(key='new-key-0002')['status'])
        self.assertEqual(2, len(self.transport.writes))

    def test_sdk_state_or_inflight_request_cannot_release_unknown(self):
        self.transport.response = TimeoutError()
        self.call()
        for result, state in (({'status': 'SUCCEEDED'}, False), ({'status': 'UNKNOWN'}, True)):
            with self.assertRaises(ValueError):
                self.registry.ledger.review_not_applied('test-key-0001', 'physical observation',
                    gateway_result=result, gateway_status={**self.transport.bridge, 'inflight': state})
        self.assertEqual('PREVIOUS_RESULT_UNRESOLVED', self.call(key='new-key-0002')['code'])

    def test_review_does_not_release_other_pending_requests(self):
        ledger = self.registry.ledger
        ledger.reserve('old-key-0001', {'on': True})
        ledger.record('old-key-0001', {'status': 'UNKNOWN'})
        ledger.db.execute("INSERT INTO light_commands VALUES ('other-key-0002','digest','DISPATCHING','{}')")
        ledger.db.commit()
        ledger.review_not_applied('old-key-0001', 'physical observation', gateway_result={'status': 'UNKNOWN'},
                                 gateway_status={**self.transport.bridge, 'inflight': False})
        self.assertEqual('PREVIOUS_RESULT_UNRESOLVED', self.call(key='new-key-0003')['code'])

    def test_crash_after_reservation_has_same_protection(self):
        self.registry.ledger.reserve("test-key-0001", {"alias": ALIAS, "on": True})
        self.registry.close(); self.registry._ledger = None
        self.assertEqual("UNKNOWN", self.call()["status"])
        self.assertEqual([], self.transport.writes)

    def test_ack_does_not_claim_physical_state_and_get_clears_pending(self):
        self.call()
        result = self.registry.call("home_get_result", {"idempotency_key": "test-key-0001"})
        self.assertEqual("SUCCEEDED", result["status"])
        self.assertFalse(result["physical_state_confirmed"])
        self.assertEqual("DISPATCHED", self.call(key="new-key-0002")["status"])

    def test_failed_result_get_does_not_prove_prior_write_rejected(self):
        self.call()
        self.transport.job_response = {"http_status": 404}
        self.assertEqual("UNKNOWN", self.registry.call("home_get_result", {"idempotency_key": "test-key-0001"})["status"])
        self.assertEqual("PREVIOUS_RESULT_UNRESOLVED", self.call(key="new-key-0002")["code"])

    def test_wrong_response_id_is_unknown(self):
        self.transport.response = {"id": "other-request", "status": "SUCCEEDED"}
        self.assertEqual("UNKNOWN", self.call()["status"])

    def test_malformed_result_does_not_clear_pending_or_resend(self):
        self.call()
        for response in (None, [], {"status": "SUCCEEDED", "id": "different-key"}):
            self.transport.job_response = response
            self.assertEqual("UNKNOWN", self.registry.call("home_get_result", {"idempotency_key": "test-key-0001"})["status"])
            self.assertEqual("PREVIOUS_RESULT_UNRESOLVED", self.call(key="new-key-0002")["code"])
        self.assertEqual(1, len(self.transport.writes))

    def test_unavailable_or_malformed_catalog_cannot_write(self):
        for response in (None, [], {"bridge": []}):
            with patch.object(self.transport, "catalog", return_value=response):
                self.assertEqual("BRIDGE_NOT_READY", self.call()["code"])
        with patch.object(self.transport, "catalog", side_effect=TimeoutError()):
            self.assertEqual("BRIDGE_NOT_READY", self.call()["code"])
        self.assertEqual([], self.transport.writes)

    def test_unknown_result_query_does_not_create_ledger(self):
        self.assertEqual("UNKNOWN_REQUEST", self.registry.call("home_get_result", {"idempotency_key": "missing-key-0001"})["code"])
        self.assertFalse(self.registry.ledger_path.exists())

    def test_unknown_result_and_invalid_tool_never_dispatch(self):
        for name, args in (("home_get_result", {"idempotency_key": "not-known-key"}),
                           ("home_set_light", {"target": "거실", "on": True, "actual_alias": ALIAS}),
                           ("set_power", {}), ("home_set_light", {"target": "거실", "on": True, "idempotency_key": "../bad"})):
            self.assertEqual("REJECTED", self.registry.call(name, args)["status"])
        self.assertEqual([], self.transport.writes)

    def test_status_and_schema_do_not_expose_private_aliases_or_secrets(self):
        public = json.dumps([self.registry.call("home_get_status", {}), self.registry.schemas(), self.registry.plan("거실", True)])
        self.assertNotIn(ALIAS, public)
        self.assertNotIn("must-not-leak", public)

    def test_direct_requests_not_quotes_negations_or_compound_commands(self):
        for text in ("거실 불 켜줘", "리오, 거실 불 켜줘.", "거실 조명 꺼주세요"):
            self.assertEqual("거실", parse_light_request(text)["target"])
        self.assertEqual({"target": None, "on": True}, parse_light_request("불 켜줘"))
        for text in ("'거실 불 켜줘' 번역해줘", "거실 불 켜지 마", "거실 불 켜줘라고 말했었지",
                     "거실 불 켜고 PC 꺼줘", "불 켜는 방법 알려줘", "집에 가고 싶어"):
            self.assertIsNone(parse_light_request(text))

    def test_explicit_confirmed_light_required_not_any_plug(self):
        for change in ({"confirmed_physical_light": False}, {"device_type": "FanDevice"}, {"device_type": []}):
            with self.assertRaises(ValueError):
                validate_targets({"schema_version": 1, "targets": {"거실": {**TARGETS["거실"], **change}}})

    def test_optional_chat_intercepts_home_only_and_restores_reader(self):
        reader = iter(["거실 불 켜줘", "일반 대화"])
        module = SimpleNamespace(read_user_input=lambda prompt: next(reader))
        original = module.read_user_input
        seen = []
        module.main = lambda: seen.append(module.read_user_input("You: "))
        self.registry.allow_execution = False
        with contextlib.redirect_stdout(io.StringIO()) as output:
            run_chat(module, self.registry)
        self.assertEqual(["일반 대화"], seen)
        self.assertIs(original, module.read_user_input)
        self.assertIn("아직 실제 조명 제어", output.getvalue())
        self.assertEqual([], self.transport.writes)

    def test_original_files_hash_unchanged_for_all_rejected_and_preview_requests(self):
        paths = []
        for name in ("nas_main.py", "identity.yaml", "v4-train.jsonl", "validation.jsonl"):
            path = self.root / name; path.write_bytes(b"existing-original\n"); paths.append(path)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.registry.allow_execution = False
        self.call()
        self.registry.call("home_set_light", {"target": None, "on": True})
        self.transport.bridge["controls_enabled"] = False
        self.registry.allow_execution = True; self.call()
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_redirects_disabled_and_proxy_disabled_in_transport(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))
        token = self.root / "token"; token.write_text("test-private-token"); token.chmod(0o600)
        transport = LocalHomeTransport(token)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError()
            self.assertEqual("GATEWAY_UNREACHABLE", transport.status()["code"])
            handlers = opener.call_args.args
            self.assertEqual({}, handlers[0].proxies)
            self.assertIsInstance(handlers[1], NoRedirects)


if __name__ == "__main__":
    unittest.main()
