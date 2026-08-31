import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
    Role,
)
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import (
    DeliveryWorker,
    DryRunPushTransport,
    ProviderPushTransport,
    push_config_from_env,
    push_transport_from_env,
)

PUSH_ENV = {
    "PUSH_MODE": "live",
    "PUSH_ENDPOINT": "https://push.example.test/send",
    "PUSH_KEY": "k-123",
    "PUSH_PROVIDER": "fcm",
}


def _clock():
    return datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


class RecordingClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, endpoint, key, payload):
        if self.fail:
            raise OSError("push endpoint unreachable")
        self.calls.append((endpoint, key, payload))


class PushTransportTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()

    def _pushes(self):
        return [d for d in self.store.all_notification_deliveries()
                if d.channel == NotificationChannel.PUSH]

    def _push_delivery(self, destination):
        return NotificationDelivery(
            id="d_test", notification_id="n_test",
            channel=NotificationChannel.PUSH, destination=destination)

    def _register_scheduler_push(self, token="device-abc123"):
        api = ApiService(self.store)
        api.set_contact_destination("scheduler", "push", token)
        api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        return next(d for d in self._pushes() if d.recipient_ref == "scheduler")

    # -- dry-run (safe default) -------------------------------------------
    def test_dry_run_records_without_sending(self):
        transport = DryRunPushTransport()
        worker = DeliveryWorker(self.store, _clock, push_transport=transport)
        res = worker.process_pending()
        self.assertEqual(res["failed"], 0)
        pushes = self._pushes()
        self.assertTrue(pushes)
        self.assertTrue(all(d.status == DeliveryStatus.SENT for d in pushes))
        self.assertEqual(len(transport.outbox), len(pushes))

    def test_default_worker_push_is_dry_run(self):
        self.assertEqual(
            DeliveryWorker(self.store, _clock).push_transport.mode, "dry_run")

    # -- config fail-safe --------------------------------------------------
    def test_config_defaults_to_dry_run(self):
        self.assertEqual(push_transport_from_env({}).mode, "dry_run")
        self.assertEqual(
            push_transport_from_env({"PUSH_MODE": "live"}).mode, "dry_run")
        self.assertEqual(
            push_transport_from_env(
                {"PUSH_MODE": "live", "PUSH_ENDPOINT": "   "}).mode, "dry_run")

    def test_config_maps_fields_and_builds_provider(self):
        cfg = push_config_from_env(PUSH_ENV)
        self.assertEqual(cfg["mode"], "live")
        self.assertEqual(cfg["endpoint"], "https://push.example.test/send")
        self.assertEqual(cfg["provider"], "fcm")
        t = push_transport_from_env(PUSH_ENV)
        self.assertIsInstance(t, ProviderPushTransport)
        self.assertEqual(t.endpoint, "https://push.example.test/send")
        self.assertEqual(t.provider, "fcm")

    # -- placeholder rejection (live) -------------------------------------
    def test_provider_rejects_placeholder_without_calling_client(self):
        client = RecordingClient()
        transport = ProviderPushTransport(
            endpoint="https://push.example.test/send", client=client)
        for bad in ("push-token:official-official_1", "push-token:public", "", "  "):
            with self.assertRaises(ValueError):
                transport.send(self._push_delivery(bad), None)
        self.assertEqual(client.calls, [])  # never called the provider

    def test_worker_provider_marks_placeholder_push_failed(self):
        client = RecordingClient()
        transport = ProviderPushTransport(
            endpoint="https://push.example.test/send", client=client)
        worker = DeliveryWorker(self.store, _clock, push_transport=transport)
        worker.process_pending()
        for d in self._pushes():
            self.assertEqual(d.status, DeliveryStatus.FAILED)
            self.assertIn("placeholder", (d.last_error or "").lower())
        self.assertEqual(client.calls, [])

    # -- success / failure / retry with a real token ----------------------
    def test_provider_success_sends_real_token(self):
        self._register_scheduler_push("device-abc123")
        client = RecordingClient()
        transport = ProviderPushTransport(
            endpoint="https://push.example.test/send", key="k", client=client)
        worker = DeliveryWorker(self.store, _clock, push_transport=transport)
        worker.process_pending()
        sched = next(d for d in self._pushes() if d.recipient_ref == "scheduler")
        self.assertEqual(sched.status, DeliveryStatus.SENT)
        self.assertTrue(any(c[2]["token"] == "device-abc123"
                            for c in client.calls))

    def test_provider_failure_then_retries_to_success(self):
        self._register_scheduler_push("device-abc123")
        state = {"fail": True}

        def client(endpoint, key, payload):
            if state["fail"]:
                raise OSError("temporary")
        transport = ProviderPushTransport(
            endpoint="https://push.example.test/send", client=client)
        worker = DeliveryWorker(self.store, _clock, push_transport=transport)

        def sched():
            return next(d for d in self._pushes() if d.recipient_ref == "scheduler")

        worker.process_pending()
        self.assertEqual(sched().status, DeliveryStatus.FAILED)
        self.assertEqual(sched().attempts, 1)
        state["fail"] = False
        worker.process_pending()
        self.assertEqual(sched().status, DeliveryStatus.SENT)
        self.assertEqual(sched().attempts, 2)

    # -- routing + overview ------------------------------------------------
    def test_only_push_goes_through_transport(self):
        transport = DryRunPushTransport()
        worker = DeliveryWorker(self.store, _clock, push_transport=transport)
        worker.process_pending()
        self.assertEqual(len(transport.outbox), len(self._pushes()))

    def test_apiservice_reflects_push_mode(self):
        store, _, _ = build_full_demo_store()
        default_ov = ApiService(store).get_delivery_overview(actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(default_ov["push_mode"], "dry_run")
        self.assertIsNone(default_ov["push_provider"])
        api = ApiService(store, push_transport=push_transport_from_env(PUSH_ENV))
        ov = api.get_delivery_overview(actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(ov["push_mode"], "live")
        self.assertEqual(ov["push_provider"], "fcm")


if __name__ == "__main__":
    unittest.main()
