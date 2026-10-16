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
# Gli scarti del classificatore, per non rivalutarli a ogni giro (vedi
# leggi_scarti). Dopo 400 giorni una voce non serve piu': la finestra delle
# fonti e' di 30 giorni, quella di Google Books dell'anno in corso.
SCARTI_FILE = ROOT / "data" / "scout-scarti.json"
SCARTI_GIORNI = 400
# Quanto aspettare prima di riprovare i candidati rimasti senza verdetto: il
# piano gratuito di Gemini conta le richieste al minuto.
PAUSA_RIPASSO = 20

TIMEOUT = 25
PAUSA = 1.0  # cortesia verso i server degli editori

# Pausa tra chiamate al modello. Parte da ZERO: il free tier regge una raffica
# iniziale, e farla aspettare a vuoto e' tempo buttato. Si alza da sola al primo
# 429 e si riabbassa quando il modello torna a rispondere.
# (La versione a pausa fissa di 6s faceva durare il run un'ora e mezza.)
PAUSA_MODELLO_MIN = 0.0
PAUSA_MODELLO_MAX = 12.0
# Quanto aspettare una risposta di Gemini. Quando risponde lo fa in pochi
# secondi; il 12 ottobre 2026 non rispondeva affatto, e con 60 secondi di
# attesa per due tentativi ogni candidato costava due minuti a vuoto.
GEMINI_TIMEOUT = 30
# Dopo tanti candidati di fila senza verdetto il classificatore si considera
# fuori servizio e si smette di chiamarlo: quel giorno 46 candidati su 46
# sono rimasti senza risposta, e il giro e' durato 22 minuti per niente.
MAX_MUTI_DI_FILA = 4
# Quante volte, per un candidato, si aspetta che passi il limite al minuto
# prima di arrendersi. Il piano gratuito concede 5 richieste al minuto per
# modello: un'attesa di solito basta.
MAX_ATTESE_MINUTO = 3

UA = {"User-Agent": "FantasyItaliaBot/1.0 (+https://www.fantasyitalianuoveproposte.it)"}

GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"

# Classificatore: Google Gemini, piano gratuito, chiave in GEMINI_API_KEY.
# Fino a luglio 2026 era GitHub Models, che GitHub ha chiuso il 30 luglio:
# da quel giorno ogni chiamata rispondeva 410.
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Modelli in ordine di preferenza. All'avvio si chiede a Google quali sono
# disponibili per questa chiave e si usa il primo: se uno viene ritirato si
# passa al successivo senza toccare il codice. Tutti del piano gratuito.
# GEMINI_MODEL, se impostata, scavalca la lista.
GEMINI_PREFERITI = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

# Il modello "pensa" prima di rispondere, e quei pensieri contano nel limite
# di lunghezza della risposta: con un limite stretto il JSON arriverebbe
# troncato o vuoto. Il piano e' gratuito, quindi si lascia spazio.
GEMINI_MAX_USCITA = 4096

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
    # Varianti dello stesso libro vendute come prodotti a parte: la copia
    # firmata o col taglio colorato non e' un titolo nuovo. Arrivavano in
    # moderazione come "Hyperversum - COPIA AUTOGRAFATA".
    "copia autografata", "copia firmata", "autografato", "sprayed edges",
    "edizione con dedica",
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

# --------------------------------------------------------------------------
# Supabase
#
# Lo Scout usa la service_role key, che ignora le regole di sicurezza del
# database. E' voluto: e' un processo automatico che deve poter leggere anche
# gli scarti (per non riproporli) e scrivere i candidati. Proprio per questo
# la chiave sta solo nei secrets di GitHub e non deve finire altrove.
# --------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def supa_headers(extra=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def supa_attivo():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def supa_leggi_libri():
    """
    Scarica TUTTI i libri, in qualunque stato.

    Gli scarti servono quanto i libri in catalogo: un titolo che hai gia'
    rifiutato non deve ricomparire settimana dopo settimana. Prima il dedup
    guardava solo catalogo.json e questo non poteva saperlo.
    """
    libri, pagina, per_pagina = [], 0, 1000
    while True:
        try:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/books",
                headers=supa_headers({
                    "Range": f"{pagina*per_pagina}-{(pagina+1)*per_pagina-1}"
                }),
                params={"select": "id,title,author,publisher,isbn,status,store_publisher_url"},
                timeout=TIMEOUT,
            )
            if r.status_code not in (200, 206):
                log(f"  ! Supabase ha risposto {r.status_code}: {r.text[:200]}")
                break
            blocco = r.json()
            libri.extend(blocco)
            if len(blocco) < per_pagina:
                break
            pagina += 1
        except (requests.RequestException, ValueError) as e:
            log(f"  ! Non riesco a leggere il catalogo da Supabase: {e}")
            break
    return libri


def supa_scrivi_candidati(righe):
    """
    Inserisce i candidati. 'merge-duplicates' fa sì che un id gia' presente
    venga aggiornato invece di provocare un errore: cosi' rilanciare lo Scout
    non crea doppioni.
    """
    if not righe:
        return True
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/books",
            headers=supa_headers({
                "Prefer": "resolution=merge-duplicates,return=minimal"
            }),
            json=righe,
            timeout=60,
        )
        if r.status_code not in (200, 201, 204):
            log(f"  ! Scrittura fallita ({r.status_code}): {r.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"  ! Scrittura fallita: {e}")
        return False


def supa_registra_esecuzione(dati):
    """Lascia traccia dell'esecuzione: storico utile, e tiene vivo il progetto."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/scout_runs",
            headers=supa_headers({"Prefer": "return=minimal"}),
            json=dati,
            timeout=30,
        )
    except requests.RequestException:
        pass  # lo storico e' un di piu': se fallisce, non blocca nulla


def supa_esporta_backup():
    """
    Riscrive data/catalogo.json con i libri approvati.

    Non serve piu' al sito, che ormai legge da Supabase. Serve come copia di
    sicurezza versionata: se il progetto Supabase venisse sospeso o cancellato,
    il catalogo sarebbe comunque qui, e basterebbe cambiare un indirizzo nel
    sito per tornare a leggerlo da GitHub.
    """
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/books",
            headers=supa_headers(),
            params={
                "select": "id,title,author,publisher,year,series,isbn,cover_url,"
                          "description,store_amazon_url,store_publisher_url,"
                          "is_debut,genre,labels,featured",
                "status": "eq.approved",
                "order": "year.desc",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log(f"  ! Backup non riuscito ({r.status_code})")
            return 0

        # Riscritto nel formato storico del sito (camelCase), così il file
        # resta utilizzabile senza conversioni in caso di emergenza.
        libri = []
        for b in r.json():
            libri.append({
                "id": b.get("id"),
                "title": b.get("title"),
                "author": b.get("author"),
                "publisher": b.get("publisher"),
                "year": b.get("year"),
                "series": b.get("series") or "",
                "isbn": b.get("isbn") or "",
                "coverUrl": b.get("cover_url") or "",
                "description": b.get("description") or "",
                "storeAmazonUrl": b.get("store_amazon_url") or "",
                "storePublisherUrl": b.get("store_publisher_url") or "",
                "isDebut": b.get("is_debut"),
                "genre": b.get("genre"),
                "labels": b.get("labels") or [],
                "featured": bool(b.get("featured")),
                "status": "approved",
            })

        CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_FILE.write_text(
            json.dumps(libri, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return len(libri)
    except (requests.RequestException, ValueError) as e:
        log(f"  ! Backup non riuscito: {e}")
        return 0


def get_json(url, params=None):
    # Aggancia la API key a ogni chiamata Google Books. Senza chiave la quota
    # anonima e' praticamente zero (429 "quota_limit_value: 0"): e' la ragione
    # per cui arricchimento e ricerca per autore fallivano a intermittenza.
    google = url.startswith(GOOGLE_BOOKS)
    if GOOGLE_BOOKS_KEY and google:
        params = dict(params or {})
        params["key"] = GOOGLE_BOOKS_KEY
    try:
        r = requests.get(url, headers=UA, params=params, timeout=TIMEOUT)
        # Google Books risponde spesso 503 ("Service temporarily unavailable")
        # e un attimo dopo funziona: il 12 ottobre 2026 sono fallite cosi' 45
        # chiamate. Una sola ripetizione, per non allungare il giro.
        if google and r.status_code in (500, 502, 503, 504):
            time.sleep(3)
            r = requests.get(url, headers=UA, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            if google:
                annota_errore_google(r.status_code, r.text)
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        if google:
            # Solo il tipo di errore: il messaggio di requests riporta
            # l'indirizzo completo, chiave compresa.
            annota_errore_google(type(e).__name__, "")
        return None


# Google Books che non risponde era invisibile: get_json restituiva None e la
# fonte risultava "0 elementi", come un editore che non ha pubblicato niente.
# Sei fonti a zero per due settimane sono passate cosi'. Qui si conta ogni
# chiamata fallita e si scrive nel registro il primo messaggio di ogni tipo.
ERRORI_GOOGLE = {}


def annota_errore_google(stato, corpo):
    primo = stato not in ERRORI_GOOGLE
    ERRORI_GOOGLE[stato] = ERRORI_GOOGLE.get(stato, 0) + 1
    if not primo:
        return
    messaggio = ""
    try:
        messaggio = (json.loads(corpo).get("error") or {}).get("message", "")
    except (ValueError, AttributeError):
        messaggio = strip_html(corpo or "")
    # La chiave non deve finire in un registro pubblico, nemmeno per sbaglio.
    if GOOGLE_BOOKS_KEY:
        messaggio = messaggio.replace(GOOGLE_BOOKS_KEY, "***")
    log(f"      ! Google Books non risponde ({stato}): {messaggio[:200] or 'nessun dettaglio'}")


def riepilogo_errori_google():
    if not ERRORI_GOOGLE:
        return
    dettaglio = ", ".join(f"{stato} × {n}" for stato, n in ERRORI_GOOGLE.items())
    log(f"  ! Google Books: {sum(ERRORI_GOOGLE.values())} chiamate fallite ({dettaglio}). "
        "Le fonti che passano da li' e l'arricchimento ne risentono.")


# --------------------------------------------------------------------------
# Memoria degli scarti del classificatore
# --------------------------------------------------------------------------

def leggi_scarti():
    """
    I titoli che il classificatore ha gia' scartato, per chiave.

    Senza questa memoria lo stesso albo per bambini di Giunti tornava ogni
    lunedi' finche' restava nella finestra dei 30 giorni: chiamate sprecate
    su un servizio gratuito e congestionato, e occasioni in piu' perche' un
    candidato restasse senza verdetto e facesse uscire il giro con errore.

    Sta in un file e non nel database perche' cosi' non riempie la lista
    "Scartati" della moderazione, e resta leggibile: per far rivalutare un
    titolo basta cancellarne la voce.
    """
    try:
        voci = json.loads(SCARTI_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    limite = (datetime.now(timezone.utc) - timedelta(days=SCARTI_GIORNI)).date().isoformat()
    return {v["chiave"]: v for v in voci
            if isinstance(v, dict) and v.get("chiave") and v.get("data", "") >= limite}


def scrivi_scarti(scarti):
    voci = sorted(scarti.values(), key=lambda v: (v.get("data", ""), v.get("titolo", "")))
    SCARTI_FILE.write_text(json.dumps(voci, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


# --------------------------------------------------------------------------
# Adapter: uno per piattaforma
# --------------------------------------------------------------------------

# Classi CSS di un prodotto WooCommerce che portano l'autore. I temi usano nomi
# diversi: tassonomia 'autore' (Lumien), attributo 'pa_autore' (La nuova carne),
# tassonomia 'product_author' (La Corte), 'authors' (Angolazioni). Non sempre
# sono esposte dalla REST API come campo, ma finiscono tutte in class_list.
CLASSI_AUTORE = ("autore-", "pa_autore-", "pa_author-", "product_author-",
                 "book_author-", "authors-")

# Le sole classi che dicono qualcosa del libro: collana, categorie, tag e gli
# altri attributi del prodotto (genere, formato). Il resto ('post-16174',
# 'instock', 'purchasable'...) riempiva i 300 caratteri che il prompt concede
# alle categorie prima di arrivare a quelle utili.
CLASSI_CATEGORIA = ("product_cat-", "product_tag-", "pa_")


def senza_suffisso(slug):
    # Il "-2" finale e' WordPress che distingue due slug uguali
    # ("parallelo45-2"): nel nome non significa niente. Si toglie solo se
    # resta qualcosa, per non svuotare valori come "2026".
    return re.sub(r"(.)-\d+$", r"\1", slug)


def leggi_class_list(classi):
    """
    Ricava da class_list le categorie leggibili e gli autori.

    WordPress la restituisce come lista, ma come oggetto {"0": ..., "1": ...}
    quando PHP ne ha tolto qualche voce (succede su La nuova carne): letta
    cosi' com'era, al classificatore arrivavano come categorie le chiavi
    "0 1 2 3".

    Dallo slug non si recuperano accenti ne' punti ("matthew-m-bartlett" diventa
    "Matthew M Bartlett"): meglio di un autore vuoto, che il classificatore e'
    tenuto a scartare, e in moderazione si corregge.
    """
    if isinstance(classi, dict):
        classi = list(classi.values())
    if not isinstance(classi, list):
        return [], []

    categorie, autori = [], []
    for c in classi:
        c = str(c)
        pref_autore = next((p for p in CLASSI_AUTORE if c.startswith(p)), None)
        if pref_autore:
            slug = senza_suffisso(c[len(pref_autore):])
            nome = " ".join(w.capitalize() for w in slug.split("-") if w)
            if nome and nome not in autori:
                autori.append(nome)
            continue
        pref_cat = next((p for p in CLASSI_CATEGORIA if c.startswith(p)), None)
        if pref_cat:
            voce = c[len(pref_cat):]
            # Negli attributi il nome dell'attributo resta come etichetta
            # ("pa_formato-cartaceo" -> "formato: cartaceo").
            if pref_cat == "pa_" and "-" in voce:
                nome_attr, valore = voce.split("-", 1)
                voce = f"{nome_attr}: {senza_suffisso(valore)}"
            else:
                voce = senza_suffisso(voce)
            voce = voce.replace("-", " ").strip()
            if voce and voce not in categorie:
                categorie.append(voce)
    return categorie, autori


def pulisci_titolo_prodotto(titolo):
    """
    Toglie dal titolo la dicitura di prevendita, che e' del negozio e non del
    libro: "Pre-Ordine – I peccati del sangue nero", "Pre-Order Luna Rossa",
    "Io sono Odio – Pre-ordine" (Winter Edizioni le usa tutte e tre). Lasciata
    li', finirebbe nella scheda proposta e il titolo non si riconoscerebbe piu'
    come gia' presente in catalogo.
    """
    titolo = re.sub(r"^\s*pre-?\s?ord(?:er|ine)\s*(?:[–—:-]\s*)?", "", titolo, flags=re.IGNORECASE)
    titolo = re.sub(r"\s*(?:[–—-]\s*|\()pre-?\s?ord(?:er|ine)\)?\s*$", "", titolo, flags=re.IGNORECASE)
    return titolo.strip()


# Le etichette con cui alcuni editori chiudono la descrizione del prodotto:
# "AUTORE: Laura Dudu GENERE: Dark Fantasy COLLANA: Lupus PAGINE: 646 ISBN: ..."
# Servono a capire dove finisce un valore: tolto l'HTML resta una riga sola.
ETICHETTE_SCHEDA = (r"uscita|autor[ei]|autrice|genere|sottogenere|collana|data di pubblicazione|"
                    r"pubblicazione|pagine|dimension[ei]|formato|copertina|illustrazion[ei]|"
                    r"edges|isbn|spedizione|prezzo|traduzione|traduttore|traduttrice")


def scheda_tecnica(testo):
    """
    Legge autore, genere e ISBN dalla scheda tecnica scritta nel testo.

    Dove c'e', e' il dato piu' affidabile: l'ha scritto l'editore, con accenti
    e punteggiatura, mentre dalle classi del prodotto si ricava solo uno slug.
    Il valore finisce alla prossima etichetta nota; il tetto di 80 caratteri
    evita che un "autore:" capitato dentro una frase si porti via un paragrafo.
    """
    trovati = {}
    for campo, etichetta in (("autore", r"autor[ei]|autrice"), ("genere", r"genere"),
                             ("sottogenere", r"sottogenere"), ("isbn", r"isbn"),
                             ("pubblicazione", r"data di pubblicazione|pubblicazione|uscita")):
        m = re.search(
            rf"\b(?:{etichetta})\s*:\s*(.{{1,80}}?)\s*(?=\b(?:{ETICHETTE_SCHEDA})\b[^:]{{0,25}}:|$)",
            testo or "", flags=re.IGNORECASE)
        valore = m.group(1).strip(" .,;-") if m else ""
        if campo == "isbn":
            # Dopo il numero puo' seguire altro testo ("... Disponibile su"):
            # si tiene solo il numero.
            numero = re.search(r"97[89](?:[\s-]?\d){10}", valore)
            valore = numero.group(0) if numero else ""
        # Due punti nel valore: l'etichetta era vuota ("AUTORE: GENERE: ...")
        # e si e' preso il campo dopo.
        if valore and ":" not in valore:
            trovati[campo] = valore
    return trovati


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
            titolo = pulisci_titolo_prodotto(
                strip_html((p.get("title") or {}).get("rendered", "")))
            if not titolo:
                continue

            # Alcuni negozi scrivono "Autore – Titolo" nel nome del prodotto
            # (Aculei). Va detto fonte per fonte in sources.json: altrove lo
            # stesso trattino separa il titolo dal sottotitolo.
            autore_nel_titolo = ""
            if source.get("titolo_con_autore"):
                parti = re.split(r"\s+[–—-]\s+", titolo, maxsplit=1)
                if len(parti) == 2 and parti[1].strip():
                    autore_nel_titolo, titolo = parti[0].strip(), parti[1].strip()

            # L'excerpt e' la presentazione editoriale: contiene i segnali
            # piu' preziosi ("gia' autore di...", "esordio", il genere).
            excerpt = strip_html((p.get("excerpt") or {}).get("rendered", ""))
            contenuto = strip_html((p.get("content") or {}).get("rendered", ""))
            scheda = scheda_tecnica(contenuto)

            autore = ""
            if tax and isinstance(p.get(tax), list):
                nomi = [autori_cache.get(i, "") for i in p[tax]]
                autore = " & ".join(n for n in nomi if n)
            if not autore:
                autore = autore_nel_titolo or scheda.get("autore", "")

            # Fallback autore per i post type senza tassonomia dedicata (es. il
            # 'book' di Acheron): molti temi mettono l'autore in un meta o in
            # una tassonomia dal nome diverso. Proviamo i nomi piu' comuni.
            if not autore:
                for campo in ("author_name", "book_author", "autore", "authors"):
                    val = p.get(campo) or (p.get("meta") or {}).get(campo)
                    # Una tassonomia esposta dalla REST API arriva come lista di
                    # ID numerici ('authors' di Angolazioni: [149, 148]): non e'
                    # un nome, e finiva nella scheda come autore "149, 148".
                    if isinstance(val, list) and all(isinstance(x, int) for x in val):
                        continue
                    if isinstance(val, list):
                        val = ", ".join(str(x) for x in val)
                    if val and isinstance(val, str) and val.strip():
                        autore = strip_html(val)
                        break

            # Ultimo ripiego: l'autore scritto nelle classi del prodotto. Viene
            # dopo la tassonomia perche' questa conserva accenti e punti.
            categorie, autori_classi = leggi_class_list(p.get("class_list"))
            if not autore and autori_classi:
                autore = " & ".join(autori_classi)
            # Il genere dichiarato dall'editore va fra le categorie: nella
            # sinossi sta in fondo, oltre il punto in cui il prompt la taglia.
            if scheda.get("genere"):
                categorie.append("genere: " + scheda["genere"])

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
                "isbn": normalizza_isbn(scheda.get("isbn")) or "",
                "data": p.get("date", ""),
                "categorie": categorie,
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


def resoconto_google(conto, dal):
    """
    Dove si perdono i volumi fra la risposta di Google Books e i candidati.

    Senza questa riga una fonte a zero non dice niente: puo' essere che
    Google non restituisca volumi, o che li restituisca e li scarti il filtro
    su editore e anno. Acheron, Delos e Sperling sono passate da 3, 3 e 1
    titoli a zero senza che nel registro comparisse un motivo.
    """
    log(f"      Google Books: {conto['ricerche']} ricerche "
        f"({conto['mute']} senza risposta, {conto['vuote']} senza volumi), "
        f"{conto['volumi']} volumi ricevuti; scartati {conto['altro_editore']} di un altro "
        f"editore, {conto['vecchi']} usciti prima del {dal.year}, {conto['senza_data']} senza data")


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
        log("      nessun autore noto per questo editore: niente da cercare")
        return []

    conto = dict.fromkeys(("ricerche", "mute", "vuote", "volumi", "altro_editore", "vecchi", "senza_data"), 0)

    for autore in sorted(autori):
        dati = get_json(GOOGLE_BOOKS, {
            "q": f'inauthor:"{autore}"',
            "langRestrict": "it",
            "orderBy": "newest",
            "maxResults": 20,
            "printType": "books",
        })
        time.sleep(0.4)
        conto["ricerche"] += 1
        if not isinstance(dati, dict):
            conto["mute"] += 1
            continue
        if not dati.get("items"):
            conto["vuote"] += 1
        conto["volumi"] += len(dati.get("items", []))

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
                conto["altro_editore"] += 1
                continue

            visti.add(titolo)
            data_pub = info.get("publishedDate", "")
            try:
                if int(data_pub[:4]) < dal.year:
                    conto["vecchi"] += 1
                    continue
            except (ValueError, IndexError):
                conto["senza_data"] += 1
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

    resoconto_google(conto, dal)
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

    conto = dict.fromkeys(("ricerche", "mute", "vuote", "volumi", "altro_editore", "vecchi", "senza_data"), 0)

    for nome in varianti:
        dati = get_json(GOOGLE_BOOKS, {
            "q": f'inpublisher:"{nome}"',
            "langRestrict": "it",
            "orderBy": "newest",
            "maxResults": 40,
            "printType": "books",
        })
        time.sleep(0.5)
        conto["ricerche"] += 1

        if not isinstance(dati, dict):
            conto["mute"] += 1
            continue
        if not dati.get("items"):
            conto["vuote"] += 1
            continue
        conto["volumi"] += len(dati["items"])

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
                    conto["vecchi"] += 1
                    continue
            except (ValueError, IndexError):
                conto["senza_data"] += 1
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

    resoconto_google(conto, dal)
    return trovati


# Tetto di sicurezza alle pagine lette per editore in un giro: un elenco
# impazzito non deve trasformare lo Scout in un programma che martella un sito.
MAX_PAGINE_PRODOTTO = 80
# I titoli piu' vecchi di cosi' non si propongono: il catalogo segue le uscite
# recenti, e il filtro per anno del sito mostra gli ultimi tre.
ANNI_SEGUITI = 2


def url_pulito(url):
    """Lo stesso indirizzo scritto in modi diversi deve risultare uguale."""
    return html.unescape(url or "").split("#")[0].split("?")[0].rstrip("/")


def leggi_pagina(url):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        return r.text if r.status_code == 200 else ""
    except requests.RequestException:
        return ""


def dati_prodotto(pagina):
    """
    I dati strutturati (JSON-LD, tipo Product) che i negozi mettono nella
    pagina per i motori di ricerca: nome, descrizione, immagine. Sono il modo
    piu' stabile di leggere un prodotto senza dipendere dall'impaginazione.
    """
    for blocco in re.findall(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", pagina):
        try:
            dati = json.loads(blocco)
        except ValueError:
            continue
        voci = dati if isinstance(dati, list) else [dati]
        if isinstance(dati, dict) and isinstance(dati.get("@graph"), list):
            voci = dati["@graph"]
        for v in voci:
            if isinstance(v, dict) and v.get("@type") == "Product":
                return v
    return {}


def adapter_pagine_prodotto(source, dal, url_noti=None):
    """
    Per i negozi senza API (Wix, costruttori di siti): si legge l'elenco dei
    prodotti — di solito la sitemap del negozio — e poi una pagina alla volta.
    Copre: Spirito Libero Edizioni, Fucine Editoriali.

    Due differenze dagli altri adapter, entrambe perche' qui ogni titolo
    costa una richiesta al sito dell'editore:
    - le pagine gia' note (in catalogo o fra gli scarti ricordati) non si
      rileggono: dopo il primo giro restano solo i prodotti nuovi;
    - questi elenchi non hanno date, quindi non c'e' finestra dei 30 giorni.
      Dove la pagina dichiara l'anno e il libro e' vecchio, lo si segna come
      scarto diretto: non va al classificatore e non si rilegge.
    """
    trovati = []
    editore = source["publisher"]
    filtro = source.get("filtro_url", "")
    url_noti = url_noti or set()

    indirizzi = []
    for elenco in source.get("elenco", []):
        for u in re.findall(r"https?://[^\s\"'<>\\]+", leggi_pagina(elenco)):
            u = url_pulito(u)
            if filtro and filtro in u and u not in indirizzi:
                indirizzi.append(u)
        time.sleep(PAUSA)

    da_leggere = [u for u in indirizzi if u not in url_noti][:MAX_PAGINE_PRODOTTO]
    log(f"      {len(indirizzi)} prodotti in elenco, {len(da_leggere)} non ancora visti")

    for u in da_leggere:
        pagina = leggi_pagina(u)
        time.sleep(PAUSA)
        dati = dati_prodotto(pagina)
        nome = strip_html(str(dati.get("name") or ""))
        if not nome:
            continue

        descrizione = strip_html(str(dati.get("description") or ""))
        testo = strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", pagina))
        # La scheda tecnica puo' stare nella descrizione (Fucine) o solo nel
        # testo della pagina (Spirito Libero): vale la prima che la riporta.
        scheda = scheda_tecnica(testo)
        scheda.update(scheda_tecnica(descrizione))

        # "TITOLO di Autore" nel nome del prodotto: si spezza sull'ultimo
        # " di ", perche' il titolo puo' contenerne altri.
        titolo, autore = nome, scheda.get("autore", "")
        separatore = source.get("nome_con_autore")
        if separatore and separatore in nome:
            titolo, autore = (x.strip() for x in nome.rsplit(separatore, 1))

        # La descrizione di Fucine prosegue con la scheda tecnica e con
        # residui di impaginazione: la sinossi finisce dove inizia la scheda.
        inizio_scheda = re.search(r"\b(?:autor[ei]|autrice|genere|formato)\s*:", descrizione, re.IGNORECASE)
        sinossi = descrizione[:inizio_scheda.start()].strip(" -") if inizio_scheda and inizio_scheda.start() > 80 else descrizione

        # Al classificatore serve anche il resto della pagina: spesso la
        # descrizione strutturata e' una riga e la trama sta sotto. Non finisce
        # nella scheda proposta, perche' porta con se' i testi dei pulsanti.
        paratesto = ""
        if sinossi[:40] and sinossi[:40] in testo:
            paratesto = testo[testo.index(sinossi[:40]):][:1500]

        immagine = dati.get("image")
        if isinstance(immagine, list):
            immagine = immagine[0] if immagine else ""
        if isinstance(immagine, dict):
            immagine = immagine.get("contentUrl") or immagine.get("url") or ""

        anno = re.search(r"(?:19|20)\d\d", scheda.get("pubblicazione", ""))
        anno = int(anno.group(0)) if anno else None

        cand = {
            "titolo": titolo,
            "autore": autore,
            "editore": editore,
            "url": u,
            "sinossi": sinossi,
            "paratesto": paratesto,
            "copertina": immagine if isinstance(immagine, str) else "",
            "isbn": normalizza_isbn(scheda.get("isbn")) or "",
            "data": str(anno) if anno else "",
            "categorie": [f"{k}: {scheda[k]}" for k in ("genere", "sottogenere") if scheda.get(k)],
            "_fonte": "pagine_prodotto",
        }
        if anno is None:
            cand["_anno_ignoto"] = True
        elif anno < dal.year - ANNI_SEGUITI:
            cand["_scarto_diretto"] = f"uscito nel {anno}, prima del periodo seguito"
        trovati.append(cand)

    return trovati


ADAPTERS = {
    "pagine_prodotto": adapter_pagine_prodotto,
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
    # Serie Disney che passava il filtro per via della parola "magia":
    # "Carica dei 101 - I Classici e la magia del backstage" e simili.
    "i classici e la magia",
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
    # Su Shopify la barra separa il titolo dalla variante di prodotto
    # ("Wings of Reverie | Custom Sprayed Edges"): e' un libro gia' visto.
    if " | " in cand.get("titolo", ""):
        return False, "variante di un prodotto"

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
# Ripiego a regole, per quando il classificatore non risponde
# --------------------------------------------------------------------------
# Il classificatore gratuito in certi giorni non risponde affatto: fra il 12 e
# il 13 ottobre 2026 non ha dato un solo verdetto, e nello stesso periodo il
# moderatore ha trovato a mano trenta titoli. Un titolo senza verdetto resta
# fuori — e deve restare cosi' per gli editori generalisti, da cui nell'estate
# del 2026 arrivo' in moderazione di tutto — ma per i piccoli editori di
# genere c'e' un segnale che non ha bisogno di un modello: l'editore stesso
# che scrive "Genere: Fantasy" sulla scheda del libro.
#
# La regola e' volutamente stretta. Propone solo se:
# - la fonte e' fra quelle abilitate in sources.json ('ripiego_a_regole');
# - l'autore c'e' e non ha un nome palesemente straniero;
# - fra le etichette dell'editore (categorie, tag, genere dichiarato) ce n'e'
#   una di fantasy, e non e' il nome di una collana mista con la fantascienza;
# - non e' un'antologia, un cofanetto, un fumetto, una rivista;
# - la presentazione non parla di traduzione.
# Non sa dire se l'autore e' italiano ne' se e' un esordio: lo scrive nel
# motivo, e la proposta arriva con confidenza bassa.
RIPIEGO_GENERI = ("fantasy", "romantasy", "grimdark", "sword and sorcery")
RIPIEGO_ESCLUSI = (
    "antologia", "racconti", "raccolta", "box ", "cofanetto", "fumetto",
    "graphic novel", "manga", "albo ", "illustrato", "saggio", "manuale",
    "librogame", "libro-game", "gioco di ruolo", "rivista", "poesi",
)
RIPIEGO_TRADUZIONI = ("traduzione", "tradotto", "traduttore", "traduttrice")
RIPIEGO_HORROR = ("horror", "gotico", "weird")


def ripiego_a_regole(cand):
    """Un verdetto di ammissione costruito sulle etichette dell'editore, o None."""
    if not cand.get("_ripiego"):
        return None
    autore = (cand.get("autore") or "").strip()
    if not autore or autore_probabilmente_straniero(autore):
        return None

    etichette = [str(c).lower() for c in cand.get("categorie", []) if c]
    # "Labyrinth fantasy e fantascienza" (La Corte) e' una collana: un romanzo
    # di fantascienza che ne fa parte porta la parola "fantasy" senza esserlo.
    dichiarate = [c for c in etichette
                  if any(g in c for g in RIPIEGO_GENERI) and "fantascienza" not in c]
    if not dichiarate:
        return None
    # Horror con un sottogenere fantasy ("genere: Gotico, sottogenere: Dark
    # Fantasy"): il genere principale dice che lo scopo e' far paura. Il 15
    # ottobre 2026 i titoli cosi' sono stati scartati tutti in moderazione.
    if any(c.startswith("genere: ") and any(h in c for h in RIPIEGO_HORROR) for c in etichette):
        return None

    intestazione = (cand.get("titolo", "") + " " + " ".join(etichette)).lower()
    if any(x in intestazione for x in RIPIEGO_ESCLUSI):
        return None
    presentazione = (cand.get("sinossi", "")[:1500] + " " + cand.get("paratesto", "")[:1500]).lower()
    if any(x in presentazione for x in RIPIEGO_TRADUZIONI):
        return None

    etichetta = dichiarate[0]
    if etichetta.startswith("genere: "):
        etichetta = etichetta[len("genere: "):]
    return {
        "ammesso": True,
        "motivo": (f"Proposto per l'etichetta dell'editore («{etichetta}»): il classificatore "
                   "non era disponibile. Autore italiano ed esordio da verificare."),
        "genere": genere_esplicito(cand),
        "esordio": None,
        "confidenza": "bassa",
    }


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
   (fantascienza CON magia esplicita).
   NON sono fantasy: fantascienza senza magia, thriller, giallo, romance senza
   elemento soprannaturale, realismo magico puramente letterario.
   NON è fantasy nemmeno l'HORROR, anche quando contiene fantasmi, demoni,
   vampiri, maledizioni, possessioni, patti con la Morte o altri elementi
   soprannaturali: un romanzo il cui scopo principale è far paura (horror,
   gotico, weird, folk horror) SI SCARTA. Lo si ammette solo se l'editore lo
   qualifica esplicitamente come fantasy (dark fantasy, urban fantasy...),
   oppure se il soprannaturale è il sistema di un mondo — una magia con le sue
   regole, popoli e creature di un mondo immaginario — e non soltanto la
   minaccia che genera paura. Nel dubbio fra horror e dark fantasy, SCARTA.
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
        self.muti_di_fila = 0
        self.detti_429 = set()   # tipi di limite gia' riportati nel registro

    def senza_verdetto(self):
        """Un candidato e' rimasto senza risposta, per qualunque motivo."""
        self.muti_di_fila += 1
        if self.muti_di_fila >= MAX_MUTI_DI_FILA and not self.esaurita:
            self.esaurita = True
            log(f"\n  ! Il classificatore non risponde da {self.muti_di_fila} candidati di fila:")
            log("    mi fermo qui. I candidati restanti NON vengono proposti e")
            log("    verranno ripresi al prossimo giro.\n")

    def prima_della_chiamata(self):
        if self.pausa > 0:
            time.sleep(self.pausa)

    def ok(self):
        self.consecutivi_429 = 0
        self.muti_di_fila = 0
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
            log("\n  ! Quota del modello esaurita. I candidati restanti NON")
            log("    vengono proposti: rilancia lo Scout domani e li riprende.\n")
            return 0

        pausa = attesa_suggerita if attesa_suggerita else self.pausa
        return min(pausa, 30)  # mai oltre mezzo minuto per un singolo retry


def gemini_modelli(chiave):
    """
    I modelli preferiti che Google dichiara disponibili per la chiave, in
    ordine di preferenza. Non uno solo: il piu' nuovo e' spesso sovraccarico
    sul piano gratuito (il 24 settembre 2026 gemini-3.8-flash rispondeva 503,
    "high demand"), e allora si passa al successivo.

    Se l'elenco non e' leggibile si tengono tutti i preferiti: saranno le
    chiamate vere a dire quali ci sono. Se e' leggibile ma nessun preferito
    compare, la lista e' vuota e non si classifica niente.
    """
    imposto = os.environ.get("GEMINI_MODEL", "").strip()
    preferiti = [imposto] if imposto else list(GEMINI_PREFERITI)
    try:
        disponibili, pagina = set(), None
        for _ in range(10):
            params = {"pageSize": 1000}
            if pagina:
                params["pageToken"] = pagina
            r = requests.get(f"{GEMINI_BASE}/models", params=params,
                             headers={"x-goog-api-key": chiave}, timeout=TIMEOUT)
            if r.status_code != 200:
                log(f"  ! Elenco dei modelli Gemini: HTTP {r.status_code}, provo i preferiti")
                return preferiti
            dati = r.json()
            for m in dati.get("models", []):
                disponibili.add(str(m.get("name", "")).split("/", 1)[-1])
            pagina = dati.get("nextPageToken")
            if not pagina:
                break
    except (requests.RequestException, ValueError) as e:
        log(f"  ! Elenco dei modelli Gemini illeggibile ({type(e).__name__}), provo i preferiti")
        return preferiti

    scelti = [m for m in preferiti if m in disponibili]
    if scelti:
        return scelti
    log(f"  ! Nessuno dei modelli preferiti e' disponibile: {', '.join(preferiti)}")
    gemini = sorted(m for m in disponibili if "flash" in m)
    if gemini:
        log(f"    Quelli con 'flash' nel nome sono: {', '.join(gemini[:12])}")
        log("    Aggiorna GEMINI_PREFERITI in scripts/scout.py.")
    return []


def testo_da_risposta(dati):
    """
    Il testo generato, qualunque sia la forma della risposta.

    Google sta passando dall'API generateContent (candidates -> content ->
    parts) alla nuova API interactions, e la documentazione non e' univoca
    su dove stia il testo. Si cercano tutte le forme note, saltando i
    pensieri del modello, che non fanno parte della risposta.
    """
    if isinstance(dati.get("output_text"), str) and dati["output_text"].strip():
        return dati["output_text"]

    pezzi = []

    def raccogli(blocchi):
        for b in blocchi or []:
            if not isinstance(b, dict):
                continue
            if b.get("thought") is True or b.get("type") in ("thought", "thinking"):
                continue
            if isinstance(b.get("text"), str):
                pezzi.append(b["text"])
            for dentro in ("content", "parts"):
                if isinstance(b.get(dentro), list):
                    raccogli(b[dentro])

    raccogli(dati.get("outputs"))
    for passo in dati.get("steps") or []:
        if isinstance(passo, dict) and passo.get("type") in (None, "model_output"):
            raccogli(passo.get("content"))
    for candidato in dati.get("candidates") or []:
        raccogli(((candidato or {}).get("content") or {}).get("parts"))
    return "".join(pezzi)


# Quale delle due API ha risposto l'ultima volta: una volta trovata quella
# buona, gli altri candidati non ripagano il tentativo sull'altra.
_API_GEMINI = {"usata": None}


def chiama_gemini(chiave, modello, sistema, utente):
    """
    Una richiesta a Gemini. Restituisce (stato_http, testo, attesa_suggerita).

    Prima l'API interactions, che Google indica come quella corrente; se
    risponde 404 o 400, la classica generateContent, che la documentazione
    chiama ormai legacy ma che e' la piu' collaudata.
    """
    testate = {"x-goog-api-key": chiave, "Content-Type": "application/json"}
    tentativi = {
        "interactions": (
            f"{GEMINI_BASE}/interactions",
            {
                "model": modello,
                "system_instruction": sistema,
                "input": utente,
                "generation_config": {
                    "thinking_level": "low",
                    "max_output_tokens": GEMINI_MAX_USCITA,
                },
            },
        ),
        "generateContent": (
            f"{GEMINI_BASE}/models/{modello}:generateContent",
            {
                "systemInstruction": {"parts": [{"text": sistema}]},
                "contents": [{"role": "user", "parts": [{"text": utente}]}],
                "generationConfig": {
                    "maxOutputTokens": GEMINI_MAX_USCITA,
                    "responseMimeType": "application/json",
                },
            },
        ),
    }
    ordine = ["interactions", "generateContent"]
    if _API_GEMINI["usata"] in ordine:
        ordine = [_API_GEMINI["usata"]]

    ultimo = (0, "", None)
    for nome in ordine:
        url, corpo = tentativi[nome]
        r = requests.post(url, headers=testate, json=corpo, timeout=GEMINI_TIMEOUT)
        attesa = r.headers.get("retry-after")
        try:
            attesa = int(attesa)
        except (TypeError, ValueError):
            attesa = None
        if r.status_code == 200:
            _API_GEMINI["usata"] = nome
            return 200, testo_da_risposta(r.json()), None
        ultimo = (r.status_code, r.text[:300], attesa)
        if r.status_code not in (400, 404):
            break  # 429, 5xx, chiave rifiutata: l'altra API non cambierebbe niente
    return ultimo


# Quando il classificatore non risponde, il candidato NON passa.
#
# Prima passava, con confidenza "bassa" e il motivo "da verificare a mano":
# l'idea era non perdere niente. Il 30 luglio 2026 GitHub ha chiuso GitHub
# Models, ogni chiamata ha cominciato a rispondere 410, e per due mesi in
# moderazione e' arrivato tutto quello che superava il filtro a regole:
# classici Disney, saggi, copie autografate, romanzi non fantasy, e quasi
# tutto e' stato scartato a mano. Il tempo di chi modera costa piu' di un titolo
# perso: un libro scartato qui torna nella finestra dei 30 giorni al giro
# dopo, e comunque si recupera con --dal.
NON_CLASSIFICATO = {
    "ammesso": False,
    "motivo": "classificatore non raggiungibile",
    "_non_classificato": True,
}


# Errori che dicono "il modello e' occupato adesso", non "la richiesta e'
# sbagliata": ha senso chiedere a un altro modello, o riprovare fra poco.
SOVRACCARICO = (500, 502, 503, 504)


def tipo_di_limite(testo):
    """
    Dal messaggio di un 429: ("giorno" | "minuto" | "", secondi da aspettare
    o None, messaggio). Google lo scrive per esteso, per esempio "Rate limit
    exceeded for model gemini-3.8-flash (limit: 5 requests per minute on Free
    Tier). Please retry in 28s".
    """
    try:
        messaggio = (json.loads(testo).get("error") or {}).get("message", "")
    except (ValueError, AttributeError):
        messaggio = testo or ""
    basso = messaggio.lower()
    attesa = re.search(r"retry in ([0-9.]+)s", basso)
    secondi = float(attesa.group(1)) if attesa else None
    if "per day" in basso or "perday" in basso:
        return "giorno", secondi, messaggio
    if "per minute" in basso or "perminute" in basso:
        return "minuto", secondi, messaggio
    return "", secondi, messaggio


def classifica(cand, chiave, modelli, quota, tentativi=2):
    """
    Chiede a Gemini se il candidato entra in catalogo.

    modelli e' la lista dei disponibili, in ordine di preferenza, condivisa
    fra tutti i candidati: se il primo e' sovraccarico si prova il
    successivo, e quello che risponde passa in testa, cosi' i candidati
    dopo vanno subito da lui.

    Due tentativi, non quattro: se il modello e' sotto pressione, insistere sul
    singolo candidato non aiuta — meglio rallentare il ritmo generale (lo fa
    l'oggetto Quota) e andare avanti.
    """
    if not chiave or not modelli or quota.esaurita:
        return NON_CLASSIFICATO

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

    # Un while e non un for: un'attesa per il limite al minuto non deve
    # consumare uno dei tentativi (vedi sotto).
    tentativo, attese_minuto = 0, 0
    while tentativo < tentativi:
        tentativo += 1
        quota.prima_della_chiamata()

        try:
            if not modelli:
                # Tutti messi da parte per oggi, durante questo candidato.
                break
            stato, attesa_minuto = None, 0
            for modello in list(modelli):
                stato, testo, suggerita = chiama_gemini(chiave, modello, PROMPT, scheda)
                if stato == 429:
                    # Su Gemini il limite di richieste e' di ciascun modello,
                    # non della chiave: uno puo' rifiutare e il successivo
                    # rispondere. E i limiti sono due, al minuto e al giorno,
                    # che vanno trattati in modo opposto: il primo passa da
                    # solo in pochi secondi, il secondo dura fino a domani.
                    tipo, secondi, messaggio = tipo_di_limite(testo)
                    if tipo not in quota.detti_429:
                        # Una volta per tipo e per giro, il messaggio di Google.
                        quota.detti_429.add(tipo)
                        log(f"      · troppe richieste (HTTP 429): {strip_html(messaggio)[:220]}")
                    if tipo == "giorno":
                        modelli.remove(modello)
                        log(f"      · {modello} ha finito le richieste di oggi: lo metto da parte")
                    else:
                        if tipo == "minuto":
                            attesa_minuto = max(attesa_minuto, secondi or 30)
                        log(f"      · {modello} oltre il limite di richieste, provo il successivo")
                    continue
                if stato not in SOVRACCARICO:
                    break
                log(f"      · {modello} sovraccarico (HTTP {stato}), provo il successivo")

            if not modelli:
                if not quota.esaurita:
                    quota.esaurita = True
                    log("")
                    log("  ! Tutti i modelli hanno finito le richieste di oggi. I candidati")
                    log("    restanti NON vengono proposti e verranno ripresi al prossimo giro.")
                    log("")
                break

            if stato == 429 and attesa_minuto and attese_minuto < MAX_ATTESE_MINUTO:
                # Tutti i modelli ancora disponibili sono al limite del minuto:
                # si aspetta quanto dice Google e si riprova, senza contare il
                # tentativo. Il 15 ottobre 2026, senza questa distinzione, lo
                # Scout ha scambiato una raffica di limiti al minuto per la
                # quota del giorno finita e ha lasciato fuori cinque titoli.
                attese_minuto += 1
                tentativo -= 1
                attesa = min(attesa_minuto + 2, 65)
                log(f"      · limite al minuto su tutti i modelli: aspetto {attesa:.0f}s")
                time.sleep(attesa)
                continue

            if stato in SOVRACCARICO:
                # Sovraccarichi tutti: si aspetta un poco e si riprova una volta.
                if tentativo == tentativi:
                    log("      ! tutti i modelli sovraccarichi")
                    break
                time.sleep(15)
                continue

            if stato == 200 and modelli[0] != modello:
                modelli.remove(modello)
                modelli.insert(0, modello)
                log(f"      · d'ora in poi uso {modello}")

            if stato == 429:
                # Qui si arriva se anche l'ultimo modello era oltre un limite di
                # cui il messaggio non dice la durata, o se le attese al minuto
                # sono finite: si torna alla vecchia regola dei 429 di fila.
                attesa = quota.rate_limited(suggerita)
                if quota.esaurita or tentativo == tentativi:
                    break
                log(f"      · rallento a {quota.pausa:.0f}s")
                time.sleep(attesa)
                continue

            if stato != 200:
                log(f"      ! HTTP {stato}: {testo[:160]}")
                break

            # Il modello a volte racchiude il JSON fra ``` anche se gli si
            # chiede di non farlo: si tolgono prima di leggerlo.
            testo = re.sub(r"^```(?:json)?|```$", "", (testo or "").strip(), flags=re.MULTILINE).strip()
            esito = json.loads(testo)
            if not isinstance(esito, dict) or "ammesso" not in esito:
                raise ValueError("risposta senza il campo 'ammesso'")
            quota.ok()

            if not esito.get("genere"):
                esito["genere"] = genere_esplicito(cand)
            return esito

        except (requests.RequestException, KeyError, ValueError) as e:
            # Solo il tipo di errore: prima questo caso non lasciava traccia,
            # e un candidato "senza verdetto" poteva voler dire qualunque cosa.
            log(f"      · nessuna risposta utilizzabile ({type(e).__name__})")
            if tentativo == tentativi:
                break
            time.sleep(2)

    quota.senza_verdetto()
    return NON_CLASSIFICATO


def prova_classificatore():
    """
    Quattro casi di cui si conosce la risposta giusta: un romanzo fantasy di un
    autore italiano, un libro illustrato per bambini, una traduzione e un
    horror soprannaturale di un autore italiano (il 15 ottobre 2026 erano
    passati come dark fantasy, e il moderatore li ha scartati tutti). Serve
    a verificare il classificatore da GitHub Actions, dove sta la chiave,
    prima di affidargli il giro vero. Non tocca il database.
    """
    casi = [
        (True, {
            "titolo": "Il canto di Liscamara", "autore": "Mattia Manfredonia",
            "editore": "Lumien", "categorie": ["Fantasy", "Narrativa"],
            "paratesto": "Un romanzo fantasy d'avventura dell'autore italiano Mattia Manfredonia.",
            "sinossi": ("Nel mare di Vespria una ciurma di pirati da' la caccia a un'antica "
                        "reliquia che risveglia la magia sopita dei popoli delle isole. "
                        "Tra patti occulti, maledizioni e creature degli abissi, la giovane "
                        "Liscamara scopre di essere legata al canto che puo' salvare o "
                        "distruggere il suo mondo. Un romanzo di avventura e stregoneria."),
        }),
        (False, {
            "titolo": "Il regno degli animali", "autore": "Redazione Giunti",
            "editore": "Giunti", "categorie": ["Bambini", "Animali", "Divulgazione"],
            "paratesto": "Un libro illustrato per scoprire gli animali del pianeta. Dai 6 anni.",
            "sinossi": ("Un viaggio illustrato tra mammiferi, uccelli, rettili e pesci per "
                        "scoprire come vivono, cosa mangiano e dove abitano gli animali. "
                        "Con curiosita', schede e tante immagini a colori."),
        }),
        (False, {
            "titolo": "The Ordeals", "autore": "Rachel Greenlaw",
            "editore": "Giunti", "categorie": ["Young Adult", "Fantasy"],
            "paratesto": "Il fenomeno fantasy internazionale arriva in Italia, tradotto dall'inglese.",
            "sinossi": ("In un'accademia magica dove ogni prova puo' essere l'ultima, una "
                        "giovane studentessa sfida le regole per salvare chi ama. "
                        "Il bestseller internazionale finalmente in italiano."),
        }),
        (False, {
            "titolo": "La casa sul lago nero", "autore": "Giulia Ferraresi",
            "editore": "Spirito Libero Edizioni", "categorie": ["genere: Horror", "sottogenere: Gotico"],
            "paratesto": "Un horror gotico che vi terra' svegli la notte, dalla penna di un'autrice italiana.",
            "sinossi": ("Quando Marta eredita la villa della nonna sulle rive di un lago, "
                        "scopre che la casa non e' mai rimasta vuota. Di notte i corridoi "
                        "si riempiono di passi, una bambina morta un secolo prima chiede di "
                        "giocare e un'antica maledizione reclama un'altra vittima. Per "
                        "spezzarla Marta dovra' affrontare lo spirito che abita la cantina, "
                        "prima che la casa si prenda anche lei."),
        }),
    ]

    chiave = os.environ.get("GEMINI_API_KEY", "")
    if not chiave:
        log("GEMINI_API_KEY assente: niente da provare.")
        return 2
    modelli = gemini_modelli(chiave)
    if not modelli:
        return 2
    log(f"Modelli disponibili, in ordine: {', '.join(modelli)}\n")

    giusti = 0
    quota = Quota()
    for atteso, cand in casi:
        esito = classifica(cand, chiave, modelli, quota)
        if esito.get("_non_classificato"):
            log(f"  ?  {cand['titolo']}: il classificatore non ha risposto")
            continue
        ok = bool(esito.get("ammesso")) == atteso
        giusti += ok
        segno = "OK" if ok else "!!"
        log(f"  {segno} {cand['titolo']}: ammesso={esito.get('ammesso')} "
            f"(atteso {atteso}) · conf={esito.get('confidenza')} · {esito.get('motivo', '')[:80]}")

    log(f"\nAPI usata: {_API_GEMINI['usata'] or 'nessuna'} · modello: {modelli[0]}")
    log(f"Verdetti giusti: {giusti} su {len(casi)}")
    return 0 if giusti == len(casi) else 2


# --------------------------------------------------------------------------
# Programma principale
# --------------------------------------------------------------------------

def codice_uscita(non_classificati, quota, verdetti):
    """
    0: tutto classificato, oppure il classificatore ha risposto almeno in
    parte. In quel caso i candidati rimasti indietro si riprendono al giro
    dopo, e un'email di errore ogni settimana per cinque titoli su quaranta
    (e' successo il 15 ottobre 2026) farebbe smettere di leggerle.
    Errore solo se il classificatore non ha dato nemmeno un verdetto:
    2 se a tratti rispondeva — riprovare piu' tardi ha senso, e il workflow
    lo fa — e 3 se e' stato dato per fuori servizio, perche' riprovare dopo
    venti minuti rileggerebbe le pagine degli editori per niente.
    """
    if not non_classificati or verdetti:
        return 0
    return 3 if quota.esaurita else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dal", help="Data di partenza (YYYY-MM-DD). Default: 30 giorni fa.")
    ap.add_argument("--dry-run", action="store_true", help="Non scrive nulla.")
    ap.add_argument("--limite", type=int, default=120,
                    help="Tetto ai candidati classificati. Con la pausa anti-429 "
                         "ogni candidato costa ~8s: 80 sono circa 11 minuti.")
    ap.add_argument("--prova-classificatore", action="store_true",
                    help="Classifica tre casi noti e dice se i verdetti tornano. "
                         "Non legge le fonti e non scrive niente.")
    args = ap.parse_args()

    if args.prova_classificatore:
        return prova_classificatore()

    if args.dal:
        dal = datetime.fromisoformat(args.dal).replace(tzinfo=timezone.utc)
    else:
        dal = datetime.now(timezone.utc) - timedelta(days=30)

    log(f"Scout — finestra dal {dal.date()}\n" + "=" * 60)

    # --- Catalogo: serve per il dedup e per il segnale esordio ---
    # Da Supabase, non piu' dal file: cosi' lo Scout vede anche i titoli che
    # hai scartato dalla moderazione, e non te li ripropone all'infinito.
    if supa_attivo():
        libri = supa_leggi_libri()
        catalogo = [{
            "id": b.get("id"),
            "title": b.get("title") or "",
            "author": b.get("author") or "",
            "publisher": b.get("publisher") or "",
            "isbn": b.get("isbn") or "",
            "status": b.get("status") or "",
            "url": b.get("store_publisher_url") or "",
        } for b in libri]
        scartati_noti = sum(1 for b in catalogo if b["status"] == "rejected")
        log(f"Supabase: {len(catalogo)} schede note "
            f"({scartati_noti} già scartate, non verranno riproposte)")
    else:
        log("  ! SUPABASE_URL/SUPABASE_SERVICE_KEY assenti: uso il file locale.")
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

    # I candidati gia' in attesa non vanno piu' cercati a parte: su Supabase
    # sono righe della stessa tabella (status='candidate'), quindi la lettura
    # qui sopra li ha gia' inclusi nel dedup.
    esistenti = []
    if not supa_attivo() and CANDIDATES_FILE.exists():
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
    avvio = time.time()
    resa_fonti = {}   # editore -> quanti elementi ha prodotto

    # Servono gia' qui, non solo al dedup: l'adapter che legge i negozi una
    # pagina alla volta salta gli indirizzi gia' in catalogo o gia' scartati.
    scarti = leggi_scarti()
    scarti_prima = len(scarti)
    url_noti = {url_pulito(b.get("url")) for b in catalogo if b.get("url")}
    url_noti.update(url_pulito(v.get("url")) for v in scarti.values() if v.get("url"))

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
            elif src["adapter"] == "pagine_prodotto":
                trovati = fn(src, dal, url_noti)
            else:
                trovati = fn(src, dal)
        except Exception as e:
            log(f"      ! errore ({type(e).__name__}: {e}) — proseguo con gli altri")
            trovati = []

        # Un editore che fallisce non deve fermare gli altri tredici.
        for t in trovati:
            t["_prefiltro_obbligatorio"] = src.get("prefiltro_obbligatorio", False)
            t["_ripiego"] = src.get("ripiego_a_regole", False)
        grezzi.extend(trovati)
        resa_fonti[nome] = len(trovati)
        log(f"      {len(trovati)} elementi")
        time.sleep(PAUSA)

    log(f"\nRaccolti {len(grezzi)} elementi grezzi.")
    riepilogo_errori_google()

    # --- Dedup e prefiltro ---
    superstiti = []
    scartati = {"gia_in_catalogo": 0, "prefiltro": 0, "non_libri": 0, "gia_valutati": 0,
                "fuori_periodo": 0}

    for g in grezzi:
        # La chiave si calcola qui, sui dati come arrivano dalla fonte: piu'
        # avanti l'arricchimento puo' aggiungere l'autore, e la stessa scheda
        # al giro dopo non si riconoscerebbe piu'.
        g["_chiave_scarto"] = chiave_titolo(g["titolo"], g.get("autore", ""))
        if g["_chiave_scarto"] in scarti:
            scartati["gia_valutati"] += 1
            continue

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

        # Troppo vecchio per il catalogo: lo dice la pagina stessa, non serve
        # il classificatore. Si ricorda, cosi' la pagina non viene riletta.
        if g.get("_scarto_diretto"):
            scarti[g["_chiave_scarto"]] = {
                "chiave": g["_chiave_scarto"], "titolo": g["titolo"],
                "autore": g.get("autore", ""), "editore": g["editore"],
                "url": g.get("url", ""), "motivo": g["_scarto_diretto"],
                "data": datetime.now(timezone.utc).date().isoformat(),
            }
            scartati["fuori_periodo"] += 1
            continue

        ok, motivo = passa_prefiltro(g, g.get("_prefiltro_obbligatorio", False))
        if not ok:
            scartati["non_libri" if "romanzo" in motivo else "prefiltro"] += 1
            continue

        superstiti.append(g)

    log(f"Dopo dedup e prefiltro: {len(superstiti)} da valutare")
    log(f"  scartati: {scartati['gia_in_catalogo']} già in catalogo, "
        f"{scartati['prefiltro']} fuori tema, {scartati['non_libri']} non romanzi, "
        f"{scartati['gia_valutati']} già scartati in un giro precedente, "
        f"{scartati['fuori_periodo']} usciti prima del periodo seguito")

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
        "pagine_prodotto": 0,       # Spirito Libero, Fucine: piccoli editori di genere
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
    chiave = os.environ.get("GEMINI_API_KEY", "")
    modelli = gemini_modelli(chiave) if chiave else []
    if not chiave:
        log("\n  ! GEMINI_API_KEY assente: senza classificatore non verra' proposto niente.\n")
    elif modelli:
        log(f"\nClassificatore: Gemini, modelli in ordine {', '.join(modelli)}")

    nuovi = []
    non_classificati = []
    per_regola = []   # proposti dal ripiego a regole, a classificatore muto
    verdetti = 0      # candidati a cui il classificatore ha risposto
    quota = Quota()
    log(f"\nClassifico {len(superstiti)} candidati...")
    inizio = time.time()

    # La coda puo' allungarsi mentre la si scorre: un candidato rimasto senza
    # verdetto viene rimesso in fondo e riprovato una volta sola, dopo una
    # pausa. Prima veniva abbandonato subito, anche se il servizio tornava a
    # rispondere al candidato dopo, e bastava a far uscire il giro con errore.
    coda = list(superstiti)
    ripasso_iniziato = False

    for i, c in enumerate(coda, 1):
        if c.get("_ripasso"):
            # Se nel frattempo il classificatore e' stato dato per fuori servizio,
            # aspettare non serve: il candidato resta fuori subito.
            if not ripasso_iniziato and not quota.esaurita:
                ripasso_iniziato = True
                log(f"\n  Riprovo i candidati rimasti senza verdetto (pausa di {PAUSA_RIPASSO}s)...")
                time.sleep(PAUSA_RIPASSO)
            log(f"  [ripasso] {c['titolo'][:55]}")
        else:
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

        esito = classifica(c, chiave, modelli, quota)
        if not esito.get("_non_classificato"):
            verdetti += 1

        if esito.get("_non_classificato"):
            # Se la quota e' finita o il classificatore manca del tutto,
            # riprovare non cambia niente.
            if not c.get("_ripasso") and chiave and modelli and not quota.esaurita:
                c["_ripasso"] = True
                coda.append(c)
                log("        ? senza verdetto: lo riprovo in fondo")
                continue
            # Il classificatore non c'e': per i piccoli editori di genere
            # decide l'etichetta dell'editore. Se la regola dice si', il
            # candidato prosegue come uno ammesso; altrimenti resta fuori.
            esito = ripiego_a_regole(c)
            if not esito:
                non_classificati.append(c["titolo"])
                log("        ? non classificato: resta fuori")
                continue
            per_regola.append(c["titolo"])
            log("        ≈ senza verdetto, ma l'editore lo dichiara fantasy: lo propongo")

        if not esito.get("ammesso"):
            log(f"        ✗ {esito.get('motivo', '')[:60]}")
            # Si ricorda solo uno scarto deciso. Con confidenza bassa il
            # classificatore sta dicendo che gli mancavano elementi (di solito
            # l'autore): al giro dopo, con dati migliori, puo' cambiare idea.
            if esito.get("confidenza") in ("alta", "media"):
                scarti[c["_chiave_scarto"]] = {
                    "chiave": c["_chiave_scarto"],
                    "titolo": c["titolo"],
                    "autore": c.get("autore", ""),
                    "editore": c["editore"],
                    "url": c.get("url", ""),
                    "motivo": (esito.get("motivo") or "")[:200],
                    "data": datetime.now(timezone.utc).date().isoformat(),
                }
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
            "cover_url": c.get("copertina", ""),
            "description": (c.get("sinossi") or "")[:2000],
            "store_amazon_url": cerca_amazon(c),
            "store_publisher_url": c.get("url", ""),
            "is_debut": esito.get("esordio"),
            "genre": esito.get("genere"),
            "labels": [],
            "featured": False,
            "status": "candidate",
            "source": c.get("_fonte", ""),
            "confidence": esito.get("confidenza", "bassa"),
            # Dove la fonte non dichiara l'anno, quello scritto qui sopra e'
            # l'anno in corso per mancanza di meglio: va detto a chi modera.
            "reason": ((esito.get("motivo") or "")[:300]
                       + (" — Anno di uscita non indicato dalla fonte: da verificare."
                          if c.get("_anno_ignoto") else "")),
        })
        log(f"        ✓ {esito.get('genere') or 'fantasy'} · "
            f"esordio={esito.get('esordio')} · conf={esito.get('confidenza')}")

    durata = int(time.time() - inizio)
    log(f"\n  (classificazione: {durata//60}m {durata%60}s)")
    # Di nuovo, perche' anche l'arricchimento interroga Google Books.
    riepilogo_errori_google()

    if per_regola:
        log("")
        log(f"  ≈ {len(per_regola)} proposti per l'etichetta dell'editore, perche' il "
            "classificatore non ha risposto:")
        for titolo in per_regola[:15]:
            log(f"      - {titolo[:70]}")

    if non_classificati:
        log(f"\n  ! {len(non_classificati)} candidati NON classificati, quindi non proposti:")
        for titolo in non_classificati[:15]:
            log(f"      - {titolo[:70]}")

    # --- Scrittura ---
    log("\n" + "=" * 60)
    log(f"Nuovi candidati: {len(nuovi)}")

    if args.dry_run:
        log("(dry-run: non scrivo nulla)")
        print(json.dumps(nuovi, ensure_ascii=False, indent=2)[:3000])
        return codice_uscita(non_classificati, quota, verdetti)

    if len(scarti) != scarti_prima:
        scrivi_scarti(scarti)
        log(f"Scarti del classificatore ricordati: {len(scarti)} "
            f"({len(scarti) - scarti_prima:+d} in questo giro).")

    durata_totale = int(time.time() - avvio)

    if supa_attivo():
        if nuovi:
            if supa_scrivi_candidati(nuovi):
                log(f"Scritti su Supabase: {len(nuovi)} candidati in attesa "
                    f"di revisione.")
                incerti = sum(1 for n in nuovi if n["confidence"] == "bassa")
                if incerti:
                    log(f"  {incerti} con confidenza bassa: guardali con attenzione.")
            else:
                log("  ! I candidati NON sono stati salvati. Il log qui sopra "
                    "dice perché.")
        else:
            log("Niente da aggiungere.")

        # Storico dell'esecuzione. Ha due utilita': vedere nel tempo quali
        # fonti rendono, e tenere attivo il progetto Supabase (i piani
        # gratuiti vengono sospesi dopo un periodo di inattivita').
        supa_registra_esecuzione({
            "dal": dal.date().isoformat(),
            "fonti_ok": sum(1 for v in resa_fonti.values() if v > 0),
            "fonti_vuote": sum(1 for v in resa_fonti.values() if v == 0),
            "grezzi": len(grezzi),
            "candidati": len(nuovi),
            "durata_sec": durata_totale,
            "dettaglio": resa_fonti,
        })

        # Copia di sicurezza versionata su GitHub. Il sito non la usa piu',
        # ma se Supabase sparisse il catalogo sarebbe comunque qui.
        quanti = supa_esporta_backup()
        if quanti:
            log(f"Backup aggiornato: {CATALOG_FILE.relative_to(ROOT)} "
                f"({quanti} titoli in catalogo).")

    else:
        # Modalita' di ripiego, senza Supabase: si scrive il vecchio file.
        if not nuovi:
            log("Niente da aggiungere.")
            return codice_uscita(non_classificati, quota, verdetti)
        tutti = esistenti + nuovi
        CANDIDATES_FILE.write_text(
            json.dumps(tutti, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"Scritto {CANDIDATES_FILE.relative_to(ROOT)} — "
            f"{len(tutti)} candidati in attesa.")

    if non_classificati:
        log("\n" + "=" * 60)
        if verdetti:
            log(f"{len(non_classificati)} candidati sono rimasti senza verdetto e verranno "
                f"ripresi al prossimo giro. Il classificatore ha risposto per gli altri "
                f"{verdetti}: nessun errore.")
        else:
            log(f"ATTENZIONE: il classificatore non ha risposto per {len(non_classificati)} "
                "candidati, che NON sono stati proposti.")
            log("Il giro esce con errore apposta, perche' GitHub lo segnali per email.")
    return codice_uscita(non_classificati, quota, verdetti)


if __name__ == "__main__":
    sys.exit(main())
