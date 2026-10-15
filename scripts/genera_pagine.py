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
import urllib.parse
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


# --------------------------------------------------------------------------
# Riga "Condividi"
# --------------------------------------------------------------------------
# Le stesse icone e lo stesso comportamento della scheda aperta dal catalogo
# (index.html): chi arriva qui da un link condiviso deve poterlo inoltrare
# senza tornare al catalogo. Le due pagine non hanno codice in comune — il
# sito non ha un passaggio di build — quindi cio' che si cambia la' va
# riportato qui: icone, hashtag, testo della didascalia.

# Tracciati di Simple Icons (licenza CC0); "link" sono due anelli a tratto.
ICONE = {
    "whatsapp": "M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z",
    "instagram": "M7.0301.084c-1.2768.0602-2.1487.264-2.911.5634-.7888.3075-1.4575.72-2.1228 1.3877-.6652.6677-1.075 1.3368-1.3802 2.127-.2954.7638-.4956 1.6365-.552 2.914-.0564 1.2775-.0689 1.6882-.0626 4.947.0062 3.2586.0206 3.6671.0825 4.9473.061 1.2765.264 2.1482.5635 2.9107.308.7889.72 1.4573 1.388 2.1228.6679.6655 1.3365 1.0743 2.1285 1.38.7632.295 1.6361.4961 2.9134.552 1.2773.056 1.6884.069 4.9462.0627 3.2578-.0062 3.668-.0207 4.9478-.0814 1.28-.0607 2.147-.2652 2.9098-.5633.7889-.3086 1.4578-.72 2.1228-1.3881.665-.6682 1.0745-1.3378 1.3795-2.1284.2957-.7632.4966-1.636.552-2.9124.056-1.2809.0692-1.6898.063-4.948-.0063-3.2583-.021-3.6668-.0817-4.9465-.0607-1.2797-.264-2.1487-.5633-2.9117-.3084-.7889-.72-1.4568-1.3876-2.1228C21.2982 1.33 20.628.9208 19.8378.6165 19.074.321 18.2017.1197 16.9244.0645 15.6471.0093 15.236-.005 11.977.0014 8.718.0076 8.31.0215 7.0301.0839m.1402 21.6932c-1.17-.0509-1.8053-.2453-2.2287-.408-.5606-.216-.96-.4771-1.3819-.895-.422-.4178-.6811-.8186-.9-1.378-.1644-.4234-.3624-1.058-.4171-2.228-.0595-1.2645-.072-1.6442-.079-4.848-.007-3.2037.0053-3.583.0607-4.848.05-1.169.2456-1.805.408-2.2282.216-.5613.4762-.96.895-1.3816.4188-.4217.8184-.6814 1.3783-.9003.423-.1651 1.0575-.3614 2.227-.4171 1.2655-.06 1.6447-.072 4.848-.079 3.2033-.007 3.5835.005 4.8495.0608 1.169.0508 1.8053.2445 2.228.408.5608.216.96.4754 1.3816.895.4217.4194.6816.8176.9005 1.3787.1653.4217.3617 1.056.4169 2.2263.0602 1.2655.0739 1.645.0796 4.848.0058 3.203-.0055 3.5834-.061 4.848-.051 1.17-.245 1.8055-.408 2.2294-.216.5604-.4763.96-.8954 1.3814-.419.4215-.8181.6811-1.3783.9-.4224.1649-1.0577.3617-2.2262.4174-1.2656.0595-1.6448.072-4.8493.079-3.2045.007-3.5825-.006-4.848-.0608M16.953 5.5864A1.44 1.44 0 1 0 18.39 4.144a1.44 1.44 0 0 0-1.437 1.4424M5.8385 12.012c.0067 3.4032 2.7706 6.1557 6.173 6.1493 3.4026-.0065 6.157-2.7701 6.1506-6.1733-.0065-3.4032-2.771-6.1565-6.174-6.1498-3.403.0067-6.156 2.771-6.1496 6.1738M8 12.0077a4 4 0 1 1 4.008 3.9921A3.9996 3.9996 0 0 1 8 12.0077",
    "facebook": "M9.101 23.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085 1.848-5.978 5.858-5.978.401 0 .955.042 1.468.103a8.68 8.68 0 0 1 1.141.195v3.325a8.623 8.623 0 0 0-.653-.036 26.805 26.805 0 0 0-.733-.009c-.707 0-1.259.096-1.675.309a1.686 1.686 0 0 0-.679.622c-.258.42-.374.995-.374 1.752v1.297h3.919l-.386 2.103-.287 1.564h-3.246v8.245C19.396 23.238 24 18.179 24 12.044c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.628 3.874 10.35 9.101 11.647Z",
    "x": "M14.234 10.162 22.977 0h-2.072l-7.591 8.824L7.251 0H.258l9.168 13.343L.258 24H2.33l8.016-9.318L16.749 24h6.993zm-2.837 3.299-.929-1.329L3.076 1.56h3.182l5.965 8.532.929 1.329 7.754 11.09h-3.182z",
    "bluesky": "M5.202 2.857C7.954 4.922 10.913 9.11 12 11.358c1.087-2.247 4.046-6.436 6.798-8.501C20.783 1.366 24 .213 24 3.883c0 .732-.42 6.156-.667 7.037-.856 3.061-3.978 3.842-6.755 3.37 4.854.826 6.089 3.562 3.422 6.299-5.065 5.196-7.28-1.304-7.847-2.97-.104-.305-.152-.448-.153-.327 0-.121-.05.022-.153.327-.568 1.666-2.782 8.166-7.847 2.97-2.667-2.737-1.432-5.473 3.422-6.3-2.777.473-5.899-.308-6.755-3.369C.42 10.04 0 4.615 0 3.883c0-3.67 3.217-2.517 5.202-1.026",
    "link": "M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1",
}

# Uguali a IG_ETICHETTE in index.html.
IG_ETICHETTE = "#fantasyitaliano #fantasy #librifantasy #libridaleggere #bookstagramitalia"

A_CAPO = chr(10)

STILE_CONDIVIDI = """
  .condividi{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:16px}
  .condividi .voce{font-size:13px;color:var(--muted);margin-right:2px}
  .ico{width:40px;height:40px;padding:0;border-radius:10px;background:#151826;border:1px solid var(--border);
       color:var(--ink);display:inline-flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none}
  .ico svg{width:20px;height:20px;fill:currentColor}
  .ico svg.tratto{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .ico:hover,.ico.pronto{border-color:var(--accent);color:var(--accent)}
  .nota{margin-top:10px;font-size:13px;line-height:1.5;color:var(--muted)}
  @media (max-width:640px){ .condividi .voce{flex-basis:100%} }
  /* Negli articoli la riga sta fra la data e il testo: serve aria sotto. */
  .articolo .condividi{margin:0 0 20px}
  .articolo .nota{margin:-10px 0 20px}
"""

# Instagram non accetta link, vuole un'immagine: dal telefono la si passa al
# pannello di condivisione, dal computer la si scarica; la didascalia va negli
# appunti perche' Instagram ignora il testo passato da fuori. E' la stessa
# logica di pulsanteInstagram() in index.html, scritta senza dipendenze.
# L'immagine si chiede con cache 'no-cache': lo Storage la fa tenere ai browser
# per un giorno, e dopo una correzione alla scheda tornava quella vecchia.
SCRIPT_CONDIVIDI = """
(function(){
  var D = __DATI__;
  var nota = document.getElementById('condNota');
  function avvisa(t){ nota.textContent = t; nota.hidden = !t; }
  var link = document.getElementById('condLink');
  if(link){
    if(!navigator.clipboard){ link.hidden = true; }
    else link.addEventListener('click', function(){
      navigator.clipboard.writeText(D.url).then(function(){ avvisa('Link copiato.'); }).catch(function(){});
    });
  }
  var ig = document.getElementById('condIg');
  if(!ig) return;
  var file = null;
  ig.addEventListener('click', async function(){
    var copiata = false;
    var appunti = navigator.clipboard
      ? navigator.clipboard.writeText(D.didascalia).then(function(){ copiata = true; }).catch(function(){})
      : Promise.resolve();
    if(!file){
      ig.disabled = true; avvisa('Preparo l’immagine…');
      try{
        var r = await fetch(D.immagine, { cache: 'no-cache' });
        if(!r.ok) throw new Error('HTTP ' + r.status);
        file = new File([await r.blob()], D.nomeFile, { type: 'image/jpeg' });
      }catch(e){
        avvisa('L’immagine per Instagram non è ancora pronta: viene preparata entro sei ore dalla pubblicazione della scheda.');
        return;
      }finally{
        ig.disabled = false;
      }
    }
    ig.classList.remove('pronto');
    function didascalia(){ return copiata ? ' La didascalia è negli appunti: incollala nel post.' : ''; }
    var suTelefono = window.matchMedia && window.matchMedia('(pointer:coarse)').matches;
    if(suTelefono && navigator.canShare && navigator.canShare({ files: [file] })){
      var esiti = await Promise.allSettled([navigator.share({ files: [file] }), appunti]);
      if(esiti[0].status === 'fulfilled'){ avvisa('Nel pannello scegli Instagram e poi «Post».' + didascalia()); return; }
      var nome = esiti[0].reason && esiti[0].reason.name;
      if(nome === 'AbortError'){ avvisa(''); return; }
      if(nome === 'NotAllowedError'){
        ig.classList.add('pronto');
        avvisa('Immagine pronta: tocca ancora l’icona di Instagram.');
        return;
      }
    }
    await appunti;
    var a = document.createElement('a');
    a.href = URL.createObjectURL(file); a.download = file.name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 10000);
    avvisa('Immagine scaricata.' + didascalia());
  });
})();
"""


def _icona(nome):
    classe = ' class="tratto"' if nome == "link" else ""
    return f'<svg viewBox="0 0 24 24" aria-hidden="true"{classe}><path d="{ICONE[nome]}"/></svg>'


def riga_condividi(indirizzo, testo, instagram=None):
    """
    La riga con le icone, la riga di stato e lo script.

    WhatsApp, Facebook, X e Bluesky sono link semplici con il messaggio gia'
    scritto: nessuno script di terzi, quindi nessun cookie e nessun
    tracciamento finche' non si tocca l'icona. `instagram`, solo per i libri,
    porta indirizzo dell'immagine, nome del file e didascalia.
    """
    q = lambda s: urllib.parse.quote(s, safe="")
    link = [
        ("whatsapp", "Condividi su WhatsApp", "https://wa.me/?text=" + q(testo + A_CAPO + indirizzo)),
        ("facebook", "Condividi su Facebook", "https://www.facebook.com/sharer/sharer.php?u=" + q(indirizzo)),
        ("x", "Condividi su X", "https://x.com/intent/post?text=" + q(testo) + "&url=" + q(indirizzo)),
        ("bluesky", "Condividi su Bluesky", "https://bsky.app/intent/compose?text=" + q(testo + A_CAPO + indirizzo)),
    ]

    def a(nome, etichetta, href):
        return (f'<a class="ico" href="{e(href)}" target="_blank" rel="noopener" '
                f'title="{e(etichetta)}" aria-label="{e(etichetta)}">{_icona(nome)}</a>')

    def bottone(ident, nome, etichetta):
        return (f'<button class="ico" type="button" id="{ident}" '
                f'title="{e(etichetta)}" aria-label="{e(etichetta)}">{_icona(nome)}</button>')

    pezzi = ['<span class="voce">Condividi</span>', a(*link[0])]
    if instagram:
        pezzi.append(bottone("condIg", "instagram", "Condividi su Instagram"))
    pezzi.extend(a(*voce) for voce in link[1:])
    pezzi.append(bottone("condLink", "link", "Copia il link"))

    dati = {"url": indirizzo}
    dati.update(instagram or {})
    # I < diventano escape, come nei dati strutturati: un titolo che contenesse
    # </script> chiuderebbe il blocco.
    dati_js = json.dumps(dati, ensure_ascii=False).replace("<", chr(92) + "u003C")
    return (
        '<div class="condividi">' + "".join(pezzi) + "</div>" + A_CAPO
        + '    <div class="nota" id="condNota" role="status" hidden></div>' + A_CAPO
        + "    <script>" + SCRIPT_CONDIVIDI.replace("__DATI__", dati_js) + "</script>"
    )


def dati_instagram(b):
    """Immagine e didascalia per Instagram: come didascaliaInstagram() in index.html."""
    slug = b["slug"]
    righe = ["«" + (b.get("title") or "") + "»" + (" di " + b["author"] if b.get("author") else "")]
    edizione = ", ".join(str(x) for x in (b.get("publisher"), b.get("year")) if x)
    if edizione:
        righe.append(edizione)
    if b.get("is_debut") is True:
        righe.append("Un esordio.")
    righe += ["", "La scheda completa è nel catalogo di Fantasy Italia — Nuove Proposte:",
              url_libro(slug).split("://", 1)[-1], "",
              IG_ETICHETTE + (" #esordio" if b.get("is_debut") is True else "")]
    return {
        # Lo stesso nome di file che usa scripts/immagini_social.py.
        "immagine": f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/copertine/instagram/{slug}.jpg",
        "nomeFile": slug + ".jpg",
        "didascalia": A_CAPO.join(righe),
    }


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

    condividi = riga_condividi(
        url_libro(slug),
        "«" + titolo + "»" + (" di " + autore if autore else ""),
        dati_instagram(b),
    )

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
<style>{STILE}{STILE_CONDIVIDI}</style>
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
    {condividi}
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
    # Senza Instagram: vuole un'immagine, e per gli articoli non se ne prepara una.
    condividi = riga_condividi(url_articolo(slug), titolo)

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
<style>{STILE}{STILE_ARTICOLO}{STILE_CONDIVIDI}</style>
</head>
<body>

<header>
  <div class="wrap"><a class="home" href="/">{e(NOME_SITO)}</a></div>
</header>

<main>
  <article class="articolo">
    <h1>{e(titolo)}</h1>
    <div class="data">{e(data_leggibile(a.get("published_at")))}</div>
    {condividi}
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
