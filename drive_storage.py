"""
Google Drive integration for Ntsacom Studios photo gallery.

Setup instructions:
1. Go to https://console.cloud.google.com/
2. Create a project (or select existing)
3. Enable the Google Drive API:
   - APIs & Services → Library → search "Google Drive API" → Enable
4. Create a Service Account:
   - APIs & Services → Credentials → Create Credentials → Service Account
   - Give it a name (e.g. "ntsacom-gallery")
   - Download the JSON key file
   - Save it as 'credentials.json' in the project root
5. Create a folder in your Google Drive for photos
6. Share that folder with the service account email
   (e.g. ntsacom-gallery@your-project.iam.gserviceaccount.com)
   Give it "Editor" access
7. Set GOOGLE_DRIVE_FOLDER_ID in your .env to the folder ID
   (the ID is in the folder's URL: https://drive.google.com/drive/folders/<FOLDER_ID>)

Environment variables:
- GOOGLE_CREDENTIALS_FILE: path to service account JSON (default: credentials.json)
- GOOGLE_DRIVE_FOLDER_ID: ID of the shared Drive folder for uploads
- USE_GOOGLE_DRIVE: set to '1' to enable Drive storage (default: '0')
"""

import os
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive']

_service = None


def get_drive_service():
    """Get or create the Google Drive API service."""
    global _service
    if _service is not None:
        return _service

    credentials_file = os.environ.get('GOOGLE_CREDENTIALS_FILE', 'credentials.json')
    credentials_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), credentials_file
    )

    if not os.path.exists(credentials_path):
        raise FileNotFoundError(
            f"Google credentials file not found at {credentials_path}. "
            "See drive_storage.py for setup instructions."
        )

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    _service = build('drive', 'v3', credentials=credentials)
    return _service


def get_folder_id():
    """Get the target Google Drive folder ID from environment."""
    folder_id = os.environ.get('GOOGLE_DRIVE_FOLDER_ID', '')
    if not folder_id:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID environment variable not set.")
    return folder_id


def upload_to_drive(filepath, filename, mimetype='image/jpeg'):
    """
    Upload a file to Google Drive.
    Returns the Drive file ID.
    """
    service = get_drive_service()
    folder_id = get_folder_id()

    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }

    media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()

    return file.get('id')


def download_from_drive(drive_file_id):
    """
    Download a file from Google Drive.
    Returns a BytesIO buffer with the file contents.
    """
    service = get_drive_service()
    request = service.files().get_media(fileId=drive_file_id)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return buffer


def delete_from_drive(drive_file_id):
    """Delete a file from Google Drive."""
    service = get_drive_service()
    try:
        service.files().delete(fileId=drive_file_id).execute()
        return True
    except Exception as e:
        print(f"Drive delete error: {e}")
        return False


def get_drive_download_url(drive_file_id):
    """
    Get a direct download link for a Drive file.
    Note: File must be shared or use service account access.
    """
    return f"https://drive.google.com/uc?export=download&id={drive_file_id}"


def make_file_public(drive_file_id):
    """Make a file publicly accessible via link."""
    service = get_drive_service()
    permission = {
        'type': 'anyone',
        'role': 'reader'
    }
    service.permissions().create(
        fileId=drive_file_id,
        body=permission
    ).execute()


def is_drive_enabled():
    """Check if Google Drive storage is enabled."""
    return os.environ.get('USE_GOOGLE_DRIVE', '0') == '1'
