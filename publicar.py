"""Robô do Escalador: roda no GitHub Actions e publica no Instagram os posts da fila que já chegaram no horário.

- queue.json: posts agendados (o site do Escalador escreve aqui)
- accounts.json: contas conectadas, com o token criptografado (chave no secret ESCALADOR_KEY)
- Os vídeos ficam como arquivos da release "videos" e são enviados direto ao Instagram (upload resumable),
  sem precisar de hospedagem externa.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue.json"
ACCOUNTS = ROOT / "accounts.json"
WORK = ROOT / ".tmp"
API_VERSION = "v22.0"
MAX_ATTEMPTS = 3
REFRESH_EVERY = timedelta(days=7)
MAX_PER_RUN = 5

fernet = Fernet(os.environ["ESCALADOR_KEY"].encode())


def now():
    return datetime.now(timezone.utc)


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def graph_host(token):
    # Tokens do "Instagram Login" começam com IG; os do Facebook Login com EAA
    return "https://graph.facebook.com" if token.startswith("EAA") else "https://graph.instagram.com"


def api_error(resp):
    try:
        err = resp.json().get("error", {})
        return f"[{resp.status_code}] {err.get('message') or resp.text[:300]}"
    except ValueError:
        return f"[{resp.status_code}] {resp.text[:300]}"


# ---------- tokens ----------

def refresh_tokens(accounts):
    changed = False
    for name, acc in accounts.items():
        token = fernet.decrypt(acc["token"].encode()).decode()
        last = datetime.fromisoformat(acc.get("refreshed_at") or "2000-01-01T00:00:00+00:00")
        if token.startswith("EAA") or now() - last < REFRESH_EVERY:
            continue
        resp = requests.get("https://graph.instagram.com/refresh_access_token",
                            params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=30)
        if resp.ok and resp.json().get("access_token"):
            acc["token"] = fernet.encrypt(resp.json()["access_token"].encode()).decode()
            acc["refreshed_at"] = now().isoformat()
            acc.pop("token_error", None)
            print(f"[token] @{acc.get('username', name)} renovado")
        else:
            acc["token_error"] = api_error(resp)
            print(f"[token] @{acc.get('username', name)} falhou: {acc['token_error']}")
        changed = True
    return changed


# ---------- publicação ----------

def download_video(asset):
    WORK.mkdir(exist_ok=True)
    out = WORK / asset
    subprocess.run(["gh", "release", "download", "videos", "-p", asset, "-D", str(WORK), "--clobber"],
                   check=True, cwd=ROOT)
    return out


def publish_reel(token, ig_user, video, caption):
    host = graph_host(token)
    # Tokens do Instagram Login aceitam "me"; os de Página do Facebook precisam do ID da conta do Instagram
    ig = "me" if host.endswith("instagram.com") else ig_user
    resp = requests.post(f"{host}/{API_VERSION}/{ig}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true", "access_token": token}, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Criar container: {api_error(resp)}")
    container = resp.json()["id"]
    upload_uri = resp.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{container}"

    data = video.read_bytes()
    resp = requests.post(upload_uri, data=data, timeout=600, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    if not resp.ok:
        raise RuntimeError(f"Enviar vídeo: {api_error(resp)}")

    for _ in range(60):  # até 10 min processando
        time.sleep(10)
        st = requests.get(f"{host}/{API_VERSION}/{container}",
                          params={"fields": "status_code,status", "access_token": token}, timeout=30)
        if not st.ok:
            raise RuntimeError(f"Status: {api_error(st)}")
        code = st.json().get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram recusou o vídeo: {st.json().get('status')}")
    else:
        raise RuntimeError("Instagram demorou demais para processar o vídeo")

    resp = requests.post(f"{host}/{API_VERSION}/{ig}/media_publish",
                         data={"creation_id": container, "access_token": token}, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Publicar: {api_error(resp)}")
    return resp.json()["id"]


# ---------- persistência ----------

def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def persist(post_updates, accounts_changed, accounts):
    """Grava e faz push. Se o site mexeu na fila ao mesmo tempo, recomeça do estado remoto
    e reaplica só os campos que o robô alterou (nada de conflito de merge)."""
    for attempt in range(5):
        queue = load(QUEUE, [])
        for p in queue:
            if p["id"] in post_updates:
                p.update(post_updates[p["id"]])
        save(QUEUE, queue)
        if accounts_changed:
            remote = load(ACCOUNTS, {})
            for name, acc in accounts.items():
                if name in remote:
                    remote[name].update({k: acc[k] for k in ("token", "refreshed_at", "token_error") if k in acc})
                    if "token_error" not in acc:
                        remote[name].pop("token_error", None)
            save(ACCOUNTS, remote)
        git("add", "queue.json", "accounts.json")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        git("commit", "-m", "status da fila [robo]")
        if git("push").returncode == 0:
            return
        git("fetch", "origin")
        git("reset", "--hard", "origin/main")
        time.sleep(2 + attempt)
    raise RuntimeError("Não consegui salvar o status da fila")


def main():
    git("config", "user.name", "Escalador")
    git("config", "user.email", "bot@escalador.local")
    queue = load(QUEUE, [])
    accounts = load(ACCOUNTS, {})
    accounts_changed = refresh_tokens(accounts)
    if accounts_changed:
        persist({}, True, accounts)

    due = [p for p in queue if p["status"] == "pending" and datetime.fromisoformat(
        p["scheduled_at"].replace("Z", "+00:00")) <= now()]
    due.sort(key=lambda p: p["scheduled_at"])
    print(f"{len(due)} post(s) no horário")

    for post in due[:MAX_PER_RUN]:
        acc = accounts.get(post["account"])
        if not acc:
            persist({post["id"]: {"status": "error", "error": "Conta não está mais conectada"}}, False, accounts)
            continue
        upd = {"attempts": post.get("attempts", 0) + 1}
        try:
            token = fernet.decrypt(acc["token"].encode()).decode()
            video = download_video(post["asset"])
            caption = f"{post['caption']}\n\n{post.get('hashtags', '')}".strip()
            upd.update(media_id=publish_reel(token, acc.get("user_id"), video, caption), status="posted",
                       posted_at=now().isoformat(), error=None)
            print(f"[ok] {post['id']} publicado em @{acc.get('username')}")
            video.unlink(missing_ok=True)
        except Exception as e:
            upd["error"] = str(e)[:500]
            if upd["attempts"] >= MAX_ATTEMPTS:
                upd["status"] = "error"
            print(f"[erro] {post['id']}: {e}")
        # Salva logo depois de cada post: se o job cair, não publica duas vezes
        persist({post["id"]: upd}, False, accounts)
        if upd.get("status") == "posted":
            subprocess.run(["gh", "release", "delete-asset", "videos", post["asset"], "-y"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
