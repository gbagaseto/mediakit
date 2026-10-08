"""Publica a pasta site/ no Netlify, já com os números mais recentes escritos no HTML.

Roda uma vez por semana e sempre que algo dentro de site/ muda
(.github/workflows/publicar.yml). Publicar todo dia gastaria créditos demais do
plano grátis; no dia a dia o site busca os números novos sozinho no GitHub.
"""

import io
import json
import os
import re
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

NETLIFY_SITE = "gbagaseto"  # nome do site (o que vem antes de .netlify.app)
RAIZ = Path(__file__).resolve().parent.parent
SITE = RAIZ / "site"
MESES = "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro".split()


# ── mesmas regras de formatação do JavaScript do site ──


def fmt_num(n, casas):
    texto = f"{n:,.{casas}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    if casas:
        texto = texto.rstrip("0").rstrip(",")
    return texto


def partes(n, formato):
    if formato == "pct":
        return fmt_num(n, 1), "%"
    if n >= 1e6:
        return fmt_num(n / 1e6, 1), "mi"
    if n >= 1e4:
        return fmt_num(n / 1e3, 0 if n >= 1e5 else 1), "mil"
    return fmt_num(n, 0), ""


def curto(n):
    if n >= 1e6:
        return fmt_num(n / 1e6, 1) + "M"
    if n >= 1e3:
        return fmt_num(n / 1e3, 0 if n >= 1e5 else 1) + "K"
    return str(n)


def pegar(dados, caminho):
    for chave in caminho.split("."):
        dados = dados.get(chave) if isinstance(dados, dict) else None
    return dados


CONTEUDO = r"((?:[^<]|<(?P<t>span|small)[^>]*>[^<]*</(?P=t)>)*)"


def escrever_numeros(html, dados):
    def bloco(m):
        tag = m.group(0)
        if pegar(dados, m.group(1)) is not None:
            tag = tag.replace(" hidden>", ">")
        return tag

    html = re.sub(r'<[^>]*data-bloco="([^"]+)"[^>]*>', bloco, html)

    def sem(m):
        tag = m.group(0)
        if pegar(dados, m.group(1)) is not None and " hidden" not in tag:
            tag = tag[:-1] + " hidden>"
        return tag

    html = re.sub(r'<[^>]*data-sem="([^"]+)"[^>]*>', sem, html)

    def metrica(m):
        abre, valor = m.group(1), pegar(dados, m.group(2))
        if valor is None:
            return m.group(0)
        formato = re.search(r'data-formato="([^"]+)"', abre)
        num, unidade = partes(valor, formato and formato.group(1))
        if not unidade:
            corpo = num
        elif 'data-estilo="grande"' in abre:
            corpo = f"{num}<span>{unidade}</span>"
        else:
            corpo = f"{num}<small>{unidade}</small>"
        return f"{abre}{corpo}</span>"

    html = re.sub(
        r'(<span[^>]*data-metrica="([^"]+)"[^>]*>)' + CONTEUDO + "</span>", metrica, html
    )

    videos = (dados.get("tiktok") or {}).get("videos", {})

    def video(m):
        v = videos.get(m.group(2))
        return f"{m.group(1)}▶ {curto(v['views'])}" if v else m.group(0)

    html = re.sub(r'(<span[^>]*data-video="(\d+)"[^>]*>)[^<]*', video, html)

    def video_metrica(m):
        vid, campo = m.group(2).split(".")
        valor = videos.get(vid, {}).get(campo)
        return f"{m.group(1)}{curto(valor)}" if valor is not None else m.group(0)

    html = re.sub(r'(<[^>]*data-video-metrica="([^"]+)"[^>]*>)[^<]*', video_metrica, html)

    quando = datetime.fromisoformat(dados["atualizado_em"])
    data = f"{quando.day} de {MESES[quando.month - 1]} de {quando.year}"
    return re.sub(
        r"(<span[^>]*data-atualizado[^>]*>)[^<]*",
        rf"\g<1>números atualizados automaticamente · {data}",
        html,
    )


# ── montar e enviar ──


def netlify(caminho, metodo="GET", corpo=None, tipo=None):
    req = urllib.request.Request(
        f"https://api.netlify.com/api/v1{caminho}",
        data=corpo,
        method=metodo,
        headers={
            "Authorization": f"Bearer {os.environ['NETLIFY_TOKEN']}",
            **({"Content-Type": tipo} if tipo else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.loads(res.read())


dados = json.loads((RAIZ / "dados.json").read_text())
html = escrever_numeros((SITE / "index.html").read_text(encoding="utf-8"), dados)

pacote = io.BytesIO()
with zipfile.ZipFile(pacote, "w", zipfile.ZIP_DEFLATED) as z:
    for arquivo in SITE.rglob("*"):
        if arquivo.is_file() and arquivo.name != "index.html" and not arquivo.name.startswith("."):
            z.write(arquivo, arquivo.relative_to(SITE).as_posix())
    z.writestr("index.html", html)
    z.writestr("dados.json", json.dumps(dados, ensure_ascii=False))

sites = netlify(f"/sites?name={NETLIFY_SITE}&filter=all")
site = next((s for s in sites if s["name"] == NETLIFY_SITE), None)
if not site:
    raise SystemExit(f"Não achei o site '{NETLIFY_SITE}' na sua conta do Netlify.")

deploy = netlify(f"/sites/{site['id']}/deploys", "POST", pacote.getvalue(), "application/zip")
print(f"Publicado! {site['url']} (deploy {deploy['id']})")
