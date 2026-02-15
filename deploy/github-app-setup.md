# Creating Your Squadron GitHub App

Squadron uses a **dedicated GitHub App per repository** (single-tenant model). Each repo you onboard to Squadron needs its own GitHub App, which provides:

- **Webhook delivery** — GitHub pushes events (issues, PRs, comments) to your Squadron instance
- **API authentication** — Squadron uses the app's installation token to interact with your repo (create branches, comment, open PRs, merge)
- **Bot identity** — actions appear as `your-app-name[bot]` in the repo

## Step 1: Create the App

Go to: **https://github.com/settings/apps/new**

(For org-owned repos: `https://github.com/organizations/<ORG>/settings/apps/new`)

Fill in:

| Field | Value |
|-------|-------|
| **GitHub App name** | `squadron-<your-repo>` (must be globally unique) |
| **Homepage URL** | Your repo URL (e.g. `https://github.com/you/your-repo`) |
| **Webhook URL** | Leave blank for now — you'll set this after deployment |
| **Webhook secret** | Generate one: `openssl rand -hex 32` — **save this value** |

## Step 2: Set Permissions

Under **Repository permissions**, enable:

| Permission | Access | Why |
|------------|--------|-----|
| **Contents** | Read & write | Clone repo, create branches, push commits |
| **Issues** | Read & write | Read issues, add comments, manage labels, close issues |
| **Pull requests** | Read & write | Create PRs, add reviews, merge |
| **Metadata** | Read-only | Required (always on) |

Leave all other permissions at "No access".

**Organization permissions**: None needed.

**Account permissions**: None needed.

## Step 3: Subscribe to Events

Under **Subscribe to events**, check:

- [x] **Issues**
- [x] **Issue comment**
- [x] **Pull request**
- [x] **Pull request review**
- [x] **Push**

## Step 4: Set Visibility

Under **Where can this GitHub App be installed?**:

> **Only on this account**

This is critical for security — it prevents anyone else from installing your app and sending webhooks to your Squadron instance.

## Step 5: Create the App

Click **Create GitHub App**. Note the **App ID** shown on the next page.

## Step 6: Generate a Private Key

On the app settings page, scroll to **Private keys** and click **Generate a private key**.

A `.pem` file will download. This is your app's authentication credential — **store it securely**.

## Step 7: Install the App on Your Repo

1. Go to your app's settings page: `https://github.com/settings/apps/<your-app-name>`
2. Click **Install App** in the sidebar
3. Select your account/org
4. Choose **"Only select repositories"** and pick your target repo
5. Click **Install**

After installation, note the **Installation ID** from the URL:
```
https://github.com/settings/installations/<INSTALLATION_ID>
```

## What You Need for Deployment

After completing the steps above, you should have four values:

| Value | Where to find it | Used as |
|-------|-------------------|---------|
| **App ID** | App settings page, top of the page | `SQ_APP_ID` secret |
| **Private Key** | The `.pem` file you downloaded | `SQ_PRIVATE_KEY` secret |
| **Installation ID** | URL after installing the app | `SQ_INSTALLATION_ID` secret |
| **Webhook Secret** | The value you generated in Step 1 | `SQ_WEBHOOK_SECRET` secret |

These go into your repo's **Settings → Secrets and variables → Actions** as repository secrets. See the [deployment guide](azure-container-apps/) for the full setup.

## After Deployment: Set the Webhook URL

Once your Squadron instance is running, go back to the app settings:

1. **Settings → Developer settings → GitHub Apps → your app**
2. Under **Webhook**, set:
   - **Active**: checked
   - **Webhook URL**: `https://<YOUR-SQUADRON-FQDN>/webhook`
   - **Content type**: `application/json`
   - **Secret**: the same value you generated earlier
3. Click **Save changes**

## Configuring `bot_username`

By default, Squadron filters out its own events using `squadron[bot]` as the bot username. Since your app has a custom name, update `.squadron/config.yaml`:

```yaml
project:
  name: your-project
  owner: your-github-username
  repo: your-repo
  bot_username: "your-app-name[bot]"   # ← match your GitHub App name
```

## Verifying the Webhook

After setting the webhook URL, go to **Advanced** in your app settings to see **Recent Deliveries**. Create a test issue in your repo — you should see a delivery with a green checkmark (200 response).

## Security Notes

- **Keep the private key secret.** Anyone with the key can authenticate as your app and get write access to repos where it's installed.
- **Keep the webhook secret secret.** It's used to verify that incoming webhooks are actually from GitHub.
- **Set the app to "Only on this account."** This prevents strangers from installing the app and sending webhooks to your server.
- Squadron validates the installation ID and repository name on every incoming webhook — even if someone forges a request, it will be rejected unless it matches your configured installation.
