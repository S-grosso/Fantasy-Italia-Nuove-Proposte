#!/usr/bin/env python3
"""
Copertine — Fantasy Italia, Nuove Proposte
==========================================

Porta le copertine sotto il nostro tetto: scarica l'immagine dall'indirizzo
attuale (quasi sempre Amazon o il sito dell'editore), la ridimensiona, la
carica su Supabase Storage e aggiorna la scheda.

Serve perche' un indirizzo altrui puo' sparire o rifiutare le richieste
esterne, e quando succede l'anteprima di un link condiviso resta senza
immagine proprio mentre l'autore la sta facendo girare.

Cosa scrive sulla scheda:
    cover_url         -> il nuovo indirizzo, sul nostro Storage
    cover_source_url  -> da dove veniva l'immagine (non si perde mai)
    cover_error       -> il motivo, se il download o il caricamento fallisce

Accanto a ogni copertina prepara una miniatura alta MINI_ALTEZZA pixel e la
salva nel repository, in copertine/mini/<slug>.jpg: e' quella che la home
mostra nelle card, servita da GitHub Pages. A ogni giro crea le miniature
che mancano e toglie quelle dei libri non piu' in catalogo; il workflow le
committa insieme alle pagine.

Serve la service key: le scritture sul catalogo e sullo Storage non passano
dalla chiave pubblica.

Uso:
    python scripts/copertine.py                 # tutte le schede da migrare
    python scripts/copertine.py --limite 3      # prova su poche
    python scripts/copertine.py --dry-run       # scarica e misura, non scrive
    python scripts/copertine.py --rifai         # rifa' anche quelle gia' nostre
"""

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "data" / "copertine-report.json"

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "https://nncnhlbaqnfqtjwembii.supabase.co").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""

BUCKET = "copertine"
PREFISSO_NOSTRO = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/"

# Lato lungo massimo. Una copertina si guarda a 300 px sulla scheda e a poco
# piu' nell'anteprima di un messaggio: oltre mille pixel e' peso inutile, e
# i file troppo pesanti le anteprime li scartano.
LATO_MAX = 1000
QUALITA = 82

# La miniatura per le card della home, che mostrano la copertina alta 160 px
# (130 su telefono). Il doppio basta per gli schermi ad alta densita'.
# Dal 19 novembre 2026: la home caricava 191 copertine intere a ogni visita,
# in media 122 kB l'una, e in pochi giorni il traffico in uscita dallo
# Storage ha superato i 5 GB del piano gratuito. Una miniatura pesa ~17 kB.
# Dal 20 novembre 2026 le miniature stanno nel repository e le serve GitHub
# Pages: il traffico delle card non pesa piu' sulla quota di Supabase.
DIR_MINI = ROOT / "copertine" / "mini"
MINI_ALTEZZA = 320
MINI_QUALITA = 78

TIMEOUT = 30
PAUSA = 0.5  # cortesia verso i server da cui scarichiamo

UA = {"User-Agent": "FantasyItaliaBot/1.0 (+https://www.fantasyitalianuoveproposte.it)"}

CAMPI = "id,slug,title,cover_url,cover_source_url"


# --------------------------------------------------------------------------
# Supabase
# --------------------------------------------------------------------------

def testate():
    return {"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY}


def schede_approvate():
    r = requests.get(
        SUPABASE_URL + "/rest/v1/books",
        params={
            "select": CAMPI,
            "status": "eq.approved",
            "slug": "not.is.null",
            "order": "year.desc,title.asc",
        },
        headers=testate(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def schede_da_migrare(schede, rifai=False):
    libri = []
    for b in schede:
        url = (b.get("cover_url") or "").strip()
        if not url:
            continue
        if url.startswith(PREFISSO_NOSTRO) and not rifai:
            continue
        libri.append(b)
    return libri


def carica_su_storage(nome, dati):
    """Carica (o sostituisce) il file nel bucket e restituisce l'indirizzo pubblico."""
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{nome}",
        headers={
            **testate(),
            "Content-Type": "image/jpeg",
            "x-upsert": "true",
            "cache-control": "max-age=86400",
        },
        data=dati,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return PREFISSO_NOSTRO + nome


def aggiorna_scheda(book_id, campi):
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/books",
        params={"id": "eq." + book_id},
        headers={**testate(), "Content-Type": "application/json", "Prefer": "return=minimal"},
        data=json.dumps(campi),
        timeout=TIMEOUT,
    )
    r.raise_for_status()


# --------------------------------------------------------------------------
# Immagine
# --------------------------------------------------------------------------

def scarica(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    tipo = (r.headers.get("Content-Type") or "").lower()
    if "image" not in tipo:
        raise ValueError(f"la risposta non e' un'immagine ({tipo or 'tipo assente'})")
    return r.content


def apri_rgb(dati):
    img = Image.open(io.BytesIO(dati))
    img.load()
    # Le copertine arrivano anche in PNG con trasparenza: sotto ci mettiamo
    # il bianco, altrimenti in JPEG diventa nero.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGB", img.size, (255, 255, 255))
        fondo.paste(img, mask=img.split()[-1])
        img = fondo
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def ridimensiona(dati):
    """JPEG con lato lungo entro LATO_MAX. Restituisce (byte, larghezza, altezza)."""
    img = apri_rgb(dati)
    lato = max(img.size)
    if lato > LATO_MAX:
        scala = LATO_MAX / float(lato)
        nuova = (max(1, int(img.width * scala)), max(1, int(img.height * scala)))
        img = img.resize(nuova, Image.LANCZOS)

    uscita = io.BytesIO()
    img.save(uscita, format="JPEG", quality=QUALITA, optimize=True, progressive=True)
    return uscita.getvalue(), img.width, img.height


def miniatura(dati):
    """JPEG alto al massimo MINI_ALTEZZA, larghezza in proporzione."""
    img = apri_rgb(dati)
    if img.height > MINI_ALTEZZA:
        scala = MINI_ALTEZZA / float(img.height)
        img = img.resize((max(1, round(img.width * scala)), MINI_ALTEZZA), Image.LANCZOS)
    uscita = io.BytesIO()
    img.save(uscita, format="JPEG", quality=MINI_QUALITA, optimize=True, progressive=True)
    return uscita.getvalue()


def file_nostro(url):
    """
    Il nome del file di una copertina sul nostro Storage ('<slug>.jpg'),
    o None se l'indirizzo e' altrui. La home ricava la miniatura dallo stesso
    nome (/copertine/mini/<nome>), quindi le due regole devono restare
    uguali. Il nome finisce in un percorso su disco: solo lettere minuscole,
    cifre e trattini.
    """
    url = (url or "").strip()
    if not url.startswith(PREFISSO_NOSTRO):
        return None
    nome = url[len(PREFISSO_NOSTRO):]
    return nome if re.fullmatch(r"[a-z0-9][a-z0-9-]*\.jpg", nome) else None


def scrivi_mini(nome, dati):
    DIR_MINI.mkdir(parents=True, exist_ok=True)
    percorso = DIR_MINI / nome
    if percorso.exists() and percorso.read_bytes() == dati:
        return False
    percorso.write_bytes(dati)
    return True


def sistema_miniature(schede, dry_run=False):
    """
    Allinea copertine/mini/ al catalogo: crea le miniature che mancano (dalla
    copertina sul nostro Storage) e toglie quelle dei libri non piu'
    approvati. Restituisce (create, rimosse, fallite).
    """
    attese = {}
    for b in schede:
        nome = file_nostro(b.get("cover_url"))
        if nome:
            attese[nome] = b["cover_url"]

    mancanti = [n for n in attese if not (DIR_MINI / n).exists()]
    create, fallite, prima, dopo = 0, 0, 0, 0
    if mancanti:
        print(f"Miniature da creare: {len(mancanti)}")
    for nome in mancanti:
        try:
            r = requests.get(attese[nome], timeout=TIMEOUT)
            r.raise_for_status()
            mini = miniatura(r.content)
            prima += len(r.content)
            dopo += len(mini)
            if not dry_run:
                scrivi_mini(nome, mini)
            create += 1
        except Exception as errore:
            fallite += 1
            print(f"  MINIATURA FALLITA {nome}: {type(errore).__name__}: {errore}"[:300])
    if create:
        print(f"Miniature {'misurate' if dry_run else 'create'}: {create} — "
              f"in media {prima // create // 1024} kB -> {dopo // create // 1024} kB")

    # Le rimozioni solo con un catalogo vero davanti: una lettura a vuoto
    # non deve cancellare tutte le miniature.
    rimosse = 0
    if attese and DIR_MINI.exists():
        for f in DIR_MINI.glob("*.jpg"):
            if f.name not in attese:
                if not dry_run:
                    f.unlink()
                rimosse += 1
        if rimosse:
            print(f"Miniature {'da togliere' if dry_run else 'tolte'}: {rimosse}")
    if not mancanti and not rimosse:
        print("Miniature: tutte allineate al catalogo.")
    return create, rimosse, fallite


def scrivi_report(dati):
    """
    Scrive il report solo se e' cambiato qualcosa oltre all'orario.

    Prima lo riscriveva a ogni giro con il solo 'quando' aggiornato, e il
    workflow committava ogni sei ore un file identico nella sostanza: tredici
    commit vuoti in quattro giorni, ognuno con il suo nuovo deploy del sito.
    """
    try:
        precedente = json.loads(REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        precedente = None

    def sostanza(d):
        return {k: v for k, v in (d or {}).items() if k != "quando"}

    if precedente is not None and sostanza(precedente) == sostanza(dati):
        return False
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(dati, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# Programma
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Porta le copertine su Supabase Storage.")
    ap.add_argument("--limite", type=int, default=0, help="quante schede trattare (0 = tutte)")
    ap.add_argument("--dry-run", action="store_true", help="scarica e misura, non scrive niente")
    ap.add_argument("--rifai", action="store_true", help="rifa' anche le copertine gia' nostre")
    args = ap.parse_args()

    if not SERVICE_KEY and not args.dry_run:
        print("Manca SUPABASE_SERVICE_KEY: senza non si scrive ne' sul catalogo ne' sullo Storage.")
        return 1

    schede = schede_approvate()
    libri = schede_da_migrare(schede, rifai=args.rifai)
    if args.limite:
        libri = libri[: args.limite]

    fatte, falliti = 0, []
    risparmio = 0

    if libri:
        print(f"Copertine da migrare: {len(libri)}")
    else:
        print("Nessuna copertina da migrare.")

    for b in libri:
        titolo = b.get("title") or b["slug"]
        origine = (b.get("cover_url") or "").strip()
        try:
            grezza = scarica(origine)
            leggera, w, h = ridimensiona(grezza)
            risparmio += max(0, len(grezza) - len(leggera))

            if args.dry_run:
                print(f"  [prova] {titolo}: {len(grezza)//1024} kB -> {len(leggera)//1024} kB ({w}x{h})")
            else:
                nuovo = carica_su_storage(b["slug"] + ".jpg", leggera)
                # La miniatura si rifa' insieme alla copertina: con --rifai o
                # con una copertina corretta, quella vecchia resterebbe nelle
                # card. Se non riesce, la ricrea il passaggio delle mancanti,
                # e nel frattempo la home ripiega sulla copertina intera.
                try:
                    scrivi_mini(b["slug"] + ".jpg", miniatura(grezza))
                except Exception as errore:
                    print(f"  miniatura non caricata per {titolo}: {type(errore).__name__}: {errore}"[:300])
                # La provenienza si scrive una volta sola: se lo script gira di
                # nuovo non deve sovrascriverla col nostro stesso indirizzo.
                campi = {"cover_url": nuovo, "cover_error": None}
                if not b.get("cover_source_url"):
                    campi["cover_source_url"] = origine
                aggiorna_scheda(b["id"], campi)
                print(f"  {titolo}: {len(grezza)//1024} kB -> {len(leggera)//1024} kB ({w}x{h})")
            fatte += 1

        except Exception as errore:
            motivo = f"{type(errore).__name__}: {errore}"[:300]
            print(f"  FALLITA {titolo}: {motivo}")
            falliti.append({"slug": b["slug"], "titolo": titolo, "origine": origine, "motivo": motivo})
            if not args.dry_run:
                try:
                    aggiorna_scheda(b["id"], {"cover_error": motivo})
                except Exception:
                    pass

        time.sleep(PAUSA)

    if libri:
        scrivi_report({"quando": time.strftime("%Y-%m-%d %H:%M"), "migrate": fatte, "falliti": falliti})
        print(f"Migrate: {fatte} — non riuscite: {len(falliti)} — peso risparmiato: {risparmio//1024} kB")
        if falliti:
            print("Da sistemare a mano dalla moderazione:")
            for f in falliti:
                print("  -", f["titolo"], "->", f["motivo"])
    else:
        scrivi_report({"falliti": [], "quando": time.strftime("%Y-%m-%d %H:%M")})

    # Le miniature non toccano il report. Se il passaggio non riesce, la home
    # usa le copertine intere dallo Storage, come prima.
    try:
        if fatte and not args.dry_run:
            schede = schede_approvate()  # ora con gli indirizzi appena migrati
        sistema_miniature(schede, dry_run=args.dry_run)
    except Exception as errore:
        print(f"Miniature non controllate: {type(errore).__name__}: {errore}"[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
