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
    """פונקציית עזר לשליחת אימיילים (הצלחה או שגיאה)"""
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
            if file_path.endswith('.description'): continue
            media = MediaFileUpload(file_path, resumable=True)
            file = service.files().create(body={'name': f, 'parents': [current_parent]}, media_body=media, fields='id, webViewLink').execute()
            uploaded_links.append(file['webViewLink'])
            if rel_path == '.':
                items_to_share.append(file['id'])
            
    return uploaded_links, items_to_share

def extract_body_from_payload(payload):
    body = ""
    if 'parts' in payload:
        for part in payload['parts']:
            body += extract_body_from_payload(part)
    elif 'body' in payload and 'data' in payload['body']:
        body += base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    return body

def download_notebooklm_audio(url, output_folder='downloads/NotebookLM'):
    os.makedirs(output_folder, exist_ok=True)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    html_content = response.text
    
    audio_urls = re.findall(r'https://storage\.googleapis\.com/notebooklm-public-prod[^"\s\'<>]+', html_content)
    if not audio_urls:
        audio_urls = re.findall(r'\"(https://[^\"\s]+?\.mp3.*?)\"', html_content)
        
    if not audio_urls:
        drive_ids = re.findall(r'https://docs\.google\.com/uc\?export=download&id=([a-zA-Z0-9_-]+)', html_content)
        if drive_ids:
            audio_urls = [f"https://docs.google.com/uc?export=download&id={drive_ids[0]}"]

    # 🔥 אם הוא לא מוצא את האודיו, הוא זורק שגיאה שמכילה את הטקסט שהוא קרא! 🔥
    if not audio_urls:
        debug_text = html_content[:1500] # לוקח את 1500 התווים הראשונים של האתר
        raise Exception(f"לא מצאתי קישור אודיו בקוד האתר. הנה מה שגוגל החזירה לי כשהצצתי בדף:\n\n{debug_text}")
        
    audio_url = audio_urls[0].replace('\\u0026', '&')
    
    file_res = requests.get(audio_url, stream=True, timeout=60)
    file_res.raise_for_status()
    
    title_match = re.search(r'<title>(.*?)</title>', html_content)
    title = title_match.group(1).replace(" - NotebookLM", "").strip() if title_match else "NotebookLM_Audio"
    title = re.sub(r'[\\/*?:"<>|]', "", title) 
    
    file_path = os.path.join(output_folder, f"{title}.mp3")
    with open(file_path, 'wb') as f:
        for chunk in file_res.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                
    return True

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
    
    if not links:
        return False
    
    urls = [link.rstrip(')]}.') for link in links]
    
    youtube_urls = [url for url in urls if 'youtube.com' in url or 'youtu.be' in url]
    notebook_urls = [url for url in urls if 'notebooklm.google' in url]

    os.makedirs('downloads', exist_ok=True)
    download_success = False

    try:
        if youtube_urls:
            is_audio = not ("וידאו" in subject or "וידאו" in body)
            out_tmpl = 'downloads/%(playlist_title,uploader,extractor_key|Unknown)s/%(album|Singles)s/%(title)s.%(ext)s'
            ydl_opts = {'outtmpl': out_tmpl, 'writedescription': True, 'ignoreerrors': True}
            
            if is_audio:
                ydl_opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]})
            else:
                ydl_opts.update({'format': 'b[ext=mp4]/best'})

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download(youtube_urls)
            download_success = True

        if notebook_urls:
            for n_url in notebook_urls:
                if download_notebooklm_audio(n_url):
                    download_success = True

        if not download_success:
            return False

        links_res, ids_to_share = upload_to_drive(drive_svc, 'downloads', BASE_FOLDER_ID)
        
        for item_id in ids_to_share:
            try: drive_svc.permissions().create(fileId=item_id, body={'type': 'user', 'role': 'reader', 'emailAddress': sender_email}).execute()
            except: pass

        if links_res:
            reply_body = f"היי!\n\nההורדה הסתיימה בהצלחה. הקבצים ממתינים לך כאן:\n{links_res[0]}\n\nתהנה!"
            send_email_reply(gmail_svc, sender_email, f"Re: {subject}", reply_body, msg['threadId'])
            
    except Exception as e:
        # 🔥 אם קורה משהו לא מתוכנן, הבוט שולח לך למייל את השגיאה המדויקת! 🔥
        error_details = traceback.format_exc()
        error_msg = f"היי,\n\nהבוט נתקל בבעיה טכנית בזמן שניסה לעבד את הבקשה שלך.\nהנה פרטי השגיאה (הלוג) כדי שתוכל לראות איפה זה נתקע:\n\n{error_details}"
        send_email_reply(gmail_svc, sender_email, f"שגיאה בעיבוד: {subject}", error_msg, msg['threadId'])

    finally:
        shutil.rmtree('downloads', ignore_errors=True)
    
    return True

def main():
    drive_svc, gmail_svc = get_services()
    query = 'is:unread {subject:יוטיוב subject:מחברת subject:קובץ}'
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
