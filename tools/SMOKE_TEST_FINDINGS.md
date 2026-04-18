# Smoke Test Findings

## Setup used
- Client: `tools/proxy_chat.py` (httpx POST to `{ANTHROPIC_BASE_URL}/chat/completions`).
- Key / base URL: `.env` (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, optional `LLM_MODEL`).
- Audit pipeline: `tools/llm_kb_audit.py`.

### Quick proxy check (curl)

Load env vars (shell):

```bash
set -a && source .env && set +a
curl -sS "${ANTHROPIC_BASE_URL}/chat/completions" \
  -H "Authorization: Bearer ${ANTHROPIC_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"Hi"}],"temperature":0,"stream":false}'
```

Expect HTTP 200 and JSON with `choices[0].message.content`.

## Results
- `claude-sonnet-4-6`: works on current key/proxy.
- `claude-opus-4-1`: works on current key/proxy.
- GPT models tested (`gpt-5.4`, `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o`, `gpt-4o-mini`): failed on current key/proxy with server errors.

## Conclusion
- Integration code is working.
- Current proxy/key route appears to allow tested Claude models.
- Current proxy/key route appears to reject tested GPT models.
- To use GPT for this project, proxy routing or key entitlements must be updated.
