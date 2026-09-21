import json

import httpx
import pytest

from tests.test_nebius import client_with
from vnr.research.reasoning import ReasoningSplitter


@pytest.mark.parametrize("cut", range(57))
def test_split_tags_never_enter_the_answer(cut):
    text = "<think>Compare evidence [S9].</think>Answer **here** [S1]."
    splitter = ReasoningSplitter()
    parts = [splitter.feed(text[:cut]), splitter.feed(text[cut:]), splitter.feed("", final=True)]
    assert "".join(a for a, _ in parts) == "Answer **here** [S1]."
    assert "".join(r for _, r in parts) == "Compare evidence [S9]."


def test_character_chunks_and_unclosed_reasoning():
    splitter = ReasoningSplitter()
    parts = [splitter.feed(char) for char in "<think>unfinished"]
    parts.append(splitter.feed("", final=True))
    assert "".join(a for a, _ in parts) == ""
    assert "".join(r for _, r in parts) == "unfinished"


async def test_provider_stream_routes_both_reasoning_formats_and_preserves_usage():
    chunks = [
        {"reasoning_content": "Separate thought. "},
        {"content": "<thi"},
        {"content": "nk>Tagged thought.</th"},
        {"content": "ink>Answer [S1]."},
    ]
    data = "".join(
        "data: " + json.dumps({"choices": [{"delta": chunk}]}) + "\n\n" for chunk in chunks
    )
    data += 'data: {"choices":[],"usage":{"completion_tokens":90}}\n\ndata: [DONE]\n\n'
    async with client_with(lambda _: httpx.Response(200, text=data)) as client:
        deltas = [delta async for delta in client.stream_completion([])]
    assert "".join(d.text for d in deltas) == "Answer [S1]."
    assert "".join(d.reasoning for d in deltas) == "Separate thought. Tagged thought."
    assert deltas[-1].usage["completion_tokens"] == 90


async def test_nonstreaming_tool_turn_retains_separate_reasoning():
    body = {
        "choices": [
            {
                "message": {
                    "content": "<think>Plan</think>Searching",
                    "reasoning_content": "Reason. ",
                }
            }
        ]
    }
    async with client_with(lambda _: httpx.Response(200, json=body)) as client:
        completion = await client.create_completion([])
    assert completion.content == "Searching"
    assert completion.reasoning == "Reason. Plan"
    assert completion.to_assistant_message()["reasoning_content"] == "Reason. Plan"
