# Zotero Figure Extract

A Zotero plugin that pulls the figures and tables out of a paper's PDF and files them under the paper as child attachments. It works with Zotero 7, 8 and 9 (built against 9.0.x).

The extraction itself is done by a separate service, [Huang-lab/figure-extractor](https://github.com/Huang-lab/figure-extractor), which wraps Allen AI's [pdffigures2](https://github.com/allenai/pdffigures2) behind a small Flask API. The plugin only talks to that service over HTTP.

## What it does

Right-click a library item, or its PDF attachment, and choose **Extract figures and tables**. For each selected item the plugin:

1. Finds the PDF attachment.
2. Sends it to the extractor service.
3. Downloads the PNG for every figure and table, plus the caption JSON.
4. Imports each PNG as a stored child attachment, titled from its caption, for example `Figure 2: Kaplan-Meier curves for relapse-free survival` or `Table 4: Clinical outcome according to genotype`.
5. Saves the caption JSON as one child attachment titled "Figure and table captions (JSON)". It is an attachment rather than a note so the raw file stays intact.
6. Adds the tag `figures-extracted` to the parent item.

Several items can be selected at once. They run one after another with a progress window and a summary at the end. Items that already carry the `figures-extracted` tag are skipped unless you confirm re-extraction. Re-extraction does not delete earlier figure attachments, so it will add duplicates that you can trash by hand.

## Install

1. Download `zotero-figure-extract-<version>.xpi` from the Releases page.
2. In Zotero, open Tools > Plugins, click the gear icon, choose "Install Plugin From File" and pick the .xpi.
3. Restart Zotero if it asks.

## Run the backend

You need Docker. Clone the backend and build the image:

```
git clone https://github.com/Huang-lab/figure-extractor.git
cd figure-extractor
docker build -t figure-extractor .
docker run -d --rm --name figex -p 5001:5001 figure-extractor
```

Check that it is up:

```
curl http://localhost:5001/health
```

You should see `"service":"figure-extractor"` and `"status":"healthy"`. Stop it with `docker stop figex`.

The backend is a separate project with its own license and its own maintainers. This plugin does not bundle it. The plugin expects these endpoints: `GET /health`, `POST /extract` (multipart field `file`) and `GET /download/<filename>`.

## Preferences

Open Settings (Preferences on macOS) and choose **Figure Extract**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Extractor service URL | `http://localhost:5001` | Where the backend runs. |
| Also store the caption JSON | on | Adds the caption JSON as a child attachment. |
| Tag the parent item | on | Adds `figures-extracted` to the parent after a successful run. |

The same values are stored as hidden prefs in the Config Editor: `extensions.zotero.figureExtract.serverUrl`, `.storeCaptionJson` and `.tagParent`.

## Caption JSON

pdffigures2 writes one entry per figure or table with fields such as `caption`, `figType` (`Figure` or `Table`), `name`, `page`, `renderURL`, `regionBoundary` and `captionBoundary`. The plugin reads `caption`, `figType` and `name` for titles and treats every field as optional. If a caption is missing the title is just `Figure 2` or `Table 4`.

## Limitations

- Scanned PDFs have no text layer, so pdffigures2 usually finds nothing.
- Unusual layouts (figures without a standard "Figure N" or "Table N" caption, supplementary material bundled in odd ways, multi-panel tables split across pages) can be missed or cropped badly. An empty result is reported in the summary and the item is not tagged.
- Only PDF attachments are used. Standalone PDFs with no parent item cannot hold child attachments and are skipped.
- Figures are stored as PNG at the resolution the backend renders.
- One extraction runs at a time, and large PDFs can take a while. The upload times out after 10 minutes.
- Sending a PDF to the service means the file leaves Zotero. Run the backend locally if the papers are sensitive.

## Troubleshooting

- **"Service unreachable"**: check that the container is running (`docker ps`) and that the URL in preferences matches the port you published. If Zotero and Docker are on different machines, use the host address instead of localhost.
- **"does not look like figure-extractor"**: something else is answering on that port. Check `curl http://localhost:5001/health`.
- **Nothing detected**: open the PDF and check that it has selectable text and captions that start with "Figure" or "Table".
- **Menu item missing**: it only appears when the selection contains a regular item or a child attachment.
- **Other problems**: enable Help > Debug Output Logging and look for lines starting with `[Figure Extract]`.

## Build from source

```
./scripts/build-xpi.sh
```

This writes `dist/zotero-figure-extract-<version>.xpi` with `manifest.json` at the zip root. Pushing a tag such as `v0.1.0` (matching the version in `manifest.json`) runs the GitHub Actions workflow, which builds the .xpi and attaches it to a release. `updates.json` points Zotero at that release asset for automatic updates, so add an entry for each new version.

## License

MIT, see `LICENSE`. The backend service has its own license.
