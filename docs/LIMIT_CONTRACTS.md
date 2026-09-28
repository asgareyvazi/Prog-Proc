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
| `SearchResponse.truncated` | **two** distinct facts OR'd together: the backend's candidate discovery hit `RETRIEVAL_CAP` (`retrieval_truncated`), or the surviving hits exceeded `MAX_CANDIDATES` and were cut (`scoring_truncated`) | which of the two happened. The one flag cannot say whether the *universe* was bounded or the *result set* was |
| `SearchResponse.broadened` | the strict reading was actually tried and abandoned for any-of-terms (`index.py`, set only inside the fallback branch) | that the reading is `"any"`. It used to be `mode == "any"`, so an explicitly broadened request reported a strict query it never ran |
| `EvidenceBundle.discovery_capped` | an uncapped request's discovery stopped before the whole population — proven by a look-ahead row, not inferred from a length | that the caller's own `limit` cut anything |
| `TopicCoverage.discovery_capped` | the same statement, per topic, carried into the package and its identity | that another topic was capped |
| `DomainReview.truncated` | some bounded read reached the bound **applied to its own query**, or a result list was cut | that a child batch was large; a batch fetched at `limit * parents` legitimately exceeds `limit` |

Both halves of `SearchResponse.truncated` are now **query-specific** on every backend. Candidate
discovery is term-aware on the FTS *and* the non-FTS scan path, so the bound is applied to rows
that match the query rather than to the raw table; the old scan applied `LIMIT RETRIEVAL_CAP` to
`search_chunk` before any term matching, which made `truncated` a corpus-size statement *and*
silently dropped a genuine match whose `chunk_id` sorted past the bound. The one remaining
corpus-size fallback is the term-less (phrase-only) query, where there is no vocabulary to
predicate on and the raw bounded scan is the only option.

What `truncated` still cannot do is say **which** bound was reached. The two facts are
distinguishable in principle - `candidates` above `MAX_CANDIDATES` implies the result-set cut,
`candidates == RETRIEVAL_CAP` implies the discovery bound - but that is an inference the caller has
to make, not a field. Splitting it into explicit `candidate_capped` / `results_capped` metadata is
the remaining open work on master ledger row 16.

Discovery is term-aware but **not** scope-aware: filters are applied after ranking, in `_score`.
So an in-scope row whose identity sorts past `RETRIEVAL_CAP` is genuinely not examined, and the
scoped answer can come back empty while a matching row exists. `truncated` is set in that case,
which is what keeps it distinguishable from a proven absence - pinned by
`TestScopeIsFilteredAfterDiscovery`.
