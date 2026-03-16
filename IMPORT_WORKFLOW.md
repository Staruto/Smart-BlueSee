# Cloud to Local Import Workflow

This guide shows how to export from email and Moodle to local files, then ingest into KB safely.

## 1) Security first

- Do not hardcode username/password into scripts.
- Input password only when prompted at runtime.
- Rotate credentials if they were ever shared in plain text.

## 2) Email export to local (.eml)

Use IMAP fetcher:

```powershell
C:/x/void/llm/client/.venv/Scripts/python.exe email_fetch_imap.py --username YOUR_EMAIL --folder INBOX --limit 300
```

Optional date filter:

```powershell
C:/x/void/llm/client/.venv/Scripts/python.exe email_fetch_imap.py --username YOUR_EMAIL --since 01-Jan-2026
```

Output defaults to:

- `knowledge/import/raw/email_eml`

## 3) Moodle export to local (.html)

Install browser automation once:

```powershell
C:/x/void/llm/client/.venv/Scripts/python.exe -m pip install playwright
C:/x/void/llm/client/.venv/Scripts/python.exe -m playwright install chromium
```

Run exporter (manual login in opened browser):

```powershell
C:/x/void/llm/client/.venv/Scripts/python.exe moodle_web_export.py --base-url https://moodle.nottingham.ac.uk/ --max-pages 150
```

Output defaults to:

- `knowledge/import/raw/moodle_html`

## 4) Import into KB (redaction on by default)

Dry-run first:

```powershell
C:/x/void/llm/client/.venv/Scripts/python.exe kb_importer.py --email-path knowledge/import/raw/email_eml --moodle-path knowledge/import/raw/moodle_html --docs-path YOUR_DOCS_DIR --dry-run
```

Actual import:

```powershell
C:/x/void/llm/client/.venv/Scripts/python.exe kb_importer.py --email-path knowledge/import/raw/email_eml --moodle-path knowledge/import/raw/moodle_html --docs-path YOUR_DOCS_DIR
```

Then reload in chatbot:

- `/reload`

## 5) Output folders

- Email sections: `knowledge/import/email`
- Moodle sections: `knowledge/import/moodle`
- Docs sections: `knowledge/import/docs`
- Import manifests: `knowledge/import/manifests`
