# Voice-Native Web Research Agent — spec

> Condensed from the original project plan supplied by the author. Where this file and
> the author's original disagree, the original wins. Section numbers follow the original.

## 1. Objective

A small native macOS app that turns spoken requests into cited web research:

```
activate → speak → inspect/edit transcript → GO → watch progress → cited answer
```

Closer to a Siri-style utility than a chatbot. Distinguishing architecture:
speech recognition is **local**, the ASR model is **streaming and quantized**, raw audio
never leaves the Mac, the user **explicitly approves** the recognized text before any
cloud call, Nebius Token Factory serves `nvidia/Nemotron-3_5-Lightning`, Tavily is the
**only** external capability, and progress + answer stream into the native UI.

Not a general computer agent. Quality must come from a polished voice workflow, clean
architecture, reliable research, low perceived latency and transparent execution — not
from the number of tools.

## 2. Principles

**2.1 Local speech first.** Privacy boundary is explicit: raw audio → local quantized
Kyutai ASR → editable transcript → *(user presses GO)* → approved text → Nebius + Tavily.
Never imply the whole system is local; speech is local, research is cloud-backed.

**2.2 Streaming improves UX, it does not trigger actions.** Use streaming ASR to update
the transcript live. Never send partial transcripts to Nebius, launch speculative
searches or model calls, hit Tavily before confirmation, or auto-execute when speech
ends. The streaming path terminates at the transcript editor.

## 4. Architecture

SwiftUI/AppKit UI ⇄ (loopback) ⇄ local Python service {resident ASR runtime → session
controller} → research service → {Nebius Token Factory, Tavily}. Keep boundaries explicit
even when components share a process. The UI consumes events, not AI internals.

## 5. Phase zero — validate the quantized ASR runtime (HARD GATE)

Target model: `efficient-nlp/stt-1b-en_fr-quantized`, **Q4_K** variant. The whole point is
to avoid loading the full BF16 1B checkpoint.

**Critical rule:** do not silently substitute `kyutai/stt-1b-en_fr-mlx` — it is Apple-Silicon
friendly but still a ~1.98 GB BF16 checkpoint.

Required properties: runs locally on Apple Silicon; accepts live audio chunks, not just
complete recordings; preserves incremental/streaming decoding; emits transcript updates
during speech; acceptable accuracy; loads weights **once**; acceptable memory; stays
responsive alongside the UI.

Measure: model load time · resident memory · real-time factor · CPU · GPU/Metal ·
audio→transcript delay · stability over several minutes.

Deliverable is a tiny standalone proof: microphone → Q4 Kyutai → continuous live
transcript. Nothing else.

## 6. ASR service

Interface: `start_session()` / `push_audio(frame)` / `finalize_session()` / `cancel_session()`.
Long-lived service: load on first use, reuse weights across activations, and unload
after five idle minutes (`VNR_ASR_IDLE_TIMEOUT_S`, configurable). Never
load-transcribe-unload per click. Cold wakes show loading and gate recording on ready.
Unloading must clear MLX caches and measurably reduce current memory. Preserve the input format the runtime
expects (Kyutai is 24 kHz mono) rather than adding needless resampling stages.

## 7. Transcript review

Recording end must **not** start research. Enter `REVIEW`: editable field, user may
correct, cancel, or press GO. GO freezes that text as the request. Keep both
`raw_asr_transcript` and `submitted_query` — they may differ, and the difference is the
ASR quality signal.

## 8. Nebius

OpenAI-compatible API. Externalized config: `NEBIUS_API_KEY`, `NEBIUS_BASE_URL`,
`NEBIUS_MODEL`, `TAVILY_API_KEY`. Default model `nvidia/Nemotron-3_5-Lightning`; verify
availability with `GET /v1/models` rather than hardcoding unverifiable assumptions.
Thin provider layer: `create_completion` / `create_tool_completion` / `stream_completion`.
No LangChain or other orchestration framework.

## 9. Research agent

One model, one conversation state, one tool, one bounded loop. No planner, no subagents,
no supervisor, no Deep Agents, no LangGraph. The model decides whether another search is
needed; the application executes the tool, never the model.

## 10. Tool design

Exactly one tool: `web_search(query, topic?, time_range?, search_depth?)`. The model never
gets Tavily's full API surface. App-owned defaults: `max_results=5`, `include_answer=false`,
`include_images=false`, `include_raw_content=false`, `topic=general`, depth `basic`/`fast`.

`include_answer=false` matters: Tavily retrieves, Nemotron reasons. Otherwise the agent
just rewrites Tavily's own LLM answer.

## 11. Research strategy & stopping policy

Most substantive requests should search, but not repeatedly without reason. Decompose
complex requests into focused searches. Prefer primary sources, official docs for product
claims, papers for research claims, recent sources when recency is asked for; notice
disagreement between sources; don't treat snippets as infallible.

Budgets (configuration values, not constants):

```
maximum agent turns:     6
maximum Tavily searches: 4
maximum results/search:  5
```

Do not raise these without evaluation evidence.

## 12. Result representation

Normalize Tavily results to: title · url · content/snippet · relevance score · published
date (when present). No stray API metadata in model context. Stable per-session IDs
`[S1] [S2] …`.

## 13. Citations

Answers must carry traceable sources: `claim … [1]` plus a `Sources` list of
`[n] Title — URL`. The model must never invent URLs; the source list must derive from URLs
Tavily actually returned this session. Validate citations after generation and remove or
flag invalid ones.

## 14. Search depth policy

Start `basic`/`fast`. Escalate to `advanced` only on explicit deep-research requests, weak
initial results, ambiguous evidence, or subjects needing precise retrieval. The agent may
*request* a depth; the application enforces the limit.

## 15. Reading full pages

No extract/read-page tool in V1. Add `read_url(url)` via Tavily Extract in V1.1 only if
evaluation shows snippets are too shallow.

## 16. Prompt

Concise and behavioral: fresh local timestamp with UTC offset at each request and
again at synthesis, explicit future-event checks for next/upcoming questions, role, the `web_search` tool, grounding requirement,
primary-source preference, recency handling, search-only-when-evidence-is-insufficient,
no invented citations, concise answers unless depth is requested. Not a multi-page prompt.

## 17. Streaming model output

Internal decision/tool rounds need no token-level UI output — expose semantic progress
instead. Stream only the final synthesis. Two streaming paths total: Kyutai → live
transcript while speaking, Nebius → live answer while researching.

## 18. Progress UI

Expose actions and states. Provider reasoning is preserved separately from answer text
and shown only inside an optional, collapsed disclosure (user update 2026-09-21).
Never mix reasoning with the final answer or its citation numbering. States:

```
IDLE · LISTENING · FINALIZING_TRANSCRIPT · REVIEW · SUBMITTED · RESEARCH_STARTED
SEARCH_STARTED · SEARCH_COMPLETED · SYNTHESIZING · ANSWER_STREAMING
COMPLETED · FAILED · CANCELLED
```

## 19. Local networking

Persistent loopback WebSocket carrying both commands and events. Bind loopback only,
never the LAN.

UI → service: `recording.start` · `audio.frame` · `recording.stop` · `research.submit` ·
`research.cancel`
Service → UI: `asr.partial` · `asr.final` · `research.started` · `research.search_started` ·
`research.search_completed` · `research.synthesizing` · `research.answer_delta` ·
`research.completed` · `research.error`

## 20. Application state

`ResearchSession`: id · created_at · raw_transcript · submitted_query · status · searches[]
· sources[] · answer · metrics · error. A search record holds query · parameters ·
started_at · completed_at · result_count · credit_usage. No database in V1 — in-memory
plus optional local JSON.

## 21. Metrics

ASR: recording duration · time to first transcript token · recording-end→final transcript ·
peak memory.
Agent: GO→first Nebius response · GO→first Tavily search · Tavily latency · search count ·
agent turns · input/output tokens · cost if available · Tavily credits · GO→first answer
token · GO→completed answer. Structured logs are enough; don't build an observability
platform.

## 22. Error handling

Microphone unavailable · ASR runtime not ready (don't enable recording) · Nebius auth/error
(keep the approved transcript visible for retry) · Tavily auth/rate limit (say "Search
unavailable" rather than faking a researched answer) · no useful results (let the model say
evidence was insufficient) · network interruption (never lose the submitted query) ·
user cancellation (cancel outstanding requests, return to transcript state).

## 23. Credentials

Never hardcode or commit `NEBIUS_API_KEY` / `TAVILY_API_KEY`. `.env` for dev, gitignored.
Keychain for a distributed app. Don't hand secrets to the Swift UI — the local service owns
the external calls.

## 24. Testing

Unit: Tavily normalization · source IDs · citation validation · budget enforcement · loop
termination · event generation · malformed tool calls · config loading.
Mocked agent: deterministic LLM/Tavily sequences asserting tool-call count, progress
events, bounded turns, correct sources, completion.
Integration: a small real Nebius+Tavily set.
ASR: a fixed set of English/French commands incl. names (Kyutai, Nebius, Tavily, NVIDIA),
pauses, longer prompts, moderate noise. Goal is workflow acceptability, not a WER benchmark.

## 25. Evaluation set

~20 prompts across: current technology · AI research · product/doc lookup · comparison ·
latest news · fact checking · multi-part research · simple lookup. Record searches, turns,
latency, tokens, credits, citation validity, usefulness. Add architecture only when
evaluation proves a weakness.

## 26. Milestones

1. Quantized streaming ASR spike (mandatory gate)
2. Nebius + Tavily research CLI (no UI)
3. End-to-end local prototype (minimal temporary UI)
4. Native macOS UX (menu bar, overlay, live transcript, review, GO, progress, streamed
   answer, clickable citations) — no feature expansion
5. Reliability & metrics — run the eval set, fix the biggest problems
6. Demo readiness

## 27. Definition of done for V1

**Speech:** quantized model runs locally · stays resident · live transcription works ·
transcript reviewable/editable · no raw audio uploaded.
**Research:** GO sends approved text · Nemotron via Token Factory · autonomous Tavily
invocation · multiple searches within a strict budget · reliable termination · traceable
sources.
**UX:** native activation · clear listening state · visible live transcript · editable
confirmation · visible research actions · streamed answer · graceful errors.
**Engineering:** secrets uncommitted · clear component boundaries · mocked agent-loop tests ·
latency/usage metrics · README covering architecture and local setup.

## 28. Non-goals for V1

Deep Agents · LangGraph · multi-agent orchestration · sub-agents · MCP · filesystem access ·
terminal access · GitHub · email · calendar · browser automation · computer control ·
long-term memory · vector DBs · RAG · wake word · TTS · voice responses · continuous
listening · speculative search · speculative LLM execution · cloud ASR · mobile clients ·
accounts/auth · multi-user.

## 29. Expansion rules

Add a capability only when evaluation identifies a concrete limitation, users repeatedly
need it, or it materially improves the workflow. Likely order: V1.1 Tavily Extract /
`read_url` · V1.2 history + saved reports · V1.3 follow-ups over a research session ·
V1.4 optional local notes · V2 additional explicitly approved tools.

## 30. Philosophy

The whole system should fit on one slide. That simplicity is a feature: a useful agent does
not require an elaborate multi-agent framework. The interesting engineering is local
streaming speech + explicit human approval + lightweight agentic research + live web
grounding + transparent progress. Build that exceptionally well before adding anything.
