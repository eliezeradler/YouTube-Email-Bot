import os
import re
import base64
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
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=query, fields='files(id, webViewLink)').execute()
    if res.get('files'): return res['files'][0]['id'], res['files'][0]['webViewLink']
    
    metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    file = service.files().create(body=metadata, fields='id, webViewLink').execute()
    return file['id'], file['webViewLink']

def upload_to_drive(service, local_path, parent_drive_id):
    """פונקציה חכמה שמעלה תיקיות וקבצים ומחזירה רק את ה-ID של מה שהועלה באמת"""
    uploaded_links = []
    items_to_share = [] # רשימה של התיקיות/קבצים הספציפיים שנוצרו עכשיו
    
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
            # אם זו התיקייה העליונה ביותר של האמן/פלייליסט - נשמור את ה-ID שלה לשיתוף מדויק
            if rel_path == '.':
                items_to_share.append(folder_id)
                
        for f in files:
            file_path = os.path.join(root, f)
            if file_path.endswith('.description'): continue
            media = MediaFileUpload(file_path, resumable=True)
            file = service.files().create(body={'name': f, 'parents': [current_parent]}, media_body=media, fields='id, webViewLink').execute()
            uploaded_links.append(file['webViewLink'])
            # אם זה קובץ בודד ללא תיקייה שיושב ישירות ב-downloads
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

def process_email(drive_svc, gmail_svc, msg_id):
    msg = gmail_svc.users().messages().get(userId='me', id=msg_id).execute()
    headers = msg['payload']['headers']
    sender = next(h['value'] for h in headers if h['name'] == 'From')
    sender_email = re.search(r'[\w\.-]+@[\w\.-]+', sender).group()
    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "")
    
    body = ""
    if 'data' in msg['payload'].get('body', {}):
        body = base64.urlsafe_b64decode(msg['payload']['body']['data']).decode('utf-8', errors='ignore')
    elif 'parts' in msg['payload']:
        for part in msg['payload']['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')

    # סינון נקי של הקישור
    links = re.findall(r'(https?://(?:www\.)?youtube\.com/[^\s"\'<>]+|https?://youtu\.be/[^\s"\'<>]+)', body)
    if not links: return False
    url = links[0].rstrip(')]}.')

    is_video = "וידאו" in subject or "וידאו" in body
    is_audio = not is_video
    
    print(f"מעבד בקשה מ: {sender_email} | סוג: {'וידאו' if is_video else 'אודיו'}")

    out_tmpl = 'downloads/%(playlist_title,uploader|Unknown)s/%(album|Singles)s/%(title)s.%(ext)s'
    
    ydl_opts = {
        'outtmpl': out_tmpl,
        'writedescription': True,
        'ignoreerrors': True,
    }

    if is_audio:
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    else:
        ydl_opts.update({'format': 'b[ext=mp4]/best'})

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    if is_audio:
        for root, dirs, files in os.walk('downloads'):
            for f in files:
                if f.endswith('.mp3'):
                    base_name = os.path.splitext(f)[0]
                    desc_file = os.path.join(root, base_name + '.description')
                    embed_lyrics_in_mp3(os.path.join(root, f), desc_file)

    links_res, ids_to_share = upload_to_drive(drive_svc, 'downloads', BASE_FOLDER_ID)
    
    # שיתוף ספציפי אך ורק לקבצים או התיקיות החדשות שנוצרו (כך שתיקיית הבסיס נשארת חסויה)
    for item_id in ids_to_share:
        try:
            drive_svc.permissions().create(fileId=item_id, body={'type': 'user', 'role': 'reader', 'emailAddress': sender_email}).execute()
        except Exception as e:
            print(f"שגיאה בשיתוף פריט {item_id}: {e}")

    # ניסוח המייל בהתאם להצלחה או לכישלון ההורדה
    if not links_res:
        reply_body = "היי,\n\nנראה שמשהו השתבש בהורדה. ייתכן שהסרטון פרטי, נמחק, או שהקישור אינו תקין. אנא נסה שוב עם קישור אחר."
    else:
        reply_link = links_res[0]
        reply_body = f"היי!\n\nההורדה שלך הסתיימה בהצלחה. הקבצים סודרו בתיקיות ומוכנים עבורך כאן (ורק אתה מורשה לצפות בהם):\n{reply_link}\n\nהאזנה/צפייה נעימה!"
    
    message = MIMEText(reply_body)
    message['to'] = sender_email
    message['subject'] = f"Re: {subject}"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_svc.users().messages().send(userId='me', body={'raw': raw, 'threadId': msg['threadId']}).execute()

    gmail_svc.users().messages().batchModify(userId='me', body={'ids': [msg_id], 'removeLabelIds': ['UNREAD']}).execute()
    
    shutil.rmtree('downloads', ignore_errors=True)
    return True

def main():
    drive_svc, gmail_svc = get_services()
    results = gmail_svc.users().messages().list(userId='me', q='is:unread subject:יוטיוב').execute()
    messages = results.get('messages', [])

    if not messages:
        print("אין מיילים חדשים עם קישורים מיוטיוב.")
        return

    for msg in messages:
        try:
            process_email(drive_svc, gmail_svc, msg['id'])
        except Exception as e:
            print(f"שגיאה בעיבוד הודעה {msg['id']}: {e}")

if __name__ == "__main__":
    main()
