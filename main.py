from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
import openai
import uvicorn
import httpx
import json
import base64
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")

db        = create_client(SUPABASE_URL, SUPABASE_KEY)
ai        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
oai       = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
scheduler = AsyncIOScheduler()

# ── Auth helpers ───────────────────────────────────────────────────
def get_user_id(request: Request) -> str:
    """Extrai o user_id (sub) do JWT Supabase no header Authorization."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    try:
        token = auth[7:]
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("sub", "")
    except Exception:
        return ""

def require_user(request: Request) -> str:
    uid = get_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return uid

# ── Config helpers ─────────────────────────────────────────────────
def get_cfg(chave: str, default: str = "", user_id: str = "") -> str:
    """Busca config do banco para o usuário; fallback para variável de ambiente."""
    try:
        q = db.table("configuracoes").select("valor").eq("chave", chave)
        if user_id:
            q = q.eq("user_id", user_id)
        else:
            q = q.eq("user_id", "")
        r = q.execute()
        if r.data and r.data[0].get("valor"):
            return r.data[0]["valor"]
    except Exception:
        pass
    return default

def get_bot_token(user_id: str = "") -> str:
    return get_cfg("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN, user_id)

def get_chat_id(user_id: str = "") -> str:
    return get_cfg("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID, user_id)

def get_anthropic_client(user_id: str = ""):
    key = get_cfg("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY, user_id)
    return anthropic.Anthropic(api_key=key)

def get_openai_client(user_id: str = ""):
    key = get_cfg("OPENAI_API_KEY", OPENAI_API_KEY, user_id)
    return openai.OpenAI(api_key=key) if key else None

# URL pública do Railway (disponível como env var)
_railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
_app_url = f"https://{_railway_domain}" if _railway_domain else ""


async def job_keepalive():
    """Ping no próprio app a cada 4 min para evitar sleep do Railway."""
    if not _app_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(f"{_app_url}/health")
            print("[KEEPALIVE] ok")
    except Exception as e:
        print(f"[KEEPALIVE ERRO] {e}")


async def auto_registrar_webhook():
    """Registra o webhook para cada usuário que tem bot token configurado."""
    if not _app_url:
        print("[WEBHOOK AUTO] RAILWAY_PUBLIC_DOMAIN não definido, pulando")
        return
    webhook_url = f"{_app_url}/telegram/webhook"
    allowed = ["channel_post", "message_reaction", "message_reaction_count", "chat_member"]
    try:
        # Busca todos os tokens configurados por usuários
        rows = db.table("configuracoes").select("user_id,valor").eq("chave", "TELEGRAM_BOT_TOKEN").execute()
        tokens_vistos = set()
        for row in (rows.data or []):
            token = row.get("valor", "").strip()
            if not token or token in tokens_vistos:
                continue
            tokens_vistos.add(token)
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{token}/setWebhook",
                        json={"url": webhook_url, "allowed_updates": allowed},
                    )
                print(f"[WEBHOOK AUTO] uid={row['user_id']} → {resp.json().get('description','ok')}")
            except Exception as e:
                print(f"[WEBHOOK AUTO ERRO] uid={row['user_id']}: {e}")
    except Exception as e:
        print(f"[WEBHOOK AUTO ERRO GERAL] {e}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(verificar_e_enviar_posts,  "interval", minutes=1,  id="check_posts")
    scheduler.add_job(job_piloto_automatico,     "interval", minutes=15, id="piloto_auto")
    scheduler.add_job(job_capturar_membros,      "interval", hours=1,    id="capturar_membros")
    scheduler.add_job(job_keepalive,             "interval", minutes=4,  id="keepalive")
    scheduler.start()
    print(f"[SCHEDULER] Iniciado | URL: {_app_url or 'local'}")
    await auto_registrar_webhook()
    yield
    scheduler.shutdown()

app       = FastAPI(lifespan=lifespan)

# buffer em memória dos últimos webhooks recebidos
_webhook_log: list = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health(request: Request):
    global _app_url
    if not _app_url:
        _app_url = str(request.base_url).rstrip("/").replace("http://", "https://")
        print(f"[APP URL] {_app_url}")
    return {"status": "ok"}


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Telegram ──────────────────────────────────────────────────────
async def enviar_telegram(chat_id: str, texto: str, tipo: str = "text",
                          arquivo_url: str = None, cta_botao: str = None, cta_url: str = None,
                          bot_token: str = None):
    token = bot_token or get_bot_token()
    base  = f"https://api.telegram.org/bot{token}"

    markup = None
    if cta_botao and cta_url:
        markup = {"inline_keyboard": [[{"text": cta_botao, "url": cta_url}]]}

    async with httpx.AsyncClient() as client:
        if tipo == "photo" and arquivo_url:
            payload = {"chat_id": chat_id, "photo": arquivo_url, "caption": texto, "parse_mode": "HTML"}
            if markup: payload["reply_markup"] = markup
            resp = await client.post(f"{base}/sendPhoto", json=payload)
        elif tipo == "video" and arquivo_url:
            payload = {"chat_id": chat_id, "video": arquivo_url, "caption": texto, "parse_mode": "HTML"}
            if markup: payload["reply_markup"] = markup
            resp = await client.post(f"{base}/sendVideo", json=payload)
        elif tipo == "audio" and arquivo_url:
            payload = {"chat_id": chat_id, "audio": arquivo_url, "caption": texto, "parse_mode": "HTML"}
            if markup: payload["reply_markup"] = markup
            resp = await client.post(f"{base}/sendAudio", json=payload)
        elif tipo == "document" and arquivo_url:
            payload = {"chat_id": chat_id, "document": arquivo_url, "caption": texto, "parse_mode": "HTML"}
            if markup: payload["reply_markup"] = markup
            resp = await client.post(f"{base}/sendDocument", json=payload)
        else:
            payload = {"chat_id": chat_id, "text": texto, "parse_mode": "HTML"}
            if markup: payload["reply_markup"] = markup
            resp = await client.post(f"{base}/sendMessage", json=payload)
    return resp.json()


# ── Scheduler job ─────────────────────────────────────────────────
async def verificar_e_enviar_posts():
    try:
        agora = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
        result = (db.table("posts_agendados")
                    .select("*")
                    .eq("status", "agendado")
                    .lte("agendado_para", agora + ":59")
                    .execute())
        posts = result.data or []

        for post in posts:
            try:
                uid     = post.get("user_id", "")
                chat_id = post.get("chat_id") or get_chat_id(uid)
                res = await enviar_telegram(
                    chat_id=chat_id,
                    texto=post.get("texto", ""),
                    tipo=post.get("tipo", "text"),
                    arquivo_url=post.get("arquivo_url"),
                    cta_botao=post.get("cta_botao"),
                    cta_url=post.get("cta_url"),
                    bot_token=get_bot_token(uid) if uid else None,
                )

                if res.get("ok"):
                    db.table("posts_agendados").update({
                        "status": "enviado",
                        "enviado_em": datetime.utcnow().isoformat(),
                    }).eq("id", post["id"]).execute()
                    print(f"[POST ✓] Enviado: {post['id']}")

                    # Recorrência
                    recorrencia = post.get("recorrencia", "nenhuma")
                    if recorrencia and recorrencia != "nenhuma":
                        dt = datetime.fromisoformat(post["agendado_para"].replace("Z", ""))
                        if recorrencia == "diaria":
                            novo_dt = dt + timedelta(days=1)
                        elif recorrencia == "semanal":
                            novo_dt = dt + timedelta(weeks=1)
                        elif recorrencia == "mensal":
                            # Adiciona 30 dias como aproximação
                            novo_dt = dt + timedelta(days=30)
                        else:
                            novo_dt = None

                        if novo_dt:
                            db.table("posts_agendados").insert({
                                "texto": post["texto"],
                                "tipo": post["tipo"],
                                "arquivo_url": post.get("arquivo_url"),
                                "chat_id": post.get("chat_id"),
                                "agendado_para": novo_dt.isoformat(),
                                "recorrencia": recorrencia,
                                "status": "agendado",
                            }).execute()
                            print(f"[POST ↺] Recorrência criada para {novo_dt.isoformat()}")
                else:
                    print(f"[POST ✗] {res}")
            except Exception as e:
                print(f"[POST ERRO] {post.get('id')}: {e}")
    except Exception as e:
        print(f"[SCHEDULER ERRO] {e}")




# ── Config ────────────────────────────────────────────────────────
@app.get("/config")
async def get_config(request: Request):
    uid = get_user_id(request)
    return {"chat_id": get_chat_id(uid)}


@app.get("/configuracoes")
async def listar_configuracoes(request: Request):
    uid = require_user(request)
    chaves = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    resultado = {}
    for chave in chaves:
        # Mostra APENAS o que o usuário salvou, sem fallback para env vars
        val = get_cfg(chave, "", uid)
        if val and len(val) > 12 and chave != "TELEGRAM_CHAT_ID":
            val_display = val[:6] + "..." + val[-4:]
        else:
            val_display = val
        resultado[chave] = {"valor_display": val_display, "configurado": bool(val)}
    return resultado


@app.post("/configuracoes")
async def salvar_configuracoes(request: Request):
    uid = require_user(request)
    data = await request.json()
    chaves_permitidas = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    for chave, valor in data.items():
        if chave not in chaves_permitidas:
            continue
        if not valor or not valor.strip():
            continue
        db.table("configuracoes").upsert({
            "user_id": uid, "chave": chave, "valor": valor.strip(),
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    return {"status": "ok"}


# ── Webhook: captura posts, reações e saídas ──────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    # Guarda últimos 20 updates em memória para debug
    _webhook_log.insert(0, {"ts": datetime.utcnow().isoformat(), "keys": list(update.keys()), "raw": update})
    if len(_webhook_log) > 20:
        _webhook_log.pop()

    # Post publicado no canal
    post = update.get("channel_post")
    if post:
        texto = post.get("text") or post.get("caption") or ""
        chat_id = str(post.get("chat", {}).get("id", ""))
        message_id = post.get("message_id")
        data = datetime.utcfromtimestamp(post.get("date", 0)).isoformat()
        try:
            # Descobre o user_id associado a esse chat_id nas configuracoes
            cfg_r = db.table("configuracoes").select("user_id").eq("chave", "TELEGRAM_CHAT_ID").eq("valor", chat_id).execute()
            uid_canal = cfg_r.data[0]["user_id"] if cfg_r.data else ""
            db.table("canal_posts").insert({
                "user_id": uid_canal,
                "message_id": message_id,
                "chat_id": chat_id,
                "texto": texto,
                "data": data,
                "reacoes": 0,
            }).execute()
            print(f"[CANAL POST] Salvo: {texto[:60]}")
        except Exception as e:
            print(f"[CANAL POST ERRO] {e}")

    # Reação individual (grupos/usuários não-anônimos)
    reaction = update.get("message_reaction")
    if reaction:
        message_id = reaction.get("message_id")
        chat_id    = str(reaction.get("chat", {}).get("id", ""))
        new_r = reaction.get("new_reaction", [])
        old_r = reaction.get("old_reaction", [])
        delta = len(new_r) - len(old_r)  # pode ser +1, -1 ou 0
        if delta != 0:
            try:
                existing = db.table("canal_posts").select("id,reacoes").eq("message_id", str(message_id)).eq("chat_id", chat_id).execute()
                if existing.data:
                    novo = max(0, (existing.data[0].get("reacoes") or 0) + delta)
                    db.table("canal_posts").update({"reacoes": novo}).eq("message_id", str(message_id)).eq("chat_id", chat_id).execute()
                    print(f"[REAÇÃO] msg={message_id} delta={delta} novo={novo}")
            except Exception as e:
                print(f"[REAÇÃO ERRO] {e}")

    # Contagem de reações de canal (anônimas — evento principal em canais públicos)
    reaction_count = update.get("message_reaction_count")
    if reaction_count:
        message_id = reaction_count.get("message_id")
        chat_id    = str(reaction_count.get("chat", {}).get("id", ""))
        reactions  = reaction_count.get("reactions", [])
        total      = sum(r.get("total_count", 0) for r in reactions)
        try:
            existing = db.table("canal_posts").select("id").eq("message_id", str(message_id)).eq("chat_id", chat_id).execute()
            if existing.data:
                db.table("canal_posts").update({"reacoes": total}).eq("message_id", str(message_id)).eq("chat_id", chat_id).execute()
                print(f"[REAÇÃO COUNT] msg={message_id} total={total}")
        except Exception as e:
            print(f"[REAÇÃO COUNT ERRO] {e}")

    # Membro saiu do canal
    chat_member = update.get("chat_member")
    if chat_member:
        new_status = chat_member.get("new_chat_member", {}).get("status", "")
        old_status = chat_member.get("old_chat_member", {}).get("status", "")
        saiu = old_status == "member" and new_status in ("left", "kicked")
        if saiu:
            chat_id = str(chat_member.get("chat", {}).get("id", ""))
            saida_user_id = chat_member.get("new_chat_member", {}).get("user", {}).get("id")
            data = datetime.utcnow().isoformat()
            try:
                cfg_r = db.table("configuracoes").select("user_id").eq("chave", "TELEGRAM_CHAT_ID").eq("valor", chat_id).execute()
                uid_canal = cfg_r.data[0]["user_id"] if cfg_r.data else ""
                db.table("canal_saidas").insert({
                    "user_id": uid_canal,
                    "chat_id": chat_id,
                    "saida_user_id": saida_user_id,
                    "data": data,
                }).execute()
                print(f"[SAÍDA] user={saida_user_id}")
            except Exception as e:
                print(f"[SAÍDA ERRO] {e}")

    return {"ok": True}


# ── Setup webhook ─────────────────────────────────────────────────
@app.get("/telegram/setup")
async def telegram_setup(request: Request):
    uid = require_user(request)
    token = get_bot_token(uid)
    if not token:
        raise HTTPException(status_code=400, detail="Bot Token não configurado")
    webhook_url = str(request.base_url).rstrip("/").replace("http://", "https://") + "/telegram/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["channel_post", "message_reaction", "message_reaction_count", "chat_member"]},
        )
    result = resp.json()
    print(f"[WEBHOOK SETUP] uid={uid} {result}")
    return result


@app.get("/debug/bot-test")
async def debug_bot_test(request: Request):
    """Testa o bot: deleta webhook, busca updates diretamente, re-registra."""
    uid   = require_user(request)
    token = get_bot_token(uid)
    if not token:
        raise HTTPException(status_code=400, detail="Bot Token não configurado")

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Info atual do webhook
        wh_info = (await client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")).json()

        # 2. Deleta webhook temporariamente
        await client.post(f"https://api.telegram.org/bot{token}/deleteWebhook", json={"drop_pending_updates": False})

        # 3. Busca updates direto (polling)
        updates_resp = (await client.get(f"https://api.telegram.org/bot{token}/getUpdates?limit=5")).json()

        # 4. Re-registra webhook
        webhook_url = str(request.base_url).rstrip("/").replace("http://", "https://") + "/telegram/webhook"
        re_reg = (await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["channel_post", "message_reaction", "message_reaction_count", "chat_member"]},
        )).json()

    return {
        "bot_token_inicio": token[:10] + "...",
        "webhook_antes": {
            "url":             wh_info.get("result", {}).get("url"),
            "last_error":      wh_info.get("result", {}).get("last_error_message"),
            "allowed_updates": wh_info.get("result", {}).get("allowed_updates"),
        },
        "updates_direto": updates_resp,
        "webhook_re_registrado": re_reg,
    }


@app.get("/debug/webhook-log")
async def debug_webhook_log(request: Request):
    require_user(request)
    return {"total": len(_webhook_log), "updates": _webhook_log[:10]}


@app.get("/debug/reacoes")
async def debug_reacoes(request: Request):
    uid = require_user(request)
    # Info do webhook no Telegram
    token = get_bot_token(uid)
    webhook_info = {}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
            webhook_info = r.json().get("result", {})
    except Exception as e:
        webhook_info = {"erro": str(e)}

    # Últimos posts com reações no banco
    posts = db.table("canal_posts").select("message_id,texto,reacoes,data").eq("user_id", uid).order("reacoes", desc=True).limit(10).execute().data or []

    return {
        "webhook": {
            "url":              webhook_info.get("url", ""),
            "allowed_updates":  webhook_info.get("allowed_updates", []),
            "pending_updates":  webhook_info.get("pending_update_count", 0),
            "last_error":       webhook_info.get("last_error_message", ""),
            "last_error_date":  webhook_info.get("last_error_date", 0),
        },
        "posts_com_reacoes": [
            {"message_id": p.get("message_id"), "reacoes": p.get("reacoes"), "texto": (p.get("texto") or "")[:60]}
            for p in posts
        ],
        "total_posts_banco": len(db.table("canal_posts").select("id").eq("user_id", uid).execute().data or []),
    }


# ── Canal posts ───────────────────────────────────────────────────
@app.get("/canal/posts")
async def listar_canal_posts(request: Request):
    uid = get_user_id(request)
    try:
        q = db.table("canal_posts").select("*").order("reacoes", desc=True).limit(50)
        if uid:
            q = q.eq("user_id", uid)
        result = q.execute()
        return {"posts": result.data or []}
    except Exception:
        return {"posts": []}


# ── Posts ─────────────────────────────────────────────────────────
@app.get("/posts")
async def listar_posts(request: Request):
    uid = require_user(request)
    try:
        result = (db.table("posts_agendados")
                    .select("*")
                    .eq("user_id", uid)
                    .order("agendado_para", desc=False)
                    .execute())
        return {"posts": result.data or []}
    except Exception as e:
        print(f"[POSTS ERRO] {e}")
        return {"posts": []}


@app.post("/posts")
async def criar_post(request: Request):
    uid = require_user(request)
    data = await request.json()
    texto = data.get("texto", "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="texto é obrigatório")

    registro = {
        "user_id":      uid,
        "texto":        texto,
        "tipo":         data.get("tipo", "text"),
        "arquivo_url":  data.get("arquivo_url") or None,
        "chat_id":      data.get("chat_id") or get_chat_id(uid),
        "agendado_para": data.get("agendado_para"),
        "recorrencia":  data.get("recorrencia", "nenhuma"),
        "status":       "agendado",
    }

    result = db.table("posts_agendados").insert(registro).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Erro ao salvar post")

    print(f"[POST] Agendado para {registro['agendado_para']}: {texto[:50]}")
    return {"status": "ok", "id": result.data[0]["id"]}


@app.delete("/posts/{post_id}")
async def remover_post(post_id: int, request: Request):
    uid = require_user(request)
    try:
        db.table("posts_agendados").delete().eq("id", post_id).eq("user_id", uid).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Stats ─────────────────────────────────────────────────────────
@app.get("/stats")
async def get_stats(request: Request):
    uid = get_user_id(request)
    try:
        q = db.table("posts_agendados").select("*")
        if uid:
            q = q.eq("user_id", uid)
        result = q.execute()
        posts  = result.data or []

        enviados  = [p for p in posts if p.get("status") == "enviado"]
        agendados = [p for p in posts if p.get("status") == "agendado"]

        tipos = {}
        for p in posts:
            t = p.get("tipo", "text")
            tipos[t] = tipos.get(t, 0) + 1

        proximo = next(
            (p for p in sorted(agendados, key=lambda x: x.get("agendado_para") or "")
             if p.get("agendado_para")),
            None
        )

        return {
            "total":     len(posts),
            "enviados":  len(enviados),
            "agendados": len(agendados),
            "tipos":     tipos,
            "proximo":   proximo,
        }
    except Exception as e:
        print(f"[STATS ERRO] {e}")
        return {"total": 0, "enviados": 0, "agendados": 0, "tipos": {}, "proximo": None}


# ── IA: Gerar post ────────────────────────────────────────────────
@app.post("/ia/gerar")
async def ia_gerar(request: Request):
    uid  = require_user(request)
    data = await request.json()
    tema = data.get("tema", "").strip()
    tom  = data.get("tom", "profissional")

    if not tema:
        raise HTTPException(status_code=400, detail="tema é obrigatório")

    prompt = f"""Você é um especialista em criação de conteúdo para Telegram.

Crie um post para Telegram sobre o tema: "{tema}"
Tom de voz: {tom}

Regras:
- Direto e engajante
- Use emojis de forma adequada
- Máximo 300 palavras
- Formatação HTML do Telegram (<b>, <i>, <code> se necessário)

Retorne APENAS o texto do post, sem explicações adicionais."""

    try:
        cliente = get_anthropic_client(uid)
        msg = cliente.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return {"texto": msg.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── IA: Gerar imagem (DALL-E) ─────────────────────────────────────
@app.post("/ia/gerar-imagem")
async def ia_gerar_imagem(request: Request):
    uid     = require_user(request)
    data    = await request.json()
    prompt  = data.get("prompt", "").strip()
    model   = data.get("model", "dall-e-3")
    size    = data.get("size", "1024x1024")
    quality = data.get("quality", "standard")
    style   = data.get("style", "vivid")

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt é obrigatório")
    cliente_oai = get_openai_client(uid)
    if not cliente_oai:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY não configurada")

    try:
        prompt_final = prompt + ". Any text or writing in the image must be in Brazilian Portuguese only."
        kwargs = {"model": model, "prompt": prompt_final, "size": size, "n": 1}
        if model == "dall-e-3":
            kwargs["quality"] = quality
            kwargs["style"] = style
        resp = cliente_oai.images.generate(**kwargs)
        return {"url": resp.data[0].url, "revised_prompt": resp.data[0].revised_prompt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── IA: Análise do canal ──────────────────────────────────────────
@app.get("/ia/analise")
async def ia_analise(request: Request):
    uid = require_user(request)
    try:
        posts = db.table("posts_agendados").select("*").eq("user_id", uid).execute().data or []
        total     = len(posts)
        enviados  = len([p for p in posts if p.get("status") == "enviado"])
        agendados = len([p for p in posts if p.get("status") == "agendado"])

        resumo_agendados = "\n".join([
            f"- [{p.get('tipo','text')}] {(p.get('texto') or '')[:80]} ({p.get('status')})"
            for p in posts[-10:]
        ]) or "Nenhum post agendado ainda."

        # Posts reais do canal + reações
        canal_posts = db.table("canal_posts").select("*").eq("user_id", uid).order("data", desc=True).limit(50).execute().data or []
        top_reacoes = sorted(canal_posts, key=lambda x: x.get("reacoes") or 0, reverse=True)[:5]
        sem_reacao  = [p for p in canal_posts if (p.get("reacoes") or 0) == 0]

        resumo_canal = "\n".join([
            f"- [reações: {p.get('reacoes',0)}] {(p.get('texto') or '')[:100]}"
            for p in canal_posts[:20]
        ]) or "Nenhum post do canal capturado ainda."

        resumo_top = "\n".join([
            f"- [⭐ {p.get('reacoes',0)} reações] {(p.get('texto') or '')[:100]}"
            for p in top_reacoes
        ]) or "Nenhum dado de reações ainda."

        # Saídas do canal
        saidas = db.table("canal_saidas").select("*").eq("user_id", uid).order("data", desc=True).limit(100).execute().data or []
        total_saidas = len(saidas)

        # Correlação: saídas por dia vs posts por dia
        from collections import defaultdict
        saidas_por_dia = defaultdict(int)
        for s in saidas:
            dia = (s.get("data") or "")[:10]
            if dia: saidas_por_dia[dia] += 1

        posts_por_dia = defaultdict(list)
        for p in canal_posts:
            dia = (p.get("data") or "")[:10]
            if dia: posts_por_dia[dia].append((p.get('texto') or '')[:60])

        dias_criticos = sorted(saidas_por_dia.items(), key=lambda x: x[1], reverse=True)[:3]
        correlacao = "\n".join([
            f"- {dia}: {n} saídas | posts publicados: {'; '.join(posts_por_dia.get(dia, ['nenhum']))}"
            for dia, n in dias_criticos
        ]) or "Nenhuma correlação disponível."

        prompt = f"""Você é um especialista em marketing de Telegram com foco em retenção de membros.
Analise os dados abaixo e retorne um JSON com esta estrutura exata:

{{
  "score": <número inteiro de 0 a 100>,
  "pontos_fortes": ["<item1>", "<item2>", "<item3>"],
  "sugestoes": ["<sugestão1>", "<sugestão2>", "<sugestão3>"],
  "posts_que_engajam": ["<descrição do tipo de post que gerou mais reações>"],
  "posts_que_afastam": ["<tipo de post correlacionado com saídas>"],
  "calendario_ideal": ["Segunda: <tipo de post>", "Terça: <tipo de post>", "Quarta: <tipo de post>", "Quinta: <tipo de post>", "Sexta: <tipo de post>", "Sábado: <tipo de post>", "Domingo: <tipo de post>"]
}}

Dados do canal:
- Posts agendados: {total} (enviados: {enviados}, pendentes: {agendados})
- Total de saídas registradas: {total_saidas}

Posts com MAIS reações:
{resumo_top}

Posts sem nenhuma reação: {len(sem_reacao)} posts

Dias com mais saídas e posts publicados nesse dia (correlação):
{correlacao}

Histórico recente de posts:
{resumo_canal}

Retorne APENAS o JSON válido, sem markdown, sem explicações."""

        cliente = get_anthropic_client(uid)
        msg = cliente.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = msg.content[0].text.strip()
        # Remove possível markdown
        if texto.startswith("```"):
            texto = texto.split("```")[1]
            if texto.startswith("json"):
                texto = texto[4:]
        return json.loads(texto)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Captura de membros ────────────────────────────────────────────
async def job_capturar_membros():
    """Roda a cada hora: salva snapshot do total de membros de cada canal."""
    try:
        rows = db.table("configuracoes").select("user_id,valor").eq("chave", "TELEGRAM_CHAT_ID").execute()
        for r in (rows.data or []):
            uid     = r.get("user_id")
            chat_id = r.get("valor")
            if not uid or not chat_id:
                continue
            try:
                token = get_bot_token(uid)
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://api.telegram.org/bot{token}/getChatMemberCount",
                        params={"chat_id": chat_id}
                    )
                data = resp.json()
                if data.get("ok"):
                    total = data["result"]
                    db.table("canal_membros").insert({
                        "user_id": uid,
                        "total_membros": total,
                        "capturado_em": datetime.utcnow().isoformat(),
                    }).execute()
                    print(f"[MEMBROS ✓] user {uid}: {total} membros")
            except Exception as e:
                print(f"[MEMBROS ERRO] user {uid}: {e}")
    except Exception as e:
        print(f"[MEMBROS JOB ERRO] {e}")


@app.post("/analytics/capturar-membros")
async def capturar_membros_agora(request: Request):
    """Captura imediata do total de membros para o usuário autenticado."""
    uid     = require_user(request)
    chat_id = get_chat_id(uid)
    token   = get_bot_token(uid)
    if not chat_id or not token:
        raise HTTPException(status_code=400, detail="Configure Bot Token e Chat ID primeiro")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{token}/getChatMemberCount",
            params={"chat_id": chat_id}
        )
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=data.get("description", "Erro Telegram"))
    total = data["result"]
    db.table("canal_membros").insert({
        "user_id": uid,
        "total_membros": total,
        "capturado_em": datetime.utcnow().isoformat(),
    }).execute()
    return {"total_membros": total}


# ── Relatórios / Analytics ────────────────────────────────────────
@app.get("/analytics")
async def get_analytics(request: Request):
    uid = require_user(request)
    try:
        from collections import defaultdict

        saidas = db.table("canal_saidas").select("data").eq("user_id", uid).limit(1000).execute().data or []
        saidas_por_dia = defaultdict(int)
        for s in saidas:
            dia = (s.get("data") or "")[:10]
            if dia:
                saidas_por_dia[dia] += 1

        canal_posts = db.table("canal_posts").select("*").eq("user_id", uid).order("data", desc=False).limit(1000).execute().data or []
        posts_por_dia   = defaultdict(int)
        reacoes_por_dia = defaultdict(int)
        reacoes_por_hora = defaultdict(lambda: {"reacoes": 0, "posts": 0})

        for p in canal_posts:
            data_str = p.get("data") or ""
            dia = data_str[:10]
            if dia:
                posts_por_dia[dia] += 1
                reacoes_por_dia[dia] += p.get("reacoes") or 0
            # hora do dia (ex: "2024-03-10T14:23:00" → hora 14)
            if len(data_str) >= 13:
                try:
                    hora = int(data_str[11:13])
                    reacoes_por_hora[hora]["reacoes"] += p.get("reacoes") or 0
                    reacoes_por_hora[hora]["posts"]   += 1
                except Exception:
                    pass

        total_reacoes = sum(p.get("reacoes") or 0 for p in canal_posts)
        media_reacoes = total_reacoes / len(canal_posts) if canal_posts else 0
        limiar_viral  = max(media_reacoes * 2, 1)

        top_posts = sorted(canal_posts, key=lambda x: x.get("reacoes") or 0, reverse=True)[:10]
        top_posts_rich = [
            {
                "id":     p.get("id"),
                "texto":  (p.get("texto") or "")[:120],
                "reacoes": p.get("reacoes") or 0,
                "data":   p.get("data") or "",
                "viral":  (p.get("reacoes") or 0) >= limiar_viral,
            }
            for p in top_posts
        ]

        # Melhor hora para postar: hora com maior média de reações
        melhor_hora = None
        melhor_media = -1
        for hora, v in reacoes_por_hora.items():
            if v["posts"] > 0:
                media_h = v["reacoes"] / v["posts"]
                if media_h > melhor_media:
                    melhor_media = media_h
                    melhor_hora  = hora

        # Histórico de membros
        membros_rows = (db.table("canal_membros")
                        .select("capturado_em,total_membros")
                        .eq("user_id", uid)
                        .order("capturado_em", desc=False)
                        .limit(200)
                        .execute().data or [])
        membros_historico = [
            {"data": r["capturado_em"][:10], "total": r["total_membros"]}
            for r in membros_rows
        ]
        total_membros_atual = membros_rows[-1]["total_membros"] if membros_rows else None

        # reacoes_por_hora como dict serializável {hora: media}
        reacoes_hora_serializado = {
            str(h): round(v["reacoes"] / v["posts"], 2) if v["posts"] else 0
            for h, v in sorted(reacoes_por_hora.items())
        }

        return {
            "saidas_por_dia":       dict(sorted(saidas_por_dia.items())),
            "posts_por_dia":        dict(sorted(posts_por_dia.items())),
            "reacoes_por_dia":      dict(sorted(reacoes_por_dia.items())),
            "reacoes_por_hora":     reacoes_hora_serializado,
            "top_posts":            top_posts_rich,
            "membros_historico":    membros_historico,
            "total_membros_atual":  total_membros_atual,
            "total_saidas":         len(saidas),
            "total_posts":          len(canal_posts),
            "total_reacoes":        total_reacoes,
            "media_reacoes":        round(media_reacoes, 2),
            "melhor_hora":          melhor_hora,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Piloto Automático ─────────────────────────────────────────────

async def job_piloto_automatico():
    """Roda a cada 15 min: gera e agenda posts para usuários com piloto ativo."""
    try:
        rows = db.table("configuracoes").select("user_id").eq("chave", "piloto_ativo").eq("valor", "true").execute()
        usuarios = [r["user_id"] for r in (rows.data or []) if r.get("user_id")]
        for uid in usuarios:
            try:
                await _gerar_post_automatico(uid)
            except Exception as e:
                print(f"[PILOTO ERRO] user {uid}: {e}")
    except Exception as e:
        print(f"[PILOTO JOB ERRO] {e}")


async def _gerar_post_automatico(uid: str):
    posts_dia   = int(get_cfg("piloto_posts_dia",    "3",   uid))
    topico      = get_cfg("piloto_topico",           "",    uid)
    estilo      = get_cfg("piloto_estilo",   "engajador",   uid)
    gerar_img   = get_cfg("piloto_imagem",       "false",   uid) == "true"
    h_inicio    = int(get_cfg("piloto_h_inicio",     "8",   uid))
    h_fim       = int(get_cfg("piloto_h_fim",       "22",   uid))
    ultimo      = get_cfg("piloto_ultimo_post",      "",    uid)
    cta_ativo   = get_cfg("piloto_cta_ativo",    "false",   uid) == "true"
    cta_botao   = get_cfg("piloto_cta_botao",        "",    uid)
    cta_url     = get_cfg("piloto_cta_url",          "",    uid)

    if not topico:
        return

    # Checar horário (UTC)
    agora = datetime.utcnow()
    hora_local = agora.hour  # assume UTC; Railway está em UTC
    if not (h_inicio <= hora_local < h_fim):
        return

    # Checar intervalo mínimo entre posts
    intervalo_horas = 24 / max(posts_dia, 1)
    if ultimo:
        try:
            dt_ultimo = datetime.fromisoformat(ultimo.replace("Z", ""))
            if (agora - dt_ultimo).total_seconds() < intervalo_horas * 3600:
                return
        except Exception:
            pass

    # Buscar dados do canal para contexto
    try:
        canal_rows = (db.table("canal_posts").select("texto,reacoes")
                      .eq("user_id", uid).order("capturado_em", desc=True).limit(5).execute())
        contexto_canal = "\n".join(
            f"- {r.get('texto','')[:80]} ({r.get('reacoes',0)} reações)"
            for r in (canal_rows.data or [])
        )
    except Exception:
        contexto_canal = ""

    prompt = f"""Você é um especialista em marketing de conteúdo para Telegram.
Gere UMA postagem para um canal sobre: {topico}
Estilo: {estilo}
{"Posts recentes do canal para referência de tom:\n" + contexto_canal if contexto_canal else ""}
Regras:
- Máximo 280 caracteres
- Use emojis relevantes
- Seja direto e envolvente
- NÃO use markdown, apenas texto puro e emojis
Retorne APENAS o texto do post, sem explicações."""

    ai_client = get_anthropic_client(uid)
    msg = ai_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    texto = msg.content[0].text.strip()

    # Agendar para daqui a 2 minutos
    agendado_para = (agora + timedelta(minutes=2)).isoformat()
    post_data = {
        "texto": texto,
        "tipo": "text",
        "chat_id": get_chat_id(uid),
        "agendado_para": agendado_para,
        "status": "agendado",
        "user_id": uid,
        "recorrencia": "nenhuma",
    }
    if cta_ativo and cta_botao and cta_url:
        post_data["cta_botao"] = cta_botao
        post_data["cta_url"]   = cta_url

    # Gerar imagem se ativado
    if gerar_img:
        try:
            oai_client = get_openai_client(uid)
            if oai_client:
                img_resp = oai_client.images.generate(
                    model="dall-e-3",
                    prompt=f"{topico}: {texto[:200]}. Any text or writing in the image must be in Brazilian Portuguese only.",
                    size="1024x1024", quality="standard", n=1
                )
                post_data["arquivo_url"] = img_resp.data[0].url
                post_data["tipo"] = "photo"
        except Exception as e:
            print(f"[PILOTO IMG ERRO] {e}")

    db.table("posts_agendados").insert(post_data).execute()

    # Salvar log de atividade
    log_atual = get_cfg("piloto_log", "[]", uid)
    try:
        log = json.loads(log_atual)
    except Exception:
        log = []
    log.insert(0, {"ts": agora.isoformat(), "texto": texto[:120], "imagem": post_data.get("tipo") == "photo"})
    log = log[:20]  # manter só os 20 últimos

    db.table("configuracoes").upsert({"user_id": uid, "chave": "piloto_ultimo_post", "valor": agora.isoformat()}).execute()
    db.table("configuracoes").upsert({"user_id": uid, "chave": "piloto_log", "valor": json.dumps(log, ensure_ascii=False)}).execute()
    print(f"[PILOTO ✓] Post gerado para user {uid}: {texto[:60]}")


@app.get("/piloto")
async def piloto_status(request: Request):
    uid = require_user(request)
    chaves = ["piloto_ativo", "piloto_topico", "piloto_estilo", "piloto_posts_dia",
              "piloto_imagem", "piloto_h_inicio", "piloto_h_fim", "piloto_ultimo_post", "piloto_log",
              "piloto_cta_ativo", "piloto_cta_botao", "piloto_cta_url"]
    cfg = {c: get_cfg(c, "", uid) for c in chaves}
    cfg.setdefault("piloto_ativo",     "false")
    cfg.setdefault("piloto_posts_dia", "3")
    cfg.setdefault("piloto_estilo",    "engajador")
    cfg.setdefault("piloto_imagem",    "false")
    cfg.setdefault("piloto_h_inicio",  "8")
    cfg.setdefault("piloto_h_fim",     "22")
    cfg.setdefault("piloto_cta_ativo", "false")
    try:
        cfg["piloto_log"] = json.loads(cfg.get("piloto_log") or "[]")
    except Exception:
        cfg["piloto_log"] = []
    return cfg


@app.post("/piloto")
async def piloto_salvar(request: Request):
    uid = require_user(request)
    body = await request.json()
    permitidos = ["piloto_ativo", "piloto_topico", "piloto_estilo", "piloto_posts_dia",
                  "piloto_imagem", "piloto_h_inicio", "piloto_h_fim",
                  "piloto_cta_ativo", "piloto_cta_botao", "piloto_cta_url"]
    for chave in permitidos:
        if chave in body:
            db.table("configuracoes").upsert(
                {"user_id": uid, "chave": chave, "valor": str(body[chave])}
            ).execute()
    return {"ok": True}


@app.post("/piloto/gerar-agora")
async def piloto_gerar_agora(request: Request):
    uid = require_user(request)
    if get_cfg("piloto_topico", "", uid) == "":
        raise HTTPException(status_code=400, detail="Configure o tópico do canal primeiro")
    # Forçar gerando ao limpar o último post
    db.table("configuracoes").upsert({"user_id": uid, "chave": "piloto_ultimo_post", "valor": ""}).execute()
    await _gerar_post_automatico(uid)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
