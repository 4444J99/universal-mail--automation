"""Tests for core.patchbay — routing matrix and actuator sink dispatch."""

import json
from pathlib import Path

import pytest

from core.models import CommAction, CommMessage
from core.patchbay import PatchBay, PatchCable, SinkType


class _FakeResponse:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


class TestPatchCableMatches:
    def make_action(self, tier=None, channel="email", labels=None):
        action = CommAction(message_id="m1", channel_id=channel, sender="a@b.c")
        if tier is not None:
            action.priority_tier = tier
        action.add_labels = labels or []
        return action

    def test_min_tier_filters_higher_tiers(self):
        cable = PatchCable("c", SinkType.CALLBACK, "x", min_tier=1)
        assert cable.matches(self.make_action(tier=1)) is True
        assert cable.matches(self.make_action(tier=2)) is False

    def test_max_tier_filters_lower_tiers(self):
        cable = PatchCable("c", SinkType.CALLBACK, "x", min_tier=3, max_tier=2)
        assert cable.matches(self.make_action(tier=2)) is True
        assert cable.matches(self.make_action(tier=3)) is True
        assert cable.matches(self.make_action(tier=4)) is False
        assert cable.matches(self.make_action(tier=1)) is False

    def test_no_tier_action_without_max_bound(self):
        cable = PatchCable("c", SinkType.CALLBACK, "x", min_tier=1)
        assert cable.matches(self.make_action(tier=None)) is False  # unknown ≠ critical

    def test_tier_read_from_message_when_action_untiered(self):
        cable = PatchCable("c", SinkType.CALLBACK, "x", min_tier=2)
        msg = CommMessage(id="m1", sender="a@b.c", subject="s", priority_tier=2)
        assert cable.matches(self.make_action(tier=None), message=msg) is True

    def test_channel_filter(self):
        cable = PatchCable("c", SinkType.CALLBACK, "x", channels=["slack"])
        assert cable.matches(self.make_action(channel="slack")) is True
        assert cable.matches(self.make_action(channel="email")) is False

    def test_label_filter(self):
        cable = PatchCable("c", SinkType.CALLBACK, "x", labels=["Finance/Banking"])
        assert cable.matches(self.make_action(labels=["Finance/Banking", "Misc/Other"])) is True
        assert cable.matches(self.make_action(labels=["Dev/GitHub"])) is False


class TestPatchBaySinks:
    def test_callback_sink(self):
        bay = PatchBay()
        seen = []
        bay.connect(PatchCable(
            "cb", SinkType.CALLBACK, "bus",
            callback=lambda action, msg: seen.append((action.message_id, msg.subject or "")),
        ))
        action = CommAction(message_id="m1", sender="a@b.c")
        results = bay.transmit(action)
        assert results[0]["status"] == "delivered"
        assert seen == [("m1", "")]

    def test_provider_sink_invokes_callback(self):
        bay = PatchBay()
        seen = []
        action = CommAction(message_id="m1", sender="a@b.c")
        results = bay.transmit(action, provider_callback=lambda a: seen.append(a.message_id))
        assert len(results) == 0  # no matching cable
        assert seen == []

        bay.connect(PatchCable("p", SinkType.PROVIDER, "gmail"))
        results = bay.transmit(action, provider_callback=lambda a: seen.append(a.message_id))
        assert results[0]["status"] == "delivered_to_provider"
        assert seen == ["m1"]

    def test_local_log_writes_jsonl(self, tmp_path):
        bay = PatchBay()
        log_file = tmp_path / "out" / "routing.jsonl"
        cable = PatchCable("log", SinkType.LOCAL_LOG, str(log_file), min_tier=2)
        bay.connect(cable)
        action = CommAction(message_id="m1", sender="a@b.c")
        action.priority_tier = 1
        action.add_labels = ["Finance/Banking"]
        results = bay.transmit(action)
        assert results[0]["status"] == "ledger_written"
        entry = json.loads(log_file.read_text().strip().splitlines()[0])
        assert entry["message_id"] == "m1"
        assert entry["labels"] == ["Finance/Banking"]

    def test_webhook_sink_dispatches(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["timeout"] = timeout
            seen["body"] = json.loads(request.data)
            return _FakeResponse(200)

        bay = PatchBay(urlopen_fn=fake_urlopen)
        bay.connect(PatchCable("wh", SinkType.WEBHOOK, "https://hooks.example/x", timeout_s=3.0))
        action = CommAction(message_id="m1", sender="a@b.c")
        action.add_labels = ["Marketing"]
        results = bay.transmit(action)
        assert results[0]["status"] == "delivered_to_webhook"
        assert seen["timeout"] == 3.0
        assert seen["body"]["message_id"] == "m1"
        assert seen["body"]["labels"] == ["Marketing"]

    def test_webhook_sink_failure_is_honest(self):
        def failing_urlopen(request, timeout=None):
            raise ConnectionError("nope")

        bay = PatchBay(urlopen_fn=failing_urlopen)
        bay.connect(PatchCable("wh", SinkType.WEBHOOK, "https://hooks.example/x"))
        action = CommAction(message_id="m1", sender="a@b.c")
        results = bay.transmit(action)
        assert results[0]["status"] == "error"
        assert "nope" in results[0].get("error", "")

    def test_no_match_emits_nothing(self):
        bay = PatchBay()
        bay.connect(PatchCable("crit", SinkType.CALLBACK, "x", min_tier=1))
        action = CommAction(message_id="m1", sender="a@b.c")
        action.priority_tier = 4
        assert bay.transmit(action) == []

    def test_execution_history_records_each_dispatch(self):
        bay = PatchBay()
        bay.connect(PatchCable("cb", SinkType.CALLBACK, "x",
                               callback=lambda a, m: None))
        action = CommAction(message_id="m1", sender="a@b.c")
        bay.transmit(action)
        bay.transmit(action)
        assert len(bay._execution_history) == 2
        assert all(r["status"] == "delivered" for r in bay._execution_history)

    def test_disconnect_removes_cable(self):
        bay = PatchBay()
        bay.connect(PatchCable("cb", SinkType.CALLBACK, "x"))
        assert bay.disconnect("cb") is True
        assert bay.disconnect("cb") is False
        assert bay.active_cables() == []

    def test_priority_tier_is_declared_field(self):
        action = CommAction(message_id="m1", sender="a@b.c")
        assert action.priority_tier is None
        action.priority_tier = 1
        assert hasattr(type(action), "priority_tier")
        assert action.priority_tier == 1