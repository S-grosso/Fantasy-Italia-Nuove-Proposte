# Fantasy Italia — Nuove Proposte · Stato e da fare

Documento di lavoro. Aggiornato: 24 settembre 2026, a chiusura della prima sessione di lavoro in locale (20–24 settembre).
Repository: `S-grosso/Fantasy-Italia-Nuove-Proposte` — ramo `main` — sito pubblicato su GitHub Pages al dominio `www.fantasyitalianuoveproposte.it`.

---

## 1. Obiettivi

Due, dichiarati a settembre 2026:

1. Rendere la piattaforma usata e trafficata, per dare visibilità al fantasy italiano.
2. Trasformarla nel tempo in un hobby redditizio: news, interviste, recensioni, collaborazioni.

Il vantaggio competitivo del sito è il dato: un elenco strutturato e aggiornato degli esordi fantasy italiani, con editore, anno, sottogenere. I dati AIE sul fantasy esistono solo aggregati e non distinguono autori italiani da traduzioni né isolano gli esordi. Il modello di riferimento è Locus negli Stati Uniti: registra tutto quello che esce, lo misura, ogni anno lo premia.

Vincolo di fondo: un solo moderatore, tempo da hobby. Ogni scelta va valutata per quanto lavoro ricorrente genera.

---

## Priorità — da dove ripartire

In ordine. Ogni voce rimanda al suo paragrafo nella sezione 4.

1. **Le 7 fonti mute dello Scout** (4.18). Lumien, Zona 42, Angolazioni, PresentARTsi, Astro, Parallelo45 e La nuova carne restituiscono zero titoli a ogni giro, eppure Lumien e Zona 42 hanno libri in catalogo. Ora che il classificatore funziona, più titoli buoni in ingresso sono il modo più diretto di far crescere il catalogo. Da indagare fonte per fonte: endpoint raggiungibile, adattatore giusto, filtro per data, risposta vuota o in errore. Obiettivo collegato: dare al classificatore più contesto (categorie, paratesto) dove le fonti lo offrono.
2. **Il primo giro vero dello Scout con Gemini**, lunedì 28 settembre (4.18): quanti candidati arrivano, con che motivazioni, e se il piano gratuito ha risposto.
3. **Pagine editore e autore** (4.5), solo per chi ha almeno due titoli.
4. **Il mese di uscita** (4.19), che serve alla pagina mensile e alla newsletter.
5. Poi, in sessioni brevi: backup del catalogo anche da `pagine.yml` (4.12), feed RSS (4.7), la pulizia di `index.html` in una sessione a sé (4.9), la PWA da decidere (4.11), `e_admin()` (4.15), il campo genere (4.6).

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

### 4.1 Errori copertina visibili in moderazione — fatto
- [x] **Fatto** (20 settembre 2026). `catalogo-admin.html` ora legge anche `cover_error`: le schede con un errore hanno un contrassegno rosso, il motivo in chiaro sotto il titolo (tre righe al massimo, per non rendere illeggibile l'elenco su schermo stretto) e un filtro "Copertine KO" che le isola a prescindere dallo stato, con l'etichetta di stato visibile in quella vista. L'editor mostra il motivo per esteso. Correggendo l'indirizzo della copertina e salvando, l'errore viene azzerato, altrimenti il contrassegno resterebbe rosso su una scheda appena sistemata.

Intervento preventivo: alla data il campo era vuoto su tutte le schede (53 copertine migrate, zero errori). Serve perché il giorno in cui una copertina fallirà, il motivo non resti solo in `data/copertine-report.json`, che dal telefono non apre nessuno.

### 4.2 Pagine statiche per gli articoli — fatto
Le notizie vivevano su indirizzi con `#` (`?n=<id>#news/<id>`), con lo stesso limite che avevano i libri: nessuna anteprima nelle chat, nessuna indicizzazione.

- [x] **Fatto** (20 settembre 2026). Ogni articolo pubblicato ha il suo indirizzo, `/articoli/<slug>/`, con title, description, canonical, Open Graph, `article:published_time` e JSON-LD `schema.org/Article`. Più l'indice `/articoli/` e le voci in sitemap.

Come è fatto, con le differenze rispetto ai libri:
- **Slug.** Colonna su `news` con indice univoco parziale e trigger `news_assign_slug()`, gemello di quello dei libri: assegnato alla pubblicazione, mai più cambiato nemmeno correggendo il titolo, perché un link già condiviso deve continuare a rispondere. Due differenze: i titoli degli articoli sono lunghi, quindi lo slug si taglia a 80 caratteri sull'ultimo trattino per non spezzare una parola; e la prima disambiguazione è l'anno invece dell'autore, più parlante per una rubrica che torna ogni anno. Backfill: 3 articoli, 3 slug.
- **Indirizzi vecchi.** `?n=<id>#news/<id>` continua a funzionare: l'articolo si apre e la barra si riscrive da sola con l'indirizzo nuovo.
- **Ripiego.** `404.html` intercetta anche `/articoli/<slug>/` non ancora generati e rimanda a `/?articolo=<slug>`.
- **Corpo dell'articolo.** Il generatore toglie script, iframe e gestori inline prima di scriverli nella pagina. Non è un sanificatore — il testo è già ripulito quando lo salvi — ma una rete per quello che potrebbe essere arrivato dall'importazione del vecchio `news.json`.
- **Workflow.** `pagine.yml` ora committa anche `articoli/`, e aggiunge i percorsi solo se esistono: un `git add` su una cartella mai creata avrebbe fermato tutto il passaggio.
- **Rete di sicurezza nuova anche per i libri.** Se la lettura da Supabase torna vuota per un errore, il generatore non tocca più niente: prima avrebbe potuto cancellare 53 pagine buone.
- **Pannello notizie.** Mostra l'indirizzo pubblico dell'articolo quando c'è, con l'avvertenza che la pagina statica compare entro sei ore.

### 4.3 Modulo Contatti: chiuso e collegato — fatto
Il modulo che esiste in `index.html` non è "Segnala un titolo" ma un Contatti generico (nome, email, motivo, messaggio). Fino al 20 settembre 2026 non aveva né `action` né un gestore JavaScript: un invio faceva un GET sulla stessa pagina, con nome, email e testo scritti nell'indirizzo e quindi nella cronologia di chi ci scriveva. La falla però non era raggiungibile — nessun pulsante, nessun link e nessun hash portavano a quella sezione, che di fatto era markup morto — quindi era un rischio in attesa, non una perdita di dati in corso.

- [x] **Chiuso e collegato** (20 settembre 2026). L'invio viene intercettato e trasformato in un messaggio di posta già compilato, con `onsubmit="return false"` come rete di sicurezza per il caso in cui lo script non parta. Il modulo non si svuota, così se il programma di posta non si apre il testo non va perso. La sezione ora si raggiunge dal pulsante "Contatti" nella barra, dall'hash `#contatti` e dal link nel footer, che prima apriva una mail vuota.
- [x] **Il "Segnala un titolo" vero** (20 settembre 2026), fatto insieme al 4.13: il modulo delle proposte scrive su `books` in stato `candidate` passando da una Edge Function. Turnstile non è stato usato, vedi 4.13 per il perché.

### 4.4 Kit autore — fatto
- [x] **Fatto** (24 settembre 2026), senza servizi esterni né dipendenze nuove. Quando una scheda proposta dal modulo pubblico viene pubblicata, il pannello resta sulla scheda e mostra **"Scrivi all'autore"**: apre il programma di posta con un messaggio già compilato — link alla scheda, un testo pronto da condividere, il codice del badge e, solo per gli esordi, l'offerta di un'intervista. Si rilegge e si invia. **"Copia il messaggio"** fa da ripiego per i programmi di posta che tagliano i messaggi lunghi (la versione con l'esordio arriva a circa 1.800 caratteri, vicino alla soglia di alcuni).
- Il testo del messaggio sta tutto in un punto di `catalogo-admin.html` (`KIT_OGGETTO`, `KIT_TESTO`, `KIT_INTERVISTA`), commentato, per poterlo cambiare senza toccare il resto.
- Prima di aprire la posta il pannello controlla che la pagina statica del libro esista già: se non c'è ancora, il link funziona ma nelle chat l'anteprima uscirebbe senza copertina, quindi avvisa e chiede conferma.
- Il badge è `badge/presente.svg`, ospitato sul sito, con i colori del catalogo.
- Note legali aggiornate: la frase "non chiede né conserva dati di chi lo consulta" non era più vera dal 4.13, perché il modulo delle proposte conserva l'email di chi propone. Ora il testo dice a cosa serve quell'indirizzo e come chiederne la cancellazione.

- [x] **Immagine per Instagram** (24 settembre 2026), dove i link non producono anteprime. `scripts/immagini_social.py` prepara per ogni scheda approvata un'immagine 1080×1350 (il formato 4:5 che Instagram mostra per intero nel feed): copertina, sfondo ricavato dalla copertina stessa, titolo, autore, editore e anno, il contrassegno "Esordio" quando serve, il marchio del sito in basso. La carica sullo Storage (`copertine/instagram/`) e ne scrive l'indirizzo in `social_url`. Gira in `pagine.yml` dopo le copertine, non scrive nel repository e non genera commit.
  - Si rifà solo se cambia qualcosa che vi compare: l'impronta sta in `social_firma`. Per rifarle tutte dopo un cambio di disegno basta alzare `VERSIONE_DISEGNO` nello script.
  - Titoli su una, due o tre righe, spezzati in righe di lunghezza simile ("Il principe dei vulcani. / Cronache del palazzo di Lava" invece di lasciare "Lava" da sola); autore ed editore si stringono invece di uscire dall'immagine. Provate tutte e 53 le schede reali, nessun errore.
  - Nel kit compare il pulsante per vederla e, nel messaggio, un paragrafo con il link e un consiglio: su Instagram i link nelle didascalie non si cliccano, meglio la bio o una storia con l'adesivo "Link".
  - Con esordio e immagine insieme il messaggio supera i 2.000 caratteri, soglia oltre la quale Outlook sul computer lo taglia: in quel caso il pannello lo mette anche negli appunti e lo dice.
  - Prima generazione il 24 settembre 2026: 53 immagini, nessun errore; il giro successivo le ha saltate tutte, come deve.
  - Effetto collaterale trovato e corretto: scrivendo `social_url`, lo script aveva fatto risultare "modificate oggi" tutte e 53 le schede, e la sitemap l'aveva dichiarato a Google. Il trigger `tocca_updated_at` ora ignora le colonne tecniche (`social_url`, `social_firma`, `cover_error` e la generata `isbn_norm`, che nei trigger BEFORE vale sempre NULL e faceva sembrare diversa ogni riga): `updated_at` si muove solo per modifiche vere, anche un salvataggio dal pannello senza cambiamenti la lascia com'è. Le date del 24 settembre sono rimaste: quelle precedenti erano a loro volta artificiali, lasciate dalla migrazione delle copertine.

### 4.5 Pagine autore ed editore
Stesso generatore delle schede e degli articoli, due nuovi tipi di pagina: `/editori/<slug>/` e `/autori/<slug>/`, con voci in sitemap e collegamenti dalle schede.

Dati misurati il 24 settembre 2026, sulle 53 schede approvate: **18 editori**, nomi puliti e senza doppioni, di cui 9 con un solo titolo e 6 con tre o più; **39 autori**, di cui solo 9 con due o più titoli, e 2 schede con più autori nello stesso campo ("Fiore Manni, Michele Monteleone"). Una pagina con un libro solo ripete la scheda ed è contenuto quasi duplicato: si generano solo le pagine di **editori e autori con almeno due titoli**. Gli autori multipli vanno separati prima di contarli.

La pagina mensile delle uscite, prevista qui in origine, non si può fare: il catalogo registra solo l'anno. Vedi 4.19.

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
- File residui in radice: `Index.txt` (vecchia copia di `index.html`), `fantasy-italia-nuove-proposte.json` (10 titoli in formato pre-Supabase) e `post.html`, pagina che mostrava un articolo da sola e che dal 4.2 è superata dalle pagine statiche: non è collegata da nessuna parte.

### 4.10 Aggiornamenti di manutenzione — fatto
- [x] **Fatto** (20 settembre 2026). `actions/checkout@v5` e `actions/setup-python@v6` in entrambi i workflow: chiude l'avviso su Node 20.

Da tenere d'occhio: `ubuntu-latest` passa a Ubuntu 26 dal 19 ottobre 2026. La prima esecuzione dopo questo aggiornamento va guardata, perché è la prima con le action nuove.

### 4.11 PWA lasciata a metà
`manifest.webmanifest`, `offline.html` e le quattro icone maskable sono nel repository, ma **nessuna pagina collega il manifest** e non esiste un service worker. Oggi sono peso morto: o si completa l'installazione su telefono (che per un catalogo consultato di rado rende poco), o si tolgono. Decidere, non lasciare a metà.

### 4.12 Il backup del catalogo segue solo lo Scout
`data/catalogo.json` lo riscrive `scout.py`, quindi si aggiorna una volta a settimana: fra un lunedì e l'altro è indietro rispetto alle approvazioni fatte in moderazione (il 20 settembre aveva 52 titoli con le copertine vecchie contro 53 schede già migrate). Non è un guasto — si allinea da solo al giro dopo — ma se serve davvero come copia di sicurezza vale la pena riscriverlo anche da `pagine.yml`, che gira ogni sei ore e i dati approvati li legge già.

### 4.13 "Proponi un titolo": ripristinato — fatto
Il modulo esisteva ma **non aveva mai spedito niente**: salvava in `state.pendingBooks`, cioè nel `localStorage` del browser di chi compilava, quindi la proposta restava sul dispositivo di chi la scriveva. Arrivava in moderazione solo quando a compilarla era il moderatore, che poi esportava il JSON e lo caricava su GitHub a mano. Dopo la migrazione a Supabase quella chiave viene cancellata a ogni caricamento, e il pulsante in barra mandava un visitatore alla pagina di accesso dell'amministrazione.

- [x] **Fatto** (20 settembre 2026). Il modulo ora scrive davvero, passando dalla Edge Function `proponi` (sorgente in `supabase/functions/proponi/`).

Come è fatto:
- **Scrittura.** Nessuna policy allentata: il catalogo resta leggibile da chiunque e scrivibile da nessuno. La funzione scrive con la service key, che vive solo lato server. `verify_jwt` è disattivato per necessità — chi propone non ha un account e la chiave pubblica del sito, in formato `sb_publishable_…`, non è un JWT.
- **Anti-spam senza terzi.** Campo-esca fuori dallo schermo più doppio limite di frequenza (dieci proposte l'ora in tutto, tre al giorno dallo stesso indirizzo). Turnstile è stato scartato: avrebbe messo uno script di Cloudflare su ogni visita e un cookie di sfida, contro due principi dichiarati in questo documento, e le proposte finiscono comunque in una coda approvata a mano. Se un giorno arriva spazzatura vera, si aggiunge sopra senza rifare niente.
- **Doppioni.** Confronto su titolo+autore e su `isbn_norm`, la colonna calcolata aggiunta al database (vedi sotto), in tutti gli stati: un titolo già scartato non torna in coda.
- **Contatto.** Colonna `proposer_contact` su `books`, visibile in moderazione come indirizzo su cui scrivere. È un dato personale: il permesso di lettura di `anon` era sull'intera tabella ed è stato tolto e riconcesso colonna per colonna, saltando quella. Verificato da visitatore anonimo: il contatto dà 401, e anche `select=*` è negato. Effetto collaterale voluto: una colonna aggiunta in futuro nasce non leggibile dal pubblico.
- **Cosa vede chi propone.** Conferma esplicita, avvertenza che l'approvazione è manuale, e il modulo che non si svuota quando la proposta è rifiutata.

Due modifiche al database, registrate come migrazioni: `proposer_contact` con i permessi ristrutturati, e `isbn_norm` (colonna calcolata con le sole cifre, più indice) perché gli ISBN in tabella convivono in formati diversi e un confronto esatto si perdeva i doppioni.

### 4.14 Un ISBN sbagliato su cinque schede
`978-8858054857` compare su cinque schede approvate diverse: *Arhos. L'acqua e l'ombra* e *Arhos. La sabbia e il vento* di Cecilia Randall, *Sussurrami, o dea* di Davide Del Popolo Riolo, *L'accademia di Lyonesse* e *Le vite di Narses*. Uno solo può averlo davvero, forse nessuno. Il dato finisce nel JSON-LD delle cinque pagine pubbliche, quindi lo legge anche Google, e falsa il controllo dei doppioni delle proposte. Da sistemare a mano dalla moderazione, verificando i libri veri uno per uno.

(Trovato grazie a `isbn_norm`: prima il formato misto lo nascondeva. L'altro ISBN ripetuto, quello di *Surikila*, è legittimo — stessa scheda approvata e scartata.)

### 4.15 Avvisi di sicurezza Supabase — due chiusi su quattro
Nessuno di questi è stato introdotto dai lavori di settembre.

- [x] **`books_assign_slug()`** (20 settembre 2026): permesso di esecuzione revocato a `public`, `anon` e `authenticated`. È una funzione di trigger, il trigger la esegue per conto suo. Verificato simulando il pannello — ruolo `authenticated` con l'email dell'amministratore nel token: inserimento e aggiornamento funzionano e lo slug viene assegnato.
- [x] **`tocca_updated_at`** (20 settembre 2026): `search_path` fissato a `public`.
- [ ] **`e_admin()`** resta richiamabile via RPC. **Non è stata toccata di proposito**: è usata dentro le policy RLS, che PostgreSQL valuta con i permessi di chi fa la query, quindi revocare l'esecuzione ad `authenticated` rischia di far fallire le policy e di chiudere l'amministratore fuori dal proprio catalogo. Il rischio attuale è basso — chiamata da un anonimo restituisce `false` e non rivela niente — ma va affrontata con una prova vera dell'accesso, non a fine sessione. Strada alternativa da valutare: spostarla in uno schema non esposto dall'API.
- [ ] **Protezione password compromesse** (HaveIBeenPwned): **non disponibile sul piano gratuito**, su cui sta il progetto — Supabase la riserva al piano Pro. Il rilievo resterà quindi visibile nel controllo di sicurezza. La protezione che conta si ottiene lo stesso: password dell'amministratore lunga, unica e custodita in un gestore di password. Un passo ulteriore, gratuito, sarebbe l'autenticazione a due fattori (TOTP), che però richiede di modificare l'accesso dei due pannelli.

### 4.16 Search Console: primi rilievi — fatto
Dal 20 settembre 2026 Google raccoglie impressioni sul sito. Due rilievi alla prima lettura, sistemati il 24 settembre:
- [x] **"Pagina duplicata senza URL canonico"** su `/index.html`: la home rispondeva su `/` e su `/index.html` senza dichiarare quale fosse quella buona. Aggiunto `<link rel="canonical">` verso `/`, e i due "← Sito" dei pannelli, da cui Google aveva scoperto il doppione, puntano ora a `/`.
- [x] **"Rilevata, ma attualmente non indicizzata"** su 5 schede libro: normale per un sito nuovo, Google le conosce ma non le ha ancora visitate. Si aiuta dando loro una strada: la home disegna le schede con JavaScript, quindi nell'HTML iniziale non c'era nessun collegamento verso `/libri/`. Ora c'è. Il resto è tempo.
- [x] **Commit a vuoto dei workflow**: 13 in quattro giorni, ognuno con un nuovo deploy del sito. `copertine.py` riscriveva il report a ogni giro con il solo orario aggiornato, e la sitemap dichiarava come `lastmod` delle pagine indice la data del giorno. Ora il report si riscrive solo se cambia la sostanza, e il `lastmod` è la data del contenuto più recente.

L'editor degli articoli trasformava ogni riga incollata in un'intestazione: sistemato al 4.17.

### 4.17 L'editor degli articoli trasformava tutto in intestazioni — fatto
Nella guida per chi propone, salvata il 20 settembre da `news-admin.html`, **ogni riga era diventata un `<h2 id="articleTitle">`** con gli stili calcolati della pagina: 40 intestazioni, nessun paragrafo, lo stesso `id` ripetuto 40 volte. Il pulsante "Titoletto" non c'entrava. Quando si incolla in un elemento modificabile, Chrome clona per ogni riga il blocco in cui sta il cursore, attributi compresi: il cursore stava nel titolo di una vecchia vista articolo, e ogni riga incollata ne è diventata una copia.

- [x] **Il contenuto** (24 settembre 2026). Struttura della guida ricostruita — 6 titoli, 6 paragrafi, 5 elenchi con 21 voci — con il testo verificato identico parola per parola (520 parole prima e dopo) e l'impronta di quanto salvato controllata contro il file verificato. L'aggiornamento è passato solo a condizione che l'articolo fosse ancora quello letto, per non sovrascrivere modifiche fatte nel frattempo.
- [x] **L'editor** (24 settembre 2026), tre interventi:
  - all'incolla entra solo il testo, rimontato in paragrafi; le righe che iniziano con trattino, pallino o asterisco diventano un elenco vero. Incollando sopra tutto il contenuto, o in un editor vuoto, il testo lo sostituisce per intero senza ereditare niente dal blocco in cui stava il cursore. Una riga sola si inserisce dove sta il cursore, spazi compresi;
  - al salvataggio si tolgono comunque `style`, `id`, `class`, gli `<span>` e `<font>` rimasti vuoti e i blocchi fatti solo di un a capo: vale anche per quello che arriva per altre strade;
  - "Titoletto" diventa un interruttore: premuto su un titolo lo riporta a paragrafo. Prima un titolo non si toglieva più.

Provato nel browser riproducendo il caso esatto della guida.

Facoltativo: *Nasce Fantasy Italia* non ha il problema, ma è scritto con a capo e grassetti invece di titoli ed elenchi veri. Si legge bene; dargli la stessa struttura della guida lo renderebbe più chiaro anche per i motori di ricerca.

### 4.18 Scout: il classificatore era spento da agosto
**Cosa è successo.** Il 30 luglio 2026 GitHub ha chiuso GitHub Models, il servizio che classificava i candidati: da quel giorno ogni chiamata rispondeva 410. Lo Scout era fatto per lasciar passare tutto quando la classificazione falliva ("non classificato — da verificare a mano", confidenza bassa), quindi per due mesi in moderazione è arrivato tutto ciò che superava il filtro a regole: classici Disney, saggi, copie autografate, romanzi non fantasy. Il 26 luglio, ultimo giro col classificatore acceso, i verdetti erano buoni: delle 14 proposte "alta" scartate, 10 erano doppioni entrati in catalogo quello stesso giorno, gli errori veri 4 su 16.

- [x] **Protezione** (24 settembre 2026): un candidato non classificato non viene più proposto, e se succede il giro esce con errore così GitHub lo segnala per email. Il filtro a regole scarta anche le varianti di prodotto ("COPIA AUTOGRAFATA", "| Custom Sprayed Edges") e la serie Disney "I Classici e la magia".
- [x] **Nuovo classificatore: Google Gemini, piano gratuito** (24 settembre 2026). All'avvio lo Scout chiede a Google quali modelli sono disponibili e usa il primo di una lista di preferenze (`GEMINI_PREFERITI` in `scripts/scout.py`): se uno viene ritirato passa al successivo da solo. Prova prima l'API `interactions`, poi la classica `generateContent`, e legge la risposta in tutte le forme documentate. Sul piano gratuito Google può usare i testi inviati per migliorare i suoi prodotti: si inviano solo presentazioni pubbliche degli editori.
- [x] **Chiave e prova** (24 settembre 2026): `GEMINI_API_KEY` nei secrets, prova da Actions superata con **3 verdetti giusti su 3**, e motivazioni sensate ("traduzione dall'inglese", "libro di divulgazione per bambini"). Risponde l'API `interactions`.
- **Il piano gratuito è congestionato.** Alla prima prova il modello più nuovo rispondeva 503 ("high demand"); alla seconda erano sovraccarichi tutti e quattro, e dopo l'attesa ha risposto `gemini-3.5-flash`. Per questo lo Scout prova i modelli uno dopo l'altro e tiene quello che risponde. Se un lunedì fossero tutti irraggiungibili, i candidati restano fuori e arriva l'email: basta rilanciare lo Scout da Actions più tardi.
- Da guardare al primo giro vero, lunedì 28 settembre: quanti candidati arrivano e con che motivazioni.
- Le 4 proposte rimaste del 21 settembre, arrivate a classificatore spento (Cobalto, The Ordeals, La madre rossa, Profili d'ambra), sono state scartate: titoli non pertinenti o autori non italiani. La coda di moderazione è vuota.
- [ ] **Fonti mute**: 7 fonti su 14 restituiscono zero titoli a ogni giro (Lumien, Zona 42, Angolazioni, PresentARTsi, Astro, Parallelo45, La nuova carne), eppure Lumien e Zona 42 hanno libri in catalogo. Può essere che non pubblichino nulla in 30 giorni, ma sette su quattordici, sempre, fa pensare ad adattatori rotti. Da indagare fonte per fonte.

### 4.19 Il mese di uscita
Il catalogo registra solo l'anno (`year`). Senza il mese non si possono fare la pagina mensile delle uscite (5.1), la base della newsletter (4.8) né statistiche per mese nel rapporto annuale (5.3). Serve una colonna nuova (per esempio `published_month`, `AAAA-MM`), compilata dallo Scout quando la fonte lo indica (Google Books dà spesso la data completa, i feed degli editori la data di pubblicazione del prodotto) e in moderazione quando manca. Conviene farla presto: ogni scheda approvata senza mese è un dato da ricostruire a mano più avanti. Per le 53 schede esistenti si può tentare un recupero da Google Books tramite ISBN.

---

## 5. Da fare — editoriale e strategico

### 5.0 Da rileggere subito: la guida per chi propone
L'articolo "Guida pratica: come proporre un romanzo" descrive una procedura precedente al modulo, che dal 20 settembre 2026 scrive davvero in moderazione. Va riletto e allineato: è il testo a cui rimandare chi chiede come si entra in catalogo, e ora può semplicemente puntare a "Proponi un titolo".

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
- Il certificato di Avast **ruota** (è successo il 23 settembre 2026): se `requests` torna a dare `CERTIFICATE_VERIFY_FAILED`, prima di cercare il guasto nel codice si rigenera il bundle da certifi più il `wscert.pem` attuale.
- `genera_pagine.py` gira senza variabili d'ambiente: usa la chiave pubblica e legge le schede approvate come farebbe un visitatore. `immagini_social.py` in locale lavora solo con `--dry-run --cartella <fuori dal repository>`. `copertine.py` vuole la service key. `scout.py` in locale conviene solo con `--dry-run`: la chiave di Gemini sta nei secrets di GitHub, e il classificatore si prova da Actions con l'interruttore `prova_classificatore`.
- GitHub CLI è installato (`C:\Program Files\GitHub CLI\gh.exe`) e autenticato: serve per lanciare i workflow a mano, leggere i registri e controllare i secrets. In PowerShell un percorso fra virgolette va preceduto da `&`.
- Supabase è raggiungibile anche dall'assistente (migrazioni, query, funzioni): ogni modifica allo schema passa da una migrazione con nome, e le prove sui dati si fanno dentro una transazione annullata.
- La copia di lavoro ha i fine riga CRLF (`core.autocrlf=true`) mentre il repository li salva LF: le modifiche fatte con script vanno riscritte in CRLF, altrimenti il diff mostra l'intero file come cambiato.
- Prima di `git pull --rebase` si committa: i workflow committano sul ramo più volte al giorno, e con modifiche in sospeso il riallineamento si rifiuta.
- La service key va passata dall'ambiente della sessione, mai scritta in un file della cartella.
