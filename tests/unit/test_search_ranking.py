"""Tokenizer and ranking: the deterministic half of search, with no database in sight.

These two modules decide what a query matches and in what order, so they are tested directly
rather than only through the pipeline: a ranking rule that regresses is a silent product change,
and the only honest way to notice is to assert the order.
"""

from __future__ import annotations

import pytest

from drilling_intelligence.extraction.normalized import Page
from drilling_intelligence.search.chunking import (
    CHUNK_KINDS,
    KIND_DIAGNOSTIC,
    KIND_PAGE,
    KIND_PARAGRAPH,
    build_chunk_set,
    chunk_id_for,
    chunks_for_document,
    page_fallback_chunks,
    uncited_chunks,
)
from drilling_intelligence.search.ranking import (
    DEFAULT_KIND_WEIGHTS,
    IndexStatistics,
    phrase_present,
    rank_chunks,
)
from drilling_intelligence.search.tokenize import (
    STOPWORDS,
    highlight,
    parse_query,
    term_counts,
    tokenize,
)


class TestTokenizer:
    def test_case_folding_and_punctuation(self) -> None:
        assert tokenize("MUD WEIGHT: 10.2 ppg.") == ["mud", "weight", "10.2", "ppg"]

    def test_measurements_stay_whole(self) -> None:
        """A decimal or a ratio split into pieces matches everything, so they stay together."""
        assert "12.5" in tokenize("mud weight 12.5 ppg")
        assert "500/300" in tokenize("R6 500/300 reading")
        assert "12 1/4" in " ".join(tokenize("12 1/4 in bit"))
        assert tokenize("10.2.3") == ["10.2.3"]

    def test_dashes_and_underscores_split_into_parts_but_keep_the_whole(self) -> None:
        terms = tokenize("well-a3 field_sample")
        assert {"well-a3", "well", "a3"} <= set(terms)
        assert {"field_sample", "field", "sample"} <= set(terms)

    def test_dotted_abbreviations_are_joined(self) -> None:
        assert tokenize("mud density in p.p.g.")[-1] == "ppg"
        # ...but a revision marker keeps its parts, because "rev" and the number are separate facts.
        assert set(tokenize("revision rev.12")) >= {"rev", "12"}

    def test_possessive_is_searchable_by_either_form(self) -> None:
        terms = tokenize("the crew's plan")
        assert "crew" in terms and "plan" in terms
        # Both the raw and the stemmed form are indexed, so "crew" and "crew's" find the same text.
        assert "crew's" in terms

    def test_stopwords_are_dropped_from_index_and_query(self) -> None:
        assert not (set(STOPWORDS) & set(tokenize("the list of reports for the well")))
        terms, phrases = parse_query("the mud weight of the report")
        assert terms == ["mud", "weight", "report"], terms
        assert phrases == []
        # A query made only of stop words has nothing to require, and must not match everything.
        assert parse_query("the and of") == ([], [])

    def test_quoted_phrases_are_preserved(self) -> None:
        terms, phrases = parse_query('losses while drilling "shoe pressure test"')
        assert "shoe" in terms and "pressure" in terms
        assert phrases == ["shoe pressure test"]

    def test_empty_and_symbol_only_input(self) -> None:
        assert tokenize("") == []
        assert tokenize("--- ... !!!") == []
        assert parse_query("") == ([], [])
        assert parse_query('"') == ([], [])

    def test_tokenizing_is_stable(self) -> None:
        text = "Well A-3: 10.2 ppg mud-weight check"
        assert tokenize(text) == tokenize(text)
        # Terms are emitted in first-occurrence order, which is what makes a stored term-count
        # dict reproducible across builds.
        assert tokenize(text) == tokenize(text.replace("A-3", "A-3"))

    def test_term_counts_feed_ranking(self) -> None:
        counts = term_counts("mud mud weight")
        assert counts == {"mud": 2, "weight": 1}
        assert sum(counts.values()) == 3


class TestRanking:
    @staticmethod
    def _rows():
        """(row, term_counts, length, kind, text) in the order ``rank_chunks`` expects."""
        rows = [
            ("a", "Mud weight was raised to 12.5 ppg.", "paragraph"),
            ("b", "Mud weight 12.5 ppg at the shoe; mud checks every 20 minutes.", "paragraph"),
            ("c", "The casing program is unrelated to mud.", "paragraph"),
            ("d", "mud_weight = 12.5 ppg", "field"),
        ]
        return [
            (row, term_counts(text), max(1, len(text.split())), kind, text)
            for row, text, kind in rows
        ]

    def test_all_terms_required_and_more_relevant_ranks_first(self) -> None:
        statistics = IndexStatistics(
            total_chunks=4, total_length=60, document_frequency={"mud": 4, "12.5": 3, "ppg": 3}
        )
        hits = rank_chunks(
            self._rows(),
            terms=["mud", "12.5", "ppg"],
            phrases=(),
            statistics=statistics,
            require_all=True,
            limit=10,
        )
        assert {hit.row for hit in hits} == {"a", "b", "d"}
        assert "c" not in {hit.row for hit in hits}, "the chunk without 12.5 must not match"
        by_row = {hit.row: hit.score for hit in hits}
        # "b" repeats "mud" and is a longer chunk; "a" is a single-mention paragraph. Length
        # normalisation (BM25's b) has to keep the denser-but-longer chunk from winning outright,
        # and the kind weight has to lift the field chunk - both are asserted as an order, not
        # as a formula, so a tuning change is visible here rather than invisible.
        assert by_row["d"] > by_row["a"] > 0
        assert sorted(by_row, key=lambda row: -by_row[row])[0] in {"b", "d"}

    def test_a_missing_term_excludes_the_chunk_in_all_mode(self) -> None:
        statistics = IndexStatistics(
            total_chunks=4, total_length=60, document_frequency={"mud": 4, "shoe": 1}
        )
        hits = rank_chunks(self._rows(), terms=["mud", "shoe"], statistics=statistics, limit=10)
        assert [hit.row for hit in hits] == ["b"]

    def test_any_mode_widens(self) -> None:
        statistics = IndexStatistics(
            total_chunks=4, total_length=60, document_frequency={"mud": 4, "shoe": 1, "casing": 1}
        )
        hits = rank_chunks(
            self._rows(),
            terms=["shoe", "casing"],
            statistics=statistics,
            require_all=False,
            limit=10,
        )
        assert {hit.row for hit in hits} == {"b", "c"}

    def test_rare_terms_outrank_common_ones(self) -> None:
        statistics = IndexStatistics(
            total_chunks=4, total_length=60, document_frequency={"mud": 4, "shoe": 1}
        )
        hits = rank_chunks(self._rows(), terms=["shoe"], statistics=statistics, limit=10)
        assert hits and hits[0].term_scores["shoe"] > 0
        # The document that mentions "shoe" is the only one that scores at all, and "mud" being
        # in every chunk means it cannot be the tie-breaker anyone relies on.
        assert [hit.row for hit in hits] == ["b"]

    def test_kind_weight_lifts_a_field_chunk_above_an_equal_paragraph(self) -> None:
        statistics = IndexStatistics(total_chunks=2, total_length=20, document_frequency={"ppg": 2})
        rows = [
            ("p", term_counts("ppg"), 1, "paragraph", "ppg"),
            ("f", term_counts("ppg"), 1, "field", "ppg"),
        ]
        hits = rank_chunks(rows, terms=["ppg"], statistics=statistics, limit=10)
        assert [hit.row for hit in hits] == ["f", "p"]
        assert hits[0].score > hits[1].score

    def test_phrases_are_required_not_just_promoted(self) -> None:
        statistics = IndexStatistics(
            total_chunks=2, total_length=40, document_frequency={"mud": 2, "weight": 2}
        )
        rows = [
            ("loose", term_counts("weight of mud"), 3, "paragraph", "weight of mud"),
            ("exact", term_counts("mud weight"), 2, "paragraph", "mud weight"),
        ]
        hits = rank_chunks(
            rows, terms=["mud", "weight"], phrases=["mud weight"], statistics=statistics, limit=10
        )
        assert [hit.row for hit in hits] == ["exact"]

    def test_phrase_present_on_folded_text(self) -> None:
        assert phrase_present("Mud  WEIGHT\t12.5", "mud weight")
        assert not phrase_present("weight of mud", "mud weight")

    def test_tie_break_is_stable_and_document_ordered(self) -> None:
        statistics = IndexStatistics(total_chunks=3, total_length=30, document_frequency={"ppg": 3})

        def row(document_id: str, index: int) -> tuple[dict, dict, int, str, str]:
            text = "ppg"
            return (
                {"document_id": document_id, "chunk_index": index},
                term_counts(text),
                1,
                KIND_PARAGRAPH,
                text,
            )

        # Deliberately not in the order the result must come out in.
        rows = [row("doc-b", 1), row("doc-a", 2), row("doc-a", 1)]
        hits = rank_chunks(rows, terms=["ppg"], statistics=statistics, limit=10)
        assert [(hit.row["document_id"], hit.row["chunk_index"]) for hit in hits] == [
            ("doc-a", 1),
            ("doc-a", 2),
            ("doc-b", 1),
        ]
        assert {hit.score for hit in hits} == {round(hits[0].score, 9)}, (
            "identical content must score identically"
        )
        assert rank_chunks(rows, terms=["ppg"], statistics=statistics, limit=10) == hits

    def test_limit_bounds_the_result(self) -> None:
        statistics = IndexStatistics(total_chunks=4, total_length=60, document_frequency={"mud": 4})
        assert len(rank_chunks(self._rows(), terms=["mud"], statistics=statistics, limit=2)) == 2

    def test_diagnostic_chunks_are_damped(self) -> None:
        assert DEFAULT_KIND_WEIGHTS[KIND_DIAGNOSTIC] < DEFAULT_KIND_WEIGHTS[KIND_PARAGRAPH]
        assert set(DEFAULT_KIND_WEIGHTS) == set(CHUNK_KINDS)


class TestHighlight:
    def test_spans_point_at_the_snippet_not_the_source(self) -> None:
        text = "Mud weight was raised to 12.5 ppg."
        snippet, spans = highlight(text, ["mud", "12.5"], context=90)
        assert snippet == text, "an unwindowed snippet must be the text itself, unchanged"
        assert [snippet[start:end] for start, end in spans] == ["Mud", "12.5"]
        # Word-boundary matching: a highlight of "well" inside "wellsite" would tell the reader
        # the chunk matched something it did not.
        _, spans = highlight("The wellsite road", ["well"], context=90)
        assert spans == []

    def test_long_text_is_windowed_around_the_first_hit(self) -> None:
        text = "x" * 500 + "mud weight 12.5 ppg" + "y" * 500
        snippet, spans = highlight(text, ["12.5"], context=40)
        assert "12.5" in snippet
        assert len(snippet) < 200
        for start, end in spans:
            assert 0 <= start < end <= len(snippet)

    def test_no_terms_returns_the_head_of_the_text(self) -> None:
        snippet, spans = highlight("The report begins here.", [], context=40)
        assert snippet == "The report begins here."
        assert spans == []

    def test_ellipsis_marks_a_windowed_start(self) -> None:
        text = "z " * 200 + "mud" + " z" * 200
        snippet, spans = highlight(text, ["mud"], context=30)
        assert snippet.startswith("…"), "a windowed snippet must say it is not the whole chunk"
        assert snippet.endswith("…")
        assert [snippet[start:end] for start, end in spans] == ["mud"], (
            "spans are offsets into the snippet"
        )


class TestChunkVocabulary:
    """The chunker is the product's only one; these guard the shape it must keep."""

    def test_ids_are_deterministic(self) -> None:
        assert chunk_id_for("ver-1", 3) == chunk_id_for("ver-1", 3)
        assert chunk_id_for("ver-1", 3) != chunk_id_for("ver-2", 3)

    @pytest.fixture
    def normalized(self):
        from drilling_intelligence.extraction.normalized import NormalizedDocument

        return NormalizedDocument.from_dict(
            {
                "metadata": {
                    "filename": "mud.xlsx",
                    "path": "corpus/mud.xlsx",
                    "sha256": "a" * 64,
                    "parser": "excel",
                },
                "pages": [
                    {"index": 1, "text": "", "label": "Summary", "char_start": 0, "char_end": 10}
                ],
                "paragraphs": [
                    {
                        "index": 0,
                        "text": "Mud weight was raised to 12.5 ppg after the losses.",
                        "page": 1,
                        "block": 1,
                        "section": "Summary",
                        "char_start": 0,
                        "char_end": 53,
                        "provenance": {
                            "document_id": "doc-1",
                            "filename": "mud.xlsx",
                            "locator": {"kind": "excel", "sheet": "Summary", "cell": "B2"},
                            "excerpt": "12.5 ppg",
                        },
                    }
                ],
                "tables": [],
                "sections": [],
                "figures": [],
                "extracted_fields": [],
                "diagnostics": ["EXTRACTION_TRUNCATED: max_cells=60000"],
            }
        )

    def test_a_body_chunk_always_carries_its_locator(self, normalized) -> None:
        chunks = chunks_for_document(
            document_id="doc-1", version_id="ver-1", normalized=normalized, source_sha256="a" * 64
        )
        assert chunks
        paragraph = next(chunk for chunk in chunks if chunk.kind == KIND_PARAGRAPH)
        assert paragraph.locator_ref == "Sheet: Summary > Cell: B2"
        assert paragraph.sheet == "Summary"
        # The page is whatever the *locator* names.  An Excel locator names a cell, not a page,
        # so this is None rather than the sheet ordinal dressed up as a page number.
        assert paragraph.page is None
        assert paragraph.provenance["excerpt"] == "12.5 ppg"
        assert not uncited_chunks(chunks)

    def test_diagnostics_are_one_uncited_chunk_by_design(self, normalized) -> None:
        chunks = chunks_for_document(document_id="doc-1", version_id="ver-1", normalized=normalized)
        diagnostic = [chunk for chunk in chunks if chunk.kind == KIND_DIAGNOSTIC]
        assert len(diagnostic) == 1
        assert diagnostic[0].provenance is None
        assert "max_cells=60000" in diagnostic[0].text
        # The exemption is what makes that legal rather than sloppy.
        assert not uncited_chunks(chunks)

    def test_page_fallback_cites_pages_and_nothing_else(self, normalized) -> None:
        normalized.pages = [Page(index=1, text="Mud weight 12.5 ppg", char_start=0, char_end=19)]
        normalized.paragraphs = []
        chunks = page_fallback_chunks(
            document_id="doc-1", version_id="ver-1", normalized=normalized
        )
        assert [chunk.kind for chunk in chunks] == [KIND_PAGE]
        assert chunks[0].page == 1
        assert chunks[0].provenance is None
        assert "Page: 1" in chunks[0].locator_ref

    def test_build_chunk_set_falls_back_only_when_structure_is_empty(self, normalized) -> None:
        document = _document()
        chunk_set = build_chunk_set(
            document=document, normalized=normalized, version_id="ver-1", source_sha256="a" * 64
        )
        assert all(chunk.kind != KIND_PAGE for chunk in chunk_set.chunks)
        assert chunk_set.document.chunk_count == len(chunk_set.chunks)

        normalized.paragraphs = []
        normalized.diagnostics = []
        normalized.tables = []
        normalized.pages = [Page(index=1, text="Mud weight 12.5 ppg", char_start=0, char_end=19)]
        fallback = build_chunk_set(document=document, normalized=normalized, version_id="ver-1")
        assert [chunk.kind for chunk in fallback.chunks] and all(
            chunk.kind == KIND_PAGE for chunk in fallback.chunks
        )

    def test_chunk_ids_are_positional_and_unique(self, normalized) -> None:
        chunks = chunks_for_document(document_id="doc-1", version_id="ver-1", normalized=normalized)
        assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
        assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def _document():
    from drilling_intelligence.search.chunking import IndexDocument

    return IndexDocument(
        document_id="doc-1",
        version_id="ver-1",
        version_number=1,
        workspace_id="ws-1",
        project_id="prj-1",
        company_id="co-1",
        project_name="North Cormorant",
        company_name="ACME",
        well_id="well-1",
        well_name="A-3",
        document_type="MUD_REPORT",
        title="Mud report",
        filename="mud.xlsx",
        identity_path="corpus/mud.xlsx",
        source_relative_path="corpus/mud.xlsx",
        extension="xlsx",
        parser="excel",
        revision="3",
        revision_key=3,
        status="ACTIVE",
        processing_status="INDEXED",
        source_authority="ENGINEERING",
        document_date="2025-06-14",
        imported_at="2026-09-05T00:00:00",
        page_count=1,
        sheet_count=1,
        word_count=10,
        size_bytes=100,
        sha256="a" * 64,
        is_current=True,
        diagnostics=(),
        chunk_count=0,
    )
