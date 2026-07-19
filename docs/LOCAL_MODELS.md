# Local models (fully-private AI)

GlucoPilot's AI features — the **Companion**, cross-domain Overview,
pattern/insight narratives, medical **lab-report extraction**, and the **Visit
Report** — can run against either the Anthropic API or a local OpenAI-compatible
server. Choose the provider on the in-app **Settings → AI** page.

For a fully-private setup where no health data leaves your machine, use local
models. Two roles:

| Role | Used for | Needs |
|---|---|---|
| **Default local model** | lab-report extraction, everyday text | must be **vision-capable** (reads document images) |
| **Report model** (optional) | Visit Report narrative only | text-only; a larger model improves prose |

Any OpenAI-compatible server works — [vLLM](https://docs.vllm.ai),
[Ollama](https://ollama.com), LM Studio, llama.cpp's server, etc. Point the
Settings fields at its base URL and model name.

## Reaching a host model from the container

The app runs in Docker. If your model server listens on the host and a firewall
blocks container→host TCP (common with UFW), the simplest fix is a **Unix-socket
proxy** mounted into the container. This repo's compose mounts `./run` at
`/run/glucopilot`, so any socket you place in `./run` is reachable inside.

Example using `systemd-socket-proxyd` (user unit) to expose an Ollama server
listening on `127.0.0.1:11435` as a socket:

```ini
# ~/.config/systemd/user/glucopilot-ollama-proxy.socket
[Socket]
ListenStream=%h/glucopilot/run/ollama.sock
SocketMode=0666
[Install]
WantedBy=sockets.target
```
```ini
# ~/.config/systemd/user/glucopilot-ollama-proxy.service
[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd 127.0.0.1:11435
```

Then set **Report server URL** = `unix:///run/glucopilot/ollama.sock` and
**Report model name** = e.g. `gemma3:27b`. The app auto-appends `/v1`.

Plain `http://host:port` URLs also work if the container can reach them
(e.g. via `host.docker.internal`, already added to compose).

## Graceful loading of a big report model

The report is generated on demand and infrequently, so a large model shouldn't
occupy GPU memory the rest of the time. **Ollama** handles this automatically —
it loads a model on request and unloads it after an idle timeout:

```ini
# ~/.config/systemd/user/glucopilot-ollama.service
[Service]
Environment=OLLAMA_HOST=127.0.0.1:11435
Environment=OLLAMA_KEEP_ALIVE=60s          # unload 60s after last use
Environment=OLLAMA_MAX_LOADED_MODELS=1
# Optional: pin to a specific GPU by UUID (nvidia-smi -L)
# Environment=CUDA_VISIBLE_DEVICES=GPU-xxxxxxxx-....
ExecStart=/usr/local/bin/ollama serve
```
```
ollama pull gemma3:27b
```

With `KEEP_ALIVE=60s`, the report model is resident only around generation
(cold-load ~20–30s the first time, then instant while warm), leaving the GPU
free for other work.

## Notes

- **Lab extraction requires a vision model** as the *default* local model
  (it reads document images). Vision-capable options include Qwen2.5-VL,
  Llama 3.2 Vision, Gemma 3, and LLaVA.
- Prefer clean, aligned instruct models for the medical narrative — avoid
  "abliterated"/uncensored variants, which are a poor fit for careful,
  non-prescriptive clinical summaries.
- If you leave the Report model blank, the report uses the default local model.
