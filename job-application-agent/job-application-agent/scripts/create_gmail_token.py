"""One-time: authorise the agent to send email as you (gmail.send scope only).

1. Cloud Console > Google Auth Platform > Clients > Create client > "Desktop app".
   Download the JSON as client_secret.json into this folder (it is git-ignored).
2. python scripts/create_gmail_token.py
3. The script prints the gcloud command that stores the token in Secret Manager.
"""
import json

from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", scopes=["https://www.googleapis.com/auth/gmail.send"])
creds = flow.run_local_server(port=0)
with open("token.json", "w") as f:
    f.write(creds.to_json())
print(json.loads(creds.to_json()).keys())
print("\nNow store it in Secret Manager and delete the local copy:\n"
      "  gcloud secrets create gmail-oauth-token --data-file=token.json\n"
      "  rm token.json")
