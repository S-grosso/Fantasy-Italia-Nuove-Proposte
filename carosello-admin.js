/* Carosello Instagram — solo per il pannello di moderazione.

   Lo include soltanto catalogo-admin.html, dopo l'accesso: nessuna pagina
   pubblica lo richiama. Disegna nel browser tre immagini 1080x1350 per un
   carosello:
     1. il gancio: il libro in una frase e "Per chi ha amato: ...", sui
        colori della copertina sfocata;
     2. la scheda: l'immagine che scripts/immagini_social.py prepara per ogni
        libro (social_url), cosi' com'e';
     3. le prime righe: l'incipit in tipografia da citazione.
   Non salva niente sullo Storage: le immagini si condividono dal telefono o
   si scaricano, e basta.

   Palette, margini e marchio in basso sono gli stessi di immagini_social.py,
   perche' le tre slide devono sembrare della stessa mano della card che sta
   in mezzo. Se cambia il disegno della card, va riportato qui. */
(function(){
  'use strict';

  const L = 1080, A = 1350, MARGINE = 70;
  const UTILE = L - 2 * MARGINE;
  const CENTRO = L / 2;

  // Gli stessi colori di immagini_social.py (FONDO, INCHIOSTRO, SPENTO,
  // ACCENTO, BORDO), scritti in esadecimale.
  const FONDO = '#0f1013';
  const INCHIOSTRO = '#e7e9ee';
  const SPENTO = '#a9adbb';
  const ACCENTO = '#7aa6ff';
  const BORDO = '#2a2e40';

  const SITO = 'www.fantasyitalianuoveproposte.it';
  // Gli stessi hashtag di IG_ETICHETTE in index.html e in genera_pagine.py:
  // il sito non ha un passaggio di build, quindi una modifica va riportata
  // in tutti e tre i posti.
  const IG_ETICHETTE = '#fantasyitaliano #fantasy #librifantasy #libridaleggere #bookstagramitalia';

  /* ---------------- caratteri ----------------
     DejaVu Sans, come la card, da fonts/ nel repository: nessun servizio
     esterno. Si carica al primo carosello e non a ogni apertura del pannello,
     perche' i due file pesano 1,4 MB. Il nome di famiglia e' proprio, cosi'
     un DejaVu installato sul computer non si mescola con questo. */
  const FAMIGLIA = 'DejaVu Sans Carosello';
  let caratteriPronti = null;

  function caricaCaratteri(){
    if(!caratteriPronti){
      caratteriPronti = (async () => {
        const facce = [
          new FontFace(FAMIGLIA, 'url(/fonts/DejaVuSans.ttf)', { weight: '400' }),
          new FontFace(FAMIGLIA, 'url(/fonts/DejaVuSans-Bold.ttf)', { weight: '700' })
        ];
        await Promise.all(facce.map(f => f.load().then(() => document.fonts.add(f))));
        // Un canvas non aspetta i caratteri: se si disegna prima, il testo
        // esce nel carattere di ripiego e le misure degli a capo sono sbagliate.
        await document.fonts.ready;
      })().catch(e => { caratteriPronti = null; throw e; });
    }
    return caratteriPronti;
  }

  // Verdana come ripiego, come fa immagini_social.py in locale: ha
  // larghezze simili a DejaVu, quindi gli a capo cambiano poco.
  function font(peso, misura){
    return (peso === 700 ? 'bold ' : '') + misura + 'px "' + FAMIGLIA + '", Verdana, sans-serif';
  }

  function altezzaRiga(misura, interlinea){
    return Math.floor(misura * (interlinea || 1.22));
  }

  /* ---------------- testo ---------------- */

  // Come a_capo() di immagini_social.py: si va a capo il piu' tardi
  // possibile; una parola piu' larga della riga resta da sola.
  function aCapo(ctx, testo, larghezza){
    const righe = [];
    let corrente = '';
    for(const parola of String(testo || '').split(/\s+/).filter(Boolean)){
      const prova = corrente ? corrente + ' ' + parola : parola;
      if(!corrente || ctx.measureText(prova).width <= larghezza) corrente = prova;
      else { righe.push(corrente); corrente = parola; }
    }
    if(corrente) righe.push(corrente);
    return righe;
  }

  // Come bilanciate(): lo stesso numero di righe, ma di lunghezza simile,
  // cosi' la frase non finisce con una parola sola sull'ultima riga.
  function bilanciate(ctx, testo, larghezza){
    const n = aCapo(ctx, testo, larghezza).length;
    let basso = Math.max(...String(testo).split(/\s+/).filter(Boolean).map(p => ctx.measureText(p).width));
    let alto = larghezza;
    while(alto - basso > 1){
      const medio = (basso + alto) / 2;
      if(aCapo(ctx, testo, medio).length <= n) alto = medio; else basso = medio;
    }
    return aCapo(ctx, testo, alto);
  }

  // Le righe di un testo con i capoversi (l'incipit): ogni riga sa se dopo
  // di lei comincia un paragrafo nuovo, che ha un po' di stacco in piu'.
  function righeConCapoversi(ctx, testo, larghezza, bilancia){
    const paragrafi = String(testo || '').split(/\n+/).map(p => p.trim()).filter(Boolean);
    const righe = [];
    paragrafi.forEach((p, i) => {
      const spezzate = bilancia ? bilanciate(ctx, p, larghezza) : aCapo(ctx, p, larghezza);
      spezzate.forEach((t, j) => righe.push({ t, stacco: j === spezzate.length - 1 && i < paragrafi.length - 1 }));
    });
    return righe;
  }

  function altezzaBlocco(righe, misura, interlinea){
    const stacchi = righe.filter(r => r.stacco).length;
    return righe.length * altezzaRiga(misura, interlinea) + stacchi * Math.round(misura * 0.6);
  }

  // La misura piu' grande, fra max e min, a cui il testo sta nello spazio
  // dato. E' il "testo che si adatta da solo": corto e grande, lungo e piu'
  // piccolo. Se non sta nemmeno alla misura minima, si tronca con i puntini
  // invece di uscire dall'immagine.
  function adatta(ctx, testo, o){
    for(let m = o.max; m >= o.min; m -= 2){
      ctx.font = font(o.peso, m);
      const righe = righeConCapoversi(ctx, testo, o.larghezza, o.bilancia);
      if(altezzaBlocco(righe, m, o.interlinea) <= o.altezza && (!o.maxRighe || righe.length <= o.maxRighe)){
        return { misura: m, righe, alto: altezzaBlocco(righe, m, o.interlinea) };
      }
    }
    const m = o.min;
    ctx.font = font(o.peso, m);
    const tutte = righeConCapoversi(ctx, testo, o.larghezza, false);
    const righe = [];
    for(const r of tutte){
      const prova = righe.concat([r]);
      if(altezzaBlocco(prova, m, o.interlinea) > o.altezza || (o.maxRighe && prova.length > o.maxRighe)) break;
      righe.push(r);
    }
    if(!righe.length) return { misura: m, righe: [], alto: 0 };
    const ultima = righe[righe.length - 1];
    ultima.stacco = false;
    while(ultima.t.includes(' ') && ctx.measureText(ultima.t + '…').width > o.larghezza){
      ultima.t = ultima.t.slice(0, ultima.t.lastIndexOf(' '));
    }
    ultima.t = ultima.t.replace(/[\s,;:.]+$/, '') + '…';
    return { misura: m, righe, alto: altezzaBlocco(righe, m, o.interlinea) };
  }

  // Come che_sta(): una riga sola, il carattere si stringe invece di andare
  // a capo, e sotto la misura minima si accorcia con i puntini.
  function rigaCheSta(ctx, testo, peso, max, min, larghezza){
    let t = String(testo || '');
    for(let m = max; m >= min; m -= 2){
      ctx.font = font(peso, m);
      if(ctx.measureText(t).width <= larghezza) return { t, misura: m };
    }
    ctx.font = font(peso, min);
    while(t.length > 1 && ctx.measureText(t + '…').width > larghezza) t = t.slice(0, -1);
    return { t: t.trimEnd() + '…', misura: min };
  }

  /* ---------------- disegno ---------------- */

  function nuovaTela(){
    const c = document.createElement('canvas');
    c.width = L; c.height = A;
    const ctx = c.getContext('2d');
    ctx.textBaseline = 'top';
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    return { c, ctx };
  }

  // Lo sfondo della card: la copertina sfocata e coperta all'80% dal colore
  // del sito. La sfocatura si ottiene rimpicciolendo e ringrandendo, non con
  // ctx.filter, che Safari non supporta: il carosello si fa anche dal telefono.
  function sfondo(ctx, img, velo){
    ctx.fillStyle = FONDO;
    ctx.fillRect(0, 0, L, A);
    if(!img) return;
    const piccola = document.createElement('canvas');
    piccola.width = 27; piccola.height = 34;
    const p = piccola.getContext('2d');
    const r = Math.max(27 / img.width, 34 / img.height);
    p.drawImage(img, (27 - img.width * r) / 2, (34 - img.height * r) / 2, img.width * r, img.height * r);
    // Un passaggio intermedio rende la sfumatura morbida invece che a quadretti.
    const media = document.createElement('canvas');
    media.width = 108; media.height = 135;
    const mctx = media.getContext('2d');
    mctx.imageSmoothingQuality = 'high';
    mctx.drawImage(piccola, 0, 0, 108, 135);
    ctx.drawImage(media, 0, 0, L, A);
    ctx.globalAlpha = velo;
    ctx.fillStyle = FONDO;
    ctx.fillRect(0, 0, L, A);
    ctx.globalAlpha = 1;
  }

  // Il marchio in basso, identico a quello della card: tre righe centrate e
  // un filo sopra. Restituisce dove sta il filo, cioe' il limite del contenuto.
  function marchio(ctx){
    const righe = [
      ['Nel catalogo di', 400, 26, SPENTO],
      ['Fantasy Italia — Nuove proposte', 700, 32, INCHIOSTRO],
      ['fantasyitalianuoveproposte.it', 400, 26, ACCENTO]
    ];
    const alto = righe.reduce((s, r) => s + altezzaRiga(r[2]), 0);
    let y = A - MARGINE - alto;
    const filo = y - 28;
    ctx.strokeStyle = BORDO; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(CENTRO - 60, filo); ctx.lineTo(CENTRO + 60, filo); ctx.stroke();
    ctx.textAlign = 'center';
    for(const [t, peso, misura, colore] of righe){
      ctx.font = font(peso, misura); ctx.fillStyle = colore;
      ctx.fillText(t, CENTRO, y);
      y += altezzaRiga(misura);
    }
    return filo;
  }

  // Il gancio e' solo testo, sui colori della copertina sfocata: la
  // copertina si vede intera nella slide dopo, che diventa la "rivelazione".
  // Un ritaglio della copertina e' stato provato (al 60% e poi a tutta
  // larghezza) e scartato: tagliava il titolo stampato e l'illustrazione
  // in punti che nessuno avrebbe scelto.
  function slideGancio(copertina, d){
    const { c, ctx } = nuovaTela();
    // Lo stesso velo della card: provato al 72%, su una copertina chiara lo
    // sfondo diventava grigio medio e "Per chi ha amato" si leggeva male.
    sfondo(ctx, copertina, 0.80);
    const limite = marchio(ctx) - 28;   // come y_blocco nella card: 56 px sopra il marchio
    const STACCO = 40;

    const affini = d.affini ? 'Per chi ha amato: ' + d.affini : '';
    const bAff = affini
      ? adatta(ctx, affini, { peso: 400, max: 36, min: 24, larghezza: UTILE, altezza: 3 * altezzaRiga(36), maxRighe: 3 })
      : null;
    const spazioFrase = limite - MARGINE - (bAff ? bAff.alto + STACCO : 0);
    const bFrase = adatta(ctx, d.pitch, { peso: 700, max: 76, min: 40, larghezza: UTILE, altezza: spazioFrase, bilancia: true });
    const altoTesto = bFrase.alto + (bAff ? bAff.alto + STACCO : 0);

    // Il testo sta al centro dello spazio sopra il marchio.
    let y = MARGINE + (limite - MARGINE - altoTesto) / 2;

    ctx.textAlign = 'center';
    if(bFrase){
      ctx.font = font(700, bFrase.misura); ctx.fillStyle = INCHIOSTRO;
      for(const r of bFrase.righe){ ctx.fillText(r.t, CENTRO, y); y += altezzaRiga(bFrase.misura); }
      y += STACCO;
    }
    if(bAff){
      ctx.font = font(400, bAff.misura);
      const PREFISSO = 'Per chi ha amato:';
      bAff.righe.forEach((r, i) => {
        // "Per chi ha amato:" nel colore d'accento, i titoli in grigio: la
        // riga si centra per intero e si scrive in due pezzi.
        if(i === 0 && r.t.startsWith(PREFISSO)){
          const x0 = CENTRO - ctx.measureText(r.t).width / 2;
          ctx.textAlign = 'left';
          ctx.fillStyle = ACCENTO; ctx.fillText(PREFISSO, x0, y);
          ctx.fillStyle = SPENTO; ctx.fillText(r.t.slice(PREFISSO.length), x0 + ctx.measureText(PREFISSO).width, y);
          ctx.textAlign = 'center';
        } else {
          ctx.fillStyle = SPENTO; ctx.fillText(r.t, CENTRO, y);
        }
        y += altezzaRiga(bAff.misura);
      });
    }
    return c;
  }

  function slideScheda(card){
    const { c, ctx } = nuovaTela();
    ctx.drawImage(card, 0, 0, L, A);
    return c;
  }

  // Oltre questa lunghezza l'incipit va su due slide: su una sola, 600
  // caratteri scendono sotto i 40 px e al telefono si leggono a fatica.
  // Sotto, una seconda slide resterebbe mezza vuota.
  const INCIPIT_SU_DUE = 300;

  // Divide l'incipit in due parti di lunghezza simile. Prima si cerca la fine
  // di un paragrafo vicina alla meta', poi la fine di una frase: tagliando
  // solo a fine frase, la prova ha separato "«Ascolta» disse." da "«Lo senti
  // il battito?»", spezzando un dialogo. Se il testo non ha punti fermi, si
  // taglia fra due parole e si mettono i puntini.
  function spezzaIncipit(testo){
    const meta = testo.length / 2;
    const vicini = re => {
      const tagli = [];
      let m;
      while((m = re.exec(testo))) tagli.push(m.index + m[0].length);
      return tagli.filter(i => i > testo.length * 0.25 && i < testo.length * 0.75);
    };
    let buoni = vicini(/\n+/g);
    if(!buoni.length) buoni = vicini(/[.!?…]["»”’)]*(?=\s)/g);
    if(buoni.length){
      const i = buoni.reduce((a, b) => Math.abs(b - meta) < Math.abs(a - meta) ? b : a);
      return [testo.slice(0, i).trim(), testo.slice(i).trim()];
    }
    const spazio = testo.lastIndexOf(' ', meta);
    const i = spazio > 0 ? spazio : Math.floor(meta);
    return [testo.slice(0, i).trim() + '…', '…' + testo.slice(i).trim()];
  }

  // parte e totale numerano le slide dell'incipit ("1/2"), quando sono due.
  function slidePrimeRighe(copertina, d, testo, parte, totale){
    const { c, ctx } = nuovaTela();
    // Velo piu' fitto che nella card: qui c'e' un testo lungo da leggere.
    sfondo(ctx, copertina, 0.88);
    const filo = marchio(ctx);

    // Titolo e autore piccoli, allineati a destra come la firma di una
    // citazione, sopra il marchio.
    const autore = d.autore ? rigaCheSta(ctx, d.autore, 400, 26, 20, UTILE) : null;
    const titolo = rigaCheSta(ctx, d.titolo, 700, 30, 22, UTILE);
    let y = filo - 44 - (autore ? altezzaRiga(autore.misura) : 0) - altezzaRiga(titolo.misura);
    const yFirma = y;
    ctx.textAlign = 'right';
    ctx.font = font(700, titolo.misura); ctx.fillStyle = INCHIOSTRO;
    ctx.fillText(titolo.t, L - MARGINE, y); y += altezzaRiga(titolo.misura);
    if(autore){ ctx.font = font(400, autore.misura); ctx.fillStyle = SPENTO; ctx.fillText(autore.t, L - MARGINE, y); }
    ctx.strokeStyle = ACCENTO; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(L - MARGINE - 60, yFirma - 22); ctx.lineTo(L - MARGINE, yFirma - 22); ctx.stroke();

    // Le virgolette grandi e l'etichetta, in alto a sinistra.
    ctx.textAlign = 'left';
    ctx.font = font(700, 200); ctx.fillStyle = ACCENTO;
    ctx.fillText('“', MARGINE - 8, MARGINE - 30);
    ctx.font = font(700, 24);
    ctx.fillText('LE PRIME RIGHE' + (totale > 1 ? '  ·  ' + parte + '/' + totale : ''), MARGINE + 120, MARGINE + 46);

    const alto = MARGINE + 150;
    const basso = yFirma - 60;
    const b = adatta(ctx, testo, { peso: 400, max: 48, min: 24, larghezza: UTILE, altezza: basso - alto, interlinea: 1.5 });
    y = alto + (basso - alto - b.alto) / 2;
    ctx.font = font(400, b.misura); ctx.fillStyle = INCHIOSTRO;
    for(const r of b.righe){
      ctx.fillText(r.t, MARGINE, y);
      y += altezzaRiga(b.misura, 1.5) + (r.stacco ? Math.round(b.misura * 0.6) : 0);
    }
    return c;
  }

  /* ---------------- immagini di partenza ----------------
     Con fetch in modalita' CORS invece di <img crossOrigin>: il requisito e'
     lo stesso (senza il permesso del dominio il canvas resterebbe "sporco" e
     non si potrebbe esportare), ma cosi' si puo' chiedere cache 'no-cache',
     come fa il sito: card e copertine si rifanno allo stesso indirizzo, e la
     copia in memoria del browser sarebbe quella vecchia. */
  async function caricaImmagine(url){
    const r = await fetch(url, { mode: 'cors', cache: 'no-cache' });
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const blob = await r.blob();
    // createImageBitmap e non img.decode(): decode() aspetta che la pagina
    // venga disegnata, e con la finestra in secondo piano non finiva mai
    // (provato: il carosello restava su "Preparo le immagini…").
    if(window.createImageBitmap){
      try{ return await createImageBitmap(blob); }catch(e){ /* si ripiega sotto */ }
    }
    return new Promise((ok, ko) => {
      const img = new Image();
      img.onload = () => ok(img);
      img.onerror = () => ko(new Error('immagine illeggibile'));
      img.src = URL.createObjectURL(blob);
    });
  }

  function dominio(url){
    try{ return new URL(url).hostname; }catch(e){ return url; }
  }

  /* ---------------- interfaccia per il pannello ---------------- */

  // d: titolo, autore, editore, anno, copertina, socialUrl, pitch, affini,
  // incipit, consenso, esordio, instagram, slug, nomeBase.
  async function prepara(d){
    const avvisi = [];
    try{ await caricaCaratteri(); }
    catch(e){ avvisi.push('Il carattere DejaVu Sans non si è caricato: le immagini usano un carattere di ripiego, controllale prima di pubblicarle.'); }

    let copertina = null;
    if(d.copertina){
      try{ copertina = await caricaImmagine(d.copertina); }
      catch(e){
        avvisi.push('Copertina non utilizzabile (' + dominio(d.copertina) + '): il dominio non concede il permesso CORS o l\'indirizzo non risponde. '
          + 'Succede alle copertine non ancora migrate sullo Storage: le slide hanno lo sfondo neutro.');
      }
    } else {
      avvisi.push('La scheda non ha l\'indirizzo della copertina: le slide hanno lo sfondo neutro.');
    }

    let card = null;
    if(d.socialUrl){
      try{ card = await caricaImmagine(d.socialUrl); }
      catch(e){ avvisi.push('Manca la slide della scheda: l\'immagine per Instagram non si scarica (' + e.message + ').'); }
    } else {
      avvisi.push('Manca la slide della scheda: l\'immagine per Instagram non è ancora pronta. La prepara pagine.yml entro quattro ore dalla pubblicazione.');
    }

    const slide = [];
    // Il gancio regge senza le letture affini, non senza la frase: una riga
    // di titoli da sola su un fondo vuoto non aggancia nessuno, e la slide
    // si omette.
    if(d.pitch){
      slide.push(['gancio', 'Gancio', slideGancio(copertina, d)]);
      if(!d.affini) avvisi.push('Il gancio è senza «Per chi ha amato»: la scheda non ha le letture affini.');
    } else {
      avvisi.push('Manca la slide del gancio: la scheda non ha «Il libro in una frase».');
    }
    if(card) slide.push(['scheda', 'Scheda', slideScheda(card)]);
    // L'incipit e' un testo protetto: senza il consenso registrato non si
    // prepara nemmeno l'immagine.
    if(d.incipit && d.consenso){
      const parti = d.incipit.length > INCIPIT_SU_DUE ? spezzaIncipit(d.incipit) : [d.incipit];
      parti.forEach((testo, i) => {
        const n = parti.length > 1 ? ' ' + (i + 1) + '/' + parti.length : '';
        slide.push(['prime-righe' + (n ? '-' + (i + 1) : ''), 'Le prime righe' + n, slidePrimeRighe(copertina, d, testo, i + 1, parti.length)]);
      });
    }
    else if(d.incipit) avvisi.push('Manca la slide delle prime righe: l\'incipit c\'è, ma non il consenso a pubblicarlo.');
    else avvisi.push('Manca la slide delle prime righe: la scheda non ha l\'incipit.');

    // Il numero nel nome tiene l'ordine del carosello anche fra i file scaricati.
    const immagini = await Promise.all(slide.map(async ([tipo, etichetta, c], i) => {
      const blob = await new Promise((ok, ko) => c.toBlob(b => b ? ok(b) : ko(new Error('toBlob')), 'image/png'));
      return { nome: d.nomeBase + '-' + (i + 1) + '-' + tipo + '.png', etichetta, blob, url: URL.createObjectURL(blob) };
    }));
    return { immagini, avvisi };
  }

  // La didascalia da incollare nel post, come quella del sito, con in piu'
  // il profilo da taggare. L'indirizzo e' senza https:// perche' nelle
  // didascalie di Instagram i link non si cliccano.
  function didascalia(d){
    const righe = ['«' + (d.titolo || '') + '»' + (d.autore ? ' di ' + d.autore : '')];
    const edizione = [d.editore, d.anno].filter(Boolean).join(', ');
    if(edizione) righe.push(edizione);
    if(d.esordio) righe.push('Un esordio.');
    if(d.instagram) righe.push('@' + d.instagram);
    if(d.slug) righe.push('', 'La scheda completa è nel catalogo di Fantasy Italia — Nuove Proposte:', SITO + '/libri/' + d.slug + '/');
    righe.push('', IG_ETICHETTE + (d.esordio ? ' #esordio' : ''));
    return righe.join('\n');
  }

  function puoCondividere(immagini){
    // Solo su telefono e tablet, come nel sito: dal computer il pannello di
    // condivisione esiste, ma Instagram non c'e', e i file scaricati servono di piu'.
    const suTelefono = window.matchMedia && window.matchMedia('(pointer:coarse)').matches;
    if(!suTelefono || !navigator.canShare) return false;
    const file = immagini.map(i => new File([i.blob], i.nome, { type: 'image/png' }));
    return navigator.canShare({ files: file });
  }

  // La stessa logica di pulsanteInstagram in index.html: la didascalia negli
  // appunti subito, finche' il tocco e' "fresco"; poi il pannello di
  // condivisione con i soli file; e lo scaricamento come ripiego.
  async function consegna(immagini, testoDidascalia){
    let copiata = false;
    const appunti = navigator.clipboard && testoDidascalia
      ? navigator.clipboard.writeText(testoDidascalia).then(() => { copiata = true; }).catch(() => {})
      : Promise.resolve();
    const nota = () => copiata ? ' La didascalia è negli appunti: incollala nel post.' : '';

    if(puoCondividere(immagini)){
      const file = immagini.map(i => new File([i.blob], i.nome, { type: 'image/png' }));
      // Solo i file: se insieme si passa del testo, alcune app prendono
      // quello e lasciano cadere le immagini.
      const [esito] = await Promise.allSettled([navigator.share({ files: file }), appunti]);
      if(esito.status === 'fulfilled') return { testo: 'Nel pannello scegli Instagram: le immagini arrivano nell\'ordine dell\'anteprima.' + nota() };
      const nome = esito.reason && esito.reason.name;
      if(nome === 'AbortError') return { testo: '' };
      // Safari a volte rifiuta il primo tocco: le immagini sono pronte, il
      // secondo parte subito.
      if(nome === 'NotAllowedError') return { testo: 'Immagini pronte: tocca ancora il pulsante.', ritocca: true };
      // Qualunque altro errore: si ripiega sullo scaricamento.
    }

    await appunti;
    for(const i of immagini){
      const a = document.createElement('a');
      a.href = i.url; a.download = i.nome;
      document.body.appendChild(a); a.click(); a.remove();
      // Una pausa fra un file e l'altro: alcuni browser, a scaricamenti
      // troppo ravvicinati, tengono solo il primo.
      await new Promise(ok => setTimeout(ok, 400));
    }
    return { testo: 'Immagini scaricate (' + immagini.length + '). Se il browser chiede il permesso per più file, concedilo.' + nota() };
  }

  function libera(immagini){
    (immagini || []).forEach(i => URL.revokeObjectURL(i.url));
  }

  window.CaroselloInstagram = { prepara, consegna, didascalia, puoCondividere, libera };
})();
