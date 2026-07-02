# Search Query Coverage

`run_news_scan` creates temporary hypothesis and wildcard searches, but durable
coverage comes from the topic's configured `searchQueries`. Operators cannot
edit topic JSON directly in this session. Durable query updates must go through
the MCP query-governance lifecycle:

## Lifecycle

1. `read_search_queries(slug)` to inspect configured queries and generated
   preview channels.
2. `propose_search_query_update(slug, add=[...], remove=[...], rationale=...,
   coverage_gaps=[...])` to file a typed proposal. This does not mutate state.
3. `red_team_search_query_update(proposal_id)` to run the mandatory MCP
   red-team gate. The MCP performs deterministic lint for hard structural
   failures, then uses the local model jury path for the substantive retrieval
   coverage verdict.
4. `apply_search_query_update(proposal_id, dry_run=true)` to preview the final
   before/after set.
5. `apply_search_query_update(proposal_id, dry_run=false)` only after red-team
   verdict `APPROVE` and human approval. This mutates `searchQueries` and
   appends `governance.search_query_history`; it never changes evidence,
   likelihoods, posteriors, or `lastScanned`.

If query coverage is missing, stale, or over-narrow, do not merely describe the
problem in prose. File a search-query proposal through MCP or explicitly report
that you did not update durable retrieval coverage.

## Query philosophy

Treat `searchQueries` as retrieval hooks, not evidence claims. They must be
hypothesis-neutral and bidirectional: a good query can surface evidence for
or against any hypothesis. Never write queries that only hunt for the current
favorite hypothesis.

## Coverage axes

Before scanning, do a coverage audit. A live topic should normally have
queries covering these axes when applicable:

- **Core event axis**: the event, place, and main actors in short keywords.
- **Escalation / adverse axis**: closure, attack, breakdown, sanctions,
  enforcement, suspension, denial, collapse, or other bad-state verbs.
- **De-escalation / recovery axis**: reopen, agreement, ceasefire, escort,
  resumption, normalization, or other good-state verbs.
- **Measurement axis**: the resolution metric and primary data sources.
- **Institutional/source axis**: agencies, wires, regulators, militaries,
  exchanges, courts, or technical bodies that publish authoritative signals.
- **Schema axis**: terms from high-value unfired indicators and anti-indicators,
  especially observable metrics that would create FIRE/OBSERVE candidates.

## Authoring rules

Build queries with these rules:

- Use 4-10 stable terms: actors, places, action verbs, source/metric words.
  Example: `Iran Strait of Hormuz closure transit rules`.
- Prefer several orthogonal keyword queries over one long sentence. Each query
  gets its own retrieval budget and freshness gate.
- Include both common names and domain-specific terms when they differ:
  `Strait of Hormuz`, `Hormuz`, `IRGC`, `tanker transits`, `war risk premium`.
- Include high-signal source or metric names when they matter: `Reuters`,
  `AP`, `Lloyd's List`, `EIA`, `CENTCOM`, `IRNA`, `maritime insurance`.
- Use source names as normal terms before using `site:` filters. A `site:`
  query is a deliberate narrow probe; it can reduce broad recall and should
  not be the only query for an axis.
- Avoid exact headlines, quotes, Boolean syntax, and complex parentheses unless
  intentionally chasing a known report. They are brittle across search backends.
- Do not stuff dates into every query. Recency is handled by the adaptive scan
  window and freshness gate. Use `{window}`, `{window_label}`, or `{date}` only
  when the words improve retrieval.
- Cap most topics at roughly 8-15 configured queries. For a hot geopolitical
  or market topic, prefer 10-20 well-separated queries over fewer giant ones.

## Retrieval limits

Know the retrieval limits when judging coverage. For each query channel,
`run_news_scan` defaults to `max_results_per_channel=4`, caps it at 6, then
aggregates at most 24 deduped hits for that channel. Each channel uses DDGS
text search, DDGS news search, and small source-qualified searches against the
server's built-in source list when the query does not already contain `site:`.
Freshness filtering happens after dedupe and full-article fetch. Because of
these caps, missing query axes are a real recall failure; the scan can look
busy while still missing the event class the topic needs.

## Coverage-gap proposal template

When query coverage is weak, file a query update proposal. Use this structure
as the content for `coverage_gaps`, `add`, `remove`, and `rationale`; do not
treat it as a substitute for the MCP proposal:

```text
SLUG: <topic slug>
COVERAGE_GAPS:
- <missing axis or stale query problem>
QUERIES_TO_ADD:
- <short query>
QUERIES_TO_REMOVE:
- <query, or none>
SCAN_CONFIDENCE: comprehensive | partial | weak
REASON: <one paragraph explaining the retrieval risk>
```

If `red_team_search_query_update` returns `REVISE` or `REJECT`, revise or
withdraw the proposal. Do not apply it and do not describe the scan as
comprehensive until the durable query set passes the red-team gate.
