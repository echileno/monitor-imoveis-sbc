#!/usr/bin/env python3
"""Monitor de aluguel em condomínios de São Bernardo do Campo.

Fluxo de cada execução:
  1. Para cada site e cada condomínio, baixa a página de busca.
  2. Extrai anúncios (preço + link) de blocos JSON embutidos na página
     (__NEXT_DATA__, JSON-LD) e, se não achar nada, do HTML dos cards.
  3. Mantém só anúncios cujo texto cita o condomínio e com aluguel <= max_rent.
  4. Compara com state.json: avisa no Telegram se for novo ou se o preço caiu.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRICE_KEYS = ("price", "rentalprice", "rent", "preco", "valor", "lowprice", "amount")
URL_KEYS = ("url", "href", "link", "canonicalurl", "@id")
PRICE_RE = re.compile(r"R\$\s*([\d]{1,3}(?:\.\d{3})*|\d+)(?:,\d{2})?")


# ---------------------------------------------------------------- utilidades
def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def canonical(url: str) -> str:
    """URL sem querystring/fragmento, para identificar o mesmo anúncio sempre."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def to_price(value):
    """Converte 3900, '3900', 'R$ 3.900,00' em float. Retorna None se não der."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        m = PRICE_RE.search(value)
        if m:
            return float(m.group(1).replace(".", ""))
        digits = value.strip().replace(".", "").replace(",", ".")
        try:
            v = float(digits)
            return v if v > 0 else None
        except ValueError:
            return None
    return None


def fmt_brl(v: float) -> str:
    return "R$ {:,.0f}".format(v).replace(",", ".")


# ---------------------------------------------------------------- extração
def walk(obj):
    """Percorre qualquer JSON devolvendo todos os dicionários."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def json_blobs(soup):
    blobs = []
    for tag in soup.find_all("script"):
        is_json = tag.get("type") == "application/ld+json" or tag.get("id") == "__NEXT_DATA__"
        if not is_json:
            continue
        raw = tag.string or tag.get_text()
        try:
            blobs.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return blobs


def find_price(d: dict):
    for k, v in d.items():
        if k.lower() in PRICE_KEYS:
            p = to_price(v)
            if p:
                return p
    offers = d.get("offers")
    if isinstance(offers, dict):
        return find_price(offers)
    pricing = d.get("pricingInfos")  # formato usado por ZAP/Viva Real
    if isinstance(pricing, list):
        for item in pricing:
            if isinstance(item, dict) and str(item.get("businessType", "")).upper() in ("RENTAL", ""):
                p = to_price(item.get("price"))
                if p:
                    return p
    return None


def find_url(d: dict, base: str):
    for k, v in d.items():
        if k.lower() in URL_KEYS and isinstance(v, str) and ("/" in v):
            return urljoin(base, v)
    return None


def from_json(soup, base):
    found = {}
    for blob in json_blobs(soup):
        for d in walk(blob):
            price = find_price(d)
            url = find_url(d, base)
            if not (price and url):
                continue
            text = json.dumps(d, ensure_ascii=False)[:4000]
            found[canonical(url)] = {"url": url, "price": price, "text": text}
    return list(found.values())


def from_html(soup, base):
    """Plano B: sobe a partir de cada link até achar um bloco que tenha 'R$'."""
    found = {}
    for a in soup.find_all("a", href=True):
        node = a
        for _ in range(5):
            if node is None:
                break
            txt = node.get_text(" ", strip=True)
            m = PRICE_RE.search(txt)
            if m and len(txt) < 1500:
                price = float(m.group(1).replace(".", ""))
                url = urljoin(base, a["href"])
                found.setdefault(canonical(url), {"url": url, "price": price, "text": txt})
                break
            node = node.parent
    return list(found.values())


def extract(html_text, base):
    soup = BeautifulSoup(html_text, "html.parser")
    items = from_json(soup, base)
    if not items:
        items = from_html(soup, base)
    return items


# ---------------------------------------------------------------- busca
def fetch(url, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(last)


def condo_regexes(condos):
    out = []
    for c in condos:
        pattern = r"\b(" + "|".join(re.escape(norm(a)) for a in c["aliases"]) + r")\b"
        out.append((c["name"], re.compile(pattern)))
    return out


def run_search(cfg, debug=False):
    regexes = condo_regexes(cfg["condos"])
    max_rent = cfg["max_rent"]
    matches = {}   # url canônica -> dict
    health = {}    # fonte -> quantos anúncios brutos foram lidos

    for src in cfg["sources"]:
        total_raw = 0
        failed = None
        for condo in cfg["condos"]:
            url = src["url"].format(q=quote(condo["name"].lower()))
            try:
                page = fetch(url)
            except RuntimeError as e:
                failed = str(e)
                print(f"[{src['name']}] {condo['name']}: falhou ({e})")
                continue
            if debug:
                DEBUG_DIR.mkdir(exist_ok=True)
                fname = f"{norm(src['name']).replace(' ', '_')}_{condo['name'].lower()}.html"
                (DEBUG_DIR / fname).write_text(page, encoding="utf-8")
            items = extract(page, url)
            total_raw += len(items)
            hits = 0
            for it in items:
                text = norm(it["text"])
                for cname, rx in regexes:
                    if rx.search(text) and it["price"] <= max_rent:
                        key = canonical(it["url"])
                        matches[key] = {
                            "url": it["url"], "price": it["price"],
                            "condo": cname, "source": src["name"],
                        }
                        hits += 1
                        break
            print(f"[{src['name']}] {condo['name']}: {len(items)} lidos, {hits} servem")
            time.sleep(1.5)
        health[src["name"]] = {"raw": total_raw, "error": failed}
    return matches, health


# ---------------------------------------------------------------- estado e alertas
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "empty_runs": {}, "warned": {}}


def telegram(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Erro Telegram:", r.status_code, r.text[:200])
    return r.status_code == 200


def listing_message(kind, m, old_price=None):
    head = "🏠 <b>Novo anúncio</b>" if kind == "new" else "📉 <b>Preço baixou</b>"
    price = fmt_brl(m["price"])
    if old_price:
        price = f"{fmt_brl(old_price)} → <b>{fmt_brl(m['price'])}</b>"
    return (
        f"{head} — {html.escape(m['condo'])}\n"
        f"Aluguel: {price}\n"
        f"Fonte: {html.escape(m['source'])}\n"
        f"{html.escape(m['url'])}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="não envia Telegram nem grava estado")
    ap.add_argument("--debug", action="store_true", help="salva o HTML baixado em debug/")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    can_send = bool(token and chat_id) and not args.dry_run

    state = load_state()
    first_run = not state["seen"]
    matches, health = run_search(cfg, debug=args.debug)

    alerts = []
    for key, m in matches.items():
        old = state["seen"].get(key)
        if old is None:
            alerts.append(("new", m, None))
        elif m["price"] < old["price"]:
            alerts.append(("drop", m, old["price"]))
        state["seen"][key] = {
            "price": m["price"], "condo": m["condo"], "source": m["source"],
            "first_seen": (old or {}).get("first_seen", time.strftime("%Y-%m-%d %H:%M")),
        }

    # avisos de saúde das fontes
    health_msgs = []
    for name, h in health.items():
        if h["raw"] == 0:
            state["empty_runs"][name] = state["empty_runs"].get(name, 0) + 1
            if (state["empty_runs"][name] >= cfg["warn_after_empty_runs"]
                    and not state["warned"].get(name)):
                health_msgs.append(
                    f"⚠️ <b>{html.escape(name)}</b> não retornou nenhum anúncio há "
                    f"{state['empty_runs'][name]} execuções seguidas "
                    f"(bloqueio ou mudança no site). Veja o README, seção 'Se uma fonte parar'."
                )
                state["warned"][name] = True
        else:
            state["empty_runs"][name] = 0
            state["warned"][name] = False

    print(f"\n{len(matches)} anúncios dentro do critério, {len(alerts)} para avisar.")

    if can_send:
        limit = cfg["max_individual_alerts"]
        for kind, m, old in alerts[:limit]:
            telegram(token, chat_id, listing_message(kind, m, old))
            time.sleep(0.5)
        if len(alerts) > limit:
            rest = alerts[limit:]
            lines = [f"• {a[1]['condo']} {fmt_brl(a[1]['price'])} ({a[1]['source']})\n  {a[1]['url']}"
                     for a in rest]
            telegram(token, chat_id,
                     f"➕ Mais {len(rest)} anúncios:\n" + html.escape("\n".join(lines))[:3500])
        for msg in health_msgs:
            telegram(token, chat_id, msg)
        if first_run:
            telegram(token, chat_id,
                     f"✅ Monitor ligado. Primeira varredura achou {len(matches)} anúncios "
                     f"até {fmt_brl(cfg['max_rent'])}. Daqui pra frente aviso só novidades.")
    else:
        for kind, m, old in alerts:
            print("ALERTA (não enviado):", kind, m["condo"], fmt_brl(m["price"]), m["url"])
        for msg in health_msgs:
            print("SAÚDE (não enviado):", msg)

    if not args.dry_run:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
