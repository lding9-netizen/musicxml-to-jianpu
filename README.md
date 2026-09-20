# MusicXML to Jianpu

Standalone browser application for converting MusicXML sheet music into Chinese numbered
notation (jianpu / scale degrees). The conversion runs locally in the browser; files are
never uploaded.

## Run in this Codespace

From the repository root:

```bash
python3 -m http.server 8000
```

Open the forwarded port **8000** in the VS Code Ports panel, or visit:

```text
http://localhost:8000/
```

You can also preview the site with the VS Code Live Server extension by opening
`index.html` and choosing **Open with Live Server**.

## Use

1. Drop a `.mxl`, `.musicxml`, or `.xml` file onto the page, or click the upload area.
2. Select the parts to display and optionally override the key or notation size.
3. Use **Download score** to create a standalone printable HTML excerpt, or **Print** to
	print/save the current selection as PDF.

The **sample** button loads a local demonstration score covering multiple parts, lyrics,
rests, accidentals, tuplets, grace notes, ties, repeats, and percussion notation.

## GitHub Pages

The app is a static site. Publish the repository root with GitHub Pages; `index.html` is
the entry point and has no build step or external dependency.
