import json
import os
import re
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2 import service_account
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
        if self.service:
            return self.service

        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, 'rb') as token:
                    creds = pickle.load(token)
            except (OSError, pickle.PickleError, EOFError) as e:
                print(f"   [Drive] ⚠️ Не удалось прочитать token.json: {e}")
                creds = None

        if creds and creds.valid:
            pass
        elif creds and creds.expired and getattr(creds, "refresh_token", None):
            try:
                print("   [Drive] Обновление токена доступа...")
                creds.refresh(Request())
            except Exception as e:
                print(f"   [Drive] ⚠️ Токен устарел: {e}")
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                print("   [!] ОШИБКА: Файл client_secrets.json не найден!")
                return None

            try:
                # Поддерживаем оба варианта файла: OAuth Desktop App и
                # Service Account. Это устраняет противоречие в инструкции.
                with open(CLIENT_SECRETS_FILE, "r", encoding="utf-8") as source:
                    client_data = json.load(source)
                if not isinstance(client_data, dict):
                    raise ValueError("ожидался JSON-объект с настройками клиента")

                if client_data.get("type") == "service_account":
                    print("   [Drive] Используем Service Account.")
                    creds = service_account.Credentials.from_service_account_info(
                        client_data,
                        scopes=SCOPES,
                    )
                else:
                    print("\n   [!] Сейчас откроется браузер для авторизации Google.")
                    print("   [!] Подтвердите доступ к Google Drive.\n")
                    flow = InstalledAppFlow.from_client_config(client_data, SCOPES)
                    creds = flow.run_local_server(port=0)
            except (OSError, ValueError, json.JSONDecodeError) as e:
                print(f"   [!] ОШИБКА: client_secrets.json имеет неверный формат: {e}")
                return None

            # Service Account не требует и не поддерживает OAuth token.json.
            if client_data.get("type") != "service_account":
                try:
                    with open(TOKEN_FILE, 'wb') as token:
                        pickle.dump(creds, token)
                except OSError as e:
                    print(f"   [Drive] ⚠️ Не удалось сохранить token.json: {e}")

        try:
            self.service = build('drive', 'v3', credentials=creds)
            return self.service
        except Exception as e:
            print(f"   [!] Ошибка подключения к Google Drive: {e}")
            return None

    def _clean_id(self, folder_id):
        if not folder_id: return None
        if "folders/" in folder_id:
            folder_id = folder_id.split("folders/")[1]
        folder_id = re.split(r'[\?\/]', folder_id)[0]
        return folder_id.strip()

    def create_folder(self, folder_name):
        service = self._authenticate()
        if not service:
            return None

        parent_id = self._clean_id(os.getenv("GOOGLE_DRIVE_FOLDER_ID"))
        if not parent_id:
            print("   [!] ОШИБКА: GOOGLE_DRIVE_FOLDER_ID не указан в .env")
            return None

        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id],
        }

        try:
            folder = service.files().create(body=file_metadata, fields='id').execute()
            fid = folder.get('id')
            if not fid:
                print("   [!] Google Drive не вернул ID папки.")
                return None
            service.permissions().create(
                fileId=fid,
                body={'type': 'anyone', 'role': 'reader'},
            ).execute()
            return fid
        except Exception as e:
            print(f"   [!] Ошибка Google Drive при создании папки: {e}")
            return None

    def upload_file(self, file_path, folder_id):
        service = self._authenticate()
        if not service or not folder_id:
            return None

        try:
            file_metadata = {
                'name': os.path.basename(file_path),
                'parents': [folder_id],
            }
            media = MediaFileUpload(file_path, mimetype='text/plain')
            file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink, webContentLink',
            ).execute()
            file_id = file.get('id')
            return (
                file.get('webViewLink')
                or file.get('webContentLink')
                or (f"https://drive.google.com/open?id={file_id}" if file_id else None)
            )
        except Exception as e:
            print(f"   [!] Ошибка Google Drive при загрузке файла: {e}")
            return None

gdrive = GoogleDriveManager()
