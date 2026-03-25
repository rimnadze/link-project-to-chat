# State

## Pending
- `subprocess.Popen` + `asyncio.to_thread` not yet tested end-to-end
- `_proc` on `ClaudeClient` is a single slot — concurrent messages to the same chat could overwrite it
