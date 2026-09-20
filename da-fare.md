# Fantasy Italia — Nuove Proposte · Stato e da fare

Documento di lavoro. Aggiornato: 20 settembre 2026, dopo una verifica riga per riga di codice, sito pubblicato e database.
Repository: `S-grosso/Fantasy-Italia-Nuove-Proposte` — ramo `main` — sito pubblicato su GitHub Pages al dominio `www.fantasyitalianuoveproposte.it`.

---

## 1. Obiettivi

Due, dichiarati a settembre 2026:

1. Rendere la piattaforma usata e trafficata, per dare visibilità al fantasy italiano.
2. Trasformarla nel tempo in un hobby redditizio: news, interviste, recensioni, collaborazioni.

Il vantaggio competitivo del sito è il dato: un elenco strutturato e aggiornato degli esordi fantasy italiani, con editore, anno, sottogenere. I dati AIE sul fantasy esistono solo aggregati e non distinguono autori italiani da traduzioni né isolano gli esordi. Il modello di riferimento è Locus negli Stati Uniti: registra tutto quello che esce, lo misura, ogni anno lo premia.

Vincolo di fondo: un solo moderatore, tempo da hobby. Ogni scelta va valutata per quanto lavoro ricorrente genera.

---

## 2. Stack

- **Dati**: Supabase (`https://nncnhlbaqnfqtjwembii.supabase.co`), tabelle `books` (`candidate`/`draft`/`approved`/`rejected`) e `news` (`draft`/`published`), più `scout_runs`. RLS con funzione `e_admin()` su allowlist di email; autenticazione Supabase Auth.
- **Sito**: HTML/CSS/JS statico, nessun framework, nessun passaggio di build. `index.html` contiene catalogo, notizie e scheda libro; `catalogo-admin.html` e `news-admin.html` sono i pannelli di moderazione, ottimizzati per telefono.
- **Automazione**: GitHub Actions. `scout.yml` (settimanale) esegue `scripts/scout.py`, che scopre le uscite da 14 editori monitorati e le classifica con GitHub Models. `pagine.yml` (ogni 6 ore) esegue `scripts/copertine.py` e `scripts/genera_pagine.py`.
- **Niente cookie, niente analytics, nessun dato raccolto dai visitatori.** Il controllo del traffico passa da Google Search Console.

---

## 3. Fatto (settembre 2026)

- [x] **Slug per i libri.** Colonna `slug` su `books`, indice univoco, trigger `books_assign_slug()` che la riempie solo alle schede `approved`, non la cambia più dopo l'assegnazione e risolve le omonimie con l'autore o un progressivo. Funzione di appoggio `fi_slugify()` con estensione `unaccent`. Backfill eseguito: 52 schede approvate, 52 slug distinti.
- [x] **Scheda libro in rilievo** in `index.html`: elemento `<dialog>` nativo, sinossi completa dai dati già in memoria, nessuna nuova chiamata di rete, indirizzo gestito con History API su `/libri/<slug>/`. Chiusura con pulsante, Esc, tocco fuori o tasto indietro; il tasto avanti riapre. Copertina e titolo sono collegamenti veri alla pagina statica. Pulsante "Copia link".
- [x] **`404.html`** come ripiego: intercetta `/libri/<slug>/` non ancora generati e rimanda a `/?libro=<slug>`, dove la scheda si apre da sola.
- [x] **Generatore di pagine statiche** `scripts/genera_pagine.py`: una pagina per libro con title, description, canonical, Open Graph completo, Twitter card e JSON-LD `schema.org/Book`; indice `libri/index.html`; `sitemap.xml`; `robots.txt`. Scrive solo i file cambiati, rimuove le cartelle orfane, convalida gli slug prima di toccare il disco, escapa sinossi e JSON-LD.
- [x] **Workflow `pagine.yml`**: ogni 6 ore e a mano da Actions; commit solo se qualcosa è cambiato; `git pull --rebase` prima del push per non litigare con Scout.
- [x] **Copertine su Supabase Storage.** Bucket pubblico `copertine` (max 5 MB, solo jpeg/png/webp), colonne `cover_source_url` e `cover_error` su `books`, script `scripts/copertine.py` che scarica, ridimensiona a 1000 px di lato lungo in JPEG 82, carica come `<slug>.jpg` e aggiorna la scheda. Integrato in `pagine.yml` come primo passaggio, con `continue-on-error`.
- [x] **Note legali** riscritte: paragrafo su copertine e diritti, rimozione su richiesta, sezione dati personali allineata alla realtà (GitHub e Supabase registrano i dati tecnici di connessione).
- [x] **Search Console**: proprietà verificata, `sitemap.xml` inviata.

**Verifica del 20 settembre 2026.** Il sito pubblicato risponde su `/`, `/libri/`, `/sitemap.xml`, `/robots.txt`; uno slug inesistente cade sul `404.html` e viene dirottato sul catalogo, quindi il ripiego funziona. 53 schede approvate, tutte con slug, 53 cartelle `libri/<slug>/`, 55 URL in sitemap. Copertine: **53 su 53 sullo Storage di Supabase**, nessuna più appesa a un dominio altrui, immagini verificate a campione (60–180 kB l'una). Tutti e tre i secrets sono configurati sul repository.

---

## 4. Da fare — tecnico

In ordine di priorità.

### 4.1 Errori copertina visibili in moderazione
`catalogo-admin.html` legge `id,title,author,publisher,year,cover_url,status,genre,featured,confidence`: `cover_error` non compare da nessuna parte. Serve un contrassegno sulle schede che hanno un errore, il testo del motivo in chiaro e un filtro per isolarle. Fonte alternativa già disponibile: `data/copertine-report.json`, committato a ogni run.

Oggi il campo è vuoto su tutte le schede (l'ultima migrazione ha fatto 53 copertine e zero errori), quindi l'intervento è preventivo: serve perché il giorno in cui una copertina fallirà, il motivo non resti solo in un file JSON che nessuno apre dal telefono.

### 4.2 Pagine statiche per gli articoli
Le notizie vivono su indirizzi con `#` (`?n=<id>#news/<id>`), quindi hanno lo stesso limite che avevano i libri: nessuna anteprima nelle chat, nessuna indicizzazione. Serve lo stesso trattamento: colonna `slug` su `news` con trigger analogo, generazione di `/articoli/<slug>/` dentro `genera_pagine.py`, `schema.org/Article`, voci in sitemap. È il prerequisito perché le interviste circolino.

### 4.3 Modulo pubblico: prima chiuderlo, poi collegarlo
Il modulo che esiste in `index.html` non è "Segnala un titolo" ma un Contatti generico (nome, email, motivo, messaggio), e non ha né `action` né un gestore JavaScript. Premendo "Invia il messaggio" il browser fa un GET sulla stessa pagina: **nome, email e testo finiscono nella barra degli indirizzi e nella cronologia del visitatore**, chi scrive non riceve conferma e la segnalazione non arriva a nessuno. È il primo pezzo da sistemare, prima ancora di aggiungere funzioni: oggi il modulo promette qualcosa che non fa e sparge dati personali in un indirizzo.

Due passaggi distinti:
1. **Chiudere la falla.** O si intercetta l'invio e si spiega che il canale è l'email, o si toglie il modulo finché non c'è un backend. Costo: minuti.
2. **Il "Segnala un titolo" vero.** Scrittura in `books` con stato `candidate`, protezione anti-spam con Cloudflare Turnstile (gratuito), campo contatto per chi segnala.

### 4.4 Kit autore
Alla prima approvazione di un titolo, avvisare autore o editore con: link alla scheda, immagine pronta per i social, badge da incorporare sul proprio sito. Richiede un campo contatto su `books` e un canale di invio email. È la leva di traffico a costo più basso: ogni approvazione genera una condivisione e un collegamento in entrata.

### 4.5 Pagine autore, editore e mese
Stesso generatore, tre nuovi tipi di pagina: `/autori/<slug>/`, `/editori/<slug>/`, `/uscite/<anno>-<mese>/`. La pagina mensile intercetta chi cerca novità senza un titolo in mente ed è la base della newsletter.

### 4.6 Qualità del campo genere
`genre` è testo libero e contiene voci composte separate da virgola. Serve normalizzazione e, a regime, una lista chiusa con possibilità di aggiunta controllata dalla moderazione.

### 4.7 Feed RSS
`/feed.xml` con le ultime schede approvate e gli ultimi articoli. Costo basso, utile agli aggregatori e ai lettori forti.

### 4.8 Newsletter
Iscrizione sul sito e invio mensile (Brevo, fornitore europeo con piano gratuito: https://www.brevo.com/it/). Contenuto composto in gran parte dal database: nuovi ingressi, articolo del mese, scadenze dei concorsi. È l'unico canale di proprietà e la base su cui, più avanti, si vendono spazi.

### 4.9 Pulizie
Eredità della migrazione a Supabase, tutte in `index.html` salvo dove indicato. Da fare in una sessione a sé, separata dalle modifiche funzionali.
- Testi e nomi che raccontano un'architettura che non esiste più: "Caricamento catalogo da GitHub…", la funzione `loadFromGitHub()`, e soprattutto il messaggio d'errore "Non riesco a caricare i JSON da GitHub. Controlla gli URL Raw in **Impostazioni**" — che compare quando è Supabase a non rispondere e rimanda a campi che in Impostazioni non ci sono più.
- Codice morto: il download di `data/candidates.json` da raw.githubusercontent (fermo a 16 candidati di un flusso superato), l'editor notizie locale con "esporta e carica su GitHub", il pulsante "Esporta catalogo".
- File residui in radice: `Index.txt` (vecchia copia di `index.html`) e `fantasy-italia-nuove-proposte.json` (10 titoli in formato pre-Supabase).

### 4.10 Aggiornamenti di manutenzione
`actions/checkout@v4` → `@v5` e `actions/setup-python@v5` → `@v6` in entrambi i workflow, per chiudere l'avviso su Node 20. Da tenere d'occhio: `ubuntu-latest` passa a Ubuntu 26 dal 19 ottobre 2026.

### 4.11 PWA lasciata a metà
`manifest.webmanifest`, `offline.html` e le quattro icone maskable sono nel repository, ma **nessuna pagina collega il manifest** e non esiste un service worker. Oggi sono peso morto: o si completa l'installazione su telefono (che per un catalogo consultato di rado rende poco), o si tolgono. Decidere, non lasciare a metà.

### 4.12 Il backup del catalogo segue solo lo Scout
`data/catalogo.json` lo riscrive `scout.py`, quindi si aggiorna una volta a settimana: fra un lunedì e l'altro è indietro rispetto alle approvazioni fatte in moderazione (il 20 settembre aveva 52 titoli con le copertine vecchie contro 53 schede già migrate). Non è un guasto — si allinea da solo al giro dopo — ma se serve davvero come copia di sicurezza vale la pena riscriverlo anche da `pagine.yml`, che gira ogni sei ore e i dati approvati li legge già.

---

## 5. Da fare — editoriale e strategico

### 5.1 Formati ricorrenti (capacità: circa 2 pezzi al mese)
- Pagina mensile delle uscite con cappello redazionale, semiautomatica.
- Serie di interviste agli esordienti con domande fisse: comparabili negli anni, metà del lavoro lo fa l'intervistato, che poi condivide.
- Un approfondimento al mese su collane, editori o dati.
- Recensioni: poche e scelte, mai legate a un pagamento.

### 5.2 Pagine di servizio per chi scrive
Pubblico più numeroso e più assiduo dei lettori di esordi.
- Elenco degli editori che pubblicano fantasy e accettano proposte, con finestre di invio e condizioni dichiarate dall'editore. Solo criteri verificabili dalle pagine ufficiali, con data dell'ultimo controllo, senza etichette di merito (terreno delicato per via dell'editoria a pagamento).
- Calendario di premi e concorsi con le scadenze.

### 5.3 Rapporto annuale
"Gli esordi fantasy italiani del 2026", da pubblicare tra gennaio e febbraio: quanti titoli, quanti esordi, quali editori, quota di romantasy, formati e prezzi, con nota di metodo sul perimetro (editori monitorati più segnalazioni, quindi stima dichiarata). Da inviare a Giornale della Libreria, ilLibraio e siti di genere. Non richiede appoggi esterni: i dati sono già nel database.

### 5.4 Premio al miglior esordio dell'anno
Rimandato. Da riprendere dopo il passaggio lavorativo in Regione, per valutare la via istituzionale. Impianto previsto: longlist automatica da catalogo, voto dei lettori autenticato per la cinquina, piccola giuria per il vincitore, proclamazione agganciata a una fiera.
Cautela già rilevata: da dipendente pubblico vale il codice di comportamento (DPR 62/2013, art. 7 sull'astensione e art. 10 sui rapporti privati). La via corretta è il patrocinio richiesto con domanda formale.

### 5.5 Ricavi
Scala realistica, nell'ordine:
- Affiliazione, con aspettative basse: Amazon riconosce circa il 5% sui libri; IBS e laFeltrinelli hanno programmi tramite Awin (https://www.ibs.it/affiliati/affiliazioni). Bookshop.org paga il 10% ma non opera in Italia. Copre i costi, poco oltre. **Attenzione**: la meta description del sito dichiara "Nessun cookie né affiliazioni"; va aggiornata nello stesso momento in cui si attiva l'affiliazione, insieme alle note legali.
- Spazi promozionali dichiarati per gli editori, nella newsletter e nella pagina mensile, etichettati come promozione e separati per iscritto da recensioni e premio, con pagina pubblica di linee editoriali.
- Sponsorizzazioni e bandi culturali per rapporto e premio, presentati da un'associazione culturale o APS che detiene il sito.
- Collaborazioni retribuite: articoli per altre testate, moderazione di incontri, lavoro sui dati.
Ordine di grandezza a regime: qualche migliaio di euro l'anno. Da escludere la pubblicità display.
Da verificare prima di incassare: regolamento aziendale sugli incarichi extraistituzionali (art. 53 D.Lgs. 165/2001 esclude dall'autorizzazione collaborazioni giornalistiche e sfruttamento di opere dell'ingegno; vendita di spazi e affiliazione continuativa somigliano invece ad attività commerciale) e inquadramento fiscale con un commercialista.

---

## 6. Indicatori da guardare

- Clic e impressioni organiche in Search Console, e numero di pagine indicizzate.
- Iscritti alla newsletter e tasso di apertura.
- Quota di autori che condividono la propria scheda, misurabile con parametri UTM sui link del kit.
- Collegamenti in entrata, soprattutto da stampa di settore.

Regola: non vendere spazi prima di avere numeri misurabili da mostrare.

---

## 7. Come si lavora su questo repository

- Modifiche incrementali e verificabili; niente riscritture ampie.
- Modifiche funzionali e pulizie cosmetiche in sessioni separate.
- Patch come blocchi "cerca questo / sostituisci con questo", con ancore non ambigue. Per interventi estesi, sostituzione dell'intero file.
- Commenti nel codice in italiano, sul perché di una scelta, non sul cosa fa la riga.
- Nessuna dipendenza nuova sul fronte del sito: resta HTML/CSS/JS senza build.
- Gli script Python usano `requests` e, per le immagini, `pillow`.
- Le chiavi di servizio stanno solo nei secrets del repository: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GOOGLE_BOOKS_KEY`. La chiave pubblica di Supabase è nel codice del sito, per progetto.

### Ambiente locale (Windows)

- Virtualenv in `.venv/`, con `requests` e `pillow`. Ignorato da git, come `.env` e i file temporanei.
- **Avast intercetta il traffico HTTPS con una CA propria**, quindi `pip` e `requests` rifiutano i certificati (`CERTIFICATE_VERIFY_FAILED`) mentre `git` funziona: non è un errore del codice, si vede solo in locale. Rimedio: `.venv/ca-bundle.pem` (certifi più la radice Avast) esportato in `REQUESTS_CA_BUNDLE`.
- `genera_pagine.py` gira senza variabili d'ambiente: usa la chiave pubblica e legge le schede approvate come farebbe un visitatore. `copertine.py` e `scout.py` vogliono la service key; `scout.py` in locale conviene solo con `--dry-run`, perché la classificazione usa il `GITHUB_TOKEN` che esiste solo dentro le Actions.
- La service key va passata dall'ambiente della sessione, mai scritta in un file della cartella.
