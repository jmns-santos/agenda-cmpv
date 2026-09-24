# -*- coding: utf-8 -*-
"""
Agenda Municipal da Póvoa de Varzim em formato iCalendar (.ics).

Lê os eventos publicados na agenda do site www.cm-pvarzim.pt e gera:

    agenda.ics              agenda completa
    temas/<tema>.ics        um calendário por tema
    index.html              página pública com os links de subscrição

Cada evento é arrumado num único tema, a partir de palavras-chave no
título e na descrição (ver TEMAS mais abaixo). Assim, quem subscrever
vários temas não vê eventos repetidos.

Uso:
    python gerar_agenda_ics.py
    python gerar_agenda_ics.py --pasta-saida saida --url-publica https://exemplo.pt/agenda/
"""

import argparse
import base64
import hashlib
import html
import logging
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter, Retry
from icalendar import Calendar, Event, vText

try:
    from zoneinfo import ZoneInfo
    LISBOA = ZoneInfo("Europe/Lisbon")
except Exception:  # Windows sem o pacote tzdata
    LISBOA = timezone.utc

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

FONTE_API = "https://www.cm-pvarzim.pt/wp-json/plone/v1/event"
FONTE_PAGINA = "https://www.cm-pvarzim.pt/eventos/"

# Endereço público onde os ficheiros ficam alojados (termina em "/").
URL_PUBLICA = "https://jmns-santos.github.io/agenda-cmpv/"

# Só são aceites links de eventos que apontem para o site do município.
DOMINIOS_PERMITIDOS = {"www.cm-pvarzim.pt", "cm-pvarzim.pt"}

PASTA_SCRIPT = Path(__file__).resolve().parent
MODELO_PAGINA = PASTA_SCRIPT / "modelo_pagina.html"
PASTA_FONTES = PASTA_SCRIPT / "fontes"

NOME_GERAL = "Agenda Municipal da Póvoa de Varzim"
PREFIXO_TEMA = "Póvoa de Varzim: "
PRODID = "-//Póvoa de Varzim//Agenda Municipal//PT"
DOMINIO_UID = "agenda.cm-pvarzim.pt"  # não alterar: mantém os eventos já subscritos
HORAS_ATUALIZACAO = 4


@dataclass
class Tema:
    slug: str
    nome: str
    resumo: str
    palavras: list = field(default_factory=list)
    cor: str = "#6b7780"


# Ordem da lista = ordem de apresentação na página. Máximo de 5 temas.
# Palavras-chave sem acentos e em minúsculas; cada uma é procurada no início
# de uma palavra ("teatro" apanha "teatros", "cine" apanha "cinema").
# Para exigir a palavra exata, terminar com "\b" (ex. r"correr\b").
TEMAS = [
    Tema("cultura", "Cultura", "Espetáculos, exposições, livros e património", [
        "cinema", "filme", "documentario", "teatro", "concerto", "musica", "musical",
        "opereta", "opera", "orquestra", "banda", "tributo", "fado", "danca", "bailado",
        "espetaculo", "festival", "comedia", "stand up", "circo", "sons no patrimonio",
        "exposi", "mostra", "pintura", "fotografia", "escultura", "galeria", "museu",
        "patrimonio", "historia", "historic", "arqueolog", "visita guiada", "jornadas europeias",
        "livro", "literari", "leitura", "poesia", "conferencia", "palestra", "coloquio",
        "seminario", "tertulia", "congresso",
        "arraial", "romaria", "tradic", "folclore", "cantares", r"festas?\b", "carnaval",
        r"natal\b", "sao pedro(?! de rates)", "geminac",
    ], "#b03a5b"),
    Tema("desporto", "Desporto", "Provas, torneios e atividade física", [
        "desport", "futebol", "futsal", "andebol", "basquet", "voleibol", "hoquei",
        "atletismo", "natacao", "surf", "bodyboard", "kickboxing", "boxe", "karate", "judo",
        "ginastica", "padel", "tenis", "ciclismo", "btt", "regata", "remo", "canoagem",
        "torneio", "campeonato", "championship", "maratona", "corrida", r"correr\b",
        "caminhada", r"caminhar\b", "trail", "fitness", "yoga",
    ], "#2e7d4f"),
    Tema("ambiente-mobilidade", "Ambiente e mobilidade", "Sustentabilidade, energia e transportes", [
        "mobilidade", "transporte", "bicicleta", "ciclovia", "pedonal", "autocarro",
        "estacionamento", "transito", "seguranca rodoviaria",
        "ambient", "energ", "sustentab", "reciclag", "plastico", "residuos", "clima",
        "floresta", "plantac", "biodiversidade", "limpeza de praia", "eco escola",
    ], "#1c7fa6"),
    Tema("turismo-economia", "Turismo e economia", "Turismo, gastronomia, feiras e empresas", [
        "turism", "turistic", "gastronom", "vinho", "vinic", "wine", "feira", "mercado",
        "artesanato", r"empresas?\b", "negocio", "empreend", "emprego",
        "comercio", "economia", "investimento", "startup", "formacao profissional",
    ], "#c0532a"),
    Tema("comunidade-cidadania", "Comunidade e cidadania", "Saúde, família, ação social e vida municipal", [
        "saude", "senior", "idoso", "envelhecimento", "rastreio", "dadiva", "sangue",
        "social", "solidari", "voluntari", "inclusao", "cuidados",
        "bebe", "crianca", "infantil", "infancia", r"familias?\b", "miudos", "pais e filhos",
        "ludoteca", "ferias escolares",
        "assembleia", "sessao solene", "tomada de posse", "reuniao publica", "cerimonia",
        "eleic", "referendo", "cidadania", "participativ", "debate",
    ], "#5b4b9a"),
]

# Tema dos eventos que não correspondem a nenhuma palavra-chave: assim,
# nenhum evento fica fora dos temas (e todos estão sempre na agenda completa).
TEMA_PADRAO = TEMAS[-1]

# Em caso de empate na pontuação, ganha o tema que aparece primeiro aqui.
PRIORIDADE = ["ambiente-mobilidade", "desporto", "cultura", "turismo-economia", "comunidade-cidadania"]

# Campos da API que possam trazer categorias/etiquetas do próprio site.
# Se existirem, contam tanto como o título na classificação.
CAMPOS_CATEGORIA = ("categories", "category", "subjects", "subject", "tags", "event_type", "type")

PESO_TITULO = 3
PESO_CATEGORIA = 3
PESO_DESCRICAO = 1

TAG_RE = re.compile(r"<[^>]+>")
ESPACOS_RE = re.compile(r"\s+")


class FalhaRecolha(RuntimeError):
    """Não foi possível obter dados do site (diferente de 'não há eventos')."""


# ---------------------------------------------------------------------------
# Texto e datas
# ---------------------------------------------------------------------------

def limpar_texto(txt):
    if not txt:
        return ""
    txt = TAG_RE.sub(" ", str(txt))
    txt = html.unescape(txt)
    return ESPACOS_RE.sub(" ", txt).strip()


def normalizar(txt):
    """Minúsculas, sem acentos e sem pontuação, para comparar palavras-chave."""
    txt = unicodedata.normalize("NFKD", txt or "")
    txt = "".join(c for c in txt if not unicodedata.combining(c)).lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", txt) + " "


def parse_iso(valor):
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def eh_dia_inteiro(inicio, fim):
    """Começa à meia-noite (hora de Lisboa) e dura pelo menos ~23h."""
    if inicio is None or fim is None:
        return False
    local = inicio.astimezone(LISBOA)
    return local.hour == 0 and local.minute == 0 and (fim - inicio) >= timedelta(hours=23)


def link_seguro(url):
    """Devolve o link só se for do site do município (e sempre em https)."""
    if not url:
        return ""
    partes = urlsplit(str(url).strip())
    if partes.scheme not in ("http", "https") or partes.hostname not in DOMINIOS_PERMITIDOS:
        return ""
    return partes._replace(scheme="https").geturl()


# ---------------------------------------------------------------------------
# Classificação por tema
# ---------------------------------------------------------------------------

def _compilar(temas):
    padroes = {}
    for tema in temas:
        padroes[tema.slug] = [
            re.compile(r"(?<=[ ])" + p.replace(" ", r"\s+")) for p in tema.palavras
        ]
    return padroes


PADROES = _compilar(TEMAS)
ORDEM_EMPATE = {slug: i for i, slug in enumerate(PRIORIDADE)}


def texto_categorias(item):
    partes = []
    for campo in CAMPOS_CATEGORIA:
        valor = item.get(campo)
        valores = valor if isinstance(valor, list) else [valor]
        for v in valores:
            if isinstance(v, dict):
                v = v.get("title") or v.get("name") or v.get("slug")
            if isinstance(v, str):
                partes.append(v)
    return " ".join(partes)


def classificar(item):
    titulo = normalizar(limpar_texto(item.get("title")))
    categorias = normalizar(texto_categorias(item))
    descricao = normalizar(limpar_texto(item.get("description") or item.get("text")))

    melhor, melhor_pontos = TEMA_PADRAO, 0
    for tema in TEMAS:
        pontos = 0
        for padrao in PADROES[tema.slug]:
            if padrao.search(titulo):
                pontos += PESO_TITULO
            if padrao.search(categorias):
                pontos += PESO_CATEGORIA
            if padrao.search(descricao):
                pontos += PESO_DESCRICAO
        if pontos > melhor_pontos or (
            pontos == melhor_pontos and pontos > 0
            and ORDEM_EMPATE.get(tema.slug, 99) < ORDEM_EMPATE.get(melhor.slug, 99)
        ):
            melhor, melhor_pontos = tema, pontos
    return melhor


# ---------------------------------------------------------------------------
# Recolha
# ---------------------------------------------------------------------------

def obter_sessao():
    sessao = requests.Session()
    retries = Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET"])
    sessao.mount("https://", HTTPAdapter(max_retries=retries))
    sessao.headers.update({"User-Agent": "AgendaMunicipalICS/2.0 (+https://www.cm-pvarzim.pt/eventos/)"})
    return sessao


def recolher(sessao, params_base, hoje, horizonte, eventos, max_paginas, obrigatorio, timeout=20):
    """Percorre as páginas da API e junta a 'eventos' os que ainda não
    terminaram e começam dentro do horizonte. Devolve quantos foram novos."""
    novos_total, sem_novidade = 0, 0
    for pagina in range(1, max_paginas + 1):
        params = dict(params_base, paged=pagina)
        try:
            resp = sessao.get(FONTE_API, params=params, timeout=timeout)
            resp.raise_for_status()
            itens = resp.json().get("items") or []
        except (requests.RequestException, ValueError, AttributeError) as exc:
            if obrigatorio:
                # Nunca publicar uma agenda incompleta: os eventos em falta
                # desapareceriam dos calendários de quem subscreveu.
                raise FalhaRecolha("Sem resposta válida da agenda do site (página {}): {}".format(pagina, exc)) from exc
            logging.warning("Página %s sem resposta válida, a usar o que já foi recolhido: %s", pagina, exc)
            break
        if not itens:
            break

        novos = 0
        for item in itens:
            if not isinstance(item, dict):
                continue
            inicio = parse_iso(item.get("start"))
            if inicio is None:
                continue
            fim = parse_iso(item.get("end")) or inicio + timedelta(hours=1)
            if fim < hoje or inicio > horizonte:
                continue
            chave = item.get("uid") or item.get("id") or item.get("url")
            if chave and chave not in eventos:
                eventos[chave] = item
                novos += 1

        novos_total += novos
        sem_novidade = sem_novidade + 1 if novos == 0 else 0
        if sem_novidade >= 3:
            break
    return novos_total


def obter_eventos(sessao, hoje, horizonte):
    eventos = {}
    dia = hoje.astimezone(LISBOA).date().isoformat()

    # 1) Eventos que começam hoje ou depois.
    n = recolher(sessao, {
        "start.query": dia, "start.range": "min", "sort_on": "start", "sort_order": "ascending",
    }, hoje, horizonte, eventos, max_paginas=60, obrigatorio=True)
    logging.info("Eventos futuros: %s", n)

    # 2) Eventos que já começaram mas ainda decorrem (exposições, festivais).
    #    Se a API não aceitar este filtro, o resultado é filtrado aqui na mesma.
    try:
        n = recolher(sessao, {
            "end.query": dia, "end.range": "min", "start.query": dia, "start.range": "max",
            "sort_on": "start", "sort_order": "descending",
        }, hoje, horizonte, eventos, max_paginas=20, obrigatorio=False)
        logging.info("Eventos em curso: %s", n)
    except Exception as exc:  # nunca impede a publicação dos eventos futuros
        logging.warning("Não foi possível obter eventos em curso: %s", exc)

    return list(eventos.values())


def mostrar_campos(sessao):
    """Diagnóstico: mostra os campos que a API devolve (para ver se há categorias)."""
    resp = sessao.get(FONTE_API, params={"paged": 1}, timeout=20)
    resp.raise_for_status()
    dados = resp.json()
    print("Campos da resposta:", ", ".join(sorted(dados)))
    for item in (dados.get("items") or [])[:3]:
        print("-", limpar_texto(item.get("title")))
        for chave, valor in sorted(item.items()):
            print("    {}: {}".format(chave, str(valor)[:80]))


# ---------------------------------------------------------------------------
# Geração dos calendários
# ---------------------------------------------------------------------------

def url_ics(url_base, caminho):
    return url_base + caminho


def novo_calendario(nome, descricao, url_fonte):
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("name", nome)
    cal.add("x-wr-calname", nome)
    cal.add("x-wr-caldesc", descricao)
    cal.add("x-wr-timezone", "Europe/Lisbon")
    cal.add("source", url_fonte, parameters={"VALUE": "URI"})
    cal.add("refresh-interval", timedelta(hours=HORAS_ATUALIZACAO), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "PT{}H".format(HORAS_ATUALIZACAO))
    return cal


def criar_evento(item, tema):
    inicio = parse_iso(item.get("start"))
    fim = parse_iso(item.get("end")) or inicio + timedelta(hours=1)
    if fim < inicio:
        fim = inicio + timedelta(hours=1)

    ev = Event()
    chave = item.get("uid") or item.get("id") or item.get("url")
    ev.add("uid", "{}@{}".format(chave, DOMINIO_UID))

    modificado = parse_iso(item.get("modified"))
    # DTSTAMP estável: o ficheiro só muda quando os eventos mudam.
    ev.add("dtstamp", modificado or parse_iso(item.get("created")) or inicio)
    if modificado:
        ev.add("last-modified", modificado)

    ev.add("summary", vText(limpar_texto(item.get("title")) or "Evento"))

    if eh_dia_inteiro(inicio, fim):
        ev.add("dtstart", inicio.astimezone(LISBOA).date())
        # DTEND de dia inteiro é exclusivo (RFC 5545): dia seguinte ao último.
        ev.add("dtend", fim.astimezone(LISBOA).date() + timedelta(days=1))
    else:
        ev.add("dtstart", inicio)
        ev.add("dtend", fim)

    local = limpar_texto(item.get("location"))
    if local:
        ev.add("location", vText(local))

    link = link_seguro(item.get("url") or item.get("eventUrl"))
    descricao = limpar_texto(item.get("description") or item.get("text"))
    partes = [p for p in (descricao, "Mais informação: " + link if link else "") if p]
    if partes:
        ev.add("description", vText("\n\n".join(partes)))
    if link:
        ev.add("url", link)

    ev.add("categories", [tema.nome])
    ev.add("transp", "TRANSPARENT")  # não marca a pessoa como "ocupada"
    return ev


def ordenar(itens):
    return sorted(itens, key=lambda i: (parse_iso(i.get("start")), limpar_texto(i.get("title"))))


def construir_calendarios(itens, url_base):
    """Devolve {caminho_relativo: bytes} e a contagem de eventos por tema."""
    temas_todos = TEMAS
    geral = novo_calendario(NOME_GERAL, "Todos os eventos da agenda municipal.",
                            url_ics(url_base, "agenda.ics"))
    por_tema = {
        t.slug: novo_calendario(PREFIXO_TEMA + t.nome, t.resumo + ".",
                                url_ics(url_base, "temas/{}.ics".format(t.slug)))
        for t in temas_todos
    }
    contagem = {t.slug: 0 for t in temas_todos}

    for item in ordenar(itens):
        tema = classificar(item)
        contagem[tema.slug] += 1
        geral.add_component(criar_evento(item, tema))
        por_tema[tema.slug].add_component(criar_evento(item, tema))
        logging.debug("%-22s %s", tema.slug, limpar_texto(item.get("title")))

    ficheiros = {"agenda.ics": geral.to_ical()}
    for slug, cal in por_tema.items():
        ficheiros["temas/{}.ics".format(slug)] = cal.to_ical()
    return ficheiros, contagem


# ---------------------------------------------------------------------------
# Página pública
# ---------------------------------------------------------------------------

def texto_contagem(n):
    if n == 0:
        return "Sem eventos de momento"
    return "1 evento" if n == 1 else "{} eventos".format(n)


def links(url_base, caminho):
    https = url_ics(url_base, caminho)
    webcal = "webcal://" + https.split("://", 1)[1]
    google = "https://calendar.google.com/calendar/render?cid=" + quote(webcal, safe="")
    return {"https": https, "webcal": webcal, "google": google}


def bloco_subscricao(url_base, caminho, rotulo):
    l = {k: html.escape(v, quote=True) for k, v in links(url_base, caminho).items()}
    r = html.escape(rotulo, quote=True)
    return (
        '<div class="acoes">\n'
        '  <a class="botao so-apple" href="{webcal}" aria-label="Subscrever {r} no iPhone, iPad ou Mac">Subscrever</a>\n'
        '  <a class="botao so-google" href="{google}" target="_blank" rel="noopener noreferrer" '
        'aria-label="Subscrever {r} no Google Calendar">Subscrever</a>\n'
        '  <a class="botao so-outro" href="{webcal}" aria-label="Subscrever {r} no Outlook ou noutra aplicação">Subscrever</a>\n'
        '  <div class="copiar so-outro">\n'
        '    <input type="text" readonly value="{https}" aria-label="Link de {r}">\n'
        '    <button type="button" class="botao-copiar" data-link="{https}">Copiar link</button>\n'
        '  </div>\n'
        '</div>'
    ).format(r=r, **l)


def gerar_pagina(contagem, total, url_base):
    modelo = MODELO_PAGINA.read_text(encoding="utf-8")

    linhas = []
    for tema in TEMAS:
        linhas.append(
            '<li class="tema" style="--cor-tema: {cor}">\n'
            '  <div class="tema-texto">\n'
            '    <h3>{nome}</h3>\n'
            '    <p>{resumo}</p>\n'
            '    <p class="contagem">{contagem}</p>\n'
            '  </div>\n'
            '  {acoes}\n'
            '</li>'.format(
                cor=html.escape(tema.cor, quote=True),
                nome=html.escape(tema.nome),
                resumo=html.escape(tema.resumo),
                contagem=texto_contagem(contagem[tema.slug]),
                acoes=bloco_subscricao(url_base, "temas/{}.ics".format(tema.slug), tema.nome),
            )
        )

    script = re.search(r"<script>(.*?)</script>", modelo, re.S)
    hash_script = base64.b64encode(hashlib.sha256(script.group(1).encode("utf-8")).digest()).decode() if script else ""

    substituicoes = {
        "{{HASH_SCRIPT}}": hash_script,
        "{{TOTAL}}": texto_contagem(total),
        "{{ACOES_GERAL}}": bloco_subscricao(url_base, "agenda.ics", "a agenda completa"),
        "{{LISTA_TEMAS}}": "\n".join(linhas),
        "{{FONTE_PAGINA}}": html.escape(FONTE_PAGINA, quote=True),
    }
    for chave, valor in substituicoes.items():
        modelo = modelo.replace(chave, valor)
    return modelo.encode("utf-8")


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------

def escrever_atomico(conteudo, destino: Path):
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(destino.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo)
        os.replace(tmp, destino)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def gerar_saidas(itens, pasta, url_base):
    ficheiros, contagem = construir_calendarios(itens, url_base)
    ficheiros["index.html"] = gerar_pagina(contagem, len(itens), url_base)
    for caminho, conteudo in ficheiros.items():
        escrever_atomico(conteudo, pasta / caminho)
    atuais = {Path(c).name for c in ficheiros if c.startswith("temas/")}
    for antigo in (pasta / "temas").glob("*.ics"):
        if antigo.name not in atuais:
            antigo.unlink()
    if pasta.resolve() != PASTA_SCRIPT and PASTA_FONTES.is_dir():
        # para a página funcionar também quando é gerada noutra pasta
        (pasta / "fontes").mkdir(exist_ok=True)
        for fonte in PASTA_FONTES.glob("*.woff2"):
            shutil.copy2(fonte, pasta / "fontes" / fonte.name)
    return contagem


def main():
    parser = argparse.ArgumentParser(description="Gera a agenda municipal em .ics (geral e por tema).")
    parser.add_argument("--pasta-saida", type=Path, default=PASTA_SCRIPT)
    parser.add_argument("--url-publica", default=URL_PUBLICA, help="Endereço público dos ficheiros (termina em /)")
    parser.add_argument("--horizonte-meses", type=int, default=18)
    parser.add_argument("--detalhe", action="store_true", help="Mostra o tema atribuído a cada evento")
    parser.add_argument("--campos", action="store_true", help="Mostra os campos que o site devolve e termina")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.detalhe else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    url_base = args.url_publica if args.url_publica.endswith("/") else args.url_publica + "/"

    if args.campos:
        mostrar_campos(obter_sessao())
        return

    hoje = datetime.now(LISBOA).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    horizonte = hoje + timedelta(days=30 * args.horizonte_meses)

    try:
        itens = obter_eventos(obter_sessao(), hoje, horizonte)
    except Exception as exc:
        logging.error("Recolha falhou; os ficheiros publicados ficam como estavam. %s", exc)
        sys.exit(1)

    if not itens:
        # Uma agenda municipal vazia é quase certamente uma falha do site:
        # não se apaga o que as pessoas já têm no calendário.
        logging.error("Nenhum evento recebido; os ficheiros publicados ficam como estavam.")
        sys.exit(1)

    try:
        contagem = gerar_saidas(itens, args.pasta_saida, url_base)
    except Exception:
        logging.exception("Falha ao escrever os ficheiros.")
        sys.exit(1)

    logging.info("Total: %s eventos", len(itens))
    for tema in TEMAS:
        logging.info("  %-34s %s", tema.nome, contagem[tema.slug])


if __name__ == "__main__":
    main()
