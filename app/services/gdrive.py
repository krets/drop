"""Google Drive API v3 wrapper. Never reads file content back (no downloads)."""
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config

TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"


def drive_client(refresh_token: str):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folders(drive, parent_id: str = "root") -> list[dict]:
    if not parent_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid folder id")
    resp = drive.files().list(
        q=f"mimeType='{FOLDER_MIME}' and '{parent_id}' in parents and trashed=false",
        fields="files(id, name)",
        orderBy="name",
        pageSize=200,
    ).execute()
    return resp["files"]


def get_folder(drive, folder_id: str) -> dict:
    return drive.files().get(fileId=folder_id, fields="id, name, mimeType").execute()


# TODO: stream_upload(folder_id, name, mime_type, stream) -> file_id via MediaIoBaseUpload (resumable,
#       chunked from the request stream; never buffer to disk).
# TODO: rename_file(file_id, name), trash_file(file_id), upload_text(folder_id, name, text)
#       for the `<filename>.meta.txt` sidecar comments.
