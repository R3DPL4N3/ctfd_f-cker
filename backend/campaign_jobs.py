"""Detached, durable and network-isolated offline cracking jobs.

John/Hashcat jobs are operator-configured and intentionally disconnected from
the target network. No command shells or arbitrary model-provided argv.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.campaign_store import CampaignStore, _utc

FORMATS = {
    "krb5asrep": ("krb5asrep", "18200"),
    "krb5tgs": ("krb5tgs", "13100"),
    "ntlm": ("NT", "1000"),
}


def _alive(pid: int | None) -> bool:
    if not pid or pid < 2:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


class BackgroundJobs:
    def __init__(self, store: CampaignStore, *, wordlist: str = "",
                 image: str = "ctf-sandbox", runner: str = "docker",
                 john: str = "john", hashcat: str = "hashcat"):
        if runner not in {"docker", "native"}:
            raise ValueError("runner must be docker or native")
        self.store = store
        self.wordlist = wordlist
        self.image = image
        self.runner = runner
        self.john = john
        self.hashcat = hashcat

    def _wordlist(self) -> Path:
        if not self.wordlist:
            raise ValueError("Set CTF_CAMPAIGN_WORDLIST to enable offline jobs")
        path = Path(self.wordlist).expanduser().resolve()
        if not path.is_file():
            raise ValueError("Configured cracking wordlist does not exist")
        if self.runner == "docker" and not shutil.which("docker"):
            raise RuntimeError("Docker CLI unavailable")
        return path

    def submit(self, *, artifact_id: str, principal: str,
               hash_type: str, engine: str = "john") -> dict[str, Any]:
        if hash_type not in FORMATS or engine not in {"john", "hashcat"}:
            raise ValueError("Unsupported offline cracker engine/hash type")
        if not principal or len(principal) > 256:
            raise ValueError("principal required (max 256 chars)")
        self.store.artifact_path(artifact_id)
        wordlist = self._wordlist()
        if self.runner == "native" and not shutil.which(
            self.john if engine == "john" else self.hashcat
        ):
            raise RuntimeError(f"{engine} is not installed on the host")
        fingerprint = hashlib.sha256(
            f"{engine}|{hash_type}|{artifact_id}|{principal.casefold()}|{wordlist}".encode()
        ).hexdigest()
        job_id = fingerprint[:24]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id,status FROM jobs WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                db.execute("COMMIT")
                return {"id": existing["id"], "status": existing["status"], "deduplicated": True}
            db.execute("""INSERT INTO jobs
                (id,fingerprint,engine,hash_type,artifact_id,principal,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id,fingerprint,engine,hash_type,artifact_id,principal,"queued",_utc(),_utc()))
            db.execute("COMMIT")
        self._spawn(job_id)
        return {"id": job_id, "status": "queued", "deduplicated": False}

    def _spawn(self, job_id: str) -> None:
        wordlist = self._wordlist()
        private_dir = self.store.jobs_dir / job_id
        private_dir.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            private_dir.chmod(0o700)
        argv = [sys.executable, "-m", "backend.campaign_jobs", "--execute",
                str(self.store.root), job_id, str(wordlist), self.runner,
                self.image, self.john, self.hashcat]
        # Separate process session: Ctrl+C on ctf-solve does not terminate workers.
        # Discard worker output, since crackers may print recovered passwords.
        with (private_dir / "supervisor.log").open("ab") as log:
            proc = subprocess.Popen(
                argv, cwd=str(Path(__file__).resolve().parent.parent),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        if os.name == "posix":
            (private_dir / "supervisor.log").chmod(0o600)
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET pid=?,updated_at=? WHERE id=? AND status='queued'",
                (proc.pid,_utc(),job_id),
            )

    def get(self, job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{24}", job_id):
            raise ValueError("invalid job ID")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("job not found")
        return {key: row[key] for key in (
            "id","engine","hash_type","artifact_id","principal",
            "status","attempts","error","updated_at",
        )}

    def resume_incomplete(self) -> int:
        if not self.wordlist:
            return 0
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,pid FROM jobs WHERE status IN ('queued','running','interrupted')"
            ).fetchall()
        restarted = 0
        for row in rows:
            if _alive(row["pid"]):
                continue
            with self.store.connect() as db:
                cursor = db.execute("""UPDATE jobs SET status='queued',pid=NULL,updated_at=?
                    WHERE id=? AND status IN ('queued','running','interrupted')""",
                    (_utc(), row["id"]))
            if cursor.rowcount:
                self._spawn(row["id"])
                restarted += 1
        return restarted


def _docker_prefix(jobdir: Path, artifact: Path, wordlist: Path, image: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--network", "none",
        "--cpus", "2", "--memory", "2g",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp", "--env", "HOME=/job",
        "--mount", f"type=bind,src={jobdir},dst=/job",
        "--mount", f"type=bind,src={artifact},dst=/job/hash,readonly",
        "--mount", f"type=bind,src={wordlist},dst=/job/wordlist,readonly",
        image,
    ]


def _command(row: Any, wordlist: Path, jobdir: Path,
             artifact: Path, runner: str, image: str, john: str, hashcat: str) -> list[str]:
    john_format, hashcat_mode = FORMATS[row["hash_type"]]
    session = "ctf_" + row["id"]
    if runner == "docker":
        prefix = _docker_prefix(jobdir, artifact, wordlist, image)
        directory, hash_path, word_path = "/job", "/job/hash", "/job/wordlist"
    else:
        prefix, directory = [], str(jobdir)
        hash_path, word_path = str(artifact), str(wordlist)
    if row["engine"] == "john":
        restore = jobdir / (session + ".rec")
        if (jobdir / "attempted").exists() and restore.exists():
            return prefix + [john, f"--restore={directory}/{session}",
                             f"--pot={directory}/john.pot"]
        return prefix + [john, f"--format={john_format}",
                         f"--pot={directory}/john.pot", f"--session={directory}/{session}",
                         f"--wordlist={word_path}", hash_path]
    restore = jobdir / "hashcat.restore"
    if (jobdir / "attempted").exists() and restore.exists():
        return prefix + [hashcat, "--restore", "--session", session,
                         "--restore-file-path", f"{directory}/hashcat.restore"]
    return prefix + [hashcat, "-m", hashcat_mode, "-a", "0", "--session", session,
                     "--potfile-path", f"{directory}/hashcat.pot",
                     "--outfile", f"{directory}/cracked.txt",
                     "--restore-file-path", f"{directory}/hashcat.restore",
                     hash_path,word_path]


def execute(root: str, job_id: str, wordlist: str, runner: str,
            image: str, john: str, hashcat: str) -> None:
    store = CampaignStore(root)
    jobdir = store.jobs_dir / job_id
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] not in {"queued","interrupted"}:
            db.execute("ROLLBACK")
            return
        db.execute("UPDATE jobs SET status='running',pid=?,attempts=attempts+1,updated_at=? WHERE id=?",
                   (os.getpid(),_utc(),job_id))
        db.execute("COMMIT")
    try:
        artifact = store.artifact_path(row["artifact_id"])
        argv = _command(row,Path(wordlist),jobdir,artifact,runner,image,john,hashcat)
        (jobdir / "attempted").touch()
        # Suppress stdout and stderr to avoid writing recovered secrets to job logs.
        result = subprocess.run(argv,cwd=jobdir,stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,check=False)
        password = None
        if row["engine"] == "john":
            base = _docker_prefix(jobdir,artifact,Path(wordlist),image) if runner == "docker" else []
            directory = "/job" if runner == "docker" else str(jobdir)
            hash_path = "/job/hash" if runner == "docker" else str(artifact)
            args = base + [john,f"--format={FORMATS[row['hash_type']][0]}",
                           f"--pot={directory}/john.pot","--show",hash_path]
            shown = subprocess.run(args,capture_output=True,text=True,timeout=40,check=False)
            for line in shown.stdout.splitlines():
                if ":" in line and not re.match(r"^\d+ password",line):
                    password = line.split(":",1)[1].strip()
                    break
        else:
            output = jobdir / "cracked.txt"
            if output.exists():
                for line in output.read_text(encoding="utf-8",errors="replace").splitlines():
                    if ":" in line:
                        password = line.rsplit(":",1)[-1]
                        break
        if password:
            domain, separator, username = row["principal"].rpartition("\\")
            if not separator:
                domain, username = "", row["principal"]
            store.publish({"credentials": [{
                "username": username, "domain": domain, "password": password,
                "verified": True,
            }]}, "offline-job:" + job_id)
        state = "completed" if password else ("exhausted" if result.returncode == 0 else "failed")
        error = "" if result.returncode == 0 else f"cracker exit code {result.returncode}"
    except Exception as exc:
        state, error = "failed", type(exc).__name__
    with store.connect() as db:
        db.execute("UPDATE jobs SET status=?,error=?,pid=NULL,updated_at=? WHERE id=?",
                   (state,error,_utc(),job_id))
        db.execute("INSERT INTO events(kind,summary,challenge,created_at) VALUES(?,?,?,?)",
                   ("job",f"Offline job {job_id} {state}; check status","offline-job",_utc()))


if __name__ == "__main__":
    if len(sys.argv) != 9 or sys.argv[1] != "--execute":
        raise SystemExit("usage: python -m backend.campaign_jobs --execute ROOT JOB WORDLIST RUNNER IMAGE JOHN HASHCAT")
    execute(*sys.argv[2:])
