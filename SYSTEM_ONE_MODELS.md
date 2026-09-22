# System One decision models, and Gemini — reference notes

Written before any implementation (per the step-0 instruction: learn first).
Everything below is sourced from primary docs, not memory — URLs inline.
Update this file if any of it turns out stale once real keys are in and
Step 5 actually exercises these against live endpoints.

## Jev (TypeSafe AI)

Sources: [typesafe.ai/blog/introducing-system-one-models-and-jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev),
[docs.typesafe.ai/api.md](https://docs.typesafe.ai/api.md),
[docs.typesafe.ai/introduction/quickstart](https://docs.typesafe.ai/introduction/quickstart),
[docs.typesafe.ai/primitives](https://docs.typesafe.ai/primitives),
[typesafe-sdk-python](https://github.com/typesafe-ai/typesafe-sdk-python).

**What it is**: a hosted "System One Model" — trained with RLCD (Reinforcement
Learning for Calibrated Decisions), not RLHF. Takes unstructured state in,
returns typed, probabilistic decisions out. It cannot generate text — this
is a classifier/scorer, never a substitute for the extraction LLM call.

**Endpoint**: `POST https://api.typesafe.ai/v1/systemone`
Headers: `Authorization: Bearer <TYPESAFE_API_KEY>`, `Content-Type: application/json`.

**Request body**:
```json
{
  "state": "Hi, I've been trying to connect my Stripe account for 3 days...",
  "model": "jev-latest",
  "questions": {
    "department": {
      "type": "choice",
      "instructions": "Which team should handle this",
      "criteria": {
        "billing": "Payment or subscription issues",
        "technical": "Bugs or integration problems",
        "sales": "Pricing or account questions"
      }
    },
    "frustration": {
      "type": "score",
      "instructions": "How frustrated the customer appears",
      "criteria": ["Calm, just stating facts", "Frustrated but civil", "Very angry, strong language"]
    },
    "is_urgent": {
      "type": "noul",
      "instructions": "The message conveys urgency or time-sensitivity"
    }
  }
}
```
`state` is the only piece of context sent — no separate system prompt. Every
question needs `type` + `instructions`; `choice`/`score` also need
`criteria` (a `{option: description}` map for choice, an ordered list of
spectrum levels for score); `noul`'s `criteria` is optional clarification
text for the true/false meaning.

**Response**:
```json
{
  "model": "jev-1.13.0",
  "answers": {
    "department": {"type": "choice", "choice": "technical", "probabilities": {"...": 0.0}, "confidence": 0.78},
    "frustration": {"type": "score", "score": 1.0, "legend": {"0": "Calm...", "1": "..."}, "probabilities": {"...": 0.0}, "confidence": 1.0},
    "is_urgent": {"type": "noul", "noul": 1.0}
  },
  "usage": {"input_tokens": 392, "output_tokens": 65}
}
```
- **choice**: `choice` (winning key), `probabilities` (every option → prob),
  `confidence` (0-1, how peaked the distribution is).
- **score**: `score` (probability-weighted position along the levels, not
  necessarily an integer), `legend` (level index → description),
  `probabilities`, `confidence`.
- **noul**: just `noul` (0-1, P(true)) — no separate confidence field; treat
  distance from 0.5 as the de facto confidence if a single scalar is needed.

**Limits**: up to 255 options per `choice`. Above that, TypeSafe recommends a
two-stage flow (independent per-option scoring, then explicit selection) —
not needed for anything in this codebase today (branch selection tops out
at 1132 raw branches, but see the flat-site finding — that's not really 1132
*options in one question*, it's 1132 separate yes/no decisions, and Step 3's
"one yes/no per item" design sidesteps this limit entirely by construction).

**Pricing**: $0.042 / 1M input tokens; output "free (too cheap to meter)".
No published RPM/RPS number — rate limiting is signaled via `429`, with
`529` for transient overload. **Retry with exponential backoff, not
immediate retry** — stated explicitly in the docs.

**Errors**: `401` missing/invalid key, `422` schema validation failure,
`429` rate limit, `529` overloaded (retry after a delay).

**SDK**: official `typesafe-sdk-python` exists (`pip install typesafe-sdk`,
Python ≥3.10), but the raw HTTP surface is small and well-typed enough that
a plain `requests.post()` client (same pattern this repo already uses for
`LocalOllamaEmbeddings` in `llm_factory.py`) avoids a new dependency
entirely. **Open decision for Step 3, not resolved here**: SDK vs. raw
`requests`. Leaning `requests` for consistency with the existing
provider-routing style and to avoid asking for a new package when the API
surface doesn't need one — but not committed to that yet.

## Laya (Convai Innovations)

Sources: [huggingface.co/convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya),
[github.com/NandhaKishorM/laya](https://github.com/NandhaKishorM/laya).

**What it is**: open-weights (Apache 2.0), non-autoregressive — a single
forward pass per question, no text generation, same three typed-decision
primitives as Jev (`choice`/`score`/`noul`). Runs locally (GPU or CPU).

**Three checkpoints**:
| Checkpoint | Backbone | Params | Context | Best for |
|---|---|---|---|---|
| `laya` (root) | ModernBERT-large | 421M | 512 tok | English, zero-shot-ish |
| `laya-multilingual` | mmBERT-base | 322M | 1024 tok | 100+ languages, ~2× faster |
| `laya-typed-decisions` | ModernBERT-large | 421M | 1024 tok | Fine-tuned for typed decisions (0.766 acc) |

**The Router**: auto-detects language/script, dispatches to the right
checkpoint in one forward pass. `Router(preload=True)` keeps all three
resident (production mode, ~33ms GPU latency); `Router(max_loaded=1)`
evicts LRU-style (7-10s reload penalty per switch — **never use this mode
for a live pipeline**, matches the step-3 instruction to load once and keep
resident).

**API**:
```python
import laya
agent = laya.load("convaiinnovations/laya")   # or Router(preload=True) for multi-checkpoint
result = agent.predict(state, questions)       # same state/questions shape as Jev, roughly
answers = result["answers"]
answers["department"]["choice"]      # winning option
answers["department"]["confidence"]  # 0-1
answers["urgency"]["score"]          # ordinal position
answers["churn_risk"]["noul"]        # P(true), 0-1
```
`state`/`questions` shape is compatible in spirit with Jev's (dict of typed
questions with `type`/`instructions`/`criteria`), though field names should
be diffed exactly against Jev's when building the shared interface — not
assumed identical just because both call it `criteria`.

**Token budgets — the one number that actually constrains branch
selection**: context splits into a document budget and an option budget
(`head_max_len`). English default: 512 total, 192 for options, ~320 for
state. High-cardinality `choice` (50+ options) degrades hard at defaults —
confirmed on Banking77 (77 labels): Laya 0.425 vs. Jev 0.870, because each
label gets only 3-4 tokens. Doesn't bite this project's branch-selection
design once it's "one yes/no per branch" (noul, not one giant choice) —
each `noul` question only needs to fit one branch's own sample URLs in the
document budget, not all branches' names in one option list.

**Calibration**: ships over-confident out of the box. Temperature fitting
drops ECE 0.466→0.081 for `laya`; `laya-multilingual` has no fitted
temperatures at all yet — don't trust its raw confidence numbers.

**Install**: `pip install laya`. Needs `huggingface_hub` 1.x, `transformers`
5.x, `torch` 2.14+, Python ≥3.10. **`USE_TF=0` env var needed if model
construction hangs** (prevents a TensorFlow import deadlock) — per the
step-1 instruction, this env var will need to be set wherever Laya loads,
not left to bite someone once.

**Flagging now, before Step 3**: this project has zero ML-inference
dependencies today (no `torch`, no `transformers`) and one load-bearing pin
— `numpy<2` (`pyproject.toml`, `LESSONS_LEARNED.md` #1). `torch>=2.14`/
`transformers>=5` are recent enough that a `numpy<2` conflict is a real
possibility, not a hypothetical — **must be checked with `uv add laya
--dry-run` before actually installing**, per `CLAUDE.md`'s existing `uv`
workflow rule, and the install itself needs explicit go-ahead per
`CLAUDE.md`'s "ask before installing a package" convention. Not done as
part of this research pass.

### Honest Limits (from Laya's own docs, load-bearing for how the traditional-path comparison in Step 5 gets read)

1. **Base checkpoints are near chance zero-shot on typed-decisions**: 0.352-0.362
   accuracy vs. 0.318 random / 0.461 majority-class baseline. The 0.766
   headline number is the *fine-tuned* checkpoint only, trained on that
   benchmark's own data — "treat Laya as a fast base to specialise, not a
   zero-shot decision engine" (their words). This matters directly: nothing
   in this codebase's decision points has a fine-tuning dataset, so any
   Laya deployment here runs zero-shot unless that changes.
2. **High-cardinality choice collapses** without tuning (Banking77: 0.425 vs
   Jev's 0.870) — mitigated by this project's "one noul per branch" design,
   not by Laya having gotten better at big `choice` questions.
3. **`score` is the weakest primitive** (SST-5: 0.372 accuracy) — relevant
   if `sample quality scoring` (Step 2's fourth decision point) ends up
   using `score` rather than `noul`/`choice`.
4. **English-only on the root checkpoint** — Khmer scores 0.000 at 95.2%
   confidence on it; the Router exists specifically to prevent this by
   routing non-Latin script elsewhere. Always go through the Router, never
   load the English checkpoint directly for anything that might see
   non-English content (crawled site text is exactly that case).
5. **Ships over-confident** — recalibrate before gating any real decision on
   a raw confidence number (see Step 4's traceability requirement — record
   confidence either way, but don't threshold on it uncalibrated).

## Gemini (Google)

Sources: [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing),
[ai.google.dev/gemini-api/docs/openai](https://ai.google.dev/gemini-api/docs/openai),
[ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits).

**OpenAI-compatible endpoint confirmed**: `base_url =
"https://generativelanguage.googleapis.com/v1beta/openai/"`, API key passed
as the OpenAI client's `api_key`. Supports chat completions AND embeddings
(`/v1/embeddings`) through the same compat layer — this project's existing
`get_llm()` pattern (`ChatOpenAI(base_url=..., api_key=...)`) slots Gemini
in exactly like nvidia/openai/ollama_cloud already work, no new client code
shape needed. Docs note the OpenAI-compat layer is still "beta" — some
OpenAI params are silently ignored, and Gemini 2.5 Pro/3.x can't disable
reasoning through it.

**Free-tier chat models available today** (per current pricing docs):
`gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.5-pro`, plus newer
`gemini-3.x-flash` variants. **Not live-verified against a real API call**
(no `GEMINI_API_KEY` in this environment yet) — picked `gemini-2.5-flash`
as the default candidate for Step 1 because it's non-preview, explicitly
still free-tier-listed, and matches this project's existing pattern of
defaulting to a "flash"/fast tier rather than the biggest model. **Must be
confirmed with a real call once the key is in** (Step 5), same as every
other provider default in this audit.

**Rate limits**: the docs page doesn't publish exact free-tier numbers
directly — it points to a live dashboard (`aistudio.google.com/rate-limit`)
that's account-specific and requires being signed in, so it can't be
read here. Third-party aggregators (not primary source, not fully trusted)
suggest free tier is roughly 10 RPM / 250 RPD for Flash-tier models — **treat
as unverified** until checked against the actual account's dashboard once
a key exists.

**Embeddings — the thing worth flagging per the step-1 instruction**:
`gemini-embedding-2` exists and is free-tier-eligible ("free of charge" for
text/image/audio/video input on the free tier; $0.20/1M tokens text on the
paid standard tier). This is a real, working hosted embeddings option — the
first one available to this project that isn't local Ollama. **Per
instruction: noting this, not switching embeddings over.** `EMBEDDING_
CAPABLE_PROVIDERS` in `config.py` currently excludes Gemini entirely (it
doesn't exist as a provider yet); adding Gemini as a chat provider alone
does not have to touch that set.

## What this means for Step 1 (this project's config/provider work)

- Gemini slots into `llm_factory.py::get_llm()` the same way every other
  `ChatOpenAI`-backed provider does — new `LLMProvider.GEMINI` enum value,
  new `LLMConfig` branch in `config.py::get_llm_config()`, new entry in
  `main.py::_DEFAULT_MODEL_BY_PROVIDER`. No new client class needed.
- Jev and Laya do **not** fit the `ChatOpenAI` shape at all — neither
  returns chat-completion-shaped text, both return typed decisions. They
  are not "providers" in the `llm_factory.py` sense; they're a different
  kind of thing entirely (a decision backend, not a chat backend) and
  belong in the new decision-interface abstraction (Step 3), not
  `llm_factory.py`.
- Jev needs no new dependency (plain `requests`, matching the existing
  `LocalOllamaEmbeddings` style) unless the SDK is preferred later — open
  question, not resolved here.
- Laya needs a real dependency addition (`torch`, `transformers`,
  `huggingface_hub`) with a live pin-conflict risk against this project's
  `numpy<2` — needs an explicit ask + `--dry-run` check before install,
  not bundled silently into a larger change.
