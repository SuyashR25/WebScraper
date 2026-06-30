import asyncio
import os
import json
from src.crawler import crawl_site

def main():
    config_path = "config.json"
    target_url = ""
    output_dir = "./mirror_output"
    concurrency = 5
    
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        target_url = config.get("target_url", "")
        output_dir = config.get("output_dir", "./mirror_output")
        concurrency = config.get("concurrency", 5)
        
    if not target_url:
        target_url = input("Enter the target URL: ")
        
    output_dir = os.path.abspath(output_dir)
    print(f"[*] Starting to crawl {target_url} with concurrency {concurrency}...")
    asyncio.run(crawl_site(target_url, output_dir, concurrency))

if __name__ == "__main__":
    main()