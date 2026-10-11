from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from backend.category_filter import category_allowed, filter_challenges, normalize_categories
from backend.filtered_ctfd import FilteredCTFdClient


def test_category_helpers_are_case_insensitive_and_preserve_order() -> None:
    assert normalize_categories([" Windows ", "web", "WINDOWS", "Linux"]) == [
        "Windows",
        "web",
        "Linux",
    ]
    assert category_allowed(["Windows", "Web"], "windows")
    assert category_allowed([], "Forensics")
    assert not category_allowed(["Windows", "Web"], "Forensics")


def test_filter_challenges_whitelists_only_selected_categories() -> None:
    challenges = [
        {"name": "AD", "category": "Windows"},
        {"name": "Portal", "category": "Web"},
        {"name": "Memory", "category": "Forensics"},
    ]
    assert [c["name"] for c in filter_challenges(["windows", "WEB"], challenges)] == [
        "AD",
        "Portal",
    ]
    assert filter_challenges([], challenges) == challenges


@pytest.mark.asyncio
async def test_filtered_ctfd_hides_disallowed_categories(monkeypatch) -> None:
    async def fake_stubs(self):
        return [
            {"id": 1, "name": "AD-01", "category": "Windows", "type": "standard"},
            {"id": 2, "name": "WEB-01", "category": "Web", "type": "standard"},
            {"id": 3, "name": "MEM-01", "category": "Forensics", "type": "standard"},
        ]

    async def fake_detail(self, challenge_id: int):
        by_id = {
            1: {"id": 1, "name": "AD-01", "category": "Windows"},
            2: {"id": 2, "name": "WEB-01", "category": "Web"},
            3: {"id": 3, "name": "MEM-01", "category": "Forensics"},
        }
        return by_id[challenge_id]

    from backend.ctfd import CTFdClient

    monkeypatch.setattr(CTFdClient, "fetch_challenge_stubs", fake_stubs)
    monkeypatch.setattr(CTFdClient, "fetch_challenge_detail", fake_detail)

    client = FilteredCTFdClient(allowed_categories=["Windows", "Web"])
    stubs = await client.fetch_challenge_stubs()
    details = await client.fetch_all_challenges()

    assert [item["name"] for item in stubs] == ["AD-01", "WEB-01"]
    assert [item["name"] for item in details] == ["AD-01", "WEB-01"]


def test_cli_accepts_repeatable_and_csv_category_filters(monkeypatch) -> None:
    import backend.cli as cli

    captured = {}

    async def fake_run_coordinator(
        settings,
        model_specs,
        challenges_dir,
        no_submit,
        coordinator_model,
        coordinator_backend,
        max_challenges,
        msg_port=0,
    ):
        captured["categories"] = list(settings.allowed_categories)

    monkeypatch.setattr(cli, "_run_coordinator", fake_run_coordinator)
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: __import__("asyncio").new_event_loop().run_until_complete(coro))

    result = CliRunner().invoke(
        cli.main,
        [
            "--category",
            "Windows",
            "--category",
            "Web",
            "--categories",
            "Linux,windows",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["categories"] == ["Windows", "Web", "Linux"]
