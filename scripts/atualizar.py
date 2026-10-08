"""Atualiza dados.json com as métricas do TikTok e do Instagram.

Roda todo dia pelo GitHub Actions (.github/workflows/atualizar.yml).
Se uma plataforma falhar, mantém os últimos números bons e anota o erro.
Só usa a biblioteca padrão do Python — nada para instalar.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TIKTOK_USUARIO = "gbagaseto"
RAIZ = Path(__file__).resolve().parent.parent
SITE_HTML = RAIZ / "site" / "index.html"  # os vídeos em destaque são lidos daqui
ARQUIVO = RAIZ / "dados.json"
DIAS_DE_HISTORICO = 400
POSTS_INSTAGRAM_PARA_MEDIA = 12
POSTS_INSTAGRAM_NO_SITE = 6

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
IG_API = "https://graph.instagram.com/v23.0"

erros = []
agora = datetime.now(timezone.utc)
hoje = agora.date().isoformat()

try:
    anterior = json.loads(ARQUIVO.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    anterior = {}


def baixar(url, cabecalhos=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(cabecalhos or {})})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8")


def arred(n):
    return round(n, 1)


# ───────────────────────── TikTok (dados públicos) ─────────────────────────


def ler_pagina_tiktok(url):
    for tentativa in range(1, 4):
        try:
            html = baixar(url, {"Accept-Language": "pt-BR,pt;q=0.9"})
            m = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                html,
                re.S,
            )
            if m:
                return json.loads(m.group(1))["__DEFAULT_SCOPE__"]
        except Exception:
            pass
        time.sleep(3 * tentativa)
    raise RuntimeError(f"não consegui ler {url}")


def ids_dos_videos_em_destaque():
    html = SITE_HTML.read_text(encoding="utf-8")
    ids = re.findall(rf"tiktok\.com/@{TIKTOK_USUARIO}/(?:video|photo)/(\d+)", html)
    return list(dict.fromkeys(ids))  # sem repetidos, na ordem do site


def coletar_tiktok():
    perfil = ler_pagina_tiktok(f"https://www.tiktok.com/@{TIKTOK_USUARIO}")
    info = perfil.get("webapp.user-detail", {}).get("userInfo", {})
    stats = info.get("statsV2") or info.get("stats") or {}  # statsV2 traz o número exato
    if not stats.get("followerCount"):
        raise RuntimeError("perfil do TikTok veio sem números")

    videos_anteriores = (anterior.get("tiktok") or {}).get("videos", {})
    try:
        ids = ids_dos_videos_em_destaque()
    except Exception as e:
        erros.append(f"TikTok: não li os vídeos do site ({e})")
        ids = list(videos_anteriores)

    videos = {}
    for vid in ids:
        try:
            pagina = ler_pagina_tiktok(
                f"https://www.tiktok.com/@{TIKTOK_USUARIO}/video/{vid}"
            )
            item = pagina.get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct", {})
            s = item.get("statsV2") or item.get("stats") or {}
            if not s.get("playCount"):
                raise RuntimeError("sem números")
            videos[vid] = {
                "views": int(s["playCount"]),
                "curtidas": int(s.get("diggCount", 0)),
                "comentarios": int(s.get("commentCount", 0)),
                "compartilhamentos": int(s.get("shareCount", 0)),
                "salvamentos": int(s.get("collectCount", 0)),
            }
        except Exception as e:
            if vid in videos_anteriores:
                videos[vid] = videos_anteriores[vid]
            erros.append(f"TikTok vídeo {vid}: {e}")

    views = sum(v["views"] for v in videos.values())
    interacoes = sum(
        v["curtidas"] + v["comentarios"] + v["compartilhamentos"] + v["salvamentos"]
        for v in videos.values()
    )
    return {
        "seguidores": int(stats["followerCount"]),
        "curtidas": int(stats.get("heartCount", 0)),
        "total_videos": int(stats.get("videoCount", 0)),
        "views_destaques": views,
        "engajamento_destaques": arred(interacoes / views * 100) if views else None,
        "videos": videos,
        "atualizado_em": agora.isoformat(),
    }


# ───────────────────────── Instagram (API oficial) ─────────────────────────

ig_token = os.environ.get("IG_TOKEN", "").strip()


def ig(caminho, **params):
    params["access_token"] = ig_token
    url = f"{IG_API}{caminho}?{urllib.parse.urlencode(params)}"
    try:
        dados = json.loads(baixar(url))
    except urllib.error.HTTPError as e:
        dados = json.loads(e.read().decode("utf-8"))
    if "error" in dados:
        raise RuntimeError(dados["error"].get("message", "erro da API"))
    return dados


def renovar_token_instagram():
    """Tokens de longa duração valem 60 dias; renovando todo dia eles nunca vencem."""
    global ig_token
    try:
        url = "https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "ig_refresh_token", "access_token": ig_token}
        )
        novo = json.loads(baixar(url)).get("access_token")
        if novo and novo != ig_token:
            print(f"::add-mask::{novo}")
            ig_token = novo
            destino = os.environ.get("NOVO_TOKEN_ARQUIVO")
            if destino:
                Path(destino).write_text(novo)
    except Exception:
        pass  # token com menos de 24h não pode ser renovado; tudo bem


def coletar_instagram():
    renovar_token_instagram()

    perfil = ig("/me", fields="username,followers_count,media_count")
    posts = ig(
        "/me/media",
        fields="id,media_type,media_url,thumbnail_url,permalink,caption,like_count,comments_count,timestamp",
        limit=POSTS_INSTAGRAM_PARA_MEDIA,
    ).get("data", [])

    # Alcance e interações de cada post (precisa da permissão de insights)
    alcance_total = interacoes_total = posts_com_insights = 0
    for post in posts:
        try:
            dados = ig(f"/{post['id']}/insights", metric="reach,total_interactions,views")["data"]
            valor = {d["name"]: d["values"][0]["value"] for d in dados}
            alcance_total += valor.get("reach", 0)
            interacoes_total += valor.get("total_interactions", 0)
            post["views"] = valor.get("views")
            posts_com_insights += 1
        except Exception:
            pass

    # Últimos posts para a vitrine do site
    vitrine = [
        {
            "link": p.get("permalink"),
            "imagem": p.get("thumbnail_url") or p.get("media_url"),
            "tipo": p.get("media_type"),
            "legenda": (p.get("caption") or "").split("\n")[0][:80],
            "curtidas": p.get("like_count"),
            "comentarios": p.get("comments_count"),
            "views": p.get("views"),
        }
        for p in posts[:POSTS_INSTAGRAM_NO_SITE]
        if p.get("permalink")
    ]

    engajamento = None
    if posts_com_insights and alcance_total:
        engajamento = arred(interacoes_total / alcance_total * 100)
    elif posts and perfil.get("followers_count"):
        media = sum(p.get("like_count", 0) + p.get("comments_count", 0) for p in posts) / len(posts)
        engajamento = arred(media / perfil["followers_count"] * 100)

    # Alcance e visualizações da conta nos últimos 28 dias
    alcance_28d = views_28d = None
    try:
        dados = ig(
            "/me/insights",
            metric="reach,views",
            period="day",
            metric_type="total_value",
            since=int((agora - timedelta(days=28)).timestamp()),
            until=int(agora.timestamp()),
        )["data"]
        valor = {d["name"]: d.get("total_value", {}).get("value") for d in dados}
        alcance_28d, views_28d = valor.get("reach"), valor.get("views")
    except Exception as e:
        erros.append(f"Instagram insights da conta: {e}")

    return {
        "seguidores": perfil.get("followers_count"),
        "total_posts": perfil.get("media_count"),
        "alcance_28d": alcance_28d,
        "views_28d": views_28d,
        "engajamento": engajamento,
        "posts": vitrine or None,
        "atualizado_em": agora.isoformat(),
    }


# ───────────────────────── Execução ─────────────────────────


def coletar(nome, funcao):
    try:
        return funcao()
    except Exception as e:
        erros.append(f"{nome}: {e}")
        return anterior.get(nome)  # mantém os últimos números bons


tiktok = coletar("tiktok", coletar_tiktok)
if ig_token:
    instagram = coletar("instagram", coletar_instagram)
else:
    print("Instagram ainda não configurado (falta o segredo IG_TOKEN)")
    instagram = anterior.get("instagram")

historico = [h for h in anterior.get("historico", []) if h["data"] != hoje]
historico.append(
    {
        "data": hoje,
        "tiktok": (tiktok or {}).get("seguidores"),
        "instagram": (instagram or {}).get("seguidores"),
    }
)

dados = {
    "atualizado_em": agora.isoformat(),
    "tiktok": tiktok,
    "instagram": instagram,
    "historico": historico[-DIAS_DE_HISTORICO:],
    "erros": erros,
}
ARQUIVO.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n")

print(
    f"TikTok: {(tiktok or {}).get('seguidores')} seguidores · "
    f"Instagram: {(instagram or {}).get('seguidores')} seguidores"
)
if erros:
    print("Avisos:\n- " + "\n- ".join(erros))
