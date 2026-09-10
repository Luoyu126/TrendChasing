> GitHub 托管部署使用 [Actions + Supabase 独立入口](github-actions-supabase.md)；本文的本机定时、SQLite 与旧热榜入口说明不用于托管调度。

# Paper recommendations

The optional `trendradar.papers` branch discovers arXiv papers, scores their complete
abstracts against a separate research question, and appends **Paper Recommendations**
to the existing daily HTML report. The existing email sender uses that same HTML.
News, RSS, social content pools and their classification/storage are unchanged.

## Configure and enable

1. Edit `config/papers.yaml`. Replace the example `research_question` with your
   **complete** research question and context, and set `topics` to your own topics.
   No research domain or safety keywords are implicitly added.
2. Set `enabled: true`. The default is disabled. The loader discovers `papers.yaml`
   beside the main config; set `PAPERS_CONFIG_PATH=/absolute/path/interests.yaml`
   to override it. A missing default file disables the branch; an invalid explicit
   file reports a paper failure without stopping news.
3. Set `arxiv_query` to the categories/search scope you want. The adapter uses the
   [arXiv Atom API](https://info.arxiv.org/help/api/user-manual.html), independently
   of RSS feeds. Do not add paper candidates to the news RSS pipeline.
4. Use the existing main `ai` config and `AI_API_KEY` environment variable. The
   paper scorer uses TrendRadar's `AIClient`, its timeout and transient retries.
   Model fallbacks are disabled for paper calls so a cached score is never silently
   attributed to a different model. The provider must support JSON object output.
5. Use an effective `daily` report mode in the existing timeline/scheduler, with
   collection enabled. Email credentials, notification enablement and push/once
   scheduling remain the existing settings. An enabled paper branch generates HTML
   even if HTML storage was disabled, because email needs the report file.

`keyword_prefilter: false` bypasses both keyword groups for better recall. When true,
`keywords` and `required_keywords` each require **at least one** matching phrase if
nonempty; both groups must pass. Matching is case-insensitive substring matching
with whitespace normalization, over title plus full abstract. Empty lists impose
no constraint. Topics describe relevance to the LLM; they do not restrict discovery
unless included in `arxiv_query`.

`relevance_threshold` is inclusive, on a 0–1 scale. A model's boolean flag cannot
bypass it, cached results are checked again, and there is no quota-filling fallback.
Successful zero matches display an empty-state message. Processing failures display
an incomplete-processing message and remain retryable.

Ranking happens **after** threshold filtering:

```
recency = 0.5 ** (age_hours / recency_half_life_hours)
rank = (relevance_weight * relevance_score + recency_weight * recency)
       / (relevance_weight + recency_weight)
```

Defaults are 0.85 relevance, 0.15 recency, and 72 hours half-life. Age uses original
publication time. `daily_output_limit` caps each daily digest (zero is valid), not
the cumulative number delivered across repeated daily-mode runs. Existing scheduler
push-once settings govern repeated delivery. A paper may appear on successive days
within the lookback window; there is no separate delivery ledger in this version.

## State and failure behavior

The default database is selected by `config/content_pool.yaml` (`db_path`), shared
with social and WeRSS candidates, digest batches, and delivery records. Paper metadata
and scoring caches use the `papers` and `judgments` tables in this database.
Raw arXiv candidates enter the pool before screening; filtered and failed candidates
remain available for reconsideration or retries. An explicit `state_path` is still
supported for isolated standalone previews; the unified processor always uses its pool
connection. Existing installations should follow the [local migration](unified-database.md).
The legacy news remote backend does not synchronize this database.

Candidates are deduplicated by canonical arXiv ID, with the highest version retained.
All authors, categories, dates, links and complete abstracts are preserved. Judgments
are keyed by the full metadata/content (including ID and version), research config,
model settings/endpoint, and prompt version/content. API key rotation does not force
reclassification. Different research questions never share judgments. Changing
interest or ranking settings conservatively invalidates the cache. Retained candidates
within the current publication lookback are rescored for a new interest; discovery
of papers outside the configured arXiv query is not implied.

A successful negative judgment is cached. API failures or malformed structured
responses have a distinct failed status and are attempted again next run. The shared
client handles configured transient LLM retries; arXiv network/429/5xx errors get up
to three attempts with backoff. On ingestion failure, retained candidates can still
be scored and the report explicitly notes incomplete processing. Error records store
exception types, not provider messages that could contain secrets.

## Offline verification (no emails or API calls)

From the repository root, in an environment with the project's dependencies:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True python3 -m unittest discover -s tests -v
LITELLM_LOCAL_MODEL_COST_MAP=True python3 -m trendradar.papers \
  --fixture tests/fixtures/papers/arxiv.xml \
  --scores tests/fixtures/papers/scores.json \
  --now 2026-09-09T12:00:00Z \
  --output /tmp/trendradar-paper-preview.html
```

The preview command deliberately runs the supplied fixture even when `enabled` is
false. It uses supplied synthetic scores, an isolated temporary database, and writes
HTML plus JSON. It cannot send email. The environment flag also prevents LiteLLM
from refreshing model-cost metadata during TrendRadar package imports. Tests also exercise the existing report builder
and SMTP MIME construction with SMTP mocked, including papers-only daily output.

## Boundaries and maintenance

Only three existing files contain integration hooks: `core/loader.py` discovers the
config; `__main__.py` runs the daily branch and recognizes paper content;
`report/generator.py` appends the section after news filtering and translation, before
writing all report copies. Selected papers never enter the title-only classifier.
All other implementation lives in `trendradar/papers`.

V1 uses one bounded arXiv request for the newest `max_candidates` (1–2000) publications
within `lookback_days`. It does not paginate beyond the cap, download PDFs, perform
full-text analysis, infer missing metadata, or guarantee exhaustiveness. Full abstracts
can make large emails and consume LLM tokens; context-limit failures are surfaced,
not silently truncated. Existing non-email channels do not include this section.
State currently has no automatic retention cleanup; remove/archive the paper database
when appropriate, understanding that this also removes reusable judgments.

For future full-text work, add a post-selection enrichment stage that accepts selected
paper records and returns separately labeled evidence; use a distinct prompt/cache
namespace. The `Paper` adapter contract and the selection result are the extension
points. Never overwrite the source abstract with generated text.

## Reference and licensing review

Reference: [Paper-Pulse](https://github.com/yangjunx21/Paper-Pulse), Junxiao Yang (2025),
revision `f05147ac12eab8ffd7b84a7d44e819678d512e03`, available locally in
`external/Paper-Pulse/` as a standalone reference checkout (not a Git submodule). Its README declares MIT and links a LICENSE, but that revision contains
no LICENSE file. Therefore **no source code or prompts were copied**. This is an
independent implementation under TrendRadar's existing GPL-3.0 license. The reference
informed the staged discovery concept; its safety-specific defaults, boolean threshold
bypass, below-threshold fallback and PDF pipeline were not ported.
