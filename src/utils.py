import os
import re
import ssl
import urllib.request, urllib.parse, urllib.error
import http.client
import time

def download_binaries(url: str, dest_path: str, retries: int = 3):
    """Downloads a file with retries, bypassing SSL verification."""
    
    # Spoof User-Agent to prevent basic bot blocking
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }
    )

    # Bypass SSL verification for poorly configured endpoints
    ctx = ssl._create_unverified_context()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))

    temp_path = dest_path + ".tmp"

    for attempt in range(retries):
        try:
            with opener.open(req, timeout=60) as response:
                # Attempt to grab the real filename from Content-Disposition if provided
                cd_header = response.headers.get("Content-Disposition", "")
                if 'filename=' in cd_header:
                    match = re.search(r'filename="?([^";]+)"?', cd_header)
                    if match:
                        inferred_name = match.group(1)
                        dest_path = os.path.join(os.path.dirname(dest_path), inferred_name)
                        temp_path = dest_path + ".tmp"
                dir_path = os.path.dirname(dest_path)
                if dir_path:
                    os.makedirs(dir_path, exist_ok=True)

                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                    f.write(chunk)

            if os.path.exists(temp_path):
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                os.rename(temp_path, dest_path)
            return os.path.getsize(dest_path)
        
        except (http.client.IncompleteRead, TimeoutError, ConnectionError, urllib.error.URLError, ssl.SSLError) as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            
            if attempt < retries - 1:
                time.sleep(2 ** (attempt+ 1))
                continue
            else:
                raise e
            
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e

def url_to_local_path(url: str) -> str:
    """Safely converts a URL to a normalized local file path for storage."""
    parsed_url = urllib.parse.urlparse(url)
    netloc = parsed_url.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed_url.path.strip("/")

    if not path:
        path = "index.html"

    base, ext = os.path.splitext(path)
    allowed_extensions = [
        '.html', '.htm', '.xhtml', '.xml', '.txt', '.json',
        '.pdf', '.zip', '.tar', '.gz', '.tgz', '.exe', '.msi', '.dmg',
        '.xls', '.xlsx', '.doc', '.docx', '.ppt', '.pptx',
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico',
        '.mp4', '.mp3', '.wav', '.avi', '.mov', '.css', '.js'
    ]

    # Force .html extension on server-side scripts and parameter-heavy endpoints
    if not ext or ext.lower() not in allowed_extensions:
        if ext.lower() in ['.aspx', '.php', '.jsp', '.asp', '.do']:
            path = path + ".html"
        else:
            path = path + "/index.html"

    # Append sanitized query params and fragments to the filename to avoid collisions
    if parsed_url.query:
        cleaned_query = re.sub(r'[^a-zA-Z0-9_\-=]', '_', parsed_url.query)
        base, ext = os.path.splitext(path)
        path = f"{base}_{cleaned_query}{ext}"
    
    if parsed_url.fragment:
        cleaned_fragment = re.sub(r'[^a-zA-Z0-9_\-=]', '_', parsed_url.fragment)
        base, ext = os.path.splitext(path)
        path = f"{base}_hash_{cleaned_fragment}{ext}"

    path = path.replace("\\", "/").replace("//", "/")

    full_path = f"{netloc}/{path}"

    full_path = re.sub(r'[^a-zA-Z0-9_\-./]', '_', full_path)

    full_path = os.path.normpath(full_path)

    return full_path
