# WebScraper

An asynchronous web crawler designed to recursively download and mirror full websites for offline use. It is specifically built to handle modern Single Page Applications (SPAs), dynamic content, and binary assets.

## Technical Implementation

### Asynchronous Architecture
The crawler utilizes Python's `asyncio` coupled with `playwright.async_api` to achieve concurrency. A centralized queue manages discovered URLs, and multiple worker threads process the queue simultaneously. This prevents the crawler from blocking on network requests or page rendering.

### WebDriver & Rendering
- **Playwright (Firefox):** Operates in headless mode to fully execute client-side JavaScript. This allows the crawler to effortlessly scrape modern Single Page Applications (SPAs) built on dynamic frameworks like React, Vue, and Angular, which traditional HTML parsers cannot read. 
- **Context Recycling:** To mitigate memory leaks inherent in long-running browser sessions, each worker automatically closes and recycles its page context after processing 100 pages.
- **Bot Evasion:** Injects scripts to mask the `navigator.webdriver` property and spoofs common User-Agents to bypass basic bot protection.

### Dynamic Content Handling
- **SPA Routing Capture:** Injects a script to hijack the browser's History API (`pushState` and `replaceState`). This allows the crawler to discover virtual routes triggered by client-side JavaScript without full page reloads.
- **Interaction Emulation:** Automatically queries and clicks interactable DOM elements (buttons, tabs, navigation links) to trigger hidden content or state changes, explicitly avoiding form submissions.
- **Lazy Loading:** Emulates user scrolling to the bottom of the page up to 5 times to force lazy-loaded images or DOM elements to appear before capturing the HTML.
- **Pagination:** Detects standard "Next" buttons and iterates through pagination lists iteratively until the page content stops changing or the button disappears.

### Asset and File Management
- **Binary Downloading:** Static assets and binary files (PDFs, images, ZIPs) are bypassed from the headless browser to save memory and are instead downloaded via `urllib` with automatic retry logic and SSL verification disabled.
- **Cache System:** Maintains local copies of all fetched HTML and binaries. It checks `cache_log.txt` and file sizes before initiating network requests to prevent redundant fetches on subsequent runs.
- **Path Sanitization:** Translates complex URLs containing query parameters and hash fragments into safe, normalized local directory structures.

## Current Limitations & QA Design Risks

- **Visual Fidelity (CSS):** While the tool mirrors the full website structure and content, some stylesheets (CSS files) might be missed if dynamically loaded or heavily obfuscated. The primary focus of the scraper is comprehensive data and asset extraction rather than pixel-perfect offline rendering.
- **Advanced CAPTCHAs:** While basic bot evasion is implemented, the crawler will fail against advanced anti-bot challenges (e.g., Cloudflare Turnstile, Datadome).
- **Deep Infinite Scrolling:** The lazy-loading scroll logic is hardcapped at 5 iterations. Pages requiring deeper infinite scrolling will not be fully captured.
- **Form Submissions:** The crawler intentionally avoids interacting with `<form>` elements to prevent accidental data submission or destructive actions. Pages strictly requiring POST requests or logins cannot be scraped.
- **Broad Click Triggers (Hazard):** The interaction emulator blindly clicks on elements with `role="button"` or `.nav-link` classes. **Risk Warning:** The naive form check does not protect against non-form buttons that trigger destructive side effects (e.g., "Add to cart", "Delete", "Logout", or payment API calls). Running this blindly against a live, stateful application (vs. a sandbox or documentation site) is a genuine hazard.
- **Pagination False Positives:** If a site's pagination updates a minor timestamp but fails to load new content, the loop breaker might fail to realize it is stuck on the same page.
- **SSL Verification Disabled:** For binary downloads, SSL certificate validation is globally disabled to easily scrape targets with poorly configured cert chains. In a strict production environment, this introduces MITM risks and should be configurable.
- **Failure Handling:** If a page fails to load after 3 attempts or returns a >=400 HTTP status, the failure is tracked in `cache/error_log.txt` and the URL is permanently dropped. Requeuing logic for intermittent failures is currently out of scope.

## Usage

Ensure the requirements are installed:
```bash
pip install -r requirements.txt
playwright install firefox
```

Configure the scraper by editing `config.json`:
```json
{
    "target_url": "",
    "output_dir": "./mirror_output",
    "concurrency": 5
}
```
*(If `target_url` is left empty, the script will prompt you for it when executed.)*

Run the scraper:
```bash
python main.py
```
