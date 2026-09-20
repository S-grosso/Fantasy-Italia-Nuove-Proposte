// Proposte dal modulo pubblico "Nuovo titolo".
//
// Perche' una funzione e non una scrittura diretta dal sito: il catalogo e'
// leggibile da chiunque ma scrivibile da nessuno, e deve restare cosi'. Qui
// la scrittura avviene con la service key, che vive solo in questo ambiente
// e non passa mai dal browser. Il pubblico non tocca la tabella: parla con
// questa funzione, che decide cosa entra.
//
// verify_jwt e' disattivato apposta: chi propone non ha un account, e la
// chiave pubblica del sito e' del formato sb_publishable_..., che non e' un
// JWT e verrebbe rifiutata. Il controllo lo fanno la validazione, il
// campo-esca e il limite di frequenza qui sotto.
//
// Nota sui dati personali: non si registra l'indirizzo IP di chi propone.
// Il limite di frequenza guarda solo quello che e' gia' in tabella, cosi'
// il sito continua a non raccogliere niente sui visitatori.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const REST = `${SUPABASE_URL}/rest/v1/books`;

const TESTATE = {
  apikey: SERVICE_KEY,
  Authorization: `Bearer ${SERVICE_KEY}`,
  "Content-Type": "application/json",
};

// Origini ammesse. Quelle locali servono a provare il modulo prima di
// pubblicarlo: CORS non e' una barriera di sicurezza (chiunque puo' chiamare
// la funzione con curl), quindi lasciarle costa poco. Le difese vere sono
// piu' sotto.
function origineAmmessa(origine: string | null): string | null {
  if (!origine) return null;
  if (
    origine === "https://www.fantasyitalianuoveproposte.it" ||
    origine === "https://fantasyitalianuoveproposte.it" ||
    /^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(origine)
  ) return origine;
  return null;
}

function intestazioni(origine: string | null) {
  return {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": origineAmmessa(origine) ?? "https://www.fantasyitalianuoveproposte.it",
    // apikey e authorization non servono (verify_jwt e' disattivato), ma
    // i client Supabase le mandano di prassi: se non sono dichiarate qui,
    // il browser blocca la richiesta nel preflight e il modulo sembra
    // rotto senza che la funzione venga nemmeno raggiunta.
    "Access-Control-Allow-Headers": "content-type, apikey, authorization, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}

function risposta(origine: string | null, stato: number, corpo: Record<string, unknown>) {
  return new Response(JSON.stringify(corpo), { status: stato, headers: intestazioni(origine) });
}

// --- limiti dei campi -------------------------------------------------------
// Generosi ma finiti: servono a impedire che un invio gonfi la tabella, non
// a fare i pignoli con chi scrive.
const MAX = {
  titolo: 200,
  autore: 120,
  editore: 120,
  isbn: 20,
  link: 500,
  contatto: 160,
  note: 1000,
};

function pulisci(valore: unknown, limite: number): string {
  return String(valore ?? "").replace(/\s+/g, " ").trim().slice(0, limite);
}

function emailPlausibile(valore: string): boolean {
  // Non si valida un'email con una regex, si valida spedendoci sopra. Qui
  // serve solo a fermare gli errori di battitura evidenti.
  return /^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(valore);
}

function isbnNormalizzato(grezzo: string): string {
  return grezzo.replace(/[^0-9Xx]/g, "").toUpperCase();
}

// Le virgole e i punti nei valori non sono un problema: encodeURIComponent
// li codifica e PostgREST li rilegge come testo. Racchiuderli fra virgolette
// sarebbe invece sbagliato, perche' le virgolette entrerebbero nel confronto
// e non troverebbero mai niente (provato sul database).
// Quello che va protetto sono i caratteri jolly di ilike: senza, un titolo
// che contenesse % o _ pescherebbe schede che non c'entrano.
function letterale(valore: string): string {
  return valore.replace(/([\\%_])/g, "\\$1");
}

async function conta(parametri: string): Promise<number> {
  const r = await fetch(`${REST}?${parametri}`, {
    headers: { ...TESTATE, Prefer: "count=exact", Range: "0-0" },
  });
  const intervallo = r.headers.get("content-range") || "";
  const totale = intervallo.split("/")[1];
  return Number(totale) || 0;
}

Deno.serve(async (req: Request) => {
  const origine = req.headers.get("origin");

  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: intestazioni(origine) });
  }
  if (req.method !== "POST") {
    return risposta(origine, 405, { ok: false, messaggio: "Metodo non ammesso." });
  }

  // Un corpo enorme si scarta prima di leggerlo tutto.
  const lunghezza = Number(req.headers.get("content-length") || "0");
  if (lunghezza > 8192) {
    return risposta(origine, 413, { ok: false, messaggio: "Messaggio troppo lungo." });
  }

  let dati: Record<string, unknown>;
  try {
    dati = await req.json();
  } catch {
    return risposta(origine, 400, { ok: false, messaggio: "Richiesta non leggibile." });
  }

  // Campo-esca: e' nascosto via CSS, una persona non lo vede e non lo
  // compila. Se arriva pieno e' un programma automatico. Si risponde come
  // se fosse andato tutto bene: dire "sei un bot" serve solo a fargli
  // correggere il tiro al tentativo dopo.
  if (pulisci(dati.sito, 50) !== "") {
    return risposta(origine, 200, { ok: true, messaggio: "Proposta ricevuta. La guardo appena posso." });
  }

  const titolo = pulisci(dati.titolo, MAX.titolo);
  const autore = pulisci(dati.autore, MAX.autore);
  const editore = pulisci(dati.editore, MAX.editore);
  const contatto = pulisci(dati.contatto, MAX.contatto);
  const note = pulisci(dati.note, MAX.note);
  const link = pulisci(dati.link, MAX.link);
  const isbn = isbnNormalizzato(pulisci(dati.isbn, MAX.isbn));

  const annoGrezzo = parseInt(String(dati.anno ?? ""), 10);
  const annoAttuale = new Date().getFullYear();
  const anno = Number.isFinite(annoGrezzo) &&
      annoGrezzo >= 1900 && annoGrezzo <= annoAttuale + 2
    ? annoGrezzo
    : null;

  if (!titolo || !autore || !editore) {
    return risposta(origine, 400, { ok: false, messaggio: "Servono almeno titolo, autore ed editore." });
  }
  if (!emailPlausibile(contatto)) {
    return risposta(origine, 400, { ok: false, messaggio: "Serve un'email valida per poterti rispondere." });
  }
  if (link && !/^https?:\/\//i.test(link)) {
    return risposta(origine, 400, { ok: false, messaggio: "Il link deve cominciare con http:// o https://" });
  }

  try {
    // --- limite di frequenza ------------------------------------------------
    // Due soglie: una sul totale, che ferma una raffica da qualunque parte
    // arrivi, e una sullo stesso contatto, che ferma chi insiste. Numeri
    // pensati per un sito che riceve qualche proposta a settimana: se un
    // giorno diventassero stretti, si alzano qui.
    const unOraFa = new Date(Date.now() - 3600_000).toISOString();
    const unGiornoFa = new Date(Date.now() - 86_400_000).toISOString();

    const nellUltimaOra = await conta(
      `select=id&source=eq.proposta%20dal%20sito&created_at=gt.${unOraFa}`,
    );
    if (nellUltimaOra >= 10) {
      return risposta(origine, 429, {
        ok: false,
        messaggio: "Sto ricevendo troppe proposte in questo momento. Riprova fra un'ora.",
      });
    }

    const dalloStessoContatto = await conta(
      `select=id&source=eq.proposta%20dal%20sito&created_at=gt.${unGiornoFa}` +
        `&proposer_contact=eq.${encodeURIComponent(contatto)}`,
    );
    if (dalloStessoContatto >= 3) {
      return risposta(origine, 429, {
        ok: false,
        messaggio: "Hai già inviato tre proposte oggi: le guardo e poi ne riparliamo.",
      });
    }

    // --- gia' noto? ---------------------------------------------------------
    // Si guarda in tutti gli stati, anche fra gli scarti: se un titolo e'
    // stato valutato e rifiutato, riproporlo non deve rimetterlo in coda.
    let gia = 0;
    if (isbn.length >= 10) {
      // Si confronta isbn_norm, la colonna che il database ricava tenendo
      // le sole cifre: in tabella gli ISBN convivono in formati diversi
      // ('979-1280868190' accanto a '9788825425666') e un confronto sul
      // valore scritto a mano si perderebbe i doppioni a seconda di come
      // chi propone mette i trattini.
      gia = await conta(`select=id&isbn_norm=eq.${encodeURIComponent(isbn)}`);
    }
    if (!gia) {
      gia = await conta(
        `select=id&title=ilike.${encodeURIComponent(letterale(titolo))}` +
          `&author=ilike.${encodeURIComponent(letterale(autore))}`,
      );
    }
    if (gia) {
      return risposta(origine, 200, {
        ok: false,
        gia: true,
        messaggio: "Questo titolo risulta già censito o già in valutazione. Se trovi un errore nella scheda, scrivimi dai Contatti.",
      });
    }

    // --- scrittura ----------------------------------------------------------
    // Stato 'candidate': entra nella stessa coda dei titoli trovati dallo
    // Scout, quindi non compare da nessuna parte finche' non lo approvi.
    const riga = {
      id: "p" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      title: titolo,
      author: autore,
      publisher: editore,
      year: anno,
      isbn: isbn,
      store_publisher_url: link,
      status: "candidate",
      source: "proposta dal sito",
      reason: note ? `Nota di chi propone: ${note}` : "",
      proposer_contact: contatto,
    };

    const scrittura = await fetch(REST, {
      method: "POST",
      headers: { ...TESTATE, Prefer: "return=minimal" },
      body: JSON.stringify(riga),
    });

    if (!scrittura.ok) {
      console.error("scrittura fallita", scrittura.status, await scrittura.text());
      return risposta(origine, 502, {
        ok: false,
        messaggio: "Non sono riuscito a registrare la proposta. Riprova fra poco.",
      });
    }

    return risposta(origine, 200, {
      ok: true,
      messaggio: "Proposta ricevuta. La leggo io prima che compaia in catalogo, quindi non è immediata: se serve ti scrivo all'indirizzo che hai lasciato.",
    });
  } catch (errore) {
    console.error("errore inatteso", errore);
    return risposta(origine, 500, {
      ok: false,
      messaggio: "Qualcosa è andato storto. Riprova fra poco.",
    });
  }
});
