# Limit contracts, layer by layer

Every limit in the platform, what zero means at that layer, what bounds the answer behind the
caller's back, and which signal says a bound was reached.

This exists because the layers genuinely do **not** share one convention, and one of them used to
claim otherwise. A caller who assumes "zero means everything" everywhere will be wrong at exactly
one layer — search — and wrong silently.

## 1. The five distinct kinds of bound

Conflating these is how a complete answer gets reported as truncated, and how a truncated answer
gets reported as complete.

| kind | what it limits | who sets it |
| --- | --- | --- |
| **caller result limit** | how many rows the caller asked for | the request |
| **safety limit** | how many rows a read may touch so a corrupt database cannot make a screen allocate without bound | the layer |
| **candidate discovery limit** | how many candidates the ranker examines | the layer |
| **read batch size** | how many rows one repository call fetches | the layer |
| **backend accelerator cap** | how many chunks the index will hand over | the search index |

## 2. What `limit = 0` means per layer

| layer | default | `0` means | internal bound | signal |
| --- | ---: | --- | --- | --- |
| `SearchService.search` | `20` (`default_limit`) | **"use the default"** — `int(limit or self.default_limit)`, `search/service.py:351` | `MAX_CANDIDATES = 4000` (`search/index.py:91`), `RETRIEVAL_CAP = 16000` (`:100`) | `SearchResponse.truncated`, `.candidates` |
| `RetrievalService` | `20` (`RetrievalRequest.limit`) | **no caller cap** (`retrieval/contract.py:73`) | `_DISCOVERY_CAP = MAX_CANDIDATES` (`retrieval/service.py:109`) | `EvidenceBundle.discovery_capped` |
| `EvidenceQueryService` | `0` (`EvidenceQuery.limit`) | **no cap** (`evidence/contract.py:76`) | inherited from retrieval | `TopicCoverage.discovery_capped` |
| `DomainReviewService` | `0` (`DomainReviewRequest.limit`) | **no application-level cap** (`review/contract.py:32`) | `_SAFE_LIMIT = 10_000` (`review/service.py:95`) | `DomainReview.truncated` |
| `FieldIntelligence` | `200` (`field.py:784`), `10` (`:935`) | **no cap** — `.limit(limit if limit and limit > 0 else None)` (`:826`) | none of its own | none; counts are named in the payload (`:633`) |

**Search is the outlier, deliberately.** It is a search box: an unset limit should not become an
unbounded read. Retrieval never forwards a zero to it — `_discover` substitutes its own discovery
bound — so the difference does not leak through the evidence path. It only bites a caller driving
`SearchService.search` (or `drillintel search`) directly.

`evidence/contract.py` used to state that zero-means-no-cap was "the convention the search and
intelligence layers use". The intelligence half was true; the search half was **false**, and it was
false in the direction that misleads. Corrected in V5.4 and pinned by
`tests/integration/test_limit_contracts.py`.

## 3. CLI `--limit`

| command | default | help text | matches its service? |
| --- | ---: | --- | --- |
| `ingest` | `0` | "process at most N files (0 = no limit)" | yes |
| `search` | `20` | "maximum results (default 20)" | **silent** — `--limit 0` yields 20, not everything |
| `evidence query` | `0` | "per topic, at most N (0 = no limit)" | yes |
| `knowledge conflicts` | `50` | "maximum conflicts to list" | yes |
| `knowledge facts` | `50` | "maximum facts to list" | yes |
| `timeline` | `0` | "at most N entries (0 = no limit)" | yes |
| `records` | `0` | "at most N returned records (0 = no application-level cap)" | yes |
| generic `list` actions | `50` | "at most N rows" | yes |

The CLI never re-implements limit policy; it forwards to the service beneath it. The `search` row is
the one place where the help text does not say what the service actually does with a zero.

## 4. Truncation signals: what each one really claims

| flag | true means | does **not** mean |
| --- | --- | --- |
| `SearchResponse.truncated` | the candidate universe exceeded `MAX_CANDIDATES`; ranking ran over a bounded set | that this query had more *relevant* rows than were examined |
| `EvidenceBundle.discovery_capped` | an uncapped request's discovery stopped before the whole population — proven by a look-ahead row, not inferred from a length | that the caller's own `limit` cut anything |
| `TopicCoverage.discovery_capped` | the same statement, per topic, carried into the package and its identity | that another topic was capped |
| `DomainReview.truncated` | some bounded read reached the bound **applied to its own query**, or a result list was cut | that a child batch was large; a batch fetched at `limit * parents` legitimately exceeds `limit` |

`SearchResponse.truncated` is a corpus-size statement, not a per-query one, and remains the weakest
of the four. Making it query-honest is still open work (master ledger row 16).
