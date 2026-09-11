import asyncio
import httpx

from forge import providers


def _run(conn, model, body, seen):
    def handler(request):
        assert request.method == 'POST'
        payload = request.read().decode()
        assert '"stream":true' in payload
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)

    async def collect(item):
        seen.append(item)

    return asyncio.run(providers.complete(
        conn, model, [{'role': 'user', 'content': 'hello'}], [], 'test-key', 10,
        transport=httpx.MockTransport(handler), on_delta=collect))


def test_openai_chat_stream_emits_real_text_deltas_and_final_reply():
    seen = []
    body = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":2}}\n\n'
            b'data: [DONE]\n\n')
    reply = _run(
        {'base_url': 'https://example.test/v1', 'protocol': 'openai_chat', 'external': True},
        {'model_id': 'demo', 'allowed_parameters': [], 'parameters': {}}, body, seen)
    assert reply.text == 'Hello'
    assert ''.join(item['text'] for item in seen) == 'Hello'
    assert [item['sequence'] for item in seen] == [1, 2]
    assert reply.input_tokens == 2 and reply.output_tokens == 2


def test_anthropic_stream_emits_only_upstream_text_not_heartbeat():
    seen = []
    body = (b'event: message_start\n'
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","content_block":{"type":"text","text":""}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"A"}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"B"}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n')
    reply = _run(
        {'base_url': 'https://example.test/v1', 'protocol': 'anthropic_messages', 'external': True},
        {'model_id': 'demo', 'allowed_parameters': ['max_tokens'], 'parameters': {'max_tokens': 32}}, body, seen)
    assert reply.text == 'AB'
    assert [item['protocol'] for item in seen] == ['anthropic_messages', 'anthropic_messages']
