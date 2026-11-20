"""Accessible-occupancy audit: attribution, contracts and evidence versions."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from parking import accessible, capacity, gate, report, sensor
from parking.sources import EvidenceConflict
from parking.store import connect

AS_OF = "2026-11-20T11:05:00Z"


def fixture(name):
    return json.loads((ROOT / "fixtures" / "parking" / name).read_text(encoding="utf-8"))


class AccessibleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "case.sqlite")
        sensor.ingest(self.db, fixture("accessible_sensor.json"))
        gate.ingest(self.db, fixture("accessible_gate.json"))
        capacity.ingest(self.db, fixture("accessible_capacity.json"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def audit(self, as_of=AS_OF, **kwargs):
        return accessible.audit(self.db, as_of, **kwargs)

    def bay(self, result, bay_id="A"):
        matches = [p for p in result["occupancy"] if p["bay_id"] == bay_id]
        self.assertEqual(len(matches), 1, result["occupancy"])
        return matches[0]

    def targets(self, result):
        return {item["bay_id"]: item for item in result["contracts"]["targeted"]}

    def patrols(self, result):
        return {item["bay_id"]: item for item in result["contracts"]["patrol"]}


class AttributionCases(AccessibleBase):
    def test_interchangeable_stays_still_prove_unauthorized_bay(self):
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unauthorized", "every_attribution_lacks_valid_permit"))
        self.assertEqual(bay["candidates"], ["S1", "S2"])
        self.assertIsNone(bay["always_unlicensed_stay"])
        self.assertEqual(result["assignment_count"], 2)
        # Neither stay is pinned: both complete attributions remain feasible.
        for attribution in bay["attributions"]:
            self.assertEqual((attribution["occurrences"], attribution["total"]), (1, 2))

    def test_patrol_dispatches_to_bay_even_without_a_named_stay(self):
        result = self.audit()
        patrol = self.patrols(result)["A"]
        self.assertEqual((patrol["decision"], patrol["reason"]),
                         ("dispatch", "unauthorized_occupancy_proven"))
        self.assertEqual((patrol["from"], patrol["to"]),
                         ("2026-11-20T10:00:00Z", "2026-11-20T11:00:00Z"))
        self.assertNotIn("stay_id", patrol)

    def test_targeted_contract_refuses_to_pick_a_stay(self):
        result = self.audit()
        targeted = self.targets(result)["A"]
        self.assertEqual((targeted["decision"], targeted["stay_id"], targeted["reason"]),
                         ("pending_identity", None,
                          "stay_identity_differs_across_attributions"))

    def test_late_scan_pins_stay_and_both_contracts_fire(self):
        accessible.ingest(self.db, fixture("accessible_late_scan.json"))
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"], bay["pins"]),
                         ("unauthorized", "attribution_lacks_valid_permit", ["S2"]))
        self.assertEqual(bay["always_unlicensed_stay"], "S2")
        self.assertEqual([a["stay_ids"] for a in bay["attributions"]], [["S2"]])
        self.assertEqual(bay["attributions"][0]["reason"], "permit_expired")
        self.assertEqual(self.patrols(result)["A"]["decision"], "dispatch")
        targeted = self.targets(result)["A"]
        self.assertEqual((targeted["decision"], targeted["stay_id"]),
                         ("notify", "S2"))
        # The expired credential does not become an authorization.
        self.assertFalse(bay["attributions"][0]["licensed"])

    def test_valid_permit_held_by_one_of_two_stays_stays_unknown(self):
        accessible.ingest(self.db, fixture("accessible_valid_permit.json"))
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unknown", "licensed_under_some_attributions_only"))
        self.assertEqual(self.patrols(result)["A"]["decision"], "no_dispatch")
        self.assertEqual(self.targets(result)["A"]["decision"], "pending_verification")
        self.assertIsNone(self.targets(result)["A"]["stay_id"])
        self.assertFalse(any(p["decision"] == "notify"
                             for p in result["contracts"]["targeted"]))

    def test_one_stay_cannot_occupy_two_bays_at_once(self):
        result = self.audit()
        # There are exactly two complete attributions, never a doubled stay.
        self.assertEqual(result["assignment_count"], 2)
        self.assertNotIn("no_feasible_attribution", result["reasons"])

    def test_bay_violation_with_unknown_car_is_not_a_targeted_notice(self):
        # Baseline-only lot: the bay is proven unauthorized, but without a stay
        # identity the patrol contract fires and the targeted one stays pending.
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "baseline.sqlite")
            try:
                sensor.ingest(db, fixture("accessible_sensor.json"))
                capacity.ingest(db, fixture("accessible_capacity.json"))
                gate.ingest(db, [{"event_id": "acc-baseline", "op": "baseline",
                                  "count": 2,
                                  "event_time": "2026-11-20T09:00:00Z"}])
                result = accessible.audit(db, AS_OF, save=False)
                self.assertEqual(self.bay(result)["verdict"], "unauthorized")
                self.assertEqual(self.patrols(result)["A"]["decision"], "dispatch")
                self.assertEqual(self.targets(result)["A"]["decision"],
                                 "pending_identity")
                self.assertIsNone(self.targets(result)["A"]["stay_id"])
            finally:
                db.close()

    def test_gate_baseline_alone_never_invents_a_stay(self):
        # A fresh lot: only a certified count, with no gate receipts at all.
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "baseline.sqlite")
            try:
                sensor.ingest(db, fixture("accessible_sensor.json"))
                capacity.ingest(db, fixture("accessible_capacity.json"))
                gate.ingest(db, [{"event_id": "acc-baseline", "op": "baseline",
                                  "count": 2,
                                  "event_time": "2026-11-20T09:00:00Z"}])
                result = accessible.audit(db, AS_OF, save=False)
                bay = self.bay(result)
                # No stay identity exists, but the permit ledger proves the
                # bay had no valid permit: the bay violation stands even though
                # the car is unknown.
                self.assertEqual(
                    (bay["verdict"], bay["reason"], bay["candidates"]),
                    ("unauthorized", "no_stay_identity_for_occupied_bay", []))
                self.assertEqual(self.patrols(result)["A"]["decision"], "dispatch")
                targeted = self.targets(result)["A"]
                self.assertEqual((targeted["decision"], targeted["stay_id"]),
                                 ("pending_identity", None))
                self.assertEqual(result["stays"], [])
            finally:
                db.close()


class ReasonSeparation(AccessibleBase):
    def test_late_revocation_produces_new_audit_version(self):
        accessible.ingest(self.db, fixture("accessible_late_scan.json"))
        # Replace the expired permit by a currently valid one, scanned by S2.
        accessible.ingest(self.db, [{
            "event_id": "acc-permit-late-valid", "kind": "permit", "permit_id": "P1",
            "credential_id": "C1", "status": "approved", "bays": ["A"],
            "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
            "source": "ranger", "trusted": True, "event_time": "2026-11-20T08:00:00Z"}])
        before = self.audit()
        self.assertEqual(self.bay(before)["verdict"], "authorized")
        version_before = before["audit_version"]
        accessible.ingest(self.db, [{
            "event_id": "acc-permit-revoke", "kind": "permit", "permit_id": "P1",
            "credential_id": "C1", "status": "revoked", "bays": ["A"],
            "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
            "source": "ranger", "trusted": True, "event_time": "2026-11-20T10:40:00Z"}])
        after = self.audit()
        self.assertEqual(self.bay(after)["verdict"], "unauthorized")
        self.assertEqual(self.bay(after)["attributions"][0]["reason"], "permit_revoked")
        self.assertGreater(after["audit_version"], version_before)
        # Re-evaluating as of a moment before the revocation keeps the old view.
        earlier = accessible.audit(self.db, "2026-11-20T10:30:00Z", save=False)
        self.assertEqual(self.bay(earlier)["verdict"], "authorized")

    def test_permit_used_at_another_bay_cannot_license_this_one(self):
        # A has a valid permit, but the credential is scanned at bay B by S2
        # during the same period.  S2 is thereby pinned to B, and the permit
        # cannot belong to S1 at A: A is unauthorized for the only attribution.
        accessible.ingest(self.db, [
            {"event_id": "acc-permit-a", "kind": "permit", "permit_id": "PA",
             "credential_id": "CA", "status": "approved", "bays": ["A"],
             "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
             "source": "ranger", "trusted": True,
             "event_time": "2026-11-20T08:00:00Z"},
            {"event_id": "acc-scan-b", "kind": "bay_scan", "bay_id": "B",
             "permit_id": "PA", "credential_id": "CA", "stay_id": "S2",
             "scanned_at": "2026-11-20T10:20:00Z", "source": "bay_reader",
             "trusted": True}])
        result = self.audit()
        bay = self.bay(result, "A")
        self.assertEqual((bay["verdict"], bay["candidates"]),
                         ("unauthorized", ["S1"]))
        self.assertEqual(bay["always_unlicensed_stay"], "S1")
        self.assertEqual(self.targets(result)["A"]["stay_id"], "S1")

    def test_duplicate_gate_receipt_cannot_identify_a_stay(self):
        gate.ingest(self.db, [{"event_id": "acc-dup-in", "stay_id": "S1",
                               "direction": "in",
                               "event_time": "2026-11-20T09:57:00Z"}])
        result = self.audit()
        self.assertNotIn("S1", {s["stay_id"] for s in result["stays"]})
        self.assertIn({"stay_id": "S1", "reason": "duplicate_gate_receipt"},
                      result["excluded_stays"])
        self.assertNotEqual(self.bay(result)["candidates"], ["S1", "S2"])
        self.assertEqual(self.bay(result)["verdict"], "unauthorized")

    def test_orphan_exit_does_not_identify_a_stay(self):
        gate.ingest(self.db, [{"event_id": "acc-orphan", "stay_id": "S9",
                               "direction": "out",
                               "event_time": "2026-11-20T10:30:00Z"}])
        result = self.audit()
        self.assertIn({"stay_id": "S9", "reason": "orphan_gate_receipt"},
                      result["excluded_stays"])

    def test_new_permit_revision_is_not_a_duplicate_source(self):
        accessible.ingest(self.db, [
            {"event_id": "acc-permit-orig", "kind": "permit", "permit_id": "PR",
             "credential_id": "CR", "status": "approved", "bays": ["A"],
             "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
             "source": "ranger", "trusted": True,
             "event_time": "2026-11-20T08:00:00Z"},
            {"event_id": "acc-ver-r", "kind": "verification", "permit_id": "PR",
             "credential_id": "CR", "stay_id": "S2",
             "verified_at": "2026-11-20T10:10:00Z", "source": "ranger",
             "trusted": True}])
        accessible.ingest(self.db, [
            {"event_id": "acc-permit-revoked", "kind": "permit", "permit_id": "PR",
             "credential_id": "CR", "status": "revoked", "bays": ["A"],
             "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
             "source": "ranger", "trusted": True,
             "event_time": "2026-11-20T10:20:00Z"}])
        # Re-approval is a new revision, never a duplicate source.
        report = accessible.ingest(self.db, [
            {"event_id": "acc-permit-reapproved", "kind": "permit", "permit_id": "PR",
             "credential_id": "CR", "status": "approved", "bays": ["A"],
             "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
             "source": "ranger", "trusted": True,
             "event_time": "2026-11-20T10:40:00Z"}])
        self.assertEqual((report["imported"], report["duplicates"]), (1, 0))
        result = self.audit()
        self.assertNotIn("duplicate_source_ignored", result["reasons"])
        # Re-approval restores S2's license; S1/S2 remain interchangeable, so
        # the bay cannot be declared authorized or unauthorized.
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unknown", "licensed_under_some_attributions_only"))
        licensed = {a["stay_ids"][0] for a in bay["attributions"] if a["licensed"]}
        self.assertEqual(licensed, {"S2"})

    def test_duplicate_source_is_reported_but_not_counted_twice(self):
        accessible.ingest(self.db, [{
            "event_id": "acc-permit-b", "kind": "permit", "permit_id": "P9",
            "credential_id": "C9", "status": "approved", "bays": ["B"],
            "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
            "source": "ranger", "trusted": True,
            "event_time": "2026-11-20T08:00:00Z"}])
        scan = {"event_id": "acc-dup-1", "kind": "bay_scan", "bay_id": "A",
                "permit_id": "P9", "credential_id": "C9", "stay_id": "S2",
                "scanned_at": "2026-11-20T10:30:00Z", "source": "bay_reader",
                "trusted": True}
        self.assertEqual(accessible.ingest(self.db, [scan])["imported"], 1)
        self.assertEqual(accessible.ingest(self.db, [dict(scan, event_id="acc-dup-2")]),
                         {"imported": 0, "duplicates": 1})
        result = self.audit()
        self.assertEqual(result["evidence_refs"]["duplicates"], ["acc-dup-2"])
        self.assertIn("duplicate_source_ignored", result["reasons"])
        # The second copy is flagged and ignored; the first stays a real scan.
        self.assertEqual(self.bay(result)["candidates"], ["S2"])
        self.assertEqual(self.bay(result)["pins"], ["S2"])
        self.assertEqual(self.bay(result)["verdict"], "unauthorized")
        self.assertEqual(self.bay(result)["attributions"][0]["reason"],
                         "permit_does_not_cover_bay")

    def test_same_event_id_different_body_is_a_conflict(self):
        scan = {"event_id": "acc-clash", "kind": "bay_scan", "bay_id": "A",
                "permit_id": "P9", "credential_id": "C9", "stay_id": "S2",
                "scanned_at": "2026-11-20T10:30:00Z", "source": "bay_reader",
                "trusted": True}
        with self.assertRaises(EvidenceConflict):
            accessible.ingest(self.db, [scan, dict(scan, bay_id="B")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bay_scan").fetchone()[0], 0)

    def test_unknown_geomagnetic_reading_blocks_a_verdict(self):
        sensor.ingest(self.db, [{"event_id": "acc-dev-bad", "op": "device",
                                 "device_id": "acc-da", "bay_id": "A",
                                 "calibrated_until": "2026-11-20T10:20:00Z",
                                 "event_time": "2026-11-20T10:01:00Z"}])
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]), ("unknown", "sensor_unknown"))
        self.assertIn("untrusted_device",
                      {item["reason"] for item in result["unknown_readings"]})
        self.assertEqual(self.patrols(result)["A"]["decision"], "no_dispatch")

    def test_scan_outside_the_occupancy_is_not_evidence_of_absence(self):
        accessible.ingest(self.db, [{
            "event_id": "acc-scan-early", "kind": "bay_scan", "bay_id": "A",
            "permit_id": "P1", "credential_id": "C1", "stay_id": "S2",
            "scanned_at": "2026-11-20T09:10:00Z", "source": "bay_reader",
            "trusted": True}])
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unknown", "credential_use_outside_occupancy"))

    def test_valid_permit_not_bound_to_a_stay_blocks_unauthorized_verdict(self):
        accessible.ingest(self.db, [{
            "event_id": "acc-permit-open", "kind": "permit", "permit_id": "P1",
            "credential_id": "C1", "status": "approved", "bays": ["A"],
            "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
            "source": "ranger", "trusted": True,
            "event_time": "2026-11-20T08:00:00Z"}])
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unknown", "valid_permit_for_bay_unbound"))
        self.assertEqual(self.patrols(result)["A"]["decision"], "no_dispatch")
        self.assertEqual(self.targets(result)["A"]["decision"], "pending_verification")

    def test_permit_used_elsewhere_in_time_does_not_license(self):
        accessible.ingest(self.db, [{
            "event_id": "acc-permit-ok", "kind": "permit", "permit_id": "P1",
            "credential_id": "C1", "status": "approved", "bays": ["A"],
            "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
            "source": "ranger", "trusted": True,
            "event_time": "2026-11-20T08:00:00Z"},
            {"event_id": "acc-scan-early", "kind": "bay_scan", "bay_id": "A",
             "permit_id": "P1", "credential_id": "C1", "stay_id": "S2",
             "scanned_at": "2026-11-20T09:10:00Z", "source": "bay_reader",
             "trusted": True}])
        bay = self.bay(self.audit())
        # The credential is not bound to the period for S2; the period stays
        # unknown rather than becoming either authorized or unauthorized.
        self.assertEqual(bay["verdict"], "unknown")
        self.assertFalse(any(a["licensed"] for a in bay["attributions"]))

    def test_shared_credential_scan_without_stay_does_not_crash(self):
        # A bay scan carrying a credential used by two stays, plus no stay_id
        # on the scan, previously raised KeyError while applying the conflict.
        accessible.ingest(self.db, [
            {"event_id": "acc-permit-shared", "kind": "permit", "permit_id": "PS",
             "credential_id": "CS", "status": "approved", "bays": ["A"],
             "valid_from": "2026-11-20T08:00:00Z", "valid_to": "2026-11-20T12:00:00Z",
             "source": "ranger", "trusted": True,
             "event_time": "2026-11-20T08:00:00Z"},
            {"event_id": "acc-ver-1", "kind": "verification", "permit_id": "PS",
             "credential_id": "CS", "stay_id": "S1",
             "verified_at": "2026-11-20T10:10:00Z", "source": "ranger",
             "trusted": True},
            {"event_id": "acc-ver-2", "kind": "verification", "permit_id": "PS",
             "credential_id": "CS", "stay_id": "S2",
             "verified_at": "2026-11-20T10:10:00Z", "source": "ranger",
             "trusted": True},
            {"event_id": "acc-scan-shared", "kind": "bay_scan", "bay_id": "A",
             "permit_id": "PS", "credential_id": "CS",
             "scanned_at": "2026-11-20T10:20:00Z", "source": "bay_reader",
             "trusted": True}])
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unknown", "credential_shared_by_stays"))
        self.assertIn("credential_shared_by_stays", result["reasons"])

    def test_one_stay_scanned_at_two_bays_is_a_conflict_not_a_notice(self):
        # S1 is scanned at A and B over the same interval; a car cannot occupy
        # both.  Both bays stay unresolved and no patrol notice is issued.
        capacity.ingest(self.db, [{
            "event_id": "acc-cap-ab", "order_id": "acc-order-ab",
            "purpose": "accessible", "priority": 5, "approved": True,
            "revoked": False, "bays": ["A", "B"],
            "starts_at": "2026-11-20T09:00:00Z", "ends_at": "2026-11-20T12:00:00Z",
            "event_time": "2026-11-20T09:00:00Z"}])
        for bay in ("A", "B"):
            accessible.ingest(self.db, [{
                "event_id": f"acc-scan-{bay}", "kind": "bay_scan", "bay_id": bay,
                "credential_id": "CX", "stay_id": "S1",
                "scanned_at": "2026-11-20T10:30:00Z", "source": "bay_reader",
                "trusted": True}])
        result = self.audit()
        for bay_id in ("A", "B"):
            bay = self.bay(result, bay_id)
            self.assertEqual((bay["verdict"], bay["reason"]),
                             ("unknown", "conflicting_scans_for_stay"))
            self.assertEqual(bay["pins"], [])
        self.assertFalse(any(c["decision"] == "dispatch"
                             for c in result["contracts"]["patrol"]))

    def test_credential_reused_sequentially_is_not_a_simultaneous_share(self):
        # S1 leaves before S2 arrives; the same credential is verified for each
        # at a different time.  That is reuse, not two cars using it at once.
        gate.ingest(self.db, [
            {"event_id": "acc-seq-s1-in", "stay_id": "Q1", "direction": "in",
             "event_time": "2026-11-20T09:55:00Z"},
            {"event_id": "acc-seq-s1-out", "stay_id": "Q1", "direction": "out",
             "event_time": "2026-11-20T10:20:00Z"},
            {"event_id": "acc-seq-s2-in", "stay_id": "Q2", "direction": "in",
             "event_time": "2026-11-20T10:25:00Z"},
            {"event_id": "acc-seq-s2-out", "stay_id": "Q2", "direction": "out",
             "event_time": "2026-11-20T11:05:00Z"}])
        for stay, moment in (("Q1", "2026-11-20T10:05:00Z"),
                             ("Q2", "2026-11-20T10:30:00Z")):
            accessible.ingest(self.db, [{
                "event_id": f"acc-seq-ver-{stay}", "kind": "verification",
                "permit_id": "PQ", "credential_id": "CQ", "stay_id": stay,
                "verified_at": moment, "source": "ranger", "trusted": True}])
        result = self.audit()
        bay = self.bay(result)
        self.assertNotEqual(bay["reason"], "credential_shared_by_stays")
        self.assertNotIn("credential_shared_by_stays", result["reasons"])
        # Neither Q1 nor Q2 covers the full period, so no stay can be attributed;
        # the bay violation may still warrant patrol but never a targeted notice.
        self.assertFalse(any(c["decision"] == "notify"
                             for c in result["contracts"]["targeted"]))
        self.assertIsNone(self.targets(result)["A"]["stay_id"])

    def test_one_credential_used_by_two_stays_stays_conflicted(self):
        for stay in ("S1", "S2"):
            accessible.ingest(self.db, [{
                "event_id": f"acc-ver-{stay}", "kind": "verification",
                "permit_id": "P1", "credential_id": "C1", "stay_id": stay,
                "verified_at": "2026-11-20T10:10:00Z", "source": "ranger",
                "trusted": True}])
        result = self.audit()
        bay = self.bay(result)
        self.assertEqual((bay["verdict"], bay["reason"]),
                         ("unknown", "credential_shared_by_stays"))
        self.assertEqual(self.targets(result)["A"]["decision"], "pending_verification")
        self.assertIn("credential_shared_by_stays", result["reasons"])

    def test_late_manual_observation_appends_a_new_verdict(self):
        first = self.audit()
        self.assertEqual(first["assignment_count"], 2)
        accessible.ingest(self.db, [{
            "event_id": "acc-observation", "kind": "manual_observation", "bay_id": "A",
            "permit_id": None, "credential_id": "C1", "stay_id": "S1",
            "observed_from": "2026-11-20T10:20:00Z",
            "observed_to": "2026-11-20T10:25:00Z", "source": "ranger",
            "trusted": True}])
        second = self.audit()
        self.assertEqual(self.bay(second)["pins"], ["S1"])
        self.assertEqual(self.bay(second)["always_unlicensed_stay"], "S1")
        self.assertEqual(self.targets(second)["A"]["stay_id"], "S1")
        self.assertGreater(second["audit_version"], first["audit_version"])


class CapacityRecompute(AccessibleBase):
    def test_capacity_revocation_and_reason_change_recompute_scope(self):
        first = self.audit()
        self.assertEqual(first["scope"]["reason"], "capacity_accessible_order")
        self.assertEqual(first["accessible_bays"], ["A"])
        capacity.ingest(self.db, [{
            "event_id": "acc-cap-revoke", "order_id": "acc-order-a",
            "purpose": "accessible", "priority": 5, "approved": True,
            "revoked": True, "bays": ["A"], "starts_at": "2026-11-20T09:00:00Z",
            "ends_at": "2026-11-20T12:00:00Z",
            "event_time": "2026-11-20T10:50:00Z"}])
        second = self.audit()
        self.assertEqual(second["accessible_bays"], [])
        self.assertEqual(second["scope"]["reason"], "accessible_order_not_active")
        self.assertEqual(second["scope_notices"][0]["reason"], "accessible_order_revoked")
        self.assertEqual(second["contracts"]["patrol"], [])
        self.assertGreater(second["audit_version"], first["audit_version"])

    def test_unrelated_ordinary_occupancy_does_not_trigger_notices(self):
        # Bay B is occupied 12:00-13:00, an hour after the accessible period.
        sensor.ingest(self.db, [
            {"event_id": "acc-read-b3", "op": "reading", "device_id": "acc-db",
             "bay_id": "B", "occupied": True, "sequence_no": 3,
             "event_time": "2026-11-20T12:00:00Z"},
            {"event_id": "acc-read-b4", "op": "reading", "device_id": "acc-db",
             "bay_id": "B", "occupied": False, "sequence_no": 4,
             "event_time": "2026-11-20T12:30:00Z"}])
        result = self.audit()
        self.assertEqual([p["bay_id"] for p in result["occupancy"]], ["A"])
        self.assertEqual(self.patrols(result)["A"]["decision"], "dispatch")
        self.assertEqual(len(result["contracts"]["targeted"]), 1)
        self.assertEqual(self.targets(result)["A"]["decision"], "pending_identity")


class PersistenceAndViews(AccessibleBase):
    def test_audit_persists_versions_and_attribution_evidence(self):
        result = self.audit()
        audit_id = result["audit_version"]
        self.assertIsNotNone(audit_id)
        rows = accessible.assignments(self.db, audit_id)
        self.assertEqual({r["stay_id"] for r in rows}, {"S1", "S2"})
        self.assertEqual({r["confidence"] for r in rows}, {"ambiguous"})
        evidence = json.loads(rows[0]["permutation_json"])
        self.assertEqual(evidence["assignment_count"], 2)
        self.assertIn("excluded", evidence)
        incidents = accessible.incidents(self.db, audit_id)
        self.assertEqual([(i["bay_id"], i["decision"]) for i in incidents],
                         [("A", "unauthorized")])
        dispatches = accessible.dispatches(self.db, audit_id)
        self.assertEqual({d["contract"] for d in dispatches}, {"patrol", "targeted"})
        # Patrol is dispatched while the targeted contract remains pending.
        decision = {(d["contract"], d["decision"]) for d in dispatches}
        self.assertIn(("patrol", "dispatch"), decision)
        self.assertIn(("targeted", "pending_identity"), decision)

    def test_late_scan_creates_a_second_version_without_editing_the_first(self):
        first = self.audit()
        accessible.ingest(self.db, fixture("accessible_late_scan.json"))
        second = self.audit()
        self.assertEqual(len(accessible.history(self.db)), 2)
        first_rows = accessible.assignments(self.db, first["audit_version"])
        self.assertEqual({r["confidence"] for r in first_rows}, {"ambiguous"})
        second_rows = accessible.assignments(self.db, second["audit_version"])
        self.assertEqual({r["stay_id"] for r in second_rows}, {"S2"})
        self.assertEqual({r["confidence"] for r in second_rows}, {"proven"})

    def test_read_only_audit_does_not_add_a_version(self):
        report.accessible_view(self.db, AS_OF, save=False)
        self.assertEqual(accessible.history(self.db), [])
        report.accessible_view(self.db, AS_OF, save=True)
        self.assertEqual(len(accessible.history(self.db)), 1)

    def test_accessible_audit_does_not_change_ordinary_availability(self):
        before = report.decide(self.db, AS_OF, 3)
        self.audit()
        accessible.ingest(self.db, fixture("accessible_late_scan.json"))
        self.audit()
        after = report.decide(self.db, AS_OF, 3)
        self.assertEqual((before["state"], before["available"], before["capacity"]),
                         (after["state"], after["available"], after["capacity"]))

    def test_report_view_surfaces_verdict_without_raw_dump(self):
        accessible.ingest(self.db, fixture("accessible_late_scan.json"))
        view = report.accessible_view(self.db, AS_OF, save=True)
        self.assertEqual(view["accessible_bays"], ["A"])
        self.assertEqual(view["occupancy"][0]["verdict"], "unauthorized")
        self.assertEqual(view["contracts"]["targeted"][0]["stay_id"], "S2")
        self.assertIn("evidence_refs", view)
        self.assertEqual(set(view["evidence_refs"]),
                         {"permits", "verifications", "bay_scans", "observations",
                          "duplicates", "conflicts", "notes"})


if __name__ == "__main__":
    unittest.main()
