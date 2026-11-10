// Il pulsante "Pubblica le pagine" del pannello di moderazione.
//
// Pagine statiche, copertine e immagini per Instagram le prepara il workflow
// pagine.yml, che GitHub dovrebbe avviare ogni quattro ore. Non lo fa:
// misurato il 10 novembre 2026, salta quasi quattro giri su dieci e gli
// altri partono in media con due ore di ritardo (il 9 novembre otto titoli
// approvati alle 9 sono andati online il giorno dopo). Da qui il moderatore
// lo avvia quando serve, e vede quando il sito e' aggiornato.
//
// Perche' una funzione: avviare un workflow richiede un token GitHub, che
// non deve passare dal browser. Vive nei secrets di questo ambiente: un
// token fine-grained limitato a questo repository, con il solo permesso
// Actions in lettura e scrittura. Al pannello arriva solo lo stato dei giri.
//
// Chi puo' usarla: solo l'amministratore. verify_jwt e' disattivato e il
// controllo si fa qui, chiedendo e_admin() al database con il token di chi
// chiama: un token valido non basta, deve essere dell'amministratore, e
// cosi' la lista degli ammessi resta in un posto solo, quello delle regole
// RLS. La firma del token la verifica il database.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const NOME_SECRET = "GH_PAGINE_TOKEN";
const GH_TOKEN = Deno.env.get(NOME_SECRET) ?? "";

const API = "https://api.github.com/repos/S-grosso/Fantasy-Italia-Nuove-Proposte";
const GIRI_PAGINE = "/actions/workflows/pagine.yml/runs?per_page=10";
// La pubblicazione su Pages non e' un file del repository: e' un workflow
// che GitHub crea da se' e lancia a ogni commit su main, anche a quelli
// dello Scout. Non si lascia chiedere per nome, quindi si filtra l'elenco.
const GIRI_DINAMICI = "/actions/runs?event=dynamic&per_page=10";
const PERCORSO_PAGES = "dynamic/pages/pages-build-deployment";

const IN_ATTESA = ["queued", "waiting", "pending", "requested"];

// La apikey serve solo a farsi ascoltare dal database: chi chiama si
// riconosce dal suo token, non da questa chiave, che e' pubblica. Si prende
// la prima chiave pubblica nuova qualunque sia il suo nome (in questo
// progetto 'new_default', non 'default'): la chiave anon legacy resta solo
// come ripiego, perche' Supabase la spegne a fine 2026.
function chiavePubblica(): string {
  try {
    const chiavi = JSON.parse(Deno.env.get("SUPABASE_PUBLISHABLE_KEYS") ?? "{}");
    const prima = chiavi.default ?? Object.values(chiavi)[0];
    if (typeof prima === "string" && prima) return prima;
  } catch { /* si ripiega sulla chiave anon */ }
  return Deno.env.get("SUPABASE_ANON_KEY") ?? "";
}
const CHIAVE_PUBBLICA = chiavePubblica();

// Come in 'proponi': CORS non protegge niente, la difesa vera e' il
// controllo dell'amministratore. Le origini locali servono a provare il
// pannello prima di pubblicarlo.
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
    "Access-Control-Allow-Headers": "content-type, apikey, authorization, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}

function risposta(origine: string | null, stato: number, corpo: Record<string, unknown>) {
  return new Response(JSON.stringify(corpo), { status: stato, headers: intestazioni(origine) });
}

// --- chi chiama --------------------------------------------------------------

// 200 amministratore, 401 token non valido o scaduto, 403 chiunque altro.
async function controllaAdmin(autorizzazione: string): Promise<200 | 401 | 403> {
  const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/e_admin`, {
    method: "POST",
    headers: {
      apikey: CHIAVE_PUBBLICA,
      Authorization: autorizzazione,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  if (r.status === 401) return 401;
  if (!r.ok) throw new Error(`e_admin ha risposto ${r.status}: ${await r.text()}`);
  return (await r.json()) === true ? 200 : 403;
}

// --- GitHub ------------------------------------------------------------------

class ErroreGitHub extends Error {
  constructor(public codice: number, public dettaglio: string) {
    super(`GitHub ${codice}`);
  }
  // Cosa dire al moderatore: dal telefono deve capire se e' il token da
  // rifare o se basta riprovare.
  get perIlPannello(): string {
    if (this.codice === 401) {
      return `GitHub rifiuta il token: forse è scaduto. Rigeneralo e aggiorna il secret ${NOME_SECRET} su Supabase.`;
    }
    if (this.codice === 403 || this.codice === 404) {
      return "Il token non ha il permesso Actions (lettura e scrittura) su questo repository, oppure GitHub sta limitando le richieste. Riprova fra qualche minuto; se si ripete, controlla il token.";
    }
    if (this.codice === 422) {
      return "GitHub non accetta l'avvio del workflow. Controlla che pagine.yml abbia ancora workflow_dispatch.";
    }
    return `GitHub non risponde come dovrebbe (codice ${this.codice}). Riprova fra poco.`;
  }
}

function github(percorso: string, init: RequestInit = {}) {
  return fetch(`${API}${percorso}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${GH_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "fantasy-italia-pubblica",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
}

type Giro = {
  id: number;
  path: string;
  status: string;
  conclusion: string | null;
  event: string;
  created_at: string;
  run_started_at?: string;
  updated_at: string;
  html_url: string;
};

async function giri(percorso: string): Promise<Giro[]> {
  const r = await github(percorso);
  if (!r.ok) throw new ErroreGitHub(r.status, await r.text());
  return (await r.json()).workflow_runs ?? [];
}

async function pubblicazioni(): Promise<Giro[]> {
  return (await giri(GIRI_DINAMICI)).filter((g) => g.path === PERCORSO_PAGES);
}

function riassunto(g: Giro | null | undefined) {
  if (!g) return null;
  return {
    stato: g.status,
    esito: g.conclusion,
    evento: g.event,
    avviato: g.created_at,
    finito: g.status === "completed" ? g.updated_at : null,
    url: g.html_url,
  };
}

// Il giro prepara le pagine e le committa; il commit fa partire la
// pubblicazione su Pages. Per il moderatore e' una cosa sola, "il sito e'
// aggiornato", quindi le due fasi si leggono insieme.
function fase(giro: Giro | undefined, pub: Giro[], adesso: number) {
  if (!giro) return { fase: "nessuno", testo: "Nessun giro registrato." };

  if (giro.status !== "completed") {
    return {
      fase: "in_corso",
      testo: IN_ATTESA.includes(giro.status) ? "In coda su GitHub…" : "Preparo pagine e copertine…",
    };
  }
  if (giro.conclusion !== "success") {
    return {
      fase: "fallito",
      passo: "giro",
      testo: giro.conclusion === "cancelled"
        ? "Il giro è stato annullato."
        : "Il giro di pagine e copertine non è riuscito.",
    };
  }

  // Il commit e' l'ultimo passo del giro, quindi la sua pubblicazione nasce
  // a ridosso della fine. Una pubblicazione nata prima viene da un commit
  // precedente e non dice niente di questo giro.
  const fine = Date.parse(giro.updated_at);
  const inizio = Date.parse(giro.run_started_at ?? giro.created_at);
  const soglia = Math.max(inizio, fine - 90_000);
  const sua = pub.find((p) => Date.parse(p.created_at) >= soglia);

  if (!sua) {
    // Senza modifiche il giro non committa, e senza commit GitHub non
    // ripubblica: e' l'esito normale quando non c'era niente di nuovo. Si
    // aspetta qualche minuto prima di concluderlo, perche' la pubblicazione
    // puo' comparire nell'elenco con un po' di ritardo.
    if (adesso - fine < 180_000) {
      return { fase: "in_corso", testo: "Aspetto la pubblicazione sul sito…", pubblicazione: null };
    }
    return { fase: "fatto", testo: "Niente di nuovo: il sito era già aggiornato.", pubblicazione: null };
  }
  if (sua.status !== "completed") {
    return { fase: "in_corso", testo: "Pubblico sul sito…", pubblicazione: riassunto(sua) };
  }
  if (sua.conclusion === "success") {
    return { fase: "fatto", testo: "Pagine online.", pubblicazione: riassunto(sua) };
  }
  // Succede: il 10 novembre 2026 la pubblicazione di un giro riuscito e'
  // fallita al primo tentativo. Rifare il giro non serve, perche' non
  // troverebbe niente da committare: va ripetuta la pubblicazione.
  return {
    fase: "fallito",
    passo: "pubblicazione",
    testo: "Le pagine sono pronte, ma GitHub non è riuscito a metterle online.",
    pubblicazione: riassunto(sua),
  };
}

async function leggiStato() {
  const [pagine, pub] = await Promise.all([giri(GIRI_PAGINE), pubblicazioni()]);
  const giro = pagine[0];
  const riuscito = pagine.find((g) => g.status === "completed" && g.conclusion === "success");
  return {
    ok: true,
    giro: riassunto(giro),
    ...fase(giro, pub, Date.now()),
    // Il pannello conta le schede approvate o corrette dopo questo momento.
    // Si usa la fine del giro e non l'inizio: durante il giro copertine.py
    // riscrive l'indirizzo delle copertine nuove, e con l'inizio quelle
    // schede risulterebbero ancora da pubblicare.
    ultimo_riuscito: riuscito ? riuscito.updated_at : null,
  };
}

async function avvia(origine: string | null) {
  const pagine = await giri(GIRI_PAGINE);
  // Un giro in coda legge il catalogo quando parte, quindi comprende gia'
  // le approvazioni di adesso: un secondo avvio non aggiungerebbe niente.
  // Uno gia' in corso invece puo' aver letto il catalogo prima dell'ultima
  // approvazione, e allora si avvia: il concurrency di pagine.yml lo mette
  // in fila dietro a quello in corso, invece di farli lavorare insieme.
  if (pagine.some((g) => IN_ATTESA.includes(g.status))) {
    return risposta(origine, 409, {
      ok: false,
      gia: true,
      messaggio: "C'è già un giro in coda: comprenderà anche le ultime approvazioni.",
    });
  }
  const r = await github("/actions/workflows/pagine.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: "main" }),
  });
  if (!r.ok) throw new ErroreGitHub(r.status, await r.text());
  return risposta(origine, 202, {
    ok: true,
    avviato_alle: new Date().toISOString(),
    messaggio: "Giro avviato: GitHub lo fa partire entro qualche secondo.",
  });
}

async function ripubblica(origine: string | null) {
  const ultima = (await pubblicazioni())[0];
  if (!ultima || ultima.status !== "completed" || ultima.conclusion === "success") {
    return risposta(origine, 409, {
      ok: false,
      messaggio: "L'ultima pubblicazione non è fallita: non c'è niente da riprovare.",
    });
  }
  const r = await github(`/actions/runs/${ultima.id}/rerun-failed-jobs`, { method: "POST" });
  if (!r.ok) throw new ErroreGitHub(r.status, await r.text());
  return risposta(origine, 202, { ok: true, messaggio: "Pubblicazione rilanciata." });
}

// --- richiesta ---------------------------------------------------------------

Deno.serve(async (req: Request) => {
  const origine = req.headers.get("origin");

  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: intestazioni(origine) });
  }
  if (req.method !== "POST") {
    return risposta(origine, 405, { ok: false, messaggio: "Metodo non ammesso." });
  }

  // Senza sessione il client di Supabase manda la chiave pubblica al posto
  // del token: non e' un JWT, e la si rifiuta subito.
  const autorizzazione = req.headers.get("authorization") ?? "";
  if (!/^Bearer eyJ/.test(autorizzazione)) {
    return risposta(origine, 401, { ok: false, messaggio: "Serve l'accesso al pannello." });
  }
  if (Number(req.headers.get("content-length") || "0") > 1024) {
    return risposta(origine, 413, { ok: false, messaggio: "Richiesta troppo lunga." });
  }

  try {
    const chi = await controllaAdmin(autorizzazione);
    if (chi === 401) {
      return risposta(origine, 401, { ok: false, messaggio: "Sessione scaduta: esci e rientra nel pannello." });
    }
    if (chi === 403) {
      return risposta(origine, 403, { ok: false, messaggio: "Solo l'amministratore può pubblicare le pagine." });
    }

    let azione = "";
    try {
      azione = String((await req.json())?.azione ?? "");
    } catch {
      return risposta(origine, 400, { ok: false, messaggio: "Richiesta non leggibile." });
    }

    if (!GH_TOKEN) {
      console.error(`manca il secret ${NOME_SECRET}`);
      return risposta(origine, 500, {
        ok: false,
        messaggio: `Manca il token GitHub: aggiungi il secret ${NOME_SECRET} nelle Edge Functions di Supabase.`,
      });
    }

    if (azione === "stato") return risposta(origine, 200, await leggiStato());
    if (azione === "avvia") return await avvia(origine);
    if (azione === "ripubblica") return await ripubblica(origine);
    return risposta(origine, 400, { ok: false, messaggio: "Azione sconosciuta." });
  } catch (errore) {
    if (errore instanceof ErroreGitHub) {
      // Il dettaglio di GitHub resta nel registro della funzione: al
      // pannello basta sapere cosa fare.
      console.error("GitHub", errore.codice, errore.dettaglio.slice(0, 500));
      return risposta(origine, 502, { ok: false, messaggio: errore.perIlPannello });
    }
    console.error("errore inatteso", errore);
    return risposta(origine, 500, { ok: false, messaggio: "Qualcosa è andato storto. Riprova fra poco." });
  }
});
