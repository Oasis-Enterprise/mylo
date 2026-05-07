# Running Mylo as a Standalone Container

Mylo is distributed as a Home Assistant add-on image, but it runs perfectly
well as a standalone container — no HAOS, no Supervisor required. This guide
covers everything needed to deploy it in a standard container environment,
including Kubernetes.

---

## How Mylo Works Without the Supervisor

Inside the add-on image, Mylo is a Python (aiohttp) web server. At startup it:

1. Reads `/data/options.json` for its configuration.
2. Reads `SUPERVISOR_TOKEN` from the environment as its Home Assistant
   authentication credential.
3. Connects to Home Assistant via WebSocket at the URL in `HA_URL`.
4. Mounts the HA config directory (used for memory, notes, and scratchpad
   storage) at `/config` or `/homeassistant`.
5. Serves its own UI on port `8099`.

None of these steps require the Supervisor. The Supervisor's only roles are
writing `options.json` and proxying the ingress — both of which you replace
yourself.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Home Assistant long-lived access token | Profile → Security → Long-Lived Access Tokens |
| HA WebSocket reachable from the container | e.g. `http://<ha-host>:8123` |
| HA config directory accessible | NFS, hostPath, or PVC — same path HA uses |
| LLM backend | Anthropic API key, OpenAI API key, or self-hosted Ollama/Open-WebUI |

---

## Configuration

Mylo's full configuration lives in `/data/options.json`. There is no other
config file. The schema:

```json
{
  "llm_provider": "openai",
  "api_key": "<your-llm-api-key>",
  "model": "gpt-4o",
  "reconciliation_model": "gpt-4o",
  "ollama_url": "",
  "log_level": "INFO",
  "sync_frequency": "nightly",
  "memory_token_limit": 8000,
  "proactive_notifications": true,
  "max_daily_notifications": 3,
  "quiet_hours_start": "22:00",
  "quiet_hours_end": "07:00",
  "session_budget_usd": 0.50,
  "monthly_budget_usd": 15.00
}
```

**`llm_provider`** — one of `anthropic`, `openai`, `gemini`, or `ollama`.

**`api_key`** — required for `anthropic`, `openai`, and `gemini` providers.
This field must be non-empty; an empty string is treated as unset and the
provider will not initialise. For `ollama`, leave blank.

**`ollama_url`** — only used when `llm_provider` is `ollama`. Set to the base
URL of your Ollama instance (e.g. `http://ollama:11434`). When using
Open-WebUI as an Ollama proxy, use the `/ollama` passthrough path:
`https://your-openwebui-host/ollama`.

**`model`** — the model identifier exactly as your LLM backend reports it.
For Open-WebUI's OpenAI-compatible endpoint, verify the exact ID via
`GET /api/models` — it will not fall back gracefully on a mismatch.

### Provider-Specific Notes

**Anthropic** — set `llm_provider: "anthropic"` and provide your Anthropic
API key in `api_key`. No URL configuration needed.

**OpenAI or OpenAI-compatible (Open-WebUI)** — set `llm_provider: "openai"`.
Mylo does not read `OPENAI_BASE_URL` through its config loader; the env var
is picked up automatically by the OpenAI SDK only because Mylo does not pass
an explicit `base_url` to the client constructor. To use a custom endpoint,
set `OPENAI_BASE_URL` in the container environment to the full base URL
**including `/v1`** (e.g. `https://your-openwebui-host/api/v1`). The API key
in `options.json` is still required and must be non-empty — the env var
`OPENAI_API_KEY` is checked first, but `options.json` `api_key` is the
fallback and an empty string there will not trigger the env var fallback.

**Ollama (direct)** — set `llm_provider: "ollama"` and `ollama_url` to your
Ollama base URL. No API key needed. This is the simplest path for self-hosted
inference.

**Ollama via Open-WebUI** — use `llm_provider: "ollama"` with
`ollama_url: "https://your-openwebui-host/ollama"`. Open-WebUI proxies Ollama
at `/ollama` without requiring authentication from within your network. This
avoids the OpenAI-compat layer entirely and is the most reliable path for
self-hosted Open-WebUI deployments.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SUPERVISOR_TOKEN` | Yes | HA long-lived access token |
| `HA_URL` | Yes | HA base URL, e.g. `http://homeassistant:8123` |
| `MYLO_PORT` | No | Port to serve on (default: `8099`) |
| `MYLO_LOG_LEVEL` | No | Log level (default: `INFO`) |
| `OPENAI_BASE_URL` | Conditional | Required when using a custom OpenAI-compat endpoint |
| `OPENAI_API_KEY` | No | Checked before `options.json` `api_key` for OpenAI provider |
| `ANTHROPIC_API_KEY` | No | Checked before `options.json` `api_key` for Anthropic provider |
| `SSL_CERT_FILE` | No | Path to CA bundle for TLS verification (Python ssl) |
| `HTTPX_SSL_CERT_FILE` | No | Path to CA bundle for httpx (used by some HA calls) |
| `REQUESTS_CA_BUNDLE` | No | Path to CA bundle for requests library |

If your HA or LLM endpoint uses a private CA (e.g. FreeIPA/Let's Encrypt
internal), all three CA bundle variables should point to a merged bundle
containing both the system CAs and your private CA.

---

## HA Config Directory

Mylo stores its memory, notes, scratchpad, conversation history, and audit
log under a `.mylo/` subdirectory inside the HA config directory. It checks
for the config dir in this order:

1. `MYLO_CONFIG_DIR` env var (if set)
2. `/homeassistant` (if it exists)
3. `/config` (legacy fallback)
4. `/homeassistant` (default if neither exists)

Mount your HA config volume at `/config` or `/homeassistant` — whichever path
you prefer. The container does not need write access to the HA config files
themselves, only to the `.mylo/` subdirectory it creates within them.

---

## Docker Compose Example

```yaml
services:
  mylo:
    image: ghcr.io/oasis-enterprise/amd64-addon-mylo:1.0.6
    restart: unless-stopped
    ports:
      - "8099:8099"
    environment:
      SUPERVISOR_TOKEN: "<ha-long-lived-token>"
      HA_URL: "http://homeassistant:8123"
      MYLO_PORT: "8099"
      MYLO_LOG_LEVEL: "INFO"
    volumes:
      - ./options.json:/data/options.json:ro
      - ha-config:/config
    networks:
      - ha-net

networks:
  ha-net:
    external: true

volumes:
  ha-config:
    external: true
```

---

## Kubernetes Example

The following shows a production-grade deployment with secrets management,
a private CA, and an NFS-backed HA config volume.

### Secret

Store sensitive values — the HA token and LLM API key — in a Kubernetes
Secret rather than in the ConfigMap.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mylo-secrets
  namespace: mylo
type: Opaque
stringData:
  ha-token: "<ha-long-lived-token>"
  llm-api-key: "<llm-api-key>"
```

### ConfigMap

The `options.json` ConfigMap uses `__API_KEY__` as a placeholder. An init
container substitutes the real value from the Secret at startup, writing the
rendered file to an `emptyDir`. This keeps the API key out of ConfigMap
plaintext while avoiding the need for a custom entrypoint script in the image.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mylo-options
  namespace: mylo
data:
  options.json: |
    {
      "llm_provider": "ollama",
      "api_key": "__API_KEY__",
      "ollama_url": "https://your-openwebui-host/ollama",
      "model": "your-model-name",
      "reconciliation_model": "your-model-name",
      "log_level": "INFO",
      "sync_frequency": "nightly",
      "memory_token_limit": 8000,
      "proactive_notifications": true,
      "max_daily_notifications": 3,
      "quiet_hours_start": "22:00",
      "quiet_hours_end": "07:00",
      "session_budget_usd": 0.50,
      "monthly_budget_usd": 15.00
    }
```

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mylo
  namespace: mylo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mylo
  template:
    metadata:
      labels:
        app: mylo
    spec:
      initContainers:
        # Optional: merge a private CA into the system bundle.
        # Remove if you don't use a private CA.
        - name: inject-ca
          image: alpine:latest
          command: ["/bin/sh", "-c"]
          args:
            - "cat /etc/ssl/certs/ca-certificates.crt /private-ca/ca.crt > /shared/ca-certificates.crt"
          volumeMounts:
            - name: private-ca
              mountPath: /private-ca
            - name: ca-bundle
              mountPath: /shared

        # Render options.json with the real API key from the Secret.
        - name: write-options
          image: alpine:latest
          command: ["/bin/sh", "-c"]
          args:
            - |
              sed "s/__API_KEY__/${LLM_API_KEY}/" \
                /config-template/options.json > /data/options.json
          env:
            - name: LLM_API_KEY
              valueFrom:
                secretKeyRef:
                  name: mylo-secrets
                  key: llm-api-key
          volumeMounts:
            - name: options-template
              mountPath: /config-template
            - name: options-writable
              mountPath: /data

      containers:
        - name: mylo
          image: ghcr.io/oasis-enterprise/amd64-addon-mylo:1.0.6
          env:
            - name: SUPERVISOR_TOKEN
              valueFrom:
                secretKeyRef:
                  name: mylo-secrets
                  key: ha-token
            - name: HA_URL
              value: "http://homeassistant.homeassistant.svc.cluster.local:8123"
            - name: MYLO_PORT
              value: "8099"
            - name: MYLO_LOG_LEVEL
              value: "INFO"
            # Only needed for OpenAI-compat provider with a custom endpoint:
            # - name: OPENAI_BASE_URL
            #   value: "https://your-openwebui-host/api/v1"
            # Only needed if using a private CA:
            # - name: SSL_CERT_FILE
            #   value: "/shared-certs/ca-certificates.crt"
            # - name: HTTPX_SSL_CERT_FILE
            #   value: "/shared-certs/ca-certificates.crt"
            # - name: REQUESTS_CA_BUNDLE
            #   value: "/shared-certs/ca-certificates.crt"
          ports:
            - containerPort: 8099
              name: http
          volumeMounts:
            - name: options-writable
              mountPath: /data/options.json
              subPath: options.json
            - name: ha-config
              mountPath: /config
            # Only needed if using a private CA:
            # - name: ca-bundle
            #   mountPath: /shared-certs
            #   readOnly: true
          resources:
            requests:
              memory: 256Mi
            limits:
              memory: 1Gi

      volumes:
        - name: options-template
          configMap:
            name: mylo-options
        - name: options-writable
          emptyDir: {}
        - name: ha-config
          nfs:
            server: <nfs-server>
            path: /path/to/ha-config
        # Only needed if using a private CA:
        # - name: private-ca
        #   configMap:
        #     name: my-private-ca
        # - name: ca-bundle
        #   emptyDir: {}
```

---

## Known Behaviours and Gotchas

**`api_key` must be non-empty in `options.json`.**
Mylo's config loader treats an empty string the same as a missing key and
returns the default — also an empty string. An explicitly empty `api_key`
passed to the OpenAI SDK constructor overrides the `OPENAI_API_KEY` env var;
the SDK does not fall back to the env var when an explicit value (even empty)
is provided. Always provide a non-empty value, either the real key or a
placeholder substituted at runtime by an init container.

**`OPENAI_BASE_URL` must include `/v1`.**
The OpenAI SDK appends `/chat/completions` to the base URL directly. If your
endpoint is at `/api/v1/chat/completions`, set `OPENAI_BASE_URL` to
`https://your-host/api/v1`, not `https://your-host/api`.

**Model IDs must match exactly.**
Verify the model ID via your backend's `/models` endpoint. Open-WebUI may
expose models with or without a provider prefix depending on its version and
configuration. A mismatched model ID results in a null response from the SDK
rather than a clear error.

**Conversation history corruption on turn failure.**
If a chat turn fails after the user message has been appended to history but
before the assistant reply is stored, the history will contain an orphaned
user message with no corresponding assistant response. Subsequent turns replay
this malformed history, which causes the LLM backend to return null responses,
producing a persistent `AttributeError: 'NoneType' object has no attribute
'choices'` on every subsequent turn. The fix is to clear the conversation
history from the UI or via the `/api/conversation/clear` endpoint, then start
a new conversation.

**HA config directory must be writable.**
Mylo creates `.mylo/` under the HA config dir at startup for its database,
memory files, and audit log. If the volume is read-only or the container UID
lacks write permission, startup will fail silently or Mylo will run without
persistence.

**Single replica only.**
Mylo holds an active WebSocket connection to HA and maintains an in-memory
conversation state. Running more than one replica will result in split state
and competing WebSocket connections. Keep `replicas: 1`.
