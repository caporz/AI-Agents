# Reel Agent

A Python script that turns a folder of raw videos into **a vertical reel ready for review**. It uses a Claude model to select the best moments, edits the video with FFmpeg, generates a caption and hashtags, and can optionally send you a Telegram notification.

This project is a **demonstration example**: it contains a single file, `reel_agent_example.py`. It does not include API keys, tokens, or personal data.

## What it does

1. Reads new videos from an input folder (`Inbox`).
2. If files are still being uploaded (for example, from a cloud-synced folder), waits up to 20 minutes for the uploads to complete.
3. Extracts **downscaled video frames** from each video and sends them to a Claude model along with a prompt describing the available footage.
4. The model returns an **editing plan** (which segments to use and in what order), a caption, and hashtags.
5. The script validates the plan, edits the reel with FFmpeg (1080x1920, no audio), and saves the output in a `To Approve` folder.
6. If the optional `telegram_notify` module is available, it sends a preview via Telegram.

Audio is intentionally excluded: the workflow assumes that you will add it yourself (voice-over or music) before publishing.

```text
Inbox/  --->  frames  --->  Claude  --->  editing plan
                                             |
                    To Approve/  <---  FFmpeg + caption
                    (.mp4  .txt  .json)
```

## Requirements

- Python 3.10 or later
- `ffmpeg` and `ffprobe` available in the system `PATH`
- An Anthropic API key
- Python libraries: `pip install anthropic keyring requests`

## Installation

```bash
git clone <your-repository>
cd <your-repository>
python -m pip install anthropic keyring requests
```

### Storing the API key

The API key **must never be hardcoded or stored in a repository file**. The script looks for it in the following order:

1. In the operating system's credential manager (Windows Credential Manager, macOS Keychain, Linux Secret Service), using service `ig-agente` and username `anthropic`.
2. In the `ANTHROPIC_API_KEY` environment variable.

To store the key in the credential manager without displaying it on screen:

```bash
python -c "import keyring, getpass; keyring.set_password('ig-agente', 'anthropic', getpass.getpass('API key: '))"
```

On Windows, paste the key using the **right mouse button**: Ctrl+V may insert a control character instead of the key in some terminals. The script rejects keys that do not start with `sk-ant-` or are too short, and reports this in the error message.

## Configuration

Personal settings are stored in a `config.json` file **in the same directory as the script**. This file must not be published (see the Security and Privacy section). Example:

```json
{
  "inbox": "~/ReelAgent/1_Inbox",
  "out": "~/ReelAgent/2_ToApprove",
  "durata_min": 60,
  "durata_max": 75,
  "tono": "motivational but practical, never exaggerated"
}
```

Use forward slashes (`/`) on Windows as well. In JSON, every line except the last one must end with a comma. If the file is invalid, the script reports the line and column of the error.

| Key | Description | Default |
|---|---|---|
| `inbox` | Folder containing the videos to be edited | `~/Google Drive/IG-Agente/1_Inbox` |
| `out` | Folder where generated reels are saved | `~/Google Drive/IG-Agente/2_DaApprovare` |
| `durata_min`, `durata_max` | Minimum and maximum reel duration, in seconds | 60 and 120 |
| `quota_materiale` | Proportion of the footage used for the reel (within the duration limits) | 0.20 |
| `min_materiale` | Minimum footage duration required to generate a reel | 120 |
| `tono` | Caption tone | Motivational but practical |
| `nomi_esercizi` | If `false`, hooks and captions do not mention exercises or equipment | `false` |
| `modello` | Claude model to use | `claude-sonnet-5-5` |
| `lato_max_px` | Maximum frame dimension sent to the model | 384 |
| `fotogramma_ogni_s` | Extract one frame every N seconds of video | 4.0 |
| `fotogrammi_max` | Maximum number of frames per API call | 90 |

Two settings must be changed **directly in the code**, near the top of the file:

- `NICHE`: the account's topic (the prompt describes it as an Instagram/TikTok account about this subject).
- `STRUTTURA_SESSIONE`: a description of the footage and the typical order of scenes. The model uses it to arrange the segments in the correct sequence. The more precise the description, the better the result.

The opening text font is configured through the `IG_FONT` environment variable (the path to a `.ttf` file). The default is a Windows font: on macOS and Linux, it must be changed, otherwise the text will not be rendered on the video.

## Usage

```bash
python reel_agent_example.py
```

Each execution creates **a single reel** using all videos in the input folder (up to 30). The output, saved in `out`, consists of three files with the same name (`YYYYMMDD_HHMM_title`):

- `.mp4`: the reel;
- `.txt`: caption and hashtags, ready to copy;
- `.json`: editing details (segments, duration, model, list of videos used and processed).

### Automated execution

The script is designed to run at scheduled times.

- **Windows:** Use Task Scheduler, for example, to run it every day at 6 PM:
  ```bash
  schtasks /create /tn "Reel Agent" /tr "C:\path\to\run.bat" /sc daily /st 18:00
  ```
  The `run.bat` file should launch Python and write the output to a log file.
- **macOS/Linux:** Use `cron` or a `systemd` timer.

The computer must be powered on and the user must be logged in, because the API key is stored in the user's credential manager.

## How it works in detail

### File ordering

Videos are sorted by the date and time included in their filenames (for example, `20250101_120000.mp4`, a common format used by smartphones). If the filenames do not contain a timestamp, the file modification date is used instead. This order is passed to the model as the "likely chronological order", and the model can adjust it by analyzing the images.

### Upload completion check

Before starting, the script checks that no file has been modified in the last two minutes and that file sizes remain stable. Otherwise, it waits up to 20 minutes to avoid editing an incomplete recording session.

### Reel duration

`duration = footage_ratio x total_footage`, constrained by `durata_min` and `durata_max`. If the available footage is shorter than `min_materiale`, no reel is generated. If `telegram_notify` is available, a single notification is sent for that group of files.

### Frames and costs

For each video, the script extracts downscaled frames (maximum dimension: `lato_max_px`) at regular intervals, subject to the overall `fotogrammi_max` limit. Frames are the most expensive part of the process: reducing their size or number lowers costs, but may reduce editing precision.

For each API call, the script prints the number of tokens used and an estimated cost, then records them in `costi.csv` (date, model, frames, tokens, cost). Prices are stored in a table in the code (`PREZZI`) and **may be outdated**: check Anthropic's pricing page for the latest rates.

### The model's editing plan

The model returns a JSON object containing `segments` (file, start, end), `hook_text`, `caption`, `hashtags`, and `title`. If the response is not valid JSON, the script retries once. If the second attempt also fails, it stops and prints the reason (stop reason, tokens, and the beginning of the response).

Before editing, the plan is **validated**: segments exceeding the actual video duration are corrected, segments shorter than one second are discarded, and the total duration is limited to the requested length (segments exceeding the limit are removed and the final segment is shortened).

The prompt asks the model to use footage from all videos, but **this is not enforced by the code**: some videos may be left out of the final reel.

### Video editing

FFmpeg crops each segment to 1080x1920 at 30 fps, joins them, applies a maximum bitrate of 8 Mbps (to keep files lightweight for cloud storage and uploading), and, if the font is available, overlays `hook_text` during the first 2.5 seconds. No audio is included.

### Processing state

`state.json` (stored alongside the script) keeps track of previously processed files, identified by filename, size, and modification date. All videos in a group are marked as used after editing, preventing them from being included in the next reel. To process the same videos again, delete `state.json`.

### Telegram notifications (optional)

If a `telegram_notify` module containing the following functions is available in the same directory, the script uses it:

- `credenziali()` returns `(token, chat_id)`;
- `api(token, metodo, **kwargs)` calls the Telegram Bot API;
- `invia_in_attesa(cartella)` sends videos that have not yet been notified.

If the module is missing or fails, the reel is still saved and a warning is printed.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `config.json non valido: riga N` | A comma is missing, or an extra comma appears at the end of the last line |
| `Cartella Inbox non trovata` | Incorrect path in `config.json`; check that the folder exists |
| `La chiave salvata non e' valida` | The API key may have been pasted incorrectly; save it again |
| API error 400 with an empty response and no `request-id` | The request may not be reaching Anthropic: a proxy, antivirus, or corporate network may be intercepting the traffic, or the API key may be corrupted |
| API credit error | The prepaid balance in the Anthropic console may be exhausted |
| `Il modello non ha restituito un JSON valido` | The response may have been truncated or may contain additional text; check the printed details |
| Opening text does not appear | The file specified in `IG_FONT` does not exist |
| No reel is generated | The footage is shorter than `min_materiale`, or files have been modified less than two minutes ago |

## Security and Privacy

- The following files **must not be committed to the repository**: `config.json` (personal paths), `state.json`, `costi.csv`, log files, videos, and generated output. Example `.gitignore`:
  ```gitignore
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
- API keys must be stored in the credential manager, never in the source code. If a key is accidentally included in a commit, **revoke it** from the Anthropic console. Deleting the file is not enough, because the key remains in Git history.
- Frames extracted from your videos are sent to Anthropic's API: do not use footage that you do not want to share with the service.

## Known Limitations

- Output quality depends on the prompt and the description of the footage in `STRUTTURA_SESSIONE`.
- The model analyzes low-resolution frames and may select moments that are not sufficiently sharp. In that case, increase `lato_max_px` or `fotogrammi_max`, keeping in mind that costs will also increase.
- Default paths and the font are designed for Windows and a cloud-synced folder; they must be adapted for other operating systems.
- The editing process does not include audio, transitions, or subtitles.
