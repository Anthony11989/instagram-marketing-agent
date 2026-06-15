# Instagram posting agent

Posts an image you supply to your Instagram business account. You give it an
image and a topic, Claude writes the caption, the agent sends both to you in
Telegram with Approve and Reject buttons, and it publishes only after you tap
Approve. It runs on GitHub Actions, so there is no server to pay for or keep
running. Total infrastructure cost is zero. The only charge is the Claude API
for captions, which is a fraction of a cent per post.

## Repo layout

```
ig_agent.py                  the agent
requirements.txt             python dependencies
inbox/                       put the image you want to post here
.github/workflows/post.yml   the workflow you run to post
.github/workflows/refresh.yml  keeps your access token alive
```

## What you need to collect once

You will gather a set of values and save each as a GitHub Actions Secret. The
full list is at the top of ig_agent.py. Grouped by where they come from:

- Meta app: IG_ACCESS_TOKEN, IG_USER_ID, FB_APP_ID, FB_APP_SECRET
- Cloudflare R2: S3_BUCKET, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_PUBLIC_BASE
- Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
- Anthropic: ANTHROPIC_API_KEY
- GitHub: GH_PAT (a fine-grained token with read and write on this repo's Secrets)

## How to post

1. Add your image to the inbox/ folder and push it to GitHub.
2. Go to the Actions tab, choose "Post to Instagram", click Run workflow.
3. Enter the image filename and a topic for the caption.
4. The photo and caption arrive in Telegram. Tap Approve to publish.

## Notes

- Only Instagram Business or Creator accounts can post through the API, and the
  account must be linked to a Facebook Page.
- The access token lasts 60 days. The refresh workflow renews it weekly so
  posting never stops. It needs the GH_PAT secret to write the new token back.
- The posting run waits while you decide. Approve within 30 minutes, or make the
  repo public so Action minutes are unlimited and free.
- The Telegram bot uses long polling. Do not also set a webhook on the same bot.

## Run it on your own computer (optional)

Copy the secret values into a file named .env in the project folder using the
same names, then:

```
pip install -r requirements.txt
python ig_agent.py inbox/yourphoto.jpg "what the caption is about"
```
