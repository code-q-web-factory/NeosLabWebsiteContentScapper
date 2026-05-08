# Neos Lab Website Content Scapper Prototype

Small scraper for `https://lab.neos.eu/sitemap.xml`.

It scrapes pages whose URLs start with:

- `https://lab.neos.eu/thinktank/glossar`
- `https://lab.neos.eu/thinktank/publikationen`

It also follows matching links found inside `main#main`, downloads allowed assets referenced inside that content area, rewrites local links for offline viewing, writes Markdown files into `./LabsWebsiteContent` next to the script, and stores assets flat in `./LabsWebsiteContent/Media`.

For responsive image markup, the scraper prefers `img[data-src]` and ignores generated `srcset` variants, so it downloads the original image URL instead of every generated size.

Relative links that are not scraped or downloaded are rewritten to absolute `https://lab.neos.eu/...` links in the generated Markdown.

Custom YouTube players are converted into linked poster images, for example `[![poster](Media/...jpg)](https://www.youtube.com/watch?v=...)`.

Inline SVG icons and XML processing instructions are removed before Markdown conversion to keep button/link text clean.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run

From this folder:

```bash
python scrape_labs_content.py
```

To override the output path:

```bash
python scrape_labs_content.py --output-dir /tmp/LabsWebsiteContent
```

The scraper uses a deliberately defensive 2.1 second wait after every request. A full run currently takes roughly 35 minutes, so it is best started during a quiet time, for example at 1am.

The output path is configured in `scrape_labs_content.py`:

```python
OUTPUT_DIR = BASE_DIR / "LabsWebsiteContent"
```

The script deletes and recreates that output folder on each run.
