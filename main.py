import os
import io
import re
import base64
import requests
import traceback
import yt_dlp
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from mutagen.id3 import ID3, USLT, APIC
from mutagen.mp3 import MP3
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import shutil

# ספריות ל-Selenium (דפדפן וירטואלי)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ספריות לטלגרם
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename, InputMessagesFilterVideo
from telethon.tl.functions.contacts import SearchRequest

# ===== הגדרות =====
BASE_FOLDER_ID = "12o0xHyXAuj5f3v3nHszVdCKZj8Lxjx-4"
# ==================

# נתוני גוגל
CLIENT_ID = os.environ.get('GDRIVE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('GDRIVE_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('GDRIVE_REFRESH_TOKEN')

# נתוני טלגרם
TG_API_ID = os.environ.get('TELEGRAM_API_ID')
TG_API_HASH = os.environ.get('TELEGRAM_API_HASH')
TG_SESSION = os.environ.get('TELEGRAM_SESSION_STRING')

def get_services():
    creds = Credentials(token=None, refresh_token=REFRESH_TOKEN, token_uri="https://oauth2.googleapis.com/token",
                        client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    return build('drive', 'v3', credentials=creds), build('gmail', 'v1', credentials=creds)

def cleanup_old_drive_files(drive_svc):
    """מחיקת קבצים ותיקיות בתיקיית הבסיס שעברו 24 שעות ממועד יצירתם"""
    try:
        cutoff_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        query = f"'{BASE_FOLDER_ID}' in parents and createdTime < '{cutoff_time}' and trashed = false"
        results = drive_svc.files().list(q=query, fields="files(id, name)").execute()
        for f in results.get('files', []):
            try:
                drive_svc.files().delete(fileId=f['id']).execute()
                print(f"נמחק אוטומטית (עברו 24 שעות): {f['name']}")
            except Exception as del_err:
                print(f"שגיאה במחיקת {f['name']}: {del_err}")
    except Exception as e:
        print(f"שגיאה בסריקת קבצים ישנים למחיקה: {e}")

def send_email_reply(gmail_svc, to_email, subject, body, thread_id):
    message = MIMEText(body)
    message['to'] = to_email
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_svc.users().messages().send(userId='me', body={'raw': raw, 'threadId': thread_id}).execute()

def create_drive_folder(service, folder_name, parent_id, always_create=False):
    if not always_create:
        safe_query_name = folder_name.replace("'", "\\'").replace('"', '\\"')
        query = f"name = '{safe_query_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, fields='files(id, webViewLink)').execute()
        if res.get('files'): return res['files'][0]['id'], res['files'][0]['webViewLink']
    
    metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    file = service.files().create(body=metadata, fields='id, webViewLink').execute()
    return file['id'], file['webViewLink']

def process_mp3_metadata(audio_file, description_file, thumbnail_file):
    """הטמעת מילות השיר ותמונת עטיפה מוקטנת (500x500) לתוך קובץ ה-MP3"""
    try:
        audio = MP3(audio_file, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        # 1. הטמעת מילות השיר
        if description_file and os.path.exists(description_file):
            with open(description_file, 'r', encoding='utf-8') as df:
                lyrics = df.read().strip()
            if lyrics:
                audio.tags.add(USLT(encoding=3, lang='heb', desc='Lyrics', text=lyrics))

        # 2. מזעור והטמעת תמונת עטיפה
        if thumbnail_file and os.path.exists(thumbnail_file):
            with Image.open(thumbnail_file) as img:
                img = img.convert('RGB')
                img.thumbnail((500, 500))
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=85)
                img_data = img_byte_arr.getvalue()

            audio.tags.add(
                APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc='Cover',
                    data=img_data
                )
            )

        audio.save()
    except Exception as e:
        print(f"שגיאה בעריכת נתוני ה-MP3: {e}")

def extract_body_from_payload(payload):
    body = ""
    if 'parts' in payload:
        for part in payload['parts']:
            body += extract_body_from_payload(part)
    elif 'body' in payload and 'data' in payload['body']:
        body += base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    return body

def process_email(drive_svc, gmail_svc, msg_id):
    msg = gmail_svc.users().messages().get(userId='me', id=msg_id).execute()
    headers = msg['payload']['headers']
    sender = next(h['value'] for h in headers if h['name'] == 'From')
    sender_email = re.search(r'[\w\.-]+@[\w\.-]+', sender).group()
    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "")
    
    body = extract_body_from_payload(msg['payload'])
    text_to_search = f"{subject} {body}"
    
    links = re.findall(r'(https?://[^\s"\'<>]+)', text_to_search)
    
    gmail_svc.users().messages().batchModify(userId='me', body={'ids': [msg_id], 'removeLabelIds': ['UNREAD']}).execute()
    
    is_search = "חיפוש" in subject
    is_browse = "דפדף" in subject
    
    if not links and not is_search and not is_browse:
        return False
    
    urls = []
    for link in links:
        clean_link = link.rstrip(')]}.')
        if clean_link not in urls:
            urls.append(clean_link)
            
    is_text = "טקסט" in subject
    is_video = "וידאו" in subject or "וידיאו" in subject
    is_audio = not is_video and not is_text and not is_search and not is_browse
    
    try:
        email_folder_id = None
        email_folder_link = ""
        has_downloaded_anything = False

        def get_email_folder():
            nonlocal email_folder_id, email_folder_link
            if not email_folder_id:
                current_time = datetime.now().strftime("%d.%m.%Y %H:%M")
                email_folder_name = f"הורדה - {subject} [{current_time}]"
                email_folder_id, email_folder_link = create_drive_folder(drive_svc, email_folder_name, BASE_FOLDER_ID, always_create=True)
                try:
                    drive_svc.permissions().create(
                        fileId=email_folder_id, 
                        body={'type': 'user', 'role': 'reader', 'emailAddress': sender_email}
                    ).execute()
                except:
                    pass
            return email_folder_id

        ydl_opts_info = {
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
            'geo_bypass_country': 'IL',
        }

        # 🔥 מסלול דפדוף בהודעות אחרונות בערוץ/קבוצה בטלגרם 🔥
        if is_browse:
            if not (TG_API_ID and TG_API_HASH and TG_SESSION):
                print("הגדרות טלגרם חסרות בשרת.")
                return False

            entity_name = body.strip()
            if not entity_name:
                send_email_reply(gmail_svc, sender_email, f"Re: {subject}", "לא צוין שם קבוצה או ערוץ בגוף המייל.", msg['threadId'])
                return True

            try:
                with TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH) as client:
                    messages = client.get_messages(entity_name, limit=2)
                    reply_text = f"2 ההודעות האחרונות מתוך '{entity_name}':\n\n"
                    for m in messages:
                        date_str = m.date.strftime("%d-%m-%Y %H:%M") if m.date else ""
                        content = m.text if m.text else "[קובץ/מדיה ללא טקסט]"
                        reply_text += f"📅 {date_str}\n💬 {content}\n" + ("-" * 30) + "\n"
                    send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_text, msg['threadId'])
            except Exception as e:
                send_email_reply(gmail_svc, sender_email, f"Re: {subject}", f"שגיאה בגישה לקבוצה '{entity_name}': {e}", msg['threadId'])
            return True

        # 🔥 מסלול חיפוש נרחב בטלגרם 🔥
        if is_search:
            try:
                if not (TG_API_ID and TG_API_HASH and TG_SESSION):
                    print("הגדרות טלגרם חסרות בשרת.")
                    return False
                
                search_query = body.strip()
                if not search_query:
                    send_email_reply(gmail_svc, sender_email, f"Re: {subject}", "לא צוין טקסט לחיפוש בגוף המייל.", msg['threadId'])
                    return True

                with TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH) as client:
                    for dialog in client.iter_dialogs():
                        entity = dialog.entity
                        try:
                            messages = client.iter_messages(
                                entity,
                                search=search_query,
                                filter=InputMessagesFilterVideo,
                                limit=2
                            )
                            for message in messages:
                                if message and message.media:
                                    shutil.rmtree('downloads_temp', ignore_errors=True)
                                    os.makedirs('downloads_temp', exist_ok=True)
                                    client.download_media(message, 'downloads_temp')
                                    
                                    folder_to_use = get_email_folder()
                                    for root, dirs, files in os.walk('downloads_temp'):
                                        for f in files:
                                            file_path = os.path.join(root, f)
                                            media = MediaFileUpload(file_path, resumable=True)
                                            drive_svc.files().create(
                                                body={'name': f, 'parents': [folder_to_use]}, 
                                                media_body=media, 
                                                fields='id, webViewLink'
                                            ).execute()
                                            has_downloaded_anything = True
                                    shutil.rmtree('downloads_temp', ignore_errors=True)
                        except Exception:
                            continue

                    try:
                        global_result = client(SearchRequest(q=search_query, limit=10))
                        for chat in global_result.chats:
                            try:
                                messages = client.iter_messages(
                                    chat,
                                    search=search_query,
                                    filter=InputMessagesFilterVideo,
                                    limit=2
                                )
                                for message in messages:
                                    if message and message.media:
                                        shutil.rmtree('downloads_temp', ignore_errors=True)
                                        os.makedirs('downloads_temp', exist_ok=True)
                                        client.download_media(message, 'downloads_temp')
                                        
                                        folder_to_use = get_email_folder()
                                        for root, dirs, files in os.walk('downloads_temp'):
                                            for f in files:
                                                file_path = os.path.join(root, f)
                                                media = MediaFileUpload(file_path, resumable=True)
                                                drive_svc.files().create(
                                                    body={'name': f, 'parents': [folder_to_use]}, 
                                                    media_body=media, 
                                                    fields='id, webViewLink'
                                                ).execute()
                                                has_downloaded_anything = True
                                        shutil.rmtree('downloads_temp', ignore_errors=True)
                            except Exception:
                                continue
                    except Exception as ge:
                        print(f"שגיאה בחיפוש הגלובלי: {ge}")

            except Exception as e:
                print(f"שגיאה בחיפוש בטלגרם: {e}")

        for url in urls:
            shutil.rmtree('downloads_temp', ignore_errors=True)
            os.makedirs('downloads_temp', exist_ok=True)
            
            target_folder_id = None
            
            # 🔥 מסלול טלגרם רגיל לפי קישור 🔥
            if 't.me/' in url:
                try:
                    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
                        print("הגדרות טלגרם חסרות בשרת. מדלג.")
                        continue
                        
                    clean_url = url.split('?')[0].rstrip('/')
                    parts = clean_url.split('/')
                    
                    msg_id_tg = int(parts[-1])
                    
                    if 'c' in parts:
                        entity = int('-100' + parts[-2])
                    else:
                        entity = parts[-2]
                    
                    with TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH) as client:
                        message = client.get_messages(entity, ids=msg_id_tg)
                        if message and message.media:
                            client.download_media(message, 'downloads_temp')
                        else:
                            print(f"לא נמצאה מדיה בקישור: {url}")
                            
                except ValueError as ve:
                    print(f"שגיאת משתמש/ערוץ בטלגרם: {ve}")
                    continue
                except Exception as e:
                    print(f"שגיאה בהורדה מטלגרם: {e}")
                    continue

            # 🔥 מסלול טקסט (HTML) 🔥
            elif is_text:
                driver = None
                try:
                    options = Options()
                    options.add_argument("--headless")
                    options.add_argument("--disable-gpu")
                    options.add_argument("--no-sandbox")
                    options.add_argument("--disable-dev-shm-usage")
                    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                    
                    service = Service(ChromeDriverManager().install())
                    driver = webdriver.Chrome(service=service, options=options)
                    
                    driver.get(url)
                    driver.implicitly_wait(6)
                    
                    page_source = driver.page_source
                    safe_name = re.sub(r'[^a-zA-Z0-9א-ת]', '_', url)[:40]
                    file_path = os.path.join('downloads_temp', f"page_{safe_name}.html")
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(page_source)
                        
                except Exception as e:
                    print(f"שגיאה ב-Selenium עבור {url}: {e}")
                    continue
                finally:
                    if driver:
                        driver.quit()
            
            # 🔥 מסלול מדיה רגיל (אודיו/וידאו - YouTube וכו') 🔥
            elif not is_search and not is_browse:
                if 'drive.google.com' in url:
                    print(f"מדלג על קישור דרייב: {url}")
                    continue

                source_title = ""
                entries = []
                try:
                    with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if info:
                            if 'entries' in info:
                                entries = [e for e in info['entries'] if e]
                                source_title = info.get('title', '')
                            else:
                                entries = [info]
                except:
                    continue

                if not entries:
                    continue

                main_folder_id = get_email_folder()
                target_folder_id = main_folder_id

                if len(entries) > 1 and source_title:
                    safe_playlist_title = "".join([c for c in source_title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                    if safe_playlist_title:
                        target_folder_id, _ = create_drive_folder(drive_svc, safe_playlist_title, main_folder_id, always_create=False)
                
                ydl_opts = {
                    'outtmpl': 'downloads_temp/%(title)s.%(ext)s',
                    'writedescription': True,
                    'ignoreerrors': True,
                    'geo_bypass_country': 'IL',
                }

                if is_audio:
                    ydl_opts.update({
                        'format': 'bestaudio/best',
                        'writethumbnail': True,
                        'postprocessors': [
                            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                            {'key': 'FFmpegMetadata', 'add_metadata': True},
                        ],
                    })
                else:
                    ydl_opts.update({
                        'format': 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best',
                        'merge_output_format': 'mp4'
                    })

                with yt_dlp.YoutubeDL(ydl_opts) as ydl_dl:
                    ydl_dl.download([url])

                if is_audio:
                    for root, dirs, files in os.walk('downloads_temp'):
                        for f in files:
                            if f.endswith('.mp3'):
                                base_name = os.path.splitext(f)[0]
                                mp3_path = os.path.join(root, f)
                                desc_file = os.path.join(root, base_name + '.description')
                                
                                thumbnail_file = None
                                for ext in ['.jpg', '.webp', '.png', '.jpeg']:
                                    temp_thumb = os.path.join(root, base_name + ext)
                                    if os.path.exists(temp_thumb):
                                        thumbnail_file = temp_thumb
                                        break
                                
                                process_mp3_metadata(mp3_path, desc_file, thumbnail_file)

            # העלאה משותפת לדרייב לכל סוגי הקבצים (ללא קובצי העזר של התמונות והתיאור)
            for root, dirs, files in os.walk('downloads_temp'):
                for f in files:
                    file_path = os.path.join(root, f)
                    if file_path.endswith('.description'): continue
                    if any(f.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']): continue
                    
                    try:
                        folder_to_use = target_folder_id or get_email_folder()
                        media = MediaFileUpload(file_path, resumable=True)
                        drive_svc.files().create(
                            body={'name': f, 'parents': [folder_to_use]}, 
                            media_body=media, 
                            fields='id, webViewLink'
                        ).execute()
                        has_downloaded_anything = True
                    except Exception as e:
                        print(f"שגיאה בהעלאה לדרייב: {e}")

            shutil.rmtree('downloads_temp', ignore_errors=True)

        if has_downloaded_anything:
            reply_body = f"היי!\n\nהפעולה הסתיימה בהצלחה. הקבצים מחכים לך בתיקיית הדרייב (יימחקו אוטומטית בעוד 24 שעות):\n{email_folder_link}\n\nתהנה!"
            send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_body, msg['threadId'])
        elif is_search:
            reply_body = "היי,\n\nהחיפוש בטלגרם הסתיים, אך לא נמצאו קובצי וידאו התואמים את מילת החיפוש."
            send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_body, msg['threadId'])
            
    except Exception as e:
        error_details = traceback.format_exc()
        error_msg = f"היי,\n\nהבוט נתקל בבעיה טכנית:\n\n{error_details}"
        send_email_reply(gmail_svc, sender_email, f"שגיאה בעיבוד: {subject}", error_msg, msg['threadId'])
        
    return True

def main():
    drive_svc, gmail_svc = get_services()
    
    # ניקוי אוטומטי של קבצים ותיקיות שעברו 24 שעות
    cleanup_old_drive_files(drive_svc)
    
    query = 'is:unread (subject:יוטיוב OR subject:וידאו OR subject:טקסט OR subject:וידיאו OR subject:חיפוש OR subject:דפדף)'
    results = gmail_svc.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        return

    for msg in messages:
        try:
            process_email(drive_svc, gmail_svc, msg['id'])
        except Exception as e:
            print(f"שגיאה כללית: {e}")

if __name__ == "__main__":
    main()
