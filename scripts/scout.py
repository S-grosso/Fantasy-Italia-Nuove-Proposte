#!/usr/bin/env python3
"""
Scout — Fantasy Italia, Nuove Proposte
======================================

Scopre le nuove uscite fantasy italiane interrogando i siti degli editori
monitorati, le classifica, e scrive i candidati in data/candidates.json
perche' tu li approvi dal pannello Moderazione.

Non pubblica nulla da solo: produce candidati, non voci di catalogo.

Uso:
    python scripts/scout.py                    # ultimi 30 giorni
    python scripts/scout.py --dal 2025-07-01   # finestra esplicita (backfill)
    python scripts/scout.py --dry-run          # non scrive, stampa e basta
"""

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests

# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "data" / "sources.json"
CATALOG_FILE = ROOT / "data" / "catalogo.json"
CANDIDATES_FILE = ROOT / "data" / "candidates.json"

TIMEOUT = 25
PAUSA = 1.0  # cortesia verso i server degli editori

# Pausa tra chiamate al modello. Parte da ZERO: il free tier regge una raffica
# iniziale, e farla aspettare a vuoto e' tempo buttato. Si alza da sola al primo
# 429 e si riabbassa quando il modello torna a rispondere.
# (La versione a pausa fissa di 6s faceva durare il run un'ora e mezza.)
PAUSA_MODELLO_MIN = 0.0
PAUSA_MODELLO_MAX = 12.0

UA = {"User-Agent": "FantasyItaliaBot/1.0 (+https://www.fantasyitalianuoveproposte.it)"}

GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"

# GitHub Models: gratuito dentro le Actions, si autentica col GITHUB_TOKEN.
GH_MODELS_URL = "https://models.github.ai/inference/chat/completions"
GH_MODEL = "openai/gpt-4o-mini"

# Parole che qualificano il genere in modo ESPLICITO. Sono il segnale forte:
# quando l'editore si autodichiara, non serve inferire dalla trama.
GENERI_ESPLICITI = [
    "romantasy", "fantasy romance", "romance fantasy",
    "urban fantasy", "dark fantasy", "high fantasy", "epic fantasy",
    "fantasy storico", "historical fantasy", "low fantasy",
    "grimdark", "sword and sorcery", "portal fantasy",
    "fantasy epico", "fantasy eroico", "science fantasy",
    "fantasy",  # generico, per ultimo: match piu' debole
]

# Roba che NON e' un romanzo: bundle, gadget, abbonamenti, merchandising.
NON_LIBRI = [
    "kit ", "bundle", "cofanetto", "abbonamento", "gadget", "poster",
    "segnalibro", "shopper", "spilla", "tazza", "maglietta", "t-shirt",
    "buono regalo", "gift card", "iscrizione", "quota", "pubblicita",
]


# --------------------------------------------------------------------------
# Utilita'
# --------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def strip_html(testo):
    """Toglie i tag HTML e normalizza gli spazi. I feed sono pieni di markup."""
    if not testo:
        return ""
    testo = re.sub(r"<[^>]+>", " ", testo)
    testo = html.unescape(testo)
    return re.sub(r"\s+", " ", testo).strip()


def normalizza_isbn(raw):
    """
    Porta qualunque forma di ISBN a ISBN-13 senza trattini.
    Il catalogo attuale ha formati misti ('979-1280868190', '9788825425666'),
    e almeno un valore corrotto ('1254980679', che e' un frammento dell'ISBN
    vero di Urbis): senza questa normalizzazione il dedup non funziona.

    Verifica la cifra di controllo: un numero che non la supera NON viene
    convertito, perche' produrrebbe un ISBN plausibile ma falso — e un ISBN
    falso nel dedup e' peggio di un ISBN assente (fa entrare doppioni o, peggio,
    fa scartare libri buoni).
    Restituisce None se non e' un ISBN valido.
    """
    if not raw:
        return None
    cifre = re.sub(r"[^0-9Xx]", "", str(raw)).upper()

    if len(cifre) == 13:
        if not cifre.isdigit():
            return None
        somma = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(cifre[:12]))
        if (10 - somma % 10) % 10 != int(cifre[12]):
            return None  # cifra di controllo sbagliata
        return cifre

    if len(cifre) == 10:
        # Verifica la cifra di controllo dell'ISBN-10 prima di fidarsi
        somma = 0
        for i, c in enumerate(cifre[:9]):
            if not c.isdigit():
                return None
            somma += (10 - i) * int(c)
        atteso = 11 - (somma % 11)
        controllo = "X" if atteso == 10 else ("0" if atteso == 11 else str(atteso))
        if controllo != cifre[9]:
            return None  # non e' un ISBN-10 valido: probabilmente un frammento

        core = "978" + cifre[:9]
        somma13 = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core))
        return core + str((10 - somma13 % 10) % 10)

    return None  # lunghezza anomala


def chiave_titolo(titolo, autore=""):
    """
    Chiave di dedup di riserva, per quando l'ISBN manca.
    Abbassa, toglie accenti e punteggiatura: 'Seiđmađur – Lo Sciamano' e
    'Seidmadur - Lo sciamano' devono collidere.

    Attenzione: NFKD non basta. Caratteri come 'đ' (d con tratto) o 'ø' non
    sono lettera+accento combinabili, sono glifi autonomi: vanno mappati a mano,
    altrimenti lo stesso libro entra due volte in catalogo.
    """
    SOSTITUZIONI = {
        "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
        "ø": "o", "Ø": "O", "ł": "l", "Ł": "L",
        "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
        "ß": "ss", "þ": "th", "Þ": "TH",
    }
    testo = f"{titolo} {autore}"
    for k, v in SOSTITUZIONI.items():
        testo = testo.replace(k, v)
    testo = testo.lower()
    testo = unicodedata.normalize("NFKD", testo)
    testo = "".join(c for c in testo if not unicodedata.combining(c))
    testo = re.sub(r"[^a-z0-9]+", "", testo)
    return testo


def chiave_solo_titolo(titolo):
    """
    Chiave basata SOLO sul titolo normalizzato, senza autore e senza troncare.
    Il confronto vero e proprio (per contenimento) lo fa titolo_gia_noto():
    qui restituiamo solo la forma pulita.
    """
    SOSTITUZIONI = {
        "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "ø": "o", "Ø": "O",
        "ł": "l", "Ł": "L", "æ": "ae", "œ": "oe", "ß": "ss", "þ": "th",
    }
    t = titolo or ""
    for k, v in SOSTITUZIONI.items():
        t = t.replace(k, v)
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", t)


def titolo_gia_noto(titolo_candidato, prefissi_noti):
    """
    Vero se il titolo del candidato e' gia' in catalogo, tollerando i
    sottotitoli in entrambe le direzioni:
      'Namirya'  vs  'Namirya. L'enigma degli Elfi'  -> stesso libro
    Confronta per CONTENIMENTO sul piu' corto dei due, con una soglia minima
    di 6 caratteri per non far collidere titoli genericamente brevi.
    """
    c = chiave_solo_titolo(titolo_candidato)
    if len(c) < 6:
        # Titolo troppo corto: il contenimento darebbe falsi positivi.
        # Ci si affida al confronto esatto altrove.
        return c in prefissi_noti
    for noto in prefissi_noti:
        if len(noto) < 6:
            continue
        corto, lungo = (c, noto) if len(c) <= len(noto) else (noto, c)
        if lungo.startswith(corto):
            return True
    return False


GOOGLE_BOOKS_KEY = os.environ.get("GOOGLE_BOOKS_KEY", "")


def get_json(url, params=None):
    # Aggancia la API key a ogni chiamata Google Books. Senza chiave la quota
    # anonima e' praticamente zero (429 "quota_limit_value: 0"): e' la ragione
    # per cui arricchimento e ricerca per autore fallivano a intermittenza.
    if GOOGLE_BOOKS_KEY and url.startswith(GOOGLE_BOOKS):
        params = dict(params or {})
        params["key"] = GOOGLE_BOOKS_KEY
    try:
        r = requests.get(url, headers=UA, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


# --------------------------------------------------------------------------
# Adapter: uno per piattaforma
# --------------------------------------------------------------------------

def adapter_wordpress(source, dal):
    """
    Adapter generico per la REST API di WordPress. Funziona con qualunque
    post type: 'product' (WooCommerce), 'book'/'books' (custom, come Acheron),
    o altri. L'endpoint nel sources.json decide quale.

    Copre: Lumien, La Corte, La Nuova Carne, Alcatraz, Parallelo45, Angolazioni
    (product) e Acheron (books).
    """
    trovati = []
    endpoint = source["endpoint"]
    autori_cache = {}

    # La tassonomia 'autore' (dove esiste) da' il nome dell'autore senza doverlo
    # indovinare dal testo. Lumien ce l'ha; la scarichiamo una volta sola.
    tax = source.get("author_taxonomy")
    if tax:
        base = endpoint.rsplit("/", 1)[0]
        termini = get_json(f"{base}/{tax}", {"per_page": 100})
        if isinstance(termini, list):
            autori_cache = {t["id"]: t.get("name", "") for t in termini if "id" in t}

    pagina = 1
    while pagina <= 5:  # tetto di sicurezza: 500 prodotti per editore
        dati = get_json(endpoint, {
            "per_page": 100,
            "page": pagina,
            "after": dal.isoformat(),
            "orderby": "date",
            "order": "desc",
            # _embed fa restituire a WordPress anche i dati collegati, tra cui
            # l'immagine in evidenza. Senza questo, 'featured_media' e' solo un
            # ID numerico e le copertine restano vuote.
            "_embed": "wp:featuredmedia",
        })
        if not isinstance(dati, list) or not dati:
            break

        for p in dati:
            titolo = strip_html((p.get("title") or {}).get("rendered", ""))
            if not titolo:
                continue

            # L'excerpt e' la presentazione editoriale: contiene i segnali
            # piu' preziosi ("gia' autore di...", "esordio", il genere).
            excerpt = strip_html((p.get("excerpt") or {}).get("rendered", ""))
            contenuto = strip_html((p.get("content") or {}).get("rendered", ""))

            autore = ""
            if tax and isinstance(p.get(tax), list):
                nomi = [autori_cache.get(i, "") for i in p[tax]]
                autore = " & ".join(n for n in nomi if n)

            # Fallback autore per i post type senza tassonomia dedicata (es. il
            # 'book' di Acheron): molti temi mettono l'autore in un meta o in
            # una tassonomia dal nome diverso. Proviamo i nomi piu' comuni.
            if not autore:
                for campo in ("author_name", "book_author", "autore", "authors"):
                    val = p.get(campo) or (p.get("meta") or {}).get(campo)
                    if isinstance(val, list):
                        val = ", ".join(str(x) for x in val)
                    if val and isinstance(val, str) and val.strip():
                        autore = strip_html(val)
                        break

            # La copertina arriva dentro _embedded, annidata in profondita'.
            copertina = ""
            try:
                media = p["_embedded"]["wp:featuredmedia"][0]
                # 'full' e' l'originale; se manca si ripiega sul source_url.
                sizes = media.get("media_details", {}).get("sizes", {})
                copertina = (
                    sizes.get("full", {}).get("source_url")
                    or sizes.get("large", {}).get("source_url")
                    or media.get("source_url", "")
                )
            except (KeyError, IndexError, TypeError):
                pass  # nessuna immagine: la recupereremo da Google Books

            trovati.append({
                "titolo": titolo,
                "autore": autore,
                "editore": source["publisher"],
                "url": p.get("link", ""),
                "sinossi": contenuto or excerpt,
                "paratesto": excerpt,  # separato: e' la voce dell'editore
                "copertina": copertina,
                "data": p.get("date", ""),
                "categorie": p.get("class_list", []),  # contiene product_cat-*
                "_fonte": "woocommerce",
            })

        if len(dati) < 100:
            break
        pagina += 1
        time.sleep(PAUSA)

    return trovati


def adapter_shopify(source, dal):
    """
    Shopify espone /products.json su qualunque store pubblico.
    Copre: Giunti, Another Coffee Stories.
    """
    trovati = []
    pagina = 1
    # Giunti ha migliaia di prodotti ma li serve in ordine cronologico inverso:
    # le pagine oltre la quarta sono libri vecchi che il filtro data scarterebbe
    # comunque. Quattro pagine (1000 prodotti) coprono abbondantemente un anno.
    max_pagine = 4 if source.get("prefiltro_obbligatorio") else 10
    fuori_finestra = 0

    while pagina <= max_pagine:
        dati = get_json(source["endpoint"], {"limit": 250, "page": pagina})
        if not isinstance(dati, dict):
            break
        prodotti = dati.get("products", [])
        if not prodotti:
            break

        for p in prodotti:
            pubblicato = p.get("published_at") or p.get("created_at") or ""
            if pubblicato:
                try:
                    quando = datetime.fromisoformat(pubblicato.replace("Z", "+00:00"))
                    if quando < dal:
                        fuori_finestra += 1
                        continue
                except ValueError:
                    pass

            # ATTENZIONE: su Shopify editoriale 'vendor' e' l'EDITORE, non l'autore.
            # Per Giunti, 'vendor' vale "Giunti Editore": usarlo come autore faceva
            # risultare "Uncharmed" (di Lucy Jane Wood, tradotta) come scritto da
            # un italiano. Se vendor coincide con l'editore, l'autore va cercato
            # altrove: nei tag "Autore_..." o, in mancanza, da Google Books.
            vendor = (p.get("vendor") or "").strip()
            editore_nome = source["publisher"].lower()
            autore = ""
            if vendor and vendor.lower() not in editore_nome and editore_nome not in vendor.lower():
                autore = vendor  # e' un vero autore, non l'editore

            # Cerca un tag "Autore: X" / "Autore_X" (comune negli store editoriali)
            for tag in (p.get("tags") or []):
                t = str(tag)
                m = re.match(r"(?:autore|author)[\s:_-]+(.+)", t, re.IGNORECASE)
                if m:
                    autore = m.group(1).strip()
                    break

            # Shopify porta le immagini nel payload: bastava leggerle.
            copertina = ""
            immagini = p.get("images") or []
            if immagini and isinstance(immagini[0], dict):
                copertina = immagini[0].get("src", "")

            trovati.append({
                "titolo": p.get("title", ""),
                "autore": autore,
                "editore": source["publisher"],
                "url": f"{source['endpoint'].replace('/products.json', '')}/products/{p.get('handle', '')}",
                "sinossi": strip_html(p.get("body_html", "")),
                "paratesto": "",
                "copertina": copertina,
                "data": pubblicato,
                "categorie": p.get("tags", []) + [p.get("product_type", "")],
                "_fonte": "shopify",
            })

        # Se un'intera pagina e' fuori finestra, le successive lo saranno di piu'.
        if fuori_finestra >= 250 and not trovati:
            break

        if len(prodotti) < 250:
            break
        pagina += 1
        time.sleep(PAUSA)

    return trovati


def adapter_rss(source, dal):
    """
    Feed RSS: l'ultima spiaggia.
    Copre: Acheron, Zona 42, Astro, Sperling.
    I metadati sono poveri (spesso non c'e' nemmeno l'autore del libro): questi
    candidati vanno arricchiti con Google Books prima di essere presentabili.
    """
    trovati = []
    try:
        r = requests.get(source["endpoint"], headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        radice = ElementTree.fromstring(r.content)
    except (requests.RequestException, ElementTree.ParseError):
        return []

    for item in radice.iter("item"):
        titolo = (item.findtext("title") or "").strip()
        if not titolo:
            continue

        pubdata = item.findtext("pubDate") or ""
        # I feed usano il formato RFC-822; il parsing e' tollerante perche'
        # una data assente non deve far perdere il candidato.
        try:
            from email.utils import parsedate_to_datetime
            quando = parsedate_to_datetime(pubdata)
            if quando and quando < dal:
                continue
        except (TypeError, ValueError):
            pass

        descrizione = strip_html(
            item.findtext("description")
            or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            or ""
        )

        trovati.append({
            "titolo": titolo,
            "autore": "",
            "editore": source["publisher"],
            "url": (item.findtext("link") or "").strip(),
            "sinossi": descrizione,
            "paratesto": descrizione,
            "data": pubdata,
            "categorie": [c.text for c in item.iter("category") if c.text],
            "_fonte": "rss",
        })

    return trovati


def adapter_google_books_autori(source, dal, autori_editore=None):
    """
    Per gli editori SENZA API di catalogo (Acheron, Zona 42, Astro, Sperling):
    invece di sperare nel feed RSS — che perde quasi tutti i libri, come hanno
    dimostrato i cinque titoli Acheron sfuggiti — si cerca su Google Books per
    AUTORE.

    L'intuizione: questi editori hanno una scuderia ricorrente. Se Giacomo Arzani
    e' gia' in catalogo con un titolo Acheron, cercando 'inauthor:Arzani' si
    trovano anche i suoi libri nuovi. Gli autori vengono da due fonti:
      1. quelli gia' in catalogo per questo editore (passati in autori_editore)
      2. una watchlist manuale nel sources.json (campo 'autori_watch')

    Non trova autori mai visti prima — per quelli serve la segnalazione manuale —
    ma recupera tutta la produzione degli autori noti, che e' la maggioranza.
    """
    trovati = []
    editore = source["publisher"]
    visti = set()

    autori = set(autori_editore or [])
    autori.update(source.get("autori_watch", []))
    autori.discard("")

    if not autori:
        return []  # nessun autore noto: niente da cercare

    for autore in sorted(autori):
        dati = get_json(GOOGLE_BOOKS, {
            "q": f'inauthor:"{autore}"',
            "langRestrict": "it",
            "orderBy": "newest",
            "maxResults": 20,
            "printType": "books",
        })
        time.sleep(0.4)
        if not isinstance(dati, dict):
            continue

        for v in dati.get("items", []):
            info = v.get("volumeInfo", {})
            titolo = info.get("title", "")
            if not titolo or titolo in visti:
                continue

            # Deve essere di QUESTO editore — ma se Google Books non riporta il
            # publisher (frequente per gli ebook dei piccoli editori), NON scarto:
            # l'autore e' gia' associato a questo editore dalla watchlist o dal
            # catalogo, e questo basta come indizio. Scarto solo se il publisher
            # c'e' ed e' un ALTRO editore.
            ed_pubblicato = (info.get("publisher") or "").lower()
            nome_ed = editore.lower().split()[0]  # 'acheron' da 'Acheron Books'
            if ed_pubblicato and nome_ed not in ed_pubblicato:
                continue

            visti.add(titolo)
            data_pub = info.get("publishedDate", "")
            try:
                if int(data_pub[:4]) < dal.year:
                    continue
            except (ValueError, IndexError):
                continue

            isbn = ""
            for ident in info.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_13":
                    isbn = ident.get("identifier", "")
                    break

            img = (info.get("imageLinks") or {}).get("thumbnail", "")
            trovati.append({
                "titolo": titolo,
                "autore": ", ".join(info.get("authors", [])) or autore,
                "editore": editore,
                "url": info.get("infoLink", ""),
                "sinossi": info.get("description", ""),
                "paratesto": "",
                "isbn": isbn,
                "copertina": (img.replace("http://", "https://")
                                 .replace("&zoom=1", "&zoom=2")
                                 .replace("&edge=curl", "")),
                "data": data_pub,
                "categorie": info.get("categories", []),
                "_fonte": "google_books_autori",
            })

    return trovati


def adapter_google_books(source, dal):
    """
    Per gli editori senza API: si interroga Google Books per editore.
    Copre: Delos Digital, PresentARTsi.

    La query 'inpublisher' e' capricciosa: il nome dell'editore su Google Books
    non coincide sempre con quello che usi tu ("Delos Digital" vs "Delos Books"
    vs "Delos"). Al primo run restituiva zero risultati. Ora si provano piu'
    varianti del nome e si tiene tutto quello che esce.
    """
    trovati = []
    editore = source["publisher"]
    visti = set()

    # Varianti del nome: completo, prima parola, e alias configurabili.
    varianti = [editore]
    prima = editore.split()[0]
    if prima != editore:
        varianti.append(prima)
    varianti.extend(source.get("alias", []))

    for nome in varianti:
        dati = get_json(GOOGLE_BOOKS, {
            "q": f'inpublisher:"{nome}"',
            "langRestrict": "it",
            "orderBy": "newest",
            "maxResults": 40,
            "printType": "books",
        })
        time.sleep(0.5)

        if not isinstance(dati, dict) or not dati.get("items"):
            continue

        for v in dati["items"]:
            info = v.get("volumeInfo", {})
            titolo = info.get("title", "")
            if not titolo or titolo in visti:
                continue
            visti.add(titolo)

            data_pub = info.get("publishedDate", "")
            try:
                anno = int(data_pub[:4])
                if anno < dal.year:
                    continue
            except (ValueError, IndexError):
                continue

            isbn = ""
            for ident in info.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_13":
                    isbn = ident.get("identifier", "")
                    break

            img = (info.get("imageLinks") or {}).get("thumbnail", "")
            trovati.append({
                "titolo": titolo,
                "autore": ", ".join(info.get("authors", [])),
                "editore": editore,  # sempre il nome canonico, non la variante
                "url": info.get("infoLink", ""),
                "sinossi": info.get("description", ""),
                "paratesto": "",
                "isbn": isbn,
                "copertina": (img.replace("http://", "https://")
                                 .replace("&zoom=1", "&zoom=2")
                                 .replace("&edge=curl", "")),
                "data": data_pub,
                "categorie": info.get("categories", []),
                "_fonte": "google_books",
            })

    return trovati


ADAPTERS = {
    "wordpress": adapter_wordpress,
    "woocommerce": adapter_wordpress,  # alias: e' lo stesso adapter generico
    "shopify": adapter_shopify,
    "rss": adapter_rss,
    "google_books": adapter_google_books,
    "google_books_autori": adapter_google_books_autori,
}


# --------------------------------------------------------------------------
# Arricchimento: ISBN e copertina da Google Books
# --------------------------------------------------------------------------

def somiglianza(a, b):
    """
    Quanto si somigliano due titoli normalizzati (0.0 - 1.0).
    Serve perche' Google Books scrive i titoli in modo leggermente diverso
    dagli editori: sottotitoli, articoli, due punti. Un confronto esatto
    scarta match validi.
    """
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def arricchisci(cand):
    """
    I feed degli editori non danno ISBN, e non sempre danno la copertina.
    Google Books li ha.

    La verifica anti-omonimo confronta i titoli con tolleranza: la prima
    versione esigeva che i primi 20 caratteri normalizzati coincidessero, e
    scartava match buoni per un sottotitolo di differenza — lasciando i
    candidati senza ISBN NE' copertina.
    """
    if cand.get("isbn") and cand.get("copertina"):
        return cand

    query = f'intitle:"{cand["titolo"]}"'
    if cand.get("autore"):
        query += f' inauthor:"{cand["autore"]}"'

    dati = get_json(GOOGLE_BOOKS, {
        "q": query,
        "langRestrict": "it",
        "maxResults": 5,
    })
    if not isinstance(dati, dict):
        return cand

    mio = chiave_titolo(cand["titolo"])

    for v in dati.get("items", []):
        info = v.get("volumeInfo", {})
        suo = chiave_titolo(info.get("title", ""))

        # Accetta se i titoli si somigliano abbastanza, oppure se uno contiene
        # l'altro (caso tipico: "Namirya" vs "Namirya. L'enigma degli Elfi").
        if not (somiglianza(mio, suo) > 0.75 or mio in suo or suo in mio):
            continue

        if not cand.get("isbn"):
            for ident in info.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_13":
                    cand["isbn"] = ident.get("identifier", "")
                    break
        if not cand.get("autore") and info.get("authors"):
            cand["autore"] = ", ".join(info["authors"])
        if not cand.get("copertina"):
            img = (info.get("imageLinks") or {}).get("thumbnail", "")
            # Google serve le thumbnail in http e piccole: https e zoom maggiore.
            cand["copertina"] = (img.replace("http://", "https://")
                                    .replace("&zoom=1", "&zoom=2")
                                    .replace("&edge=curl", ""))
        if not cand.get("sinossi") and info.get("description"):
            cand["sinossi"] = info["description"]
        break

    return cand


def cerca_amazon(cand):
    """
    Compone un link di ricerca Amazon.it a partire da ISBN o titolo+autore.

    Non e' il link diretto al prodotto — quello richiederebbe di interrogare
    Amazon, che non ha un'API aperta e blocca lo scraping. E' una ricerca
    precompilata: apri il link e il libro e' li' in cima. In moderazione puoi
    sostituirlo con l'URL definitivo in due click.
    """
    from urllib.parse import quote_plus

    if cand.get("isbn"):
        termine = cand["isbn"]
    else:
        termine = f"{cand.get('titolo', '')} {cand.get('autore', '')}".strip()
    if not termine:
        return ""
    return f"https://www.amazon.it/s?k={quote_plus(termine)}"


def segnale_esordio_dal_paratesto(cand):
    """
    Il testo dell'editore dice quasi sempre se l'autore e' al debutto.
    Quando lo dice, e' un segnale piu' forte del conteggio su Google Books
    (che gli omonimi inquinano) — e ci risparmia una chiamata di rete.

    Restituisce True (esordio), False (non esordio), o None (non si capisce).
    """
    testo = " ".join([
        cand.get("paratesto", ""),
        cand.get("sinossi", "")[:500],
    ]).lower()

    NON_ESORDIO = [
        "già autore", "gia' autore", "dopo il successo", "torna con",
        "torna in libreria", "autore di numerosi", "ha pubblicato",
        "dopo la trilogia", "dopo la dilogia", "dopo la saga",
        "nuovo romanzo di", "il suo secondo", "il suo terzo",
    ]
    ESORDIO = [
        "romanzo d'esordio", "romanzo di esordio", "esordio narrativo",
        "esordisce", "primo romanzo", "opera prima", "debutto narrativo",
        "il suo debutto",
    ]

    if any(x in testo for x in NON_ESORDIO):
        return False
    if any(x in testo for x in ESORDIO):
        return True
    return None


# --------------------------------------------------------------------------
# Prefiltro
#
# Il prefiltro esiste per non annegare nel catalogo di Giunti (2000+ prodotti,
# di cui forse cinque sono fantasy italiani). La prima versione cercava parole
# come "magic" e faceva passare "Albo magico", "pennarelli magici", "Mondi
# magici": il fantasy per bambini di 4 anni non e' il fantasy di questo catalogo.
#
# La logica ora e' a due stadi:
#   1. ESCLUSIONE — se il prodotto ha i segni di NON essere un romanzo per
#      adulti/YA (album, sticker, cartonato, manga, Disney...), esce subito.
#   2. INCLUSIONE — dopo l'esclusione, deve comunque mostrare un indizio
#      di fantasy per passare al modello.
# L'esclusione viene prima perche' e' molto piu' affidabile: "sticker" nel
# titolo e' una prova quasi certa, "magico" non prova nulla.
# --------------------------------------------------------------------------

# Se una di queste compare nel titolo, NON e' un romanzo. Punto.
# Sono i falsi positivi visti nel primo run su Giunti.
ESCLUSIONI_TITOLO = [
    # Libri-attivita' e cartoleria
    "albo magico", "album", "sticker", "staccattacca", "da colorare",
    "colouring", "coloring", "colora", "pennarelli", "glitter",
    "attività", "giochi", "enigmistica", "quiz", "labirinti", "cornicette",
    "libro bagno", "sagomine", "puzzle", "leporello", "cartonato",
    "librottini", "libriccini", "mini libri", "party pack", "super collection",
    "calendario", "avvento", "tarocchi", "segnalibro", "poster", "mappamondo",
    # Prescolare / didattica
    "imparo", "vado in prima", "il mio primo", "primo libro", "prescolare",
    "impara", "stampatello", "grafismi",
    # Fumetti e periodici
    "vol.", "graphic tales", "graphic novel", "fumetti", "manga",
    "art e dossier", "n. ", "rivista",
    # Libri per bambini, primi lettori, libro-gioco
    "primi lettori", "prime letture", "libro-gioco", "libro gioco",
    "librogame", "libro game", "cerca e trova", "aguzza la vista",
    "leggo e imparo", "filastrocche", "ninna nanna",
    # Merchandising e bundle (gia' in NON_LIBRI, ripetuti per sicurezza)
    "quiz box", "cofanetto", "gift", "collana ",
]

# Fasce d'eta' esplicite: quasi sempre libri per bambini.
ETA_BAMBINI = [
    "dai 3 anni", "dai 4 anni", "dai 5 anni", "dai 6 anni", "dai 7 anni",
    "3-5 anni", "4-6 anni", "5-7 anni", "6-8 anni", "età prescolare",
    "0-3 anni", "1-3 anni", "2-4 anni", "primissima infanzia",
]

# Franchise e marchi che non producono narrativa fantasy italiana d'autore.
ESCLUSIONI_BRAND = [
    "disney", "marvel", "pixar", "barbie", "hot wheels", "stitch", "dumbo",
    "toy story", "zootropolis", "oceania", "minions", "w.i.t.c.h.",
    "cenerentola", "aladdin", "bella addormentata", "cappuccetto",
    "principesse", "unicorni", "gabby", "nebulous stars", "mini cuccioli",
    "babbo natale", "natale",
]

# Dopo l'esclusione, serve almeno un indizio POSITIVO di fantasy.
# Niente "magic" nudo: troppo permissivo. Solo termini che nel paratesto
# editoriale indicano davvero il genere.
INDIZI_FANTASY = [
    "fantasy", "romantasy", "fantastico", "dark fantasy", "urban fantasy",
    "epic fantasy", "high fantasy", "grimdark", "sword and sorcery",
    "worldbuilding", "sistema magico", "magia", "stregoneria", "incantesim",
    "creature magiche", "soprannaturale", "sovrannaturale",
    "elfi", "elfico", "draghi", "necromant", "vampir", "licantrop",
    "regno", "profezia", "arcano", "grimorio",
]


def passa_prefiltro(cand, obbligatorio):
    titolo_low = cand.get("titolo", "").lower()

    # Stadio 0: bundle, gadget, abbonamenti (vale per tutti gli editori)
    if any(x in titolo_low for x in NON_LIBRI):
        return False, "non e' un romanzo"

    if not obbligatorio:
        return True, ""

    # --- Da qui in poi solo gli editori generalisti (Giunti, Sperling) ---

    # Testo esteso per i controlli su eta' e contenuto
    testo_esteso = " ".join([
        titolo_low,
        cand.get("sinossi", "")[:500],
        " ".join(str(c) for c in cand.get("categorie", [])),
    ]).lower()

    # Stadio 1: esclusione. Piu' affidabile dell'inclusione.
    if any(x in titolo_low for x in ESCLUSIONI_TITOLO):
        return False, "non e' un romanzo"
    if any(x in titolo_low for x in ESCLUSIONI_BRAND):
        return False, "non e' un romanzo"
    if any(x in testo_esteso for x in ETA_BAMBINI):
        return False, "non e' un romanzo"

    # I titoli tutti maiuscoli su Shopify sono quasi sempre manga o periodici.
    # I titoli con '::' sono sottotitoli commerciali ("::Con 7 storie", "::In
    # maiuscolo"), tipici dei prodotti per l'infanzia.
    if "::" in cand.get("titolo", ""):
        return False, "non e' un romanzo"

    # Stadio 2: inclusione. Serve un indizio positivo di fantasy.
    testo = " ".join([
        cand.get("titolo", ""),
        cand.get("sinossi", "")[:1000],
        cand.get("paratesto", "")[:600],
        " ".join(str(c) for c in cand.get("categorie", [])),
    ]).lower()

    if not any(p in testo for p in INDIZI_FANTASY):
        return False, "prefiltro: nessun indizio di fantasy"

    # Stadio 3: una sinossi troppo corta e' segno di prodotto non narrativo.
    # I romanzi hanno quarte di copertina; i libri-gioco no.
    if len(cand.get("sinossi", "")) < 150:
        return False, "prefiltro: sinossi troppo breve per un romanzo"

    return True, ""


def autore_probabilmente_straniero(autore):
    """
    Euristica leggera per scartare le traduzioni prima di chiamare il modello.
    NON e' infallibile (esistono italiani con nomi esteri e viceversa), quindi
    e' volutamente prudente: segnala solo i casi abbastanza chiari, e la parola
    finale resta al classificatore.

    Restituisce True solo per nomi con marcatori stranieri evidenti.
    """
    if not autore:
        return False
    a = autore.lower()

    # Pattern anglosassoni tipici: iniziale puntata centrale (J. R. R.),
    # cognomi con doppia consonante finale rara in italiano, ecc.
    marcatori = [
        "j.", "k.", "w.", "th ", "ph", "ck", "sh", "oo", "ee", "wood",
        "smith", "jones", "brown", "wilson", "taylor", "williams",
    ]
    # Nomi di battesimo palesemente non italiani
    nomi_esteri = [
        "lucy", "sarah", "emily", "jennifer", "jessica", "ashley", "brandon",
        "jake", "ryan", "kyle", "dylan", "chloe", "megan", "hannah", "grace",
        "leigh", "jane", "james", "john", "william", "george", "charles",
    ]

    parole = a.replace(".", ". ").split()
    if any(n in parole for n in nomi_esteri):
        return True
    if any(m in a for m in ["wood", "smith", "jones", "brown", " j. ", " k. "]):
        return True
    return False


def genere_esplicito(cand):
    """
    Cerca una qualificazione di genere ESPLICITA nel paratesto.
    Questo e' il segnale forte: se l'editore scrive 'dark fantasy', non serve
    che il modello inferisca il genere dalla trama.
    """
    testo = " ".join([
        cand.get("paratesto", ""),
        cand.get("sinossi", "")[:800],
        " ".join(str(c) for c in cand.get("categorie", [])),
        cand.get("url", ""),  # gli URL Amazon contengono spesso il genere
    ]).lower()

    for g in GENERI_ESPLICITI:
        if g in testo:
            return g
    return None


# --------------------------------------------------------------------------
# Classificatore (GitHub Models)
# --------------------------------------------------------------------------

PROMPT = """Sei un bibliotecario specializzato in narrativa fantastica italiana.
Valuti se un libro appartiene al catalogo "Fantasy Italia — Nuove proposte".

CRITERI DI AMMISSIONE (tutti necessari):
1. È un ROMANZO (non saggio, manuale, antologia curata, guida, fumetto, cofanetto).
2. È FANTASY IN SENSO STRETTO: contiene un elemento magico o soprannaturale
   STRUTTURALE, non decorativo. Sono fantasy: high/epic fantasy, dark fantasy,
   urban fantasy, fantasy storico, romantasy e fantasy romance, science fantasy
   (fantascienza CON magia esplicita), horror soprannaturale con struttura fantasy.
   NON sono fantasy: fantascienza senza magia, thriller, giallo, romance senza
   elemento soprannaturale, realismo magico puramente letterario.
3. L'AUTORE è ITALIANO. Questo criterio è STRINGENTE:
   - Se l'autore ha un nome palesemente straniero (es. "Lucy Jane Wood",
     "Sarah J. Maas"), l'opera è una TRADUZIONE: SCARTA, anche se l'editore
     è italiano (Giunti, Sperling e altri pubblicano moltissime traduzioni).
   - Se il campo autore è VUOTO, "(ignoto)", o coincide col nome dell'editore,
     NON puoi confermare che l'autore sia italiano: SCARTA con confidenza bassa.
     Non dare il beneficio del dubbio: un catalogo di autori italiani non può
     includere libri di cui non si conosce l'autore.
   - Consideri italiano solo un autore con nome italiano credibile, o quando
     il testo dell'editore lo qualifica esplicitamente come autore italiano.

4. NON è un libro per bambini in età prescolare/primi lettori, un libro-gioco,
   un librogame, un albo illustrato, una raccolta di attività, un cartonato.
   Cerchiamo ROMANZI per un pubblico adulto o young adult. Segnali da scartare:
   "dai 5 anni", "dai 6 anni", "primi lettori", "libro-gioco", "librogame",
   "album", "attività", poche decine di pagine, linguaggio da catalogo per
   l'infanzia.

REGOLA DECISIVA — LA QUALIFICAZIONE ESPLICITA BATTE L'INFERENZA:
Se il testo dell'editore, il premio ricevuto o la scheda prodotto qualificano
esplicitamente l'opera come "fantasy" (o un suo sottogenere), quella qualificazione
PREVALE sulla tua lettura della trama. Un romanzo con insetti senzienti e nessuna
magia visibile, se l'editore lo chiama fantasy ed è premiato come "miglior romanzo
fantasy", È fantasy. Inferisci dalla trama SOLO quando nessuna qualificazione
esplicita è disponibile, e in quel caso segnala confidenza bassa.

ESORDIO:
Indica se è il primo romanzo pubblicato dall'autore. Indizi: il paratesto dice
"esordio", "primo romanzo", "debutto"; oppure NON dice "già autore di", "dopo il
successo di", "torna con". Il conteggio di opere precedenti è un indizio, non una
prova (gli omonimi inquinano). Se non hai elementi, usa null.

Rispondi SOLO con JSON valido, nessun preambolo, nessun markdown:
{
  "ammesso": true|false,
  "motivo": "una frase breve",
  "genere": "fantasy|dark fantasy|urban fantasy|romantasy|fantasy storico|epic fantasy|science fantasy|..." oppure null,
  "esordio": true|false|null,
  "confidenza": "alta|media|bassa"
}"""


class Quota:
    """
    Tiene traccia di come sta rispondendo il modello, e regola il ritmo.

    Il problema che risolve: con un backoff che raddoppia (20s, 40s, 80s) e
    quattro tentativi, un candidato che va sempre in 429 costa 140 secondi.
    Su ottanta candidati sono tre ore di attesa a vuoto.

    Qui la pausa e' condivisa tra tutti i candidati: se il modello e' sotto
    pressione rallentiamo TUTTI, invece di far ricominciare ognuno da capo.
    E se e' esaurito davvero, si smette di provare: meglio quaranta candidati
    classificati bene e quaranta da rivedere a mano, che tre ore di attesa.
    """
    def __init__(self):
        self.pausa = PAUSA_MODELLO_MIN
        self.esaurita = False
        self.consecutivi_429 = 0

    def prima_della_chiamata(self):
        if self.pausa > 0:
            time.sleep(self.pausa)

    def ok(self):
        self.consecutivi_429 = 0
        # Il modello risponde: allenta gradualmente
        if self.pausa > PAUSA_MODELLO_MIN:
            self.pausa = max(PAUSA_MODELLO_MIN, self.pausa - 1.0)

    def rate_limited(self, attesa_suggerita=None):
        self.consecutivi_429 += 1
        # Alza la pausa per tutti, non solo per questo candidato
        self.pausa = min(PAUSA_MODELLO_MAX, max(self.pausa * 2, 3.0))

        # Cinque 429 di fila: la quota giornaliera e' finita, inutile insistere.
        if self.consecutivi_429 >= 5:
            self.esaurita = True
            log("\n  ! Quota del modello esaurita. I candidati restanti passano")
            log("    in moderazione senza classificazione: li rivedi a mano,")
            log("    oppure rilanci lo Scout domani e li riprende.\n")
            return 0

        pausa = attesa_suggerita if attesa_suggerita else self.pausa
        return min(pausa, 30)  # mai oltre mezzo minuto per un singolo retry


def classifica(cand, token, quota, tentativi=2):
    """
    Chiama GitHub Models.

    Due tentativi, non quattro: se il modello e' sotto pressione, insistere sul
    singolo candidato non aiuta — meglio rallentare il ritmo generale (lo fa
    l'oggetto Quota) e andare avanti.
    """
    if not token or quota.esaurita:
        return {"ammesso": True,
                "motivo": "non classificato — da verificare a mano",
                "genere": genere_esplicito(cand), "esordio": None,
                "confidenza": "bassa"}

    opere = cand.get("_opere_autore")
    nota_naz = ""
    if cand.get("_dubbio_nazionalita"):
        nota_naz = ("\nNOTA: il nome dell'autore potrebbe sembrare straniero, ma "
                    "MOLTI autori italiani usano nomi o pseudonimi dal suono estero. "
                    "NON dedurre la nazionalità dal solo nome: usa il paratesto, "
                    "l'editore e la lingua. Scarta solo se e' chiaramente una traduzione.")
    scheda = f"""TITOLO: {cand.get('titolo', '')}
AUTORE: {cand.get('autore', '(ignoto)')}{nota_naz}
EDITORE: {cand.get('editore', '')}
CATEGORIE/TAG: {', '.join(str(c) for c in cand.get('categorie', []))[:300]}
GENERE ESPLICITO RILEVATO: {genere_esplicito(cand) or '(nessuno)'}
OPERE PRECEDENTI DELL'AUTORE (Google Books, indicativo): {opere if opere is not None else 'ignoto'}

PRESENTAZIONE DELL'EDITORE:
{cand.get('paratesto', '')[:1200]}

SINOSSI:
{cand.get('sinossi', '')[:1800]}"""

    for tentativo in range(1, tentativi + 1):
        quota.prima_della_chiamata()

        try:
            r = requests.post(
                GH_MODELS_URL,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={
                    "model": GH_MODEL,
                    "messages": [
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": scheda},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 300,
                },
                timeout=40,
            )

            if r.status_code == 429:
                suggerita = r.headers.get("retry-after")
                try:
                    suggerita = int(suggerita)
                except (TypeError, ValueError):
                    suggerita = None

                attesa = quota.rate_limited(suggerita)
                if quota.esaurita or tentativo == tentativi:
                    break
                log(f"      · rallento a {quota.pausa:.0f}s")
                time.sleep(attesa)
                continue

            if r.status_code != 200:
                log(f"      ! HTTP {r.status_code}")
                break

            testo = r.json()["choices"][0]["message"]["content"].strip()
            testo = re.sub(r"^```(?:json)?|```$", "", testo, flags=re.MULTILINE).strip()
            esito = json.loads(testo)
            quota.ok()

            if not esito.get("genere"):
                esito["genere"] = genere_esplicito(cand)
            return esito

        except (requests.RequestException, KeyError, ValueError):
            if tentativo == tentativi:
                break
            time.sleep(2)

    return {
        "ammesso": True,
        "motivo": "non classificato — da verificare a mano",
        "genere": genere_esplicito(cand),
        "esordio": None,
        "confidenza": "bassa",
    }


# --------------------------------------------------------------------------
# Programma principale
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dal", help="Data di partenza (YYYY-MM-DD). Default: 30 giorni fa.")
    ap.add_argument("--dry-run", action="store_true", help="Non scrive nulla.")
    ap.add_argument("--limite", type=int, default=120,
                    help="Tetto ai candidati classificati. Con la pausa anti-429 "
                         "ogni candidato costa ~8s: 80 sono circa 11 minuti.")
    args = ap.parse_args()

    if args.dal:
        dal = datetime.fromisoformat(args.dal).replace(tzinfo=timezone.utc)
    else:
        dal = datetime.now(timezone.utc) - timedelta(days=30)

    log(f"Scout — finestra dal {dal.date()}\n" + "=" * 60)

    # --- Catalogo esistente: serve per il dedup e per il segnale esordio ---
    catalogo = []
    if CATALOG_FILE.exists():
        catalogo = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))

    isbn_noti = {normalizza_isbn(b.get("isbn")) for b in catalogo}
    isbn_noti.discard(None)
    titoli_noti = {chiave_titolo(b.get("title", ""), b.get("author", "")) for b in catalogo}
    prefissi_noti = {chiave_solo_titolo(b.get("title", "")) for b in catalogo}
    prefissi_noti.discard("")
    autori_noti = {(b.get("author") or "").strip().lower() for b in catalogo}

    # Mappa editore -> autori gia' in catalogo. Serve all'adapter "per autore":
    # per gli editori senza API, si cercano su Google Books i libri degli autori
    # gia' noti per quell'editore (Arzani con Acheron -> trova i suoi titoli nuovi).
    autori_per_editore = {}
    for b in catalogo:
        ed = (b.get("publisher") or "").strip()
        au = (b.get("author") or "").strip()
        if ed and au:
            autori_per_editore.setdefault(ed, set()).add(au)

    log(f"Catalogo: {len(catalogo)} titoli, {len(isbn_noti)} ISBN validi\n")

    # --- Candidati gia' in attesa: non li riproponiamo ---
    esistenti = []
    if CANDIDATES_FILE.exists():
        try:
            esistenti = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
        except ValueError:
            esistenti = []
    for c in esistenti:
        i = normalizza_isbn(c.get("isbn"))
        if i:
            isbn_noti.add(i)
        titoli_noti.add(chiave_titolo(c.get("title", ""), c.get("author", "")))

    # --- Raccolta ---
    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    grezzi = []

    for src in config["sources"]:
        if not src.get("attivo", True):
            continue
        nome = src["publisher"]
        fn = ADAPTERS.get(src["adapter"])
        if not fn:
            log(f"  {nome}: adapter '{src['adapter']}' sconosciuto, salto")
            continue

        log(f"  {nome} [{src['adapter']}] ... ", )
        try:
            if src["adapter"] == "google_books_autori":
                autori_ed = autori_per_editore.get(nome, set())
                trovati = fn(src, dal, autori_ed)
            else:
                trovati = fn(src, dal)
        except Exception as e:
            log(f"      ! errore ({type(e).__name__}: {e}) — proseguo con gli altri")
            trovati = []

        # Un editore che fallisce non deve fermare gli altri tredici.
        for t in trovati:
            t["_prefiltro_obbligatorio"] = src.get("prefiltro_obbligatorio", False)
        grezzi.extend(trovati)
        log(f"      {len(trovati)} elementi")
        time.sleep(PAUSA)

    log(f"\nRaccolti {len(grezzi)} elementi grezzi.")

    # --- Dedup e prefiltro ---
    superstiti = []
    scartati = {"gia_in_catalogo": 0, "prefiltro": 0, "non_libri": 0}

    for g in grezzi:
        isbn = normalizza_isbn(g.get("isbn"))
        if isbn and isbn in isbn_noti:
            scartati["gia_in_catalogo"] += 1
            continue
        if chiave_titolo(g["titolo"], g.get("autore", "")) in titoli_noti:
            scartati["gia_in_catalogo"] += 1
            continue
        # Rete aggiuntiva: stesso titolo, sottotitolo diverso ('Namirya' vs
        # 'Namirya. L'enigma degli Elfi'). Confronto per contenimento.
        if titolo_gia_noto(g["titolo"], prefissi_noti):
            scartati["gia_in_catalogo"] += 1
            continue

        ok, motivo = passa_prefiltro(g, g.get("_prefiltro_obbligatorio", False))
        if not ok:
            scartati["non_libri" if "romanzo" in motivo else "prefiltro"] += 1
            continue

        superstiti.append(g)

    log(f"Dopo dedup e prefiltro: {len(superstiti)} da valutare")
    log(f"  scartati: {scartati['gia_in_catalogo']} già in catalogo, "
        f"{scartati['prefiltro']} fuori tema, {scartati['non_libri']} non romanzi")

    # PRIORITA' DELLE FONTI. Bug scoperto nel log: Giunti da solo produce 1000
    # elementi, per lo piu' spazzatura generalista, e satura il limite PRIMA che
    # i libri buoni degli editori specializzati vengano guardati. Risultato: 0
    # candidati validi, non perche' non ci sono, ma perche' troncati via.
    #
    # Soluzione: classifico PRIMA gli editori che pubblicano quasi solo fantasy
    # (dove quasi ogni candidato e' buono), e lascio i generalisti in coda. Cosi'
    # il limite protegge la quota senza sacrificare i libri che contano.
    PRIORITA = {
        "woocommerce": 0,           # Lumien, La Corte, Alcatraz... quasi solo fantasy
        "wordpress": 0,
        "google_books_autori": 1,   # fonti per autore: mirate, poche e buone
        "google_books": 1,
        "rss": 1,
        "shopify": 2,               # Giunti, ACS: generalisti, tanta spazzatura
    }
    superstiti.sort(key=lambda g: PRIORITA.get(g.get("_fonte", ""), 3))

    if len(superstiti) > args.limite:
        # Conta quanti buoni (priorita' 0) restano dentro e fuori il taglio,
        # cosi' nel log si vede se il limite sta tagliando roba importante.
        buoni_totali = sum(1 for g in superstiti if PRIORITA.get(g.get("_fonte",""),3)==0)
        buoni_dentro = sum(1 for g in superstiti[:args.limite] if PRIORITA.get(g.get("_fonte",""),3)==0)
        log(f"  Supero il limite di {args.limite}: classifico prima le fonti "
            f"specializzate ({buoni_dentro}/{buoni_totali} coperte).")
        if buoni_dentro < buoni_totali:
            log(f"  → {buoni_totali - buoni_dentro} candidati di qualità restano "
                f"fuori: rilancia con --limite più alto per recuperarli.")
        superstiti = superstiti[:args.limite]

    # --- Arricchimento e classificazione ---
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log("\n  ! GITHUB_TOKEN assente: i candidati passeranno senza classificazione.\n")

    nuovi = []
    quota = Quota()
    log(f"\nClassifico {len(superstiti)} candidati...")
    inizio = time.time()

    for i, c in enumerate(superstiti, 1):
        log(f"  [{i}/{len(superstiti)}] {c['titolo'][:55]}")

        # --- Arricchimento: solo se serve davvero ---
        # Un candidato che ha gia' copertina e ISBN non ha bisogno di Google Books.
        # (Nella versione precedente il giro si faceva sempre: 80 chiamate inutili.)
        if not (c.get("isbn") and c.get("copertina")):
            c = arricchisci(c)
            time.sleep(0.2)

        # Secondo dedup: ora che l'arricchimento ha (forse) trovato l'ISBN,
        # ricontrolliamo. Il primo dedup girava prima, quando l'ISBN mancava e
        # solo il titolo poteva collidere: e' qui che si prendono i doppioni
        # sfuggiti, tipici dei candidati arrivati senza ISBN dai feed.
        isbn_arricchito = normalizza_isbn(c.get("isbn"))
        if isbn_arricchito and isbn_arricchito in isbn_noti:
            log(f"        ✗ già in catalogo (ISBN {isbn_arricchito})")
            continue

        # --- Esordio: prima il paratesto, la rete solo se serve ---
        # Il testo dell'editore ("gia' autore di...", "romanzo d'esordio") e' piu'
        # affidabile del conteggio su Google Books, e costa zero.
        autore_low = (c.get("autore") or "").strip().lower()

        if autore_low and autore_low in autori_noti:
            # Gia' in catalogo con un altro libro: non e' un esordiente.
            c["_opere_autore"] = "già presente nel tuo catalogo (non è un esordio)"
        else:
            segnale = segnale_esordio_dal_paratesto(c)
            if segnale is True:
                c["_opere_autore"] = "il paratesto dichiara un ESORDIO"
            elif segnale is False:
                c["_opere_autore"] = "il paratesto dichiara opere precedenti"
            else:
                c["_opere_autore"] = "ignoto"

        # Nome dell'autore potenzialmente straniero? NON scarto piu' in automatico:
        # "Grace D." e' italiana nonostante il nome, e un filtro che scarta da solo
        # perdeva libri buoni. Passo il dubbio al classificatore, che leggendo il
        # paratesto (in italiano, con "l'autrice italiana...") decide meglio di me.
        if autore_probabilmente_straniero(c.get("autore", "")):
            c["_dubbio_nazionalita"] = True

        esito = classifica(c, token, quota)

        if not esito.get("ammesso"):
            log(f"        ✗ {esito.get('motivo', '')[:60]}")
            continue

        anno = datetime.now().year
        m = re.search(r"(20\d{2})", str(c.get("data", "")))
        if m:
            anno = int(m.group(1))

        nuovi.append({
            "id": "c" + str(int(time.time() * 1000))[-10:] + str(i),
            "title": c["titolo"],
            "author": c.get("autore", ""),
            "publisher": c["editore"],
            "year": anno,
            "series": "",
            "isbn": c.get("isbn", ""),
            "coverUrl": c.get("copertina", ""),
            "description": (c.get("sinossi") or "")[:2000],
            "storeAmazonUrl": cerca_amazon(c),
            "storePublisherUrl": c.get("url", ""),
            "isDebut": esito.get("esordio"),
            "genre": esito.get("genere"),
            "labels": [],
            "status": "pending",
            "featured": False,
            "_auto": True,
            "_source": c.get("_fonte", ""),
            "_confidence": esito.get("confidenza", "bassa"),
            "_reason": esito.get("motivo", ""),
            "ts": int(time.time() * 1000),
        })
        log(f"        ✓ {esito.get('genere') or 'fantasy'} · "
            f"esordio={esito.get('esordio')} · conf={esito.get('confidenza')}")

    durata = int(time.time() - inizio)
    log(f"\n  (classificazione: {durata//60}m {durata%60}s)")

    # --- Scrittura ---
    log("\n" + "=" * 60)
    log(f"Nuovi candidati: {len(nuovi)}")

    if args.dry_run:
        log("(dry-run: non scrivo nulla)")
        print(json.dumps(nuovi, ensure_ascii=False, indent=2)[:3000])
        return

    if not nuovi:
        log("Niente da aggiungere.")
        return

    tutti = esistenti + nuovi
    CANDIDATES_FILE.write_text(
        json.dumps(tutti, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"Scritto {CANDIDATES_FILE.relative_to(ROOT)} — {len(tutti)} candidati in attesa.")

    # Riepilogo per il messaggio di commit
    da_verificare = sum(1 for n in nuovi if n["_confidence"] == "bassa")
    if da_verificare:
        log(f"  {da_verificare} con confidenza bassa: guardali con attenzione.")


if __name__ == "__main__":
    main()
