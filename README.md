# Google Indexing API — Automated Submitter

Waiting for Google to notice your new pages is frustrating. The `smart_indexer.py` script takes control of that process by pinging Google directly the moment you hit publish. It uses the Google Indexing API to push your URLs straight into their queue.

I built this tool for [Ramesh Das — Custom Web Development & AI Engineering](https://www.rameshdas.dev) to handle large-scale site management efficiently. It tracks exactly what has been submitted, manages your daily API quota (200 requests), and categorizes your pages so you always know your indexing status.

## Authenticating Your Access

You cannot talk to the Indexing API without proving who you are. That requires a Service Account JSON key.

Here is the exact process to get one:
1. Open the Google Cloud Console and select your project.
2. Search for the **Indexing API** and enable it.
3. Head over to **IAM & Admin > Service Accounts** and create a new account. Name it something recognizable, like `Google Indexing API`.
4. Click on that new account, navigate to the **Keys** tab, and create a new JSON key.
5. Download the file. Move it into the `` directory and rename it to `google-service-account.json`.

### Linking the Bot to Google Search Console (Crucial Step)

You aren't done yet. Generating the key just proves the bot exists. You still have to give it explicit permission to touch your website's data. Without this step, Google will reject every single submission with a 403 Forbidden error.

1. Open your downloaded `google-service-account.json` file in any text editor.
2. Find the line that says `"client_email"` and copy the email address next to it (it usually looks something like `bot-name@project-id.iam.gserviceaccount.com`).
3. Log into [Google Search Console](https://search.google.com/search-console).
4. Select your target website property from the top-left dropdown menu.
5. In the left sidebar, scroll down and click on **Settings**.
6. Click on **Users and permissions**.
7. Click the blue **Add User** button.
8. Paste the `client_email` address you copied earlier.
9. **CRITICAL**: Change the permission level dropdown to **Owner**. The Indexing API requires strict Owner-level privileges to function properly.
10. Click **Add**.

Your service account is now fully authorized to index pages on your behalf.

## Managing Your URLs

The script needs to know what to index. By default, it looks for `main-urls.txt` in this directory.

You can group your URLs logically using headers. The script reads these headers and organizes your final terminal report around them so you aren't staring at a wall of raw links.

```text
# === Core Services ===
https://www.rameshdas.dev/hire-full-stack-developer.html

# === Locations ===
https://www.rameshdas.dev/hire-developers-usa.html
```

## Running the Submitter

You have complete control over how the script runs through command-line arguments.

**1. Standard Run**
Send your pending URLs to Google up to your daily limit.
```bash
python3 smart_indexer.py
```

**2. Check Your Status**
Want to see where you stand without burning through your quota? This prints the dashboard.
```bash
python3 smart_indexer.py --report
```

**3. Safe Preview**
Test exactly what the script intends to do before making any real API calls.
```bash
python3 smart_indexer.py --dry-run
```

**4. Target a Specific text file**
You don't have to use the default text file. You can point the script at any custom list or even a live XML sitemap.
```bash
python3 smart_indexer.py --url-file path-of-txt-file 
```

**4. Target a Specific Source**
You don't have to use the default text file. You can point the script at any custom list or even a live XML sitemap.
```bash
python3 smart_indexer.py --url-file https://www.rameshdas.dev/sitemap-main.xml
```

**5. Limit Your Batch**
Cap the number of submissions for the current run.
```bash
python3 smart_indexer.py --limit 50
```

**6. Force a Re-submission**
Google occasionally drops pages from the index. You can force the script to ignore its internal memory and resend URLs.
```bash
python3 smart_indexer.py --force
```

**7. Resetting States**
If you need to start fresh or retry failed attempts, you can edit the script's memory.
- Reset a single page: `python3 smart_indexer.py --reset-url https://www.rameshdas.dev/tokyo.html`
- Retry server errors: `python3 smart_indexer.py --reset-failed`
- Wipe the slate clean: `python3 smart_indexer.py --reset`

## Folder Breakdown

Everything related to indexing lives in this directory so it stays out of your way.
- **`smart_indexer.py`**: The engine running the logic.
- **`google-service-account.json`**: The Google Service Account authentication key.
- **`indexing_state_v3.json`**: The script's memory. It tracks submissions, failures, and pending pages.
- **`indexing_runs.log`**: A human-readable record of every action the script takes.
- **`indexing_runs_history.jsonl`**: Machine-readable logs for parsing.
