# Evaluation set (docs/PLAN.md §25)

Twenty prompts representing the product, written the way someone would *speak* them — the
input to this system is dictation, not typing. Run them at Milestone 5 and record what
happened. The point is to find out whether the lightweight loop is enough before adding
any architecture.

```bash
for i in $(seq 1 20); do
  uv run vnr-research --json --save runs/eval "$(sed -n "${i}p" docs/eval-prompts.txt)"
done
```

| # | Category | Prompt |
|---|---|---|
| 1 | current technology | What's new in Kyutai STT? |
| 2 | current technology | Which open speech-to-text models run well on Apple Silicon right now? |
| 3 | AI research | Find recent work on streaming ASR for low-resource languages. |
| 4 | AI research | What are the current approaches to reducing latency in streaming speech recognition? |
| 5 | AI research | Has anyone published on adapting speech models to Moroccan Darija? |
| 6 | product / docs | How does Nebius Token Factory handle function calling? |
| 7 | product / docs | What search depths does the Tavily API support and what do they cost? |
| 8 | product / docs | What context length does Nemotron 3.5 Lightning support? |
| 9 | comparison | Compare Kyutai STT and Whisper for real-time transcription. |
| 10 | comparison | Compare two recent open speech models on latency and accuracy. |
| 11 | comparison | Is Nemotron 3.5 Lightning better suited to tool use than the previous Nemotron? |
| 12 | latest news | What did NVIDIA recently release around agentic models? |
| 13 | latest news | What happened in open-source speech AI in the last month? |
| 14 | fact checking | Is it true that Kyutai STT has a half-second delay? |
| 15 | fact checking | Does Tavily charge more for advanced search than basic? |
| 16 | multi-part | Compare Kyutai with recent low-resource streaming ASR approaches and say what applies to Darija. |
| 17 | multi-part | Find what NVIDIA released around agentic models, explain whether Nemotron 3.5 Lightning targets tool-using agents, and cite the original sources. |
| 18 | multi-part | What quantization formats exist for Kyutai STT, and what does each cost in memory? |
| 19 | simple lookup | Who makes the Mimi audio codec? |
| 20 | simple lookup | What licence is Kyutai STT released under? |

## What to record per run

The CLI already prints all of it, and `--json` stores it:

| Field | Source | What it tells us |
|---|---|---|
| searches | `metrics.searches` | is the model decomposing, or over-searching? |
| turns | `metrics.turns` | is the loop terminating early or grinding to the limit? |
| latency | `go_to_first_answer_token_ms`, `go_to_completed_ms` | does it feel fast enough to speak to? |
| tokens | `input_tokens`, `output_tokens` | cost per request |
| Tavily credits | `tavily_credits` | is the budget policy holding? |
| citation validity | `citations.invalid_citation_ids`, `uncited` | is grounding actually working? |
| usefulness | your judgement | would you have been satisfied? |

## What each category is probing

- **Simple lookups (19, 20)** should use *one* search. More than two means the stopping
  policy is too weak.
- **Multi-part (16–18)** should use several focused searches. One broad search means the
  decomposition guidance in the prompt isn't landing.
- **Fact checking (14, 15)** should notice when sources disagree rather than picking one.
- **Latest news (12, 13)** should use `topic: news` or a `time_range`. If it never does,
  the tool description needs work — not a new tool.
- **Product/docs (6–8)** should cite official documentation. Citing a blog aggregator over
  the vendor's own docs is a retrieval-quality finding.

## Only then

If the failures cluster on *shallow snippets* — the model has the right pages but not
enough text — that is the evidence for adding `read_url` via Tavily Extract in V1.1
(PLAN §15). Any other conclusion needs its own evidence. Do not add tools speculatively.
