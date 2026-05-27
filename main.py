import os
import re
import base64
import requests
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
            if file_path.endswith('.description'): continue
            media = MediaFileUpload(file_path, resumable=True)
            file = service.files().create(body={'name': f, 'parents': [current_parent]}, media_body=media, fields='id, webViewLink').execute()
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
        pass

def extract_body_from_payload(payload):
    body = ""
    if 'parts' in payload:
        for part in payload['parts']:
            body += extract_body_from_payload(part)
    elif 'body' in payload and 'data' in payload['body']:
        body += base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    return body

def download_notebooklm_audio(url, output_folder='downloads/NotebookLM'):
    """פונקציה חכמה שמחלצת ומורידה את קובץ האודיו מתוך דף שיתוף של NotebookLM"""
    try:
        os.makedirs(output_folder, exist_ok=True)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        html_content = response.text
        
        # חיפוש מזהה קובץ ה-Drive של האודיו או כתובת ה-Google Storage המוצפנת בקוד העמוד
        audio_urls = re.findall(r'https://storage\.googleapis\.com/notebooklm-public-prod[^"\s\'<>]+', html_content)
        if not audio_urls:
            # ניסיון חלופי לאיתור כתובות וידאו/אודיו מוטמעות במבנה נתוני ה-JSON של גוגל בדף
            audio_urls = re.findall(r'\"(https://[^\"\s]+?\.mp3.*?)\"', html_content)
            
        if not audio_urls:
            # חיפוש תבנית מזהה קובץ Drive כללי המשמש את NotebookLM לפודקאסטים
            drive_ids = re.findall(r'https://docs\.google\.com/uc\?export=download&id=([a-zA-Z0-9_-]+)', html_content)
            if drive_ids:
                audio_urls = [f"https://docs.google.com/uc?export=download&id={drive_ids[0]}"]

        if not audio_urls:
            print("❌ לא נמצא קובץ שמע ישיר בתוך דף ה-NotebookLM.")
            return False
            
        audio_url = audio_urls[0].replace('\\u0026', '&')
        print(f"🔗 נמצא מקור השמע החבוי: {audio_url}")
        
        # הורדת הקובץ בפועל
        file_res = requests.get(audio_url, stream=True, timeout=60)
        file_res.raise_for_status()
        
        # שליפת שם חכם או יצירת שם ברירת מחדל לפודקאסט
        title_match = re.search(r'<title>(.*?)</title>', html_content)
        title = title_match.group(1).replace(" - NotebookLM", "").strip() if title_match else "NotebookLM_Audio"
        title = re.sub(r'[\\/*?:"<>|]', "", title) # ניקוי תווים אסורים לשמות קבצים
        
        file_path = os.path.join(output_folder, f"{title}.mp3")
        with open(file_path, 'wb') as f:
            for chunk in file_res.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    
        print(f"✅ הפודקאסט הורד בהצלחה: {title}.mp3")
        return True
    except Exception as e:
        print(f"❌ שגיאה בחילוץ שמע מ-NotebookLM: {e}")
        return False

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
    print(f"🔒 המייל סומן כ'נקרא' כדי למנוע כפילויות.")
    
    if not links:
        return False
    
    urls = [link.rstrip(')]}.') for link in links]
    
    youtube_urls = []
    notebook_urls = []
    
    for url in urls:
        if 'youtube.com' in url or 'youtu.be' in url:
            youtube_urls.append(url)
        elif 'notebooklm.google' in url:
            notebook_urls.append(url)

    os.makedirs('downloads', exist_ok=True)
    download_success = False

    # 1. טיפול בקישורי יוטיוב
    if youtube_urls:
        is_video = "וידאו" in subject or "וידאו" in body
        is_audio = not is_video
        out_tmpl = 'downloads/%(playlist_title,uploader,extractor_key|Unknown)s/%(album|Singles)s/%(title)s.%(ext)s'
        ydl_opts = {'outtmpl': out_tmpl, 'writedescription': True, 'ignoreerrors': True}
        
        if is_audio:
            ydl_opts.update({
                'format': 'bestaudio/best', 'writethumbnail': True, 
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                    {'key': 'FFmpegMetadata', 'add_metadata': True}, 
                    {'key': 'EmbedThumbnail', 'already_have_thumbnail': False}, 
                ],
            })
        else:
            ydl_opts.update({'format': 'b[ext=mp4]/best'})

        print(f"🎬 מוריד {len(youtube_urls)} סרטונים מיוטיוב...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(youtube_urls)
        download_success = True

    # 2. טיפול בקישורי NotebookLM
    if notebook_urls:
        print(f"🧠 מזהה {len(notebook_urls)} קישורי שיתוף של NotebookLM...")
        for n_url in notebook_urls:
            if download_notebooklm_audio(n_url):
                download_success = True

    if not download_success:
        return False

    # הטמעת מילים לקובצי MP3 (ליוטיוב)
    for root, dirs, files in os.walk('downloads'):
        for f in files:
            if f.endswith('.mp3'):
                base_name = os.path.splitext(f)[0]
                desc_file = os.path.join(root, base_name + '.description')
                embed_lyrics_in_mp3(os.path.join(root, f), desc_file)

    # העלאה לגוגל דרייב ושיתוף
    links_res, ids_to_share = upload_to_drive(drive_svc, 'downloads', BASE_FOLDER_ID)
    
    for item_id in ids_to_share:
        try:
            drive_svc.permissions().create(fileId=item_id, body={'type': 'user', 'role': 'reader', 'emailAddress': sender_email}).execute()
        except Exception as e:
            pass

    if links_res:
        reply_link = links_res[0]
        reply_body = f"היי!\n\nהורדת קובצי השמע והתוצרים הסתיימה בהצלחה!\nהכל מוכן וממתין עבורך בתיקיית הדרייב כאן:\n{reply_link}\n\nהאזנה נעימה!"
        message = MIMEText(reply_body)
        message['to'] = sender_email
        message['subject'] = f"Re: {subject}"
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        gmail_svc.users().messages().send(userId='me', body={'raw': raw, 'threadId': msg['threadId']}).execute()
    
    shutil.rmtree('downloads', ignore_errors=True)
    return True

def main():
    drive_svc, gmail_svc = get_services()
    # הבוט סורק מיילים עם הנושאים הבאים
    query = 'is:unread {subject:יוטיוב subject:מחברת subject:קובץ}'
    results = gmail_svc.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        return

    for msg in messages:
        try:
            process_email(drive_svc, gmail_svc, msg['id'])
        except Exception as e:
            pass

if __name__ == "__main__":
    main()
