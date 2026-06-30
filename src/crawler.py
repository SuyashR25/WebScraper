import asyncio
import os
import sys
import mimetypes
import urllib.parse
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from src.utils import download_binaries, url_to_local_path

if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8")

cache_lock = asyncio.Lock()
pdf_mapping_lock = asyncio.Lock()

_seen_urls = set()

async def append_to_error_log(root: str, url: str, error_msg: str):
    """Logs failed URLs and errors to maintain an audit trail."""
    async with cache_lock:
        cache_dir = os.path.join(root, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        error_log = os.path.join(cache_dir, "error_log.txt")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(error_log, "a", encoding="utf-8") as f:
            f.write(f"{now}\t{url}\t{error_msg}\n")

async def append_to_cache_log(root: str, url: str, local_path: str, file_size: int, status_code: int = 200, mime_type: str = "text/html"):
    """Logs downloaded URLs to prevent redundant fetching and maintain history."""
    global _seen_urls
    async with cache_lock:
        cache_dir = os.path.join(root, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        log_file = os.path.join(cache_dir, "cache_log.txt")

        if _seen_urls is None:
            _seen_urls = set()
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.startswith("#") and "\t" in line:
                            parts = line.strip().split("\t")
                            if len(parts) >= 8:
                                _seen_urls.add(parts[7])

        if url in _seen_urls:
            return
        _seen_urls.add(url)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{now}\t{file_size}\t-\t{status_code}\tOK\t{mime_type}\t-\t{url}\t{local_path}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)

async def append_to_asset_mapping(root: str, asset_url: str, source_url: str, local_path: str):
    """Tracks relationships between downloaded assets and their source pages."""
    async with pdf_mapping_lock:
        cache_dir = os.path.join(root, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        mapping_file = os.path.join(cache_dir, "asset_mapping.txt")

        if not os.path.exists(mapping_file):
            with open(mapping_file, "w", encoding="utf-8") as f:
                f.write("# Asset URL\tSource Page URL\tLocal Relative Path\n")

        with open(mapping_file, "a", encoding="utf-8") as f:
            f.write(f"{asset_url}\t{source_url}\t{local_path}\n")

async def crawl_site(entry_url: str, root: str, concurrency: int = 5):
    """Main orchestrator: sets up Playwright browser and spawns async workers."""

    global _seen_urls
    _seen_urls = None

    parsed_entry_url = urllib.parse.urlparse(entry_url)
    allowed_domains = parsed_entry_url.netloc.lower()

    print(f"[*] Starting engine on domain: {allowed_domains} (Workers: {concurrency})")

    queue = asyncio.Queue()
    await queue.put(entry_url)

    visited = {entry_url}
    visited_lock = asyncio.Lock()
    seen_asset_mapping = set()
    pending_asset_mapping = {}
    pages_crawled_count = 0
    pages_crawled_lock = asyncio.Lock()

    cache_dir = os.path.join(root, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    log_file = os.path.join(cache_dir, "cache_log.txt")

    if not os.path.exists(log_file):
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("# date \t size \t flags \t statuscode \t statusmsg \t MIME \t Etag \t URL \t localfile\n")

    async with async_playwright() as p:
        print("[*] Launching headless browser...")
        browser = await p.firefox.launch(headless=True, args=["--window-size=1920,1080"])

        async def worker(worker_id: int):
            """Core crawler task: fetches pages, handles SPA routes, pagination, and file downloads."""
            nonlocal pages_crawled_count
            print(f"[*] Worker {worker_id} Initialized.")

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-User": "?1",
                    "Sec-Fetch-Dest": "document",
                    "Upgrade-Insecure-Requests": "1"
                }
            )

            # Hijack History API to capture SPA routing and evade basic bot detection
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                window.__captured_routes__ = [];
                const origPush = history.pushState.bind(history);
                history.pushState = function(state, title, url) {
                    window.__captured_routes__.push(url);
                    return origPush(state, title, url);
                };
                const origRep = history.replaceState.bind(history);
                history.replaceState = function(state, title, url) {
                    window.__captured_routes__.push(url);
                    return origRep(state, title, url);
                };
            """)

            page = await context.new_page()
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))

            pages_processed = 0

            while True:
                current_url = await queue.get()
                # Periodically recycle page contexts to prevent Playwright memory leaks
                if pages_processed >= 100:
                    print(f"[Worker {worker_id}] Recycling page context to free up browser memory...")
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = await context.new_page()
                    page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
                    pages_processed = 0

                parsed_current = urllib.parse.urlparse(current_url)
                netloc = parsed_current.netloc.lower()
                if netloc.startswith("www."):
                    netloc = netloc[4:]
                
                normalized_url = urllib.parse.urlunparse((
                    parsed_current.scheme,
                    netloc,
                    parsed_current.path,
                    parsed_current.params,
                    parsed_current.query,
                    parsed_current.fragment # preserve fragment for JS tabs
                ))

                async with pages_crawled_lock:
                    pages_crawled_count += 1
                
                print(f"[Worker {worker_id}] Crawling ({pages_crawled_count}): {normalized_url}")
                print("[*] Queue size:", queue.qsize())

                try:
                    local_relative_path = url_to_local_path(normalized_url)
                    local_absolute_path = os.path.join(root, local_relative_path)

                    is_cached = False
                    rendered_html  = ""
                    status_code = 200
                    file_size = 0

                    is_binary = any(normalized_url.lower().split('?')[0].endswith(ext) for ext in [
                        '.pdf', '.zip', '.tar', '.gz', '.tgz', '.exe', '.msi', '.dmg',
                        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico',
                        '.mp4', '.mp3', '.wav', '.avi', '.mov', '.docx', '.xlsx', '.pptx',
                        '.doc', '.xls', '.ppt', '.css', '.js', '.json'
                    ])

                    if os.path.exists(local_absolute_path) and os.path.getsize(local_absolute_path) > 0:
                        is_cached = True
                        file_size = os.path.getsize(local_absolute_path)
                        if not is_binary:
                            try:
                                with open(local_absolute_path, "r", encoding="utf-8") as f:
                                    rendered_html = f.read()
                            except Exception as e:
                                print(f"[Worker {worker_id}] Error reading cached HTML for {normalized_url}: {e}")
                                is_cached = False  
                    
                    if is_binary:
                        if not is_cached:
                            try:
                                file_size = await asyncio.to_thread(download_binaries, normalized_url, local_absolute_path)
                                status_code = 200

                                mime_type, _ = mimetypes.guess_type(normalized_url.split('?')[0])
                                mime_type = mime_type or "application/octet-stream"

                                await append_to_cache_log(root, normalized_url, local_relative_path, file_size, status_code, mime_type)
                                pages_processed += 1

                                async with visited_lock:
                                    pending_sources = pending_asset_mapping.pop(normalized_url, [])
                                    for source_url in pending_sources:
                                        await append_to_asset_mapping(root, normalized_url, source_url, local_relative_path)
                                        seen_asset_mapping.add((normalized_url, source_url))

                            except Exception as dl_err:
                                print(f"[Worker {worker_id}] [!] Failed to download binary {normalized_url}: {dl_err}")
                                await append_to_error_log(root, normalized_url, f"Binary download failed: {dl_err}")
                                # Discard deferred mappings — file was never saved
                                async with visited_lock:
                                    pending_asset_mapping.pop(normalized_url, None)
                                continue
                        else:
                            print(f"[Worker {worker_id}] [Cache Hit] Skipped fetch for existing local binary: {normalized_url}")
                            
                        continue

                    if not is_cached:
                        response = None
                        status_code = 0
                        for goto_attempt in range(3):
                            try:
                                response = await page.goto(normalized_url, wait_until="networkidle", timeout=30000)
                                status_code = response.status if response else 0
                                break
                            except Exception as goto_err:
                                print(f"[Worker {worker_id}] Timeout/Error on attempt {goto_attempt+1} for {normalized_url}: {goto_err}")
                                if goto_attempt == 2:
                                    await append_to_error_log(root, normalized_url, f"Goto failed: {goto_err}")
                                    break
                                await asyncio.sleep(2)

                        if response is None:
                            continue

                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)

                        except Exception:
                            pass

                        if response.status >= 400:
                            print(f"[Worker {worker_id}] [!] Failed status {response.status} for {normalized_url}")
                            await append_to_error_log(root, normalized_url, f"HTTP {response.status}")
                            continue

                        try:
                            # Scroll down the page multiple times to trigger lazy-loaded elements
                            prev_height = 0
                            for _ in range(5):
                                current_height = await page.evaluate("document.body.scrollHeight")
                                if current_height == prev_height:
                                    break
                                prev_height = current_height
                                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                await asyncio.sleep(1)
                                prev_height = current_height

                        except Exception:
                            pass

                        try:
                            await page.evaluate("""
                                document.querySelectorAll('[href]').forEach(el => {
                                    try { el.setAttribute('href', el.href); } catch(e) {}
                                });
                                document.querySelectorAll('[src]').forEach(el => {
                                    try { el.setAttribute('src', el.src); } catch(e) {}
                                });
                            """)
                        except Exception: pass

                        rendered_html = await page.content()

                        try:
                            # Force click on interactable elements to trigger SPA state changes
                            await page.evaluate("""
                                document.querySelectorAll('button, [role="button"], .nav-link, .nav-item, .MuiTab-root, .tab, .img-thumbnail, div[data-toggle="tooltip"], .item-title, .tour-name').forEach(el => {
                                    try { 
                                        // Avoid clicking inside forms to prevent accidental submissions
                                        if (!el.closest('form')) {
                                            el.click(); 
                                        }
                                    } catch(e) {}
                                });
                            """)
                            # Give the SPA time to process clicks and fire pushState
                            await asyncio.sleep(1.0)
                        except Exception: 
                            pass

                        spa_routes = []
                        try:
                            spa_routes = await page.evaluate("window.__captured_routes__ || []")
                        except Exception:
                            pass

                        for route in spa_routes:
                            if route and isinstance(route, str):
                                absolute_route = urllib.parse.urljoin(normalized_url, route)

                                async with visited_lock:
                                    if absolute_route not in visited:
                                        visited.add(absolute_route)
                                        await queue.put(absolute_route)

                        file_size = len(rendered_html.encode("utf-8"))
                        
                        dir_path = os.path.dirname(local_absolute_path)
                        if dir_path:
                            os.makedirs(dir_path, exist_ok=True)
                        with open(local_absolute_path, "w", encoding="utf-8") as f:
                            f.write(rendered_html)

                        await append_to_cache_log(root, normalized_url, local_relative_path, file_size, status_code, "text/html")
                        pages_processed += 1
                    
                    else:
                        print(f"[Worker {worker_id}] [Cache Hit] Loaded local content for: {normalized_url}")
                    
                    htmls_to_parse = [rendered_html]

                    # Pagination handling
                    if not is_cached:
                        page_num = 1
                        previous_html = rendered_html
                        # Repeatedly click the "Next" button to exhaust pagination links
                        while True:
                            # Check if a 'Next' pagination button exists broadly
                            next_button = page.locator("a[title='Next'], a:has-text('Next'), a[aria-label='Next page'], a.pagination-next").first
                            if not await next_button.is_visible():
                                break
                                
                            page_num += 1
                            print(f"[Worker {worker_id}] [Pagination] Clicking to Page {page_num} for {normalized_url}")
                            await next_button.click()
                            
                            try:
                                await page.wait_for_load_state("networkidle", timeout=5000)
                            except Exception:
                                pass
                            await asyncio.sleep(1.0) # Settle delay
                            
                            new_html = await page.content()
                            
                            # Break if page hasn't changed (prevents infinite loops on broken pagination)
                            if new_html == previous_html:
                                print(f"[Worker {worker_id}] [Pagination] Page hasn't changed on Page {page_num}. Stopping.")
                                break
                                
                            previous_html = new_html
                            htmls_to_parse.append(new_html)
                            
                            # Create a fake URL so downstream RAG treats it as unique
                            faked_url = f"{normalized_url}&page={page_num}" if "?" in normalized_url else f"{normalized_url}?page={page_num}"
                            
                            # Apply the requested filename format "- 2", "- 3", etc.
                            base_path, ext = os.path.splitext(url_to_local_path(normalized_url))
                            local_relative_path = f"{base_path} - {page_num}{ext}"
                            local_absolute_path = os.path.join(root, local_relative_path)
                            
                            file_size = len(new_html.encode('utf-8'))
                            
                            dir_path = os.path.dirname(local_absolute_path)
                            if dir_path: os.makedirs(dir_path, exist_ok=True)
                            with open(local_absolute_path, "w", encoding="utf-8") as f:
                                f.write(new_html)
                                
                            await append_to_cache_log(root, faked_url, local_relative_path, file_size, status_code, "text/html")

                    discovered_links = 0
                    for html_to_parse in htmls_to_parse:
                        soup = BeautifulSoup(html_to_parse, "html.parser")

                        elements = soup.find_all(lambda tag: tag.has_attr('href') or tag.has_attr('data-href') or tag.has_attr('data-url') or tag.has_attr('src') or tag.has_attr('data-src') or tag.has_attr('action'))

                        for element in elements:

                            href = ""
                            if element.has_attr('href'):
                                href = element['href']
                            elif element.has_attr('data-href'):
                                href = element['data-href']
                            elif element.has_attr('data-url'):
                                href = element['data-url']
                            elif element.has_attr('src'):
                                href = element['src']
                            elif element.has_attr('data-src'):
                                href = element['data-src']
                            elif element.has_attr('action'):
                                href = element['action']

                            href = href.strip()
                            if not href or href.startswith("javascript:") or href.startswith("mailto:") or href.startswith("#"):
                                continue

                            absolute_target = urllib.parse.urljoin(normalized_url, href)
                            parsed_target = urllib.parse.urlparse(absolute_target)

                            base_domain = allowed_domains[4:] if allowed_domains.startswith("www.") else allowed_domains
                            if parsed_target.netloc.lower() in  [base_domain, f"www.{base_domain}"]:
                                t_netloc = parsed_target.netloc.lower()
                                if t_netloc.startswith("www."): 
                                    t_netloc = t_netloc[4:]
                                
                                target_normalized = urllib.parse.urlunparse((
                                    parsed_target.scheme,
                                    t_netloc,
                                    parsed_target.path,
                                    parsed_target.params,
                                    parsed_target.query,
                                    parsed_target.fragment # preserve fragment for JS tabs
                                ))

                                # Convert literal space characters to standard URL-encoded %20 to prevent download crashes
                                target_normalized = target_normalized.replace(" ", "%20")

                                is_binary_link = any(target_normalized.lower().split('?')[0].endswith(ext) for ext in [
                                '.pdf', '.zip', '.tar', '.gz', '.tgz', '.exe', '.msi', '.dmg',
                                '.xls', '.xlsx', '.doc', '.docx', '.ppt', '.pptx',
                                '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico',
                                '.mp4', '.mp3', '.wav', '.avi', '.mov', '.css', '.js', '.json'
                                ])

                                if is_binary_link:
                                    mapping_tuple = (target_normalized, normalized_url)
                                    local_rel = url_to_local_path(target_normalized)
                                    target_abs = os.path.join(root, local_rel)
                                    should_write_now = False
                                    async with visited_lock:
                                        if mapping_tuple not in seen_asset_mapping:
                                            seen_asset_mapping.add(mapping_tuple)
                                            if os.path.exists(target_abs):
                                                # File already on disk (cache hit) — safe to write mapping now
                                                should_write_now = True
                                            else:
                                                # Defer mapping until after the download succeeds
                                                pending_asset_mapping.setdefault(target_normalized, []).append(normalized_url)
                                    if should_write_now:
                                        await append_to_asset_mapping(root, target_normalized, normalized_url, local_rel)

                                async with visited_lock:
                                    if target_normalized not in visited:
                                        visited.add(target_normalized)
                                        await queue.put(target_normalized)

                except Exception as e:
                    print(f"[Worker {worker_id}] [!] Error on '{normalized_url}': {e}")
                    await append_to_error_log(root, normalized_url, str(e))
                    print(f"[Worker {worker_id}] Re-initializing worker page state to recover from error...")
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = await context.new_page()
                    page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
                    pages_processed = 0
                    
                finally:
                    # Notify queue that page is fully completed
                    queue.task_done()

        print(f"[*] Starting {concurrency} crawler worker threads...")
        workers = []
        for i in range(concurrency):
            task = asyncio.create_task(worker(i))
            workers.append(task)
            
        # Wait until all queued items are processed
        await queue.join()
        
        # Stop workers
        print("\n[*] All queued URLs successfully processed. Stopping workers...")
        for task in workers:
            task.cancel()
            
        await asyncio.gather(*workers, return_exceptions=True)
        await browser.close()
