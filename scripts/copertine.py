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

TIMEOUT = 30
PAUSA = 0.5  # cortesia verso i server da cui scarichiamo

UA = {"User-Agent": "FantasyItaliaBot/1.0 (+https://www.fantasyitalianuoveproposte.it)"}

CAMPI = "id,slug,title,cover_url,cover_source_url"


# --------------------------------------------------------------------------
# Supabase
# --------------------------------------------------------------------------

def testate():
    return {"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY}


def schede_da_migrare(rifai=False):
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
    libri = []
    for b in r.json():
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


def ridimensiona(dati):
    """JPEG con lato lungo entro LATO_MAX. Restituisce (byte, larghezza, altezza)."""
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

    lato = max(img.size)
    if lato > LATO_MAX:
        scala = LATO_MAX / float(lato)
        nuova = (max(1, int(img.width * scala)), max(1, int(img.height * scala)))
        img = img.resize(nuova, Image.LANCZOS)

    uscita = io.BytesIO()
    img.save(uscita, format="JPEG", quality=QUALITA, optimize=True, progressive=True)
    return uscita.getvalue(), img.width, img.height


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

    libri = schede_da_migrare(rifai=args.rifai)
    if args.limite:
        libri = libri[: args.limite]

    if not libri:
        print("Nessuna copertina da migrare.")
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"falliti": [], "quando": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    print(f"Copertine da migrare: {len(libri)}")

    fatte, falliti = 0, []
    risparmio = 0

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

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {"quando": time.strftime("%Y-%m-%d %H:%M"), "migrate": fatte, "falliti": falliti},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Migrate: {fatte} — non riuscite: {len(falliti)} — peso risparmiato: {risparmio//1024} kB")
    if falliti:
        print("Da sistemare a mano dalla moderazione:")
        for f in falliti:
            print("  -", f["titolo"], "->", f["motivo"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
