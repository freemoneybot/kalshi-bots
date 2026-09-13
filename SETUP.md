# SETUP — get the bots running for free, start to finish

Written for someone who has never used GitHub. **No credit card. No terminal.
No installing anything.** About 10 minutes.

You need: an email address, and the folder of files this came in.

---

## Step 1 — Make a free GitHub account (2 min)

1. Go to **https://github.com/signup**
2. Type your email, click the green **Continue** button.
3. Make a password → **Continue**. Pick a username (this becomes part of your
   website address, so pick something you don't mind seeing —
   e.g. `anthonyk`) → **Continue**.
4. It asks "Receive occasional product updates?" — type `n` or `y`, either is fine
   → **Continue**.
5. Solve the little puzzle, click **Create account**.
6. It emails you an 8-digit code. Type it in.
7. It may ask "How many people?" / "What are you interested in?" — you can click
   **Skip personalization** or just pick anything. If it offers you GitHub
   Copilot or a paid plan, choose **Continue for free** / **Skip**.

**At no point does it ask for a card. If a page asks for payment details, you
are on a paid-plan page — back out; the free plan is all this needs.**

---

## Step 2 — Make a new PUBLIC repository (1 min)

1. Go to **https://github.com/new**
2. **Repository name**: type `kalshi-bots`
3. **Description**: leave blank.
4. Below that are two radio buttons, **Public** and **Private**.
   **Choose Public.** ⚠️ This matters: public repos get *unlimited free* Actions
   minutes and free Pages. Private ones don't.
5. Leave "Add a README file" **unchecked**, and both dropdowns on **None**.
6. Click the green **Create repository** button at the bottom.

You'll land on a mostly empty page headed "Quick setup — if you've done this
kind of thing before".

---

## Step 3 — Upload the files (3 min)

1. On that page, find the line **"uploading an existing file"** (it's a blue
   link in the sentence "…or push an existing repository from the command
   line"). Click it. If you can't find it, go straight to
   `https://github.com/YOURUSERNAME/kalshi-bots/upload/main`
2. Unzip `kalshi-bots-gha.zip` on your computer first. You'll get a folder with
   `bot`, `docs`, `record`, `.github`, `config.json`, `README.md`, `SETUP.md`
   inside it.
3. **Open that folder**, select **everything inside it** (Ctrl+A on Windows,
   Cmd+A on Mac) and **drag it onto the big dashed box** that says
   "Drag files here to add them to your repository".
   - Drag the *contents*, not the folder itself.
   - ⚠️ **The `.github` folder is hidden on a Mac.** In Finder press
     **Cmd + Shift + . (period)** to show hidden files, then drag it too. Without
     `.github` nothing will ever run. On Windows it is visible normally.
4. Wait for the file list to finish appearing (there are ~12 files). You should
   see `.github/workflows/bots.yml` in the list.
5. Scroll down to the box headed **"Commit changes"**, then click the green
   **Commit changes** button.

---

## Step 4 — Turn on Actions (30 sec)

1. At the top of your repo, click the **Actions** tab.
2. You'll see a page saying *"Workflows aren't being run on this forked
   repository"* or a green button **"I understand my workflows, go ahead and
   enable them"** — click it. If instead you immediately see a workflow called
   **kalshi-bots** in the left sidebar, Actions is already on. Good.

---

## Step 5 — Let the bots write to your repo (30 sec)

The job saves the win/loss record back into the repo, so it needs write
permission.

1. Click the **Settings** tab (top right of the repo, gear icon).
2. In the left sidebar scroll down to **Actions** → click it → click
   **General**.
3. Scroll to the bottom section, **"Workflow permissions"**.
4. Select **Read and write permissions** (the first radio button).
5. Click **Save**.

---

## Step 6 — Turn on the free website (1 min)

1. Still in **Settings**, in the left sidebar click **Pages**.
2. Under **"Build and deployment" → Source**, leave it on
   **Deploy from a branch**.
3. Under **Branch**, the first dropdown says `None` — change it to **main**.
4. The second dropdown next to it says `/ (root)` — change it to **/docs**.
5. Click **Save**.
6. Wait about a minute, then refresh that page. A box appears at the top:
   *"Your site is live at https://YOURUSERNAME.github.io/kalshi-bots/"*.

**That is your dashboard address.** Bookmark it.

---

## Step 7 — Start the bots (30 sec)

1. Click the **Actions** tab.
2. In the left sidebar click **kalshi-bots**.
3. On the right, click the **Run workflow** dropdown button, then the green
   **Run workflow** button inside it.
4. Refresh the page after ~10 seconds. A run appears with a yellow spinning
   dot. Click it to watch the log if you like — you'll see lines like
   `cycle 4: snapshot written, 3/6 live markets`.

That's it. The job runs for 5 hours 25 minutes, then starts its own replacement
automatically, forever. A backup timer also checks every 30 minutes that a run
is alive and restarts it if not. **You never have to touch it again.**

The dashboard updates every ~2 minutes (GitHub takes a minute or so to rebuild
the page after each update, so the "last updated" stamp will usually read
1–3 minutes old — that's normal, not broken).

---

## Optional — Discord posts

If you want every call posted to your Discord too:

1. In Discord: pick the channel → **Edit Channel** (the gear next to the
   channel name) → **Integrations** → **Webhooks** → **New Webhook** →
   **Copy Webhook URL**.
2. On GitHub: **Settings** → left sidebar **Secrets and variables** →
   **Actions** → green **New repository secret** button.
3. **Name**: type exactly `DISCORD_WEBHOOK_URL`
   **Secret**: paste the webhook URL.
4. Click **Add secret**.
5. Go to **Actions** → **kalshi-bots** → click the currently running run →
   **Cancel run** (top right), then **Run workflow** again to pick up the secret.

⚠️ **Never paste the webhook into `config.json`.** The repo is public; secrets
are not. The Secret box is the safe place.

---

## If something looks wrong

**The site says "MARKET CLOSED" for gold / silver / oil.** Correct and expected
on a weekend. Kalshi only lists those 15-minute contracts while those markets
trade. The three crypto ones run 24/7.

**The page says the numbers are old / the dot is red.** Go to **Actions** →
**kalshi-bots** and see whether a run is going. If not, click
**Run workflow**. (The 30-minute safety timer will also do it on its own.)

**A run has a red X.** Click it, click the failed job, and read the last few
red lines. The two usual causes: Step 5 (Read and write permissions) was
skipped, or the `.github` folder never got uploaded in Step 3.

**"Run workflow" button isn't there.** The `.github/workflows/bots.yml` file
didn't upload. Redo Step 3 for that folder (Mac: Cmd+Shift+. to unhide it).

---

## What it costs

Nothing, ever. Public repositories get unlimited GitHub Actions minutes and
free GitHub Pages hosting. There is no card on the account and no trial to
expire.
