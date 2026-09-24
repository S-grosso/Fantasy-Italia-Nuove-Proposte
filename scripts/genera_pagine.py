#!/usr/bin/env python3
"""
Pagine libro — Fantasy Italia, Nuove Proposte
=============================================

Legge da Supabase le schede approvate e scrive una pagina statica per ogni
libro in libri/<slug>/index.html, piu' l'indice libri/index.html, la
sitemap.xml e il robots.txt.

Le pagine servono a due lettori che il catalogo dinamico non raggiunge:
i motori di ricerca, che indicizzano male il contenuto caricato via
JavaScript, e le anteprime dei link (WhatsApp, Telegram, Facebook), che
leggono solo i meta tag dell'HTML iniziale. Chi naviga il sito continua a
vedere la scheda in rilievo sopra l'elenco: l'indirizzo e' lo stesso.

Uso:
    python scripts/genera_pagine.py
    python scripts/genera_pagine.py --dry-run     # non scrive, riepiloga
    python scripts/genera_pagine.py --da-file x.json   # prova offline
"""

import argparse
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DIR_LIBRI = ROOT / "libri"
DIR_ARTICOLI = ROOT / "articoli"
SITEMAP = ROOT / "sitemap.xml"
ROBOTS = ROOT / "robots.txt"

SITO = "https://www.fantasyitalianuoveproposte.it"
NOME_SITO = "Fantasy Italia — Nuove proposte"

# La chiave pubblica e' la stessa che usa il sito: legge solo le schede
# approvate, come chiunque apra il catalogo. Se nelle Actions sono presenti
# i secrets dello Scout si usano quelli.
SUPABASE_URL = os.environ.get("SUPABASE_URL") or "https://nncnhlbaqnfqtjwembii.supabase.co"
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or "sb_publishable_0prYsjRCftybZ7uQ7tTryA_D5N2r9kQ"
)

CAMPI = (
    "slug,title,author,publisher,year,series,isbn,cover_url,description,"
    "store_amazon_url,store_publisher_url,is_debut,genre,labels,updated_at"
)

CAMPI_NEWS = "slug,title,body_html,published_at,updated_at"

TIMEOUT = 30

# Lo slug arriva dal database, ma finisce in un percorso su disco: prima di
# creare cartelle si controlla che contenga solo quello che deve contenere.
SLUG_VALIDO = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# --------------------------------------------------------------------------
# Utilita'
# --------------------------------------------------------------------------

def e(testo):
    """Testo pronto per finire dentro l'HTML."""
    return html.escape(str(testo or ""), quote=True)


def riassunto(testo, limite=160):
    """Prima parte della sinossi, su una riga, per description e og:description."""
    pulito = re.sub(r"\s+", " ", str(testo or "")).strip()
    if len(pulito) <= limite:
        return pulito
    taglio = pulito[:limite].rsplit(" ", 1)[0]
    return taglio + "…"


def testo_semplice(html_grezzo):
    """Testo senza tag, per description e anteprima dell'indice."""
    senza_tag = re.sub(r"<[^>]+>", " ", str(html_grezzo or ""))
    return re.sub(r"\s+", " ", html.unescape(senza_tag)).strip()


def corpo_sicuro(html_grezzo):
    """
    Toglie dal corpo dell'articolo i tag che non devono finire in una pagina
    statica. Non e' un sanificatore: il testo e' gia' ripulito quando lo salvi
    da news-admin, e viene da te, non da estranei. Serve come rete, perche' un
    <script> arrivato per altre strade (per esempio con l'importazione del
    vecchio news.json) qui dentro sarebbe eseguibile.
    """
    pulito = re.sub(
        r"<\s*(script|iframe|object|embed|style|link|meta)\b[^>]*>.*?<\s*/\s*\1\s*>",
        "", str(html_grezzo or ""), flags=re.I | re.S,
    )
    pulito = re.sub(
        r"<\s*(script|iframe|object|embed|style|link|meta)\b[^>]*/?>",
        "", pulito, flags=re.I,
    )
    # Gestori scritti nell'attributo: onclick, onerror e simili.
    pulito = re.sub(r'\son\w+\s*=\s*"[^"]*"', "", pulito, flags=re.I)
    pulito = re.sub(r"\son\w+\s*=\s*'[^']*'", "", pulito, flags=re.I)
    return pulito


def data_leggibile(valore):
    """AAAA-MM-GG -> '14 settembre 2026': per il lettore, non per la macchina."""
    mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    iso = data_iso(valore)
    try:
        anno, mese, giorno = iso.split("-")
        return f"{int(giorno)} {mesi[int(mese) - 1]} {anno}"
    except (ValueError, IndexError):
        return iso


def data_iso(valore):
    """updated_at di Supabase -> AAAA-MM-GG per la sitemap."""
    testo = str(valore or "")[:10]
    return testo if re.match(r"^\d{4}-\d{2}-\d{2}$", testo) else datetime.now(timezone.utc).strftime("%Y-%m-%d")


def url_libro(slug):
    return SITO + "/libri/" + slug + "/"


def url_articolo(slug):
    return SITO + "/articoli/" + slug + "/"


# --------------------------------------------------------------------------
# Lettura dei dati
# --------------------------------------------------------------------------

def scarica_libri():
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/books"
    parametri = {
        "select": CAMPI,
        "status": "eq.approved",
        "slug": "not.is.null",
        "order": "year.desc,title.asc",
    }
    testate = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY}
    r = requests.get(url, params=parametri, headers=testate, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def scarica_articoli():
    """
    Articoli pubblicati che hanno uno slug. Le bozze non ce l'hanno, quindi
    non possono finire online per sbaglio: a decidere e' il trigger sul
    database, non questo script.
    """
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/news"
    parametri = {
        "select": CAMPI_NEWS,
        "status": "eq.published",
        "slug": "not.is.null",
        "order": "published_at.desc",
    }
    testate = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY}
    r = requests.get(url, params=parametri, headers=testate, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Pagina del singolo libro
# --------------------------------------------------------------------------

STILE = """
  :root{ --bg:#0f1013; --panel:#171923; --ink:#e7e9ee; --muted:#a9adbb; --accent:#7aa6ff; --border:#232635; }
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,'Helvetica Neue',Arial;
       background:linear-gradient(180deg,#0f1013,#0d0e12 140vh);color:var(--ink);line-height:1.5}
  header{position:sticky;top:0;z-index:5;background:rgba(13,14,18,.75);backdrop-filter:blur(6px);border-bottom:1px solid var(--border)}
  header .wrap{max-width:1100px;margin:0 auto;padding:12px 16px;display:flex;gap:12px;align-items:center}
  header a.home{color:var(--ink);text-decoration:none;font-weight:600}
  main{max-width:820px;margin:0 auto;padding:20px 16px 40px}
  article{background:#10121a;border:1px solid var(--border);border-radius:14px;padding:16px;
          box-shadow:0 10px 30px rgba(0,0,0,.25)}
  .head{display:flex;gap:16px;align-items:flex-start}
  img.cover{display:block;height:230px;width:auto;border-radius:8px;border:1px solid var(--border);background:#080a0f;object-fit:cover}
  h1{font-size:22px;line-height:1.25;margin:0 0 6px 0}
  .by{color:var(--muted);font-size:14px}
  .tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
  .tag{font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:999px;color:var(--muted)}
  .tag.genre{color:#c9a0ff;border-color:#5a3d8a;text-transform:lowercase}
  .tag.label{color:#9fc8ff;border-color:#2854aa}
  .dati{margin-top:12px;font-size:13px;color:var(--muted);display:grid;gap:3px}
  .sinossi{margin-top:18px;white-space:pre-wrap;line-height:1.65}
  .azioni{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
  .btn{background:#151826;border:1px solid var(--border);color:var(--ink);padding:9px 12px;border-radius:10px;
       text-decoration:none;display:inline-flex;align-items:center;gap:8px;font-size:14px}
  footer{color:var(--muted);font-size:12px;padding:16px;text-align:center}
  footer a{color:#9fc8ff;text-decoration:none}
  @media (max-width:640px){
    .head{flex-direction:column}
    img.cover{height:240px;align-self:center}
  }
"""


# Il corpo dell'articolo e' HTML scritto con l'editor delle notizie: qui si
# danno le misure a quello che puo' contenere, senza toccare il resto.
STILE_ARTICOLO = """
  article.articolo{max-width:720px}
  .articolo h1{margin:0 0 4px 0}
  .articolo .data{color:var(--muted);font-size:13px;margin-bottom:20px}
  .corpo{line-height:1.75;font-size:16.5px}
  .corpo h2{font-size:20px;margin:28px 0 10px}
  .corpo h3{font-size:17px;margin:22px 0 8px}
  .corpo p{margin:0 0 16px}
  .corpo ul,.corpo ol{margin:0 0 16px;padding-left:22px}
  .corpo li{margin-bottom:6px}
  .corpo a{color:#9fc8ff}
  .corpo img{max-width:100%;height:auto;border-radius:8px}
  .corpo blockquote{margin:0 0 16px;padding-left:14px;border-left:2px solid var(--border);color:var(--muted)}
"""


def schema_libro(b):
    """Dati strutturati: e' quello che Google legge per la scheda in SERP."""
    dati = {
        "@context": "https://schema.org",
        "@type": "Book",
        "name": b.get("title") or "",
        "url": url_libro(b["slug"]),
        "inLanguage": "it",
    }
    if b.get("author"):
        dati["author"] = {"@type": "Person", "name": b["author"]}
    if b.get("publisher"):
        dati["publisher"] = {"@type": "Organization", "name": b["publisher"]}
    if b.get("year"):
        dati["datePublished"] = str(b["year"])
    if b.get("isbn"):
        dati["isbn"] = b["isbn"]
    if b.get("cover_url"):
        dati["image"] = b["cover_url"]
    if b.get("description"):
        dati["description"] = riassunto(b["description"], 600)
    if b.get("genre"):
        dati["genre"] = b["genre"]
    if b.get("series"):
        dati["isPartOf"] = {"@type": "BookSeries", "name": b["series"]}
    # I < diventano escape: una sinossi che contenesse </script> chiuderebbe
    # il blocco e spezzerebbe la pagina.
    return json.dumps(dati, ensure_ascii=False, indent=2).replace("<", "\\u003C")


def pagina_libro(b):
    slug = b["slug"]
    titolo = b.get("title") or "(Senza titolo)"
    autore = b.get("author") or ""
    editore = b.get("publisher") or ""
    anno = b.get("year") or ""
    sinossi = (b.get("description") or "").strip()
    descrizione = riassunto(sinossi) or f"{titolo}, {autore}: scheda nel catalogo della narrativa fantasy italiana."
    intestazione = " • ".join([p for p in [autore, editore, str(anno) if anno else ""] if p])

    titolo_pagina = f"{titolo}" + (f" — {autore}" if autore else "") + f" | {NOME_SITO}"

    meta_immagine = ""
    if b.get("cover_url"):
        meta_immagine = (
            f'<meta property="og:image" content="{e(b["cover_url"])}">\n'
            f'<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:image" content="{e(b["cover_url"])}">'
        )
    else:
        meta_immagine = '<meta name="twitter:card" content="summary">'

    copertina = ""
    if b.get("cover_url"):
        copertina = f'<img class="cover" src="{e(b["cover_url"])}" alt="Copertina di {e(titolo)}" loading="eager">'

    etichette = []
    if b.get("genre"):
        etichette.append(f'<span class="tag genre">{e(b["genre"])}</span>')
    esordio = "Esordio: sì" if b.get("is_debut") is True else ("Esordio: no" if b.get("is_debut") is False else "Esordio: ?")
    etichette.append(f'<span class="tag">{e(esordio)}</span>')
    for l in (b.get("labels") or []):
        etichette.append(f'<span class="tag label">{e(l)}</span>')

    dati = []
    if b.get("series"):
        dati.append(f'<div>Serie: {e(b["series"])}</div>')
    if b.get("isbn"):
        dati.append(f'<div>ISBN: {e(b["isbn"])}</div>')

    azioni = []
    if b.get("store_amazon_url"):
        azioni.append(f'<a class="btn" href="{e(b["store_amazon_url"])}" target="_blank" rel="noopener nofollow">Vedi su Amazon</a>')
    if b.get("store_publisher_url"):
        azioni.append(f'<a class="btn" href="{e(b["store_publisher_url"])}" target="_blank" rel="noopener nofollow">Sito dell\'editore</a>')
    azioni.append('<a class="btn" href="/">Torna al catalogo</a>')

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titolo_pagina)}</title>
<meta name="description" content="{e(descrizione)}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta name="theme-color" content="#7aa6ff">
<link rel="canonical" href="{e(url_libro(slug))}">
<meta property="og:type" content="book">
<meta property="og:site_name" content="{e(NOME_SITO)}">
<meta property="og:locale" content="it_IT">
<meta property="og:title" content="{e(titolo + (' — ' + autore if autore else ''))}">
<meta property="og:description" content="{e(descrizione)}">
<meta property="og:url" content="{e(url_libro(slug))}">
{meta_immagine}
<script type="application/ld+json">
{schema_libro(b)}
</script>
<style>{STILE}</style>
</head>
<body>

<header>
  <div class="wrap"><a class="home" href="/">{e(NOME_SITO)}</a></div>
</header>

<main>
  <article>
    <div class="head">
      {copertina}
      <div>
        <h1>{e(titolo)}</h1>
        <div class="by">{e(intestazione)}</div>
        <div class="tags">{''.join(etichette)}</div>
        {'<div class="dati">' + ''.join(dati) + '</div>' if dati else ''}
      </div>
    </div>
    <div class="sinossi">{e(sinossi) if sinossi else 'Sinossi non ancora disponibile.'}</div>
    <div class="azioni">{''.join(azioni)}</div>
  </article>
</main>

<footer>
  <div>Scheda del catalogo di {e(NOME_SITO)}, dedicato alla narrativa fantasy italiana contemporanea.</div>
  <div style="margin-top:6px"><a href="/">Catalogo</a> · <a href="/legal.html">Note legali &amp; privacy</a></div>
</footer>

</body>
</html>
"""


# --------------------------------------------------------------------------
# Indice delle pagine libro
# --------------------------------------------------------------------------

def pagina_indice(libri):
    voci = []
    for b in libri:
        riga = e(b.get("title") or "(Senza titolo)")
        coda = " • ".join([p for p in [b.get("author") or "", b.get("publisher") or "", str(b.get("year") or "")] if p])
        voci.append(f'<li><a href="/libri/{e(b["slug"])}/">{riga}</a> <span class="muted">{e(coda)}</span></li>')

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tutte le schede | {e(NOME_SITO)}</title>
<meta name="description" content="Elenco completo delle schede del catalogo: {len(libri)} titoli di narrativa fantasy italiana.">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{e(SITO)}/libri/">
<style>{STILE}
  ul{{list-style:none;padding:0;margin:0;display:grid;gap:10px}}
  li{{background:#10121a;border:1px solid var(--border);border-radius:10px;padding:10px 12px}}
  li a{{color:var(--ink);text-decoration:none;font-weight:600}}
  li a:hover{{text-decoration:underline}}
  .muted{{color:var(--muted);font-size:13px;display:block;margin-top:2px}}
</style>
</head>
<body>

<header>
  <div class="wrap"><a class="home" href="/">{e(NOME_SITO)}</a></div>
</header>

<main>
  <h1>Tutte le schede</h1>
  <p class="muted">{len(libri)} titoli censiti. Il catalogo con ricerca e filtri sta in <a href="/">homepage</a>.</p>
  <ul>
    {chr(10).join(voci)}
  </ul>
</main>

<footer><div><a href="/">Catalogo</a> · <a href="/legal.html">Note legali &amp; privacy</a></div></footer>

</body>
</html>
"""


# --------------------------------------------------------------------------
# Pagina del singolo articolo
# --------------------------------------------------------------------------

def schema_articolo(a, descrizione):
    """schema.org/Article: e' quello che Google legge per datare l'articolo."""
    dati = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": riassunto(a.get("title") or "", 110),
        "url": url_articolo(a["slug"]),
        "inLanguage": "it",
        "datePublished": data_iso(a.get("published_at")),
        "dateModified": data_iso(a.get("updated_at") or a.get("published_at")),
        "author": {"@type": "Organization", "name": NOME_SITO, "url": SITO},
        "publisher": {"@type": "Organization", "name": NOME_SITO, "url": SITO},
        "mainEntityOfPage": {"@type": "WebPage", "@id": url_articolo(a["slug"])},
    }
    if descrizione:
        dati["description"] = descrizione
    return json.dumps(dati, ensure_ascii=False, indent=2).replace("<", "\\u003C")


def pagina_articolo(a):
    slug = a["slug"]
    titolo = a.get("title") or "(Senza titolo)"
    corpo = corpo_sicuro(a.get("body_html"))
    descrizione = riassunto(testo_semplice(corpo)) or f"{titolo} — {NOME_SITO}"

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titolo)} | {e(NOME_SITO)}</title>
<meta name="description" content="{e(descrizione)}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta name="theme-color" content="#7aa6ff">
<link rel="canonical" href="{e(url_articolo(slug))}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="{e(NOME_SITO)}">
<meta property="og:locale" content="it_IT">
<meta property="og:title" content="{e(titolo)}">
<meta property="og:description" content="{e(descrizione)}">
<meta property="og:url" content="{e(url_articolo(slug))}">
<meta property="article:published_time" content="{e(data_iso(a.get("published_at")))}">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">
{schema_articolo(a, descrizione)}
</script>
<style>{STILE}{STILE_ARTICOLO}</style>
</head>
<body>

<header>
  <div class="wrap"><a class="home" href="/">{e(NOME_SITO)}</a></div>
</header>

<main>
  <article class="articolo">
    <h1>{e(titolo)}</h1>
    <div class="data">{e(data_leggibile(a.get("published_at")))}</div>
    <div class="corpo">{corpo}</div>
    <div class="azioni">
      <a class="btn" href="/articoli/">Tutti gli articoli</a>
      <a class="btn" href="/">Vai al catalogo</a>
    </div>
  </article>
</main>

<footer>
  <div>Articolo di {e(NOME_SITO)}, dedicato alla narrativa fantasy italiana contemporanea.</div>
  <div style="margin-top:6px"><a href="/">Catalogo</a> &middot; <a href="/articoli/">Articoli</a> &middot; <a href="/legal.html">Note legali &amp; privacy</a></div>
</footer>

</body>
</html>
"""


def pagina_indice_articoli(articoli):
    voci = []
    for a in articoli:
        anteprima = riassunto(testo_semplice(a.get("body_html")), 180)
        voci.append(
            f'<li><a href="/articoli/{e(a["slug"])}/">{e(a.get("title") or "(Senza titolo)")}</a>'
            f'<span class="muted">{e(data_leggibile(a.get("published_at")))}</span>'
            f'<span class="muted">{e(anteprima)}</span></li>'
        )

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Articoli | {e(NOME_SITO)}</title>
<meta name="description" content="Articoli, guide e approfondimenti sulla narrativa fantasy italiana contemporanea.">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{e(SITO)}/articoli/">
<style>{STILE}
  ul{{list-style:none;padding:0;margin:0;display:grid;gap:10px}}
  li{{background:#10121a;border:1px solid var(--border);border-radius:10px;padding:12px 14px}}
  li a{{color:var(--ink);text-decoration:none;font-weight:600}}
  li a:hover{{text-decoration:underline}}
  .muted{{color:var(--muted);font-size:13px;display:block;margin-top:4px}}
</style>
</head>
<body>

<header>
  <div class="wrap"><a class="home" href="/">{e(NOME_SITO)}</a></div>
</header>

<main>
  <h1>Articoli</h1>
  <p class="muted">{len(articoli)} pubblicati. Il catalogo dei libri sta in <a href="/">homepage</a>.</p>
  <ul>
    {chr(10).join(voci)}
  </ul>
</main>

<footer><div><a href="/">Catalogo</a> &middot; <a href="/legal.html">Note legali &amp; privacy</a></div></footer>

</body>
</html>
"""


# --------------------------------------------------------------------------
# Sitemap e robots
# --------------------------------------------------------------------------

def sitemap(libri, articoli):
    # Il lastmod delle pagine indice e' la data del contenuto piu' recente
    # che elencano, non quella di oggi. Con "oggi" la sitemap cambiava ogni
    # giorno e veniva ricommittata senza motivo, e Google impara presto a
    # ignorare un lastmod che dice sempre la stessa cosa.
    date_libri = [data_iso(b.get("updated_at")) for b in libri if b.get("updated_at")]
    date_articoli = [data_iso(a.get("updated_at") or a.get("published_at")) for a in articoli
                     if a.get("updated_at") or a.get("published_at")]
    ripiego = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_libri = max(date_libri) if date_libri else ripiego
    data_articoli = max(date_articoli) if date_articoli else ripiego
    data_home = max(date_libri + date_articoli) if (date_libri or date_articoli) else ripiego
    righe = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{SITO}/</loc><lastmod>{data_home}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{SITO}/libri/</loc><lastmod>{data_libri}</lastmod><changefreq>weekly</changefreq></url>",
    ]
    if articoli:
        righe.append(
            f"  <url><loc>{SITO}/articoli/</loc><lastmod>{data_articoli}</lastmod>"
            f"<changefreq>weekly</changefreq></url>"
        )
    for b in libri:
        righe.append(
            f"  <url><loc>{url_libro(b['slug'])}</loc>"
            f"<lastmod>{data_iso(b.get('updated_at'))}</lastmod>"
            f"<changefreq>monthly</changefreq></url>"
        )
    # Un articolo cambia molto meno di una scheda: dichiararlo yearly evita
    # che i motori tornino a controllarlo senza motivo.
    for a in articoli:
        righe.append(
            f"  <url><loc>{url_articolo(a['slug'])}</loc>"
            f"<lastmod>{data_iso(a.get('updated_at') or a.get('published_at'))}</lastmod>"
            f"<changefreq>yearly</changefreq></url>"
        )
    righe.append("</urlset>")
    return "\n".join(righe) + "\n"


def robots():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /catalogo-admin.html\n"
        "Disallow: /news-admin.html\n"
        f"Sitemap: {SITO}/sitemap.xml\n"
    )


# --------------------------------------------------------------------------
# Scrittura
# --------------------------------------------------------------------------

def scrivi(percorso, contenuto, scritti):
    """Scrive solo se il contenuto e' cambiato: cosi' il commit resta pulito."""
    percorso.parent.mkdir(parents=True, exist_ok=True)
    if percorso.exists() and percorso.read_text(encoding="utf-8") == contenuto:
        return False
    percorso.write_text(contenuto, encoding="utf-8")
    scritti.append(str(percorso.relative_to(ROOT)))
    return True


def rimuovi_orfane(radice, slug_validi):
    """Pagine tolte o con slug cambiato: via le cartelle che non servono piu'."""
    rimosse = []
    if not radice.exists():
        return rimosse
    for cartella in radice.iterdir():
        if cartella.is_dir() and cartella.name not in slug_validi:
            shutil.rmtree(cartella)
            rimosse.append(cartella.name)
    return rimosse


def main():
    ap = argparse.ArgumentParser(description="Genera le pagine statiche di libri e articoli.")
    ap.add_argument("--dry-run", action="store_true", help="non scrive niente, riepiloga e basta")
    ap.add_argument("--da-file", help="legge i libri da un JSON locale invece che da Supabase")
    ap.add_argument("--solo", choices=["libri", "articoli"],
                    help="genera solo una delle due famiglie di pagine")
    args = ap.parse_args()

    fai_libri = args.solo != "articoli"
    fai_articoli = args.solo != "libri"

    libri = []
    if fai_libri:
        if args.da_file:
            libri = json.loads(Path(args.da_file).read_text(encoding="utf-8"))
        else:
            libri = scarica_libri()

    # Gli articoli stanno su Supabase: con --da-file si sta provando offline
    # il solo catalogo, quindi si lasciano stare.
    articoli = []
    if fai_articoli and not args.da_file:
        articoli = scarica_articoli()

    validi, scartati = [], []
    for b in libri:
        slug = (b.get("slug") or "").strip()
        if SLUG_VALIDO.match(slug):
            validi.append(b)
        else:
            scartati.append(b.get("title") or b.get("slug") or "?")

    articoli_validi = []
    for a in articoli:
        slug = (a.get("slug") or "").strip()
        if SLUG_VALIDO.match(slug):
            articoli_validi.append(a)
        else:
            scartati.append(a.get("title") or a.get("slug") or "?")

    if scartati:
        print(f"Slug non validi, saltati: {len(scartati)} -> {', '.join(scartati[:5])}")

    if not validi and not articoli_validi:
        print("Niente da pubblicare: non tocco niente.")
        return 0

    # Una lettura a vuoto non deve cancellare quello che c'e': se il catalogo
    # torna vuoto per un errore di rete si fermano anche le rimozioni, che
    # altrimenti spazzerebbero via 53 pagine buone.
    if fai_libri and not validi:
        print("Nessuna scheda approvata con slug: lascio stare le pagine libro.")
        fai_libri = False
    if fai_articoli and not articoli_validi:
        print("Nessun articolo pubblicato con slug: lascio stare le pagine articolo.")
        fai_articoli = False

    print(f"Schede approvate: {len(validi)} — articoli pubblicati: {len(articoli_validi)}")

    if args.dry_run:
        for b in validi[:3]:
            print("  ", url_libro(b["slug"]))
        for a in articoli_validi[:3]:
            print("  ", url_articolo(a["slug"]))
        print("   (dry-run: nessun file scritto)")
        return 0

    scritti = []
    if fai_libri:
        for b in validi:
            scrivi(DIR_LIBRI / b["slug"] / "index.html", pagina_libro(b), scritti)
        scrivi(DIR_LIBRI / "index.html", pagina_indice(validi), scritti)

    if fai_articoli:
        for a in articoli_validi:
            scrivi(DIR_ARTICOLI / a["slug"] / "index.html", pagina_articolo(a), scritti)
        scrivi(DIR_ARTICOLI / "index.html", pagina_indice_articoli(articoli_validi), scritti)

    # La sitemap si riscrive solo se si e' guardato tutto: con --solo
    # resterebbe fuori meta' del sito.
    if fai_libri and fai_articoli:
        scrivi(SITEMAP, sitemap(validi, articoli_validi), scritti)
        scrivi(ROBOTS, robots(), scritti)

    rimosse = []
    if fai_libri:
        rimosse += rimuovi_orfane(DIR_LIBRI, {b["slug"] for b in validi} | {"index.html"})
    if fai_articoli:
        rimosse += rimuovi_orfane(DIR_ARTICOLI, {a["slug"] for a in articoli_validi} | {"index.html"})

    print(f"File scritti o aggiornati: {len(scritti)}")
    for p in scritti[:10]:
        print("  ", p)
    if len(scritti) > 10:
        print(f"   … e altri {len(scritti) - 10}")
    if rimosse:
        print(f"Cartelle rimosse: {', '.join(rimosse)}")
    if not scritti and not rimosse:
        print("Tutto già aggiornato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
