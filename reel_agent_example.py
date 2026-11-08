#!/usr/bin/env python3
"""Agente per reel Instagram/TikTok (esempio).

Legge i video da una cartella di input, ne sceglie i momenti migliori con un
modello Claude (analizzando fotogrammi ridotti), monta un reel verticale con
ffmpeg, scrive caption e hashtag e salva tutto in una cartella "da approvare".
Poi, se disponibile, avvisa su Telegram.

Note:
- nel codice non ci sono chiavi: la chiave Anthropic si legge dal gestore
  credenziali del sistema (libreria keyring, servizio 'ig-agente', utente
  'anthropic') oppure dalla variabile d'ambiente ANTHROPIC_API_KEY;
- i percorsi e le opzioni personali vanno in un config.json accanto allo
  script (non pubblicarlo);
- il modulo telegram_notify (notifiche) e' opzionale e non e' incluso qui.

Requisiti: Python 3.10+, ffmpeg e ffprobe nel PATH,
`pip install anthropic keyring requests`.
"""
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import anthropic
import keyring

# ============================ CONFIGURAZIONE ============================
INBOX = Path("~/Google Drive/IG-Agente/1_Inbox").expanduser()
OUT = Path("~/Google Drive/IG-Agente/2_DaApprovare").expanduser()
STATE = Path(__file__).with_name("state.json")

# Durata del reel: proporzionale al materiale disponibile, tra min e max.
DURATA_MIN = 60              # secondi
DURATA_MAX = 120             # secondi
QUOTA_MATERIALE = 0.20       # 20% del girato: 360 s di video -> reel da 72 s
MIN_MATERIALE = 120          # sotto questi secondi di girato non si genera

NICHE = "fitness e allenamento"   # cambia con il tuo argomento
# Se False, ne' l'hook ne' la caption nominano esercizi o attrezzi: li
# commenti tu a voce nel video.
NOMI_ESERCIZI = False
TONE = "motivazionale ma concreto, mai esagerato"
# Descrivi qui com'e' fatto il tuo materiale e l'ordine tipico delle scene:
# il modello lo usa per montare i segmenti nella sequenza giusta.
STRUTTURA_SESSIONE = (
    "Il materiale e' una singola sessione ripresa in tanti brevi video. "
    "Di solito segue quest'ordine: introduzione, parte principale, conclusione.")
MODEL = os.environ.get("IG_MODEL", "claude-sonnet-5-5")

# Font per il testo a schermo (hook). Se il file non esiste, il testo
# non viene impresso nel video ma resta nel file di note.
FONT = os.environ.get("IG_FONT", "C:/Windows/Fonts/arialbd.ttf")

FRAME_EVERY = 4.0            # un fotogramma ogni N secondi di video
MAX_FRAMES = 90              # tetto ai fotogrammi inviati al modello
LATO_MAX_PX = 384            # lato lungo dei fotogrammi: meno px = meno costo
COSTI = Path(__file__).with_name("costi.csv")
# Prezzi in USD per milione di token (input, output): verifica sulla pagina
# prezzi di Anthropic, possono cambiare. Il costo stampato e' una stima.
PREZZI = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
MAX_FILES_PER_RUN = 30
MIN_FILE_AGE_S = 120         # ignora file modificati da meno di 2 minuti
ATTESA_MAX_MIN = 20          # attesa massima se i file stanno ancora arrivando
FPS = 30
USA_FOTO = False             # le foto in Inbox vengono ignorate
PHOTO_SECONDS = 2.5
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
# ========================================================================

# Valori in config.json accanto allo script: non si perdono quando
# sostituisci lo script. Usa le barre normali nei percorsi.
CONFIG = Path(__file__).with_name("config.json")
if CONFIG.exists():
    try:
        _c = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        sys.exit(f"config.json non valido: riga {e.lineno}, colonna {e.colno} "
                 f"({e.msg}).\nControlla che ogni riga, tranne l'ultima, "
                 "finisca con una virgola.")
    INBOX = Path(_c.get("inbox", INBOX)).expanduser()
    OUT = Path(_c.get("out", OUT)).expanduser()
    TONE = _c.get("tono", TONE)
    NOMI_ESERCIZI = _c.get("nomi_esercizi", NOMI_ESERCIZI)
    MODEL = _c.get("modello", MODEL)
    LATO_MAX_PX = _c.get("lato_max_px", LATO_MAX_PX)
    FRAME_EVERY = _c.get("fotogramma_ogni_s", FRAME_EVERY)
    MAX_FRAMES = _c.get("fotogrammi_max", MAX_FRAMES)
    DURATA_MIN = _c.get("durata_min", DURATA_MIN)
    DURATA_MAX = _c.get("durata_max", DURATA_MAX)
    QUOTA_MATERIALE = _c.get("quota_materiale", QUOTA_MATERIALE)
    MIN_MATERIALE = _c.get("min_materiale", MIN_MATERIALE)


def costruisci_prompt(target, ordine_file):
    elenco = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(ordine_file))
    if NOMI_ESERCIZI:
        regola_nomi = ("- se non sei sicuro del nome di un esercizio, resta "
                       "generico.\n")
        hook_info = "massimo 6 parole"
    else:
        regola_nomi = (
            "- il video non deve contenere testo con nomi di esercizi o "
            "attrezzi: l'autore\n  commentera' tutto a voce. L'hook e la "
            "caption non nominano esercizi, attrezzi,\n  ripetizioni o carichi; "
            "restano generici e parlano del video nel suo insieme.\n")
        hook_info = ("massimo 6 parole, generico e motivazionale o di "
                     "curiosita', senza nomi di esercizi")
    return f"""Sei il montatore di un account Instagram/TikTok su {NICHE}.
{STRUTTURA_SESSIONE}
I file qui sotto sono elencati nell'ordine di caricamento
(probabile ordine cronologico): verifica con le immagini e correggi se serve.

File:
{elenco}

Crea UN reel verticale di circa {target} secondi (sempre tra {DURATA_MIN} e
{DURATA_MAX}) che racconti la sessione.

Regole:
- apri con il momento piu' forte dell'intera sessione: l'hook deve funzionare
  nei primi 2 secondi; poi segui l'ordine della sessione descritto sopra;
- dai piu' spazio alla parte principale; apertura e chiusura restano brevi; se
  una fase non compare nel materiale, ignorala;
- usa materiale da tutti i video, almeno un segmento per file quando possibile;
  se un video mostra lo stesso esercizio piu' volte, scegli il momento migliore
  (azione chiara e completa);
- ogni segmento dura tra 2 e 6 secondi; nello stesso file i segmenti vanno in
  ordine di tempo crescente e non si sovrappongono;
- scegli inquadrature nitide con il soggetto ben visibile; scarta pause,
  momenti statici e fuori fuoco;
- la durata totale deve restare entro +/- 4 secondi da {target};
- usa solo i nomi file elencati e non superare la durata di ciascun file;
- non inventare cio' che non si vede.
{regola_nomi}
Rispondi SOLO con JSON valido in questa forma:
{{"segments":[{{"file":"nome.mp4","start":0.0,"end":3.0}}],
 "hook_text":"{hook_info}",
 "caption":"in italiano, tono {TONE}, 2-4 righe che raccontano il video
 senza inventare dettagli, piu' una call to action",
 "hashtags":["#esempio"],
 "title":"titolo breve per i miei appunti"}}
Gli hashtag sono 8-12, un mix di nicchia e di media diffusione."""


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} fallito:\n{r.stderr[-1500:]}")
    return r.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip())


def file_key(p):
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def avvisa(testo):
    """Messaggio Telegram; se manca la configurazione non blocca nulla."""
    try:
        import telegram_notify as tn
        token, chat = tn.credenziali()
        tn.api(token, "sendMessage", data={"chat_id": chat, "text": testo})
    except Exception as e:
        print(f"(Avviso Telegram non inviato: {e})")


def elenco_inbox(state):
    estensioni = VIDEO_EXT | (PHOTO_EXT if USA_FOTO else set())
    nuovi = [p for p in INBOX.iterdir()
             if p.is_file() and p.suffix.lower() in estensioni
             and file_key(p) not in state]
    return ordina_cronologico(nuovi)


def ordina_cronologico(files):
    """Ordine di ripresa: usa data e ora nel nome del file (es. 20250101_120000,
    tipico dei telefoni); se mancano, ripiega sulla data di modifica."""
    stampe = {}
    for p in files:
        m = re.search(r"(\d{8})[_-]?(\d{6})", p.name)
        if not m:
            return sorted(files, key=lambda p: p.stat().st_mtime)
        stampe[p] = m.group(1) + m.group(2)
    return sorted(files, key=lambda p: (stampe[p], p.name))


def attendi_caricamento_finito(state):
    """Se i file stanno ancora arrivando da Drive, aspetta (max ATTESA_MAX_MIN):
    serve che nessun file sia stato modificato di recente e che le dimensioni
    restino stabili, cosi' non si monta una sessione a meta'."""
    inizio = time.time()
    while True:
        files = elenco_inbox(state)
        if not files:
            return
        dimensioni = {p.name: p.stat().st_size for p in files}
        recente = min(time.time() - p.stat().st_mtime for p in files)
        if recente >= MIN_FILE_AGE_S:
            time.sleep(20)
            dopo = {p.name: p.stat().st_size for p in elenco_inbox(state)}
            if dopo == dimensioni:
                return
        if time.time() - inizio > ATTESA_MAX_MIN * 60:
            print("Attesa massima raggiunta: proseguo con quello che c'e'.")
            return
        print("File ancora in arrivo, attendo...")
        time.sleep(30)


def calcola_target(disponibile):
    return max(DURATA_MIN, min(DURATA_MAX, round(disponibile * QUOTA_MATERIALE)))


def sample_frames(path, idx, tmp, cap):
    """Estrae fotogrammi ridotti da un file. Restituisce (nome, secondo, jpg)."""
    if path.suffix.lower() in PHOTO_EXT:
        times = [None]
    else:
        d = duration(path)
        times, t = [], 0.5
        while t < d - 0.3:
            times.append(round(t, 1))
            t += FRAME_EVERY
        times = times or [0.0]
    if len(times) > cap:
        step = math.ceil(len(times) / cap)
        times = times[::step]
    frames = []
    for i, t in enumerate(times):
        out = tmp / f"f{idx}_{i}.jpg"
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if t is not None:
            cmd += ["-ss", str(t)]
        cmd += ["-i", str(path), "-frames:v", "1", "-vf",
                f"scale={LATO_MAX_PX}:{LATO_MAX_PX}:"
                "force_original_aspect_ratio=decrease",
                "-q:v", "5", str(out)]
        run(cmd)
        frames.append((path.name, t, out.read_bytes()))
    return frames


def get_api_key():
    """Legge la chiave da Gestione credenziali di Windows (servizio
    'ig-agente', utente 'anthropic'); ripiega su ANTHROPIC_API_KEY."""
    key = keyring.get_password("ig-agente", "anthropic")
    key = key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("Chiave API non trovata. Salvala in Gestione credenziali "
                 "lanciando salva_chiave.py.")
    key = key.strip()
    if not key.startswith("sk-ant-") or len(key) < 40:
        sys.exit(f"La chiave salvata non e' valida (lunghezza {len(key)}). "
                 "Rilancia salva_chiave.py e incollala con il tasto destro.")
    return key


def registra_costo(resp, n_frames):
    """Stampa e salva in costi.csv token e costo stimato della chiamata."""
    t_in, t_out = resp.usage.input_tokens, resp.usage.output_tokens
    prezzo = PREZZI.get(MODEL)
    if prezzo and MODEL == "claude-haiku-5-5" and t_in > 100_000:
        prezzo = (0.50, 2.50)  # fascia per prompt oltre 100.000 token
    costo = (t_in * prezzo[0] + t_out * prezzo[1]) / 1e6 if prezzo else None
    print(f"Token: {t_in} in, {t_out} out"
          + (f" - costo stimato ${costo:.3f}" if costo is not None else ""))
    try:
        nuovo = not COSTI.exists()
        with open(COSTI, "a", encoding="utf-8") as f:
            if nuovo:
                f.write("data,modello,fotogrammi,token_in,token_out,costo_usd\n")
            f.write(f"{datetime.now():%Y-%m-%d %H:%M},{MODEL},{n_frames},"
                    f"{t_in},{t_out},{'' if costo is None else round(costo, 4)}\n")
    except OSError:
        pass


def ask_plan(frames, info, target, ordine):
    client = anthropic.Anthropic(api_key=get_api_key())
    content = [{"type": "text", "text": costruisci_prompt(target, ordine)}]
    for name, t, jpg in frames:
        label = f"[{name} @ {t}s]" if t is not None else f"[{name} (foto)]"
        content.append({"type": "text", "text": label})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(jpg).decode()}})
    content.append({"type": "text", "text":
                    "Durata in secondi dei file: " + json.dumps(info)
                    + "\nRispondi solo con il JSON."})
    ultimo = ""
    for tentativo in (1, 2):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=8000,
                system=("Rispondi esclusivamente con un oggetto JSON valido, "
                        "senza testo prima o dopo e senza blocchi di codice."),
                messages=[{"role": "user", "content": content}])
        except anthropic.APIStatusError as e:
            sys.exit(f"Errore API {e.status_code}: {e.message}\n"
                     f"Risposta grezza: {e.response.text[:600] or '(vuota)'}\n"
                     f"Modello: {MODEL} - fotogrammi inviati: {len(frames)}")
        registra_costo(resp, len(frames))
        text = "".join(b.text for b in resp.content
                       if b.type == "text").strip()
        try:
            return estrai_json(text)
        except ValueError as e:
            ultimo = (f"stop_reason={resp.stop_reason}, "
                      f"blocchi={[b.type for b in resp.content]}, "
                      f"token in uscita={resp.usage.output_tokens}\n"
                      f"Inizio risposta: {text[:500]!r}")
            print(f"Risposta non valida (tentativo {tentativo}/2): {e}")
    sys.exit("Il modello non ha restituito un JSON valido.\n" + ultimo)


def estrai_json(text):
    """Estrae l'oggetto JSON anche se c'e' testo o ``` prima e dopo."""
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j <= i:
        raise ValueError("nessun JSON nella risposta")
    try:
        return json.loads(text[i:j + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON non valido ({e.msg})") from None


def validate_plan(plan, info, target):
    """Controlla il piano del modello e lo riporta entro i limiti reali."""
    segs, total = [], 0.0
    for s in plan.get("segments", []):
        name = s.get("file")
        if name not in info:
            continue
        if info[name] == "foto":
            segs.append({"file": name, "start": 0.0, "end": None,
                         "len": PHOTO_SECONDS})
            total += PHOTO_SECONDS
        else:
            st = max(0.0, float(s.get("start", 0)))
            en = min(info[name], float(s.get("end", 0)))
            if en - st < 1.0:
                continue
            segs.append({"file": name, "start": st, "end": en, "len": en - st})
            total += en - st
        if total >= target + 1:
            break
    if total > target + 0.5 and segs and segs[-1]["end"] is not None:
        new_len = segs[-1]["len"] - (total - target)
        if new_len >= 1.0:
            segs[-1]["end"] = segs[-1]["start"] + new_len
            segs[-1]["len"] = new_len
            total = target
    return segs, total


def render(segs, hook, out_path, folder):
    tmp = Path(tempfile.mkdtemp())
    clips = []
    for i, s in enumerate(segs):
        src = folder / s["file"]
        clip = tmp / f"c{i}.mp4"
        if s["end"] is None:  # foto con leggero zoom
            d = int(PHOTO_SECONDS * FPS)
            vf = ("scale=2160:3840:force_original_aspect_ratio=increase,"
                  "crop=2160:3840,"
                  f"zoompan=z='min(zoom+0.0008,1.12)':d={d}:s=1080x1920:fps={FPS},"
                  "format=yuv420p")
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                   "-vf", vf, "-frames:v", str(d), "-an",
                   "-c:v", "libx264", "-preset", "fast", "-crf", "20", str(clip)]
        else:
            vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
                  f"crop=1080:1920,fps={FPS},format=yuv420p")
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-ss", f"{s['start']:.2f}", "-t", f"{s['len']:.2f}",
                   "-i", str(src), "-vf", vf, "-an",
                   "-c:v", "libx264", "-preset", "fast", "-crf", "20", str(clip)]
        run(cmd)
        clips.append(clip)

    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))

    vf_final = "null"
    if hook and FONT and Path(FONT).exists():
        font = FONT.replace("\\", "/").replace(":", "\\:")
        safe = re.sub(r"[':\\%]", "", hook)
        vf_final = (f"drawtext=fontfile='{font}':text='{safe}':fontsize=76:"
                    "fontcolor=white:borderw=5:bordercolor=black:"
                    "x=(w-text_w)/2:y=h*0.16:enable='between(t,0,2.5)'")
    # Bitrate limitato (max 8 Mbps): file leggeri per Drive, Telegram e upload.
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-vf", vf_final, "-an", "-c:v", "libx264",
         "-crf", "21", "-maxrate", "8M", "-bufsize", "16M",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)])
    shutil.rmtree(tmp, ignore_errors=True)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "reel"


def main():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} non trovato nel PATH.")
    if not INBOX.exists():
        sys.exit(f"Cartella Inbox non trovata: {INBOX}\n"
                 "Controlla il percorso in config.json (o in INBOX).")
    OUT.mkdir(parents=True, exist_ok=True)
    state = set(json.loads(STATE.read_text())) if STATE.exists() else set()

    attendi_caricamento_finito(state)
    now = time.time()
    files = [p for p in elenco_inbox(state)
             if now - p.stat().st_mtime > MIN_FILE_AGE_S][:MAX_FILES_PER_RUN]
    if not files:
        print("Inbox vuota: nessun video da montare.")
        return

    info = {}
    for p in files:
        try:
            info[p.name] = ("foto" if p.suffix.lower() in PHOTO_EXT
                            else duration(p))
        except Exception as e:
            print(f"File ignorato ({p.name}): {e}")
    files = [p for p in files if p.name in info]
    disponibile = sum(PHOTO_SECONDS if v == "foto" else v for v in info.values())
    if disponibile < MIN_MATERIALE:
        print(f"Materiale insufficiente ({disponibile:.0f}s, ne servono almeno "
              f"{MIN_MATERIALE}s). Nessun video creato.")
        firma = "avviso:" + "|".join(p.name for p in files)
        if firma not in state:  # un solo avviso per lo stesso gruppo di file
            avvisa(f"Inbox: {len(files)} video per {disponibile:.0f} secondi "
                   f"complessivi. Ne servono almeno {MIN_MATERIALE} per "
                   "montare un reel: carica altro materiale.")
            state.add(firma)
            STATE.write_text(json.dumps(sorted(state)))
        return

    target = calcola_target(disponibile)
    print(f"{len(files)} video, {disponibile:.0f}s di girato -> reel da ~{target}s")

    tmp = Path(tempfile.mkdtemp())
    cap = max(2, MAX_FRAMES // len(files))
    frames = []
    for i, p in enumerate(files):
        frames += sample_frames(p, i, tmp, cap)
    shutil.rmtree(tmp, ignore_errors=True)

    plan = ask_plan(frames, info, target, [p.name for p in files])
    segs, total = validate_plan(plan, info, target)
    if not segs:
        sys.exit("Il piano del modello non contiene segmenti validi.")
    if total < target * 0.75:
        print(f"Attenzione: durata finale {total:.0f}s, sotto l'obiettivo.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = f"{stamp}_{slugify(plan.get('title', 'reel'))}"
    video_path = OUT / f"{base}.mp4"
    render(segs, plan.get("hook_text", ""), video_path, INBOX)

    caption = plan.get("caption", "").strip()
    tags = " ".join(plan.get("hashtags", []))
    (OUT / f"{base}.txt").write_text(f"{caption}\n\n{tags}\n", encoding="utf-8")
    usati = sorted({s["file"] for s in segs})
    meta = {"hook_text": plan.get("hook_text"), "caption": caption,
            "hashtags": plan.get("hashtags", []), "segments": segs,
            "duration": round(total, 1), "model": MODEL,
            "file_inbox_usati": usati,
            "file_inbox_elaborati": [p.name for p in files]}
    (OUT / f"{base}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Tutti i video del gruppo sono "consumati": appartengono alla stessa
    # sessione e non devono finire mescolati nel reel successivo.
    state |= {file_key(p) for p in files}
    STATE.write_text(json.dumps(sorted(state)))
    print(f"Creato: {video_path.name} ({total:.0f}s)")

    try:  # un errore di notifica non deve mai far perdere il video
        import telegram_notify
        telegram_notify.invia_in_attesa(OUT)
    except Exception as e:
        print(f"Notifica Telegram non inviata: {e}")


if __name__ == "__main__":
    main()
