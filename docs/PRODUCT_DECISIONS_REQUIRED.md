# Product decisions required before the next capabilities

**Status:** decision register, not an implementation plan
**As of:** 2026-10-24 (V8.17 reconciliation; the decisions themselves are unchanged)
**Companion:** [`PRODUCT_CAPABILITY.md`](PRODUCT_CAPABILITY.md)

These items are not defects. The current code fails closed. None of them is
implemented by a setting name, a table, or a protocol.

**The count, and where it comes from.** This register holds **five open
decisions** and one list of capabilities that stay excluded. The five are the
five milestones `PRODUCT_CAPABILITY.md` schedules after the current one (M2
to M6); the excluded list is the boundary, not a decision. A report that
counts "six product decisions" is counting the excluded list as a decision, and
that count was corrected in the end-to-end report on 2026-10-24.

Each decision below carries the same six parts, so that it can be answered
without reading the code: the precise question, the current behavior, the safe
default, what is blocked, the trade-offs, and the acceptance evidence that has
to exist before any code is written. The **Source** line points at the ADR and
the code that holds the current behavior, and at the test that proves it.

---

## 1. OpenAI-compatible adapter

**Missing decision.** Which HTTP API, which secret slot, which auth header, and
whether `drillintel ai status` may leave the machine.

**Current behavior.** `provider = openai-compatible` is a supported *name* and
an unimplemented *adapter*: the status reports `UNSUPPORTED` with the reason
"openai-compatible has no adapter in this build; no request is sent". No
socket is opened and the API key is not read.

**Safe default.** Keep the name unsupported and send no request, which is the
current behavior. Or document a local OpenAI-compatible base and still
require an explicit probe flag.

**What is blocked.** Any cloud call, any credential handling for a cloud
provider, and any claim that `ai status` proves reachability of a cloud API.

**Trade-off.** A cloud default would make doctor or status a network call and
a credential boundary. A local-only adapter would not satisfy an operator who
expects `https://api.openai.com/v1`.

**Acceptance evidence before code.** A written request and response shape,
timeout and retry rules, a rule that the key is never logged, and tests that
an absent key sends no request.

**Source.** ADR-51 (the Ollama adapter that does not write records);
`src/drilling_intelligence/integrations/ai/contract.py` (the status
contract); `tests/unit/test_ai_provider.py` (the unsupported state and the
"no request" rule).

## 2. Embedding sidecar

**Missing decision.** Vector dimension, distance metric, when a sidecar is
stale, and whether retrieval may change when the sidecar is absent.

**Current behavior.** `UnavailableVectorStore` is the only vector store in the
build. It reports that no store exists; retrieval is keyword and structured
SQLite search, and a rejected or accepted embedding does not insert a domain
row.

**Safe default.** Keep keyword and structured search as the only retrieval
path. Keep `embed()` as a non-authoritative call that stores nothing.

**What is blocked.** A second ranking path, a vector column on a domain table,
and any answer that changes when a sidecar is absent.

**Trade-off.** Storing vectors without a retrieval contract invites a second
ranking path. Not storing them means semantic search stays planned.

**Acceptance evidence before code.** A rebuild can drop the sidecar. Retrieval
answers are identical when it is absent. No domain table gains a vector column
without a migration test.

**Source.** ADR-0003 (SQLite is the system of record; the index is
disposable) and ADR-0005 (own the AI seams; talk to Ollama over HTTP); `src/drilling_intelligence/integrations/ai/contract.py`
(`UnavailableVectorStore`); `tests/unit/test_ai_provider.py`.

## 3. Skills runtime

**Missing decision.** Who may install a skill, what it is allowed to read, and
whether it may propose a record that a human must still accept.

**Current behavior.** The `skill` and `skill_version` tables exist and are
declared, and no service reads them. There is no executor, no tool surface,
and no promotion path for a skill.

**Safe default.** Leave the `skill` tables unused. Do not expose them as
tools.

**What is blocked.** A skill becoming a calculation, a promoted row, or a
knowledge fact without a written trust boundary.

**Trade-off.** An executor without a trust boundary can bypass the promotion
and approval rules. A read-only catalog is not a runtime.

**Acceptance evidence before code.** An operator contract that a skill cannot
become a calculation or a promoted row unless that contract says so, with a
test that an unapproved skill writes nothing.

**Source.** ADR-0010 (promotion is a separate, idempotent, self-reporting act)
and ADR-0007 (the document core's invariants live in the schema, the repository
and the checker); `src/drilling_intelligence/database/models.py`
(the two tables);
`tests/unit/test_document_invariants.py` (no service reads them).

## 4. Additional calculation method

**Missing decision.** Method id, inputs, units, outputs, and the cases that
must be refused.

**Current behavior.** NPT roll-up V1 is the only executable method. An unknown
method is not executed, and the review never recalculates: it classifies the
stored result and reports its dependency state.

**Safe default.** Keep NPT roll-up V1 as the only executable method.

**What is blocked.** A generic calculator, and any engine that would invent
engineering meaning.

**Trade-off.** A generic calculator would invent engineering meaning. One
specified method can be tested at its boundaries.

**Acceptance evidence before code.** The method text exists before the
implementation, including invalid inputs and unit mismatches.

**Source.** ADR-0012 (the engineering record is a table that can say where its
number came from) and ADR-0016 (a calculation input names the durable thing it
consumed, or admits it cannot); `src/drilling_intelligence/engineering/service.py` (the single
executable method);
`tests/integration/test_npt_rollup_production_path.py`.

## 5. Additional document-class writer

**Missing decision.** The source shape for one knowledge-supported or
extract-only class: which columns are required, which ambiguities are refused,
and which provenance locators are mandatory.

**Current behavior.** A class without a handler does not gain a domain writer.
Extraction may retain the artefact; promotion does not invent a row. The
coverage matrix says which class is at which level.

**Safe default.** Keep the class in the coverage matrix at its current
level. Extraction may retain the artefact. Promotion does not invent a row.

**What is blocked.** A promoted row whose columns the source did not state, and
a coverage-matrix level that moves without a forensic corpus case.

**Trade-off.** Promoting from prose without a shape contract writes authority
the source did not state.

**Acceptance evidence before code.** A forensic corpus case for that class,
including a refusal, before the coverage matrix changes.

**Source.** ADR-0008 (knowledge is derived facts that cite their source) and
ADR-0015 (a citation is verified by re-reading the file, not by trusting the
row);
`docs/DOCUMENT_DOMAIN_COVERAGE.md` (the matrix);
`tests/golden_corpus/` (the corpus a new class must be proven against).

---

## Still excluded

Risk scores, currency conversion, AFE rules, plan-versus-actual dashboards,
and inferred survey geometry stay excluded. Their absence is a product
boundary, not a missing patch.

Authentication, roles and multi-user server behavior stay excluded as well:
the platform is local-first and single-user by design (ADR-0001, the founding
decision, and the README's boundary statement), and adding any of them would need the same
kind of owner decision the five above need. They are listed here rather than
as a sixth decision, because there is nothing to decide *yet*: a decision
would be needed to implement them, and the boundary is what is being recorded.

## How to answer one of these

The register does not ask the owner anything. When a decision is taken, the
answer belongs in this file as a new dated section, with the ADR that records
it, and the fail-closed behavior it replaces keeps working until the code
that implements it lands with its own acceptance evidence.
