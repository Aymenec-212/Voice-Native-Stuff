# Terminal demo: speech, search, evidence

This is a developer release run from a source checkout, not a signed standalone installer.
Use the README setup on Apple Silicon. Start with `uv run vnr doctor`; it checks installed
modules and key presence without making paid calls, opening the mic or allocating the model.
A successful check does not establish microphone permission, valid credentials or quality.

## Rehearsal before recording

1. Run `uv run vnr ask --list-models` to confirm your configured model is available.
2. Start `uv run vnr serve` in a second terminal. Keep `.env` and debug output off camera.
3. Start `uv run vnr voice`. Allow terminal microphone access and wait for **Listening**.
   Say a short request, stop with Enter, deliberately correct a word in the editable GO line,
   then approve. Confirm the corrected request—not the original transcript—is researched.
4. Open one cited source and confirm the exact claim, date and timezone. Pick a clear,
   bounded question with an authoritative source. Do not prewrite the expected live answer.
5. Press Enter to ask a second question without restarting the service. Try cancelling an
   edited transcript by clearing it; it must cause no research request.
6. Stop the client, leave the service idle for five minutes, and observe its unload log.
   A later recording should prepare the model again. Cold loading is visible; don't speak
   until Listening. Keep the service terminal available to diagnose failures.

## Suggested video sequence (3–5 minutes)

**Architecture, 20 seconds:** microphone → local MLX ASR → editable transcript → approved
text → Nemotron's bounded loop with one `web_search` tool → cited answer. The model decides
when and what to search. Defaults bound the loop to six turns and four searches. A forced
final synthesis stops it at the budget. No agent framework, shell tool or hidden browsing.

**Voice, 60–90 seconds:** show `vnr serve` and `vnr voice`, the loading/listening states,
live transcript and a deliberate edit. Approve, then show actual queries, result counts,
answer and sources. Read one supported claim aloud. Use `t` only if you want to show the
provider's optional trace; it is not the final answer or a reliability proof. Ask again to
show that the model stays warm and that the previous answer does not cover the transcript.

**Verification, 60 seconds:** run a text query with `vnr ask --save runs "..."`, then
`vnr inspect runs/ACTUAL-FILENAME.json`. Show the query, retrieved snippet and citation
mapping. `[1]` can map to retrieval ID `S3`; they are intentionally different orders.
Open the source URL in a browser and compare the actual page against the claim. For sports,
verify the team/category, competition, event date and kickoff timezone against today's
local date. Fresh time is supplied to the agent automatically, but it can still be wrong.
If evidence conflicts, show the uncertainty or failure instead of presenting it as verified.

**Serving and memory, 40 seconds:** explain that the BF16 checkpoint is downloaded once
and weights are quantized to 8-bit during load. The codec, attention state and runtime add
to the footprint; 8-bit does not mean a 1 GB process limit. Show the configured 128 MiB
Metal cache cap and 300-second idle timeout using `vnr doctor`. Show measured release using:

```bash
uv run python scripts/check_asr_memory.py
```

Run this separately from the service to avoid loading two models. It reports current RSS,
MLX active/cache allocations and the historical peak. The peak is a high-water mark, so it
should not fall on unload. Compare the active/cache and RSS before/after instead. Previous
hardware results are in [the measurement report](reliability-2026-09-21.md); label those as
previous measurements if shown. Don't claim 4-bit transcription quality has been validated.

## Troubleshooting and release limits

- **No service / disconnect:** start `vnr serve`; only one voice/native client can own a
  session. Close the native app or other voice client before retrying.
- **Load failure:** inspect the service terminal; verify network access for first download,
  available disk/memory and the MLX extras. A health response alone does not load the model.
- **Silent mic:** check macOS microphone permission and input device. ASR supports this
  checkpoint's English/French languages; other languages aren't a shipping claim.
- **Missing or rejected keys:** edit `.env`, restart the service, retry. Local ASR remains
  available without keys. Keychain integration remains deferred.
- **Search failure / unsupported answer:** retry or narrow the query. Citations and model
  confidence alone do not demonstrate correctness; manually inspect primary evidence.
- **Saving:** `vnr ask --save` includes evidence and reasoning; voice does not yet export
  a session. Files are local, but avoid sharing private queries/snippets accidentally.
- **Validation boundary:** automated tests use simulated microphone/transport. Prior real
  hardware memory and five-minute WAV tests passed; a five-minute live-microphone run and
  testing on other Macs remain release checks, not completed claims.
