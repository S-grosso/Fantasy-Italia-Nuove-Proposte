#!/usr/bin/env python3
"""
Immagini per i social — Fantasy Italia, Nuove Proposte
======================================================

Per ogni scheda approvata prepara un'immagine 1080x1350 con copertina,
titolo, autore e marchio del sito, la carica sullo Storage e ne scrive
l'indirizzo sulla scheda.

Serve perche' su Instagram i link non producono anteprime: chi vuole far
sapere che il suo libro e' in catalogo ha bisogno di un'immagine da
pubblicare, e il kit autore gliela manda gia' pronta. 1080x1350 e' il
formato 4:5, il piu' alto che Instagram mostra per intero nel feed.

Un'immagine si rifa' solo quando cambia qualcosa che vi compare, o il
disegno stesso: l'impronta di quei dati sta in social_firma. Cosi' lo
script gira ogni sei ore senza ricaricare niente se niente e' cambiato.

Uso:
    python scripts/immagini_social.py                         # quelle da fare
    python scripts/immagini_social.py --limite 3              # prova su poche
    python scripts/immagini_social.py --rifai                 # tutte da capo
    python scripts/immagini_social.py --slug il-priore-oscuro
    python scripts/immagini_social.py --dry-run --cartella C:\\prove
        # le scrive su disco senza caricare niente: non serve la service key
"""

import argparse
import hashlib
import io
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "https://nncnhlbaqnfqtjwembii.supabase.co").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""
# In prova basta la chiave pubblica: legge le schede approvate come il sito.
CHIAVE_PUBBLICA = "sb_publishable_0prYsjRCftybZ7uQ7tTryA_D5N2r9kQ"

BUCKET = "copertine"
CARTELLA = "instagram"
PREFISSO_COPERTINE = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/"
PREFISSO_SOCIAL = f"{PREFISSO_COPERTINE}{CARTELLA}/"

# Se cambia il disegno, si alza questo numero: tutte le impronte cambiano e
# al giro dopo le immagini si rifanno da sole.
VERSIONE_DISEGNO = "2"

LARGHEZZA, ALTEZZA = 1080, 1350
MARGINE = 70
QUALITA = 88
TIMEOUT = 30
PAUSA = 0.3

# I colori del sito, cosi' l'immagine si riconosce quando si arriva al catalogo.
FONDO = (15, 16, 19)
INCHIOSTRO = (231, 233, 238)
SPENTO = (169, 173, 187)
ACCENTO = (122, 166, 255)
BORDO = (42, 46, 64)

# DejaVu c'e' sulle macchine delle GitHub Actions (il workflow lo installa se
# manca). In locale, su Windows, Verdana ha larghezze simili: l'anteprima che
# si guarda sul proprio computer somiglia a quella che andra' online.
CARATTERI = {
    "grassetto": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        r"C:\Windows\Fonts\verdanab.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    ],
    "normale": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\verdana.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ],
}

CAMPI = "id,slug,title,author,publisher,year,cover_url,is_debut"

UA = {"User-Agent": "FantasyItaliaBot/1.0 (+https://www.fantasyitalianuoveproposte.it)"}


# --------------------------------------------------------------------------
# Caratteri e testo
# --------------------------------------------------------------------------

_cache_caratteri = {}


def carattere(peso, misura):
    chiave = (peso, misura)
    if chiave not in _cache_caratteri:
        for percorso in CARATTERI[peso]:
            if Path(percorso).exists():
                _cache_caratteri[chiave] = ImageFont.truetype(percorso, misura)
                break
        else:
            sys.exit(f"Nessun carattere '{peso}' disponibile: installa fonts-dejavu-core.")
    return _cache_caratteri[chiave]


def a_capo(testo, font, larghezza):
    """Spezza il testo in righe che stanno nella larghezza data."""
    righe, corrente = [], ""
    for parola in str(testo or "").split():
        prova = (corrente + " " + parola).strip()
        if font.getlength(prova) <= larghezza:
            corrente = prova
        else:
            if corrente:
                righe.append(corrente)
            corrente = parola
    if corrente:
        righe.append(corrente)
    return righe


def bilanciate(testo, font, larghezza, n):
    """
    Le stesse parole in al piu' n righe, ma di lunghezza simile. Andando a
    capo il piu' tardi possibile, l'ultima riga resta spesso con una parola
    sola ("Cronache del palazzo di / Lava"): qui si cerca la larghezza piu'
    stretta che ci sta ancora in n righe, e a quella si va a capo.
    """
    if len(a_capo(testo, font, larghezza)) > n:
        return None
    basso = int(max(font.getlength(p) for p in str(testo).split()))
    alto = int(larghezza)
    while basso < alto:
        medio = (basso + alto) // 2
        if len(a_capo(testo, font, medio)) <= n:
            alto = medio
        else:
            basso = medio + 1
    return a_capo(testo, font, alto)


def titolo_che_sta(testo, larghezza, max_righe=3):
    """
    La misura del titolo. I titoli vanno da due parole a quasi sessanta
    caratteri: una misura fissa andrebbe bene solo per meta'.

    Meno righe e' meglio, purche' il carattere resti grande: una riga sola
    fino a 50 px, due fino a 44, tre solo per i titoli che non ci stanno.
    """
    for righe_max, minima in ((1, 50), (2, 44), (max_righe, 38)):
        for misura in range(60, minima - 1, -2):
            font = carattere("grassetto", misura)
            righe = bilanciate(testo, font, larghezza, righe_max)
            if righe:
                return font, righe
    font = carattere("grassetto", 38)
    righe = a_capo(testo, font, larghezza)
    # Anche alla misura minima non sta: si tronca l'ultima riga.
    righe = righe[:max_righe]
    while righe and font.getlength(righe[-1] + "…") > larghezza:
        righe[-1] = righe[-1].rsplit(" ", 1)[0] if " " in righe[-1] else righe[-1][:-1]
    righe[-1] += "…"
    return font, righe


def che_sta(testo, peso, misura, minima, larghezza):
    """
    La misura piu' grande, fra misura e minima, a cui il testo sta su una riga.
    Autore ed editore non vanno a capo: con piu' autori ("Fiore Manni, Michele
    Monteleone") il carattere si stringe invece di uscire dall'immagine.
    """
    for m in range(misura, minima - 1, -2):
        font = carattere(peso, m)
        if font.getlength(str(testo)) <= larghezza:
            return font
    return carattere(peso, minima)


def altezza_riga(font):
    return int(font.size * 1.22)


# --------------------------------------------------------------------------
# Disegno
# --------------------------------------------------------------------------

def sfondo_da_copertina(copertina):
    """La copertina stessa, sfocata e scurita: ogni immagine ha i suoi colori."""
    rapporto = max(LARGHEZZA / copertina.width, ALTEZZA / copertina.height)
    grande = copertina.resize(
        (int(copertina.width * rapporto) + 1, int(copertina.height * rapporto) + 1),
        Image.LANCZOS,
    )
    x = (grande.width - LARGHEZZA) // 2
    y = (grande.height - ALTEZZA) // 2
    ritaglio = grande.crop((x, y, x + LARGHEZZA, y + ALTEZZA)).filter(ImageFilter.GaussianBlur(40))
    return Image.blend(ritaglio, Image.new("RGB", (LARGHEZZA, ALTEZZA), FONDO), 0.80)


def angoli_arrotondati(img, raggio):
    maschera = Image.new("L", img.size, 0)
    ImageDraw.Draw(maschera).rounded_rectangle((0, 0, img.width - 1, img.height - 1), raggio, fill=255)
    return maschera


def componi(copertina, b):
    tela = sfondo_da_copertina(copertina)
    disegno = ImageDraw.Draw(tela)
    centro = LARGHEZZA // 2
    utile = LARGHEZZA - 2 * MARGINE

    # --- blocco del marchio, ancorato in basso ---
    f_piccolo = carattere("normale", 26)
    f_marchio = carattere("grassetto", 32)
    marchio = [
        ("Nel catalogo di", f_piccolo, SPENTO),
        ("Fantasy Italia — Nuove proposte", f_marchio, INCHIOSTRO),
        ("fantasyitalianuoveproposte.it", f_piccolo, ACCENTO),
    ]
    alto_marchio = sum(altezza_riga(f) for _, f, _ in marchio)
    y_marchio = ALTEZZA - MARGINE - alto_marchio

    # --- blocco del libro: esordio, titolo, autore, editore e anno ---
    f_titolo, righe_titolo = titolo_che_sta(b.get("title") or "", utile)
    dati = " · ".join(str(x) for x in [b.get("publisher"), b.get("year")] if x)
    f_autore = che_sta(b.get("author") or "", "normale", 36, 24, utile)
    f_dati = che_sta(dati, "normale", 28, 20, utile)

    blocco = []  # (testo, font, colore, spazio_dopo, e_pastiglia)
    if b.get("is_debut") is True:
        blocco.append(("ESORDIO", carattere("grassetto", 24), ACCENTO, 18, True))
    for i, riga in enumerate(righe_titolo):
        blocco.append((riga, f_titolo, INCHIOSTRO, 14 if i == len(righe_titolo) - 1 else 0, False))
    if b.get("author"):
        blocco.append((b["author"], f_autore, SPENTO, 6, False))
    if dati:
        blocco.append((dati, f_dati, SPENTO, 0, False))

    alto_blocco = sum(altezza_riga(f) + dopo for _, f, _, dopo, _ in blocco)
    y_blocco = y_marchio - 56 - alto_blocco

    # --- la copertina prende tutto lo spazio che resta sopra ---
    spazio_alto = y_blocco - 48 - MARGINE
    scala = min(620 / copertina.width, spazio_alto / copertina.height)
    w, h = int(copertina.width * scala), int(copertina.height * scala)
    x0, y0 = centro - w // 2, MARGINE + (spazio_alto - h) // 2

    # ombra morbida sotto la copertina, perche' si stacchi dal fondo
    ombra = Image.new("L", (LARGHEZZA, ALTEZZA), 0)
    ImageDraw.Draw(ombra).rounded_rectangle((x0 + 6, y0 + 18, x0 + w + 6, y0 + h + 18), 18, fill=170)
    ombra = ombra.filter(ImageFilter.GaussianBlur(22))
    tela.paste(Image.new("RGB", (LARGHEZZA, ALTEZZA), (0, 0, 0)), (0, 0), ombra)

    piccola = copertina.resize((w, h), Image.LANCZOS)
    tela.paste(piccola, (x0, y0), angoli_arrotondati(piccola, 14))
    disegno.rounded_rectangle((x0, y0, x0 + w - 1, y0 + h - 1), 14, outline=BORDO, width=2)

    # --- testi ---
    y = y_blocco
    for testo, font, colore, dopo, pastiglia in blocco:
        if pastiglia:
            # il contrassegno dell'esordio e' una pastiglia, come sul sito
            larg = int(font.getlength(testo)) + 44
            disegno.rounded_rectangle(
                (centro - larg // 2, y - 4, centro + larg // 2, y + altezza_riga(font) + 4),
                radius=20, outline=ACCENTO, width=2,
            )
            disegno.text((centro, y + 4), testo, font=font, fill=colore, anchor="mt")
        else:
            disegno.text((centro, y), testo, font=font, fill=colore, anchor="mt")
        y += altezza_riga(font) + dopo

    # filo sottile fra libro e marchio
    disegno.line((centro - 60, y_marchio - 28, centro + 60, y_marchio - 28), fill=BORDO, width=2)

    y = y_marchio
    for testo, font, colore in marchio:
        disegno.text((centro, y), testo, font=font, fill=colore, anchor="mt")
        y += altezza_riga(font)

    return tela


# --------------------------------------------------------------------------
# Dati e Storage
# --------------------------------------------------------------------------

def firma(b):
    """Impronta di tutto cio' che compare nell'immagine, piu' la versione del disegno."""
    parti = [VERSIONE_DISEGNO, b.get("title"), b.get("author"), b.get("publisher"),
             b.get("year"), b.get("cover_url"), b.get("is_debut")]
    return hashlib.sha1("|".join(str(p) for p in parti).encode("utf-8")).hexdigest()[:12]


def testate(chiave):
    return {"apikey": chiave, "Authorization": "Bearer " + chiave}


def schede(con_chiave):
    campi = CAMPI + (",social_firma" if con_chiave else "")
    chiave = SERVICE_KEY if con_chiave else CHIAVE_PUBBLICA
    r = requests.get(
        SUPABASE_URL + "/rest/v1/books",
        params={"select": campi, "status": "eq.approved", "slug": "not.is.null", "order": "title.asc"},
        headers=testate(chiave), timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def carica(nome, dati):
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{CARTELLA}/{nome}",
        headers={**testate(SERVICE_KEY), "Content-Type": "image/jpeg",
                 "x-upsert": "true", "cache-control": "max-age=86400"},
        data=dati, timeout=TIMEOUT,
    )
    r.raise_for_status()


def aggiorna(book_id, campi):
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/books",
        params={"id": "eq." + book_id},
        headers={**testate(SERVICE_KEY), "Content-Type": "application/json", "Prefer": "return=minimal"},
        json=campi, timeout=TIMEOUT,
    )
    r.raise_for_status()


# --------------------------------------------------------------------------
# Programma
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Prepara le immagini per Instagram.")
    ap.add_argument("--limite", type=int, default=0, help="quante schede trattare (0 = tutte)")
    ap.add_argument("--slug", help="solo questa scheda")
    ap.add_argument("--rifai", action="store_true", help="rifa' anche quelle gia' aggiornate")
    ap.add_argument("--dry-run", action="store_true", help="scrive su disco, non carica niente")
    ap.add_argument("--cartella", help="dove scrivere le immagini con --dry-run")
    args = ap.parse_args()

    if args.dry_run and not args.cartella:
        sys.exit("Con --dry-run serve --cartella: le prove non devono finire nel repository.")
    if not args.dry_run and not SERVICE_KEY:
        print("Manca SUPABASE_SERVICE_KEY: senza non si carica niente. Per provare usa --dry-run.")
        return 1

    elenco = schede(con_chiave=bool(SERVICE_KEY))
    if args.slug:
        elenco = [b for b in elenco if b.get("slug") == args.slug]

    da_fare = []
    for b in elenco:
        copertina = (b.get("cover_url") or "").strip()
        # Solo copertine gia' sul nostro Storage: un indirizzo altrui puo'
        # sparire, e quando copertine.py lo migra l'impronta cambia comunque.
        if not copertina.startswith(PREFISSO_COPERTINE):
            continue
        if not args.rifai and not args.dry_run and b.get("social_firma") == firma(b):
            continue
        da_fare.append(b)
    if args.limite:
        da_fare = da_fare[: args.limite]

    if not da_fare:
        print("Immagini per i social: tutte aggiornate.")
        return 0

    print(f"Immagini per i social da preparare: {len(da_fare)}")
    fatte, fallite = 0, []
    for b in da_fare:
        try:
            r = requests.get(b["cover_url"], headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            copertina = Image.open(io.BytesIO(r.content)).convert("RGB")

            immagine = componi(copertina, b)
            uscita = io.BytesIO()
            immagine.save(uscita, format="JPEG", quality=QUALITA, optimize=True, progressive=True)
            dati = uscita.getvalue()
            impronta = firma(b)

            if args.dry_run:
                percorso = Path(args.cartella) / f"{b['slug']}.jpg"
                percorso.parent.mkdir(parents=True, exist_ok=True)
                percorso.write_bytes(dati)
                print(f"  [prova] {b['slug']}: {len(dati) // 1024} kB -> {percorso}")
            else:
                nome = f"{b['slug']}.jpg"
                carica(nome, dati)
                # ?v= cambia con l'impronta: l'indirizzo nuovo scavalca le
                # cache che conservano ancora l'immagine precedente.
                aggiorna(b["id"], {"social_url": f"{PREFISSO_SOCIAL}{nome}?v={impronta}",
                                   "social_firma": impronta})
                print(f"  {b['slug']}: {len(dati) // 1024} kB")
            fatte += 1
        except Exception as errore:
            motivo = f"{type(errore).__name__}: {errore}"[:200]
            print(f"  FALLITA {b.get('slug')}: {motivo}")
            fallite.append(b.get("slug"))
        time.sleep(PAUSA)

    print(f"Preparate: {fatte} — non riuscite: {len(fallite)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
