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
PAUSA_MODELLO = 6.0  # GitHub Models free tier: ~10 richieste/minuto
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


def get_json(url, params=None):
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

def adapter_woocommerce(source, dal):
    """
    WooCommerce via REST API di WordPress.
    Copre: Lumien, La Corte, La Nuova Carne, Alcatraz, Parallelo45, Angolazioni.
    Restituisce i prodotti pubblicati dopo `dal`.
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

            trovati.append({
                "titolo": titolo,
                "autore": autore,
                "editore": source["publisher"],
                "url": p.get("link", ""),
                "sinossi": contenuto or excerpt,
                "paratesto": excerpt,  # separato: e' la voce dell'editore
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

    while pagina <= 10:  # Giunti e' grosso: fino a 2500 prodotti
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
                        continue
                except ValueError:
                    pass

            trovati.append({
                "titolo": p.get("title", ""),
                "autore": p.get("vendor", ""),  # su Shopify l'autore sta spesso qui
                "editore": source["publisher"],
                "url": f"{source['endpoint'].replace('/products.json', '')}/products/{p.get('handle', '')}",
                "sinossi": strip_html(p.get("body_html", "")),
                "paratesto": "",
                "data": pubblicato,
                "categorie": p.get("tags", []) + [p.get("product_type", "")],
                "_fonte": "shopify",
            })

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


def adapter_google_books(source, dal):
    """
    Per gli editori senza API: si interroga Google Books per editore.
    Copre: Delos Digital, PresentARTsi, piu' i fallback.
    """
    trovati = []
    editore = source["publisher"]

    dati = get_json(GOOGLE_BOOKS, {
        "q": f'inpublisher:"{editore}"',
        "langRestrict": "it",
        "orderBy": "newest",
        "maxResults": 40,
        "printType": "books",
    })
    if not isinstance(dati, dict):
        return []

    for v in dati.get("items", []):
        info = v.get("volumeInfo", {})
        data_pub = info.get("publishedDate", "")

        # Google Books da' date in formati diversi: 2025, 2025-06, 2025-06-01
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

        trovati.append({
            "titolo": info.get("title", ""),
            "autore": ", ".join(info.get("authors", [])),
            "editore": editore,
            "url": info.get("infoLink", ""),
            "sinossi": info.get("description", ""),
            "paratesto": "",
            "isbn": isbn,
            "copertina": (info.get("imageLinks") or {}).get("thumbnail", ""),
            "data": data_pub,
            "categorie": info.get("categories", []),
            "_fonte": "google_books",
        })

    return trovati


ADAPTERS = {
    "woocommerce": adapter_woocommerce,
    "shopify": adapter_shopify,
    "rss": adapter_rss,
    "google_books": adapter_google_books,
}


# --------------------------------------------------------------------------
# Arricchimento: ISBN e copertina da Google Books
# --------------------------------------------------------------------------

def arricchisci(cand):
    """
    I feed degli editori quasi mai danno ISBN e copertina in formato utile.
    Google Books li ha. Cerchiamo per titolo + autore.
    """
    if cand.get("isbn") and cand.get("copertina"):
        return cand

    query = f'intitle:"{cand["titolo"]}"'
    if cand.get("autore"):
        query += f' inauthor:"{cand["autore"]}"'

    dati = get_json(GOOGLE_BOOKS, {
        "q": query,
        "langRestrict": "it",
        "maxResults": 3,
    })
    if not isinstance(dati, dict):
        return cand

    for v in dati.get("items", []):
        info = v.get("volumeInfo", {})
        # Verifica che sia davvero lo stesso libro, non un omonimo
        if chiave_titolo(info.get("title", ""))[:20] != chiave_titolo(cand["titolo"])[:20]:
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
            # Google serve le thumbnail in http e piccole: le portiamo a https e grandi
            cand["copertina"] = img.replace("http://", "https://").replace("&zoom=1", "&zoom=2")
        if not cand.get("sinossi") and info.get("description"):
            cand["sinossi"] = info["description"]
        break

    return cand


def conta_opere_precedenti(autore):
    """
    Segnale per il tag esordiente: quante opere ha gia' pubblicato questo autore?
    Zero o una (quella corrente) = probabile esordio.
    E' un indizio, non una prova: il classificatore lo pesa insieme al paratesto.
    """
    if not autore:
        return None
    dati = get_json(GOOGLE_BOOKS, {
        "q": f'inauthor:"{autore}"',
        "langRestrict": "it",
        "maxResults": 20,
    })
    if not isinstance(dati, dict):
        return None
    return dati.get("totalItems", 0)


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
    # Merchandising e bundle (gia' in NON_LIBRI, ripetuti per sicurezza)
    "quiz box", "cofanetto", "gift", "collana ",
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

    # Stadio 1: esclusione. Piu' affidabile dell'inclusione.
    if any(x in titolo_low for x in ESCLUSIONI_TITOLO):
        return False, "non e' un romanzo"
    if any(x in titolo_low for x in ESCLUSIONI_BRAND):
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
3. L'AUTORE è ITALIANO (nome italiano, editore italiano, testo originale in italiano;
   non una traduzione).

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


def classifica(cand, token, tentativi=4):
    """
    Chiama GitHub Models.

    Il free tier ha un limite stretto di richieste al minuto: al primo run
    il 429 e' arrivato dopo ~15 chiamate e da li' in poi TUTTI i candidati
    sono passati senza classificazione (91 su 97 con confidenza bassa).
    Il fallback funzionava, ma il risultato era inutilizzabile.

    Ora: quando arriva un 429 aspetta e riprova, con attesa crescente.
    Se dopo tutti i tentativi il modello non risponde, il candidato passa
    comunque in revisione manuale — un errore di rete non deve farti perdere
    un libro — ma questo diventa l'eccezione, non la regola.
    """
    if not token:
        return {"ammesso": True, "motivo": "nessun token: classificazione saltata",
                "genere": genere_esplicito(cand), "esordio": None, "confidenza": "bassa"}

    opere = cand.get("_opere_autore")
    scheda = f"""TITOLO: {cand.get('titolo', '')}
AUTORE: {cand.get('autore', '(ignoto)')}
EDITORE: {cand.get('editore', '')}
CATEGORIE/TAG: {', '.join(str(c) for c in cand.get('categorie', []))[:300]}
GENERE ESPLICITO RILEVATO: {genere_esplicito(cand) or '(nessuno)'}
OPERE PRECEDENTI DELL'AUTORE (Google Books, indicativo): {opere if opere is not None else 'ignoto'}

PRESENTAZIONE DELL'EDITORE:
{cand.get('paratesto', '')[:1200]}

SINOSSI:
{cand.get('sinossi', '')[:1800]}"""

    attesa = 20  # secondi; raddoppia a ogni 429

    for tentativo in range(1, tentativi + 1):
        try:
            r = requests.post(
                GH_MODELS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GH_MODEL,
                    "messages": [
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": scheda},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 300,
                },
                timeout=60,
            )

            if r.status_code == 429:
                # Il server dice spesso quanto aspettare: ascoltiamolo.
                suggerita = r.headers.get("retry-after") or r.headers.get("x-ratelimit-timeremaining")
                try:
                    pausa = int(suggerita) + 2
                except (TypeError, ValueError):
                    pausa = attesa

                if tentativo == tentativi:
                    log(f"      ! quota esaurita dopo {tentativi} tentativi")
                    break

                log(f"      · limite raggiunto, attendo {pausa}s "
                    f"(tentativo {tentativo}/{tentativi})")
                time.sleep(pausa)
                attesa *= 2
                continue

            if r.status_code != 200:
                log(f"      ! modello HTTP {r.status_code}")
                break

            testo = r.json()["choices"][0]["message"]["content"].strip()
            testo = re.sub(r"^```(?:json)?|```$", "", testo, flags=re.MULTILINE).strip()
            esito = json.loads(testo)

            # Rete di sicurezza: se il paratesto dichiara un genere e il modello
            # non ne ha trovato uno, ci fidiamo dell'editore.
            if not esito.get("genere"):
                esito["genere"] = genere_esplicito(cand)

            return esito

        except (requests.RequestException, KeyError, ValueError) as e:
            log(f"      ! errore {type(e).__name__}")
            if tentativo == tentativi:
                break
            time.sleep(attesa)
            attesa *= 2

    # Tutti i tentativi falliti: passa in revisione manuale.
    log("      ! classificazione non riuscita: passa in revisione manuale")
    return {
        "ammesso": True,
        "motivo": "CLASSIFICAZIONE NON RIUSCITA — da verificare a mano",
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
    ap.add_argument("--limite", type=int, default=80,
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
    autori_noti = {(b.get("author") or "").strip().lower() for b in catalogo}

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

        ok, motivo = passa_prefiltro(g, g.get("_prefiltro_obbligatorio", False))
        if not ok:
            scartati["non_libri" if "romanzo" in motivo else "prefiltro"] += 1
            continue

        superstiti.append(g)

    log(f"Dopo dedup e prefiltro: {len(superstiti)} da valutare")
    log(f"  scartati: {scartati['gia_in_catalogo']} già in catalogo, "
        f"{scartati['prefiltro']} fuori tema, {scartati['non_libri']} non romanzi")

    if len(superstiti) > args.limite:
        log(f"  ATTENZIONE: supero il limite di {args.limite}, tronco.")
        superstiti = superstiti[:args.limite]

    # --- Arricchimento e classificazione ---
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log("\n  ! GITHUB_TOKEN assente: i candidati passeranno senza classificazione.\n")

    nuovi = []
    log(f"\nClassifico {len(superstiti)} candidati...")

    for i, c in enumerate(superstiti, 1):
        log(f"  [{i}/{len(superstiti)}] {c['titolo'][:55]}")

        c = arricchisci(c)
        time.sleep(0.3)

        # Se l'autore e' gia' in catalogo, non e' un esordiente: segnale forte.
        autore_low = (c.get("autore") or "").strip().lower()
        if autore_low and autore_low in autori_noti:
            c["_opere_autore"] = "già presente nel tuo catalogo"
        else:
            c["_opere_autore"] = conta_opere_precedenti(c.get("autore", ""))
            time.sleep(0.3)

        esito = classifica(c, token)

        # Pausa tra una classificazione e l'altra: il free tier di GitHub Models
        # ha un limite di richieste al minuto. Meglio andare piano e classificare
        # bene 60 candidati che correre e non classificarne nessuno.
        if token:
            time.sleep(PAUSA_MODELLO)

        if not esito.get("ammesso"):
            log(f"        ✗ {esito.get('motivo', '')[:60]}")
            continue

        # Data di uscita -> anno
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
            "storeAmazonUrl": "",
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
