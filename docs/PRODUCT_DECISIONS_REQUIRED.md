# Product decisions required before the next capabilities

**Status:** decision register, not an implementation plan
**As of:** 2026-10-17
**Companion:** [`PRODUCT_CAPABILITY.md`](PRODUCT_CAPABILITY.md)

These items are not defects. The current code fails closed. None of them is
implemented by a setting name, a table, or a protocol.

## OpenAI-compatible adapter

**Missing decision.** Which HTTP API, which secret slot, which auth header, and
whether `drillintel ai status` may leave the machine.

**Safe alternatives.** Keep the name unsupported and send no request, which is
the current behavior. Or document a local OpenAI-compatible base and still
require an explicit probe flag.

**Trade-off.** A cloud default would make doctor or status a network call and
a credential boundary. A local-only adapter would not satisfy an operator who
expects `https://api.openai.com/v1`.

**Fail-closed now.** `provider = openai-compatible` returns unsupported and
does not open a socket. The API key is not read.

**Acceptance evidence before code.** A written request and response shape,
timeout and retry rules, a rule that the key is never logged, and tests that
an absent key sends no request.

## Embedding sidecar

**Missing decision.** Vector dimension, distance metric, when a sidecar is
stale, and whether retrieval may change when the sidecar is absent.

**Safe alternatives.** Keep keyword and structured search as the only retrieval
path. Keep `embed()` as a non-authoritative call that stores nothing.

**Trade-off.** Storing vectors without a retrieval contract invites a second
ranking path. Not storing them means semantic search stays planned.

**Fail-closed now.** `UnavailableVectorStore` reports that no store exists.
A rejected or accepted embedding does not insert a domain row.

**Acceptance evidence before code.** A rebuild can drop the sidecar. Retrieval
answers are identical when it is absent. No domain table gains a vector column
without a migration test.

## Skills runtime

**Missing decision.** Who may install a skill, what it is allowed to read, and
whether it may propose a record that a human must still accept.

**Safe alternatives.** Leave the `skill` tables unused. Do not expose them as
tools.

**Trade-off.** An executor without a trust boundary can bypass the promotion
and approval rules. A read-only catalog is not a runtime.

**Fail-closed now.** No service reads `Skill` or `SkillVersion`.

**Acceptance evidence before code.** An operator contract that a skill cannot
become a calculation or a promoted row unless that contract says so, with a
test that an unapproved skill writes nothing.

## Additional calculation method

**Missing decision.** Method id, inputs, units, outputs, and the cases that
must be refused.

**Safe alternatives.** Keep NPT roll-up V1 as the only executable method.

**Trade-off.** A generic calculator would invent engineering meaning. One
specified method can be tested at its boundaries.

**Fail-closed now.** An unknown method is not executed. Review does not
recalculate.

**Acceptance evidence before code.** The method text exists before the
implementation, including invalid inputs and unit mismatches.

## Additional document-class writer

**Missing decision.** The source shape for one knowledge-supported or
extract-only class: which columns are required, which ambiguities are refused,
and which provenance locators are mandatory.

**Safe alternatives.** Keep the class in the coverage matrix at its current
level. Extraction may retain the artefact. Promotion does not invent a row.

**Trade-off.** Promoting from prose without a shape contract writes authority
the source did not state.

**Fail-closed now.** A class without a handler does not gain a domain writer.

**Acceptance evidence before code.** A forensic corpus case for that class,
including a refusal, before the coverage matrix changes.

## Still excluded

Risk scores, currency conversion, AFE rules, plan-versus-actual dashboards,
and inferred survey geometry stay excluded. Their absence is a product
boundary, not a missing patch.
