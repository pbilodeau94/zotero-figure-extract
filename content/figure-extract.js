/* Zotero Figure Extract: main logic.
 * Loaded by bootstrap.js. Defines the global FigureExtract object. */

var FigureExtract = {
  id: null,
  version: null,
  rootURI: null,
  menuItemID: "figure-extract-menuitem",
  listeners: new Map(), // window -> {menu, handler}
  running: false,

  TAG: "figures-extracted",
  PREF_PREFIX: "figureExtract.",

  init({ id, version, rootURI }) {
    this.id = id;
    this.version = version;
    this.rootURI = rootURI;
  },

  shutdown() {
    this.listeners.clear();
  },

  log(msg) {
    Zotero.debug("[Figure Extract] " + msg);
  },

  // ---------- preferences ----------

  serverUrl() {
    let url = String(
      Zotero.Prefs.get(this.PREF_PREFIX + "serverUrl") || "http://localhost:5001"
    ).trim();
    return url.replace(/\/+$/, "");
  },

  // ---------- window / menu ----------

  addToWindow(win) {
    if (this.listeners.has(win)) return;
    const doc = win.document;
    const menu = doc.getElementById("zotero-itemmenu");
    if (!menu) return;
    const item = doc.createXULElement("menuitem");
    item.id = this.menuItemID;
    item.setAttribute("label", "Extract figures and tables");
    item.addEventListener("command", () => this.onMenuCommand(win));
    menu.appendChild(item);
    const handler = () => {
      let show = false;
      try {
        show = win.ZoteroPane.getSelectedItems().some(
          (i) => i.isRegularItem() || (i.isAttachment() && i.parentItemID)
        );
      } catch (e) {}
      item.hidden = !show;
    };
    menu.addEventListener("popupshowing", handler);
    this.listeners.set(win, { menu, item, handler });
  },

  removeFromWindow(win) {
    const entry = this.listeners.get(win);
    if (entry) {
      entry.menu.removeEventListener("popupshowing", entry.handler);
      entry.item.remove();
      this.listeners.delete(win);
    }
    const el = win.document.getElementById(this.menuItemID);
    if (el) el.remove();
  },

  // ---------- command ----------

  async onMenuCommand(win) {
    if (this.running) {
      Services.prompt.alert(win, "Figure Extract", "An extraction is already running.");
      return;
    }
    this.running = true;
    try {
      await this.run(win);
    } catch (e) {
      Zotero.logError(e);
      Services.prompt.alert(win, "Figure Extract", "Unexpected error: " + (e && e.message ? e.message : e));
    } finally {
      this.running = false;
    }
  },

  async run(win) {
    const selected = win.ZoteroPane.getSelectedItems();
    const { targets, notes } = await this.resolveTargets(selected);
    if (!targets.length) {
      Services.prompt.alert(
        win,
        "Figure Extract",
        "No PDF attachment found in the selection." + (notes.length ? "\n\n" + notes.join("\n") : "")
      );
      return;
    }

    const baseUrl = this.serverUrl();
    try {
      await this.checkHealth(win, baseUrl);
    } catch (e) {
      Services.prompt.alert(
        win,
        "Figure Extract: service unreachable",
        `Could not reach the figure-extractor service at ${baseUrl}.\n\n` +
          `${e.message}\n\nStart the backend (see the README) or fix the URL in ` +
          `Settings > Figure Extract.`
      );
      return;
    }

    // Skip items already tagged unless the user confirms.
    const already = targets.filter((t) => t.parent.hasTag(this.TAG));
    let queue = targets;
    if (already.length) {
      const redo = Services.prompt.confirm(
        win,
        "Figure Extract",
        `${already.length} of ${targets.length} item(s) already carry the tag "${this.TAG}".\n\n` +
          `Extract them again? Existing figure attachments are kept, so re-extraction adds duplicates.\n\n` +
          `OK: re-extract them. Cancel: skip them.`
      );
      if (!redo) queue = targets.filter((t) => !already.includes(t));
    }
    const skipped = targets.length - queue.length;
    if (!queue.length) return;

    const pw = new Zotero.ProgressWindow({ closeOnClick: false });
    pw.changeHeadline("Extracting figures and tables");
    pw.show();
    const icon = Zotero.ItemTypes.getImageSrc("attachmentPDF");
    const results = [];

    for (const t of queue) {
      const title = t.parent.getDisplayTitle();
      const line = new pw.ItemProgress(icon, title);
      line.setProgress(5);
      try {
        const r = await this.processOne(win, t, baseUrl, (pct, text) => {
          line.setProgress(pct);
          if (text) line.setText(text);
        });
        line.setProgress(100);
        line.setText(`${title} (${r.figures} figures, ${r.tables} tables)`);
        results.push({ title, ok: true, ...r });
      } catch (e) {
        Zotero.logError(e);
        line.setError();
        line.setText(`${title}: ${e.message}`);
        results.push({ title, ok: false, error: e.message });
      }
    }

    const okRes = results.filter((r) => r.ok);
    const failed = results.filter((r) => !r.ok);
    const empty = okRes.filter((r) => r.figures + r.tables === 0);
    const nFig = okRes.reduce((a, r) => a + r.figures, 0);
    const nTab = okRes.reduce((a, r) => a + r.tables, 0);
    let summary = `Done. ${okRes.length} item(s) processed, ${nFig} figures and ${nTab} tables imported.`;
    if (empty.length) summary += ` ${empty.length} item(s) had nothing detected.`;
    if (failed.length) summary += ` ${failed.length} failed.`;
    if (skipped) summary += ` ${skipped} skipped (already extracted).`;
    pw.addDescription(summary);
    pw.startCloseTimer(failed.length ? 15000 : 8000);
  },

  // ---------- selection handling ----------

  async resolveTargets(selected) {
    const targets = [];
    const notes = [];
    const seen = new Set();
    for (const item of selected) {
      let parent, pdf;
      if (item.isRegularItem()) {
        parent = item;
        pdf = await this.findPDF(item);
      } else if (item.isAttachment()) {
        if (!item.parentItemID) {
          notes.push(`"${item.getDisplayTitle()}" is a standalone attachment and cannot hold child figures.`);
          continue;
        }
        parent = Zotero.Items.get(item.parentItemID);
        pdf = item.isPDFAttachment() ? item : await this.findPDF(parent);
      } else {
        continue;
      }
      if (!parent || !pdf) {
        if (parent) notes.push(`"${parent.getDisplayTitle()}" has no PDF attachment.`);
        continue;
      }
      if (seen.has(parent.id)) continue;
      seen.add(parent.id);
      targets.push({ parent, pdf });
    }
    return { targets, notes };
  },

  async findPDF(parent) {
    try {
      const best = await parent.getBestAttachment();
      if (best && best.isPDFAttachment() && (await best.fileExists())) return best;
    } catch (e) {}
    for (const id of parent.getAttachments()) {
      const a = Zotero.Items.get(id);
      if (a && a.isPDFAttachment() && (await a.fileExists())) return a;
    }
    return null;
  },

  // ---------- network ----------

  async fetchWithTimeout(win, url, opts, ms) {
    const ctl = new win.AbortController();
    const timer = win.setTimeout(() => ctl.abort(), ms);
    try {
      return await win.fetch(url, Object.assign({}, opts, { signal: ctl.signal }));
    } catch (e) {
      if (e && e.name === "AbortError") throw new Error(`Request timed out after ${Math.round(ms / 1000)} s.`);
      throw new Error(e && e.message ? e.message : String(e));
    } finally {
      win.clearTimeout(timer);
    }
  },

  async checkHealth(win, baseUrl) {
    const resp = await this.fetchWithTimeout(win, baseUrl + "/health", {}, 5000);
    if (!resp.ok) throw new Error(`Health check returned HTTP ${resp.status}.`);
    let data = null;
    try { data = await resp.json(); } catch (e) {}
    if (!data || data.service !== "figure-extractor") {
      throw new Error("The service answered but does not look like figure-extractor.");
    }
  },

  async download(win, baseUrl, filename, destPath) {
    const resp = await this.fetchWithTimeout(
      win,
      `${baseUrl}/download/${encodeURIComponent(filename)}`,
      {},
      120000
    );
    if (!resp.ok) throw new Error(`Download of ${filename} failed (HTTP ${resp.status}).`);
    const buf = new Uint8Array(await resp.arrayBuffer());
    await IOUtils.write(destPath, buf);
  },

  // ---------- per-item work ----------

  async processOne(win, target, baseUrl, progress) {
    const { parent, pdf } = target;
    const tmpDir = PathUtils.join(
      Zotero.getTempDirectoryPath(),
      "figure-extract-" + Zotero.Utilities.randomString(8)
    );
    await IOUtils.makeDirectory(tmpDir, { createAncestors: true });
    try {
      progress(10, "Uploading PDF");
      const pdfPath = await pdf.getFilePathAsync();
      const bytes = await IOUtils.read(pdfPath);
      const form = new win.FormData();
      form.append("file", new win.Blob([bytes], { type: "application/pdf" }), PathUtils.filename(pdfPath));
      const resp = await this.fetchWithTimeout(win, baseUrl + "/extract", { method: "POST", body: form }, 600000);
      if (!resp.ok) {
        let detail = "";
        try { detail = (await resp.text()).slice(0, 200); } catch (e) {}
        throw new Error(`Extraction failed (HTTP ${resp.status}) ${detail}`.trim());
      }
      const payload = await resp.json();
      const data = (payload && payload.data) || {};
      const figures = Array.isArray(data.figures) ? data.figures : [];
      const tables = Array.isArray(data.tables) ? data.tables : [];
      const metaName = data.metadata_filename || null;

      progress(40, "Downloading results");
      let sidecar = [];
      let metaPath = null;
      if (metaName) {
        try {
          metaPath = PathUtils.join(tmpDir, this.safeName(metaName));
          await this.download(win, baseUrl, metaName, metaPath);
          const parsed = JSON.parse(await IOUtils.readUTF8(metaPath));
          if (Array.isArray(parsed)) sidecar = parsed;
        } catch (e) {
          this.log("Could not read caption JSON: " + e.message);
          metaPath = null;
        }
      }
      const capByFile = new Map();
      for (const s of sidecar) {
        if (s && s.renderURL) capByFile.set(this.baseName(s.renderURL), s);
      }

      const entries = [];
      for (const [kind, list] of [["Figure", figures], ["Table", tables]]) {
        for (const f of list) {
          if (!f || !f.renderURL) continue;
          const file = this.baseName(f.renderURL);
          const meta = Object.assign({}, f, capByFile.get(file) || {});
          entries.push({ kind, file, meta });
        }
      }
      entries.sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === "Figure" ? -1 : 1;
        return this.nameOrder(a.meta.name) - this.nameOrder(b.meta.name);
      });

      let nFig = 0;
      let nTab = 0;
      let i = 0;
      for (const e of entries) {
        i++;
        progress(40 + Math.round((50 * i) / entries.length));
        const imgPath = PathUtils.join(tmpDir, this.safeName(e.file));
        try {
          await this.download(win, baseUrl, e.file, imgPath);
        } catch (err) {
          this.log(err.message);
          continue;
        }
        const title = this.makeTitle(e.kind, e.meta);
        const att = await Zotero.Attachments.importFromFile({
          file: Zotero.File.pathToFile(imgPath),
          libraryID: parent.libraryID,
          parentItemID: parent.id,
          contentType: "image/png",
          fileBaseName: this.safeName(title).replace(/\.[^.]+$/, "") || undefined,
        });
        att.setField("title", title);
        await att.saveTx();
        if (e.kind === "Figure") nFig++; else nTab++;
      }

      if (metaPath && this.boolPref("storeCaptionJson")) {
        const att = await Zotero.Attachments.importFromFile({
          file: Zotero.File.pathToFile(metaPath),
          libraryID: parent.libraryID,
          parentItemID: parent.id,
          contentType: "application/json",
          fileBaseName: "figure-captions",
        });
        att.setField("title", "Figure and table captions (JSON)");
        await att.saveTx();
      }

      if (this.boolPref("tagParent") && nFig + nTab > 0) {
        parent.addTag(this.TAG);
        await parent.saveTx();
      }
      progress(100);
      return { figures: nFig, tables: nTab };
    } finally {
      try {
        await IOUtils.remove(tmpDir, { recursive: true, ignoreAbsent: true });
      } catch (e) {
        this.log("Temp cleanup failed: " + e.message);
      }
    }
  },

  // ---------- helpers ----------

  boolPref(name) {
    const v = Zotero.Prefs.get(this.PREF_PREFIX + name);
    return v === undefined ? true : !!v;
  },

  baseName(p) {
    return String(p).split(/[\\/]/).pop();
  },

  safeName(s) {
    return Zotero.File.getValidFileName(String(s)) || "file";
  },

  nameOrder(name) {
    const n = parseInt(String(name == null ? "" : name).replace(/\D+/g, ""), 10);
    const supp = /^s/i.test(String(name || "")) ? 100000 : 0;
    return isNaN(n) ? 1e9 : n + supp;
  },

  /** Build "Figure 2: caption start" from defensive metadata. */
  makeTitle(kind, meta) {
    const type = /table/i.test(String(meta.figType || kind)) ? "Table" : "Figure";
    const name = meta.name != null && String(meta.name).trim() !== "" ? String(meta.name).trim() : "";
    let cap = typeof meta.caption === "string" ? meta.caption : "";
    cap = cap
      .replace(/\s+/g, " ")
      .replace(/^\s*(fig(ure)?s?\.?|table)\s*S?\d+[A-Za-z]?\s*[.:|\-]?\s*/i, "")
      .trim();
    if (cap.length > 80) {
      cap = cap.slice(0, 80).replace(/\s+\S*$/, "") + "...";
    }
    const head = name ? `${type} ${name}` : type;
    return cap ? `${head}: ${cap}` : head;
  },
};
