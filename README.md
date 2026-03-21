# Valorant Rank Analyzer

A desktop GUI tool that fetches match history for any Valorant player and generates charts, graphs, and interactive HTML reports — all in one click.

---

![app screenshot](https://github.com/BrandonBahret/ValorantMatchTrends/blob/main/app_screenshot.png)

## Features

- 🎯 Look up any player by **Riot ID** (name + tag) across all regions
- 📊 Generates **charts & graphs**: RR history, lobby rank distribution, agent stats, role breakdowns, and more
- 🌐 Produces a self-contained **interactive HTML report** per player
- 📄 **PDF report** export via ReportLab
- 🗂️ Outputs everything neatly into a **per-player folder** under `outputs/`
- ⚡ Runs analysis in a background thread — UI stays responsive
- 💾 Caches player lookups to avoid repeat API calls

---

## Requirements

- Python **3.10+**
- A **Henrik Dev API key** — get one free at [https://docs.henrikdev.xyz](https://docs.henrikdev.xyz)

---

## Installation

1. **Clone or download** this repository.

2. **Install dependencies** from the requirements file:

   ```bash
   pip install -r requirements.txt
   ```

3. **Run the app:**

   ```bash
   python main.py
   ```

---

## Usage

1. Enter your **Henrik API key** in the settings field (saved automatically to `config.json`).
2. Type the player's **Name** and **Tag** (e.g. `PlayerName` / `NA1`), then select their **region**.
3. Click **Look Up Player** to load the acts/episodes they've played in.
4. Check the **acts** you want to analyze.
5. Set your preferred **match count** and **timespan** (days).
6. Click **Generate** — outputs are saved to `outputs/<PlayerName>/`.
7. When complete, you'll be prompted to open the output folder directly.

> ⚠️ Requesting too many matches will be slow and can trigger Henrik API rate limits.

---

## Output Files

All results are saved under `outputs/<PlayerName>#<Tag>_<timestamp>/`:

---


## Configuration

`config.json` is created automatically on first run and stores your API key:

```json
{
  "api_key": "your-henrik-api-key-here"
}
```

You can also paste the key directly into the GUI — it will be saved on the next run.

---

## License

Apache 2.0
