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

def upload_to_drive(service, local_path, parent_drive_id):
    uploaded_links = []
    items_to_share = [] 
    
    if not os.path.exists(local_path):
        return [], []

    folder_mapping = {'.': parent_drive_id}
    
    for root, dirs, files in os.walk(local_path):
        rel_path = os.path.relpath(root, local_path)
        current_parent = folder_mapping.get(rel_path, parent_drive_id)
        
        for d in dirs:
            dir_path = os.path.normpath(os.path.join(rel_path, d))
            folder_id, folder_link = create_drive_folder(service, d, current_parent)
            folder_mapping[dir_path] = folder_id
            if folder_link not in uploaded_links: 
                uploaded_links.append(folder_link)
            if rel_path == '.':
                items_to_share.append(folder_id)
                
        for f in files:
            file_path = os.path.join(root, f)
            
            # 🔥 תיקון 2: חוסם העלאת קבצי תיאור ותמונות נפרדות לגוגל דרייב 🔥
            if file_path.endswith('.description'): continue
            if any(f.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']): continue
            
            media = MediaFileUpload(file_path, resumable=True)
            file = service.files().create(body={'name': f, 'parents': [current_parent]}, media_body=media).execute()
            uploaded_links.append(file['webViewLink'])
            if rel_path == '.':
                items_to_share.append(file['id'])
            
    return uploaded_links, items_to_share

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
    
    urls = [link.rstrip(')]}.') for link in links]
    is_video = "וידאו" in subject or "וידאו" in body
    is_audio = not is_video
    
    # 🔥 תיקון 1: מבנה התיקיות נקי ללא תיקיית Singles באמצע 🔥
    out_tmpl = 'downloads/%(playlist_title,uploader,extractor_key|Unknown)s/%(title)s.%(ext)s'
    
    ydl_opts = {
        'outtmpl': out_tmpl,
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

    os.makedirs('downloads', exist_ok=True)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(urls)

        if is_audio:
            for root, dirs, files in os.walk('downloads'):
                for f in files:
                    if f.endswith('.mp3'):
                        base_name = os.path.splitext(f)[0]
                        desc_file = os.path.join(root, base_name + '.description')
                        embed_lyrics_in_mp3(os.path.join(root, f), desc_file)

        links_res, ids_to_share = upload_to_drive(drive_svc, 'downloads', BASE_FOLDER_ID)
        
        for item_id in ids_to_share:
            try: drive_svc.permissions().create(fileId=item_id, body={'type': 'user', 'role': 'reader', 'emailAddress': sender_email}).execute()
            except: pass

        if links_res:
            reply_body = f"היי!\n\nההורדה הסתיימה בהצלחה. הקבצים מוכנים עבורך כאן:\n{links_res[0]}\n\nהאזנה/צפייה נעימה!"
            send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_body, msg['threadId'])
            
    except Exception as e:
        error_details = traceback.format_exc()
        error_msg = f"היי,\n\nהבוט נתקל בבעיה טכנית בזמן שניסה לעבד את הבקשה שלך.\nהנה פרטי השגיאה (הלוג):\n\n{error_details}"
        send_email_reply(gmail_svc, sender_email, f"שגיאה בעיבוד: {subject}", error_msg, msg['threadId'])
    
    finally:
        shutil.rmtree('downloads', ignore_errors=True)
        
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
