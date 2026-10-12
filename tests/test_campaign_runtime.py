"""Process-level integration tests for detached worker and operator bootstrap."""
import os
import subprocess
import sys
import time

from backend.campaign_jobs import BackgroundJobs
from backend.campaign_store import CampaignStore


def test_detached_native_worker_publishes_to_vault(tmp_path):
    store = CampaignStore(tmp_path / "campaign")
    wordlist = tmp_path / "rockyou.txt"
    wordlist.write_text("one\ntwo\n")
    fake_john = tmp_path / "fake_john"
    fake_john.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if '--show' in sys.argv:\n"
        "    print('burak:RecoveredPassword123!')\n"
        "else:\n"
        "    print('Cracking job ran successfully')\n"
    )
    fake_john.chmod(0o700)
    ref = store.stage_artifact(b"$krb5asrep$23$TEST_HASH", "burak.hash", "WIN-103")
    jobs = BackgroundJobs(store, wordlist=str(wordlist), runner="native", john=str(fake_john))
    result = jobs.submit(
        artifact_id=ref, principal="VERIMARKT\\burak", hash_type="krb5asrep"
    )
    deadline = time.monotonic() + 10
    state = jobs.get(result["id"])
    while state["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
        state = jobs.get(result["id"])
    assert state["status"] == "completed", state
    reloaded = CampaignStore(tmp_path / "campaign")
    cred_ref = reloaded.search("burak")["credentials"][0]["ref"]
    assert reloaded.credential(cred_ref)["password"] == "RecoveredPassword123!"
    assert b"RecoveredPassword123!" not in reloaded.db_path.read_bytes()
    log_path = reloaded.jobs_dir / result["id"] / "supervisor.log"
    assert b"RecoveredPassword123!" not in log_path.read_bytes()
    assert any(event["kind"] == "job" for event in reloaded.events())


def test_manual_import_persists_for_new_run(tmp_path):
    data = tmp_path / "campaign-import.json"
    data.write_text(
        '{"credentials":[{"username":"jale","domain":"VERIMARKT",'
        '"password":"OldLabPass!","verified":true}],'
        '"targets":[{"host":"10.20.103.110"}]}'
    )
    data.chmod(0o600)
    environment = dict(os.environ)
    environment["CTF_CAMPAIGN_DATA_DIR"] = str(tmp_path)
    command = [
        sys.executable, "-m", "backend.campaign_cli", str(data),
        "--campaign-id", "lab-2026", "--challenge", "WIN-103",
    ]
    result = subprocess.run(command,env=environment,capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert "OldLabPass!" not in result.stdout
    store = CampaignStore(tmp_path / "lab-2026")
    credential = store.search("jale")["credentials"][0]
    assert store.credential(credential["ref"])["password"] == "OldLabPass!"
    assert len(store.search("10.20.103.110")["facts"]) == 1


def test_unverified_candidate_does_not_poison_verified_secret(tmp_path):
    store = CampaignStore(tmp_path)
    store.publish({"credentials": [{"username": "cem", "password": "known",
                                     "verified": True}]}, "WIN-103")
    store.publish({"credentials": [{"username": "cem", "password": "guessed"}]}, "WIN-104")
    ref = store.search("cem")["credentials"][0]["ref"]
    assert store.credential(ref)["password"] == "known"
    assert store.credential(ref)["status"] == "verified"
