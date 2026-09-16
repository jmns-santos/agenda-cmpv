# -*- coding: utf-8 -*-
"""
gerar_agenda_ics.py

Le a agenda de eventos do site da Camara Municipal da Povoa de Varzim
(cm-pvarzim.pt) atraves do endpoint JSON que o site ja expoe
(wp-json/plone/v1/event) e gera um ficheiro .ics (iCalendar) com os
eventos futuros, pronto a publicar num link do site ou a ser
subscrito diretamente num calendario de telemovel.

Pensado para correr periodicamente (ex. Windows Task Scheduler, tal
como os outros scripts de integracao do Jorge na VM), sobrescrevendo
sempre o mesmo ficheiro .ics de forma atomica. Se a recolha falhar
por completo (ex. site em baixo, sem rede), o ficheiro .ics anterior
e' mantido intacto - nunca e' substituido por um ficheiro vazio.

Uso:
    python gerar_agenda_ics.py
    python gerar_agenda_ics.py --output C:\caminho\agenda.ics --horizonte-meses 12

Requisitos: ver requirements.txt (requests, icalendar)
"""

import argparse
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

try:
    from icalendar import Calendar, Event, vText
except ImportError:
    print(
        "Falta o pacote 'icalendar'. Instala as dependencias com:\n"
        "    pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise

try:
    from zoneinfo import ZoneInfo
    LISBOA = ZoneInfo("Europe/Lisbon")
except Exception:
    # Em alguns Windows sem tzdata instalado, zoneinfo falha.
    # 'pip install tzdata' resolve. Sem isso, cai-se para UTC
    # apenas para o calculo de "e dia inteiro?" (nao afeta a hora
    # gravada no .ics, que fica sempre em UTC).
    LISBOA = timezone.utc

BASE_URL = "https://www.cm-pvarzim.pt/wp-json/plone/v1/event"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "agenda.ics"
DEFAULT_LOG = Path(__file__).resolve().parent / "gerar_agenda_ics.log"

CALNAME = "Agenda Municipal - Povoa de Varzim"
PRODID = "-//CMPV//Agenda de Eventos//PT"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


class FalhaRecolha(RuntimeError):
    """Levantado quando nao foi possivel obter dados nenhuns do site
    (para nunca se confundir com 'nao ha eventos futuros', que e' um
    resultado valido)."""


def limpar_texto(txt):
    """Remove tags HTML residuais e normaliza espacos, sem dependencias extra."""
    if not txt:
        return ""
    txt = TAG_RE.sub(" ", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&")
    txt = WS_RE.sub(" ", txt)
    return txt.strip()


def parse_iso(dt_str):
    """Converte strings ISO 8601 devolvidas pela API (ex. 2026-09-16T09:00:00+00:00,
    que e' UTC real) para datetime timezone-aware em UTC. Devolve None se vazio/invalido."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def eh_dia_inteiro(inicio_utc, fim_utc):
    """Heuristica: evento e' considerado 'dia inteiro' (ou varios dias completos)
    se, em hora de Lisboa, comeca a meia-noite e dura pelo menos ~23h."""
    if inicio_utc is None or fim_utc is None:
        return False
    inicio_local = inicio_utc.astimezone(LISBOA)
    duracao = fim_utc - inicio_utc
    return inicio_local.hour == 0 and inicio_local.minute == 0 and duracao >= timedelta(hours=23)


def obter_sessao():
    sessao = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    sessao.mount("https://", HTTPAdapter(max_retries=retries))
    sessao.headers.update({"User-Agent": "CMPV-AgendaICS/1.0 (+cm-pvarzim.pt)"})
    return sessao


def obter_eventos_futuros(sessao, hoje, horizonte, max_paginas=60, timeout=20):
    """Percorre o endpoint plone/v1/event paginado, filtrando por data de inicio
    a partir de hoje (com o proprio filtro da API) e, por seguranca, tambem
    localmente. Para quando uma pagina vem vazia, quando se atinge max_paginas,
    ou quando varias paginas seguidas ja nao trazem eventos dentro do horizonte.

    Se a primeira pagina falhar (sem conseguir sequer contactar o site), levanta
    FalhaRecolha - isto e' tratado pelo chamador como falha critica, para nunca
    se sobrescrever um agenda.ics bom por um vazio."""
    logging.info("A obter eventos a partir de %s ate %s", hoje.date(), horizonte.date())

    eventos = {}
    paginas_sem_novidade = 0
    pagina = 1

    while pagina <= max_paginas:
        params = {
            "start.query": hoje.date().isoformat(),
            "start.range": "min",
            "sort_on": "start",
            "sort_order": "ascending",
            "paged": pagina,
        }
        try:
            resp = sessao.get(BASE_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            dados = resp.json()
        except (requests.RequestException, ValueError) as exc:
            if pagina == 1 and not eventos:
                raise FalhaRecolha("Nao foi possivel contactar {}: {}".format(BASE_URL, exc)) from exc
            logging.warning("Falha ao obter a pagina %s, a continuar com o que ja foi recolhido: %s", pagina, exc)
            break

        itens = dados.get("items") or []
        if not itens:
            logging.info("Pagina %s vazia, fim da paginacao.", pagina)
            break

        novos_nesta_pagina = 0
        for item in itens:
            inicio = parse_iso(item.get("start"))
            if inicio is None:
                continue
            if inicio < hoje or inicio > horizonte:
                continue
            chave = item.get("uid") or item.get("id") or item.get("url")
            if chave and chave not in eventos:
                eventos[chave] = item
                novos_nesta_pagina += 1

        if novos_nesta_pagina == 0:
            paginas_sem_novidade += 1
        else:
            paginas_sem_novidade = 0

        # Se ja levamos 3 paginas seguidas sem trazer nada de util dentro do
        # horizonte, nao vale a pena continuar a paginar.
        if paginas_sem_novidade >= 3:
            logging.info("3 paginas seguidas sem eventos novos no horizonte, a parar.")
            break

        pagina += 1

    logging.info("Total de eventos futuros recolhidos: %s", len(eventos))
    return list(eventos.values())


def construir_calendario(itens, ttl_horas):
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", CALNAME)
    cal.add("x-wr-timezone", "Europe/Lisbon")
    cal.add("x-published-ttl", "PT{}H".format(ttl_horas))

    agora = datetime.now(timezone.utc)

    for item in itens:
        inicio = parse_iso(item.get("start"))
        fim = parse_iso(item.get("end")) or (inicio + timedelta(hours=1) if inicio else None)
        if inicio is None:
            continue

        ev = Event()
        chave = item.get("uid") or item.get("id") or item.get("url")
        ev.add("uid", "{}@agenda.cm-pvarzim.pt".format(chave))
        ev.add("dtstamp", agora)
        ev.add("summary", vText(limpar_texto(item.get("title") or "(sem titulo)")))

        if eh_dia_inteiro(inicio, fim):
            data_inicio = inicio.astimezone(LISBOA).date()
            data_fim = fim.astimezone(LISBOA).date()
            ev.add("dtstart", data_inicio)
            # DTEND em VEVENT de dia-inteiro e' sempre exclusivo (RFC 5545) ->
            # soma sempre 1 dia ao ultimo dia do evento, mesmo em eventos de 1 dia so.
            ev.add("dtend", data_fim + timedelta(days=1))
        else:
            ev.add("dtstart", inicio)
            ev.add("dtend", fim)

        local = limpar_texto(item.get("location") or "")
        if local:
            ev.add("location", vText(local))

        descricao = limpar_texto(item.get("description") or item.get("text") or "")
        url_evento = item.get("url") or item.get("eventUrl") or ""
        partes_desc = [p for p in (descricao, ("Mais informacao: " + url_evento) if url_evento else "") if p]
        if partes_desc:
            ev.add("description", vText("\n\n".join(partes_desc)))

        if url_evento:
            ev.add("url", url_evento)

        modificado = parse_iso(item.get("modified"))
        if modificado:
            ev.add("last-modified", modificado)

        cal.add_component(ev)

    return cal


def escrever_atomico(conteudo_bytes, destino: Path):
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".agenda_tmp_", dir=str(destino.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo_bytes)
        os.replace(tmp_path, destino)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description="Gera agenda.ics a partir da agenda de eventos do site da CMPV.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Caminho do ficheiro .ics a gerar")
    parser.add_argument("--horizonte-meses", type=int, default=18, help="Quantos meses para a frente incluir")
    parser.add_argument("--ttl-horas", type=int, default=6, help="TTL sugerido aos leitores de calendario (X-PUBLISHED-TTL)")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG, help="Ficheiro de log")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log_file, encoding="utf-8"), logging.StreamHandler()],
    )

    hoje = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    horizonte = hoje + timedelta(days=30 * args.horizonte_meses)

    sessao = obter_sessao()
    try:
        itens = obter_eventos_futuros(sessao, hoje, horizonte)
    except FalhaRecolha as exc:
        logging.error("Recolha falhou por completo, agenda.ics anterior mantido intacto: %s", exc)
        sys.exit(1)
    except Exception:
        logging.exception("Falha inesperada a obter eventos. Ficheiro .ics anterior mantido.")
        sys.exit(1)

    if not itens:
        logging.warning("Nenhum evento futuro encontrado (recolha teve sucesso, lista veio vazia mesmo).")

    cal = construir_calendario(itens, args.ttl_horas)

    try:
        escrever_atomico(cal.to_ical(), args.output)
    except Exception:
        logging.exception("Falha ao escrever o ficheiro .ics. Ficheiro anterior mantido intacto.")
        sys.exit(1)

    logging.info("agenda.ics atualizado com sucesso: %s (%s eventos)", args.output, len(itens))


if __name__ == "__main__":
    main()
