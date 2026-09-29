# Changelog

## 0.1.1

- Fix: extraction failed in Zotero 9 because the plugin called a temp-directory function that does not exist. Now uses `Zotero.getTempDirectory()`.
- Verified end to end in Zotero 9.0.6 against a live figure-extractor container.

## 0.1.0

- First release.
- Context menu item "Extract figures and tables" for regular items and PDF attachments.
- Sends the PDF to a figure-extractor service, imports each figure and table as a child attachment titled from its caption.
- Optional caption JSON attachment and optional `figures-extracted` tag on the parent.
- Preferences pane for the service URL and the two options.
