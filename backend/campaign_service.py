"""Host-side campaign context bridge for all swarms and Codex agents."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.campaign_jobs import BackgroundJobs
from backend.campaign_store import CampaignStore, campaign_identity

log = logging.getLogger(__name__)


class CampaignService:
    def __init__(self, settings: Any):
        self.campaign_id = campaign_identity(
            settings.ctfd_url, getattr(settings, "allowed_categories", []),
            getattr(settings, "campaign_id", ""),
        )
        base = getattr(settings, "campaign_data_dir", "") or "~/.local/share/ctfd-fcker/campaigns"
        self.store = CampaignStore(Path(base).expanduser() / self.campaign_id)
        self.jobs = BackgroundJobs(
            self.store,
            wordlist=getattr(settings, "campaign_wordlist", ""),
            runner=getattr(settings, "campaign_job_runner", "docker"),
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
        )
        self._last_event = self.store.latest_event_id()
        self._buses: list[Any] = []

    def register_bus(self, bus: Any) -> None:
        if not any(item is bus for item in self._buses):
            self._buses.append(bus)

    async def poll_events(self) -> int:
        """Propagate host/worker events to every active challenge bus, without secrets."""
        total = 0
        while True:
            batch = self.store.events(after=self._last_event, limit=100)
            if not batch:
                break
            for event in batch:
                self._last_event = event["id"]
                total += 1
                notice = (f"Campaign {self.campaign_id}, {event['challenge']}: "
                          f"{event['kind']}: {event['summary']}")
                for bus in list(self._buses):
                    try:
                        await bus.broadcast(notice, source="campaign")
                    except Exception:
                        log.warning("Campaign event broadcast failed", exc_info=True)
            if len(batch) < 100:
                break
        return total

    async def publish(self, payload: dict[str, Any], challenge: str) -> str:
        updated = self.store.publish(payload, challenge)
        if any(updated.values()):
            await self.poll_events()
        return json.dumps({"saved": updated, "campaign": self.campaign_id})

    def search(self, query: str = "", limit: int = 30) -> str:
        return json.dumps(self.store.search(query, limit), ensure_ascii=False)

    def credential(self, ref: str) -> str:
        # Only the explicit secret-retrieval tool returns a decrypted value.
        return json.dumps(self.store.credential(ref), ensure_ascii=False)

    async def stage_hash(self, sandbox: Any, path: str, challenge: str) -> str:
        if not (path.startswith("/challenge/workspace/") or path.startswith("/challenge/distfiles/")):
            raise ValueError("stage hashes only from challenge workspace or distfiles")
        if ".." in Path(path).parts:
            raise ValueError("invalid hash file path")
        content = await sandbox.read_file_bytes(path)
        return self.store.stage_artifact(content, Path(path).name, challenge)

    def job_submit(self, artifact_id: str, principal: str, hash_type: str,
                   engine: str = "john") -> str:
        return json.dumps(self.jobs.submit(artifact_id=artifact_id,principal=principal,
                                           hash_type=hash_type,engine=engine))

    def job_status(self, job_id: str) -> str:
        return json.dumps(self.jobs.get(job_id))

    def resume_incomplete(self) -> int:
        try:
            return self.jobs.resume_incomplete()
        except Exception:
            log.warning("Could not resume offline campaign jobs", exc_info=True)
            return 0
