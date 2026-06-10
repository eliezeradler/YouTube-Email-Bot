import os
import re
import base64
import requests
import traceback
import yt_dlp
from email.mime.text import MIMEText
from mutagen.id3 import ID3, USLT
from mutagen.mp3 import MP3
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import shutil

# ===== הגדרות =====
BASE_FOLDER_ID = "12o0xHyXAuj5f3v3nHszVdCKZj8Lxjx-4"
# ==================

CLIENT_ID = os.environ['GDRIVE_CLIENT_ID']
CLIENT_SECRET = os.environ['GDRIVE_CLIENT_SECRET']
REFRESH_TOKEN = os.environ['GDRIVE_REFRESH_TOKEN']

def get_services():
    creds = Credentials(token=None, refresh_token=REFRESH_TOKEN, token_uri="https://oauth2.googleapis.com/token",
                        client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    return build('drive', 'v3', credentials=creds), build('gmail', 'v1', credentials=creds)

def send_email_reply(gmail_svc, to_email, subject, body, thread_id):
    message = MIMEText(body)
    message['to'] = to_email
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_svc.users().messages().send(userId='me', body={'raw': raw, 'threadId': thread_id}).execute()

def create_drive_folder(service, folder_name, parent_id):
    safe_query_name = folder_name.replace("'", "\\'").replace('"', '\\"')
    query = f"name = '{safe_query_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=query, fields='files(id, webViewLink)').execute()
    if res.get('files'): return res['files'][0]['id'], res['files'][0]['webViewLink']
    
    metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    file = service.files().create(body=metadata, fields='id, webViewLink').execute()
    return file['id'], file['webViewLink']

def embed_lyrics_in_mp3(audio_file, description_file):
    if not os.path.exists(description_file): return
    with open(description_file, 'r', encoding='utf-8') as df:
        lyrics = df.read()
    if not lyrics.strip(): return
    
    try:
        audio = MP3(audio_file, ID3=ID3)
        if audio.tags is None: audio.add_tags()
        audio.tags.add(USLT(encoding=3, lang='heb', desc='Lyrics', text=lyrics))
        audio.save()
    except Exception as e:
        print(f"שגיאה בהטמעת מילים: {e}")

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
    links = re.findall(r'(https?://(?:www\.|music\.)?youtube\.com/[^\s"\'<>]+|https?://youtu\.be/[^\s"\'<>]+)', text_to_search)
    
    gmail_svc.users().messages().batchModify(userId='me', body={'ids': [msg_id], 'removeLabelIds': ['UNREAD']}).execute()
    
    if not links:
        return False
    
    urls = []
    for link in links:
        clean_link = link.rstrip(')]}.')
        if clean_link not in urls:
            urls.append(clean_link)
            
    is_video = "וידאו" in subject or "וידאו" in body
    is_audio = not is_video
    
    try:
        # 1. 🔥 חוק 1: יצירת תיקייה ייעודית למייל הנוכחי בדרייב ושיתופה עם השולח 🔥
        email_folder_name = f"הורדה - {subject}"
        email_folder_id, email_folder_link = create_drive_folder(drive_svc, email_folder_name, BASE_FOLDER_ID)
        
        try:
            drive_svc.permissions().create(
                fileId=email_folder_id, 
                body={'type': 'user', 'role': 'reader', 'emailAddress': sender_email}
            ).execute()
        except:
            pass

        # הגדרות בסיסיות לסריקת הקישורים
        ydl_opts_info = {
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
            'geo_bypass_country': 'IL',
        }
        
        has_downloaded_anything = False

        # 2. 🔥 חוק 2: מעבר על הקישורים בנפרד ליישום לוגיקת אלבומים/שירים בודדים 🔥
        for url in urls:
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

            # קביעת תיקיית היעד לקישור הנוכחי בתוך תיקיית המייל
            if len(entries) > 1 and source_title:
                safe_playlist_title = "".join([c for c in source_title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                if safe_playlist_title:
                    target_folder_id, _ = create_drive_folder(drive_svc, safe_playlist_title, email_folder_id)
                else:
                    target_folder_id = email_folder_id
            else:
                # שיר בודד - נזרק ישירות לתיקיית המייל הראשית
                target_folder_id = email_folder_id

            # הורדה זמנית מקומית
            shutil.rmtree('downloads_temp', ignore_errors=True)
            os.makedirs('downloads_temp', exist_ok=True)

            ydl_opts = {
                'outtmpl': 'downloads_temp/%(title)s.%(ext)s',
                'writedescription': True,
                'ignoreerrors': True,
            }

            if is_audio:
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'writethumbnail': True, 
                    'postprocessors': [
                        {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                        {'key': 'FFmpegMetadata', 'add_metadata': True}, 
                        {'key': 'EmbedThumbnail', 'already_have_thumbnail': False}, 
                    ],
                })
            else:
                ydl_opts.update({'format': 'b[ext=mp4]/best'})

            with yt_dlp.YoutubeDL(ydl_opts) as ydl_dl:
                ydl_dl.download([url])

            if is_audio:
                for root, dirs, files in os.walk('downloads_temp'):
                    for f in files:
                        if f.endswith('.mp3'):
                            base_name = os.path.splitext(f)[0]
                            desc_file = os.path.join(root, base_name + '.description')
                            embed_lyrics_in_mp3(os.path.join(root, f), desc_file)

            # העלאת הקבצים לתיקיית היעד שנקבעה (הראשית או תיקיית האלבום)
            for root, dirs, files in os.walk('downloads_temp'):
                for f in files:
                    file_path = os.path.join(root, f)
                    if file_path.endswith('.description'): continue
                    if any(f.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']): continue
                    
                    try:
                        media = MediaFileUpload(file_path, resumable=True)
                        drive_svc.files().create(
                            body={'name': f, 'parents': [target_folder_id]}, 
                            media_body=media, 
                            fields='id'
                        ).execute()
                        has_downloaded_anything = True
                    except:
                        pass

            shutil.rmtree('downloads_temp', ignore_errors=True)

        # שליחת תשובה עם הקישור לתיקיית המייל המרכזית
        if has_downloaded_anything:
            reply_body = f"היי!\n\nההורדה הסתיימה בהצלחה. כל הקבצים מאורגנים ומחכים לך בתיקיית המייל המיוחדת שלך כאן:\n{email_folder_link}\n\nהאזנה/צפייה נעימה!"
            send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_body, msg['threadId'])
            
    except Exception as e:
        error_details = traceback.format_exc()
        error_msg = f"היי,\n\nהבוט נתקל בבעיה טכנית בזמן שניסה לעבד את הבקשה שלך.\nהנה פרטי השגיאה (הלוג):\n\n{error_details}"
        send_email_reply(gmail_svc, sender_email, f"שגיאה בעיבוד: {subject}", error_msg, msg['threadId'])
        
    return True

def main():
    drive_svc, gmail_svc = get_services()
    query = 'is:unread subject:יוטיוב'
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
