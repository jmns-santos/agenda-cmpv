# -*- coding: utf-8 -*-
"""
Agenda Municipal da Póvoa de Varzim em formato iCalendar (.ics).

Lê os eventos publicados na agenda do site www.cm-pvarzim.pt e gera:

    agenda.ics              agenda completa
    temas/<tema>.ics        um calendário por tema
    tipos/<tipo>.ics        um calendário por tipo de evento
    index.html              página pública com os links de subscrição

Cada evento é arrumado num único tema e num único tipo, a partir de
palavras-chave no título e na descrição (ver TEMAS e TIPOS mais abaixo).
Assim, quem subscrever vários temas (ou vários tipos) não vê eventos
repetidos.

Uso:
    python gerar_agenda_ics.py
    python gerar_agenda_ics.py --pasta-saida saida --url-publica https://exemplo.pt/agenda/
"""

import argparse
import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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

# Jogos do Varzim SC (fixtur.es, sempre atualizado). Fonte externa: se falhar,
# a agenda publica-se sem os jogos, nunca o contrario (ver obter_jogos_varzim).
FONTE_VARZIM_ICS = "http://ics.fixtur.es/v2/varzim.ics?e23e09232ebff109"
VARZIM_LINK = "https://www.flashscore.pt/equipa/varzim/K8O5Kkl7/lista/"

# Endereço público onde os ficheiros ficam alojados (termina em "/").
URL_PUBLICA = "https://jmns-santos.github.io/agenda-cmpv/"

# Só são aceites links de eventos que apontem para o site do município.
DOMINIOS_PERMITIDOS = {"www.cm-pvarzim.pt", "cm-pvarzim.pt"}

PASTA_SCRIPT = Path(__file__).resolve().parent
MODELO_PAGINA = PASTA_SCRIPT / "modelo_pagina.html"
TRADUCOES = PASTA_SCRIPT / "traducoes.json"
LOGOTIPO = PASTA_SCRIPT / "logotipo.png"
PASTA_FONTES = PASTA_SCRIPT / "fontes"

NOME_GERAL = "Agenda Municipal da Póvoa de Varzim"
PREFIXO_TEMA = "Póvoa de Varzim: "
PRODID = "-//Póvoa de Varzim//Agenda Municipal//PT"
DOMINIO_UID = "agenda.cm-pvarzim.pt"  # não alterar: mantém os eventos já subscritos
HORAS_ATUALIZACAO = 4

# "Próximo mês" = janela corrida a partir de hoje (não o mês de calendário).
JANELA_PROXIMOS_DIAS = 30


@dataclass
class Tema:
    slug: str
    nome: str
    resumo: str
    palavras: list = field(default_factory=list)
    cor: str = "#6b7780"
    fracas: list = field(default_factory=list)   # só contam se nenhuma palavra forte aparecer
    tema: str = ""                               # tipos: tema a que o tipo puxa (ver PESO_TIPO)
    so_titulo: list = field(default_factory=list)  # tipos: palavras que só contam no título


# TEMA = o assunto do evento. TIPO = o formato do evento.
# Cada evento tem um só tema e um só tipo, e está sempre na agenda completa.
#
# Palavras-chave sem acentos e em minúsculas; cada uma é procurada no início
# de uma palavra ("teatro" apanha "teatros", "exposi" apanha "exposição").
# Para exigir a palavra exata, terminar com "\b" (ex. r"correr\b").
# Ordem da lista = ordem de apresentação na página.
# "feira" sem apanhar "quinta-feira", "sexta-feira"...
FEIRA = r"(?<!segunda\W)(?<!terca\W)(?<!quarta\W)(?<!quinta\W)(?<!sexta\W)feiras?\b"

TEMAS = [
    Tema("cultura", "Cultura", "Música, cinema, teatro, exposições, livros e património", [
        "cinema", "filme", "documentario", "teatro", "concerto", "musica", "musical",
        "opereta", "opera", "orquestra", "filarmonica", r"bandas?\b", "tributo", "fado", r"coros?\b",
        "coral", "danca", "bailado", "espetaculo", "comedia", "stand up", "circo", "sons no patrimonio",
        "exposi", "pintura", "fotografia", "escultura", "galeria", "museu", "arte", "artista",
        "patrimonio", "historia", "historic", "arqueolog", "arquivo municipal", "jornadas europeias",
        "livro", "literari", "literatura", "leitura", "poesia", "poeta", "escritor", "autor",
        "biblioteca", "camoes", "eca de queiros", "camilo",
        "folclore", "cantares", "tradic", "etnograf", "carnaval", "entrudo", "domingo gordo", "geminac",
        "romaria", "arraial", "procissao", "peregrinac", "festas? em honra", "santos populares",
        r"sao joao\b", "sao pedro(?! de rates)", "santo antonio", "semana santa", "queima de judas",
        "trono de sao pedro", "presepio", "janeiras", "cortejo", "camisola poveira", r"rancho\b",
        "tragedia maritima", "monumento", "correntes d escritas", "museus", r"festas?\b",
        "expressao dramatica", "expressao plastica",
    ], "#8e1b4f"),
    Tema("desporto", "Desporto", "Provas, torneios e atividade física", [
        "desport", "futebol", "futsal", "andebol", "basquet", "voleibol", "hoquei",
        "atletismo", "natacao", "surf", "bodyboard", "kickboxing", "boxe", "karate", "judo",
        "ginastica", "padel", "tenis", "ciclismo", "btt", "regata", "remo", "canoagem",
        "torneio", "campeonato", "championship", "maratona", "corrida", r"correr\b",
        "caminhada", r"caminhar\b", "trail", "fitness", "zumba", "pilates", "atividade fisica",
        "xadrez", "triatlo", "duatlo", "skate", "patinagem", "football", "jogos desportivos",
    ], "#1b5e20"),
    Tema("ambiente", "Ambiente", "Natureza, clima, energia, água, resíduos e reciclagem", [
        "ambient", "energ", "sustentab", "reciclag", "residuos", "desperdicio", "plastico", r"clima\b", "climatic",
        "floresta", r"arvores?(?! genealog)", "plantac", "reflorest", "biodiversidade", "natureza", "limpeza de praia", "limpeza",
        "eco escola", "eco escolas", "beach clean", "clean up", "agricultura", "horta", r"aves\b", "oceano", "litoral",
        "gestao de combustivel", "incendio", "compostagem", "ecocentro", "economia circular", "centro do clima",
        "monstros domesticos", r"agua\b", "poupanca de agua", "animais", "zero waste", "lipor", "gia\b",
        "interpretacao ambiental", "dunas", "praias? (?:acessive|limpa)", "bandeira azul",
    ], "#004d40"),
    Tema("mobilidade-transportes", "Mobilidade e transportes", "Transportes públicos, bicicleta, andar a pé e segurança rodoviária", [
        "mobilidade", "transporte", "bicicleta", "ciclovia", "ciclavel", "pedonal", "autocarro", "metro\b",
        "comboio", "estacionamento", "transito", "seguranca rodoviaria", "rede de transportes", "unir\b",
        "trotinete", "boleia", "car free", "dia sem carros", "cycling", "bike to", "xeque bike", "passadeira",
        "andar a pe", "caminhar para a escola", "comboio de bicicletas", "schoolbus", "school bus", ], "#1565c0"),
    Tema("turismo", "Turismo", "Turismo, gastronomia, vinhos, artesanato e promoção da Póvoa", [
        "turism", "turistic", "gastronom", "vinho", "vinic", "wine", "artesanato", "artesao", "artesaos",
        "rabanada", "sardinh", "doces", "docaria", "chocolate", "showcooking", "cozinha ao vivo",
        "burger", "food truck", "street food", "petisc", "fins de semana gastronomicos", "alojamento local",
        "visitantes", "cocktail", "marisco", "pescado", "rosca doce",
    ], "#9e5d00"),
    Tema("economia", "Economia", "Comércio, empresas, emprego, formação e mercados", [
        FEIRA, "mercadinho", r"empresas?\b", "empresari", "negocio", "empreend", "emprego", "empregab",
        "comercio", "comerciante", "economia", "economic", "investimento", "startup", "formacao profissional",
        r"cooperativas?\b", "agrosemana", "flores", "networking", "fundos comunitarios", "financiamento",
        "industria", r"pme\b", "lojas", "inovacao", "propriedade intelectual", "portugal 2030",
        "prr\b", "apoio especializado", "competitividade",
    ], "#3e2723"),
    Tema("cidadania", "Cidadania", "Saúde, educação, família, juventude, inclusão e vida municipal", [
        "saude", "senior", "idoso", "envelhecimento", "rastreio", "dadiva", "sangue",
        "social", "solidari", "voluntari", "inclusao", "cuidador", "cuidados", "deficiencia",
        "bebe", "crianca", "infantil", "infancia", r"familias?\b", "parental", r"pais\b", "filhos",
        "escola", "educac", "educativ", "ensino", "ensino superior", "jovens", "juventude", "juvenil", "ferias escolares",
        "assembleia", "sessao solene", "tomada de posse", "reuniao publica", "cidadania",
        "participativ", "eleic", "direitos", "igualdade", "violencia", "interculturalidade",
        "seguranca", "protecao civil", r"paz\b", "bairro", "consumidor", "alimentac", "bem estar",
        "bupi", "literacia", "discussao publica", "consulta publica", "plano de pormenor", "plano diretor", "bullying", "mulher", "comunidade",
    ], "#1a237e"),
]

# Eventos sem nenhuma palavra-chave de tema.
TEMA_PADRAO = TEMAS[-1]

# Em caso de empate na pontuação, ganha o tema que aparece primeiro aqui.
PRIORIDADE = ["mobilidade-transportes", "ambiente", "desporto", "turismo", "economia", "cultura", "cidadania"]

# Tipos de evento (o formato). Ganha a palavra-chave forte que aparece mais
# cedo no título ("Oficina de teatro" é oficina; "Concerto de Natal" é
# concerto). Sem nada no título, vale a descrição e depois o texto completo.
# Só depois contam as palavras fracas. Sem nada, o tipo vem do tema
# (Desporto: provas desportivas; restantes: oficinas e atividades).
TIPOS = [
    Tema("concertos", "Concertos", "Concertos, recitais, coros, bandas e música ao vivo", [
        "concerto", "recital", "orquestra", "filarmonica", "fado", r"coros?\b", "coral",
        "tributo", "ensemble", "sons no patrimonio", r"dj\b", r"djs\b", "jazz", "blues", "fanfarra", r"tunas?\b",
        "audicao", "cantares", "cantigas", "janeiras", "pifaro", "showcase", "pimba", "fadista",
        r"festival de (?:tunas|musica|jazz|fado|bandas|coros|folclore|guitarra)", "festival de tunas",
        r"gala (?:do|de) (?:fado|musica)", r"grande gala do fado",
    ], "#d32f2f", fracas=["noite de", r"live\b", "ao vivo"], tema="cultura",
        so_titulo=[r"bandas?\b", "musica", r"cantar\b", "guitarra", "trompete", "piano", "violino", "acustic",
                   "cantor", r"rock\b", "cantautor", "quarteto", "trio"]),
    Tema("cinema", "Cinema", "Sessões de cinema, filmes e documentários", [
        "cinema", r"filmes?\b", "documentario", "curtas metragens", "curta metragem", "longa metragem",
        "cineclube", "projecao", "cine concerto", r"realizad[oa] por", "realizador", "serao de cinema",
        "sessao de cinema", "sessoes de cinema", "pelicula", "classificacao etaria", r"m/\d+", "octopus",
        r"ecra\b", r"duracao.{0,4}\d+ minutos",
    ], "#6a3fb5", fracas=["cine teatro garrett", r"exibe\b", "exibicao", "exibem", "estreia nacional"], tema="cultura"),
    Tema("teatro", "Teatro", "Peças de teatro, teatro de rua, marionetas e leituras encenadas", [
        "(?<!cine.)teatro", "teatral", "peca de teatro", "dramaturg", "encenac", "leitura encenada",
        "marionetas", "fantoches", "monologo", r"revista\b", "auto de natal", "presepio vivo",
        "recriacao d", "paixao de cristo",
    ], "#c2185b", tema="cultura"),
    Tema("espetaculos", "Espetáculos", "Dança, circo, magia, humor, musicais e espetáculos de rua", [
        "danca", r"dance\b", "bailado", "ballet", "hip hop", "breaking", r"musical\b", r"opera\b", "opereta",
        "circo", "circense", "acrobac", "malabar", "stand up", "comedia", "comedy", "humorista",
        "ilusionis", "fashion", "desfile de moda", "variedades",
        "fogo de artificio", "videomapping", "video mapping", r"gala de opera\b", "gala final", "miss povoa",
        "concurso de danca",
    ], "#e07b00", fracas=["espetaculo", r"show\b", "atuac", "animacao de rua", "animacao"], tema="cultura",
        so_titulo=["magia", "magico", "humor", "performance", "rusgas"]),
    Tema("exposicoes", "Exposições", "Exposições de arte, fotografia, história e património", [
        "exposi", r"patentes?\b", "bienal", "residencia artistica", "inauguracao da exposi",
    ], "#8a7300", tema="cultura", so_titulo=[r"mostras?\b", "instalacao", "mural"]),
    Tema("literatura", "Literatura", "Livros, leitura, poesia e contos", [
        "poesia", "poemas", "poeta", "literari", "literatura", "conta contos", "hora do conto", "book club",
        "boletim cultural", "feira do livro", "tertulia literaria", "declamac", r"sarau\b",
        "biblioteca no jardim", "biblioteca de jardim", "bibliotecas de verao", "lancamento do livro",
        "apresentacao do livro", r"livros? .{0,60}lancad", "correntes d escritas", "coletanea",
        "escritores", "lancamento de livro",
    ], "#795548", tema="cultura",
        so_titulo=["livro", "leitura", "leitores", r"ler\b", r"contos?\b", r"historias\b", r"mesa \d+", "autores"]),
    Tema("oficinas-atividades", "Oficinas e atividades", "Oficinas, cursos, conferências, jogos e atividades para todas as idades", [
        "oficina", "workshop", "atelie", "atelier", "curso", "formacao", "capacitacao", "masterclass",
        r"aulas?\b", "explicacoes", r"artes (?:e )?oficios", "primeiros socorros",
        "repair cafe", "crochet", "tricot", r"ferias\b", "acampamento", "programa psicoeducativo",
        "atividade ludica", "ludoteca", "gincana", r"quizz?\b", "escape room",
        "hackathon", "lavorada",
        "conferencia", "palestra", "coloquio", "seminario", "congresso", "jornadas", "tertulia", "debate",
        "mesa redonda", "mesa de reflexao", "webinar", "forum", "foruns", "cimeira", "summit", "ciclo aberto",
        "conversas?\b", "orcamento participativo", "assembleia de cidadaos", "apresentacao do projeto",
        "apresentacao do pacto", "apresentacao do plano", "apresentacao do programa", "apresentacao do estudo",
    ], "#00897b", fracas=["concurso", "sessao", "sessoes", "encontro", "ciclo", "programa", "projeto", "atividade",
                         "iniciativa", r"clube\b", r"familias?\b", "estagio", "dinamiz"],
        so_titulo=["laboratorio", r"jogos?\b", r"jogar\b", "misterio", "aprender", "demonstrac", "experimenta"]),
    Tema("provas-desportivas", "Provas desportivas", "Torneios, corridas, caminhadas, jogos e treinos abertos", [
        "torneio", "campeonato", "championship", r"taca\b", r"cup\b", r"open\b", "trofeu",
        "corrida", "maratona", "caminhada", "trail", r"cross\b", "corta mato", r"raid\b", "meeting",
        "classica", r"provas?\b", "regata", "futebol", "football", "futsal", "andebol", "basquet",
        "voleibol", "hoquei", "atletismo", r"ppa\b", "natacao", "surf", "bodyboard", "ciclismo",
        "btt", "padel", "tenis", "squash", "boccia", "xadrez", "ginastica", "aerobica", "fitness",
        "yoga", "zumba", "pilates", "atividade fisica", r"sub \d+", "karate", "judo",
        "kickboxing", "boxe", "triatlo", "duatlo", "aquatlo", "canoagem", r"remo\b", "patinagem", "skate",
        "milha", r"run\b", r"walk\b", r"bike\b", "showdown", "estagio internacional",
        "jogos desportivos", "jogos olimpicos", "jogos juvenis", "walking football", "grande premio",
        "futvolei", "golf", "hidro",
    ], "#388e3c", tema="desporto", so_titulo=["desport"]),
    Tema("festas-feiras", "Festas, feiras e mercados", "Festas populares, festivais, feiras, mercados e gastronomia", [
        r"festas?\b", r"fest\b", "festival", "festeja", "arraial", "romaria", "procissao",
        "peregrinacao", "semana santa", "solenidades", "queima de judas", "trono de sao pedro",
        FEIRA, "mercadinho", "carnaval", "halloween", "fim d ano",
        "passagem de ano", "sardinhada", "magusto", "santos populares", "cortejo",
        "desfile", "convivio", "by night", "sunset", "noite branca", "mostra gastronom",
        "gastronom", "cozinha ao vivo", "showcooking", "agrosemana",
        "burger", "food truck", "rabanada", "papagaios", r"provas? de vinhos?", "prova comentada", r"bailes?\b", "springfest",
        "noitada", "dias no parque", "rusgas", "desfolhada", "sao martinho", "noites? de", "sao pedrinho", "pequenada",
    ], "#f4511e", fracas=["semana d", "folia"],
        so_titulo=["mercado", r"natal\b", "pascoa", "vinho", "wine", "chocolate"]),
    Tema("visitas-passeios", "Visitas e passeios", "Visitas guiadas, roteiros, passeios e percursos", [
        "visita", "roteiro", "passeio", r"passear\b", "peddy paper",
        "observacao de aves", "excursao", "jornadas europeias do patrimonio",
        "portas abertas", "dia aberto", "open day", "caminho de santiago", "noite dos museus", "noite no museu",
    ], "#0288d1", so_titulo=["percurso", r"rotas?\b", "trilho", "descobert", "descobrir"]),
    Tema("acoes-campanhas", "Ações e campanhas", "Sensibilização, rastreios, recolhas, limpezas e campanhas solidárias", [
        "plantacao", "acao de controlo", "ecocentro", "reciclag", "campanha", "peditorio",
        "rastreio", "dadiva", "doacao", "voluntariado", r"saude primeiro",
        r"acao (?:de )?(?:promocao|saude)", "outubro rosa", "pelo clima", "restart a heart",
        "sensibilizac", "sessao de esclarecimento", "sessoes de esclarecimento", "sessao informativa",
        "sessoes informativas", "informar para", "consultorio", "vacinac", "esterilizac",
        "banco alimentar", "angariac", "embrulha", "simulacro", "beach clean", "clean up", "promocao de saude", "promocao da saude",
        "rastreios", "divulgacao d[aoe]", "apoio especializado", "balcao de atendimento", "atendimento",
    ], "#546e7a", so_titulo=["limpeza", "recolha", "prevencao", "adocao"]),
    Tema("comemoracoes", "Comemorações e cerimónias", "Dias comemorativos, cerimónias, galas, homenagens e sessões solenes", [
        "sessao solene", "cerimonia", "tomada de posse", "assembleia municipal", "reuniao publica",
        "reuniao de camara", "inauguracao", r"galas?\b",
        "aniversario", "centenario", "bicentenario", "25 de abril", "1 de dezembro", "geminac",
        "tragedia maritima", "memoria da tragedia", "entrega de premios", "premiacao", "abertura oficial",
        "evocac", r"missa\b", "dia da cidade", "dia do municipio", r"\d+ anos da freguesia",
    ], "#9c27b0", fracas=[r"dia (?:mundial|internacional|nacional|europeu|municipal)\b", r"dia d[aoe]s?\b",
                         "semana europeia", "semana nacional", "lancamento", "abertura", "reabertura"],
        so_titulo=["homenagem", "comemora", "celebracao",
                   r"dia (?:mundial|internacional|nacional|europeu|municipal) ",
                   r"dia d[aoe]s? (?:pai|mae|crianca|mulher|agricultor|horticultor|europa|mar|cidade|municipio|avo|namorados)",
                   r"semana (?:europeia|nacional|mundial) "]),
    Tema("jogos-varzim", "Jogos do Varzim", "Calendario de jogos do Varzim SC", [],
         "#2e7d32", tema="desporto"),
]
TIPO_PADRAO = TIPOS[6]                     # Oficinas e atividades
TIPO_DO_TEMA = {"desporto": "provas-desportivas", "ambiente": "acoes-campanhas", "mobilidade-transportes": "acoes-campanhas"}   # sem palavra-chave nenhuma

# Campos da API que possam trazer categorias/etiquetas do próprio site.
# Se existirem, contam tanto como o título na classificação.
CAMPOS_CATEGORIA = ("categories", "category", "subjects", "subject", "tags", "event_type", "type")

PESO_TITULO = 3
PESO_CATEGORIA = 3
PESO_DESCRICAO = 1
PESO_TIPO = 3      # o tipo puxa o tema: um concerto é cultura, uma prova é desporto

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

def _compilar_lista(palavras):
    return [re.compile(r"(?<=[ ])" + p.replace(" ", r"\s+")) for p in palavras]


def _compilar(temas):
    return {t.slug: _compilar_lista(t.palavras) for t in temas}


PADROES = _compilar(TEMAS)
PADROES_TIPO = _compilar(TIPOS)
PADROES_TIPO_FRACOS = {t.slug: _compilar_lista(t.fracas) for t in TIPOS}
PADROES_TIPO_TITULO = {t.slug: _compilar_lista(t.palavras + t.so_titulo) for t in TIPOS}
TIPOS_POR_SLUG = {t.slug: t for t in TIPOS}
TEMAS_POR_SLUG = {t.slug: t for t in TEMAS}
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


def _textos(item):
    titulo = normalizar(limpar_texto(item.get("title")))
    descricao = normalizar(limpar_texto(item.get("description")))
    completo = normalizar(limpar_texto(item.get("text")))
    return titulo, descricao, completo


def _pontuar_temas(item, tipo=None):
    titulo, descricao, completo = _textos(item)
    categorias = normalizar(texto_categorias(item))
    descricao = descricao if descricao.strip() else completo
    pontos = {}
    for tema in TEMAS:
        p = 0
        for padrao in PADROES[tema.slug]:
            if padrao.search(titulo):
                p += PESO_TITULO
            if padrao.search(categorias):
                p += PESO_CATEGORIA
            if padrao.search(descricao):
                p += PESO_DESCRICAO
        if tipo is not None and tipo.tema == tema.slug:
            p += PESO_TIPO
        pontos[tema.slug] = p
    return pontos


def _melhor_tema(pontos):
    melhor, melhor_pontos = TEMA_PADRAO, 0
    for tema in TEMAS:
        p = pontos[tema.slug]
        if p > melhor_pontos or (p == melhor_pontos and p > 0
                                 and ORDEM_EMPATE.get(tema.slug, 99) < ORDEM_EMPATE.get(melhor.slug, 99)):
            melhor, melhor_pontos = tema, p
    return melhor


def _primeiro_tipo(texto, padroes):
    """Tipo cuja palavra-chave aparece mais cedo no texto (empate: a mais longa)."""
    melhor, melhor_chave = None, None
    for tipo in TIPOS:
        for padrao in padroes[tipo.slug]:
            m = padrao.search(texto)
            if m:
                chave = (m.start(), -(m.end() - m.start()))
                if melhor_chave is None or chave < melhor_chave:
                    melhor, melhor_chave = tipo, chave
    return melhor


def _tipo_dominante(texto, padroes):
    """Tipo com mais palavras-chave no texto (empate: a que aparece mais cedo).
    Na descrição e no texto completo uma palavra solta engana ("magia" numa
    sinopse); o tipo que mais se repete é o que descreve o evento."""
    contagem, primeira = {}, {}
    for tipo in TIPOS:
        for padrao in padroes[tipo.slug]:
            for m in padrao.finditer(texto):
                contagem[tipo.slug] = contagem.get(tipo.slug, 0) + 1
                primeira[tipo.slug] = min(primeira.get(tipo.slug, m.start()), m.start())
    if not contagem:
        return None
    melhor = min(contagem, key=lambda s: (-contagem[s], primeira[s]))
    return TIPOS_POR_SLUG[melhor]


def _tipo_por_palavras(item):
    titulo, descricao, completo = _textos(item)
    for padroes_titulo, padroes in ((PADROES_TIPO_TITULO, PADROES_TIPO), (PADROES_TIPO_FRACOS, PADROES_TIPO_FRACOS)):
        tipo = _primeiro_tipo(titulo, padroes_titulo)
        if tipo is not None:
            return tipo
        for texto in (descricao, completo):
            tipo = _tipo_dominante(texto, padroes)
            if tipo is not None:
                return tipo
    return None


def classificar_tipo(item):
    tipo = _tipo_por_palavras(item)
    if tipo is None:
        tema = _melhor_tema(_pontuar_temas(item))
        tipo = TIPOS_POR_SLUG.get(TIPO_DO_TEMA.get(tema.slug), TIPO_PADRAO)
    return tipo


def classificar(item):
    """Tema do evento. O tipo (o formato) também conta: um concerto puxa para Cultura."""
    return _melhor_tema(_pontuar_temas(item, _tipo_por_palavras(item)))


# ---------------------------------------------------------------------------
# Bilhetes, entradas e inscrições
# ---------------------------------------------------------------------------
# Copia do texto do evento as frases sobre bilhetes, preços, entrada livre e
# inscrições, e os links de compra (BOL, Ticketline...) e de inscrição. Só
# usa o que está escrito no site: nada é deduzido nem completado.

ACESSO_RE = re.compile(
    r"bilhete|bilheteira|ingresso|inscri[çc]|gratuit|entrada (?:é )?(?:livre|gratuita)|entradas?\s*[:|]|"
    r"acesso (?:é )?livre|(?:levantamento de|mediante|com) convite", re.I)
# Um valor em euros só conta quando a frase fala de pagar (evita prémios, orçamentos...).
EUROS_RE = re.compile(r"\d\s?€|€\s?\d|\beuros?\b", re.I)
PAGAR_RE = re.compile(r"\bcust(?:a|am|ar|o de)\b|pre[çc]o|\bpag|s[óo]cio|desconto|valor da (?:entrada|inscri)", re.I)
HOSTS_BILHETES = ("bol.pt", "ticketline.pt", "ticketline.sapo.pt", "blueticket.pt", "seetickets.com")
HOSTS_INSCRICAO = ("forms.gle", "forms.office.com")
A_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
BLOCO_RE = re.compile(r"<br\s*/?>|</p>|</li>|</div>|</h\d>|</tr>", re.I)
FRASE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý0-9«“\"])")
MARCA_RE = re.compile(r"⟦(\d+)⟧")
MAX_FRASES_ACESSO = 3
MAX_CARACTERES_FRASE = 320


def _host(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _do_host(host, lista):
    return any(host == h or host.endswith("." + h) for h in lista)


def _url_publico(url):
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname) and len(url) <= 500


def _sem_parenteses_longos(frase, limite=60):
    """Tira parênteses compridos (ex. o horário da bilheteira): estão na página do evento."""
    saida, abertos = [], []
    for c in frase:
        if c == "(":
            abertos.append(len(saida))
        saida.append(c)
        if c == ")" and abertos:
            inicio = abertos.pop()
            if not abertos and len(saida) - inicio > limite:
                del saida[inicio:]
    frase = re.sub(r"\s{2,}", " ", "".join(saida))
    return re.sub(r"\s+([.,;:])", r"\1", frase).strip()


def info_acesso(item):
    """Devolve (frases, [(rótulo, url)]) sobre bilhetes, entradas e inscrições."""
    bruto = "{}\n{}".format(item.get("text") or "", item.get("description") or "")
    urls = []

    def ancora(m):
        url = html.unescape(m.group(1)).strip()
        rotulo = TAG_RE.sub("", m.group(2)).strip()
        if url.lower().startswith("mailto:"):
            return rotulo or url[7:]
        if not _url_publico(url):
            return rotulo
        urls.append(url)
        return "{} ⟦{}⟧".format(rotulo if rotulo != url else "", len(urls) - 1)

    texto = A_RE.sub(ancora, bruto)
    texto = html.unescape(TAG_RE.sub(" ", BLOCO_RE.sub("\n", texto)))

    frases, vistas, ligacoes = [], set(), []

    def juntar(rotulo, url):
        if url not in [u for _, u in ligacoes]:
            ligacoes.append((rotulo, url))

    for bloco in texto.split("\n"):
        bloco = re.sub(r"\s+", " ", bloco).strip()
        for frase in FRASE_RE.split(bloco):
            marcas = [urls[int(n)] for n in MARCA_RE.findall(frase)]
            frase = re.sub(r"https?://\S+", "", MARCA_RE.sub("", frase))
            frase = _sem_parenteses_longos(frase).strip(" -–|")
            if len(frase) < 8 or not (ACESSO_RE.search(frase) or (EUROS_RE.search(frase) and PAGAR_RE.search(frase))):
                continue
            if frase.lower() in vistas or len(frases) >= MAX_FRASES_ACESSO:
                continue
            vistas.add(frase.lower())
            if len(frase) > MAX_CARACTERES_FRASE:
                frase = frase[:MAX_CARACTERES_FRASE].rsplit(" ", 1)[0] + "…"
            frases.append(frase)
            for url in marcas:
                if _do_host(_host(url), HOSTS_BILHETES):
                    juntar("Bilhetes", url)
                elif re.search(r"inscri", frase, re.I):
                    juntar("Inscrição", url)
                elif re.search(r"bilhete|bilheteira|ingresso", frase, re.I):
                    juntar("Bilhetes", url)

    # Links de compra ou formulários de inscrição em qualquer parte do texto.
    for url in urls + re.findall(r"https?://[^\s<>\"'⟦⟧]+", bruto):
        url = url.rstrip(".,;)")
        if not _url_publico(url):
            continue
        if _do_host(_host(url), HOSTS_BILHETES):
            juntar("Bilhetes", url)
        elif _do_host(_host(url), HOSTS_INSCRICAO):
            juntar("Inscrição", url)
    return frases, ligacoes[:3]


TEXTOS_PT = {
    "bilhetes_titulo": "Bilhetes e inscrições:", "lab_bilhetes": "Bilhetes", "lab_inscricao": "Inscrição",
    "mais_info": "Mais informação", "nota_lingua": "",
}


def texto_acesso(item, descricao="", t=None):
    """Bloco para as notas do evento. Não repete frases que já estão na descrição."""
    t = t or TEXTOS_PT
    frases, ligacoes = info_acesso(item)
    ja = normalizar(descricao)
    frases = [f for f in frases if normalizar(f).strip() not in ja]
    if not frases and not ligacoes:
        return ""
    rotulos = {"Bilhetes": t["lab_bilhetes"], "Inscrição": t["lab_inscricao"]}
    linhas = [t["bilhetes_titulo"]] + frases + ["{}: {}".format(rotulos.get(r, r), u) for r, u in ligacoes]
    return "\n".join(linhas)


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


def obter_jogos_varzim(sessao, hoje, horizonte):
    """Calendario de jogos do Varzim SC (fixtur.es). Fonte externa e
    independente da API da Camara: qualquer falha fica so em log -- a agenda
    publica-se na mesma, sem os jogos, nunca ao contrario."""
    try:
        resp = sessao.get(FONTE_VARZIM_ICS, timeout=20)
        resp.raise_for_status()
        calendario = Calendar.from_ical(resp.content)
    except Exception as exc:
        logging.warning("Jogos do Varzim indisponiveis, a agenda segue sem eles: %s", exc)
        return []

    itens = []
    for comp in calendario.walk("VEVENT"):
        campo_inicio = comp.get("dtstart")
        inicio = campo_inicio.dt if campo_inicio else None
        if inicio is None:
            continue
        if isinstance(inicio, date) and not isinstance(inicio, datetime):
            inicio = datetime.combine(inicio, datetime.min.time(), tzinfo=LISBOA)
        elif inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=LISBOA)
        inicio = inicio.astimezone(timezone.utc)

        campo_fim = comp.get("dtend")
        fim = campo_fim.dt if campo_fim else None
        if fim is not None:
            if isinstance(fim, date) and not isinstance(fim, datetime):
                fim = datetime.combine(fim, datetime.min.time(), tzinfo=LISBOA)
            elif fim.tzinfo is None:
                fim = fim.replace(tzinfo=LISBOA)
            fim = fim.astimezone(timezone.utc)
        else:
            fim = inicio + timedelta(hours=2)   # duracao habitual de um jogo

        if fim < hoje or inicio > horizonte:
            continue

        uid = str(comp.get("uid") or "").strip()
        if not uid:
            continue

        itens.append({
            "uid": "varzim-{}".format(uid),
            "title": limpar_texto(str(comp.get("summary") or "Varzim SC")),
            "start": inicio.isoformat(),
            "end": fim.isoformat(),
            "location": limpar_texto(str(comp.get("location") or "")),
            "description": limpar_texto(str(comp.get("description") or "")),
            "url": VARZIM_LINK,
            "_link_direto": True,
            "_classificacao_fixa": ("desporto", "jogos-varzim"),
        })
    logging.info("Jogos do Varzim: %s", len(itens))
    return itens


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


def novo_calendario(nome, descricao, url_fonte, cor=None):
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
    if cor:
        cal.add("x-apple-calendar-color", cor)  # cor sugerida (Calendário da Apple)
    return cal


def criar_evento(item, tema, tipo, t=None, nomes=None):
    """t: textos da língua (por omissão, português). nomes: {slug: nome} dos temas e tipos nessa língua."""
    t = t or TEXTOS_PT
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

    link = item.get("url") if item.get("_link_direto") else link_seguro(item.get("url") or item.get("eventUrl"))
    descricao = limpar_texto(item.get("description") or item.get("text"))
    partes = [p for p in (descricao, texto_acesso(item, descricao, t), t["nota_lingua"],
                          "{}: {}".format(t["mais_info"], link) if link else "") if p]
    if partes:
        ev.add("description", vText("\n\n".join(partes)))
    if link:
        ev.add("url", link)

    nomes = nomes or {}
    ev.add("categories", [nomes.get(tema.slug, tema.nome), nomes.get(tipo.slug, tipo.nome)])
    ev.add("transp", "TRANSPARENT")  # não marca a pessoa como "ocupada"
    if CANCELADO_RE.search(limpar_texto(item.get("title"))):
        ev.add("status", "CANCELLED")  # as aplicações mostram-no riscado ou como cancelado
    return ev


def ordenar(itens):
    return sorted(itens, key=lambda i: (parse_iso(i.get("start")), limpar_texto(i.get("title"))))


def carregar_traducoes():
    """Línguas da página e dos calendários (traducoes.json, ao lado deste script)."""
    if not TRADUCOES.is_file():
        raise FileNotFoundError("Falta {} (textos da página em cada língua).".format(TRADUCOES))
    return json.loads(TRADUCOES.read_text(encoding="utf-8"))


def prefixo_lingua(lingua):
    """O português fica na raiz (não muda os links já subscritos); as outras em l/<código>/."""
    return "" if lingua == "pt" else "l/{}/".format(lingua)


def nome_grupo(g, t):
    """(nome, resumo) de um tema ou tipo na língua t; em português vem de TEMAS/TIPOS."""
    if t:
        for chave in ("temas", "tipos"):
            if g.slug in t.get(chave, {}):
                return tuple(t[chave][g.slug])
    return g.nome, g.resumo


def classificar_todos(itens):
    """Tema e tipo de cada evento, calculados uma vez para todas as línguas.
    Um item com '_classificacao_fixa' = (tema_slug, tipo_slug) nao passa pelas
    palavras-chave -- usado por fontes externas ja sabidas (jogos do Varzim)."""
    resultado = []
    for item in ordenar(itens):
        fixa = item.get("_classificacao_fixa")
        if fixa:
            tema_slug, tipo_slug = fixa
            resultado.append((item, TEMAS_POR_SLUG[tema_slug], TIPOS_POR_SLUG[tipo_slug]))
        else:
            resultado.append((item, classificar(item), classificar_tipo(item)))
    return resultado


def construir_calendarios(itens, url_base, lingua="pt", t=None, classificados=None, agora=None):
    """Devolve {caminho_relativo: bytes} e a contagem de eventos por tema e por tipo."""
    t_ics = dict(TEXTOS_PT, **{k: v for k, v in (t or {}).items() if k in TEXTOS_PT})
    pre = prefixo_lingua(lingua)
    nomes = {g.slug: nome_grupo(g, t)[0] for g in TEMAS + TIPOS}
    cal_geral = (t or {}).get("cal_geral", NOME_GERAL)
    cal_desc = (t or {}).get("cal_geral_desc", "Todos os eventos da agenda municipal.")
    cal_pref = (t or {}).get("cal_prefixo", PREFIXO_TEMA)

    geral = novo_calendario(cal_geral, cal_desc, url_ics(url_base, pre + "agenda.ics"), "#0f4c6b")
    grupos = {}
    for pasta, lista in (("temas", TEMAS), ("tipos", TIPOS)):
        for g in lista:
            caminho = "{}{}/{}.ics".format(pre, pasta, g.slug)
            nome, resumo = nome_grupo(g, t)
            grupos[caminho] = novo_calendario(cal_pref + nome, resumo + ".", url_ics(url_base, caminho), g.cor)
    contagem = {"temas": {g.slug: 0 for g in TEMAS}, "tipos": {g.slug: 0 for g in TIPOS}}

    lista_classificados = classificados if classificados is not None else classificar_todos(itens)
    for item, tema, tipo in lista_classificados:
        contagem["temas"][tema.slug] += 1
        contagem["tipos"][tipo.slug] += 1
        geral.add_component(criar_evento(item, tema, tipo, t_ics, nomes))
        grupos["{}temas/{}.ics".format(pre, tema.slug)].add_component(criar_evento(item, tema, tipo, t_ics, nomes))
        grupos["{}tipos/{}.ics".format(pre, tipo.slug)].add_component(criar_evento(item, tema, tipo, t_ics, nomes))
        if lingua == "pt":
            logging.debug("%-22s %-22s %s", tema.slug, tipo.slug, limpar_texto(item.get("title")))

    ficheiros = {pre + "agenda.ics": geral.to_ical()}
    for caminho, cal in grupos.items():
        ficheiros[caminho] = cal.to_ical()
    proximos = eventos_proximos(lista_classificados, agora or hoje_utc(), t_ics, nomes, t)
    return ficheiros, contagem, proximos


# ---------------------------------------------------------------------------
# Página pública
# ---------------------------------------------------------------------------

def texto_contagem(n, t=None):
    if t is None:
        if n == 0:
            return "Sem eventos de momento"
        return "1 evento" if n == 1 else "{} eventos".format(n)
    if n == 0:
        return t["sem_eventos"]
    return t["evento_1"] if n == 1 else t["eventos_n"].format(n=n)


def texto_percentagem(n, total):
    """Parte da agenda atual, arredondada (\"<1%\" em vez de \"0%\" quando há eventos)."""
    if not total or not n:
        return "0%"
    p = 100.0 * n / total
    return "<1%" if p < 1 else "{}%".format(int(p + 0.5))


ICONE_CALENDARIO = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 10h18M12 13v5M9.5 15.5h5"/></svg>'
)
ICONE_COPIAR = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>'
)


def _estrela(cx, cy, r, rot=0.0):
    import math
    pts = []
    for i in range(10):
        ang = math.radians(-90 + rot + i * 36)
        rr = r if i % 2 == 0 else r * 0.382
        pts.append("{:.2f},{:.2f}".format(cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return '<polygon points="{}" fill="#ffde00"/>'.format(" ".join(pts))


# Bandeiras simplificadas (desenhadas aqui: os emojis de bandeira não aparecem no Windows).
BANDEIRAS = {
    "pt": '<rect width="30" height="20" fill="#da291c"/><rect width="12" height="20" fill="#046a38"/>'
          '<circle cx="12" cy="10" r="4.4" fill="#ffe900"/><rect x="10.2" y="7.4" width="3.6" height="5" rx="1" fill="#fff" stroke="#da291c" stroke-width=".8"/>',
    "gb": '<rect width="30" height="20" fill="#012169"/><path d="M0,0 30,20M30,0 0,20" stroke="#fff" stroke-width="4"/>'
          '<path d="M0,0 30,20M30,0 0,20" stroke="#c8102e" stroke-width="1.5"/><path d="M15,0V20M0,10H30" stroke="#fff" stroke-width="6"/>'
          '<path d="M15,0V20M0,10H30" stroke="#c8102e" stroke-width="3.4"/>',
    "es": '<rect width="30" height="20" fill="#aa151b"/><rect y="5" width="30" height="10" fill="#f1bf00"/>',
    "it": '<rect width="30" height="20" fill="#fff"/><rect width="10" height="20" fill="#009246"/><rect x="20" width="10" height="20" fill="#ce2b37"/>',
    "fr": '<rect width="30" height="20" fill="#fff"/><rect width="10" height="20" fill="#002654"/><rect x="20" width="10" height="20" fill="#ce1126"/>',
    "de": '<rect width="30" height="20" fill="#ffce00"/><rect width="30" height="6.67" fill="#000"/><rect y="6.67" width="30" height="6.67" fill="#dd0000"/>',
    "nl": '<rect width="30" height="20" fill="#21468b"/><rect width="30" height="13.33" fill="#fff"/><rect width="30" height="6.67" fill="#ae1c28"/>',
    "pl": '<rect width="30" height="20" fill="#dc143c"/><rect width="30" height="10" fill="#fff"/>',
    "jp": '<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#bc002d"/>',
    "cn": '<rect width="30" height="20" fill="#de2910"/>' + _estrela(5, 5, 3) + _estrela(10, 2, 1, 23) +
          _estrela(12, 4, 1, 45) + _estrela(12, 7, 1, 70) + _estrela(10, 9, 1, 20),
}


def bandeira(codigo):
    return ('<svg class="bandeira" viewBox="0 0 30 20" aria-hidden="true" focusable="false">{}</svg>'
            .format(BANDEIRAS.get(codigo, "")))


def links(url_base, caminho):
    https = url_ics(url_base, caminho)
    webcal = "webcal://" + https.split("://", 1)[1]
    return {
        "https": https,
        "webcal": webcal,
        # Abre o Google Calendar na conta com sessão iniciada e pede confirmação.
        "google": "https://calendar.google.com/calendar/render?cid=" + quote(webcal, safe=""),
        # Abre o Outlook na web (Microsoft 365) e pede confirmação.
        "outlook": "https://outlook.office.com/calendar/0/addfromweb?url=" + quote(webcal, safe=""),
    }


def bloco_subscricao(url_base, caminho, rotulo, t):
    l = {k: html.escape(v, quote=True) for k, v in links(url_base, caminho).items()}
    sub = html.escape(t["subscrever"])
    aria = html.escape("{}: {}".format(t["subscrever"], rotulo), quote=True)
    aria_copiar = html.escape("{}: {}".format(t["copiar"], rotulo), quote=True)
    return (
        '<div class="acoes">\n'
        '  <a class="botao so-google so-android" href="{google}" target="_blank" rel="noopener noreferrer" aria-label="{aria} (Google)">{i}{sub}</a>\n'
        '  <a class="botao so-outlook" href="{outlook}" target="_blank" rel="noopener noreferrer" aria-label="{aria} (Outlook)">{i}{sub}</a>\n'
        '  <a class="botao so-apple" href="{webcal}" target="_top" aria-label="{aria} (iPhone, iPad, Mac)">{i}{sub}</a>\n'
        '  <div class="copiar so-outro">\n'
        '    <button type="button" class="botao botao-copiar" data-link="{https}" aria-label="{aria_copiar}">{c}{copiar}</button>\n'
        '    <input type="text" readonly value="{https}" aria-label="{aria_copiar}">\n'
        '  </div>\n'
        '</div>'
    ).format(i=ICONE_CALENDARIO, c=ICONE_COPIAR, sub=sub, aria=aria, aria_copiar=aria_copiar,
             copiar=html.escape(t["copiar"]), **l)


def lista_grupos(grupos, pasta, contagem, total, url_base, lingua, t):
    linhas = []
    pre = prefixo_lingua(lingua)
    for g in grupos:
        n = contagem[g.slug]
        pct = 100.0 * n / total if total else 0.0
        nome, resumo = nome_grupo(g, t if lingua != "pt" else None)
        linhas.append(
            '<li class="cartao{vazio}" style="--cor: {cor}; --pct: {pct:.1f}%">\n'
            '  <h3>{nome}</h3>\n'
            '  <p class="resumo">{resumo}</p>\n'
            '  <span class="barra" aria-hidden="true"><span></span></span>\n'
            '  <div class="rodape">\n'
            '    <p class="valor">{contagem}{percentagem}</p>\n'
            '    {acoes}\n'
            '  </div>\n'
            '</li>'.format(
                vazio=" vazio" if n == 0 else "",
                cor=html.escape(g.cor, quote=True),
                pct=pct,
                nome=html.escape(nome),
                resumo=html.escape(resumo),
                contagem=html.escape(texto_contagem(n, t)),
                percentagem=" <small>· {}</small>".format(
                    html.escape(t["da_agenda"].format(p=texto_percentagem(n, total)))) if n else "",
                acoes=bloco_subscricao(url_base, "{}{}/{}.ics".format(pre, pasta, g.slug), nome, t),
            )
        )
    return "\n".join(linhas)


def logotipo_data_uri():
    if not LOGOTIPO.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(LOGOTIPO.read_bytes()).decode()


def caminho_pagina(de, para):
    """Link relativo da página da língua `de` para a da língua `para`."""
    raiz = "" if de == "pt" else "../../"
    return raiz + prefixo_lingua(para) + "index.html"


def seletor_linguas(lingua, trad):
    itens = []
    for cod in trad["ordem"]:
        t = trad["linguas"][cod]
        atual = ' aria-current="page"' if cod == lingua else ""
        itens.append(
            '<li><a href="{href}" hreflang="{hl}" lang="{hl}"{atual} data-lingua="{cod}">{b}<span>{nome}</span></a></li>'.format(
                href=caminho_pagina(lingua, cod), hl=t["html_lang"], atual=atual, cod=cod,
                b=bandeira(t["bandeira"]), nome=html.escape(t["nome"])))
    return "\n        ".join(itens)


def sugestoes_linguas(lingua, trad):
    """Uma frase por língua ("Ver esta página em …"), mostrada só a quem tem o navegador nessa língua."""
    itens = []
    for cod in trad["ordem"]:
        if cod == lingua:
            continue
        t = trad["linguas"][cod]
        itens.append('<li data-lingua="{cod}" lang="{hl}"><a href="{href}" hreflang="{hl}">{b}{txt}</a></li>'.format(
            cod=cod, hl=t["html_lang"], href=caminho_pagina(lingua, cod), b=bandeira(t["bandeira"]),
            txt=html.escape(t["ver_em"])))
    return "\n      ".join(itens)


# Eventos com «cancelado/a» no título: ficam marcados como cancelados no calendário.
CANCELADO_RE = re.compile(r"\bcancelad[oa]s?\b", re.I)


def hoje_utc():
    """Meia-noite de hoje, hora de Lisboa, em UTC (serve de "agora" para os próximos eventos)."""
    return datetime.now(LISBOA).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def eventos_proximos(classificados, agora, t_ics, nomes, t=None):
    """Eventos dos próximos JANELA_PROXIMOS_DIAS dias, já com o VEVENT pronto (mesma serialização
    do resto do site) para a secção "Próximos eventos" montar o .ics de um ou vários sem rede."""
    limite = agora + timedelta(days=JANELA_PROXIMOS_DIAS)
    resultado = []
    for item, tema, tipo in classificados:
        inicio = parse_iso(item.get("start"))
        if inicio is None:
            continue
        fim = parse_iso(item.get("end")) or (inicio + timedelta(hours=1))
        if fim < inicio:
            fim = inicio + timedelta(hours=1)
        if fim < agora or inicio > limite:
            continue
        ev = criar_evento(item, tema, tipo, t_ics, nomes)
        if str(ev.get("status") or "") == "CANCELLED":
            continue
        resultado.append({
            "titulo": str(ev.get("summary") or ""),
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
            "diaInteiro": bool(eh_dia_inteiro(inicio, fim)),
            "local": str(ev.get("location") or ""),
            "temaSlug": tema.slug, "temaNome": nome_grupo(tema, t)[0], "temaCor": tema.cor,
            "tipoSlug": tipo.slug, "tipoNome": nome_grupo(tipo, t)[0], "tipoCor": tipo.cor,
            "vevent": ev.to_ical().decode("utf-8"),
        })
    return resultado


def _data_curta(dt):
    dt = dt.astimezone(LISBOA)
    return dt.strftime("%d/%m/%Y %H:%M")


def _chip_proximo(slug, nome, cor, classe):
    return (
        '<input type="checkbox" class="escondido chip-filtro {classe}" data-slug="{slug}" id="{classe}-{slug}">\n'
        '<label for="{classe}-{slug}" style="--cor: {cor}">{nome}</label>'
    ).format(classe=classe, slug=html.escape(slug, quote=True), cor=html.escape(cor, quote=True), nome=html.escape(nome))


def secao_proximos(proximos, t):
    """Linhas da lista de "próximos eventos" + filtros (só os temas/tipos com eventos nos próximos dias)."""
    linhas = []
    temas_vistos, tipos_vistos = {}, {}
    for idx, p in enumerate(proximos):
        inicio = parse_iso(p["inicio"])
        if p["diaInteiro"]:
            quando = _data_curta(inicio).split(" ")[0] + " · " + t["proximos_dia_inteiro"]
        else:
            quando = _data_curta(inicio)
        local = p["local"]
        ics_unico = (
            "BEGIN:VCALENDAR\r\nPRODID:{prodid}\r\nVERSION:2.0\r\nCALSCALE:GREGORIAN\r\n{vevent}END:VCALENDAR\r\n"
        ).format(prodid=PRODID, vevent=p["vevent"])
        href = "data:text/calendar;charset=utf-8," + quote(ics_unico.encode("utf-8"))
        linhas.append((
            '<li class="item-proximo" data-idx="{idx}" data-tema="{tema_slug}" data-tipo="{tipo_slug}" '
            'style="--ct: {cor_tema}; --cp: {cor_tipo}">\n'
            '  <span class="marcar-coluna">\n'
            '    <input type="checkbox" class="marcar-evento" id="proximo-{idx}" aria-label="{aria_marcar}">\n'
            '  </span>\n'
            '  <label for="proximo-{idx}" class="info-proximo">\n'
            '    <span class="titulo-proximo">{titulo}</span>\n'
            '    <span class="meta-proximo"><time datetime="{iso}">{quando}</time>{local_html}</span>\n'
            '    <span class="etiquetas-proximo"><span class="etiqueta et-tema">{tema_nome}</span>'
            '<span class="etiqueta et-tipo">{tipo_nome}</span></span>\n'
            '  </label>\n'
            '  <a class="botao botao-adicionar-um" href="{href}" download="evento.ics" aria-label="{aria_add}">{icone}<span>{rotulo}</span></a>\n'
            '</li>'
        ).format(
            idx=idx,
            tema_slug=html.escape(p["temaSlug"], quote=True), tipo_slug=html.escape(p["tipoSlug"], quote=True),
            cor_tema=html.escape(p["temaCor"], quote=True), cor_tipo=html.escape(p["tipoCor"], quote=True),
            aria_marcar=html.escape("{}: {}".format(t["selecionar_evento"], p["titulo"]), quote=True),
            titulo=html.escape(p["titulo"]),
            iso=html.escape(p["inicio"], quote=True), quando=html.escape(quando),
            local_html=" · {}".format(html.escape(local)) if local else "",
            tema_nome=html.escape(p["temaNome"]), tipo_nome=html.escape(p["tipoNome"]),
            href=href, aria_add=html.escape("{}: {}".format(t["adicionar_um"], p["titulo"]), quote=True),
            icone=ICONE_CALENDARIO, rotulo=html.escape(t["adicionar_um"]),
        ))
        temas_vistos[p["temaSlug"]] = (p["temaNome"], p["temaCor"])
        tipos_vistos[p["tipoSlug"]] = (p["tipoNome"], p["tipoCor"])
    chips_temas = "\n".join(
        _chip_proximo(g.slug, temas_vistos[g.slug][0], temas_vistos[g.slug][1], "chip-tema")
        for g in TEMAS if g.slug in temas_vistos)
    chips_tipos = "\n".join(
        _chip_proximo(g.slug, tipos_vistos[g.slug][0], tipos_vistos[g.slug][1], "chip-tipo")
        for g in TIPOS if g.slug in tipos_vistos)
    return "\n".join(linhas), chips_temas, chips_tipos


def gerar_pagina(contagem, total, url_base, lingua="pt", trad=None, proximos=None):
    trad = trad or carregar_traducoes()
    t = dict(trad["linguas"]["pt"])
    t.update(trad["linguas"][lingua])  # chaves novas ainda sem tradução ficam em português, nunca falham
    modelo = MODELO_PAGINA.read_text(encoding="utf-8")

    script = re.search(r"<script>(.*?)</script>", modelo, re.S)
    hash_script = base64.b64encode(hashlib.sha256(script.group(1).encode("utf-8")).digest()).decode() if script else ""

    pre = prefixo_lingua(lingua)
    proximos = proximos or []
    linhas_proximos, chips_temas, chips_tipos = secao_proximos(proximos, t)
    dados_proximos = json.dumps(
        {"prodid": PRODID, "eventos": [{"vevent": p["vevent"]} for p in proximos]},
        ensure_ascii=False, separators=(",", ":"),
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    substituicoes = {
        "{{HASH_SCRIPT}}": hash_script,
        "{{LINGUA}}": t["html_lang"],
        "{{CODIGO_LINGUA}}": lingua,
        "{{RAIZ}}": "" if lingua == "pt" else "../../",
        "{{LOGOTIPO}}": logotipo_data_uri(),
        "{{BANDEIRA}}": bandeira(t["bandeira"]),
        "{{SELETOR_LINGUAS}}": seletor_linguas(lingua, trad),
        "{{SUGESTOES}}": sugestoes_linguas(lingua, trad),
        "{{ACOES_GERAL}}": bloco_subscricao(url_base, pre + "agenda.ics", t["completa_titulo"], t),
        "{{LISTA_TEMAS}}": lista_grupos(TEMAS, "temas", contagem["temas"], total, url_base, lingua, t),
        "{{LISTA_TIPOS}}": lista_grupos(TIPOS, "tipos", contagem["tipos"], total, url_base, lingua, t),
        "{{LISTA_PROXIMOS}}": linhas_proximos,
        "{{CHIPS_TEMAS_PROXIMOS}}": chips_temas,
        "{{CHIPS_TIPOS_PROXIMOS}}": chips_tipos,
        "{{DADOS_PROXIMOS}}": dados_proximos,
        "{{ALTERNATIVAS}}": "\n".join(
            '<link rel="alternate" hreflang="{}" href="{}">'.format(
                trad["linguas"][c]["html_lang"], html.escape(url_base + prefixo_lingua(c), quote=True))
            for c in trad["ordem"]),
        "{{URL_PAGINA}}": html.escape(url_base + pre, quote=True),
    }
    for chave, valor in substituicoes.items():
        modelo = modelo.replace(chave, valor)

    def texto(m):
        chave = m.group(1)
        valor = t[chave]
        if chave == "completa_texto":
            return html.escape(valor.format(total=texto_contagem(total, t)))
        if chave == "rodape_html":
            return valor.format(fonte=html.escape(FONTE_PAGINA, quote=True))
        return valor if chave.endswith("_html") else html.escape(valor, quote=True)

    modelo = re.sub(r"\{\{T:([a-z0-9_]+)\}\}", texto, modelo)
    falta = re.findall(r"\{\{[^}]+\}\}", modelo)
    if falta:
        raise ValueError("Marcadores por preencher no modelo: {}".format(sorted(set(falta))))
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
    """Escreve os calendários e a página em todas as línguas. Devolve a contagem (igual em todas)."""
    trad = carregar_traducoes()
    classificados = classificar_todos(itens)
    agora = hoje_utc()
    contagem = None
    # Restos de versões anteriores (ficheiros por evento): apagar.
    if (pasta / "e").is_dir():
        for antigo in (pasta / "e").glob("*.ics"):
            antigo.unlink()
        try:
            (pasta / "e").rmdir()
        except OSError:
            pass
    for lingua in trad["ordem"]:
        t = trad["linguas"][lingua]
        ficheiros, contagem, proximos = construir_calendarios(
            itens, url_base, lingua, t if lingua != "pt" else None, classificados, agora)
        ficheiros[prefixo_lingua(lingua) + "index.html"] = gerar_pagina(contagem, len(itens), url_base, lingua, trad, proximos)
        for caminho, conteudo in ficheiros.items():
            escrever_atomico(conteudo, pasta / caminho)
        for sub in ("temas", "tipos"):
            dir_sub = pasta / (prefixo_lingua(lingua) + sub)
            atuais = {Path(c).name for c in ficheiros if c.startswith(prefixo_lingua(lingua) + sub + "/")}
            for antigo in dir_sub.glob("*.ics"):
                if antigo.name not in atuais:
                    antigo.unlink()
    if pasta.resolve() != PASTA_SCRIPT and PASTA_FONTES.is_dir():
        # para a página funcionar também quando é gerada noutra pasta
        (pasta / "fontes").mkdir(exist_ok=True)
        for fonte in PASTA_FONTES.glob("*.woff2"):
            shutil.copy2(fonte, pasta / "fontes" / fonte.name)
    return contagem


def main():
    parser = argparse.ArgumentParser(description="Gera a agenda municipal em .ics (geral, por tema e por tipo).")
    parser.add_argument("--pasta-saida", type=Path, default=PASTA_SCRIPT)
    parser.add_argument("--url-publica", default=URL_PUBLICA, help="Endereço público dos ficheiros (termina em /)")
    parser.add_argument("--horizonte-meses", type=int, default=18)
    parser.add_argument("--detalhe", action="store_true", help="Mostra o tema e o tipo atribuídos a cada evento")
    parser.add_argument("--campos", action="store_true", help="Mostra os campos que o site devolve e termina")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.detalhe else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    url_base = args.url_publica if args.url_publica.endswith("/") else args.url_publica + "/"

    if args.campos:
        mostrar_campos(obter_sessao())
        return

    hoje = hoje_utc()
    horizonte = hoje + timedelta(days=30 * args.horizonte_meses)

    sessao = obter_sessao()
    try:
        itens = obter_eventos(sessao, hoje, horizonte)
    except Exception as exc:
        logging.error("Recolha falhou; os ficheiros publicados ficam como estavam. %s", exc)
        sys.exit(1)

    if not itens:
        # Uma agenda municipal vazia é quase certamente uma falha do site:
        # não se apaga o que as pessoas já têm no calendário.
        logging.error("Nenhum evento recebido; os ficheiros publicados ficam como estavam.")
        sys.exit(1)

    itens += obter_jogos_varzim(sessao, hoje, horizonte)

    try:
        contagem = gerar_saidas(itens, args.pasta_saida, url_base)
    except Exception:
        logging.exception("Falha ao escrever os ficheiros.")
        sys.exit(1)

    logging.info("Total: %s eventos", len(itens))
    for tema in TEMAS:
        logging.info("  %-34s %s", tema.nome, contagem["temas"][tema.slug])
    for tipo in TIPOS:
        logging.info("  %-34s %s", tipo.nome, contagem["tipos"][tipo.slug])


if __name__ == "__main__":
    main()
