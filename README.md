# hAI.llama.cppPortainer

Portainer-Stack: **llama.cpp-Server** (GGUF, OpenAI-kompatible API, ohne WebUI) + **Admin-WebUI** (Port 8066) zur Verwaltung der Modelle und Anzeige des Server-Status.

## Architektur

| Service | Image | Port (Host) | Zweck |
|---|---|---|---|
| `llamacpp` | `ghcr.io/ggml-org/llama.cpp:server` (CPU) / `:server-cuda` (NVIDIA) | 8065 | Inferenz-API `/v1/chat/completions` |
| `admin` | `ghcr.io/jbkunama1/hai.llama.cppportainer:latest` | 8066 | Admin-WebUI (Status, Modelle, Downloads) |

Das Admin-Image wird bei jedem Push auf `main` per GitHub Action gebaut und nach **ghcr.io** gepusht (`:latest` + `:sha-<commit>`), Multi-Arch `amd64`/`arm64` (DietPi-tauglich).

## Modell-Aktivierung (Konzept)

`llama-server` laedt fest `/models/current.gguf`. Die Admin-UI legt beim Aktivieren einen **Symlink** `current.gguf -> <gewaehltes-modell>.gguf` im gemeinsamen Volume an und startet den Container per Docker-Socket neu. So funktioniert der Modellwechsel ohne Stack-Aenderung.

## Ersteinrichtung (Portainer)

1. **Stacks → Add stack** → `docker-compose.yml` einfuegen.
2. Env-Variablen setzen (siehe `.env.example`): mindestens `ADMIN_API_KEY` und `LLAMA_API_KEY`.
3. Stack deployen. `llamacpp` startet erst durch, sobald ein Modell aktiv ist (Restart-Loop ist normal).
4. Admin-UI oeffnen: `http://<host>:8066` → Admin-Key eingeben.
5. Tab **Download & Suche**: GGUF-URL einfuegen (z. B. von HuggingFace) oder direkt suchen → downloaden.
6. Tab **Modelle**: Modell **Aktivieren** → Symlink + automatischer Neustart von `llamacpp`.
7. API testen:

```bash
curl http://<host>:8065/v1/chat/completions \
  -H "Authorization: Bearer $LLAMA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hallo!"}]}'
```

## Empfohlenes Modell (CPU, Homelab)

`Qwen/Qwen2.5-7B-Instruct-GGUF` → Datei `qwen2.5-7b-instruct-q4_k_m.gguf` (guter Kompromiss; mit mehr RAM: `q5_k_m`). Fuer Coding: `Qwen/Qwen2.5-Coder-7B-Instruct-GGUF`.

## Admin-API (kurz)

Alle Endpunkte mit Header `X-Admin-Key: <ADMIN_API_KEY>`:

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/status` | llama.cpp-Status: `/health`, `/props`, `/v1/models`, `/slots` |
| GET | `/api/models` | Dateien im Modell-Volume + aktives Modell |
| POST | `/api/models/activate` | `{name}` → Symlink + Neustart |
| POST | `/api/models/delete` | `{name}` loeschen (aktives Modell gesperrt) |
| POST | `/api/models/rename` | `{old,new}` umbenennen |
| POST | `/api/models/note` | `{name,note}` Notiz speichern (JSON) |
| POST | `/api/download` | `{url, filename?}` Hintergrund-Download |
| GET | `/api/downloads` | Fortschritt aller Downloads |
| GET | `/api/hf/search?q=…` | HuggingFace-GGUF-Suche |
| GET | `/api/hf/files?repo=user/name` | GGUF-Dateien eines Repos auflisten |

## Exposition nach außen

Wie gewohnt selbst per Tunnel/Reverse-Proxy (Cloudflare) – der Admin-Container hat keine eigene TLS-Terminierung. **Wichtig:** `ADMIN_API_KEY` unbedingt setzen, bevor Port 8066 oeffentlich erreichbar ist.

## Hinweise

- GPU: `LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda` + `deploy.resources` GPU-Reservation in der Compose ergaenzen und `LLAMA_N_GPU_LAYERS` hochsetzen (z. B. `99`).
- `RESTART_VIA_DOCKER=false` deaktiviert den Docker-Socket-Zugriff; Neustart dann manuell in Portainer.
- Metadaten liegen schlicht in `/config/models.json` (Volume `llama-config`).
