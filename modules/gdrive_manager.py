import os
import re
import pickle
from dotenv import load_dotenv

load_dotenv()

# Библиотеки Google подключаем мягко: если их нет, бот должен сказать
# «установи зависимости», а не падать с многоэтажной ошибкой.
try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GOOGLE_LIBS_OK = True
    GOOGLE_LIBS_ERROR = ""
except ImportError as e:
    InstalledAppFlow = Request = service_account = build = MediaFileUpload = None
    GOOGLE_LIBS_OK = False
    GOOGLE_LIBS_ERROR = str(e)

CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "token.json"
SCOPES = ['https://www.googleapis.com/auth/drive']


def detect_auth_mode(path=None):
    """
    Определяет, какой ключ Google лежит в файле:
      "service_account" — сервисный аккаунт (браузер не нужен);
      "oauth"           — OAuth-клиент типа «Desktop app» (откроется браузер);
      "missing"         — файла нет;
      "unknown"         — файл есть, но это не то.
    Нужно потому, что раньше бот умел только второй вариант и падал
    с ValueError, если пользователь делал всё по инструкции с сервисным аккаунтом.
    """
    import json

    path = path or CLIENT_SECRETS_FILE
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "unknown"

    if data.get("type") == "service_account" and data.get("client_email"):
        return "service_account"
    if "installed" in data or "web" in data:
        return "oauth"
    return "unknown"


class GoogleDriveManager:
    def __init__(self):
        self.service = None
        self.auth_mode = None

    def _authenticate(self):
        if self.service: return self.service

        if not GOOGLE_LIBS_OK:
            print("   [!] Библиотеки Google не установлены. Выполни: pip install -r requirements.txt")
            print(f"   [!] Подробности: {GOOGLE_LIBS_ERROR}")
            return None

        mode = detect_auth_mode()

        # ─── Вариант 1: сервисный аккаунт (браузер не нужен) ───────────
        if mode == "service_account":
            try:
                creds = service_account.Credentials.from_service_account_file(
                    CLIENT_SECRETS_FILE, scopes=SCOPES
                )
                self.service = build('drive', 'v3', credentials=creds)
                self.auth_mode = "service_account"
                print("   [Drive] ✅ Авторизация сервисным аккаунтом (без браузера).")
                return self.service
            except Exception as e:
                print(f"   [!] Ошибка авторизации сервисным аккаунтом: {e}")
                return None

        if mode == "unknown":
            print(f"   [!] Файл {CLIENT_SECRETS_FILE} не похож ни на сервисный аккаунт, ни на OAuth-ключ.")
            print("   [!]   Нужен JSON из Google Cloud: либо ключ Service Account,")
            print("   [!]   либо OAuth-клиент типа «Desktop app» (тогда ключ лежит внутри поля 'installed').")
            return None

        # ─── Вариант 2: OAuth-клиент «Desktop app» (браузер) ───────────
        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, 'rb') as token:
                    creds = pickle.load(token)
            except Exception:
                creds = None  # повреждённый token.json не должен ломать запуск

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("   [Drive] Обновление токена доступа...")
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"   [Drive] ⚠️ Токен не обновился ({e}). Авторизуемся заново...")
                    creds = None

            if not creds or not creds.valid:
                if mode == "missing":
                    print(f"   [!] ОШИБКА: не найден {CLIENT_SECRETS_FILE}.")
                    print("   [!]   Скачай JSON-ключ в Google Cloud (см. README, шаг «Настрой Google Drive»)")
                    print(f"   [!]   и положи его рядом с main.py под именем {CLIENT_SECRETS_FILE}")
                    return None

                print("\n   [!] ВНИМАНИЕ: Сейчас откроется браузер для авторизации Google.")
                print("   [!] Пожалуйста, подтвердите доступ вашего аккаунта к Google Drive.\n")
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    print(f"   [!] Авторизация в браузере не удалась: {e}")
                    return None

            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)

        self.service = build('drive', 'v3', credentials=creds)
        self.auth_mode = "oauth"
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

    def self_test(self):
        """
        Проверка для режима --selftest: доступен ли Диск и можно ли писать в папку.
        Создаёт временную папку и сразу её удаляет, ничего не оставляя после себя.
        Возвращает (успех: bool, сообщение: str).
        """
        if not os.getenv("GOOGLE_DRIVE_FOLDER_ID"):
            return False, "GOOGLE_DRIVE_FOLDER_ID не задан в .env"

        mode = detect_auth_mode()
        if mode == "missing":
            return False, f"нет файла {CLIENT_SECRETS_FILE} (ключ Google Cloud)"
        if mode == "unknown":
            return False, f"{CLIENT_SECRETS_FILE} не похож на ключ Google"

        service = self._authenticate()
        if not service:
            return False, "не удалось авторизоваться (проверь ключ Google и доступ к папке)"

        parent_id = self._clean_id(os.getenv("GOOGLE_DRIVE_FOLDER_ID"))
        temp_id = None
        try:
            folder = service.files().create(
                body={'name': '_selftest_tmp', 'mimeType': 'application/vnd.google-apps.folder',
                      'parents': [parent_id]},
                fields='id'
            ).execute()
            temp_id = folder.get('id')
            service.permissions().create(
                fileId=temp_id, body={'type': 'anyone', 'role': 'reader'}
            ).execute()
            how = "сервисным аккаунтом" if mode == "service_account" else "аккаунтом Google"
            return True, f"Диск доступен ({how}), папка создаётся и выдаются права на чтение"
        except Exception as e:
            message = str(e)
            if "notFound" in message or "File not found" in message:
                message = "папка из GOOGLE_DRIVE_FOLDER_ID не найдена"
                if mode == "service_account":
                    message += (" — открой эту папку в Google Drive и поделись ею с сервисным аккаунтом "
                                f"(email внутри {CLIENT_SECRETS_FILE}, поле client_email)")
                else:
                    message += " — проверь, что GOOGLE_DRIVE_FOLDER_ID скопирован из адреса папки"
            return False, message
        finally:
            if temp_id:
                try:
                    service.files().delete(fileId=temp_id).execute()
                except Exception:
                    pass

gdrive = GoogleDriveManager()
