# Iranseda Crawler

A simple, resumable crawler that downloads MP3 episodes from the programs on [radio.iranseda.ir](http://radio.iranseda.ir) and records metadata for each file in a CSV. Useful for building Persian speech datasets.

> For research and educational use. The audio belongs to its rights holders, so keep the request rate polite and don't redistribute the files.

## Features

- Accepts program pages and single episode pages
- Resumes automatically: episodes already listed in the CSV are skipped
- Only complete, verified downloads are written to the CSV; partial files stay as `.part` and are retried on the next run
- Retries with exponential backoff, and pauses and reconnects when the server keeps failing
- Saves metadata: program, episode, channel, links, broadcast date, duration (minutes), download date

## Installation

```bash
git clone https://github.com/FatemehValeh/Iranseda_crawler.git
cd Iranseda_crawler
pip install requests beautifulsoup4
```

## Usage

Put one URL per line in `input.txt` (`#` starts a comment):

```
# Radio Goftego
http://radio.iranseda.ir/Program/?VALID=TRUE&ch=29&m=170128
```

Then run:

```bash
python iranseda_crawler.py                  # reads input.txt
python iranseda_crawler.py -i myprograms.txt
python iranseda_crawler.py "http://radio.iranseda.ir/Program/?VALID=TRUE&ch=29&m=170128"
```

If a run is interrupted, just run it again and it continues where it stopped.

## Output

```
<outdir>/<channel-slug>/
├── *.mp3
└── episodes.csv
```

`episodes.csv` columns: `file_name, program_name, episode_name, channel, link, episode_link, date_of_program, duration, download_date`

## Options

| Option | Description |
|---|---|
| `-i, --input` | File with URLs (default `input.txt`) |
| `-o, --outdir` | Where channel folders are created (default `.`) |
| `--flat` | Save directly into `--outdir`, no channel subfolder |
| `--delay` | Seconds between requests (default `2`) |
| `--retries` | Attempts per request (default `4`) |
| `--video` | Prefer MP4, fall back to MP3 |
| `--gregorian` | Write dates as Gregorian instead of Jalali |
| `--redownload-missing` | Re-fetch files listed in the CSV but missing on disk |

Run `python iranseda_crawler.py --help` for the full list.
