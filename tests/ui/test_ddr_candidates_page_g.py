"""Phase G: full UI journey for the DDR Candidates page."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _open_window(controller, workspace):
    from drilling_intelligence.ui.main_window import MainWindow

    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    return MainWindow(controller)


def _select_navigation_page(window, label: str) -> None:
    matches = [
        row
        for row in range(window.navigation.count())
        if window.navigation.item(row).text() == label
    ]
    assert len(matches) == 1
    window.navigation.setCurrentRow(matches[0])


def test_ddr_candidates_page_full_journey(qt_app, workspace, tmp_path) -> None:
    from tests.integration.test_ddr_candidates import _import_one

    from drilling_intelligence.database.models import DdrDisposition
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    text = "Daily Drilling Report SYNTHETIC. Report No. UI-1. Well UIWELL. Date 2026-11-24. Depth from 10 m to 20 m."
    version_id = _import_one(workspace, tmp_path, text)
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="UIWELL")
        well_id = well.id

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "DDR Candidates")
        page = window.ddr_candidates_page
        page.version_input.setText(version_id)
        page._load_candidates()
        assert page.table.rowCount() >= 3
        fields = {page.table.item(r, 0).text() for r in range(page.table.rowCount())}
        assert "report_number" in fields
        # dispose the report_number candidate as ACCEPTED through the UI
        row = next(
            r
            for r in range(page.table.rowCount())
            if page.table.item(r, 0).text() == "report_number"
        )
        page.table.selectRow(row)
        page._dispose("ACCEPTED")
        statuses = {
            page.table.item(r, 0).text(): page.table.item(r, 5).text()
            for r in range(page.table.rowCount())
        }
        assert statuses["report_number"] == "ACCEPTED"
        # authorize through the UI
        idx = page.well_combo.findData(well_id)
        assert idx >= 0
        page.well_combo.setCurrentIndex(idx)
        page._authorize()
        assert "CANDIDATE report UI-1" in page.status_label.text()
        # append-only table has the row
        with workspace.database.read_only() as session:
            from sqlalchemy import func, select

            n = session.execute(
                select(func.count())
                .select_from(DdrDisposition)
                .where(DdrDisposition.document_version_id == version_id)
            ).scalar_one()
            assert n == 1
    finally:
        window.close()


def test_cli_ddr_candidates_surface(qt_app, workspace, tmp_path, capsys) -> None:
    from tests.integration.test_ddr_candidates import _import_one

    from drilling_intelligence.cli.app import main

    text = "Daily Drilling Report SYNTHETIC. Report No. CLI-1. Well CLIWELL. Date 2026-11-24. Depth from 5 m to 6 m."
    version_id = _import_one(workspace, tmp_path, text)
    capsys.readouterr()  # drain import-time chatter before the CLI call under test
    rc = main(
        [
            "--workspace",
            str(workspace.root),
            "ddr-candidates",
            "list",
            "--version-id",
            version_id,
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, f"rc={rc} stderr={captured.err[:400]}"
    # Phase D contract: stdout is EXACTLY one parseable JSON document.
    try:
        payload = json.loads(captured.out)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"stdout is not one JSON document: {exc}; "
            f"stdout={captured.out[:400]!r}; stderr={captured.err[:400]!r}"
        ) from exc
    assert any(c["field"] == "report_number" for c in payload["candidates"])
    # the error path also emits structured JSON on stdout and a nonzero exit code
    rc2 = main(
        [
            "--workspace",
            str(workspace.root),
            "ddr-candidates",
            "list",
            "--version-id",
            "ver-nope",
            "--json",
        ]
    )
    captured2 = capsys.readouterr()
    assert rc2 != 0
    err_payload = json.loads(captured2.out)
    assert err_payload.get("ok") is False
