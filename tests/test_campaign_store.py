"""Regression tests for host persistence, redacted sharing and job lifecycle."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.campaign_jobs import BackgroundJobs, _hashcat_mode
from backend.campaign_service import CampaignService
from backend.campaign_store import CampaignStore, campaign_identity


def _settings(tmp_path):
    return SimpleNamespace(
        ctfd_url="http://127.0.0.1:18000", allowed_categories=["WIN-10X"],
        campaign_id="verimarkt", campaign_data_dir=str(tmp_path),
        campaign_wordlist="", campaign_job_runner="native", sandbox_image="ctf-sandbox",
    )


def test_campaign_id_is_stable_and_safe():
    x = campaign_identity("HTTP://example:8000/", ["X", "Y"])
    assert x == campaign_identity("http://example:8000", ["Y", "X"])
    assert x != campaign_identity("http://example:8001", ["Y", "X"])
    assert campaign_identity("unused", override="verimarkt") == "verimarkt"
    with pytest.raises(ValueError):
        campaign_identity("unused", override="../../escape")


def test_vault_survives_restart_and_does_not_expose_secrets(tmp_path):
    store = CampaignStore(tmp_path / "campaign")
    updated = store.publish({
        "credentials": [{
            "username": "cem", "domain": "VERIMARKT",
            "password": "SecretX2026!", "hash": "1133557799ABCDEF", "verified": True,
        }],
        "findings": ["CEM has useful AD permissions"],
    }, "WIN-103")
    assert updated == {"facts": 1, "credentials": 1}
    assert b"SecretX2026!" not in store.db_path.read_bytes()
    assert b"1133557799ABCDEF" not in store.db_path.read_bytes()
    assert "SecretX2026!" not in json.dumps(store.search("cem"))
    fresh = CampaignStore(tmp_path / "campaign")
    meta = fresh.search("cem")["credentials"][0]
    assert meta["status"] == "verified"
    assert meta["has_password"] and meta["has_hash"]
    recovered = fresh.credential(meta["ref"])
    assert recovered["password"] == "SecretX2026!"
    assert recovered["hash"] == "1133557799ABCDEF"


def test_deduplicated_intelligence_and_credential_merging(tmp_path):
    store = CampaignStore(tmp_path)
    record = {
        "targets": [{"host": "10.20.1.2"}], "findings": ["ACL edge"],
        "credentials": [{"username": "burak", "domain": "VM", "hash": "abcd"}],
    }
    assert store.publish(record, "WIN-103") == {"facts": 2, "credentials": 1}
    assert store.publish({"targets": [{"host": "10.20.1.2"}]}, "WIN-104")["facts"] == 0
    store.publish({"credentials": [{"username": "Burak", "domain": "vm", "password": "pw"}]}, "WIN-104")
    credential = store.credential(store.search("burak")["credentials"][0]["ref"])
    assert credential["password"] == "pw"
    assert credential["hash"] == "abcd"
    assert len(store.search("")["facts"]) == 2


def test_atomic_rollback_on_invalid_secrets_in_facts(tmp_path):
    store = CampaignStore(tmp_path)
    with pytest.raises(ValueError):
        store.publish({"targets": [{"host": "1.2.3.4"}, {"password": "oops"}]}, "WIN-103")
    assert store.search("")["facts"] == []


def test_stage_acceptance_is_monotonic(tmp_path):
    store = CampaignStore(tmp_path)
    store.mark_stage("WIN-101", "accepted")
    store.mark_stage("WIN-101", "active")
    assert store.search("")["stages"][0]["status"] == "accepted"
    store.publish({"findings": ["path via AD"]}, "WIN-103")
    events = store.events()
    assert len(events) == 1
    assert store.events(after=events[0]["id"]) == []


class FakeBus:
    def __init__(self):
        self.events = []

    async def broadcast(self, message, source="coordinator"):
        self.events.append((source, message))


def test_shared_campaign_bus_and_restart(tmp_path):
    a = CampaignService(_settings(tmp_path))
    b = CampaignService(_settings(tmp_path))
    one, two = FakeBus(), FakeBus()
    a.register_bus(one)
    a.register_bus(two)
    asyncio.run(a.publish({"credentials": [{"username": "jale", "password": "test-secret"}]}, "WIN-103"))
    assert len(one.events) == len(two.events) == 1
    assert "test-secret" not in one.events[0][1]
    credential_ref = json.loads(b.search("jale"))["credentials"][0]["ref"]
    assert json.loads(b.credential(credential_ref))["password"] == "test-secret"


def test_hash_artifacts_are_content_addressed_and_path_checked(tmp_path):
    store = CampaignStore(tmp_path)
    ref = store.stage_artifact(b"$krb5asrep$23$abc", "../burak.hash", "WIN-103")
    assert ref.startswith("sha256:")
    assert store.artifact_path(ref).read_bytes() == b"$krb5asrep$23$abc"
    with pytest.raises(ValueError):
        store.artifact_path("../../escape")
    with pytest.raises(ValueError):
        store.stage_artifact(b"", "empty", "WIN-103")


def test_background_jobs_dedupe_and_resume(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path / "campaign")
    wordlist = tmp_path / "rockyou.txt"
    wordlist.write_text("password\n")
    hash_id = store.stage_artifact(b"$krb5asrep$23$abc", "burak.hash", "WIN-103")
    jobs = BackgroundJobs(store, wordlist=str(wordlist), runner="native",
                          john="python", hashcat="python")
    started = []
    monkeypatch.setattr(jobs, "_spawn", lambda job_id: started.append(job_id))
    first = jobs.submit(artifact_id=hash_id, principal="VM\\burak", hash_type="krb5asrep")
    second = jobs.submit(artifact_id=hash_id, principal="VM\\burak", hash_type="krb5asrep")
    assert first["id"] == second["id"] and second["deduplicated"]
    assert started == [first["id"]]
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='running',pid=99999999 WHERE id=?", (first["id"],))
    assert jobs.resume_incomplete() == 1
    assert started == [first["id"], first["id"]]
    assert jobs.get(first["id"])["status"] == "queued"



@pytest.mark.parametrize(
    ("kind", "signature", "expected"),
    [
        ("ntlm", b"0123456789abcdef0123456789abcdef", "1000"),
        ("krb5asrep", b"$krb5asrep$23$user:test", "18200"),
        ("krb5asrep", b"$krb5asrep$17$user:test", "32100"),
        ("krb5asrep", b"$krb5asrep$18$user:test", "32200"),
        ("krb5tgs", b"$krb5tgs$23$*user*:x", "13100"),
        ("krb5tgs", b"$krb5tgs$17$*user*:x", "19600"),
        ("krb5tgs", b"$krb5tgs$18$*user*:x", "19700"),
    ],
)
def test_hashcat_selects_correct_etype(tmp_path, kind, signature, expected):
    path = tmp_path / "ticket.hash"
    path.write_bytes(signature)
    assert _hashcat_mode(kind, path) == expected


def test_hashcat_rejects_unknown_etype(tmp_path):
    path = tmp_path / "ticket.hash"
    path.write_bytes(b"$krb5tgs$99$*user*:x")
    with pytest.raises(ValueError, match="etype"):
        _hashcat_mode("krb5tgs", path)


def test_campaign_search_can_match_domain_separator(tmp_path):
    store = CampaignStore(tmp_path)
    store.publish(
        {"credentials": [{"domain": "VERIMARKT", "username": "cem", "password": "secret"}]},
        "WIN-103",
    )
    assert len(store.search(r"VERIMARKT\cem")["credentials"]) == 1
