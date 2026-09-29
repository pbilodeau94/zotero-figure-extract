/* Zotero Figure Extract: bootstrap entry point. */

var FigureExtract;
var pluginID;
var rootURI;
var prefsPaneID;

function install() {}
function uninstall() {}

async function startup({ id, version, rootURI: root }) {
  pluginID = id;
  rootURI = root;
  // Zotero 7+ reads prefs.js from the plugin root by itself.
  try {
    prefsPaneID = await Zotero.PreferencePanes.register({
      pluginID: id,
      src: root + "prefs.xhtml",
      label: "Figure Extract",
    });
  } catch (e) {
    Zotero.logError(e);
  }
  const ctx = { Zotero };
  ctx._globalThis = ctx;
  Services.scriptloader.loadSubScript(root + "content/figure-extract.js", ctx);
  FigureExtract = ctx.FigureExtract;
  FigureExtract.init({ id, version, rootURI: root });
  for (const win of Zotero.getMainWindows()) {
    FigureExtract.addToWindow(win);
  }
}

function onMainWindowLoad({ window }) {
  if (FigureExtract) FigureExtract.addToWindow(window);
}

function onMainWindowUnload({ window }) {
  if (FigureExtract) FigureExtract.removeFromWindow(window);
}

function shutdown() {
  if (FigureExtract) {
    for (const win of Zotero.getMainWindows()) {
      FigureExtract.removeFromWindow(win);
    }
    FigureExtract.shutdown();
  }
  if (prefsPaneID && Zotero.PreferencePanes.unregister) {
    try { Zotero.PreferencePanes.unregister(prefsPaneID); } catch (e) {}
  }
  FigureExtract = undefined;
}
