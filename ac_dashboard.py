#!/usr/bin/env python3
"""
ActiveCampaign Daily Dashboard
Gera um dashboard HTML com métricas de campanhas e contatos por lista/tag.
"""

import os
import sys
import json
import base64
import requests
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from ac_client import AC_API_KEY, ac_get

load_dotenv()

# ─── Limites fixos da conta ───────────────────────────────────────────────────
CONTACT_LIMIT = 1_000_000   # 1 milhão de contatos
SENDING_LIMIT = 16_000_000  # 16 milhões de disparos/mês


class _TeeLogger:
    """Redireciona stdout para console e arquivo de log simultaneamente."""
    def __init__(self, log_path: Path):
        self.terminal = sys.__stdout__
        log_path.parent.mkdir(exist_ok=True)
        self.log = open(log_path, "w", encoding="utf-8", buffering=1)

    def write(self, msg):
        self.terminal.write(msg)
        self.log.write(msg)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()

# ─── Listas monitoradas ───────────────────────────────────────────────────────
MONITORED_LISTS = [
    {"name": "Alunos - FNAT",                   "id": 71},
    {"name": "Excel - Conteúdo",                "id": 2},
    {"name": "Full Stack - Conteúdo",           "id": 42},
    {"name": "Hashtag Capacitaciones",          "id": 59},
    {"name": "Leads - FNAT",                    "id": 72},
    {"name": "Leads da Comunidade Hashtag",     "id": 23},
    {"name": "No Code - Conteúdo",              "id": 73},
    {"name": "Power BI - Conteúdo",             "id": 15},
    {"name": "Python - Conteúdo",               "id": 30},
    {"name": "Inteligência Artificial - Conteúdo", "id": 53},
]

# ─── Tags monitoradas ─────────────────────────────────────────────────────────
MONITORED_TAGS = [
    {"name": "CLIENTE_CH",                  "id": 731},
    {"name": "CLIENTEONLINE",               "id": 1352},
    {"name": "CLIENTE_ASSINATURAVITALICIA", "id": 6065},
    {"name": "CAPACITACIONES_EXCEL",        "id": 8037},
    {"name": "CAPACITACIONES_IA",           "id": 12080},
    {"name": "FNAT_ALUNO_DADOSIA",          "id": 11178},
    {"name": "FNAT_ALUNO_MBA",              "id": 13617},

    #------------ Listas para serem usadas durante o vitalício-----------------
    #{"name": "LVIT4_ORG_LISTAALUNOS",       "id": 14919},
    #{"name": "LVIT4_ORG_LISTAEXCEL",        "id": 14920},
    #{"name": "LVIT4_ORG_LISTAPBI",          "id": 15061},
    #{"name": "LVIT4_ORG_LISTAPYTHON",       "id": 14922},
    #{"name": "LVIT4_ORG_LISTAIA",           "id": 14923},
    #{"name": "LVIT4_ORG_LISTANOCODE",       "id": 14928},
    #{"name": "LVIT4_ORG_LISTACOMUNIDADE",   "id": 14927},
]


# ─── Helpers de API ───────────────────────────────────────────────────────────

def resolve_tag_ids():
    """Busca IDs das tags pelo nome diretamente (sem paginar tudo)."""
    print("  Buscando IDs de tags...")
    for entry in MONITORED_TAGS:
        if entry["id"] is not None:
            print(f"    ✓ {entry['name']} → ID {entry['id']} (já conhecido)")
            continue
        try:
            data = ac_get("tags", {"search": entry["name"], "limit": 10})
            tags = data.get("tags", [])
            # Busca match exato (case-insensitive)
            match = next((t for t in tags if t["tag"].upper() == entry["name"].upper()), None)
            if match:
                entry["id"] = int(match["id"])
                print(f"    ✓ {entry['name']} → ID {match['id']}")
            else:
                print(f"    ✗ Tag não encontrada: {entry['name']}")
        except Exception as e:
            print(f"    ✗ Erro ao buscar tag '{entry['name']}': {e}")


def parse_sdate(sdate_str):
    """Converte sdate para datetime aware (com timezone)."""
    if not sdate_str:
        return None
    try:
        # fromisoformat suporta offset como -05:00 no Python 3.7+
        return datetime.fromisoformat(sdate_str.replace(" ", "T"))
    except Exception:
        return None


def get_campaigns(prefix_filter="", days_back=30):
    print(f"  Buscando campanhas (ultimos {days_back} dias)...")
    now_utc = datetime.now(timezone.utc)
    cutoff  = now_utc - timedelta(days=days_back)
    # Exclui campanhas do dia atual e futuras — upper bound = meia-noite de hoje (BRT)
    brt_tz      = timezone(timedelta(hours=-3))
    today_brt   = datetime.now(brt_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    upper_bound = today_brt.astimezone(timezone.utc)

    offset  = 0
    results = []
    stop    = False

    while not stop:
        data = ac_get("campaigns", {
            "limit": 100,
            "offset": offset,
            "orders[id]": "DESC",   # mais confiavel que sdate
        })
        campaigns = data.get("campaigns", [])
        if not campaigns:
            break

        for c in campaigns:
            sdate = parse_sdate(c.get("sdate") or "")

            if sdate is not None:
                if sdate.tzinfo is None:
                    sdate = sdate.replace(tzinfo=timezone.utc)
                # Para paginação: se sdate é mais antiga que o cutoff, interrompe
                if sdate < cutoff:
                    stop = True
                    break
                # Pula campanhas de hoje e futuras (agendadas mas ainda não enviadas)
                if sdate >= upper_bound:
                    continue

            # Pula rascunhos sem data (nunca agendados)
            status = str(c.get("status", "0"))
            if sdate is None and status == "0":
                continue

            if prefix_filter:
                name    = c.get("name", "")
                subject = c.get("subject", "")
                if prefix_filter.upper() not in name.upper() and prefix_filter.upper() not in subject.upper():
                    continue

            results.append(c)

        offset += len(campaigns)
        if len(campaigns) < 100:
            break

    print(f"  -> {len(results)} campanhas encontradas")
    return results


def enrich_campaign(c: dict, segment_names: dict = None) -> dict:
    """Extrai e calcula métricas de uma campanha."""
    sends    = int(c.get("send_amt", 0) or 0)
    opens    = int(c.get("uniqueopens", 0) or 0)
    clicks   = int(c.get("uniquelinkclicks", 0) or 0)
    unsubs   = int(c.get("unsubscribes", 0) or 0)
    bounces  = int(c.get("hardbounces", 0) or 0) + int(c.get("softbounces", 0) or 0)

    open_rate  = round((opens  / sends * 100), 2) if sends > 0 else 0.0
    click_rate = round((clicks / sends * 100), 2) if sends > 0 else 0.0

    # Alertas
    alerts = []
    status_raw = str(c.get("status", "0"))
    now_utc = datetime.now(timezone.utc)

    if status_raw == "5":
        # Enviada: verifica metricas
        if sends > 0 and opens == 0:
            alerts.append("Abertura zerada — possivel bug de envio ou problema de entrega")
        if sends > 0 and open_rate > 150:
            alerts.append("Open rate acima de 150% — verifique a campanha")
        if sends < 100 and sends > 0:
            alerts.append(f"Apenas {sends} envios — segmentacao muito pequena?")
        bounce_rate_val = round(bounces / sends * 100, 2) if sends > 0 else 0
        if bounce_rate_val > 1:
            alerts.append(f"Bounce rate {bounce_rate_val}% — acima de 1%, verifique a lista")

    elif status_raw == "1":
        # Agendada: se ja tem envios reais, o AC nao atualizou o status — tratar como enviada
        if sends > 0:
            # Tem metricas: ja foi enviado de fato, verifica normalmente
            if opens == 0 and clicks == 0:
                alerts.append("Abertura e clique zerados — possivel bug de envio")
            if open_rate > 150:
                alerts.append("Open rate acima de 150% — verifique a campanha")
            if sends < 100:
                alerts.append(f"Apenas {sends} envios — segmentacao muito pequena?")
            bounce_rate_val = round(bounces / sends * 100, 2) if sends > 0 else 0
            if bounce_rate_val > 1:
                alerts.append(f"Bounce rate {bounce_rate_val}% — acima de 1%, verifique a lista")
        else:
            # Sem envios: alerta se horario ja passou
            raw_sdate = c.get("sdate") or ""
            if raw_sdate:
                try:
                    sdt = datetime.fromisoformat(raw_sdate.replace(" ", "T"))
                    if sdt.tzinfo is None:
                        sdt = sdt.replace(tzinfo=timezone.utc)
                    if sdt < now_utc:
                        brt = sdt.astimezone(timezone(timedelta(hours=-3)))
                        alerts.append(f"Agendada para {brt.strftime('%d/%m %H:%M')} BRT mas ainda nao enviada")
                except Exception:
                    pass

    elif status_raw == "2":
        # Pausada: sempre alerta
        alerts.append("Campanha pausada — verifique se o envio foi interrompido pelo Active Campaign")
        # Se tem envios mas abertura zero, reforça o alerta
        if sends > 0 and opens == 0:
            alerts.append("Abertura zerada apos pausa — verifique se chegou aos destinatarios")

    sdate = c.get("sdate", "") or ""
    try:
        dt = datetime.fromisoformat(sdate.replace(" ", "T"))
        if dt.tzinfo is None:
          dt = dt.replace(tzinfo=timezone.utc)
        sdate = dt.astimezone(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
    except Exception:
        sdate = sdate[:10] if sdate else "—"

    segment_id   = int(c.get("segmentid", 0) or 0)
    segment_name = (segment_names or {}).get(segment_id, "") if segment_id else ""

    return {
        "id":           c.get("id"),
        "name":         c.get("name", "—"),
        "subject":      c.get("subject", "—"),
        "sdate":        sdate,
        "segmentname":  segment_name,
        "sends":        sends,
        "opens":        opens,
        "clicks":       clicks,
        "unsubs":       unsubs,
        "bounces":      bounces,
        "open_rate":    open_rate,
        "click_rate":   click_rate,
        "bounce_rate":  round(bounces / sends * 100, 2) if sends > 0 else 0.0,
        "alerts":       alerts,
        "status_raw":   str(c.get("status", "0")),
        "status":       {
            "0": "Rascunho",
            "1": "Agendado",
            "2": "Pausado",
            "5": "Enviado",
            "6": "Enviando",
        }.get(str(c.get("status", "0")), f"status {c.get('status')}"),
    }


def get_list_contact_count(list_id):
    # GET /api/3/lists/{id} retorna subscriber_count diretamente
    data = ac_get(f"lists/{list_id}")
    lst = data.get("list", {})
    # subscriber_count = ativos; tenta campos alternativos
    for field in ("subscriber_count", "subscriberCount", "subscribers"):
        val = lst.get(field)
        if val is not None:
            return int(val)
    # Fallback: conta via contacts endpoint
    fallback = ac_get("contacts", {"listid": list_id, "status": 1, "limit": 1})
    return int(fallback.get("meta", {}).get("total", 0))


def get_tag_contact_count(tag_id: int) -> int:
    """Conta contatos com uma tag específica."""
    data = ac_get("contacts", {
        "tagid": tag_id,
        "status": 1,
        "limit": 1,
    })
    return int(data.get("meta", {}).get("total", 0))


def get_account_info() -> dict:
    return {
        "contact_limit": CONTACT_LIMIT,
        "sending_limit": SENDING_LIMIT,
    }


BILLING_RESET_DAY = 10  # dia do mês em que o limite de disparos reseta

def get_monthly_sends() -> int:
    """Soma e-mails enviados no ciclo de cobrança corrente (reseta todo dia 10)."""
    brt_tz = timezone(timedelta(hours=-3))
    now_brt = datetime.now(brt_tz)

    # Período começa no dia 10 deste mês; se ainda não chegou, do mês anterior
    if now_brt.day >= BILLING_RESET_DAY:
        period_start = now_brt.replace(day=BILLING_RESET_DAY, hour=0, minute=0, second=0, microsecond=0)
    else:
        prev_month = now_brt.month - 1 or 12
        prev_year  = now_brt.year if now_brt.month > 1 else now_brt.year - 1
        period_start = now_brt.replace(year=prev_year, month=prev_month, day=BILLING_RESET_DAY,
                                       hour=0, minute=0, second=0, microsecond=0)

    month_start_utc = period_start.astimezone(timezone.utc)

    offset = 0
    total = 0
    stop = False

    while not stop:
        data = ac_get("campaigns", {"limit": 100, "offset": offset, "orders[id]": "DESC"})
        campaigns = data.get("campaigns", [])
        if not campaigns:
            break
        for c in campaigns:
            sdate = parse_sdate(c.get("sdate") or "")
            if sdate is None:
                continue
            if sdate.tzinfo is None:
                sdate = sdate.replace(tzinfo=timezone.utc)
            if sdate < month_start_utc:
                stop = True
                break
            total += int(c.get("send_amt", 0) or 0)
        offset += len(campaigns)
        if len(campaigns) < 100:
            break

    return total


def get_total_contacts() -> int:
    """Retorna o total de contatos ativos na conta."""
    try:
        data = ac_get("contacts", {"limit": 1, "status": 1})
        return int(data.get("meta", {}).get("total", 0))
    except Exception as e:
        print(f"    ✗ Erro ao contar contatos totais: {e}")
        return 0


# ─── Coleta de dados ──────────────────────────────────────────────────────────

def collect_data(prefix_filter: str = "", days_back: int = 30) -> dict:
    print("\n📡 Coletando dados do ActiveCampaign...")

    resolve_tag_ids()

    # Campanhas
    raw_campaigns = get_campaigns(prefix_filter, days_back)

    # Nomes dos segmentos
    print("  Buscando nomes dos segmentos...")
    segment_ids = {int(c.get("segmentid", 0) or 0) for c in raw_campaigns if int(c.get("segmentid", 0) or 0) > 0}
    segment_names = {}
    for sid in segment_ids:
        try:
            resp = ac_get(f"segments/{sid}")
            segment_names[sid] = resp.get("segment", {}).get("name", "")
        except Exception:
            segment_names[sid] = ""
    print(f"    {len(segment_names)} segmento(s) encontrado(s)")

    campaigns = [enrich_campaign(c, segment_names) for c in raw_campaigns]

    # Listas
    print("  Contando contatos por lista...")
    lists_data = []
    for lst in MONITORED_LISTS:
        count = get_list_contact_count(lst["id"])
        lists_data.append({"name": lst["name"], "id": lst["id"], "count": count})
        print(f"    {lst['name']}: {count:,}")

    # Tags
    print("  Contando contatos por tag...")
    tags_data = []
    for tag in MONITORED_TAGS:
        if tag["id"]:
            count = get_tag_contact_count(tag["id"])
            tags_data.append({"name": tag["name"], "id": tag["id"], "count": count})
            print(f"    {tag['name']}: {count:,}")
        else:
            tags_data.append({"name": tag["name"], "id": None, "count": None})
            print(f"    {tag['name']}: não encontrada")

    # Limites da conta
    print("  Buscando limites e contadores da conta...")
    account_info   = get_account_info()
    total_contacts = get_total_contacts()
    monthly_sends  = get_monthly_sends()
    cl = account_info["contact_limit"]
    sl = account_info["sending_limit"]
    print(f"    Contatos: {total_contacts:,} / {cl:,}")
    print(f"    Envios no mês: {monthly_sends:,} / {sl:,}")

    total_alerts = sum(len(c["alerts"]) for c in campaigns)
    print(f"\n✅ Dados coletados! {len(campaigns)} campanhas, {total_alerts} alertas encontrados.")

    brt_now = datetime.now(timezone(timedelta(hours=-3)))
    return {
        "generated_at":   brt_now.strftime("%d/%m/%Y às %H:%M"),
        "prefix_filter":  prefix_filter or "Todas",
        "days_back":      days_back,
        "campaigns":      campaigns,
        "lists":          lists_data,
        "tags":           tags_data,
        "total_alerts":   total_alerts,
        "contact_limit":  account_info["contact_limit"],
        "sending_limit":  account_info["sending_limit"],
        "total_contacts": total_contacts,
        "monthly_sends":  monthly_sends,
    }


# ─── Geração do HTML ──────────────────────────────────────────────────────────

def generate_html(data: dict) -> str:
    campaigns_json = json.dumps(data["campaigns"], ensure_ascii=False)
    lists_json     = json.dumps(data["lists"],     ensure_ascii=False)
    tags_json      = json.dumps(data["tags"],       ensure_ascii=False)

    total_sends   = sum(c["sends"]   for c in data["campaigns"])
    total_opens   = sum(c["opens"]   for c in data["campaigns"])
    total_clicks  = sum(c["clicks"]  for c in data["campaigns"])
    total_bounces = sum(c["bounces"] for c in data["campaigns"])
    total_unsubs  = sum(c["unsubs"]  for c in data["campaigns"])
    avg_or            = round(total_opens   / total_sends * 100, 1) if total_sends else 0
    avg_cr            = round(total_clicks  / total_sends * 100, 1) if total_sends else 0
    bounce_rate_total = round(total_bounces / total_sends * 100, 2) if total_sends else 0
    unsub_rate_total  = round(total_unsubs  / total_sends * 100, 2) if total_sends else 0

    contact_limit  = data.get("contact_limit", 0)
    sending_limit  = data.get("sending_limit", 0)
    total_contacts = data.get("total_contacts", 0)
    monthly_sends  = data.get("monthly_sends", 0)
    contact_pct    = round(total_contacts / contact_limit * 100, 1) if contact_limit else 0
    sends_pct      = round(monthly_sends / sending_limit * 100, 1) if sending_limit else 0
    contact_card_class = "danger" if contact_pct > 90 else ("warning" if contact_pct > 80 else "")
    sends_card_class   = "danger" if sends_pct > 90 else ("warning" if sends_pct > 80 else "")

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>AC Dashboard · Hashtag</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
  :root {{
    --bg:       #0a0a0f;
    --surface:  #12121a;
    --border:   #1e1e2e;
    --accent:   #7c3aed;
    --accent2:  #06b6d4;
    --accent3:  #f59e0b;
    --danger:   #ef4444;
    --success:  #10b981;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --mono:     'JetBrains Mono', monospace;
    --sans:     'Inter', sans-serif;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    overflow-x: hidden;
  }}

  /* Grid noise background */
  body::before {{
    content: '';
    position: fixed; inset: 0; z-index: 0;
    background-image:
      linear-gradient(rgba(124,58,237,.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(124,58,237,.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
  }}

  .wrapper {{ position: relative; z-index: 1; max-width: 1400px; margin: 0 auto; padding: 32px 24px; }}

  /* ── Header ── */
  header {{
    display: flex; align-items: flex-start; justify-content: space-between;
    margin-bottom: 40px; flex-wrap: wrap; gap: 16px;
  }}
  .brand {{ display: flex; align-items: center; gap: 12px; }}
  .brand-dot {{
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 12px var(--accent);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{ 0%,100%{{ opacity:1 }} 50%{{ opacity:.4 }} }}
  .brand h1 {{ font-size: 22px; font-weight: 700; letter-spacing: -.3px; }}
  .brand span {{ color: var(--accent); }}
  .meta {{ text-align: right; }}
  .meta .label {{ font-family: var(--mono); font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 2px; }}
  .meta .value {{ font-family: var(--mono); font-size: 13px; color: var(--accent2); margin-top: 4px; }}

  /* filter bar removida */

  /* ── Summary cards ── */
  .summary-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }}
  .summary-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    position: relative;
    overflow: hidden;
    transition: border-color .2s, transform .2s;
  }}
  .summary-card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
  .summary-card::before {{
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
  }}
  .summary-card.danger::before {{ background: var(--danger); }}
  .summary-card.success::before {{ background: var(--success); }}
  .summary-card.warning::before {{ background: var(--accent3); }}
  .card-label {{ font-family: var(--mono); font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 8px; }}
  .card-value {{ font-size: 28px; font-weight: 800; line-height: 1; }}
  .card-sub {{ font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 6px; }}

  /* ── Section titles ── */
  .section-title {{
    font-size: 13px; font-weight: 600; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted);
    font-family: var(--mono);
    margin-bottom: 16px;
    display: flex; align-items: center; gap: 10px;
  }}
  .section-title::after {{
    content: ''; flex: 1; height: 1px; background: var(--border);
  }}

  /* ── Alerts banner ── */
  .alerts-banner {{
    background: rgba(239,68,68,.08);
    border: 1px solid rgba(239,68,68,.3);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 32px;
    display: none;
  }}
  .alerts-banner.visible {{ display: block; }}
  .alerts-banner h3 {{ color: var(--danger); font-size: 13px; font-weight: 700; margin-bottom: 10px; font-family: var(--mono); text-transform: uppercase; letter-spacing: 1px; }}
  .alert-item {{ font-size: 13px; color: #fca5a5; padding: 4px 0; border-bottom: 1px solid rgba(239,68,68,.1); }}
  .alert-item:last-child {{ border-bottom: none; }}
  .alert-item strong {{ color: #fff; }}

  /* ── Campaigns table ── */
  .table-wrap {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 32px;
  }}
  .table-search {{
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px;
  }}
  .table-search input {{
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: var(--mono);
    font-size: 12px;
    padding: 7px 14px;
    border-radius: 8px;
    outline: none;
    width: 260px;
  }}
  .table-search input:focus {{ border-color: var(--accent2); }}
  .table-search input[type="date"] {{ width: 148px; color-scheme: dark; }}
  .date-label {{ font-family: var(--mono); font-size: 11px; color: var(--muted); white-space: nowrap; }}
  .btn-clear {{
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); padding: 5px 10px; border-radius: 6px;
    cursor: pointer; font-family: var(--mono); font-size: 11px; transition: .15s;
  }}
  .btn-clear:hover {{ color: var(--danger); border-color: var(--danger); }}
  .btn-export {{
    background: rgba(16,185,129,.1); border: 1px solid rgba(16,185,129,.3);
    color: #6ee7b7; padding: 5px 12px; border-radius: 6px;
    cursor: pointer; font-family: var(--mono); font-size: 11px; transition: .15s;
  }}
  .btn-export:hover {{ background: rgba(16,185,129,.2); border-color: #6ee7b7; }}
  .btn-toggle-col {{
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); padding: 5px 10px; border-radius: 6px;
    cursor: pointer; font-family: var(--mono); font-size: 11px; transition: .15s;
  }}
  .btn-toggle-col:hover {{ color: var(--accent2); border-color: var(--accent2); }}
  .hide-segment .col-segment {{ display: none; }}
  .table-count {{ font-family: var(--mono); font-size: 11px; color: var(--muted); margin-left: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead tr {{ background: rgba(124,58,237,.08); }}
  th {{
    font-family: var(--mono);
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1.5px;
    color: var(--muted);
    padding: 12px 16px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }}
  th.num {{ text-align: right; }}
  td {{
    font-size: 13px;
    padding: 12px 16px;
    border-bottom: 1px solid rgba(30,30,46,.8);
    vertical-align: top;
  }}
  td.num {{ text-align: right; font-family: var(--mono); font-size: 12px; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(124,58,237,.04); }}
  tr.has-alert td {{ background: rgba(239,68,68,.04); }}
  tr.has-alert:hover td {{ background: rgba(239,68,68,.08); }}
  .campaign-name {{ font-weight: 600; font-size: 13px; max-width: 220px; }}
  .campaign-subject {{ font-size: 11px; color: var(--muted); margin-top: 3px; font-family: var(--mono); }}
  .badge {{
    display: inline-block;
    font-family: var(--mono); font-size: 10px;
    padding: 2px 8px; border-radius: 4px;
    font-weight: 700; text-transform: uppercase;
  }}
  .badge-ok     {{ background: rgba(16,185,129,.15); color: #6ee7b7; }}
  .badge-warn   {{ background: rgba(239,68,68,.15);  color: #fca5a5; }}
  .badge-zero   {{ background: rgba(245,158,11,.15); color: #fcd34d; }}
  .rate {{ font-family: var(--mono); font-size: 12px; }}
  .rate.high {{ color: var(--success); }}
  .rate.mid  {{ color: var(--accent2); }}
  .rate.low  {{ color: var(--muted); }}
  .rate.zero {{ color: var(--danger); }}
  .alert-cell {{ font-size: 11px; color: #fca5a5; }}

  /* ── Two-column grid ── */
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 32px; }}
  @media (max-width: 900px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

  /* ── Contact cards ── */
  .contact-panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }}
  .panel-header {{
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }}
  .panel-title {{ font-family: var(--mono); font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 2px; }}
  .panel-total {{ font-family: var(--mono); font-size: 12px; color: var(--accent2); }}
  .contact-row {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 11px 20px;
    border-bottom: 1px solid rgba(30,30,46,.6);
    transition: background .15s;
  }}
  .contact-row:last-child {{ border-bottom: none; }}
  .contact-row:hover {{ background: rgba(124,58,237,.05); }}
  .contact-row-name {{ font-size: 13px; }}
  .contact-row-count {{
    font-family: var(--mono); font-size: 13px; font-weight: 700;
    color: var(--accent);
  }}
  .contact-row-count.na {{ color: var(--muted); }}
  .bar-wrap {{ flex: 1; margin: 0 16px; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 2px; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .8s ease; }}

  /* ── Footer ── */
  footer {{
    text-align: center;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    padding: 32px 0 16px;
    border-top: 1px solid var(--border);
    margin-top: 40px;
  }}
</style>
</head>
<body>
<div class="wrapper">

  <!-- Header -->
  <header>
    <div class="brand">
      <div class="brand-dot"></div>
      <h1>Active<span>Campaign</span> · Dashboard</h1>
    </div>
    <div class="meta">
      <div class="label">Gerado em</div>
      <div class="value">{data["generated_at"]}</div>
    </div>
  </header>

  <!-- Info bar -->
  <div style="font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:24px;display:flex;gap:24px;align-items:center;">
    <span>Filtro: <strong style="color:var(--accent2)">{data['prefix_filter']}</strong></span>
    <span>Periodo: <strong style="color:var(--accent2)">{data['days_back']} dias</strong></span>
    <span style="margin-left:auto">Para atualizar: <code style="color:var(--text);background:var(--surface);padding:2px 8px;border-radius:4px">python ac_dashboard.py "[PREFIXO]" 14</code></span>
  </div>

  <!-- Summary -->
  <div class="summary-grid" id="summaryGrid">
    <div class="summary-card">
      <div class="card-label">Campanhas</div>
      <div class="card-value" id="sumCampaigns">{len(data['campaigns'])}</div>
      <div class="card-sub">no período</div>
    </div>
    <div class="summary-card">
      <div class="card-label">Total Enviados</div>
      <div class="card-value" id="sumSends">{total_sends:,}</div>
      <div class="card-sub">e-mails</div>
    </div>
    <div class="summary-card">
      <div class="card-label">Total Aberturas</div>
      <div class="card-value" id="sumOpens">{total_opens:,}</div>
      <div class="card-sub">únicas</div>
    </div>
    <div class="summary-card success">
      <div class="card-label">Open Rate Médio</div>
      <div class="card-value" id="sumOR">{avg_or}%</div>
      <div class="card-sub">do período</div>
    </div>
    <div class="summary-card">
      <div class="card-label">Click Rate Médio</div>
      <div class="card-value" id="sumCR">{avg_cr}%</div>
      <div class="card-sub">do período</div>
    </div>
    <div class="summary-card {'danger' if data['total_alerts'] > 0 else ''}">
      <div class="card-label">Alertas</div>
      <div class="card-value" id="sumAlerts" style="color:{'var(--danger)' if data['total_alerts'] > 0 else 'inherit'}">{data['total_alerts']}</div>
      <div class="card-sub">campanhas com problema</div>
    </div>
    <div class="summary-card {'danger' if bounce_rate_total > 2 else 'warning' if bounce_rate_total > 1 else ''}">
      <div class="card-label">Total Bounces</div>
      <div class="card-value" id="sumBounces">{total_bounces:,}</div>
      <div class="card-sub" id="sumBounceRate">{bounce_rate_total}% dos enviados</div>
    </div>
    <div class="summary-card {'danger' if unsub_rate_total > 0.5 else ''}">
      <div class="card-label">Total Unsubs</div>
      <div class="card-value" id="sumUnsubs">{total_unsubs:,}</div>
      <div class="card-sub" id="sumUnsubRate">{unsub_rate_total}% dos enviados</div>
    </div>
    <div class="summary-card {contact_card_class}">
      <div class="card-label">Contact Limit</div>
      <div class="card-value" style="font-size:20px">{total_contacts:,}{'<span style="font-size:13px;color:var(--muted);font-weight:400"> / ' + f"{contact_limit:,}" + '</span>' if contact_limit else ''}</div>
      <div class="card-sub">{'<span style="color:' + ('var(--danger)' if contact_pct > 90 else 'var(--accent3)' if contact_pct > 80 else 'var(--success)') + '">' + str(contact_pct) + '% utilizado</span>' if contact_limit else 'limite não disponível'}</div>
    </div>
    <div class="summary-card {sends_card_class}">
      <div class="card-label">Sending Limit</div>
      <div class="card-value" style="font-size:20px">{monthly_sends:,}{'<span style="font-size:13px;color:var(--muted);font-weight:400"> / ' + f"{sending_limit:,}" + '</span>' if sending_limit else ''}</div>
      <div class="card-sub">{'<span style="color:' + ('var(--danger)' if sends_pct > 90 else 'var(--accent3)' if sends_pct > 80 else 'var(--success)') + '">' + str(sends_pct) + '% do limite/mês</span>' if sending_limit else 'limite não disponível'}</div>
    </div>
  </div>

  <!-- Alerts banner -->
  <div class="alerts-banner {'visible' if data['total_alerts'] > 0 else ''}" id="alertsBanner">
    <div style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;" onclick="toggleAlerts()">
      <h3 style="margin:0">&#9888; Alertas detectados <span id="alertsCount" style="font-size:11px;opacity:.7"></span></h3>
      <button id="alertsToggleBtn" style="background:rgba(239,68,68,.2);border:1px solid rgba(239,68,68,.4);color:#fca5a5;font-size:11px;padding:4px 12px;border-radius:6px;cursor:pointer;font-family:var(--mono)">RECOLHER</button>
    </div>
    <div id="alertsList" style="margin-top:12px"></div>
  </div>

  <!-- Campaigns table -->
  <div class="section-title">📊 Campanhas</div>
  <div class="table-wrap">
    <div class="table-search">
      <input type="text" id="tableSearch" placeholder="Buscar campanha..." oninput="filterTable()">
      <span class="date-label" style="color:var(--accent);font-weight:600">+</span>
      <input type="text" id="tableSearch2" placeholder="E também..." oninput="filterTable()" style="width:180px">
      <span class="date-label">De</span>
      <input type="date" id="dateFrom" onchange="filterTable()">
      <span class="date-label">Até</span>
      <input type="date" id="dateTo" onchange="filterTable()">
      <button class="btn-clear" onclick="clearFilters()" title="Limpar todos os filtros">✕ limpar</button>
      <button class="btn-toggle-col" onclick="toggleSegment()" id="btnSegment" title="Mostrar/ocultar coluna Segmento">⊘ Segmento</button>
      <button class="btn-export" onclick="exportToExcel()" title="Exportar para Excel">⬇ Excel</button>
      <div class="table-count" id="tableCount"></div>
    </div>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Campanha</th>
          <th>Data Envio</th>
          <th class="num">Enviados</th>
          <th class="num">Aberturas</th>
          <th class="num">Cliques</th>
          <th class="num">Open Rate</th>
          <th class="num">Click Rate</th>
          <th class="num">Unsubs</th>
          <th class="num">Bounces</th>
          <th class="num">Bounce Rate</th>
          <th>Status</th>
          <th class="col-segment">Segmento</th>
        </tr>
      </thead>
      <tbody id="campaignBody"></tbody>
    </table>
    </div>
  </div>

  <!-- Contacts grid -->
  <div class="section-title">👥 Contatos</div>
  <div class="two-col">
    <div class="contact-panel">
      <div class="panel-header">
        <div class="panel-title">Por Lista</div>
        <div class="panel-total" id="totalLists"></div>
      </div>
      <div id="listRows"></div>
    </div>
    <div class="contact-panel">
      <div class="panel-header">
        <div class="panel-title">Por Tag</div>
        <div class="panel-total" id="totalTags"></div>
      </div>
      <div id="tagRows"></div>
    </div>
  </div>

  <footer>
    Hashtag Treinamentos · ActiveCampaign Dashboard · Gerado em {data["generated_at"]}
  </footer>
</div>

<script>
const CAMPAIGNS = {campaigns_json};
const LISTS     = {lists_json};
const TAGS      = {tags_json};

function fmt(n) {{
  if (n === null || n === undefined) return '—';
  return n.toLocaleString('pt-BR');
}}
function rateClass(r) {{
  if (r === 0) return 'zero';
  if (r < 10)  return 'low';
  if (r < 30)  return 'mid';
  return 'high';
}}

function renderCampaigns(list) {{
  const tbody = document.getElementById('campaignBody');
  const count = document.getElementById('tableCount');
  count.textContent = list.length + ' campanha(s)';

  tbody.innerHTML = list.map(c => {{
    const hasAlert = c.alerts && c.alerts.length > 0;
    const statusBadge = {{
      "Enviado":   '<span class="badge badge-ok">&#10003; Enviado</span>',
      "Agendado":  '<span class="badge" style="background:rgba(6,182,212,.15);color:#67e8f9">&#9200; Agendado</span>',
      "Enviando":  '<span class="badge" style="background:rgba(124,58,237,.2);color:#c4b5fd">&#8599; Enviando</span>',
      "Pausado":   '<span class="badge badge-zero">&#9208; Pausado</span>',
      "Rascunho":  '<span class="badge" style="background:rgba(100,116,139,.15);color:#94a3b8">&mdash; Rascunho</span>',
    }}[c.status] || '<span class="badge badge-zero">' + c.status + '</span>';
    const badge = hasAlert
      ? '<span class="badge badge-warn">⚠ Alerta</span> ' + statusBadge
      : statusBadge;

    return `<tr class="${{hasAlert ? 'has-alert' : ''}}">
      <td>
        <div class="campaign-name">${{c.name}}</div>
        <div class="campaign-subject">${{c.subject}}</div>
      </td>
      <td style="font-family:var(--mono);font-size:12px;white-space:nowrap">${{c.sdate}}</td>
      <td class="num">${{fmt(c.sends)}}</td>
      <td class="num">${{fmt(c.opens)}}</td>
      <td class="num">${{fmt(c.clicks)}}</td>
      <td class="num"><span class="rate ${{rateClass(c.open_rate)}}">${{c.open_rate}}%</span></td>
      <td class="num"><span class="rate ${{rateClass(c.click_rate)}}">${{c.click_rate}}%</span></td>
      <td class="num">${{fmt(c.unsubs)}}</td>
      <td class="num">${{fmt(c.bounces)}}</td>
      <td class="num"><span class="rate ${{c.bounce_rate > 1 ? 'zero' : c.bounce_rate > 0.5 ? 'low' : 'mid'}}">${{c.bounce_rate}}%</span></td>
      <td>${{badge}}${{hasAlert ? '<div class="alert-cell" style="margin-top:4px">' + c.alerts.join('<br>') + '</div>' : ''}}</td>
      <td class="col-segment" style="font-size:11px;color:var(--muted);max-width:200px">${{c.segmentname || '—'}}</td>
    </tr>`;
  }}).join('');
}}

function sdateToISO(sdate) {{
  // Converte "dd/mm/yyyy hh:mm" → "yyyy-mm-dd" para comparação
  if (!sdate || sdate === '—') return null;
  const d = sdate.split(' ')[0].split('/');
  return d.length === 3 ? `${{d[2]}}-${{d[1].padStart(2,'0')}}-${{d[0].padStart(2,'0')}}` : null;
}}

let currentFiltered = CAMPAIGNS;

function filterTable() {{
  const q        = document.getElementById('tableSearch').value.toLowerCase();
  const q2       = document.getElementById('tableSearch2').value.toLowerCase();
  const dateFrom = document.getElementById('dateFrom').value;
  const dateTo   = document.getElementById('dateTo').value;

  let filtered = CAMPAIGNS;

  if (q) {{
    filtered = filtered.filter(c =>
      c.name.toLowerCase().includes(q) || c.subject.toLowerCase().includes(q)
    );
  }}
  if (q2) {{
    filtered = filtered.filter(c =>
      c.name.toLowerCase().includes(q2) || c.subject.toLowerCase().includes(q2)
    );
  }}

  if (dateFrom || dateTo) {{
    filtered = filtered.filter(c => {{
      const d = sdateToISO(c.sdate);
      if (!d) return false;
      if (dateFrom && d < dateFrom) return false;
      if (dateTo   && d > dateTo)   return false;
      return true;
    }});
  }}

  currentFiltered = filtered;
  renderCampaigns(filtered);
  updateSummary(filtered);
  renderAlerts(filtered);
}}

function clearFilters() {{
  document.getElementById('tableSearch').value  = '';
  document.getElementById('tableSearch2').value = '';
  document.getElementById('dateFrom').value     = '';
  document.getElementById('dateTo').value       = '';
  filterTable();
}}

function updateSummary(list) {{
  const sent = list.filter(c => c.status === 'Enviado');
  const totalSends   = sent.reduce((s,c) => s + c.sends,   0);
  const totalOpens   = sent.reduce((s,c) => s + c.opens,   0);
  const totalClicks  = sent.reduce((s,c) => s + c.clicks,  0);
  const totalBounces = list.reduce((s,c) => s + c.bounces, 0);
  const totalUnsubs  = list.reduce((s,c) => s + c.unsubs,  0);
  const avgOR = totalSends ? (totalOpens   / totalSends * 100).toFixed(1) : 0;
  const avgCR = totalSends ? (totalClicks  / totalSends * 100).toFixed(1) : 0;
  const avgBR = totalSends ? (totalBounces / totalSends * 100).toFixed(2) : 0;
  const avgUR = totalSends ? (totalUnsubs  / totalSends * 100).toFixed(2) : 0;
  const totalAlerts = list.reduce((s,c) => s + (c.alerts ? c.alerts.length : 0), 0);

  document.getElementById('sumCampaigns').textContent  = list.length;
  document.getElementById('sumSends').textContent      = fmt(totalSends);
  document.getElementById('sumOpens').textContent      = fmt(totalOpens);
  document.getElementById('sumOR').textContent         = avgOR + '%';
  document.getElementById('sumCR').textContent         = avgCR + '%';
  document.getElementById('sumAlerts').textContent     = totalAlerts;
  document.getElementById('sumAlerts').style.color     = totalAlerts > 0 ? 'var(--danger)' : 'inherit';
  document.getElementById('sumBounces').textContent    = fmt(totalBounces);
  document.getElementById('sumBounceRate').textContent = avgBR + '% dos enviados';
  document.getElementById('sumUnsubs').textContent     = fmt(totalUnsubs);
  document.getElementById('sumUnsubRate').textContent  = avgUR + '% dos enviados';
}}

let alertsCollapsed = false;

function toggleAlerts() {{
  alertsCollapsed = !alertsCollapsed;
  document.getElementById('alertsList').style.display = alertsCollapsed ? 'none' : 'block';
  document.getElementById('alertsToggleBtn').textContent = alertsCollapsed ? 'EXPANDIR' : 'RECOLHER';
}}

function renderAlerts(list) {{
  list = list || CAMPAIGNS;
  const alerts = [];
  list.forEach(c => {{
    if (c.alerts && c.alerts.length > 0) {{
      c.alerts.forEach(a => {{
        alerts.push(`<div class="alert-item"><strong>${{c.name}}</strong> — ${{a}}</div>`);
      }});
    }}
  }});
  const banner = document.getElementById('alertsBanner');
  const listEl = document.getElementById('alertsList');
  const countEl = document.getElementById('alertsCount');
  if (alerts.length > 0) {{
    banner.classList.add('visible');
    listEl.innerHTML = alerts.join('');
    if (countEl) countEl.textContent = '(' + alerts.length + ')';
  }} else {{
    banner.classList.remove('visible');
  }}
}}

function renderContacts() {{
  // Lists
  const maxList = Math.max(...LISTS.map(l => l.count || 0));
  const totalL  = LISTS.reduce((s, l) => s + (l.count || 0), 0);
  document.getElementById('totalLists').textContent = fmt(totalL) + ' total';
  document.getElementById('listRows').innerHTML = LISTS.map(l => `
    <div class="contact-row">
      <div class="contact-row-name">${{l.name}}</div>
      <div class="bar-wrap"><div class="bar-fill" style="width:${{maxList ? Math.round(l.count/maxList*100) : 0}}%"></div></div>
      <div class="contact-row-count">${{fmt(l.count)}}</div>
    </div>
  `).join('');

  // Tags
  const validTags = TAGS.filter(t => t.count !== null);
  const maxTag  = validTags.length ? Math.max(...validTags.map(t => t.count)) : 0;
  const totalT  = validTags.reduce((s, t) => s + t.count, 0);
  document.getElementById('totalTags').textContent = fmt(totalT) + ' total';
  document.getElementById('tagRows').innerHTML = TAGS.map(t => `
    <div class="contact-row">
      <div class="contact-row-name" style="font-family:var(--mono);font-size:12px">${{t.name}}</div>
      <div class="bar-wrap"><div class="bar-fill" style="width:${{maxTag && t.count ? Math.round(t.count/maxTag*100) : 0}}%"></div></div>
      <div class="contact-row-count ${{t.count === null ? 'na' : ''}}">${{t.count !== null ? fmt(t.count) : 'N/A'}}</div>
    </div>
  `).join('');
}}

function reloadDashboard() {{
  const prefix = document.getElementById('prefixInput').value.trim();
  const days   = document.getElementById('daysSelect').value;
  // Instrução: no ambiente com Python, passe esses parâmetros via argumento
  alert('Para atualizar os dados, rode:\\n\\npython ac_dashboard.py "' + prefix + '" ' + days + '\\n\\nO dashboard será regenerado e aberto automaticamente.');
}}

function toggleSegment() {{
  const wrap = document.querySelector('.table-wrap');
  const btn  = document.getElementById('btnSegment');
  const hidden = wrap.classList.toggle('hide-segment');
  btn.textContent = hidden ? '⊕ Segmento' : '⊘ Segmento';
  btn.style.color = hidden ? 'var(--muted)' : '';
}}

function exportToExcel() {{
  const rows = currentFiltered.map(c => ({{
    'Campanha':      c.name,
    'Assunto':       c.subject,
    'Segmento':      c.segmentname || '',
    'Data Envio':    c.sdate,
    'Status':        c.status,
    'Enviados':      c.sends,
    'Aberturas':     c.opens,
    'Cliques':       c.clicks,
    'Open Rate %':   c.open_rate,
    'Click Rate %':  c.click_rate,
    'Unsubs':        c.unsubs,
    'Bounces':       c.bounces,
    'Bounce Rate %': c.bounce_rate,
    'Alertas':       (c.alerts || []).join(' | '),
  }}));
  const ws = XLSX.utils.json_to_sheet(rows);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Campanhas');
  XLSX.writeFile(wb, 'ac_campanhas.xlsx');
}}

// Init
renderCampaigns(CAMPAIGNS);
renderAlerts(CAMPAIGNS);
renderContacts();
</script>
</body>
</html>"""


# ─── GitHub Pages ─────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_USER  = os.getenv("GITHUB_USER", "")
GITHUB_REPO  = os.getenv("GITHUB_REPO", "")

def deploy_to_github_pages(file_path: Path) -> str:
    """Faz deploy do HTML no GitHub Pages e retorna a URL pública."""
    try:
        if not all([GITHUB_TOKEN, GITHUB_USER, GITHUB_REPO]):
            print("  AVISO: Credenciais do GitHub não configuradas no .env")
            return ""

        gh_headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/index.html"

        content_b64 = base64.b64encode(file_path.read_bytes()).decode()

        # Busca SHA do arquivo existente (necessário para atualizar)
        sha = None
        resp = requests.get(api_url, headers=gh_headers, timeout=15)
        if resp.status_code == 200:
            sha = resp.json().get("sha")

        payload = {
            "message": f"Dashboard atualizado em {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "content": content_b64,
        }
        if sha:
            payload["sha"] = sha

        action = "Atualizando" if sha else "Criando"
        print(f"  {action} index.html no GitHub Pages...")
        resp = requests.put(api_url, headers=gh_headers, json=payload, timeout=60)
        resp.raise_for_status()

        url = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/"
        print(f"  Dashboard publicado: {url}")
        return url

    except Exception as e:
        print(f"  ERRO no GitHub Pages: {e}")
        return ""


# ─── Slack ─────────────────────────────────────────────────────────────────────

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")

def send_slack(data: dict, site_url: str):
    """Envia resumo no Slack com link para o dashboard."""
    try:
        total_alerts = data["total_alerts"]
        campaigns    = data["campaigns"]
        sent = [c for c in campaigns if c["status"] in ("Enviado", "Agendado") and c["sends"] > 0]

        total_sends   = sum(c["sends"]   for c in sent)
        total_opens   = sum(c["opens"]   for c in sent)
        total_clicks  = sum(c["clicks"]  for c in sent)
        total_bounces = sum(c["bounces"] for c in sent)
        avg_or  = round(total_opens  / total_sends * 100, 1) if total_sends else 0
        avg_cr  = round(total_clicks / total_sends * 100, 1) if total_sends else 0
        tot_br  = round(total_bounces / total_sends * 100, 2) if total_sends else 0

        # Contexto do horário de execução (BRT)
        hour_brt = datetime.now(timezone(timedelta(hours=-3))).hour
        run_label = "Relatório Matinal :sunrise:" if hour_brt < 12 else "Relatório Vespertino :cityscape:"

        # Status geral
        if total_alerts == 0:
            status_emoji = ":white_check_mark:"
            status_text  = "Tudo certo"
        elif total_alerts <= 3:
            status_emoji = ":warning:"
            status_text  = f"{total_alerts} alerta(s) detectado(s)"
        else:
            status_emoji = ":rotating_light:"
            status_text  = f"{total_alerts} alertas — verificar com prioridade"

        # Campanha destaque (melhor open rate com pelo menos 500 envios)
        candidates = [c for c in sent if c["sends"] >= 500 and c["open_rate"] > 0]
        top = max(candidates, key=lambda c: c["open_rate"], default=None)
        top_block = ""
        if top:
            top_block = f":trophy: *Destaque:* {top['name']} — {top['open_rate']}% open rate ({top['sends']:,} envios)"

        # Alerta de bounce total
        bounce_warning = ""
        if tot_br > 1:
            bounce_warning = f":red_circle: *Bounce total: {tot_br}%* — acima de 1%, verifique as listas\n"

        # Lista de alertas (máx 8)
        alert_lines = []
        for c in campaigns:
            for a in (c.get("alerts") or []):
                alert_lines.append(f"  • *{c['name']}*: {a}")
        alert_block = "\n".join(alert_lines[:8])
        if len(alert_lines) > 8:
            alert_block += f"\n  _...e mais {len(alert_lines) - 8} alertas no dashboard_"

        link_text = f"<{site_url}|:bar_chart: Abrir Dashboard Completo>" if site_url else ":bar_chart: Dashboard (link indisponível)"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"AC Dashboard · {run_label.replace(':sunrise:', '').replace(':cityscape:', '').strip()} · {data['generated_at']}"}
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Campanhas*\n{len(campaigns)}"},
                    {"type": "mrkdwn", "text": f"*Enviados*\n{total_sends:,}"},
                    {"type": "mrkdwn", "text": f"*Open Rate médio*\n{avg_or}%"},
                    {"type": "mrkdwn", "text": f"*Click Rate médio*\n{avg_cr}%"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{status_emoji} *{status_text}*{chr(10) + bounce_warning if bounce_warning else ''}"}
            },
        ]

        if top_block:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": top_block}
            })

        if alert_lines:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Alertas:*\n{alert_block}"}
            })

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": link_text}
        })

        resp = requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        if resp.status_code == 200:
            print("  Mensagem enviada no Slack!")
        else:
            print(f"  ERRO Slack: {resp.status_code} — {resp.text}")

    except Exception as e:
        print(f"  ERRO ao enviar Slack: {e}")


def send_slack_error(traceback_str: str):
    """Envia alerta de erro no Slack."""
    try:
        if not SLACK_WEBHOOK:
            return
        now = datetime.now().strftime("%d/%m/%Y %H:%M")
        tb_preview = traceback_str[-1500:] if len(traceback_str) > 1500 else traceback_str
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "❌ Erro no AC Dashboard"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":rotating_light: *Falha na execução* — {now}\n\nO dashboard não foi gerado. Verifique o log em `logs/`."}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{tb_preview}```"}
            },
        ]
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
    except Exception:
        pass


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # ── Log file ──────────────────────────────────────────────────────────────
    log_path = Path("logs") / f"dashboard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    tee = _TeeLogger(log_path)
    sys.stdout = tee

    try:
        _run()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(f"\nERRO FATAL:\n{tb}")
        send_slack_error(tb)
        sys.exit(1)
    finally:
        sys.stdout = tee.terminal
        tee.close()


def _run():
    if not AC_API_KEY:
        print("AC_API_KEY nao encontrada no .env")
        sys.exit(1)

    # Separa flags dos argumentos posicionais
    raw_args      = sys.argv[1:]
    flags         = [a for a in raw_args if a.startswith("--")]
    positional    = [a for a in raw_args if not a.startswith("--")]

    # Convencao: use "-" para sem filtro, ou so passe o numero de dias
    # Exemplos:
    #   python ac_dashboard.py 14            → sem filtro, 14 dias
    #   python ac_dashboard.py [LPBI20] 14   → com filtro, 14 dias
    #   python ac_dashboard.py - 14          → sem filtro, 14 dias
    raw_prefix = positional[0] if len(positional) > 0 else ""
    raw_days   = positional[1] if len(positional) > 1 else ""

    # Se o primeiro argumento for so numerico, e na verdade o numero de dias
    if raw_prefix.lstrip("-").isdigit():
        days_back     = int(raw_prefix)
        prefix_filter = ""
    else:
        prefix_filter = "" if raw_prefix in ("-", "--", "") else raw_prefix
        days_back     = int(raw_days) if raw_days.isdigit() else 30

    open_browser   = "--browser"   in flags
    send_to_slack  = "--no-slack"  not in flags
    deploy_github  = "--no-github" not in flags

    print(f"Parametros: prefixo='{prefix_filter}' dias={days_back}")

    data = collect_data(prefix_filter, days_back)
    html = generate_html(data)

    # Salva localmente com nome fixo (sobrescreve sempre)
    out_path = Path("ac_dashboard.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"\nDashboard salvo: {out_path.resolve()}")

    # Deploy no GitHub Pages
    site_url = ""
    if deploy_github:
        print("\nPublicando no GitHub Pages...")
        site_url = deploy_to_github_pages(out_path)

    # Envia no Slack
    if send_to_slack:
        print("\nEnviando no Slack...")
        send_slack(data, site_url)

    # Abre no browser local
    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())
        print("Abrindo no browser...")

    print("\nConcluido!")


if __name__ == "__main__":
    main()
