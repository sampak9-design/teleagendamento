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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(verificar_e_enviar_posts, "interval", minutes=1, id="check_posts")
    scheduler.start()
    print("[SCHEDULER] Iniciado — verificando posts a cada minuto")
    yield
    scheduler.shutdown()

app       = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Telegram ──────────────────────────────────────────────────────
async def enviar_telegram(chat_id: str, texto: str, tipo: str = "text", arquivo_url: str = None):
    base = f"https://api.telegram.org/bot{get_bot_token()}"
    async with httpx.AsyncClient() as client:
        if tipo == "photo" and arquivo_url:
            resp = await client.post(f"{base}/sendPhoto",
                json={"chat_id": chat_id, "photo": arquivo_url, "caption": texto, "parse_mode": "HTML"})
        elif tipo == "video" and arquivo_url:
            resp = await client.post(f"{base}/sendVideo",
                json={"chat_id": chat_id, "video": arquivo_url, "caption": texto, "parse_mode": "HTML"})
        elif tipo == "audio" and arquivo_url:
            resp = await client.post(f"{base}/sendAudio",
                json={"chat_id": chat_id, "audio": arquivo_url, "caption": texto, "parse_mode": "HTML"})
        elif tipo == "document" and arquivo_url:
            resp = await client.post(f"{base}/sendDocument",
                json={"chat_id": chat_id, "document": arquivo_url, "caption": texto, "parse_mode": "HTML"})
        else:
            resp = await client.post(f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"})
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
                chat_id = post.get("chat_id") or get_chat_id()
                res = await enviar_telegram(
                    chat_id=chat_id,
                    texto=post.get("texto", ""),
                    tipo=post.get("tipo", "text"),
                    arquivo_url=post.get("arquivo_url"),
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

    # Reação em post do canal
    reaction = update.get("message_reaction")
    if reaction:
        message_id = reaction.get("message_id")
        chat_id = str(reaction.get("chat", {}).get("id", ""))
        new_reactions = reaction.get("new_reaction", [])
        # Conta total de reações acumuladas
        total = len(new_reactions)
        try:
            existing = db.table("canal_posts").select("id,reacoes").eq("message_id", message_id).eq("chat_id", chat_id).execute()
            if existing.data:
                atual = existing.data[0].get("reacoes") or 0
                db.table("canal_posts").update({"reacoes": atual + total}).eq("message_id", message_id).eq("chat_id", chat_id).execute()
                print(f"[REAÇÃO] msg={message_id} total={atual + total}")
        except Exception as e:
            print(f"[REAÇÃO ERRO] {e}")

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
    webhook_url = str(request.base_url).rstrip("/").replace("http://", "https://") + "/telegram/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{get_bot_token()}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["channel_post", "message_reaction", "chat_member"]},
        )
    result = resp.json()
    print(f"[WEBHOOK SETUP] {result}")
    return result


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
        kwargs = {"model": model, "prompt": prompt, "size": size, "n": 1}
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
