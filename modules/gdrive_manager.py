import os
import re
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from dotenv import load_dotenv

load_dotenv()

CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "token.json"
SCOPES = ['https://www.googleapis.com/auth/drive']

class GoogleDriveManager:
    def __init__(self):
        self.service = None

    def _authenticate(self):
        if self.service: return self.service
        
        creds = None
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'rb') as token:
                creds = pickle.load(token)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("   [Drive] Обновление токена доступа...")
                creds.refresh(Request())
            else:
                if not os.path.exists(CLIENT_SECRETS_FILE):
                    print("   [!] ОШИБКА: Файл client_secrets.json не найден!")
                    return None
                
                print("\n   [!] ВНИМАНИЕ: Сейчас откроется браузер для авторизации Google.")
                print("   [!] Пожалуйста, подтвердите доступ вашего аккаунта к Google Drive.\n")
                flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)
        
        self.service = build('drive', 'v3', credentials=creds)
        return self.service

    def _clean_id(self, folder_id):
        if not folder_id: return None
        if "folders/" in folder_id:
            folder_id = folder_id.split("folders/")[1]
        folder_id = re.split(r'[\?\/]', folder_id)[0]
        return folder_id.strip()

    def create_folder(self, folder_name):
        service = self._authenticate()
        if not service: return None
        
        parent_id = self._clean_id(os.getenv("GOOGLE_DRIVE_FOLDER_ID"))
        file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
        if parent_id: file_metadata['parents'] = [parent_id]
        
        try:
            folder = service.files().create(body=file_metadata, fields='id').execute()
            fid = folder.get('id')
            service.permissions().create(fileId=fid, body={'type': 'anyone', 'role': 'reader'}).execute()
            return fid
        except Exception as e:
            print(f"   [!] Ошибка Диска: {e}")
            return None

    def upload_file(self, file_path, folder_id):
        service = self._authenticate()
        if not service or not folder_id: return "https://drive.google.com/error"
        
        file_metadata = {'name': os.path.basename(file_path), 'parents': [folder_id]}
        media = MediaFileUpload(file_path, mimetype='text/plain')
        file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        return file.get('webViewLink')

gdrive = GoogleDriveManager()
