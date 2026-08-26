import os
import json
import threading
import urllib.parse
import http.client
import socket
import time

import requests
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

LLAMA_BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://llamacpp:8080").rstrip("/")
LLAMA_API_KEY = os.environ.get("LLAMA_API_KEY", "")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/models.json")
PORT = int(os.environ.get("PORT", "8066"))
LLAMA_CONTAINER_NAME = os.environ.get("LLAMA_CONTAINER_NAME", "llamacpp")
RESTART_VIA_DOCKER = os.environ.get("RESTART_VIA_DOCKER", "true").lower() == "true"
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
ACTIVE_LINK = os.environ.get("ACTIVE_LINK", "current.gguf")

downloads = {}
lock = threading.Lock()


def require_auth():
    if ADMIN_API_KEY and request.headers.get("X-Admin-Key", "") != ADMIN_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    return None


def llama_headers():
    return {"Authorization": f"Bearer {LLAMA_API_KEY}"} if LLAMA_API_KEY else {}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {"active_model": None, "models": {}}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def safe_name(name):
    name = os.path.basename((name or "").strip())
    if not name or name in (".", "..") or name.endswith(".part"):
        raise ValueError("ungueltiger Dateiname")
    return name


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path):
        super().__init__("localhost")
        self.sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)


def docker_restart(name):
    conn = UnixHTTPConnection(DOCKER_SOCKET)
    conn.request("POST", f"/containers/{name}/restart")
    resp = conn.getresponse()
    resp.read()
    if resp.status not in (200, 204):
        raise RuntimeError(f"Docker API Status {resp.status}")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    r = require_auth()
    if r:
        return r
    out = {"llama_base_url": LLAMA_BASE_URL, "active_model": load_config().get("active_model"), "endpoints": {}}
    for name, path in [("health", "/health"), ("props", "/props"), ("models", "/v1/models"), ("slots", "/slots")]:
        try:
            resp = requests.get(LLAMA_BASE_URL + path, headers=llama_headers(), timeout=5)
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500]
            out["endpoints"][name] = {"status_code": resp.status_code, "body": body}
        except Exception as e:
            out["endpoints"][name] = {"error": str(e)}
    return jsonify(out)


@app.get("/api/models")
def list_models():
    r = require_auth()
    if r:
        return r
    cfg = load_config()
    files = []
    if os.path.isdir(MODELS_DIR):
        for fn in sorted(os.listdir(MODELS_DIR)):
            p = os.path.join(MODELS_DIR, fn)
            if os.path.isfile(p) and not fn.endswith(".part") and fn != ACTIVE_LINK:
                st = os.stat(p)
                meta = cfg.get("models", {}).get(fn, {})
                files.append({
                    "name": fn,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "active": cfg.get("active_model") == fn,
                    "note": meta.get("note", ""),
                })
    return jsonify({"models_dir": MODELS_DIR, "active_model": cfg.get("active_model"), "files": files})


@app.post("/api/models/activate")
def activate():
    r = require_auth()
    if r:
        return r
    try:
        name = safe_name((request.json or {}).get("name"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    src = os.path.join(MODELS_DIR, name)
    if not os.path.isfile(src):
        return jsonify({"error": "Datei nicht gefunden"}), 404
    link = os.path.join(MODELS_DIR, ACTIVE_LINK)
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(name, link)
    except Exception as e:
        return jsonify({"error": f"Symlink fehlgeschlagen: {e}"}), 500
    cfg = load_config()
    cfg["active_model"] = name
    cfg.setdefault("models", {}).setdefault(name, {})["activated_at"] = time.time()
    save_config(cfg)
    restarted, err = False, None
    if RESTART_VIA_DOCKER:
        try:
            docker_restart(LLAMA_CONTAINER_NAME)
            restarted = True
        except Exception as e:
            err = str(e)
    return jsonify({
        "ok": True,
        "active_model": name,
        "restarted": restarted,
        "restart_error": err,
        "hint": None if restarted else "Bitte Container 'llamacpp' neu starten (z. B. in Portainer).",
    })


@app.post("/api/models/delete")
def delete_model():
    r = require_auth()
    if r:
        return r
    try:
        name = safe_name((request.json or {}).get("name"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    cfg = load_config()
    if cfg.get("active_model") == name:
        return jsonify({"error": "Aktives Modell kann nicht geloescht werden. Erst anderes Modell aktivieren."}), 409
    p = os.path.join(MODELS_DIR, name)
    if not os.path.isfile(p):
        return jsonify({"error": "Datei nicht gefunden"}), 404
    os.remove(p)
    cfg.get("models", {}).pop(name, None)
    save_config(cfg)
    return jsonify({"ok": True, "deleted": name})


@app.post("/api/models/rename")
def rename_model():
    r = require_auth()
    if r:
        return r
    data = request.json or {}
    try:
        old = safe_name(data.get("old"))
        new = safe_name(data.get("new"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    src = os.path.join(MODELS_DIR, old)
    dst = os.path.join(MODELS_DIR, new)
    if not os.path.isfile(src):
        return jsonify({"error": "Datei nicht gefunden"}), 404
    if os.path.exists(dst):
        return jsonify({"error": "Zieldatei existiert bereits"}), 409
    os.rename(src, dst)
    cfg = load_config()
    if cfg.get("active_model") == old:
        cfg["active_model"] = new
        link = os.path.join(MODELS_DIR, ACTIVE_LINK)
        try:
            if os.path.islink(link):
                os.remove(link)
            os.symlink(new, link)
        except Exception:
            pass
    if old in cfg.get("models", {}):
        cfg["models"][new] = cfg["models"].pop(old)
    save_config(cfg)
    return jsonify({"ok": True, "old": old, "new": new})


@app.post("/api/models/note")
def set_note():
    r = require_auth()
    if r:
        return r
    data = request.json or {}
    try:
        name = safe_name(data.get("name"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    cfg = load_config()
    cfg.setdefault("models", {}).setdefault(name, {})["note"] = (data.get("note") or "")[:500]
    save_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/download")
def start_download():
    r = require_auth()
    if r:
        return r
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Nur http/https-URLs erlaubt"}), 400
    try:
        fname = safe_name(data.get("filename") or os.path.basename(urllib.parse.urlparse(url).path) or "model.gguf")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    dest = os.path.join(MODELS_DIR, fname)
    if os.path.exists(dest):
        return jsonify({"error": "Datei existiert bereits"}), 409
    did = str(int(time.time() * 1000))
    with lock:
        downloads[did] = {"id": did, "filename": fname, "url": url, "downloaded": 0, "total": None, "status": "running", "error": None}
    threading.Thread(target=_download_worker, args=(did, url, dest), daemon=True).start()
    return jsonify({"ok": True, "id": did})


def _download_worker(did, url, dest):
    tmp = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=60, allow_redirects=True) as resp:
            resp.raise_for_status()
            total = resp.headers.get("Content-Length")
            with lock:
                downloads[did]["total"] = int(total) if total else None
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        with lock:
                            downloads[did]["downloaded"] += len(chunk)
        os.replace(tmp, dest)
        with lock:
            downloads[did]["status"] = "done"
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        with lock:
            downloads[did]["status"] = "error"
            downloads[did]["error"] = str(e)


@app.get("/api/downloads")
def list_downloads():
    r = require_auth()
    if r:
        return r
    with lock:
        return jsonify({"downloads": list(downloads.values())})


@app.get("/api/hf/search")
def hf_search():
    r = require_auth()
    if r:
        return r
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q fehlt"}), 400
    try:
        resp = requests.get(
            "https://huggingface.co/api/models",
            params={"search": q, "filter": "gguf", "limit": 20, "sort": "downloads", "direction": -1},
            timeout=15,
        )
        resp.raise_for_status()
        items = [{"id": m.get("id"), "downloads": m.get("downloads"), "likes": m.get("likes"), "author": m.get("author")} for m in resp.json()]
        return jsonify({"results": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/hf/files")
def hf_files():
    r = require_auth()
    if r:
        return r
    repo = (request.args.get("repo") or "").strip().strip("/")
    if "/" not in repo:
        return jsonify({"error": "repo muss im Format user/name sein"}), 400
    try:
        resp = requests.get(f"https://huggingface.co/api/models/{repo}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        files = []
        for s in data.get("siblings", []):
            fn = s.get("rfilename", "")
            if fn.endswith(".gguf"):
                files.append({"name": fn, "url": f"https://huggingface.co/{repo}/resolve/main/{fn}"})
        return jsonify({"repo": repo, "files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "auth_required": bool(ADMIN_API_KEY)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
