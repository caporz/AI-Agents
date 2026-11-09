# Reel Agent

Script Python che trasforma una cartella di video grezzi in **un reel verticale pronto da rivedere**: sceglie i momenti migliori con un modello Claude, monta il video con FFmpeg, scrive caption e hashtag e, se vuoi, ti avvisa su Telegram.

Il progetto è un **esempio**: contiene un solo file, `reel_agent_example.py`. Non include chiavi, token o dati personali.

## Cosa fa

1. Legge i video nuovi da una cartella di input (`Inbox`).
2. Se i file stanno ancora arrivando (per esempio da una cartella sincronizzata nel cloud), aspetta fino a 20 minuti che finiscano.
3. Estrae dei **fotogrammi ridotti** da ogni video e li invia a un modello Claude insieme a un prompt che descrive il tuo materiale.
4. Il modello risponde con un **piano di montaggio** (quali segmenti usare e in che ordine), una caption e degli hashtag.
5. Lo script controlla il piano, monta il reel con FFmpeg (1080x1920, senza audio) e salva tutto in una cartella `Da approvare`.
6. Se esiste il modulo opzionale `telegram_notify`, invia l'anteprima su Telegram.

L'audio non viene aggiunto di proposito: il flusso prevede che sia tu a inserirlo (voce, musica) prima di pubblicare.

```
Inbox/  --->  fotogrammi  --->  Claude  --->  piano di montaggio
                                                    |
                          Da approvare/  <---  FFmpeg + caption
                          (.mp4  .txt  .json)
```

## Requisiti

- Python 3.10 o superiore
- `ffmpeg` e `ffprobe` nel `PATH`
- Una chiave API Anthropic
- Librerie Python: `pip install anthropic keyring requests`

## Installazione

```bash
git clone <il-tuo-repository>
cd <il-tuo-repository>
python -m pip install anthropic keyring requests
```

### Salvare la chiave API

La chiave **non va scritta nel codice né in un file del repository**. Lo script la cerca, in quest'ordine:

1. nel gestore credenziali del sistema (Windows Credential Manager, macOS Keychain, Linux Secret Service), servizio `ig-agente`, utente `anthropic`;
2. nella variabile d'ambiente `ANTHROPIC_API_KEY`.

Per salvarla nel gestore credenziali senza mostrarla a schermo:

```bash
python -c "import keyring, getpass; keyring.set_password('ig-agente', 'anthropic', getpass.getpass('Chiave API: '))"
```

Su Windows incolla con il **tasto destro del mouse**: con Ctrl+V il terminale può inserire un carattere di controllo al posto della chiave. Lo script rifiuta chiavi che non iniziano con `sk-ant-` o sono troppo corte, e il messaggio d'errore lo segnala.

## Configurazione

I valori personali stanno in un file `config.json` **accanto allo script**. Non va pubblicato (vedi la sezione sulla sicurezza). Esempio:

```json
{
  "inbox": "~/ReelAgent/1_Inbox",
  "out": "~/ReelAgent/2_DaApprovare",
  "durata_min": 60,
  "durata_max": 75,
  "tono": "motivazionale ma concreto, mai esagerato"
}
```

Usa le barre normali (`/`) anche su Windows. In JSON ogni riga, tranne l'ultima, deve finire con una virgola. Se il file non è valido lo script lo dice, con riga e colonna.

| Chiave | Significato | Predefinito |
|---|---|---|
| `inbox` | Cartella dei video da montare | `~/Google Drive/IG-Agente/1_Inbox` |
| `out` | Cartella dei reel generati | `~/Google Drive/IG-Agente/2_DaApprovare` |
| `durata_min`, `durata_max` | Limiti di durata del reel, in secondi | 60 e 120 |
| `quota_materiale` | Quota del girato che diventa reel (entro i limiti) | 0.20 |
| `min_materiale` | Sotto questi secondi di girato non si genera nulla | 120 |
| `tono` | Tono delle caption | motivazionale ma concreto |
| `nomi_esercizi` | Se `false`, hook e caption non nominano esercizi o attrezzi | `false` |
| `modello` | Modello Claude da usare | `claude-sonnet-5-5` |
| `lato_max_px` | Lato lungo dei fotogrammi inviati al modello | 384 |
| `fotogramma_ogni_s` | Un fotogramma ogni N secondi di video | 4.0 |
| `fotogrammi_max` | Tetto ai fotogrammi per chiamata | 90 |

Due impostazioni si cambiano **direttamente nel codice**, in cima al file:

- `NICHE`: l'argomento dell'account (nel prompt compare come "un account Instagram/TikTok su ...").
- `STRUTTURA_SESSIONE`: la descrizione del tuo materiale e dell'ordine tipico delle scene. Il modello la usa per montare i segmenti nella sequenza giusta. Più è precisa, migliore è il risultato.

Il font del testo d'apertura si imposta con la variabile d'ambiente `IG_FONT` (percorso di un file `.ttf`). Il valore predefinito è un font di Windows: su macOS e Linux va cambiato, altrimenti il testo non viene impresso nel video.

## Uso

```bash
python reel_agent_example.py
```

Per ogni esecuzione lo script crea **un solo reel** con tutti i video presenti nella cartella di input (fino a 30). L'output, in `out`, è composto da tre file con lo stesso nome (`AAAAMMGG_HHMM_titolo`):

- `.mp4`: il reel;
- `.txt`: caption e hashtag, da copiare;
- `.json`: dettagli del montaggio (segmenti, durata, modello, elenco dei video usati ed elaborati).

### Esecuzione automatica

Lo script è pensato per girare a orari fissi.

- **Windows:** Utilità di pianificazione, per esempio ogni giorno alle 18:
  ```
  schtasks /create /tn "Reel Agent" /tr "C:\percorso\avvia.bat" /sc daily /st 18:00
  ```
  dove `avvia.bat` lancia Python e scrive l'output in un file di log.
- **macOS/Linux:** `cron` o un timer `systemd`.

Il computer deve essere acceso con l'accesso attivo, perché la chiave sta nel gestore credenziali dell'utente.

## Come funziona, in dettaglio

### Ordine dei file
I video vengono ordinati per data e ora scritte nel nome del file (per esempio `20250101_120000.mp4`, tipico dei telefoni). Se i nomi non le contengono, si usa la data di modifica. L'ordine è passato al modello come "probabile ordine cronologico", e il modello può correggerlo guardando le immagini.

### Attesa del caricamento
Prima di partire lo script controlla che nessun file sia stato modificato negli ultimi 2 minuti e che le dimensioni restino stabili. In caso contrario aspetta, fino a un massimo di 20 minuti, per non montare una sessione a metà.

### Durata del reel
`durata = quota_materiale x girato`, limitata tra `durata_min` e `durata_max`. Se il girato è sotto `min_materiale`, non si genera nulla e, se esiste `telegram_notify`, si invia un solo avviso per quel gruppo di file.

### Fotogrammi e costo
Per ogni video si estraggono fotogrammi ridotti (lato lungo `lato_max_px`) a intervalli regolari, con un tetto totale `fotogrammi_max`. I fotogrammi sono la parte che costa di più: ridurne dimensione o numero abbassa il costo, a scapito della precisione dei tagli.

A ogni chiamata lo script stampa i token usati e un costo stimato, e li aggiunge a `costi.csv` (data, modello, fotogrammi, token, costo). I prezzi sono in una tabella nel codice (`PREZZI`) e **possono essere obsoleti**: verificali sulla pagina prezzi di Anthropic.

### Il piano del modello
Il modello restituisce un JSON con `segments` (file, inizio, fine), `hook_text`, `caption`, `hashtags` e `title`. Se la risposta non è un JSON valido, lo script riprova una volta e, se fallisce ancora, si ferma stampando il motivo (stop reason, token, inizio della risposta).

Prima del montaggio il piano viene **validato**: i segmenti fuori dalla durata reale dei video vengono corretti, quelli più brevi di un secondo scartati e il totale limitato alla durata richiesta (i segmenti oltre il limite vengono tolti e l'ultimo viene accorciato).

Il prompt chiede al modello di usare materiale da tutti i video, ma **non è un obbligo verificato dal codice**: un video può restare fuori dal reel.

### Montaggio
FFmpeg ritaglia ogni segmento a 1080x1920 a 30 fps, li unisce, applica un bitrate massimo di 8 Mbps (file leggeri per cloud e upload) e, se il font esiste, imprime l'`hook_text` nei primi 2,5 secondi. Non c'è audio.

### Stato
`state.json` (accanto allo script) memorizza i file già elaborati, identificati da nome, dimensione e data. Tutti i video di un gruppo vengono segnati come usati dopo il montaggio, così non finiscono nel reel successivo. Per rielaborare gli stessi video, cancella `state.json`.

### Notifiche Telegram (opzionale)
Se nella stessa cartella esiste un modulo `telegram_notify` con queste funzioni, lo script lo usa:

- `credenziali()` restituisce `(token, chat_id)`;
- `api(token, metodo, **kwargs)` chiama l'API dei bot Telegram;
- `invia_in_attesa(cartella)` invia i video non ancora notificati.

Se il modulo manca o fallisce, il reel viene comunque salvato e viene stampato un avviso.

## Risoluzione dei problemi

| Sintomo | Causa probabile |
|---|---|
| `config.json non valido: riga N` | Manca una virgola, o ce n'è una di troppo sull'ultima riga |
| `Cartella Inbox non trovata` | Percorso errato in `config.json`; controlla che esista |
| `La chiave salvata non e' valida` | Chiave incollata male: salvala di nuovo |
| Errore API 400 con risposta vuota e nessun `request-id` | La richiesta non arriva ad Anthropic: proxy, antivirus o rete aziendale che intercettano il traffico, oppure chiave rotta |
| Errore API sul credito | Saldo prepagato esaurito nella console Anthropic |
| `Il modello non ha restituito un JSON valido` | Risposta troncata o con testo extra: guarda i dettagli stampati |
| Il testo d'apertura non compare | Il file di `IG_FONT` non esiste |
| Nessun reel creato | Girato sotto `min_materiale`, oppure file modificati da meno di 2 minuti |

## Sicurezza e privacy

- Nel repository **non devono finire** `config.json` (percorsi personali), `state.json`, `costi.csv`, i log, né i video e i file generati. Esempio di `.gitignore`:
  ```
  config.json
  state.json
  costi.csv
  *.log
  *.mp4
  *.txt
  *.json
  !config.example.json
  __pycache__/
  ```
- Le chiavi stanno nel gestore credenziali, mai nel codice. Se una chiave finisce per sbaglio in un commit, **revocala** dalla console: cancellare il file non basta, perché resta nella cronologia di Git.
- I fotogrammi dei tuoi video vengono inviati all'API di Anthropic: non usare materiale che non vuoi condividere con il servizio.

## Limiti noti

- Il risultato dipende dalla qualità del prompt e dalla descrizione del materiale in `STRUTTURA_SESSIONE`.
- Il modello vede fotogrammi a bassa risoluzione: può scegliere momenti poco nitidi. In quel caso aumenta `lato_max_px` o `fotogrammi_max`, tenendo presente che il costo cresce.
- I percorsi predefiniti e il font sono pensati per Windows e per una cartella sincronizzata nel cloud; su altri sistemi vanno adattati.
- Il montaggio non include audio, transizioni o sottotitoli.

## Licenza

Aggiungi qui la licenza che preferisci (per esempio MIT) prima di pubblicare il repository.
