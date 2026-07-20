import os
import re
import base64
import requests
import traceback
import yt_dlp
from datetime import datetime
from email.mime.text import MIMEText
from mutagen.id3 import ID3, USLT
from mutagen.mp3 import MP3
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import shutil
from bs4 import BeautifulSoup

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

def create_drive_folder(service, folder_name, parent_id, always_create=False):
    if not always_create:
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
    
    # תבנית מעודכנת שתופסת כל קישור אינטרנט סטנדרטי
    links = re.findall(r'(https?://[^\s"\'<>]+)', text_to_search)
    
    gmail_svc.users().messages().batchModify(userId='me', body={'ids': [msg_id], 'removeLabelIds': ['UNREAD']}).execute()
    
    if not links:
        return False
    
    urls = []
    for link in links:
        clean_link = link.rstrip(')]}.')
        if clean_link not in urls:
            urls.append(clean_link)
            
    is_text = "טקסט" in subject
    is_video = "וידאו" in subject
    is_audio = not is_video and not is_text
    
    try:
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

        ydl_opts_info = {
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
            'geo_bypass_country': 'IL',
        }
        
        has_downloaded_anything = False

        for url in urls:
            shutil.rmtree('downloads_temp', ignore_errors=True)
            os.makedirs('downloads_temp', exist_ok=True)
            
            target_folder_id = email_folder_id
            
            # 🔥 מסלול Web Scraping אם הנושא הוא טקסט 🔥
            if is_text:
                try:
                    headers_req = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                    res = requests.get(url, headers=headers_req, timeout=15)
                    res.raise_for_status()
                    
                    soup = BeautifulSoup(res.text, 'html.parser')
                    
                    # מסיר קוד מיותר
                    for script in soup(["script", "style", "nav", "footer"]):
                        script.extract()
                        
                    text_content = soup.get_text(separator='\n', strip=True)
                    
                    page_title = soup.title.string if soup.title else "Scraped_Text"
                    safe_title = "".join([c for c in page_title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                    if not safe_title:
                        safe_title = "Text_Document"
                        
                    file_path = os.path.join('downloads_temp', f"{safe_title[:50]}.txt")
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(f"מקור: {url}\n\n{text_content}")
                        
                except Exception as e:
                    print(f"שגיאה בחילוץ הטקסט מהאתר: {url}. פרטים: {e}")
                    continue
            
            # 🔥 מסלול מדיה רגיל (אודיו/וידאו) 🔥
            else:
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

                if len(entries) > 1 and source_title:
                    safe_playlist_title = "".join([c for c in source_title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                    if safe_playlist_title:
                        target_folder_id, _ = create_drive_folder(drive_svc, safe_playlist_title, email_folder_id, always_create=False)
                
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

            # העלאה משותפת (גם למסמכי טקסט וגם למדיה)
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
                            fields='id, webViewLink'
                        ).execute()
                        has_downloaded_anything = True
                    except:
                        pass

            shutil.rmtree('downloads_temp', ignore_errors=True)

        if has_downloaded_anything:
            reply_body = f"היי!\n\nהפעולה הסתיימה בהצלחה. כל הקבצים מאורגנים ומחכים לך בתיקיית המייל המיוחדת שלך כאן:\n{email_folder_link}\n\nתהנה!"
            send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_body, msg['threadId'])
            
    except Exception as e:
        error_details = traceback.format_exc()
        error_msg = f"היי,\n\nהבוט נתקל בבעיה טכנית בזמן שניסה לעבד את הבקשה שלך.\nהנה פרטי השגיאה (הלוג):\n\n{error_details}"
        send_email_reply(gmail_svc, sender_email, f"שגיאה בעיבוד: {subject}", error_msg, msg['threadId'])
        
    return True

def main():
    drive_svc, gmail_svc = get_services()
    # הוספת התנאי השלישי - טקסט
    query = 'is:unread (subject:יוטיוב OR subject:וידאו OR subject:טקסט)'
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
