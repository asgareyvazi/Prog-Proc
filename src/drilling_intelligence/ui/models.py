"""Qt model/view adapters for plain DomainReview values.

The models format DTOs for tables and filtering only.  They never query SQLite, apply lifecycle rules,
or derive engineering values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from ..review import ReviewRecord


def _sort_text(value: Any) -> str:
    return "" if value is None else str(value).casefold()


@dataclass(frozen=True)
class TableColumn:
    key: str
    title: str
    width: int = 120


class MappingTableModel(QAbstractTableModel):
    """A small read-only table for already-plain mapping rows."""

    def __init__(
        self,
        columns: Sequence[TableColumn],
        rows: Sequence[Mapping[str, Any]] = (),
        *,
        parent: Any = None,
        formatter: Callable[[Mapping[str, Any], str], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.columns = tuple(columns)
        self._rows = tuple(rows)
        self._formatter = formatter or self._default_format

    def set_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def row_payload(self, row: int) -> Mapping[str, Any] | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        column = self.columns[index.column()]
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._formatter(row, column.key)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._formatter(row, column.key)
        if role == Qt.ItemDataRole.UserRole:
            return row.get(column.key)
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < len(self.columns):
            return
        key = self.columns[column].key
        reverse = order == Qt.SortOrder.DescendingOrder
        self.beginResetModel()
        self._rows = tuple(
            sorted(
                self._rows,
                key=lambda row: (row.get(key) is None, _sort_text(row.get(key))),
                reverse=reverse,
            )
        )
        self.endResetModel()

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.columns):
            return self.columns[section].title
        if orientation == Qt.Orientation.Vertical:
            return str(section + 1)
        return None

    @staticmethod
    def _default_format(row: Mapping[str, Any], key: str) -> str:
        value = row.get(key)
        if value is None or value == "":
            return "—"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (dict, list, tuple)):
            return str(value)
        return str(value)


class ReviewRecordsModel(QAbstractTableModel):
    """Read-only table model retaining the source ``ReviewRecord`` for selection."""

    columns = (
        TableColumn("record_type", "Type", 150),
        TableColumn("record_id", "Record ID", 220),
        TableColumn("status", "Status", 120),
        TableColumn("current", "State", 90),
        TableColumn("scope", "Scope", 230),
        TableColumn("evidence", "Evidence", 90),
        TableColumn("citation", "Citation", 120),
    )

    def __init__(self, records: Sequence[ReviewRecord] = (), *, parent: Any = None) -> None:
        super().__init__(parent)
        self._records = tuple(records)

    def set_records(self, records: Sequence[ReviewRecord]) -> None:
        self.beginResetModel()
        self._records = tuple(records)
        self.endResetModel()

    def record_at(self, row: int) -> ReviewRecord | None:
        return self._records[row] if 0 <= row < len(self._records) else None

    @property
    def records(self) -> tuple[ReviewRecord, ...]:
        return self._records

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._records)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._records)):
            return None
        record = self._records[index.row()]
        key = self.columns[index.column()].key
        if key == "scope":
            value = (
                ", ".join(
                    f"{name}={record.scope[name]}"
                    for name in ("well_id", "field_id", "project_id", "section_id")
                    if record.scope.get(name)
                )
                or "—"
            )
        elif key == "current":
            value = "CURRENT" if record.current else "HISTORICAL"
        elif key == "evidence":
            value = str(len(record.evidence))
        elif key == "citation":
            value = record.verification.citation_audit
        else:
            value = getattr(record, key, "")
        if role == Qt.ItemDataRole.DisplayRole:
            return str(value) if value not in (None, "") else "—"
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(value) if value not in (None, "") else "—"
        if role == Qt.ItemDataRole.UserRole:
            return record
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < len(self.columns):
            return
        key = self.columns[column].key

        def value(record: ReviewRecord) -> Any:
            if key == "scope":
                return record.scope
            if key == "current":
                return record.current
            if key == "evidence":
                return len(record.evidence)
            if key == "citation":
                return record.verification.citation_audit
            return getattr(record, key, "")

        reverse = order == Qt.SortOrder.DescendingOrder
        self.beginResetModel()
        self._records = tuple(
            sorted(self._records, key=lambda record: _sort_text(value(record)), reverse=reverse)
        )
        self.endResetModel()

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.columns):
            return self.columns[section].title
        if orientation == Qt.Orientation.Vertical:
            return str(section + 1)
        return None


class ReviewRecordFilterProxy(QSortFilterProxyModel):
    """Presentation-only filters over the loaded records."""

    def __init__(self, *, parent: Any = None) -> None:
        super().__init__(parent)
        self._record_type = ""
        self._status = ""
        self._state = ""
        self._text = ""
        self.setDynamicSortFilter(True)

    def set_record_type(self, value: str) -> None:
        self._record_type = value.strip()
        self.invalidateFilter()

    def set_status(self, value: str) -> None:
        self._status = value.strip()
        self.invalidateFilter()

    def set_state(self, value: str) -> None:
        self._state = value.strip()
        self.invalidateFilter()

    def set_text(self, value: str) -> None:
        self._text = value.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        source = self.sourceModel()
        if not isinstance(source, ReviewRecordsModel):
            return True
        record = source.record_at(source_row)
        if record is None:
            return False
        if self._record_type and record.record_type != self._record_type:
            return False
        if self._status and record.status != self._status:
            return False
        if self._state == "CURRENT" and not record.current:
            return False
        if self._state == "HISTORICAL" and record.current:
            return False
        if self._text:
            haystack = " ".join(
                (
                    record.record_type,
                    record.record_id,
                    record.status,
                    record.record_state,
                    str(record.scope),
                    str(record.data),
                )
            ).lower()
            if self._text not in haystack:
                return False
        return True


__all__ = [
    "MappingTableModel",
    "ReviewRecordFilterProxy",
    "ReviewRecordsModel",
    "TableColumn",
]
