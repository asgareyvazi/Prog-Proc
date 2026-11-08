"""UI-level DDR comparison display limits and ambiguity, backed by real SQLite revisions."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect

pytestmark = pytest.mark.ui


def _database_fingerprint(workspace) -> tuple[Any, ...]:
    names = sorted(inspect(workspace.database.engine).get_table_names())
    with workspace.database.engine.connect() as connection:
        return tuple(
            (
                name,
                tuple(
                    tuple(str(value) for value in row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{name}" ORDER BY rowid'  # noqa: S608
                    ).fetchall()
                ),
            )
            for name in names
        )


def _tree_text(window: Any) -> list[str]:
    values: list[str] = []
    pending = [
        window.ddr_compare_tree.topLevelItem(index)
        for index in range(window.ddr_compare_tree.topLevelItemCount())
    ]
    while pending:
        item = pending.pop()
        values.extend((item.text(0), item.text(1)))
        pending.extend(item.child(index) for index in range(item.childCount()))
    return values


def test_ui_shows_ambiguity_and_exact_omitted_count_without_database_writes(
    qt_app, workspace, committed_well_id: str
) -> None:
    from drilling_intelligence.operations.ddr import DdrAuthoringService
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.lifetime import release_top_level_widgets
    from drilling_intelligence.ui.main_window import MainWindow

    service = DdrAuthoringService(workspace.database)
    predecessor = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        summary="Large authored comparison case",
        activities=[
            {"operation_type": "drilling", "label": f"Operator activity {index:03d}"}
            for index in range(1, 261)
        ],
        actor="declared-author",
        reason="Create large display-limit predecessor",
    )
    submitted = service.submit(
        predecessor["report_id"],
        expected_updated_at=predecessor["updated_at"],
        actor="declared-author",
        reason="Submit large display-limit predecessor",
    )
    successor = service.create_revision(
        predecessor["report_id"],
        expected_updated_at=submitted["updated_at"],
        actor="declared-author",
        reason="Create large display-limit successor",
    )
    successor = service.save_draft(
        successor["report_id"],
        expected_updated_at=successor["updated_at"],
        report_date="2026-11-08",
        summary="Large authored comparison case",
        activities=[
            *[
                {"operation_type": "drilling", "label": f"Operator activity {index:03d}"}
                for index in range(1, 261)
            ],
            {"operation_type": "cementing", "label": "One extra exact row"},
        ],
        actor="declared-author",
        reason="Add one authored activity",
    )

    repeated = service.create_draft(
        well_id=committed_well_id,
        report_date="2026-11-08",
        activities=[
            {"operation_type": "drilling", "label": "Repeated authored line"},
            {"operation_type": "drilling", "label": "Repeated authored line"},
        ],
        actor="declared-author",
        reason="Create UI ambiguity case",
    )
    repeated_submitted = service.submit(
        repeated["report_id"],
        expected_updated_at=repeated["updated_at"],
        actor="declared-author",
        reason="Submit UI ambiguity predecessor",
    )
    repeated_successor = service.create_revision(
        repeated["report_id"],
        expected_updated_at=repeated_submitted["updated_at"],
        actor="declared-author",
        reason="Create UI ambiguity successor",
    )

    successor["provenance"] = [
        {
            "action": f"synthetic-provenance-{index:03d}",
            "actor": "declared-author",
            "reason": "display-only provenance fixture",
            "path": "/private/unrelated/source.pdf",
        }
        for index in range(251)
    ]
    successor["activities"][0]["provenance"] = [
        {"action": f"activity-provenance-{index:03d}", "actor": "declared-author"}
        for index in range(101)
    ]

    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    try:
        window._ddr_records_by_id = {
            predecessor["report_id"]: predecessor,
            successor["report_id"]: successor,
        }
        window._selected_ddr_record = successor
        before = _database_fingerprint(controller.workspace)
        window._ddr_compare_selected_revision()
        qt_app.processEvents()
        values = _tree_text(window)
        assert "Activities (authored order; no calculation)" in values
        assert "11 comparison item(s) not expanded" in values
        assert "1 provenance events not expanded" in values
        assert "1 activity events not expanded" in values
        assert "11 activity provenance entries not expanded" in values
        assert "/private/unrelated/source.pdf" not in values
        assert "display-only provenance fixture" in values
        assert "One extra exact row" not in values
        assert _database_fingerprint(controller.workspace) == before

        window._ddr_records_by_id = {
            repeated["report_id"]: repeated,
            repeated_successor["report_id"]: repeated_successor,
        }
        window._selected_ddr_record = repeated_successor
        before_ambiguous_compare = _database_fingerprint(controller.workspace)
        window._ddr_compare_selected_revision()
        qt_app.processEvents()
        ambiguous_values = _tree_text(window)
        assert "comparison=AMBIGUOUS" in ambiguous_values[1]
        assert "Alignment warning" in ambiguous_values
        assert "no pairing was guessed." in " ".join(ambiguous_values)
        assert "ambiguous" in ambiguous_values
        assert _database_fingerprint(controller.workspace) == before_ambiguous_compare
    finally:
        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()
